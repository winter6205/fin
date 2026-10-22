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
import { ingestSubmission, isIngestSubmittable } from "./pages/HomePage";

const SRC_DIR = dirname(fileURLToPath(import.meta.url));
const HOME_PATH = join(SRC_DIR, "pages", "HomePage.tsx");
const HOME = readFileSync(HOME_PATH, "utf8");

const FILE = new File(["x"], "a.md");

/** The door's optional metadata fields; every one is blank by default. */
interface MetaFields {
  company: string;
  period: string;
  title: string;
}

function fields(overrides: Partial<MetaFields> = {}): MetaFields {
  return { company: "", period: "", title: "", ...overrides };
}

/** The JSX a component returns, comments stripped, so the guard fires on
 * rendered chrome rather than on the prose that explains the contract. */
function jsxBody(source: string, marker: string): string {
  const start = source.indexOf(marker);
  return (start === -1 ? source : source.slice(start)).replace(
    /\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
    "",
  );
}

/** The door's markup is a group of components — the hint and the generic
 * toggle, the metadata boxes, the file note and the form view — so the
 * guard reads the whole group from its first line. */
const DOOR_MARKUP = "const INGEST_DOOR_HINT";

describe("home corpus-ingest door (accept-only entry, no thread)", () => {
  it("mounts the door on the standalone home page body", () => {
    const page = jsxBody(HOME, "return (");
    expect(page).toContain("<CorpusIngestDoor");
  });

  it("takes a file plus optional title, company and period", () => {
    // The markup lives in the door's own components; the door owns the
    // submit lifecycle and calls the accept action.
    const form = jsxBody(HOME, DOOR_MARKUP);
    expect(form).toContain('data-testid="corpus-ingest-file"');
    expect(form).toContain('data-testid="corpus-ingest-title"');
    expect(form).toContain('data-testid="corpus-ingest-company"');
    expect(form).toContain('data-testid="corpus-ingest-period"');
    expect(form).toContain('data-testid="corpus-ingest-submit"');
    expect(HOME).toContain("ingestFile(");
    const door = jsxBody(HOME, "function CorpusIngestDoor");
    expect(door).toContain("<IngestFormFieldsView");
  });

  it("is not wired to a thread — the door ships without opening one", () => {
    // The attach door (thread-scoped) must never leak into the corpus
    // ingest control: no thread id, no attachment endpoint.
    const door = jsxBody(HOME, "function CorpusIngestDoor");
    expect(door).not.toContain("attachToThread");
    expect(door).not.toContain("activeThread");
    expect(door).not.toMatch(/threadId/);
    expect(jsxBody(HOME, DOOR_MARKUP)).not.toMatch(/threadId/);
  });

  // One upload flow: the file is the only required input, and every
  // metadata box is optional supplementary text.
  it("offers exactly one flow — no mode toggle and no required metadata", () => {
    const form = jsxBody(HOME, DOOR_MARKUP);
    expect(form).not.toContain('data-testid="corpus-ingest-generic"');
    expect(HOME).not.toContain("fields.generic");
    // Ticker and reporting-period examples would imply a financial
    // identity is required; the single flow never asks for one.
    expect(form).not.toContain("aapl");
    expect(form).not.toContain("2022q3");
    expect(form).not.toContain("通用文档");
  });

  it("delegates the submittability and submission rules to the pure helpers", () => {
    // The component calls the helpers rather than re-deciding inline, so
    // the two rules stay unit-tested without a DOM.
    const door = jsxBody(HOME, "function CorpusIngestDoor");
    expect(door).toContain("isIngestSubmittable(file)");
    expect(door).toContain("ingestSubmission(fields)");
  });
});

describe("the door's metadata rules (pure, so they are pinned here)", () => {
  it("refuses a submission with no file", () => {
    expect(isIngestSubmittable(null)).toBe(false);
  });

  it("accepts a file alone, with no metadata at all", () => {
    expect(isIngestSubmittable(FILE)).toBe(true);
  });

  it("sends the typed optional metadata, trimmed", () => {
    expect(
      ingestSubmission(
        fields({ title: "  Handbook  ", company: " Acme ", period: " 2024Q1 " }),
      ),
    ).toEqual({ title: "Handbook", company: "Acme", period: "2024Q1" });
  });

  it("omits blank fields instead of sending them as empty strings", () => {
    const submission = ingestSubmission(fields());
    expect(submission).toEqual({});
    expect("title" in submission).toBe(false);
    expect("company" in submission).toBe(false);
    expect("period" in submission).toBe(false);
  });

  it("never sends a mode or a caller-chosen document id", () => {
    const submission = ingestSubmission(
      fields({ title: "Handbook", company: "Acme", period: "2024Q1" }),
    );
    expect("generic" in submission).toBe(false);
    expect("document_id" in submission).toBe(false);
  });
});

describe("home catalog surface (overview + publication warning)", () => {
  it("mounts the overview on home", () => {
    expect(jsxBody(HOME, "return (")).toContain("<CorpusOverview");
  });

  it("renders the safe publication-warning copy from the closed code table", () => {
    // The panel may read the code; it must NOT render the raw warning
    // object's fields, so a persisted exception message has no path to
    // the screen. The copy itself comes from describeCatalogWarning.
    const panel = jsxBody(HOME, "function CatalogWarningSection");
    expect(panel).toContain("describeCatalogWarning");
    expect(panel).not.toContain("catalog_warning.");
    // The panel wires the sub-view from the polled job's warning field.
    const job = jsxBody(HOME, "function IngestJobPanel");
    expect(job).toContain("CatalogWarningSection");
    expect(job).toContain("job?.catalog_warning");
  });
});
