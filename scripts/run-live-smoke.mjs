#!/usr/bin/env node
// Live-provider smoke lane
// (`specs/workspace.md` Amendment 2026-08-26; plan T8).
//
//   node scripts/run-live-smoke.mjs [options]
//
// One command: refuse to start without a real credential, otherwise
// boot the workspace on the CONFIGURED provider, send at least one
// real question, read the SSE wire the SPA reads, and report the
// fidelity observables the stream contract promises.
//
//   0  every observable held
//   1  a fidelity observable regressed (no streamed deltas, no typed
//      terminal status, thinking prose leaking into the answer, a
//      citation set that contradicts the status)
//   2  the lane could not run (server never came up, thread could not
//      be created, browser harness missing) — never reported as a pass
//   3  ANTHROPIC_AUTH_TOKEN is missing: nothing was started
//
// Why exit 3 exists, and why the check lives HERE: `fin.host.compile`
// resolves the provider key as
// `ANTHROPIC_AUTH_TOKEN or "sk-no-key-required"`, so an unset token
// does not stop the boot — it reaches the provider as a placeholder
// and comes back as a 401 that is indistinguishable from a revoked
// key, a wrong base URL, or an outage. This lane therefore owns the
// credential gate: it aborts before it starts anything, naming the
// variable, so a green run always means a real question reached a
// real provider. Credential VALUES are never printed.
//
// Never part of default `uv run pytest -q`: the lane lives in
// `scripts/`, is reached only by the command above, and starts its own
// server on a free port.

import path from "node:path";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import {
  REPO_ROOT,
  ensureSpaBuild,
  exists,
  freePort,
  readCompileDefaults,
  run,
  startWorkspace,
  stopWorkspace,
  tailLog,
  waitForWorkspace,
} from "./lib/workspace-server.mjs";

const RUNNER = path.join(REPO_ROOT, "scripts", "run-stream-checks.mjs");
const DOTENV = path.join(REPO_ROOT, ".env");

const EXIT_OK = 0;
const EXIT_CHECK_FAILED = 1;
const EXIT_LANE = 2;
const EXIT_MISSING_CREDENTIAL = 3;

// The one variable this lane cannot run without. Named in the abort
// message and in `specs/workspace.md` Amendment 2026-08-26.
const CREDENTIAL_VAR = "ANTHROPIC_AUTH_TOKEN";

// A scripted transcript would make every observable below pass while
// proving nothing about the provider, so the opt-in that selects it
// (T7) is removed from the server child's environment here.
const SCRIPTED_OPT_IN = "FIN_SCRIPTED_MODEL";

const DEFAULT_QUESTION = "What was Apple's total revenue in Q3 2022?";

const USAGE = `Usage: node scripts/run-live-smoke.mjs [options]

Options:
  --question=TEXT     the question to send      (default: the AAPL 2022 Q3 revenue ask)
  --model=NAME        override FIN_MODEL for this run
  --base-url=URL      override FIN_BASE_URL for this run
  --port=N            serve the workspace here  (default: a free port)
  --boot-timeout=N    seconds to wait for the server        (default 120)
  --run-timeout=N     seconds to wait for the terminal frame (default 300)
  --require-thinking  a provider that emits no reasoning fails the lane
  --require-done      only a done turn passes (use where retrieve is wired;
                      by default any typed terminal status is a pass)
  --browser-checks    also run scripts/run-stream-checks.mjs against this
                      live workspace (needs a built SPA + playwright-cli)
  --keep-server       leave the workspace serving after the verdict
  --json              print the raw report as JSON instead of the table
  -h, --help          this text

Requires ${CREDENTIAL_VAR} in the environment (or in ./.env, which this
lane applies to the server child the way \`fin-serve\` does). Without it
the lane exits ${EXIT_MISSING_CREDENTIAL} and starts nothing — it never skips quietly.
Provider selection is read from FIN_MODEL / FIN_BASE_URL, in the
environment or in that same ./.env; no credential value is ever printed.`;

function log(message) {
  console.log(`live-smoke: ${message}`);
}

function laneError(message) {
  console.error(`live-smoke: ${message}`);
  return EXIT_LANE;
}

