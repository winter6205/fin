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
// `fin.host.scripted_boot`, and with every provider variable REMOVED
// from its environment. No network LLM can be reached even on a
// machine that has ANTHROPIC_AUTH_TOKEN exported.
//
// Assertions are not duplicated here: the lane shells out to the T6
// runner (`scripts/run-stream-checks.mjs`), which owns the checks and
// the per-check PASS/FAIL table.

import path from "node:path";
import { fileURLToPath } from "node:url";

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

// Mirrors `fin.host.scripted_boot.SCRIPTED_SCENARIOS`. Validated here
// too so a typo fails before a server boots rather than as a stack
// trace in the child's log.
const SCENARIOS = ["grounded", "bounce"];

// Dropped from the server child's environment so the scripted boot
// cannot be talked out of by a stray provider setting, and so "this
// lane needs no credentials" is enforced rather than asserted.
const PROVIDER_ENV = [
  "ANTHROPIC_AUTH_TOKEN",
  "ANTHROPIC_API_KEY",
  "OPENAI_API_KEY",
  "FIN_MODEL",
  "FIN_BASE_URL",
  "FIN_CHECKPOINT_URL",
  "FIN_RETRIEVE_ADAPTER",
];

const USAGE = `Usage: node scripts/run-scripted-e2e.mjs [options]

Options:
  --port=N           serve the workspace here     (default: a free port)
  --scenario=NAME    scripted transcript: grounded (default) or bounce.
                     \`bounce\` streams an ungrounded draft the citation gate
                     refuses, then the grounded retry, and adds the bounce
                     boundary check to the runner's table.
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
  // `compile_for_scripted_e2e` binds the fake retrieve port
  // unconditionally, but `GET /api/status` reports the CONFIGURED
  // adapter (`FIN_RETRIEVE_ADAPTER`, default `mcp`). Left unset, the
  // Sidebar strip told the operator "检索：不可用 · 回答不会带引用来源"
  // on a lane whose every run ends `done` with real citations. Saying
  // `fake` makes the configured kind match the bound port.
  env.FIN_RETRIEVE_ADAPTER = "fake";
  if (deltaMs !== "") env.FIN_SCRIPTED_DELTA_MS = String(deltaMs);
  env.FIN_E2E_BASE_URL = `http://127.0.0.1:${port}`;
  return { env, dropped };
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

    // Lane defaults first, operator flags after (the runner's last
    // occurrence wins). The scripted provider paces its deltas, so
    // this lane polls fast enough to watch the answer grow and holds
    // the runner to that stricter contract.
    const checks = await run(
      process.execPath,
      [
        RUNNER,
        `--base-url=${baseUrl}`,
        "--poll-interval-ms=200",
        "--poll-count=250",
        "--require-stream-growth",
        ...(opts.scenario === "bounce" ? ["--expect-bounce"] : []),
        ...opts.forwarded,
      ],
      { cwd: REPO_ROOT, env: process.env },
    );
    if (checks.error) {
      return fail(`could not start the stream-checks runner: ${checks.error.message}`);
    }
    console.log("");
    log(`verdict exit code ${checks.code} (server log: ${logPath})`);
    return checks.code ?? EXIT_LANE;
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
