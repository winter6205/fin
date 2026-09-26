// Source-level guard for the standalone home corpus door (spec
// ``corpus-serve-ingest``, home-entry clause). The web suite runs in a
// node environment (no DOM, no bundler transform), so — like
// ``productChrome.test.ts`` — this reads the page source via ``node:fs``
// and pins the structural facts the clause promises: the door is mounted
// on home, is reachable without an open thread, and drives the durable
// accept action. Deleting ``<CorpusIngestDoor />`` must not stay green.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = dirname(fileURLToPath(import.meta.url));
const HOME_PATH = join(SRC_DIR, "pages", "HomePage.tsx");
const HOME = readFileSync(HOME_PATH, "utf8");

/** The JSX a component returns, comments stripped, so the guard fires on
 * rendered chrome rather than on the prose that explains the contract. */
function jsxBody(source: string, marker: string): string {
  const start = source.indexOf(marker);
  return (start === -1 ? source : source.slice(start)).replace(
    /\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
    "",
  );
}

describe("home corpus-ingest door (accept-only entry, no thread)", () => {
  it("mounts the door on the standalone home page body", () => {
    const page = jsxBody(HOME, "return (");
    expect(page).toContain("<CorpusIngestDoor");
  });

  it("takes file + company + period and submits through the accept action", () => {
    const door = jsxBody(HOME, "function CorpusIngestDoor");
    expect(door).toContain('data-testid="corpus-ingest-file"');
    expect(door).toContain('data-testid="corpus-ingest-company"');
    expect(door).toContain('data-testid="corpus-ingest-period"');
    expect(door).toContain('data-testid="corpus-ingest-submit"');
    expect(HOME).toContain("ingestFile(");
  });

  it("is not wired to a thread — the door ships without opening one", () => {
    // The attach door (thread-scoped) must never leak into the corpus
    // ingest control: no thread id, no attachment endpoint.
    const door = jsxBody(HOME, "function CorpusIngestDoor");
    expect(door).not.toContain("attachToThread");
    expect(door).not.toContain("activeThread");
    expect(door).not.toMatch(/threadId/);
  });
});