function parseArgs(argv) {
  const opts = {
    question: DEFAULT_QUESTION,
    model: "",
    baseUrl: "",
    port: 0,
    bootTimeoutMs: 120_000,
    runTimeoutMs: 300_000,
    requireThinking: false,
    requireDone: false,
    browserChecks: false,
    keepServer: false,
    json: false,
    help: false,
  };
  for (const arg of argv) {
    const [flag, value] = arg.startsWith("--") ? arg.slice(2).split("=", 2) : [arg, ""];
    switch (flag) {
      case "question": opts.question = value; break;
      case "model": opts.model = value; break;
      case "base-url": opts.baseUrl = value; break;
      case "port": opts.port = Number(value); break;
      case "boot-timeout": opts.bootTimeoutMs = Number(value) * 1_000; break;
      case "run-timeout": opts.runTimeoutMs = Number(value) * 1_000; break;
      case "require-thinking": opts.requireThinking = true; break;
      case "require-done": opts.requireDone = true; break;
      case "browser-checks": opts.browserChecks = true; break;
      case "keep-server": opts.keepServer = true; break;
      case "json": opts.json = true; break;
      case "h": case "-h": case "help": opts.help = true; break;
      default: throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!opts.question.trim()) throw new Error("--question must not be empty");
  if (!Number.isInteger(opts.port) || opts.port < 0 || opts.port > 65_535) {
    throw new Error("--port must be a TCP port number");
  }
  if (!Number.isFinite(opts.bootTimeoutMs) || opts.bootTimeoutMs < 1_000) {
    throw new Error("--boot-timeout must be at least 1 second");
  }
  if (!Number.isFinite(opts.runTimeoutMs) || opts.runTimeoutMs < 1_000) {
    throw new Error("--run-timeout must be at least 1 second");
  }
  return opts;
}

// ----- operator ./.env -----

// Same shape `fin.host.operator_env.parse_env_file` reads, because
// `childEnv` below fills the server child from this file exactly as
// `fin-serve` does: a token that lives only in that file is a token
// the run really has, and aborting on it would be a false alarm.
// Values are parsed, never logged.
function parseEnvFile(text) {
  const pairs = new Map();
  for (const raw of text.split("\n")) {
    let line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice("export ".length).trim();
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    if (!key) continue;
    let value = line.slice(eq + 1).trim();
    if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === '"' || value[0] === "'")) {
      value = value.slice(1, -1);
    }
    pairs.set(key, value);
  }
  return pairs;
}

// Read the file once: the credential gate and the child environment
// must agree about what it says, or the lane could abort on a token
// the server would have had (or boot a provider the report never
// names).
async function readDotenv(dotenvPath = DOTENV) {
  if (!(await exists(dotenvPath))) return new Map();
  try {
    return parseEnvFile(await readFile(dotenvPath, "utf8"));
  } catch {
    /* unreadable .env is the same as absent — the server logs it too */
    return new Map();
  }
}

// ----- credential gate -----

// Whitespace-only counts as missing: `_read_env` in
// `fin.host.compile` treats a blank the same way and falls through to
// the placeholder key, which is exactly the silent path this gate
// exists to close.
function resolveCredential(name, { env = process.env, dotenv, dotenvPath = DOTENV }) {
  const fromProcess = env[name];
  if (typeof fromProcess === "string" && fromProcess.trim() !== "") {
    return { present: true, source: "the process environment", value: fromProcess };
  }
  const fromFile = dotenv.get(name);
  if (typeof fromFile === "string" && fromFile.trim() !== "") {
    return { present: true, source: `${dotenvPath} (applied to the server child)`, value: fromFile };
  }
  return { present: false, source: "", value: "" };
}

function abortMissingCredential() {
  const lines = [
    `${CREDENTIAL_VAR} is not set — this lane will not run without it.`,
    "",
    "Nothing was started: no workspace server, no browser, no provider call.",
    "This is an abort, not a skip. A green run of this lane must mean a real",
    "question reached the configured provider, so the lane refuses to produce",
    "a verdict it cannot stand behind.",
    "",
    `The check lives here rather than in the server because fin.host.compile`,
    `falls back to a placeholder key when ${CREDENTIAL_VAR} is unset, so a`,
    "missing credential would otherwise surface as an ordinary provider 401.",
    "",
    "Set it (the value is never printed by this lane) and re-run:",
    `  export ${CREDENTIAL_VAR}=...        # or add it to ./.env`,
    "  node scripts/run-live-smoke.mjs",
    "",
    "The credential-free browser lane is `node scripts/run-scripted-e2e.mjs`.",
  ];
  for (const line of lines) console.error(line ? `live-smoke: ${line}` : "live-smoke:");
  return EXIT_MISSING_CREDENTIAL;
}

