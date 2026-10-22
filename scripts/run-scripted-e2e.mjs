#!/usr/bin/env node
// Scripted-provider browser e2e lane
// (`specs/workspace.md` Amendment 2026-08-26; plan T7).
//
//   node scripts/run-scripted-e2e.mjs [options]
//
// One command: boot the workspace on the scripted provider, drive a
// browser through send -> stream -> collapse -> reload, tear the server
// down, and exit on the lane's verdict.
//
//   0  every check passed
//   1  a check failed or produced no verdict (a fidelity regression)
//   2  the lane could not run (no SPA build, server never came up,
//      harness missing) — never reported as a pass
//
// Credential-free by construction: the server child is started with
// FIN_SCRIPTED_MODEL=1, which selects the scripted transcript boot in
// `fin.host.scripted_boot`, and with provider credentials/endpoints removed.
// A synthetic model identity is then supplied for the status surface only;
// the scripted boot never constructs or calls a provider model.
//
// Assertions are not duplicated here: the lane shells out to the T6
// runner (`scripts/run-stream-checks.mjs`), which owns the checks and
// the per-check PASS/FAIL table.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { attachmentBoundsFromExample } from "./lib/attachment-bounds.mjs";
import { startScriptedSseProxy } from "./lib/scripted-sse-proxy.mjs";
import {
  REPO_ROOT,
  ensureSpaBuild,
  freePort,
  run,
  startWorkspace,
  stopWorkspace,
  tailLog,
  waitForWorkspace,
} from "./lib/workspace-server.mjs";

const RUNNER = path.join(REPO_ROOT, "scripts", "run-stream-checks.mjs");

const EXIT_OK = 0;
const EXIT_LANE = 2;
const SCRIPTED_ANSWER_FRAME_DELAY_MS = 225;
const SCRIPTED_MODEL_IDENTITY = "scripted-test-model";

// Mirrors `fin.host.scripted_boot.SCRIPTED_SCENARIOS`. Validated here
// too so a typo fails before a server boots rather than as a stack
// trace in the child's log.
const SCENARIOS = ["grounded", "bounce", "document_choice"];

// Dropped from the server child's environment so the scripted boot
// cannot be talked out of by a stray provider setting, and so "this
// lane needs no credentials" is enforced rather than asserted.
const PROVIDER_ENV = [
  "FIN_API_KEY",
  "ANTHROPIC_API_KEY",
  "OPENAI_API_KEY",
  "FIN_MODEL",
  "FIN_BASE_URL",
  "FIN_CHECKPOINT_URL",
  "FIN_RETRIEVE_ADAPTER",
];

// This lane reads no ./.env and starts uvicorn directly rather than through
// `serve()`, so nothing else would tell the server its upload cap: without
// the four bounds the door answers a config failure instead of a bound.
// Resolved once, at load: a checkout whose template lost a bound fails here,
// naming the key, before a server is booted or a browser is opened.
const ATTACHMENT_BOUNDS = attachmentBoundsFromExample();

// Which scenario adds which check to the runner's table. Kept as data
// beside the scenario list rather than as a chain of ternaries at the
// call site, so the two lists cannot drift: a scenario added to
// SCENARIOS with no check here would run the whole lane and report
// nothing about the thing it was built to show.
const SCENARIO_CHECK_FLAGS = {
  bounce: ["--expect-bounce"],
  document_choice: ["--expect-document-choice"],
};

const USAGE = `Usage: node scripts/run-scripted-e2e.mjs [options]

Options:
  --port=N           serve the workspace here     (default: a free port)
  --scenario=NAME    scripted transcript: grounded (default), bounce, or
                     document_choice.
                     \`bounce\` streams an ungrounded draft the citation gate
                     refuses, then the grounded retry, and adds the bounce
                     boundary check to the runner's table.
                     \`document_choice\` pauses the run on a grounded
                     document choice and adds the send -> card -> pick ->
                     resumed-answer check.
  --delta-ms=N       pacing between scripted deltas (env FIN_SCRIPTED_DELTA_MS)
  --poll-count=N     stream poll ticks, 1s each   (forwarded to the runner)
  --browser=NAME     playwright-cli browser       (forwarded to the runner)
  --json             raw JSON report              (forwarded to the runner)
  --keep-open        leave the browser session open (forwarded to the runner)
  --keep-server      leave the workspace serving after the verdict
  --boot-timeout=N   seconds to wait for the server  (default 120)
  -h, --help         this text

Prerequisites: a built SPA (\`cd web && npm run build\`) and a global
playwright-cli (\`npm install -g @playwright/cli@latest\`). No provider
credentials — the lane removes them from the server's environment.`;

function log(message) {
  console.log(`scripted-e2e: ${message}`);
}

function fail(message) {
  console.error(`scripted-e2e: ${message}`);
  return EXIT_LANE;
}

