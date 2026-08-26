// Boot / wait / stop a workspace server child for an opt-in e2e lane
// (`specs/workspace.md` Amendment 2026-08-26).
//
// Shared by the two lanes that own a server for the length of their
// run: `scripts/run-scripted-e2e.mjs` (scripted provider, plan T7) and
// `scripts/run-live-smoke.mjs` (live provider, plan T8). The lanes
// differ in the environment they hand the child and in the assertions
// they make; the process plumbing below is identical, so it lives in
// one place rather than drifting between two copies.
//
// Nothing here reads provider settings or credentials: the caller
// composes the child environment and stays the only place that
// decides what a lane may run with.

import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { access, mkdtemp, open, readFile } from "node:fs/promises";
import { constants } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
export const SPA_INDEX = path.join(REPO_ROOT, "web", "dist", "index.html");

export function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

export async function exists(target) {
  try {
    await access(target, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

export function run(bin, args, { cwd, env, stdio } = {}) {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { cwd, env, stdio: stdio ?? "inherit" });
    child.on("error", (error) => resolve({ code: null, error }));
    child.on("close", (code) => resolve({ code, error: null }));
  });
}

// The SPA is a build artifact, not committed. Building it here is what
// lets a lane be ONE command: without it the browser would land on a
// 404 that looks like a product bug.
export async function ensureSpaBuild({ log } = {}) {
  if (await exists(SPA_INDEX)) return null;
  const web = path.join(REPO_ROOT, "web");
  const say = log ?? (() => {});
  if (!(await exists(path.join(web, "node_modules")))) {
    say("web/node_modules missing — running npm ci");
    const install = await run("npm", ["ci"], { cwd: web, env: process.env });
    if (install.code !== 0) return "npm ci failed";
  }
  say("web/dist missing — running npm run build");
  const build = await run("npm", ["run", "build"], { cwd: web, env: process.env });
  if (build.code !== 0) return "npm run build failed";
  if (!(await exists(SPA_INDEX))) return `npm run build produced no ${SPA_INDEX}`;
  return null;
}

// The provider a lane boots when the operator sets no `FIN_MODEL` /
// `FIN_BASE_URL` is `compile.py`'s pair of constants, and a lane that
// reports which provider answered must read them rather than restate
// them: ADR 0022 has already been re-pointed once (local proxy ->
// official MiniMax Chat Completions), and a hardcoded copy would have
// gone on naming the retired proxy in the evidence.
//
// Returns `{ model: null, baseUrl: null }` when the interpreter cannot
// be reached; the caller degrades to naming the variables instead of
// their values, which is honest, so this never aborts a lane.
export async function readCompileDefaults() {
  const uv = process.env.FIN_E2E_UV || "uv";
  const source =
    "from fin.host.compile import DEFAULT_MODEL, DEFAULT_BASE_URL;" +
    "print(DEFAULT_MODEL);print(DEFAULT_BASE_URL)";
  const printed = await new Promise((resolve) => {
    const child = spawn(uv, ["run", "--quiet", "python", "-c", source], {
      cwd: REPO_ROOT,
      env: process.env,
      stdio: ["ignore", "pipe", "ignore"],
    });
    let out = "";
    child.stdout.on("data", (chunk) => {
      out += chunk;
    });
    child.on("error", () => resolve(""));
    child.on("close", (code) => resolve(code === 0 ? out : ""));
  });
  const [model, baseUrl] = printed.trim().split("\n");
  return { model: model || null, baseUrl: baseUrl || null };
}

// Starts uvicorn as a process GROUP leader so `stopWorkspace` can
// signal the whole group — `uv run` is the direct child, uvicorn is
// its grandchild, and signalling only the direct child leaks a server.
export async function startWorkspace({ port, env, logPrefix = "fin-e2e-" }) {
  const workDir = await mkdtemp(path.join(tmpdir(), logPrefix));
  const logPath = path.join(workDir, "workspace.log");
  const logFile = await open(logPath, "a");
  const uv = process.env.FIN_E2E_UV || "uv";

  const child = spawn(
    uv,
    [
      "run",
      "--quiet",
      "python",
      "-m",
      "uvicorn",
      "fin.host.http:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: REPO_ROOT,
      env,
      detached: true,
      stdio: ["ignore", logFile.fd, logFile.fd],
    },
  );
  let spawnError = null;
  child.on("error", (error) => {
    spawnError = error;
  });

  return {
    child,
    uv,
    logPath,
    spawnError: () => spawnError,
    closeLog: () => logFile.close(),
  };
}

export async function waitForWorkspace(baseUrl, { deadline, child, spawnFailed }) {
  let lastError = "no attempt made";
  while (Date.now() < deadline) {
    if (spawnFailed()) return "server process never started";
    if (child.exitCode !== null) {
      return `server exited with code ${child.exitCode} before it answered`;
    }
    try {
      const response = await fetch(`${baseUrl}/api/corpus`, {
        signal: AbortSignal.timeout(3_000),
      });
      if (response.ok) {
        const body = await response.json();
        const filings = Array.isArray(body) ? body : body.filings;
        if (Array.isArray(filings) && filings.length > 0) return null;
        lastError = "GET /api/corpus returned no filings";
      } else {
        lastError = `GET /api/corpus -> HTTP ${response.status}`;
      }
    } catch (error) {
      lastError = error && error.message ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return `workspace never became ready (${lastError})`;
}

export async function stopWorkspace(child) {
  if (child.exitCode !== null) return;
  const ended = new Promise((resolve) => child.once("close", resolve));
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    return;
  }
  const killed = await Promise.race([
    ended.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 10_000)),
  ]);
  if (!killed) {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
}

export async function tailLog(logPath, lines = 20) {
  try {
    const text = await readFile(logPath, "utf8");
    return text.split("\n").slice(-lines).join("\n").trim();
  } catch {
    return "(server log unreadable)";
  }
}
