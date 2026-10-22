// Fin-owned Playwright Test fixtures for P4 browser acceptance
// (`specs/harness-runtime-acceptance.md`: product setup and assertions stay
// here, not in the shared config). The bare import below is the fail-closed
// signal for the whole foundation: without `@playwright/test` no journey runs.

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { test as base, expect } from "@playwright/test";

import {
  REPO_ROOT,
  SPA_INDEX,
  ensureSpaBuild,
  exists,
  freePort,
  startWorkspace,
  stopWorkspace,
  tailLog,
  waitForWorkspace,
} from "../../scripts/lib/workspace-server.mjs";

const BOOT_TIMEOUT_MS = Number(process.env.PLAYWRIGHT_BOOT_TIMEOUT_MS || 120_000);
const BLOCKED = "P4 browser foundation blocked";

// A named, actionable failure beats a timeout three layers down.
function blocked(reason) {
  return new Error(`${BLOCKED}: ${reason}`);
}

const isAbsoluteUrl = (value) => /^[a-z][a-z0-9+.-]*:\/\//i.test(String(value));

export const test = base.extend({
  // Boots the real application (`fin.host.http:app`) once per worker on a free
  // port; no proxy, no scripted model. Failures are thrown, never skipped.
  appServer: [
    async ({}, use) => {
      const webModules = path.join(REPO_ROOT, "web", "node_modules");
      if (!(await exists(webModules))) {
        throw blocked("web/node_modules is missing; run `npm --prefix web ci` first");
      }
      const buildError = await ensureSpaBuild();
      if (buildError) throw blocked(`${buildError}; run \`npm --prefix web run build\``);

      const port = await freePort();
      const baseUrl = `http://127.0.0.1:${port}`;
      const started = await startWorkspace({
        port,
        env: { ...process.env, FIN_E2E_BASE_URL: baseUrl },
        logPrefix: "fin-p4-browser-",
      });
      try {
        // Readiness needs the seeded catalog: `npm run seed:demo` on a dev stack.
        const notReady = await waitForWorkspace(baseUrl, {
          deadline: Date.now() + BOOT_TIMEOUT_MS,
          child: started.child,
          spawnFailed: () => started.spawnError() !== null,
        });
        if (started.spawnError()) {
          throw blocked(`could not start ${started.uv}: ${started.spawnError().message}`);
        }
        if (notReady) {
          throw blocked(`${notReady}\n--- server log tail ---\n${await tailLog(started.logPath)}`);
        }
        await use({ baseUrl, port });
      } finally {
        await stopWorkspace(started.child);
        await started.closeLog();
      }
    },
    { scope: "worker" },
  ],

  // A test that navigates without `appServer` uses FIN_E2E_BASE_URL through the
  // config's `baseURL`; a relative navigation with neither is blocked by name
  // instead of surfacing Playwright's bare "Invalid URL".
  page: async ({ page, baseURL }, use) => {
    const nativeGoto = page.goto.bind(page);
    page.goto = (url, options) => {
      if (!isAbsoluteUrl(url) && !baseURL) {
        throw blocked(
          "no origin: set FIN_E2E_BASE_URL, or navigate to the absolute URL from the `appServer` fixture",
        );
      }
      return nativeGoto(url, options);
    };
    await use(page);
  },

  // Temp-root JSON readback for durable assertions; reads files only.
  durableReadback: async ({}, use) => {
    const root = await mkdtemp(path.join(tmpdir(), "fin-p4-durable-"));
    const within = (relative) => {
      const target = path.resolve(root, relative);
      if (target !== root && !target.startsWith(root + path.sep)) {
        throw blocked(`readback path escapes the temp root: ${relative}`);
      }
      return target;
    };
    await use({
      root,
      readJson: async (relative) => JSON.parse(await readFile(within(relative), "utf8")),
    });
    await rm(root, { recursive: true, force: true });
  },

  // Preflight: the runner resolved this file, and a journey has a built SPA.
  requireBrowserFoundation: async ({}, use) => {
    if (!(await exists(SPA_INDEX))) {
      throw blocked("built SPA missing at web/dist/index.html; run `npm --prefix web run build`");
    }
    await use(true);
  },
});

export { expect };