function parseArgs(argv) {
  const opts = {
    port: 0,
    scenario: "grounded",
    deltaMs: process.env.FIN_SCRIPTED_DELTA_MS ?? "",
    bootTimeoutMs: 120_000,
    keepServer: false,
    help: false,
    forwarded: [],
  };
  for (const arg of argv) {
    const [flag, value] = arg.startsWith("--") ? arg.slice(2).split("=", 2) : [arg, ""];
    switch (flag) {
      case "port": opts.port = Number(value); break;
      case "scenario": opts.scenario = value; break;
      case "delta-ms": opts.deltaMs = value; break;
      case "boot-timeout": opts.bootTimeoutMs = Number(value) * 1_000; break;
      case "keep-server": opts.keepServer = true; break;
      case "poll-count": case "poll-interval-ms": case "browser":
        opts.forwarded.push(arg);
        break;
      case "json": case "keep-open": opts.forwarded.push(arg); break;
      case "h": case "-h": case "help": opts.help = true; break;
      default: throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!Number.isInteger(opts.port) || opts.port < 0 || opts.port > 65_535) {
    throw new Error("--port must be a TCP port number");
  }
  if (!SCENARIOS.includes(opts.scenario)) {
    throw new Error(`--scenario must be one of ${SCENARIOS.join(", ")}`);
  }
  if (!Number.isFinite(opts.bootTimeoutMs) || opts.bootTimeoutMs < 1_000) {
    throw new Error("--boot-timeout must be at least 1 second");
  }
  return opts;
}

function childEnv(port, deltaMs, scenario) {
  const env = { ...process.env };
  const dropped = [];
  for (const name of PROVIDER_ENV) {
    if (name in env) {
      delete env[name];
      dropped.push(name);
    }
  }
  env.FIN_SCRIPTED_MODEL = "1";
  env.FIN_SCRIPTED_SCENARIO = scenario;
  env.FIN_MODEL = SCRIPTED_MODEL_IDENTITY;
  // `compile_for_scripted_e2e` binds the fake retrieve port, and
  // `GET /api/status` now publishes that BOUND port (`resolve_bound_retrieve_adapter_kind`),
  // so the status payload reads `fake` with or without this variable.
  // It stays for the OTHER port the same string selects:
  // `_build_default_ingest_service` reads the configured
  // `FIN_RETRIEVE_ADAPTER` for the ingest door's push / invalidation
  // ports, and left unset that door would be wired to the MCP adapters
  // on a lane with no MCP server. The lane drives no upload today, so
  // this is the port it would get if it ever did.
  env.FIN_RETRIEVE_ADAPTER = "fake";
  for (const [key, value] of Object.entries(ATTACHMENT_BOUNDS)) {
    if (!(key in env)) env[key] = value;
  }
  if (deltaMs !== "") env.FIN_SCRIPTED_DELTA_MS = String(deltaMs);
  env.FIN_E2E_BASE_URL = `http://127.0.0.1:${port}`;
  return { env, dropped };
}

async function runPacedBrowserChecks(baseUrl, opts) {
  let sseProxy = null;
  try {
    sseProxy = await startScriptedSseProxy({
      upstreamBaseUrl: baseUrl,
      answerFrameDelayMs: SCRIPTED_ANSWER_FRAME_DELAY_MS,
    });
    log(
      `browser checks use a test-only proxy that replays completed SSE responses ` +
        `with ${SCRIPTED_ANSWER_FRAME_DELAY_MS}ms between admitted answer frames`,
    );

    // Lane defaults first, operator flags after (the runner's last
    // occurrence wins). The proxy paces only complete, admitted answer
    // frames after the backend response ends; it does not simulate
    // provider generation latency or expose an in-flight draft.
    const checks = await run(
      process.execPath,
      [
        RUNNER,
        `--base-url=${sseProxy.baseUrl}`,
        "--poll-interval-ms=200",
        "--poll-count=250",
        "--require-stream-growth",
        ...(SCENARIO_CHECK_FLAGS[opts.scenario] ?? []),
        ...opts.forwarded,
      ],
      { cwd: REPO_ROOT, env: process.env },
    );
    if (checks.error) {
      return fail(`could not start the stream-checks runner: ${checks.error.message}`);
    }
    return checks.code ?? EXIT_LANE;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    const phase = sseProxy ? "run browser checks" : "start test-only SSE pacing proxy";
    return fail(`could not ${phase}: ${detail}`);
  } finally {
    if (sseProxy) await sseProxy.close();
  }
}

async function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (error) {
    console.error(`scripted-e2e: ${error.message}\n\n${USAGE}`);
    return EXIT_LANE;
  }
  if (opts.help) {
    console.log(USAGE);
    return EXIT_OK;
  }

  const spaError = await ensureSpaBuild({ log });
  if (spaError) {
    return fail(`${spaError}. Build the SPA with \`cd web && npm install && npm run build\`.`);
  }

  const port = opts.port || (await freePort());
  const baseUrl = `http://127.0.0.1:${port}`;
  const { env, dropped } = childEnv(port, opts.deltaMs, opts.scenario);

  log(`booting the workspace on the scripted provider at ${baseUrl}`);
  log(`transcript scenario: ${opts.scenario}`);
  log(
    dropped.length > 0
      ? `provider env removed from the server: ${dropped.join(", ")}`
      : "no provider env was present to remove",
  );

  const { child, uv, logPath, spawnError, closeLog } = await startWorkspace({
    port,
    env,
    logPrefix: "fin-scripted-e2e-",
  });

  try {
    const notReady = await waitForWorkspace(baseUrl, {
      deadline: Date.now() + opts.bootTimeoutMs,
      child,
      spawnFailed: () => spawnError() !== null,
    });
    if (spawnError()) {
      return fail(
        `could not start \`${uv}\` (${spawnError().message}). Install uv or point FIN_E2E_UV at it.`,
      );
    }
    if (notReady) {
      return fail(`${notReady}\n--- server log tail ---\n${await tailLog(logPath)}`);
    }
    log("workspace is up — handing the browser checks to run-stream-checks.mjs");
    console.log("");

    const verdict = await runPacedBrowserChecks(baseUrl, opts);
    console.log("");
    log(`verdict exit code ${verdict} (server log: ${logPath})`);
    return verdict;
  } finally {
    try {
      if (opts.keepServer) {
        log(`--keep-server: workspace still serving at ${baseUrl} (pid ${child.pid})`);
      } else {
        await stopWorkspace(child);
        log(`workspace stopped (server log: ${logPath})`);
      }
    } finally {
      await closeLog();
    }
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
