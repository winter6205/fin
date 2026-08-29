#!/usr/bin/env node
// Opt-in browser e2e runner for the workspace stream checks
// (`specs/workspace.md` Amendment 2026-08-26 — scripted-provider lane).
//
//   node scripts/run-stream-checks.mjs [--base-url=URL] [--json]
//
// It drives the global `playwright-cli` binary (see
// `.claude/skills/playwright-cli/SKILL.md`), evaluates every check in
// `scripts/playwright-stream-checks.mjs`, prints one PASS/FAIL line per check
// and exits:
//
//   0  every declared check reported PASS
//   1  a check reported FAIL, or a declared check produced no verdict
//   2  the harness itself could not run (missing binary, browser refused to
//      open, snippet crashed, wall-clock timeout)
//
// A check that cannot run is a FAIL, not a skip: the lane never exits 0 by
// staying quiet. The runner needs no provider credentials to start — with the
// workspace down or the model unreachable the checks simply say no.

import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CHECKS_MODULE = "./playwright-stream-checks.mjs";
const EXIT_OK = 0;
const EXIT_CHECK_FAILED = 1;
const EXIT_HARNESS = 2;

const USAGE = `Usage: node scripts/run-stream-checks.mjs [options]

Options:
  --base-url=URL       workspace under test      (env FIN_E2E_BASE_URL, default http://127.0.0.1:8000)
  --poll-count=N       stream poll ticks         (env FIN_E2E_POLL_COUNT, default 120)
  --poll-interval-ms=N ms between poll ticks     (env FIN_E2E_POLL_INTERVAL_MS, default 1000)
  --timeout-ms=N       wall-clock budget         (env FIN_E2E_TIMEOUT_MS, default poll budget + 90s)
  --require-stream-growth  fail the stream check unless the answer is seen
                       growing across polls and reaching the bubble before the
                       terminal status line (for a provider with paced deltas)
  --expect-bounce      also run the bounce boundary check; only valid against a
                       workspace whose transcript bounces the citation gate
                       (FIN_SCRIPTED_SCENARIO=bounce)
  --session=NAME       playwright-cli session    (default fin-stream-checks)
  --browser=NAME       playwright-cli browser    (default: playwright-cli's own default)
  --keep-open          leave the browser session open for debugging
  --json               print the raw report as JSON instead of the table
  -h, --help           this text

Prerequisites: a workspace served at --base-url (uv run fin-serve, with
web/dist built) and a global playwright-cli (npm install -g @playwright/cli).`;

function parseArgs(argv) {
  const opts = {
    baseUrl: process.env.FIN_E2E_BASE_URL ?? "http://127.0.0.1:8000",
    pollCount: Number(process.env.FIN_E2E_POLL_COUNT ?? 120),
    pollIntervalMs: Number(process.env.FIN_E2E_POLL_INTERVAL_MS ?? 1_000),
    timeoutMs: process.env.FIN_E2E_TIMEOUT_MS
      ? Number(process.env.FIN_E2E_TIMEOUT_MS)
      : null,
    requireStreamGrowth: false,
    expectBounce: false,
    session: "fin-stream-checks",
    browser: "",
    keepOpen: false,
    json: false,
    help: false,
  };
  for (const arg of argv) {
    const [flag, value] = arg.startsWith("--") ? arg.slice(2).split("=", 2) : [arg, ""];
    switch (flag) {
      case "base-url": opts.baseUrl = value; break;
      case "poll-count": opts.pollCount = Number(value); break;
      case "poll-interval-ms": opts.pollIntervalMs = Number(value); break;
      case "require-stream-growth": opts.requireStreamGrowth = true; break;
      case "expect-bounce": opts.expectBounce = true; break;
      case "timeout-ms": opts.timeoutMs = Number(value); break;
      case "session": opts.session = value; break;
      case "browser": opts.browser = value; break;
      case "keep-open": opts.keepOpen = true; break;
      case "json": opts.json = true; break;
      case "h": case "-h": case "help": opts.help = true; break;
      default: throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!Number.isFinite(opts.pollCount) || opts.pollCount < 1) {
    throw new Error(`--poll-count must be a positive number`);
  }
  if (!Number.isFinite(opts.pollIntervalMs) || opts.pollIntervalMs < 1) {
    throw new Error(`--poll-interval-ms must be a positive number`);
  }
  if (opts.timeoutMs === null) {
    opts.timeoutMs = opts.pollCount * opts.pollIntervalMs + 90_000;
  }
  if (!Number.isFinite(opts.timeoutMs) || opts.timeoutMs < 1_000) {
    throw new Error(`--timeout-ms must be at least 1000`);
  }
  return opts;
}

function runCli(bin, args, { cwd, timeoutMs }) {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: `${stderr}${error.message}`, timedOut, spawnFailed: true });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut, spawnFailed: false });
    });
  });
}

async function probeWorkspace(baseUrl) {
  try {
    const response = await fetch(baseUrl, { signal: AbortSignal.timeout(5_000) });
    return `reachable (HTTP ${response.status})`;
  } catch (error) {
    return `UNREACHABLE (${error && error.message ? error.message : error})`;
  }
}

// `run-code --filename` evaluates the file as a bare function expression, so
// the imported check function travels as source text. That is also why
// `runStreamChecks` must stay self-contained (see the checks module header).
function snippetFor(checkFn, checkOptions) {
  return `async (page) => {\n  const runStreamChecks = (${checkFn.toString()});\n  return await runStreamChecks(page, ${JSON.stringify(checkOptions)});\n}\n`;
}

