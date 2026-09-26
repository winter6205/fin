// The four `FIN_ATTACHMENT_*` bounds, resolved from ONE source: the keys as
// `.env.example` ships them.
//
// A lane that starts uvicorn without going through `serve()` has to hand the
// server those values itself, because the host reads all four before it binds
// a port and refuses to boot without them. Hardcoding them here made this the
// fourth copy that had to agree with the template by hand; `from_environ` in
// `src/fin/host/attachment_budgets.py` only checks that a value is a positive
// integer, never that it is the measured one.
//
// Nothing here knows what a bound means — this only reads text. The bash twin
// is `scripts/lib/attachment-bounds.sh`.

import { readFileSync } from "node:fs";
import path from "node:path";

import { REPO_ROOT } from "./workspace-server.mjs";

export const ATTACHMENT_BOUND_KEYS = [
  "FIN_ATTACHMENT_MAX_BYTES",
  "FIN_ATTACHMENT_MAX_PDF_PAGES",
  "FIN_ATTACHMENT_VISION_CALLS_PER_RUN",
  "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN",
];

function templateValues(text) {
  const pairs = new Map();
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim().replace(/^export\s+/, "");
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    let value = line.slice(eq + 1).trim();
    const quote = value[0];
    if (
      value.length >= 2 &&
      (quote === '"' || quote === "'") &&
      value.endsWith(quote)
    ) {
      value = value.slice(1, -1);
    }
    pairs.set(line.slice(0, eq).trim(), value);
  }
  return pairs;
}

/**
 * The four bounds as the template ships them, last occurrence winning like
 * `parse_env_file` does. Throws rather than guessing a number: a lane that
 * booted on an invented cap would measure the wrong thing.
 */
export function attachmentBoundsFromExample(repoRoot = REPO_ROOT) {
  let text;
  try {
    text = readFileSync(path.join(repoRoot, ".env.example"), "utf8");
  } catch {
    throw new Error(`${repoRoot}/.env.example is not readable`);
  }
  const pairs = templateValues(text);
  const missing = ATTACHMENT_BOUND_KEYS.filter((key) => !pairs.get(key));
  if (missing.length > 0) {
    throw new Error(
      `${repoRoot}/.env.example does not define ${missing.join(", ")}: ` +
        "set the variables, or restore the four attachment bounds.",
    );
  }
  return Object.fromEntries(ATTACHMENT_BOUND_KEYS.map((key) => [key, pairs.get(key)]));
}
