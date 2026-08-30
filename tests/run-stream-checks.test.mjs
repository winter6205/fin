import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { afterEach, test } from "node:test";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const workspace = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runner = path.join(workspace, "scripts", "run-stream-checks.mjs");
const fakeCliSource = `#!/usr/bin/env node
const args = process.argv.slice(2);
const has = (value) => args.includes(value);

if (has("--version")) {
  console.log("fake-playwright-cli 1.0.0");
  process.exit(0);
}

if (has("open")) {
  console.log(JSON.stringify({ result: {} }));
  process.exit(0);
}

if (has("run-code")) {
  const failedId = process.env.FAKE_FAILED_CHECK_ID;
  const ids = [
    "1-home-corpus",
    "2-stream-no-retract",
    "4-dom-separation",
    "5-trace-collapse",
    "3-reload-persist",
    "6-status-strip",
  ];
  const checks = ids.map((id) => ({
    id,
    pass: id !== failedId,
    detail: id === failedId ? { injected: true } : {},
  }));
  process.stdout.write(JSON.stringify({
    result: JSON.stringify({ checks, allPass: checks.every((check) => check.pass) }),
  }));
  process.exit(Number(process.env.FAKE_RUN_EXIT ?? 0));
}

if (has("close")) process.exit(0);
process.exit(1);
`;

const tempDirectories = [];

async function runHarness(environment = {}) {
  const directory = await mkdtemp(path.join(tmpdir(), "stream-checks-test-"));
  tempDirectories.push(directory);
  const fakeCli = path.join(directory, "fake-playwright-cli.mjs");
  await writeFile(fakeCli, fakeCliSource, "utf8");
  await chmod(fakeCli, 0o755);

  return spawnSync(process.execPath, [runner, "--base-url=http://127.0.0.1:1"], {
    cwd: workspace,
    encoding: "utf8",
    timeout: 10_000,
    env: {
      ...process.env,
      PLAYWRIGHT_CLI: fakeCli,
      ...environment,
    },
  });
}

afterEach(async () => {
  await Promise.all(
    tempDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true }),
    ),
  );
});

test("rejects an all-PASS report when the child exits non-zero", async () => {
  const result = await runHarness({ FAKE_RUN_EXIT: "7" });

  assert.notEqual(
    result.status,
    0,
    `runner succeeded despite child exit 7:\n${result.stdout}\n${result.stderr}`,
  );
});

test("prints a machine-visible FAIL line for a failed check", async () => {
  const result = await runHarness({ FAKE_FAILED_CHECK_ID: "2-stream-no-retract" });

  assert.notEqual(result.status, 0, "failed check must produce a non-success exit");
  assert.match(
    result.stdout,
    /^\[FAIL\]\s+2-stream-no-retract\b/m,
    `missing protocol FAIL line:\n${result.stdout}\n${result.stderr}`,
  );
});