function parseRunCodeResult(stdout) {
  const payload = JSON.parse(stdout);
  if (payload.isError) throw new Error(payload.error ?? "playwright-cli reported an error");
  const value = payload.result;
  return typeof value === "string" ? JSON.parse(value) : value;
}

// Declared-but-unreported ids become explicit failures so a check that
// silently vanishes from the report cannot read as a pass.
function reconcile(report, declaredIds) {
  const reported = new Map((report.checks ?? []).map((c) => [c.id, c]));
  return declaredIds.map(
    (id) =>
      reported.get(id) ?? {
        id,
        pass: false,
        detail: {},
        error: "check produced no verdict",
      },
  );
}

function printReport(checks, { baseUrl, cliVersion, session, reachability }) {
  const width = Math.max(...checks.map((c) => c.id.length));
  console.log("fin browser e2e — workspace stream checks");
  console.log(`  target       ${baseUrl} — ${reachability}`);
  console.log(`  harness      playwright-cli ${cliVersion} (session ${session})`);
  console.log("");
  for (const check of checks) {
    const verdict = check.pass ? "PASS" : "FAIL";
    const took = typeof check.ms === "number" ? ` ${(check.ms / 1000).toFixed(1)}s` : "";
    const why = check.error ? `error: ${check.error}` : JSON.stringify(check.detail ?? {});
    console.log(`[${verdict}] ${check.id.padEnd(width)}${took}  ${why}`);
  }
  const passed = checks.filter((c) => c.pass).length;
  console.log("");
  console.log(
    `verdict: ${passed === checks.length ? "PASS" : "FAIL"} — ${passed}/${checks.length} checks passed`,
  );
}

function fail(message) {
  console.error(`[FAIL] harness  ${message}`);
  return EXIT_HARNESS;
}

async function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (error) {
    console.error(`stream-checks: ${error.message}\n\n${USAGE}`);
    return EXIT_HARNESS;
  }
  if (opts.help) {
    console.log(USAGE);
    return EXIT_OK;
  }

  const bin = process.env.PLAYWRIGHT_CLI || "playwright-cli";
  const version = await runCli(bin, ["--version"], { cwd: tmpdir(), timeoutMs: 60_000 });
  if (version.spawnFailed || version.code !== 0) {
    return fail(
      `global \`${bin}\` is not runnable (${(version.stderr || "no output").trim()}). ` +
        `Install it with \`npm install -g @playwright/cli@latest\` or point PLAYWRIGHT_CLI at the binary.`,
    );
  }
  const cliVersion = version.stdout.trim().split("\n").pop() ?? "unknown";

  // Importing the checks as a real ES module is the load-time contract: a
  // module that does not parse fails here, before a browser is opened.
  const { default: runStreamChecks, declaredCheckIds } = await import(
    new URL(CHECKS_MODULE, import.meta.url).href
  );

  const checkOptions = {
    baseUrl: opts.baseUrl,
    pollCount: opts.pollCount,
    pollIntervalMs: opts.pollIntervalMs,
    requireStreamGrowth: opts.requireStreamGrowth,
    expectBounce: opts.expectBounce,
  };
  const declaredIds = declaredCheckIds(checkOptions);

  const reachability = await probeWorkspace(opts.baseUrl);
  const workDir = await mkdtemp(path.join(tmpdir(), "fin-stream-checks-"));
  const sessionArgs = [`-s=${opts.session}`];

  try {
    const openArgs = [...sessionArgs, "--json", "open", "about:blank"];
    if (opts.browser) openArgs.push(`--browser=${opts.browser}`);
    const opened = await runCli(bin, openArgs, { cwd: workDir, timeoutMs: 120_000 });
    if (opened.code !== 0) {
      return fail(`could not open a browser: ${(opened.stdout + opened.stderr).trim()}`);
    }

    const snippetPath = path.join(workDir, "stream-checks-snippet.js");
    await writeFile(snippetPath, snippetFor(runStreamChecks, checkOptions), "utf8");

    const run = await runCli(
      bin,
      [...sessionArgs, "--json", "run-code", `--filename=${snippetPath}`],
      { cwd: workDir, timeoutMs: opts.timeoutMs },
    );
    if (run.timedOut) {
      return fail(`checks did not finish within ${opts.timeoutMs}ms — killed, no verdict reached`);
    }
    if (run.spawnFailed || run.code !== EXIT_OK) {
      const status = run.spawnFailed ? "could not start" : `exited with code ${run.code}`;
      return fail(`checks process ${status}: ${(run.stderr || "no output").trim()}`);
    }

    let report;
    try {
      report = parseRunCodeResult(run.stdout);
    } catch (error) {
      return fail(
        `checks crashed before reporting: ${error.message}. ` +
          `stderr: ${(run.stderr || "none").trim()}`,
      );
    }

    const checks = reconcile(report, declaredIds);
    if (opts.json) {
      console.log(JSON.stringify({ baseUrl: opts.baseUrl, reachability, checks }, null, 2));
    } else {
      printReport(checks, {
        baseUrl: opts.baseUrl,
        cliVersion,
        session: opts.session,
        reachability,
      });
    }
    return checks.every((c) => c.pass) ? EXIT_OK : EXIT_CHECK_FAILED;
  } finally {
    if (!opts.keepOpen) {
      await runCli(bin, [...sessionArgs, "close"], { cwd: workDir, timeoutMs: 60_000 });
    }
    await rm(workDir, { recursive: true, force: true });
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
