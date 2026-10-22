// The four `FIN_ATTACHMENT_*` bounds, from ONE source: `settings.example.yaml`.
//
// A lane that starts uvicorn without going through `serve()` has to hand the
// server those values itself, because the host reads all four before it binds
// a port and refuses to boot without them. This module asks
// `fin.host.operator_settings.shipped_attachment_bounds` and does not parse
// YAML. The bash twin is `scripts/lib/attachment-bounds.sh`.

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

import { REPO_ROOT } from "./workspace-server.mjs";

export const ATTACHMENT_BOUND_KEYS = [
  "FIN_ATTACHMENT_MAX_BYTES",
  "FIN_ATTACHMENT_MAX_PDF_PAGES",
  "FIN_ATTACHMENT_VISION_CALLS_PER_RUN",
  "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN",
];

const PYTHON = `
import json
import os
import sys
from pathlib import Path

from fin.host.operator_settings import (
    OperatorSettingsConfigError,
    shipped_attachment_bounds,
)

root = Path(os.environ["FIN_ATTACHMENT_EXAMPLE_ROOT"])
try:
    bounds = shipped_attachment_bounds(repo_root=root)
except OperatorSettingsConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
print(json.dumps(bounds))
`;

function pythonInvocation() {
  const venvPython = path.join(REPO_ROOT, ".venv", "bin", "python");
  if (existsSync(venvPython)) {
    return { command: venvPython, args: ["-c", PYTHON] };
  }
  return { command: "uv", args: ["run", "python", "-c", PYTHON] };
}

/**
 * The four bounds as `settings.example.yaml` ships them. Throws rather than
 * guessing a number: a lane that booted on an invented cap would measure the
 * wrong thing. Process-env precedence is the caller's job.
 */
export function attachmentBoundsFromExample(repoRoot = REPO_ROOT) {
  const { command, args } = pythonInvocation();
  let stdout;
  try {
    stdout = execFileSync(command, args, {
      cwd: REPO_ROOT,
      env: { ...process.env, FIN_ATTACHMENT_EXAMPLE_ROOT: repoRoot },
      encoding: "utf8",
    });
  } catch (error) {
    const stderr = error && error.stderr ? String(error.stderr).trim() : "";
    throw new Error(
      stderr || `${repoRoot}/settings.example.yaml is not readable`,
    );
  }
  const bounds = JSON.parse(stdout);
  const missing = ATTACHMENT_BOUND_KEYS.filter((key) => !bounds[key]);
  if (missing.length > 0) {
    throw new Error(
      `${repoRoot}/settings.example.yaml does not define ${missing.join(", ")}: ` +
        "set the variables, or restore the four attachment bounds.",
    );
  }
  return Object.fromEntries(
    ATTACHMENT_BOUND_KEYS.map((key) => [key, bounds[key]]),
  );
}