// ----- provider config (names and non-secret values only) -----

function safeOrigin(url) {
  if (!url) return "";
  try {
    // `origin` drops any userinfo, so a URL carrying an inline
    // credential cannot reach the console through this line.
    return new URL(url).origin;
  } catch {
    return "(unparseable URL)";
  }
}

// `fin-serve` calls `fin.host.operator_env.apply_dotenv` before it
// binds, so for an operator the ./.env file IS the configured
// provider. This lane cannot boot through `fin-serve` — that entry
// point hardcodes 0.0.0.0:8000 and the lane needs a free port — so it
// starts `uvicorn fin.host.http:app`, and importing the app never
// applies the file. Filling it here is what keeps "the CONFIGURED
// provider" true: without it the child boots on `compile.py`'s own
// `DEFAULT_MODEL` / `DEFAULT_BASE_URL` pair, which is a different
// provider than the one the operator configured and fails in ways that
// have nothing to do with stream fidelity. `setdefault` semantics,
// exactly like `apply_dotenv`: an
// explicit process variable still wins.
function childEnv({ port, model, baseUrl, credential, dotenv }) {
  const env = { ...process.env };
  for (const [key, value] of dotenv) {
    if (!(key in env)) env[key] = value;
  }
  env[CREDENTIAL_VAR] = credential;
  delete env[SCRIPTED_OPT_IN];
  if (model) env.FIN_MODEL = model;
  if (baseUrl) env.FIN_BASE_URL = baseUrl;
  env.FIN_E2E_BASE_URL = `http://127.0.0.1:${port}`;
  return env;
}

// ----- the live run -----

function parseSseChunk(buffer) {
  const frames = [];
  let rest = buffer;
  let boundary = rest.indexOf("\n\n");
  while (boundary >= 0) {
    const block = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (data.length > 0) {
      let payload = null;
      try {
        payload = JSON.parse(data.join("\n"));
      } catch {
        payload = { unparsed: data.join("\n") };
      }
      frames.push({ event, data: payload });
    }
    boundary = rest.indexOf("\n\n");
  }
  return { frames, rest };
}

