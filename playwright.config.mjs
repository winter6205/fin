// Playwright Test foundation for P4 browser acceptance
// (`specs/harness-runtime-acceptance.md`). The dependency was approved on
// 2026-10-21 and installed as `@playwright/test` 1.64.1
// (`plans/harness-runtime-p4-playwright-dependency-plan.md`).
//
// Fin-specific setup lives in `tests/browser/fixtures.mjs`; this file stays
// declarative so another implementer can reorganize fixtures without touching
// the reporter, artifact, or privacy policy.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "@playwright/test";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const REPORT_DIR = path.join(ROOT, "data", "eval", "acceptance", "browser");
const ARTIFACT_DIR = path.join(REPORT_DIR, "artifacts");

// One report per run, never overwritten: campaign evidence is append-only and
// the P4 ledger keys attempts by the report file it points at.
function reportFile() {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  return path.join(REPORT_DIR, `playwright-${stamp}.json`);
}

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: true,
  // Each worker boots its own server on an ephemeral port; 2 keeps a laptop
  // stable while still proving isolation.
  workers: Number(process.env.PLAYWRIGHT_WORKERS || 2),
  // A retry would hide a failure. Attempts are recorded by the P4 ledger, so
  // the runner must report the first outcome.
  retries: 0,
  timeout: Number(process.env.PLAYWRIGHT_TIMEOUT_MS || 120_000),

  // These reporters are the evidence path: a CLI `--reporter` flag replaces
  // this whole array, so an override would suppress the JSON report file.
  reporter: [
    ["list"],
    [
      "json",
      { outputFile: process.env.PLAYWRIGHT_JSON_OUTPUT || reportFile() },
    ],
  ],

  outputDir: ARTIFACT_DIR,
  use: {
    // Empty means "no origin"; `tests/browser/fixtures.mjs` turns that into a
    // named failure instead of a bare invalid-URL error.
    baseURL: process.env.FIN_E2E_BASE_URL || "",
    // Traces record page content, so they stay off unless a campaign opts in.
    trace: "off",
    // A screenshot on failure is the only artifact allowed to capture pixels.
    screenshot: "only-on-failure",
    // Video would capture whole journeys, including any private document text.
    video: "off",
  },

  // Chromium only: one browser keeps the lane's cost and artifact set bounded.
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