async function sendQuestion(baseUrl, { threadId, question, runTimeoutMs, onFrame }) {
  const startedAt = Date.now();
  const response = await fetch(`${baseUrl}/api/threads/${threadId}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({ question, thinking: true }),
    signal: AbortSignal.timeout(runTimeoutMs),
  });
  if (!response.ok) {
    throw new Error(`POST /api/threads/{id}/messages -> HTTP ${response.status}`);
  }
  const contentType = response.headers.get("content-type") ?? "";
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";
  let truncated = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseChunk(buffer);
      buffer = parsed.rest;
      for (const frame of parsed.frames) onFrame({ ...frame, atMs: Date.now() - startedAt });
    }
  } catch (error) {
    truncated = error && error.message ? error.message : String(error);
  }
  return { contentType, truncated, elapsedMs: Date.now() - startedAt };
}

const THINKING_MARKUP = /<\/?think(ing)?\b/i;

// `workspace_http.py` wraps ANY mid-flight exception — an unreachable
// provider, a node crash — into a refuse whose reason is
// `stream raised mid-flight: <ExceptionType>`. That turn never
// happened, so none of the observables below is saying anything about
// the stream contract: an unreachable provider would otherwise print
// three PASS lines and one FAIL and point the operator at the wire
// instead of at the connection. It is exit 2, "could not run".
const CRASH_REFUSE_PREFIX = "stream raised mid-flight:";

// The wire as the SPA sees it: everything before the terminal `status`
// frame is the live stream, everything after it is the authoritative
// close (`status` -> `answer` -> `citations`, spec line 30).
function readWire(frames) {
  const statusIndex = frames.findIndex((f) => f.event === "status");
  const before = statusIndex < 0 ? frames : frames.slice(0, statusIndex);
  const after = statusIndex < 0 ? [] : frames.slice(statusIndex + 1);
  const deltas = before.filter((f) => f.event === "answer");
  const thinking = frames.filter((f) => f.event === "thinking");
  const citationsFrame = after.find((f) => f.event === "citations");
  const status = frames.find((f) => f.event === "status")?.data ?? null;
  return {
    statusIndex,
    deltas,
    deltaText: deltas.map((f) => f.data?.text ?? "").join(""),
    thinking,
    thinkingText: thinking.map((f) => f.data?.text ?? "").join(""),
    // Reasoning belongs to the live trace, so a thinking frame after
    // the terminal status is a wire-order regression, not late news.
    lateThinking: after.filter((f) => f.event === "thinking").length,
    statusFrames: frames.filter((f) => f.event === "status").length,
    status,
    statusValue: status?.status ?? "",
    statusAtMs: frames[statusIndex]?.atMs ?? null,
    track: status?.track ?? "",
    terminalAnswer: after.find((f) => f.event === "answer")?.data?.text ?? "",
    citationsFrame,
    citations: Array.isArray(citationsFrame?.data) ? citationsFrame.data : null,
  };
}

function checkAnswerStreamed(wire, { contentType, truncated }) {
  return {
    id: "1-answer-streamed",
    state:
      wire.deltas.length >= 2 && wire.deltaText.trim().length > 0 && !truncated
        ? "pass"
        : "fail",
    detail: {
      deltaFrames: wire.deltas.length,
      deltaChars: wire.deltaText.length,
      firstDeltaMs: wire.deltas[0]?.atMs ?? null,
      lastDeltaMs: wire.deltas[wire.deltas.length - 1]?.atMs ?? null,
      contentType,
      truncated: truncated || null,
    },
  };
}

function checkTerminalStatus(wire, { truncated, requireDone }) {
  // Any typed terminal status is a fidelity pass: refuse and step_cap
  // are contract outcomes, not lane failures. `--require-done` is for
  // the operator who knows retrieve is wired and wants the answer too.
  const accepted = requireDone ? ["done"] : ["done", "refuse", "step_cap"];
  return {
    id: "2-terminal-status",
    state:
      wire.statusFrames === 1 &&
      accepted.includes(wire.statusValue) &&
      wire.citationsFrame !== undefined &&
      !truncated
        ? "pass"
        : "fail",
    detail: {
      status: wire.statusValue || null,
      reason: wire.status?.reason ?? null,
      track: wire.track || null,
      thinkingMs: wire.status?.thinking_ms ?? null,
      statusFrames: wire.statusFrames,
      citationsFrameClosedTheStream: wire.citationsFrame !== undefined,
      statusAtMs: wire.statusAtMs,
      requireDone,
    },
  };
}

// SC60 on the live wire: whatever channel the provider used, raw
// reasoning markup must not ride the answer the operator reads.
function checkAnswerMarkup(wire) {
  const inDeltas = THINKING_MARKUP.test(wire.deltaText);
  const inTerminalAnswer = THINKING_MARKUP.test(wire.terminalAnswer);
  return {
    id: "3-answer-free-of-reasoning-markup",
    state: inDeltas || inTerminalAnswer ? "fail" : "pass",
    detail: { inDeltas, inTerminalAnswer, terminalAnswerChars: wire.terminalAnswer.length },
  };
}

// Every branch asserts something. A research turn that finished owes
// code-filled evidence; refuse / step_cap owe an empty list (spec); and
// a Soft-Track dialogue / clarify / off_topic turn may finish without
// tools, so for those an empty list is the correct answer (ADR 0027) —
// a non-empty one would mean citations leaked onto a turn that read no
// filing. An absent track is treated as research, the strict branch.
function checkCitations(wire) {
  const evidenceOwed =
    wire.statusValue === "done" && (wire.track === "research" || wire.track === "");
  const citations = wire.citations;
  const agree =
    citations !== null &&
    (evidenceOwed
      ? citations.length > 0 &&
        citations.every((c) => c && c.document_id && c.version && c.page != null && c.quote)
      : citations.length === 0);
  return {
    id: "4-citations-match-outcome",
    state: agree ? "pass" : "fail",
    detail: {
      status: wire.statusValue || null,
      track: wire.track || null,
      evidenceOwed,
      citations: citations === null ? null : citations.length,
      documents: citations === null ? null : [...new Set(citations.map((c) => c?.document_id))],
    },
  };
}

// The one three-state check. A provider that emits no reasoning at all
// is reported as "none" — not a pass, never counted as one — because
// the absence is the provider's, not this repo's contract breaking.
// Frames that DO arrive are held to the contract.
function checkReasoning(wire, { requireThinking }) {
  const state =
    wire.thinking.length === 0
      ? requireThinking
        ? "fail"
        : "none"
      : wire.thinkingText.trim().length > 0 && wire.lateThinking === 0
        ? "pass"
        : "fail";
  return {
    id: "5-reasoning-channel",
    state,
    detail: {
      thinkingFrames: wire.thinking.length,
      thinkingChars: wire.thinkingText.length,
      framesAfterTerminalStatus: wire.lateThinking,
      requireThinking,
    },
  };
}

function evaluate(frames, { contentType, truncated, requireThinking, requireDone }) {
  const wire = readWire(frames);
  return {
    wire,
    checks: [
      checkAnswerStreamed(wire, { contentType, truncated }),
      checkTerminalStatus(wire, { truncated, requireDone }),
      checkAnswerMarkup(wire),
      checkCitations(wire),
      checkReasoning(wire, { requireThinking }),
    ],
  };
}

function printReport(report) {
  const { checks, header } = report;
  const width = Math.max(...checks.map((c) => c.id.length));
  console.log("fin live-provider smoke — workspace stream fidelity");
  console.log(`  target       ${header.baseUrl}`);
  console.log(`  provider     model ${header.model} at ${header.origin}`);
  console.log(`  credential   ${CREDENTIAL_VAR} present (from ${header.credentialSource})`);
  console.log(`  retrieve     FIN_RETRIEVE_ADAPTER=${header.retrieve}`);
  console.log(`  question     ${header.question}`);
  console.log("");
  for (const check of checks) {
    const label = { pass: "PASS", fail: "FAIL", none: "NONE" }[check.state];
    console.log(`${label} ${check.id.padEnd(width)}  ${JSON.stringify(check.detail)}`);
  }
  console.log("");
  if (header.answerPreview) console.log(`answer (first 200 chars): ${header.answerPreview}`);
  const failed = checks.filter((c) => c.state === "fail");
  const notObserved = checks.filter((c) => c.state === "none");
  const passed = checks.filter((c) => c.state === "pass").length;
  console.log(
    `verdict: ${failed.length === 0 ? "PASS" : "FAIL"} — ${passed}/${checks.length} observables held` +
      (notObserved.length > 0
        ? `, ${notObserved.length} not observed (${notObserved
            .map((c) => c.id)
            .join(", ")}; --require-thinking makes that a failure)`
        : ""),
  );
}

async function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (error) {
    console.error(`live-smoke: ${error.message}\n\n${USAGE}`);
    return EXIT_LANE;
  }
  if (opts.help) {
    console.log(USAGE);
    return EXIT_OK;
  }

  // FIRST, before any port, any build, any process: the credential.
  const dotenv = await readDotenv();
  const credential = resolveCredential(CREDENTIAL_VAR, { dotenv });
  if (!credential.present) return abortMissingCredential();
  log(`${CREDENTIAL_VAR} found in ${credential.source} — value not printed`);

  if (opts.browserChecks) {
    const spaError = await ensureSpaBuild({ log });
    if (spaError) {
      return laneError(`${spaError}. Build the SPA with \`cd web && npm install && npm run build\`.`);
    }
  }

  const port = opts.port || (await freePort());
  const baseUrl = `http://127.0.0.1:${port}`;
  const env = childEnv({
    port,
    model: opts.model,
    baseUrl: opts.baseUrl,
    credential: credential.value,
    dotenv,
  });
  const compiled = await readCompileDefaults();
  const model =
    env.FIN_MODEL ||
    (compiled.model ? `${compiled.model} (compile.py default)` : "the FIN_MODEL default");
  const origin =
    safeOrigin(env.FIN_BASE_URL) ||
    safeOrigin(compiled.baseUrl) ||
    "the FIN_BASE_URL default";

  log(`booting the workspace on the configured provider at ${baseUrl}`);
  log(`provider: model ${model} at ${origin} (${SCRIPTED_OPT_IN} removed from the server env)`);

  const { child, uv, logPath, spawnError, closeLog } = await startWorkspace({
    port,
    env,
    logPrefix: "fin-live-smoke-",
  });

  try {
    const notReady = await waitForWorkspace(baseUrl, {
      deadline: Date.now() + opts.bootTimeoutMs,
      child,
      spawnFailed: () => spawnError() !== null,
    });
    if (spawnError()) {
      return laneError(
        `could not start \`${uv}\` (${spawnError().message}). Install uv or point FIN_E2E_UV at it.`,
      );
    }
    if (notReady) {
      return laneError(`${notReady}\n--- server log tail ---\n${await tailLog(logPath)}`);
    }

    let threadId;
    try {
      const created = await fetch(`${baseUrl}/api/threads`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ first_message: opts.question }),
        signal: AbortSignal.timeout(15_000),
      });
      if (!created.ok) throw new Error(`POST /api/threads -> HTTP ${created.status}`);
      threadId = (await created.json()).id;
    } catch (error) {
      return laneError(`could not create a thread: ${error.message}`);
    }

    log(`sending 1 real question on thread ${threadId} — this calls the provider`);
    const frames = [];
    let outcome;
    try {
      outcome = await sendQuestion(baseUrl, {
        threadId,
        question: opts.question,
        runTimeoutMs: opts.runTimeoutMs,
        onFrame: (frame) => frames.push(frame),
      });
    } catch (error) {
      return laneError(
        `the send never produced a stream: ${error.message}\n` +
          `--- server log tail ---\n${await tailLog(logPath)}`,
      );
    }
    log(`stream closed after ${(outcome.elapsedMs / 1000).toFixed(1)}s with ${frames.length} frames`);

    const evaluated = evaluate(frames, {
      contentType: outcome.contentType,
      truncated: outcome.truncated,
      requireThinking: opts.requireThinking,
      requireDone: opts.requireDone,
    });

    const crashReason = String(evaluated.wire.status?.reason ?? "");
    if (crashReason.startsWith(CRASH_REFUSE_PREFIX)) {
      return laneError(
        `the run crashed mid-stream after ${frames.length} frames (${crashReason}).\n` +
          "That refuse is the server's exception boundary, not a contract outcome, so\n" +
          "no fidelity observable can be judged on this turn. Check that the configured\n" +
          `provider (model ${model} at ${origin}) is reachable and that the credential\n` +
          "is accepted.\n" +
          `--- server log tail ---\n${await tailLog(logPath)}`,
      );
    }

    const header = {
      baseUrl,
      model,
      origin,
      credentialSource: credential.source,
      retrieve: env.FIN_RETRIEVE_ADAPTER || "mcp (compile.py default)",
      question: opts.question,
      answerPreview: (evaluated.wire.terminalAnswer || evaluated.wire.deltaText).slice(0, 200),
    };
    console.log("");
    if (opts.json) {
      console.log(JSON.stringify({ header, checks: evaluated.checks, frames }, null, 2));
    } else {
      printReport({ checks: evaluated.checks, header });
    }

    let exit = evaluated.checks.some((c) => c.state === "fail") ? EXIT_CHECK_FAILED : EXIT_OK;

    if (opts.browserChecks) {
      console.log("");
      log("running the browser stream checks against the same live workspace");
      // No --require-stream-growth: a live provider can land a whole
      // reply inside one poll tick, and that is not a regression. The
      // SSE checks above are where delta granularity is asserted.
      const checks = await run(process.execPath, [RUNNER, `--base-url=${baseUrl}`], {
        cwd: REPO_ROOT,
        env: process.env,
      });
      if (checks.error) {
        return laneError(`could not start the stream-checks runner: ${checks.error.message}`);
      }
      if (checks.code === EXIT_LANE) return EXIT_LANE;
      if (checks.code !== EXIT_OK) exit = EXIT_CHECK_FAILED;
    }

    log(`verdict exit code ${exit} (server log: ${logPath})`);
    return exit;
  } finally {
    if (opts.keepServer) {
      log(`--keep-server: workspace still serving at ${baseUrl} (pid ${child.pid})`);
    } else {
      await stopWorkspace(child);
      log(`workspace stopped (server log: ${logPath})`);
    }
    await closeLog();
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
