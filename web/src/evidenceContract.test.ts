// E6 runtime decoding of the evidence channel (specs/enterprise-evidence-
// contract.md §2/§3/§6/§7, plans/post321-evidence-schema-route.md E6).
//
// TypeScript types are compile-time only — history/SSE JSON still arrives
// as ``unknown`` (spec §6 web-decode row). These tests pin the hand-rolled
// guards: every refusal is a VISIBLE, TYPED, named-code outcome; nothing
// coerces, silently drops, or invents a default.
//
// Valid-payload cases are driven from the committed conformance fixtures
// (spec §5): ``data/conformance/evidence/**``. Expected values come from
// each fixture's own ``expect`` / ``refusal_code`` fields — independently
// defined, not read off the decoder.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { decodeComparison } from "./comparison";
import {
  CITATION_CHANNEL_SCHEMA,
  DATABASE_ROW_SNAPSHOT_SCHEMA,
  EVIDENCE_SCHEMA_VERSION,
  VISUAL_CLAIM_SCHEMA,
  canonicalSnapshotBytes,
  decodeCitationRow,
  decodeCitationsArray,
  decodeCitationsEnvelope,
  decodeDatabaseRowSnapshot,
  decodeEvidenceRefusalEntry,
  decodeEvidenceRefusalsFrame,
  decodeEvidenceSchemaField,
  decodeThreadDetail,
  decodeVisualClaimRow,
  evidenceRefusalRowText,
  databaseRowIdentityLabel,
  databaseRowFieldLines,
  mergeEvidenceRefusals,
  schemaVersionRefusal,
  snapshotValueText,
} from "./evidenceContract";
import type { EvidenceRefusal } from "./types";

const FIXTURES = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "data",
  "conformance",
  "evidence",
);

function fixture<T = unknown>(...parts: string[]): T {
  const path = join(FIXTURES, ...parts);
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

interface ConformanceCase<TPayload = unknown> {
  case: string;
  direction: string;
  expect: "accept" | "refuse";
  refusal_code: string | null;
  payload: TPayload;
}

interface ComparisonManifestEntry {
  case: string;
  file: string;
  direction: string;
  expect: "accept" | "refuse";
  refusal_code: string | null;
  class: string;
}

interface ComparisonManifest {
  profile: string;
  contract: string;
  cases: ComparisonManifestEntry[];
}

const comparisonManifest = fixture<ComparisonManifest>(
  "comparison",
  "manifest.json",
);

// ----- the frozen schema tokens (spec §2.1) -----

describe("schema tokens mirror the spec", () => {
  it("names the citation channel and the two per-object versions", () => {
    expect(CITATION_CHANNEL_SCHEMA).toBe("fin.evidence.citation");
    expect(DATABASE_ROW_SNAPSHOT_SCHEMA).toBe(
      "fin.evidence.database_row_snapshot",
    );
    expect(VISUAL_CLAIM_SCHEMA).toBe("fin.evidence.visual_claim");
    // §2.1: the token names the shape and NEVER carries a version; the
    // version is the explicit integer sibling, so a token that grew a
    // ``.v1`` suffix again is a contract regression, not a synonym.
    expect(EVIDENCE_SCHEMA_VERSION).toBe(1);
    for (const token of [
      CITATION_CHANNEL_SCHEMA,
      DATABASE_ROW_SNAPSHOT_SCHEMA,
      VISUAL_CLAIM_SCHEMA,
    ]) {
      expect(token.startsWith("fin.evidence.")).toBe(true);
      expect(/\.v\d+$/.test(token)).toBe(false);
    }
  });
});

// ----- envelope field (evidence_schema) -----

describe("decodeEvidenceSchemaField", () => {
  it("refuses the frozen channel token when it states no version", () => {
    // PREMISE CHANGED (ADR 0093 decision 12). This case previously read
    // the bare token as "known", asserting that a peer naming the shape
    // without numbering it was waved through; that leniency is withdrawn
    // and the server is tightened with it, so the two ends of the wire
    // cannot disagree about the same bytes. It is kept rather than
    // deleted because the behaviour it covers is still covered — with the
    // OPPOSITE expectation — and the pair is the point: the token alone
    // was once enough, and is now a producer type bug.
    expect(decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA)).toBe(
      "malformed",
    );
  });
  it("an absent token is absent, never defaulted to known", () => {
    // The counterweight to the case above: §7's legacy grace is keyed on
    // the ABSENCE of the token, not on the absence of a version. A record
    // with no ``schema`` never reaches the version read at all, so it is
    // absent — not malformed, and not defaulted to the current version.
    expect(decodeEvidenceSchemaField(undefined)).toBe("absent");
  });
  it("a future version is unsupported, never the nearest older parser", () => {
    expect(
      decodeEvidenceSchemaField("fin.evidence.citation.v2", 1),
    ).toBe("unsupported");
  });
  it("reads the recognized token with the current schema_version as known", () => {
    // §2.1: the token names the shape, the integer sibling versions it.
    // Both together is the current contract — the version is not optional
    // dressing once a peer states the shape.
    expect(
      decodeEvidenceSchemaField(
        CITATION_CHANNEL_SCHEMA,
        EVIDENCE_SCHEMA_VERSION,
      ),
    ).toBe("known");
  });
  it("a recognized token with an unsupported schema_version is unsupported", () => {
    // §2.1 rule 3: a recognized NAME carrying an unsupported VERSION is
    // the same named refusal as an unrecognized name — never the
    // nearest older parser, never a version guess.
    expect(
      decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA, 2),
    ).toBe("unsupported");
  });
  it("a recognized token with a non-integer schema_version is malformed", () => {
    // A carrier the contract cannot read at all is a malformed ENVELOPE,
    // distinct from a version this client does not support.
    expect(
      decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA, "1"),
    ).toBe("malformed");
  });
  it("a recognized token with no schema_version stated is malformed", () => {
    // PREMISE CHANGED (ADR 0093 decision 12) — this case previously read
    // "known" and its comment argued the leniency. That argument is
    // withdrawn: a token and its version are minted together, so a
    // carrier that names the shape and omits the number is a producer
    // type bug on this side's reading AND on the server's, and one side
    // waving it through while the other refuses it is the split this
    // boundary must not have. No back-fill: returning "known" here would
    // mean inventing a version the producer never stated.
    //
    // It is the second half of the "token alone" pair above (which now
    // also reads malformed) and is kept separately because this is the
    // case the two-argument form — a caller that FORGOT to pass the
    // version — reaches, whereas the one above is the literal no-argument
    // call. Both are the same refusal; the pair guards against a future
    // edit restoring the leniency at only one of them.
    expect(decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA)).toBe(
      "malformed",
    );
  });
  it("an unknown token is unsupported regardless of the version beside it", () => {
    expect(
      decodeEvidenceSchemaField("fin.evidence.citation.v2", 1),
    ).toBe("unsupported");
  });
  it("classifies an unknown token without its required version as malformed", () => {
    // §2.1 makes the token/version pair atomic at every boundary. Missing
    // version is a malformed half-contract even when the token is unknown;
    // token recognition must not mask the more basic pair-shape failure.
    expect(
      decodeEvidenceSchemaField("fin.evidence.citation.v2"),
    ).toBe("malformed");
  });
  it("a non-string token is malformed", () => {
    expect(decodeEvidenceSchemaField(7)).toBe("malformed");
    expect(decodeEvidenceSchemaField(null)).toBe("malformed");
  });
});

// ----- the numeric rule on the version itself (ADR 0093 decision 8) -----
//
// WHY this block is separate from the block above: the cases above are
// about the TOKEN (recognized, unknown, absent, wrong type) and pass a
// version that is always a plain integer. The version's own type is a
// second question, and it was NOT covered — the pre-existing
// "non-integer schema_version" case passed the STRING "1" only, so a
// decoder that accepted a fractional number, a boolean, or NaN would
// have been green.
//
// The rule is over the decoded VALUE and is a FINITE-INTEGER rule.
// `Number.isInteger` happens to cover all three words in one test
// (it excludes every non-number type, `NaN`, and both infinities),
// which is why these are four inputs and one predicate.
//
// WHAT IS DELIBERATELY ABSENT: no case asserts that a literal `1.0` is
// refused. `JSON.parse("1.0")` yields the integer `1`, so the spelling
// is gone before this boundary sees it and such a requirement is not
// implementable (ADR 0093 decision 8, "no test may assert that `1.0` is
// refused"). The accept half of that rule is pinned by the shared case
// set below, which is what both languages read.

describe("schemaVersionRefusal — the numeric rule", () => {
  it.each([
    ["a fractional number", 1.5],
    ["a string", "1"],
    ["a boolean true", true],
    ["a boolean false", false],
    ["null", null],
    ["a list", []],
    ["an object", {}],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["-Infinity", Number.NEGATIVE_INFINITY],
  ])("refuses %s as malformed, not unsupported", (_label, version) => {
    // The CLASS matters as much as the refusal: "unsupported" reads to
    // an operator as "your peer runs a different version, bump it", and
    // answering a boolean or a NaN that way sends the fix to the wrong
    // place. Every value here is a producer type bug.
    expect(schemaVersionRefusal(version)).toBe("malformed_evidence_contract");
    expect(
      decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA, version),
    ).toBe("malformed");
  });

  it.each([
    ["a finite integer that is not the current version", 2],
    ["zero", 0],
    ["a negative integer", -1],
    ["a far-future integer", 99],
  ])("refuses %s as unsupported, never as malformed", (_label, version) => {
    expect(schemaVersionRefusal(version)).toBe("unsupported_evidence_contract");
    expect(
      decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA, version),
    ).toBe("unsupported");
  });

  it("accepts the current version and nothing else that is a finite integer", () => {
    expect(schemaVersionRefusal(1)).toBeNull();
    // The value rule, stated on the one input JavaScript CAN represent
    // distinctly: a number whose value has no fractional part. `1.0`
    // written at the source arrives here already folded into `1`.
    expect(schemaVersionRefusal(1.0)).toBeNull();
    expect(schemaVersionRefusal(-0)).toBe("unsupported_evidence_contract");
  });

  it("refuses an absent version as malformed, never defaulting it", () => {
    // `undefined` is what a caller that has no version passes. The
    // §7 legacy grace keys on the TOKEN being absent, so it does not
    // reach here — and defaulting to 1 would invent a declaration the
    // producer never made (ADR 0093 decision 12).
    expect(schemaVersionRefusal(undefined)).toBe(
      "malformed_evidence_contract",
    );
  });
});

// ----- evidence_refusals entries -----

describe("evidence_refusals entry decoding", () => {
  it("decodes an empty evidence_refusals list as defined-empty", () => {
    // Named E6 case (§6.1 empty row): an empty list is NOT a failure and
    // NOT a missing channel — it decodes to zero refusals without
    // fabricating any.
    expect(decodeEvidenceRefusalsFrame({ refusals: [] })).toEqual([]);
  });

  it("refuses a malformed evidence_refusals entry with a non-integer index", () => {
    // Named E6 case (§6.1 negative row): a fractional index cannot
    // address a row; the entry ITSELF becomes a visible malformed refusal.
    const out = decodeEvidenceRefusalsFrame({
      refusals: [{ citation_index: 1.5, error: "malformed_evidence_contract" }],
    });
    expect(out).toHaveLength(1);
    expect(out[0].error).toBe("malformed_evidence_contract");
    expect(Number.isInteger(out[0].citation_index)).toBe(true);
  });

  it("refuses an evidence_refusals entry with a negative index as malformed", () => {
    const out = decodeEvidenceRefusalsFrame({
      refusals: [{ citation_index: -2, error: "citation_overflow" }],
    });
    expect(out).toEqual([
      { citation_index: 0, error: "malformed_evidence_contract" },
    ]);
  });

  it("refuses an evidence_refusals entry with an unknown code as malformed", () => {
    // Named E6 case: prose in the code slot is not §6 vocabulary; the
    // entry is reported, never swallowed.
    const out = decodeEvidenceRefusalsFrame({
      refusals: [{ citation_index: 2, error: "row looked wrong somehow" }],
    });
    expect(out).toEqual([
      { citation_index: 2, error: "malformed_evidence_contract" },
    ]);
  });

  it("keeps a valid entry verbatim", () => {
    const out = decodeEvidenceRefusalsFrame({
      refusals: [{ citation_index: 3, error: "malformed_evidence_contract" }],
    });
    expect(out).toEqual([
      { citation_index: 3, error: "malformed_evidence_contract" },
    ]);
  });

  it("accepts every §6 lane code, incl. delegated gate exits", () => {
    const codes = [
      "malformed_evidence_contract",
      "unsupported_evidence_contract",
      "citation_overflow",
      "database_row_overflow",
      "visual_claim_overflow",
      "sql_query_denied",
      "citation_trust_denied",
      "unknown_locator",
      "citation_repair_cap",
    ];
    for (const code of codes) {
      const entry = decodeEvidenceRefusalEntry(
        { citation_index: 0, error: code },
        0,
      );
      expect(entry.status).toBe("ok");
      if (entry.status === "ok") expect(entry.refusal.error).toBe(code);
    }
  });

  it("a payload that is not the {refusals: []} shape carries no entries", () => {
    expect(decodeEvidenceRefusalsFrame(null)).toEqual([]);
    expect(decodeEvidenceRefusalsFrame({ refusals: "nope" })).toEqual([]);
  });
});

// ----- citation rows (current frozen shape + v0 legacy per §7) -----

function companyRow(overrides: Record<string, unknown> = {}) {
  return {
    kind: "company",
    document_id: "AAPL-2022Q3",
    attachment_id: "",
    version: "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2",
    unit: 4,
    display: "page",
    filename: "",
    quote: "Total net sales increased during the third quarter",
    ...overrides,
  };
}

describe("decodeCitationRow", () => {
  it("decodes a valid company row", () => {
    const out = decodeCitationRow(companyRow(), 0);
    expect(out.status).toBe("ok");
  });

  it("refuses an unknown key as malformed, never a silent drop of the field", () => {
    // Fixture: citation/unknown-field.json (expect malformed).
    const out = decodeCitationRow(companyRow({ page_no: 3 }), 1);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("malformed_evidence_contract");
  });

  it("refuses a kind outside the quotable set as unsupported", () => {
    // Fixture: citation/unsupported-kind-visual.json.
    const out = decodeCitationRow(companyRow({ kind: "visual_observation" }), 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("unsupported_evidence_contract");
  });

  it("decodes a short quote — the 32-code-point floor is the span verifier's rule, not this boundary", () => {
    // §6 verifier exits are gate-owned: a quote that fails the floor is
    // refused upstream and reaches the web as a named ``evidence_refusals``
    // entry (pinned by the entry tests above), never as a row the web
    // re-adjudicates without the received text.
    const out = decodeCitationRow(companyRow({ quote: "too short" }), 0);
    expect(out.status).toBe("ok");
  });

  it("refuses a quote above 2,000 code points as citation_overflow", () => {
    // Fixture: citation/overflow-quote-2001.json — a transport bound
    // decidable from the row alone (§3): refused, never truncated.
    const out = decodeCitationRow(companyRow({ quote: "x".repeat(2001) }), 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("citation_overflow");
  });

  it("counts the ceiling in code points, not UTF-16 units", () => {
    // 2,001 astral characters are 4,002 UTF-16 units — a length check on
    // ``string.length`` would pass a row the bound breaks.
    const out = decodeCitationRow(companyRow({ quote: "𝄞".repeat(2001) }), 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("citation_overflow");
  });

  it("accepts a quote at the 2,000-code-point ceiling", () => {
    const out = decodeCitationRow(companyRow({ quote: "x".repeat(2000) }), 0);
    expect(out.status).toBe("ok");
  });

  it("refuses a wrong-typed unit as malformed", () => {
    // Fixture: citation/wrong-type-unit.json.
    const out = decodeCitationRow(companyRow({ unit: "4" }), 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("malformed_evidence_contract");
  });

  it("refuses a database-row placeholder break as malformed", () => {
    const snapshot = {
      schema: DATABASE_ROW_SNAPSHOT_SCHEMA,
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: "30" },
      summary: "s",
    };
    const out = decodeCitationRow(
      companyRow({
        kind: "database-row",
        document_id: "",
        display: "page", // the frozen placeholder is "record" (§4.4)
        database_row: snapshot,
      }),
      0,
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("malformed_evidence_contract");
  });

  it("refuses a mirror mismatch (quote != summary) as malformed", () => {
    // Fixture: database_row/mirror-mismatch-version.json.
    const caseFile = fixture<ConformanceCase<{
      evidence_schema: string;
      citations: Record<string, unknown>[];
    }>>("database_row", "mirror-mismatch-version.json");
    const out = decodeCitationRow(caseFile.payload.citations[0], 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe(caseFile.refusal_code);
  });

  it("reads a v0 row through the frozen legacy defaults (§7.1)", () => {
    // A T3-era row: no kind / attachment_id / filename / display, and
    // ``page`` instead of ``unit``. Absent kind reads as the document
    // kind ONLY while no lane marker is present.
    const out = decodeCitationRow(
      {
        document_id: "AAPL-2022Q3",
        version: "v1",
        page: 4,
        quote: "Total net sales increased during the third quarter",
      },
      0,
    );
    expect(out.status).toBe("ok");
    if (out.status === "ok") {
      expect(out.citation.kind).toBe("company");
      expect(out.citation.unit).toBe(4); // page -> unit is an identity map
      expect(out.citation.display).toBe("page");
      expect(out.citation.attachment_id).toBe("");
      expect(out.citation.filename).toBe("");
    }
  });

  it("refuses a v0 row carrying BOTH unit and page as a contradiction", () => {
    const out = decodeCitationRow(
      {
        document_id: "D",
        version: "v1",
        unit: 3,
        page: 4,
        quote: "Total net sales increased during the third quarter",
      },
      0,
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("unsupported_evidence_contract");
  });

  it("refuses a v0 row carrying BOTH unit and page even when they AGREE", () => {
    // Conformance case citation/v0-unit-page-both-equal.json: one
    // authority (§7.3, the server's frozen replay projection) — both
    // names is a contradiction whatever their values; equality never
    // rescues a row that states its ordinal twice.
    const caseFile = fixture<{
      case: string;
      expect: "accept" | "refuse";
      refusal_code: string | null;
      citation: Record<string, unknown>;
    }>("citation", "v0-unit-page-both-equal.json");
    const out = decodeCitationRow(caseFile.citation, 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal") expect(out.refusal.error).toBe(caseFile.refusal_code);
  });

  it("refuses a v0 row carrying NEITHER unit nor page", () => {
    const out = decodeCitationRow(
      {
        document_id: "D",
        version: "v1",
        quote: "Total net sales increased during the third quarter",
      },
      0,
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("unsupported_evidence_contract");
  });

  it("refuses a lane marker on a v0 (kindless) row", () => {
    const out = decodeCitationRow(
      {
        document_id: "D",
        version: "v1",
        page: 1,
        quote: "x".repeat(32),
        database_row: {},
      },
      0,
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe("malformed_evidence_contract");
  });
});

// ----- citations frame (array + envelope) -----

describe("decodeCitationsArray / decodeCitationsEnvelope", () => {
  it("refuses an over-limit citations frame as citation_overflow with no partial render", () => {
    // Named E6 case (§6.1 overflow row): >32 rows refuse the frame —
    // the consumer never truncates and never renders 32 of 33.
    const rows = Array.from({ length: 33 }, () => companyRow());
    const out = decodeCitationsArray(rows);
    expect(out.citations).toEqual([]);
    expect(out.refusals).toEqual([
      { citation_index: 0, error: "citation_overflow" },
    ]);
  });

  it("keeps an empty citations array defined-empty, not a failure", () => {
    // Fixture: citation/citations-empty-array.json (expect accept).
    const caseFile = fixture<ConformanceCase>("citation", "citations-empty-array.json");
    const out = decodeCitationsEnvelope(caseFile.payload);
    expect(caseFile.expect).toBe("accept");
    expect(out.citations).toEqual([]);
    expect(out.refusals).toEqual([]);
  });

  it("a non-array citations payload is a named malformed refusal", () => {
    const out = decodeCitationsArray("not-a-list");
    expect(out.citations).toEqual([]);
    expect(out.refusals).toEqual([
      { citation_index: 0, error: "malformed_evidence_contract" },
    ]);
  });

  it("decodes the fixture envelope for valid company + attachment rows", () => {
    for (const name of ["valid-company", "valid-attachment"]) {
      const caseFile = fixture<ConformanceCase<{
        citations: unknown[];
      }>>("citation", `${name}.json`);
      const out = decodeCitationsEnvelope(caseFile.payload);
      expect(caseFile.expect).toBe("accept");
      expect(out.refusals).toEqual([]);
      expect(out.citations).toHaveLength(1);
    }
  });

  it("refuses the fixture's 33-citation envelope as the named overflow", () => {
    const caseFile = fixture<ConformanceCase<{
      citations: unknown[];
    }>>("citation", "overflow-citations-33.json");
    const out = decodeCitationsEnvelope(caseFile.payload);
    expect(caseFile.refusal_code).toBe("citation_overflow");
    expect(out.citations).toEqual([]);
    expect(out.refusals).toEqual([
      { citation_index: 0, error: "citation_overflow" },
    ]);
  });

  it("refuses an unsupported envelope schema as a typed unsupported outcome", () => {
    const caseFile = fixture<ConformanceCase<{
      evidence_schema: string;
    }>>("citation", "unsupported-evidence-schema.json");
    expect(caseFile.payload.evidence_schema).not.toBe(CITATION_CHANNEL_SCHEMA);
    const out = decodeCitationsEnvelope(caseFile.payload);
    expect(out.envelope).toBe("unsupported");
    expect(out.refusals.some((r) => r.error === "unsupported_evidence_contract")).toBe(true);
  });

  it("refuses a citation frame with the current evidence_schema but a wrong schema_version", () => {
    // §2.1 rule 3 on the channel envelope: the token is the one this
    // client knows by heart, so only the version can be wrong. Driven
    // from the committed case — its expected code is the fixture's own
    // ``refusal_code``, defined independently of the decoder.
    const caseFile = fixture<ConformanceCase<{
      evidence_schema: string;
      schema_version: number;
      citations: unknown[];
    }>>("citation", "unsupported-schema-version.json");
    expect(caseFile.payload.evidence_schema).toBe(CITATION_CHANNEL_SCHEMA);
    expect(caseFile.payload.schema_version).not.toBe(EVIDENCE_SCHEMA_VERSION);
    expect(caseFile.refusal_code).toBe("unsupported_evidence_contract");
    const out = decodeCitationsEnvelope(caseFile.payload);
    expect(out.envelope).toBe("unsupported");
    expect(out.refusals).toContainEqual({
      citation_index: 0,
      error: "unsupported_evidence_contract",
    });
  });

  it("accepts a citation frame stating the current schema and version", () => {
    const caseFile = fixture<ConformanceCase<{
      evidence_schema: string;
      schema_version: number;
      citations: unknown[];
    }>>("citation", "valid-company.json");
    expect(caseFile.payload.evidence_schema).toBe(CITATION_CHANNEL_SCHEMA);
    expect(caseFile.payload.schema_version).toBe(EVIDENCE_SCHEMA_VERSION);
    const out = decodeCitationsEnvelope(caseFile.payload);
    expect(caseFile.expect).toBe("accept");
    expect(out.envelope).toBe("known");
    expect(out.refusals).toEqual([]);
    expect(out.citations).toHaveLength(1);
  });

  it("keeps the refusing row's original frame index on per-row refusals", () => {
    const out = decodeCitationsArray([companyRow(), companyRow({ unit: "4" })]);
    expect(out.citations).toHaveLength(1);
    expect(out.refusals).toEqual([
      { citation_index: 1, error: "malformed_evidence_contract" },
    ]);
  });
});

// ----- database_row snapshot (spec §4.4, R2 scalars-plus-text) -----

describe("decodeDatabaseRowSnapshot", () => {
  it("accepts the committed egress snapshot (string fields)", () => {
    const caseFile = fixture<ConformanceCase<{
      citations: { database_row: unknown }[];
    }>>("database_row", "valid-egress-citation.json");
    const out = decodeDatabaseRowSnapshot(caseFile.payload.citations[0].database_row);
    expect(caseFile.expect).toBe("accept");
    expect(out.status).toBe("ok");
  });

  it("accepts scalar field values and renders them to text (R2)", () => {
    // The committed scalar is ``notice_days: 30`` in
    // database_row/valid-ingress-envelope.json; the independently
    // defined expected text is the digest worksheet's identity_input_m,
    // which spells it "30" (spec §4.4 text rule).
    const ingress = fixture<ConformanceCase<{
      database_rows: { rows: unknown[] };
    }>>("database_row", "valid-ingress-envelope.json");
    // ``"stored-replay"`` because the committed row states the frozen v0
    // FIVE-KEY shape (spec §7.1: "v0 snapshot: the five keys of §4.4"),
    // which is the same key shape a pre-E5 stored snapshot has and the
    // same one this fixture predates the token by. The rule this test
    // pins — a JSON scalar renders to its §4.4 text — is mode-independent
    // and runs after the pairing guard, so nothing about the assertion
    // is relaxed by naming the door.
    const out = decodeDatabaseRowSnapshot(
      ingress.payload.database_rows.rows[0],
      "stored-replay",
    );
    expect(out.status).toBe("ok");
    if (out.status !== "ok") return;
    const worksheet = fixture<{
      snapshots: { identity_input_m: { fields: Record<string, string> } }[];
    }>("database_row", "digest_worksheet.json");
    const expectedFields = worksheet.snapshots[0].identity_input_m.fields;
    const rendered: Record<string, string> = {};
    for (const [k, v] of Object.entries(out.snapshot.fields)) {
      rendered[k] = snapshotValueText(v);
    }
    expect(rendered).toEqual(expectedFields);
  });

  it("measures the byte bound on the worksheet's canonical UTF-8 bytes", () => {
    // M-11 decision pin: the web NEVER recomputes the hex digest (the
    // wire carries none; ADR 0092 mints it server-side at admission —
    // see spec section 9), but its 32 KiB overflow bound must be
    // measured on the same canonical form the server hashes. The
    // independently defined expected value is the committed worksheet's
    // own canonical_json strings (spec section 4.4).
    const worksheet = fixture<{
      snapshots: {
        case: string;
        raw_ingress_snapshot?: unknown;
        canonical_json?: string;
      }[];
    }>("database_row", "digest_worksheet.json");
    let checked = 0;
    for (const snap of worksheet.snapshots) {
      if (!snap.raw_ingress_snapshot || !snap.canonical_json) continue;
      // The worksheet's ``raw_ingress_snapshot`` is the keyless v0 five-key
      // shape, so it is read on the replay door for the same reason as
      // the ingress envelope above; the canonical-JSON byte form this test
      // measures is built from the five value keys and is mode-independent.
      const out = decodeDatabaseRowSnapshot(
        snap.raw_ingress_snapshot,
        "stored-replay",
      );
      expect(out.status, snap.case).toBe("ok");
      if (out.status !== "ok") continue;
      expect(canonicalSnapshotBytes(out.snapshot), snap.case).toBe(
        new TextEncoder().encode(snap.canonical_json).length,
      );
      checked += 1;
    }
    expect(checked).toBe(2);
  });

  it("renders each JSON scalar to its canonical text form", () => {
    expect(snapshotValueText("plain")).toBe("plain");
    expect(snapshotValueText(30)).toBe("30");
    expect(snapshotValueText(-0.5)).toBe("-0.5");
    expect(snapshotValueText(true)).toBe("true");
    expect(snapshotValueText(false)).toBe("false");
    expect(snapshotValueText(null)).toBe("");
  });

  it("refuses an unknown snapshot schema as unsupported, never v0 fallback", () => {
    // The negative guarantee the §7 grace must NOT swallow: a snapshot
    // that DOES state a ``schema`` and states one this boundary does not
    // recognize is refused by name, never read as the keyless legacy v0
    // shape. Driven from the committed case, whose ENVELOPE token
    // (``fin.evidence.retrieve_rows``) is a real token off the wire but
    // the wrong family for a snapshot, grafted onto a row that is
    // otherwise shape-valid — so the refusal can only come from the
    // token check, never from the row's contents. The expected code is
    // the case's own independently defined ``refusal_code``.
    const caseFile = fixture<ConformanceCase<{
      database_rows: {
        schema: string;
        schema_version: number;
        rows: unknown[];
      };
    }>>("database_row", "unsupported-snapshot-version.json");
    const row = {
      ...(caseFile.payload.database_rows.rows[0] as Record<string, unknown>),
      schema: caseFile.payload.database_rows.schema,
      schema_version: caseFile.payload.database_rows.schema_version,
    };
    expect(row.schema).not.toBe(DATABASE_ROW_SNAPSHOT_SCHEMA);
    expect(caseFile.refusal_code).toBe("unsupported_evidence_contract");
    const out = decodeDatabaseRowSnapshot(row);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe(caseFile.refusal_code);
  });

  it("classifies an unknown snapshot token without its required version as malformed", () => {
    // §2.1 requires every object carrying `schema` to carry the version
    // beside it. The invalid pair takes precedence over whether this
    // snapshot decoder recognizes the token.
    const out = decodeDatabaseRowSnapshot({
      schema: "fin.evidence.retrieve_rows",
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30 },
      summary: "notice in 30 days",
    });
    expect(out).toEqual({
      status: "refusal",
      error: "malformed_evidence_contract",
    });
  });

  it("refuses a snapshot row carrying a key outside the closed set, never a silent drop", () => {
    // The ingress row is held to exactly its frozen keys, so the lane
    // ``kind`` marker riding the row is refused by name. This is the
    // surviving STRUCTURAL refusal for the row itself, and it pins the
    // same no-silent-downgrade guarantee from the other side: the stray
    // key is not quietly stripped and the row read as an accepted v0.
    // The expected code is the case's own ``refusal_code``.
    const caseFile = fixture<ConformanceCase<{
      database_rows: { rows: unknown[] };
    }>>("database_row", "snapshot-unknown-field-kind.json");
    const out = decodeDatabaseRowSnapshot(caseFile.payload.database_rows.rows[0]);
    expect(caseFile.refusal_code).toBe("malformed_evidence_contract");
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe(caseFile.refusal_code);
  });

  it("accepts the committed ingress row as the keyless v0 shape (§7 grace, replay door)", () => {
    // The counterweight to the two refusals above: the same case's row,
    // as committed, states NO ``schema`` and is therefore the legacy v0
    // shape. Pinning it stops a future edit from re-adding a row-level
    // ``schema`` token to make some other case refuse — that key is
    // outside the closed set and would be refused as an unknown key
    // whatever value it held.
    //
    // PREMISE CHANGED (the door, not the rule). This assertion read the
    // row through the default, which was the replay shape before the
    // emission/replay split. The v0 grace is real and still asserted —
    // on the door it belongs to. It now passes ``"stored-replay"``
    // explicitly, because a keyless row is what a pre-E5 STORED record
    // looks like and what an ACTIVE frame may not look like: spec §7.5
    // scopes the grace to "stored records, not to fresh producers", and
    // ADR 0093 decision 12 makes the same cut ("an ACTIVE frame may not
    // borrow the legacy rule to skip version validation"). The two tests
    // below pin BOTH sides of that cut on these same bytes, so the grace
    // is narrowed deliberately and visibly rather than deleted.
    const caseFile = fixture<ConformanceCase<{
      database_rows: { rows: Record<string, unknown>[] };
    }>>("database_row", "unsupported-snapshot-version.json");
    const row = caseFile.payload.database_rows.rows[0];
    expect("schema" in row).toBe(false);
    const out = decodeDatabaseRowSnapshot(row, "stored-replay");
    expect(out.status).toBe("ok");
    if (out.status === "ok") {
      expect(out.snapshot.schema).toBeUndefined();
      expect(out.snapshot.schema_version).toBeUndefined();
    }
  });

  it("accepts a v0 snapshot without a schema token on the replay door (§7 grace)", () => {
    // The §7 replay grace, pinned directly rather than only through a
    // fixture. ``_serialize_run`` publishes a pre-E5 stored snapshot
    // verbatim, so this is a shape the history read really serves; see
    // the split test below for the same bytes refused on the emission
    // door.
    const out = decodeDatabaseRowSnapshot(
      {
        source: "ledger_ro.contract_expiry",
        record_key: { contract_id: "C-2291" },
        queried_at: "2026-03-01T09:14:22Z",
        fields: { notice_days: 30 },
        summary: "notice in 30 days",
      },
      "stored-replay",
    );
    expect(out.status).toBe("ok");
  });

  it("refuses a snapshot whose recognized schema carries an unsupported schema_version", () => {
    // §2.1 rule 3: a recognized NAME with an unsupported VERSION is the
    // same named refusal as an unknown name. Driven from the committed
    // fixture so the expected code is the case's own independently
    // defined ``refusal_code``, not the decoder's output read back.
    //
    // REPOINTED (was: database_row/unsupported-envelope-schema-version.json,
    // deleted as a duplicate of the retained case). The assertion is
    // UNCHANGED and its premise still holds on the fixture loaded below:
    // a RECOGNIZED egress snapshot token carrying a version that is not
    // the current one is refused with unsupported_evidence_contract.
    // The retained case is an ENVELOPE-version case (its own manifest note
    // says so: the name is historical and the case was re-pointed at the
    // envelope's schema_version), so it declares the skew on
    // ``fin.evidence.retrieve_rows`` rather than on a row. That is not a
    // weaker case for this boundary: the value it supplies — 2, a finite
    // integer that is not the current version — is put on the egress
    // snapshot token this test actually decodes, and the expected code
    // still comes from the case's own ``refusal_code``, not from the
    // decoder. What it can no longer supply is the ingress envelope's
    // token, which no web test decodes.
    const caseFile = fixture<ConformanceCase<{
      database_rows: {
        schema: string;
        schema_version: number;
        rows: Record<string, unknown>[];
      };
    }>>("database_row", "unsupported-snapshot-version.json");
    const row = { ...caseFile.payload.database_rows.rows[0] };
    // The row itself states no token (the v0 ingress shape, §7), so the
    // skew can only come from the version this test puts on the egress
    // token — the refusal has exactly one possible source.
    expect(caseFile.payload.database_rows.schema).toBe(
      "fin.evidence.retrieve_rows",
    );
    expect("schema" in row).toBe(false);
    expect(caseFile.refusal_code).toBe("unsupported_evidence_contract");
    const out = decodeDatabaseRowSnapshot({
      ...row,
      schema: DATABASE_ROW_SNAPSHOT_SCHEMA,
      schema_version: caseFile.payload.database_rows.schema_version,
    });
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe(caseFile.refusal_code);
  });

  it("accepts a snapshot with the current schema and the current schema_version", () => {
    const out = decodeDatabaseRowSnapshot({
      schema: DATABASE_ROW_SNAPSHOT_SCHEMA,
      schema_version: EVIDENCE_SCHEMA_VERSION,
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30 },
      summary: "notice in 30 days",
    });
    expect(out.status).toBe("ok");
    // The rebuild carries BOTH contract fields: dropping the version the
    // wire stated would lose a key the row declared.
    if (out.status === "ok") {
      expect(out.snapshot.schema).toBe(DATABASE_ROW_SNAPSHOT_SCHEMA);
      expect(out.snapshot.schema_version).toBe(EVIDENCE_SCHEMA_VERSION);
    }
  });

  it("refuses a token-less snapshot on the emission door, the class the server gives (§2.1 / decision 12)", () => {
    // PREMISE CHANGED — THIS ASSERTION IS FLIPPED, not deleted, and the
    // previous version is kept below so neither half is lost.
    //
    // What moved: this test used to read a snapshot carrying
    // ``schema_version`` with NO ``schema`` and assert it was ACCEPTED,
    // on the reasoning that "the §7 grace is keyed on the token, so the
    // version check never runs against a token-less record". That
    // reasoning was applied to a path it does not govern. It was true
    // of the STORED REPLAY read; it was false of the EMISSION read, and
    // the decoder served both from one default.
    //
    // Deciding authority: ADR 0093 decision 12, together with decision 8
    // — half a contract is a producer type bug on BOTH sides' reading,
    // and "no back-fill, anywhere" names the web decoder by name.
    // Decision 12's third constraint is the one that settles which door
    // this assertion belongs to: "An ACTIVE frame may not borrow the
    // legacy rule to skip version validation — a fresh emission is not a
    // stored legacy record." Spec §7.5 says the same in the spec's own
    // words: "the grace applies to stored records, not to fresh
    // producers."
    //
    // Why it had to move rather than stay documented: the server refuses
    // these exact bytes at the live frame. ``_validate_snapshot`` holds
    // an emission snapshot to ``_SNAPSHOT_FIELDS``, so a token-less one
    // is ``malformed_evidence_contract`` ("database_row snapshot: missing
    // ['schema']") — observed, not assumed. While the web accepted them,
    // the two ends decoded the same bytes to different answers, which is
    // exactly the split decision 12 exists to close and which the shared
    // 18-case set cannot see (every case there rides a RECOGNIZED token).
    //
    // The emit door is the DEFAULT, so this test also pins that a fresh
    // call site is strict without having to say so.
    const stray = decodeDatabaseRowSnapshot({
      schema_version: EVIDENCE_SCHEMA_VERSION,
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30 },
      summary: "notice in 30 days",
    });
    expect(stray.status).toBe("refusal");
    if (stray.status === "refusal")
      expect(stray.error).toBe("malformed_evidence_contract");
    // The same refusal through the live SSE entry point, which reaches
    // the snapshot through a citation row: the strictness is the door's,
    // not this one function's private default.
    const viaFrame = decodeCitationsArray([
      {
        kind: "database-row",
        document_id: "",
        attachment_id: "",
        version: "2026-03-01T09:14:22Z",
        unit: 1,
        display: "record",
        filename: "",
        quote: "notice in 30 days",
        database_row: {
          schema_version: EVIDENCE_SCHEMA_VERSION,
          source: "ledger_ro.contract_expiry",
          record_key: { contract_id: "C-2291" },
          queried_at: "2026-03-01T09:14:22Z",
          fields: { notice_days: 30 },
          summary: "notice in 30 days",
        },
      },
    ]);
    expect(viaFrame.citations).toEqual([]);
    expect(viaFrame.refusals).toEqual([
      { citation_index: 0, error: "malformed_evidence_contract" },
    ]);
  });

  it("the SAME bytes: refused on the emission door, accepted on the replay door (§7 split)", () => {
    // The split is INTENTIONAL and this is what makes it visible. One
    // object literal, two doors, two outcomes — so a reader can see that
    // the difference is the caller's declared intent and not a bug in
    // one of the branches.
    //
    // The replay half is the PREMISES-CHANGED test's original assertion,
    // preserved rather than removed: on the stored door the token-less
    // snapshot is the pre-E5 v0 shape spec §7.1 leaves "exactly as it
    // are", and the version it cannot pair with a token is DROPPED from
    // the rebuild rather than kept as half a contract. §7 replay grace is
    // therefore still covered — on the door where it is a live contract,
    // not merely tolerated.
    //
    // This is not the web inventing a distinction: the server draws the
    // same line at the same two doors. ``_citations_wire_projection``
    // (the live SSE frame) refuses these bytes, and ``_serialize_run``
    // (the history read) publishes a token-less stored snapshot
    // verbatim — observed from the running server, which is why the
    // replay door cannot simply be tightened without breaking reload of
    // every pre-cutover thread.
    const bytes = {
      schema_version: EVIDENCE_SCHEMA_VERSION,
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30 },
      summary: "notice in 30 days",
    };
    const emitted = decodeDatabaseRowSnapshot(bytes, "emission");
    expect(emitted.status).toBe("refusal");
    if (emitted.status === "refusal")
      expect(emitted.error).toBe("malformed_evidence_contract");

    const replayed = decodeDatabaseRowSnapshot(bytes, "stored-replay");
    expect(replayed.status).toBe("ok");
    if (replayed.status === "ok") {
      // Neither contract field is invented for a row that stated neither,
      // and the stray version is dropped rather than kept as half a pair.
      expect(replayed.snapshot.schema).toBeUndefined();
      expect(replayed.snapshot.schema_version).toBeUndefined();
      expect(replayed.snapshot.source).toBe("ledger_ro.contract_expiry");
    }
  });

  it("refuses a snapshot whose schema_version is not a number", () => {
    const out = decodeDatabaseRowSnapshot({
      schema: DATABASE_ROW_SNAPSHOT_SCHEMA,
      schema_version: "1",
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30 },
      summary: "notice in 30 days",
    });
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe("malformed_evidence_contract");
  });

  it("refuses a wrong-typed record_key as malformed", () => {
    const caseFile = fixture<ConformanceCase<{
      database_rows: { rows: unknown[] };
    }>>("database_row", "record-key-wrong-type.json");
    const out = decodeDatabaseRowSnapshot(caseFile.payload.database_rows.rows[0]);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe(caseFile.refusal_code);
  });

  it("refuses a malformed timestamp as malformed", () => {
    for (const name of [
      "malformed-timestamp-offset",
      "malformed-timestamp-fraction",
      "malformed-timestamp-leap-second",
    ]) {
      const caseFile = fixture<ConformanceCase<{
        database_rows: { rows: unknown[] };
      }>>("database_row", `${name}.json`);
      const out = decodeDatabaseRowSnapshot(caseFile.payload.database_rows.rows[0]);
      expect(out.status, name).toBe("refusal");
      if (out.status === "refusal")
        expect(out.error, name).toBe("malformed_evidence_contract");
    }
  });

  it("refuses more than 16 fields as database_row_overflow", () => {
    const caseFile = fixture<ConformanceCase<{
      database_rows: { rows: unknown[] };
    }>>("database_row", "overflow-fields-17.json");
    // Replay door: the committed row is the frozen v0 five-key shape, and
    // these two overflow tests exist to pin the §3 BOUNDS, not the
    // pairing. Read on the emission door the missing-``schema`` guard
    // would exit first and the declared ``database_row_overflow`` would
    // never be reached — the expected code is unchanged, and the byte /
    // entry bounds still run in full behind the guard.
    const out = decodeDatabaseRowSnapshot(
      caseFile.payload.database_rows.rows[0],
      "stored-replay",
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe(caseFile.refusal_code);
  });

  it("refuses a >32 KiB serialized snapshot as the byte-bound overflow", () => {
    const caseFile = fixture<ConformanceCase<{
      database_rows: { rows: unknown[] };
    }>>("database_row", "overflow-snapshot-33kib.json");
    // Replay door, for the same reason as the field-count overflow above:
    // this pins the §3 byte bound, and the committed row is the keyless
    // v0 five-key shape.
    const out = decodeDatabaseRowSnapshot(
      caseFile.payload.database_rows.rows[0],
      "stored-replay",
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe("database_row_overflow");
  });

  it("rejects a nested mapping in fields — a mapping is never nested (§4.4)", () => {
    const out = decodeDatabaseRowSnapshot({
      source: "v",
      record_key: { id: "1" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { nested: { a: 1 } },
      summary: "s",
    });
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.error).toBe("malformed_evidence_contract");
  });
});

// ----- visual claims (schema token when present, v0 still valid) -----

describe("decodeVisualClaimRow", () => {
  it("accepts a row carrying the frozen schema token", () => {
    const caseFile = fixture<ConformanceCase<{
      visual_claims: unknown[];
    }>>("visual_claim", "valid-whole-page.json");
    const out = decodeVisualClaimRow(caseFile.payload.visual_claims[0], 0);
    expect(caseFile.expect).toBe("accept");
    expect(out.status).toBe("ok");
  });

  it("refuses an unknown visual schema as unsupported", () => {
    const caseFile = fixture<ConformanceCase<{
      visual_claims: unknown[];
    }>>("visual_claim", "unsupported-version.json");
    const out = decodeVisualClaimRow(caseFile.payload.visual_claims[0], 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe(caseFile.refusal_code);
  });

  it("refuses a visual row whose recognized schema carries an unsupported schema_version", () => {
    // §2.1 rule 3 on the visual lane: the committed case states the
    // CURRENT shape name and an unsupported version, so the refusal must
    // come from the VERSION and not from a token this client does not
    // know. Driven from the fixture's own independently defined code.
    const caseFile = fixture<ConformanceCase<{
      visual_claims: unknown[];
    }>>("visual_claim", "unsupported-schema-version.json");
    const row = caseFile.payload.visual_claims[0] as Record<string, unknown>;
    expect(row.schema).toBe(VISUAL_CLAIM_SCHEMA);
    expect(row.schema_version).not.toBe(EVIDENCE_SCHEMA_VERSION);
    expect(caseFile.refusal_code).toBe("unsupported_evidence_contract");
    const out = decodeVisualClaimRow(row, 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe(caseFile.refusal_code);
  });

  it("accepts a visual row with the current schema and schema_version", () => {
    const caseFile = fixture<ConformanceCase<{
      visual_claims: unknown[];
    }>>("visual_claim", "valid-whole-page.json");
    const row = caseFile.payload.visual_claims[0] as Record<string, unknown>;
    expect(row.schema).toBe(VISUAL_CLAIM_SCHEMA);
    expect(row.schema_version).toBe(EVIDENCE_SCHEMA_VERSION);
    const out = decodeVisualClaimRow(row, 0);
    expect(out.status).toBe("ok");
  });

  it("refuses a 4,001-code-point detail as visual_claim_overflow", () => {
    const caseFile = fixture<ConformanceCase<{
      visual_claims: unknown[];
    }>>("visual_claim", "overflow-detail-4001.json");
    const out = decodeVisualClaimRow(caseFile.payload.visual_claims[0], 0);
    expect(out.status).toBe("refusal");
    if (out.status === "refusal")
      expect(out.refusal.error).toBe(caseFile.refusal_code);
  });

  it("accepts a v0 fourteen-key row with no schema token (§7)", () => {
    const out = decodeVisualClaimRow(
      {
        kind: "visual_observation",
        attachment_id: "att-1",
        version: "3",
        unit: 2,
        page: 2,
        observation_version: 1,
        scope: null,
        provider: "p",
        model_version: "m",
        page_image_sha256: "a".repeat(64),
        detail: "x".repeat(32),
        filename: "f.txt",
        reused: true,
        outbound_calls: 0,
      },
      0,
    );
    expect(out.status).toBe("ok");
  });

  it("a refusal on this lane never yields a claim object", () => {
    const out = decodeVisualClaimRow(
      { kind: "visual_observation", page: "4" },
      3,
    );
    expect(out.status).toBe("refusal");
    if (out.status === "refusal") {
      expect(out.refusal.citation_index).toBe(3);
      expect(out.refusal.error).toBe("malformed_evidence_contract");
    }
  });
});

// ----- comparison envelope (§4.3; schema + committed manifest) -----

describe("comparison conformance fixtures", () => {
  const shapeCases = comparisonManifest.cases.filter(
    (entry) =>
      entry.direction === "consumer" && entry.class !== "untrusted-identity",
  );

  for (const manifestEntry of shapeCases) {
    it(`matches manifest case ${manifestEntry.case}`, () => {
      const caseFile = fixture<
        ConformanceCase<{ comparison: Record<string, unknown> }>
      >("comparison", manifestEntry.file);
      expect(caseFile.case).toBe(manifestEntry.case);
      expect(caseFile.expect).toBe(manifestEntry.expect);
      expect(caseFile.refusal_code).toBe(manifestEntry.refusal_code);

      const outcome = decodeComparison(caseFile.payload.comparison);
      if (manifestEntry.expect === "accept") {
        expect(outcome.status).toBe("ok");
      } else {
        expect(manifestEntry.refusal_code).not.toBeNull();
        expect(outcome).toEqual({
          status: "refusal",
          error: manifestEntry.refusal_code,
        });
      }
    });
  }

  const validPeriod = fixture<
    ConformanceCase<{ comparison: Record<string, unknown> }>
  >("comparison", "valid-period-table.json").payload.comparison;
  const validEmpty = fixture<
    ConformanceCase<{ comparison: Record<string, unknown> }>
  >("comparison", "cells-all-missing.json").payload.comparison;

  function firstRecord(value: unknown): Record<string, unknown> {
    return (value as Record<string, unknown>[])[0];
  }

  const malformedMutations: {
    name: string;
    source: "period" | "empty";
    mutate: (comparison: Record<string, unknown>) => void;
  }[] = [
    {
      name: "column additional property",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.columns).notes = "internal";
      },
    },
    {
      name: "column key with the wrong type",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.columns).key = 7;
      },
    },
    {
      name: "row additional property",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.rows).notes = "internal";
      },
    },
    {
      name: "cell additional property",
      source: "period",
      mutate: (comparison) => {
        const row = firstRecord(comparison.rows);
        firstRecord(row.cells).notes = "internal";
      },
    },
    {
      name: "cell status outside the closed hit/missing set",
      source: "period",
      mutate: (comparison) => {
        const row = firstRecord(comparison.rows);
        firstRecord(row.cells).status = "unknown";
      },
    },
    {
      name: "cell locator additional property",
      source: "period",
      mutate: (comparison) => {
        const row = firstRecord(comparison.rows);
        const cell = firstRecord(row.cells);
        (cell.locator as Record<string, unknown>).display = "page";
      },
    },
    {
      name: "envelope locator with an unlisted display property",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.locators).display = "page";
      },
    },
    {
      name: "envelope locator with a non-positive unit",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.locators).unit = 0;
      },
    },
    {
      name: "delta additional property",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.deltas).notes = "internal";
      },
    },
    {
      name: "delta side additional property",
      source: "period",
      mutate: (comparison) => {
        const delta = firstRecord(comparison.deltas);
        (delta.from as Record<string, unknown>).notes = "internal";
      },
    },
    {
      name: "delta amount with the wrong type",
      source: "period",
      mutate: (comparison) => {
        firstRecord(comparison.deltas).absolute = "100666";
      },
    },
    {
      name: "missing entry additional property",
      source: "empty",
      mutate: (comparison) => {
        firstRecord(comparison.missing).notes = "internal";
      },
    },
    {
      name: "hit count that disagrees with the grid",
      source: "period",
      mutate: (comparison) => {
        comparison.hit_count = 1;
      },
    },
    {
      name: "cell count that disagrees with the grid",
      source: "period",
      mutate: (comparison) => {
        comparison.cell_count = 1;
      },
    },
  ];

  for (const mutation of malformedMutations) {
    it(`refuses ${mutation.name} as a malformed evidence contract`, () => {
      const baseline = mutation.source === "period" ? validPeriod : validEmpty;
      const candidate = structuredClone(baseline);
      mutation.mutate(candidate);
      expect(decodeComparison(candidate)).toEqual({
        status: "refusal",
        error: "malformed_evidence_contract",
      });
    });
  }
});

// ----- http_sse wire conformance example (E5 committed) -----

describe("http_sse terminal-frame conformance example", () => {
  it("decodes every emitted frame to its defined outcome", () => {
    const caseFile = fixture<ConformanceCase<{
      emitted_frames: { event: string; data: unknown }[];
    }>>("http_sse", "valid-terminal-frames.json");
    expect(caseFile.expect).toBe("accept");
    let citations: unknown[] = [];
    let refusals: EvidenceRefusal[] = [];
    let envelope = "absent";
    for (const frame of caseFile.payload.emitted_frames) {
      if (frame.event === "status") {
        // The version is the envelope's SIBLING of the token (spec §2.1),
        // so the status frame is read as the pair it actually states.
        const status = frame.data as {
          evidence_schema?: unknown;
          schema_version?: unknown;
        };
        envelope = decodeEvidenceSchemaField(
          status.evidence_schema,
          status.schema_version,
        );
      } else if (frame.event === "evidence_refusals") {
        refusals = decodeEvidenceRefusalsFrame(frame.data);
      } else if (frame.event === "citations") {
        citations = decodeCitationsArray(frame.data).citations;
      }
    }
    expect(envelope).toBe("known");
    // The named mid-stream refusal (row 3, display=paragraph) arrives as
    // an entry, and the citations frame decodes the three valid rows with
    // the ledger snapshot kept verbatim.
    expect(refusals).toEqual([
      { citation_index: 3, error: "malformed_evidence_contract" },
    ]);
    expect(citations).toHaveLength(3);
  });
});

// ----- history decoding (ThreadDetail) -----

describe("decodeThreadDetail", () => {
  // SOURCED, NOT HAND-BUILT (the operator's preference, and the reason
  // this fixture changed). The parts of a history body that the
  // evidence contract governs — the envelope token/version pair and the
  // ``rejected_citations`` refusal entries — are read from the committed
  // E5 wire example (``http_sse/valid-terminal-frames.json``), which is
  // what the server actually emits and which the Python side pins live.
  //
  // WHY that matters and not just as tidiness: the previous hand-built
  // body declared ``evidence_schema`` with NO ``schema_version`` and was
  // accepted, which is exactly the payload ADR 0093 decision 12 now
  // refuses. A hand-built fixture can disagree with the wire and nothing
  // says so; sourcing the pair from the committed emission means this
  // body carries the version the server really sends, and if the
  // emission ever dropped the version this test would start failing
  // rather than quietly asserting a shape no producer makes.
  //
  // The THREAD scaffolding around it (id / title / created_at / turns)
  // is still hand-built, and honestly so: no committed thread-detail
  // fixture exists — ``data/conformance/evidence/`` covers the evidence
  // payload roots and the SSE frame sequence, not the history read's
  // envelope. Those four keys are the transport shape the decoder only
  // checks for presence, so a fixture for them would pin nothing about
  // the evidence rules.
  const wire = fixture<ConformanceCase<{
    emitted_frames: {
      event: string;
      data: Record<string, unknown>;
    }[];
  }>>("http_sse", "valid-terminal-frames.json");
  const statusFrame = wire.payload.emitted_frames.find(
    (frame) => frame.event === "status",
  );
  const refusalsFrame = wire.payload.emitted_frames.find(
    (frame) => frame.event === "evidence_refusals",
  );
  if (!statusFrame || !refusalsFrame) {
    throw new Error("committed http_sse example lost its status/refusals frame");
  }
  const historyBody = {
    id: "t-1",
    title: "Q",
    created_at: "2026-01-01T00:00:00Z",
    // The pair as the server emits it: the token AND its integer sibling.
    evidence_schema: statusFrame.data.evidence_schema,
    schema_version: statusFrame.data.schema_version,
    turns: [
      {
        run_id: "r-1",
        question: "q",
        status: "done",
        answer: "a",
        citations: [companyRow()],
        // The refusal rows as the server emits them, verbatim.
        rejected_citations: refusalsFrame.data.refusals,
        created_at: "2026-01-01T00:00:00Z",
      },
    ],
  };

  it("folds history rejected_citations into the turn's visible refusals", () => {
    const detail = decodeThreadDetail(structuredClone(historyBody));
    // The sourced body is a CURRENT envelope: token plus integer version.
    // Asserted explicitly because it is the premise the rest of this
    // describe block rests on — a body whose version is absent is
    // refused, and every assertion below would then be reading a
    // refusal list the ENVELOPE produced rather than the history rows.
    expect(
      decodeEvidenceSchemaField(
        historyBody.evidence_schema,
        historyBody.schema_version,
      ),
    ).toBe("known");
    expect(detail.turns[0].citations).toHaveLength(1);
    // The expected entries are the committed frame's own, not this
    // test's invention: the history read must render the server's
    // refusal rows verbatim.
    expect(detail.turns[0].evidence_refusals).toEqual(
      refusalsFrame.data.refusals,
    );
  });

  it("a clean history row carries a defined-empty refusal list", () => {
    const clean = structuredClone(historyBody);
    clean.turns[0].rejected_citations = [];
    const detail = decodeThreadDetail(clean);
    expect(detail.turns[0].evidence_refusals).toEqual([]);
  });

  it("keeps a valid history comparison and visibly refuses a legacy token without rewriting storage", () => {
    const comparisonCase = fixture<
      ConformanceCase<{ comparison: Record<string, unknown> }>
    >("comparison", "valid-period-table.json");
    expect(comparisonCase.expect).toBe("accept");
    expect(comparisonCase.refusal_code).toBeNull();

    const currentComparison = comparisonCase.payload.comparison;
    const legacyComparison = {
      ...currentComparison,
      schema: "fin.comparison.v1",
    };
    const withComparisons = {
      ...structuredClone(historyBody),
      turns: [
        {
          ...structuredClone(historyBody.turns[0]),
          comparisons: [currentComparison, legacyComparison],
        },
      ],
    };
    const storedBytes = structuredClone(withComparisons);

    const detail = decodeThreadDetail(withComparisons);

    expect(detail.turns[0].comparisons).toEqual([currentComparison]);
    expect(detail.turns[0].evidence_refusals).toContainEqual({
      citation_index: 1,
      error: "unsupported_evidence_contract",
    });
    // Replay is read-only: an unsupported historical token remains intact
    // in the source record, including the separately stored integer version.
    expect(withComparisons).toEqual(storedBytes);
    expect(withComparisons.turns[0].comparisons[1]).toMatchObject({
      schema: "fin.comparison.v1",
      schema_version: 1,
    });
  });

  it("visibly refuses an unknown comparison envelope field while preserving valid history neighbors", () => {
    const validBefore = fixture<
      ConformanceCase<{ comparison: Record<string, unknown> }>
    >("comparison", "valid-period-table.json");
    const invalid = fixture<
      ConformanceCase<{ comparison: Record<string, unknown> }>
    >("comparison", "unknown-field-envelope.json");
    const validAfter = fixture<
      ConformanceCase<{ comparison: Record<string, unknown> }>
    >("comparison", "cells-all-missing.json");
    expect(validBefore.expect).toBe("accept");
    expect(invalid.expect).toBe("refuse");
    expect(invalid.refusal_code).toBe("malformed_evidence_contract");
    expect(validAfter.expect).toBe("accept");

    const withComparisons = {
      ...structuredClone(historyBody),
      turns: [
        {
          ...structuredClone(historyBody.turns[0]),
          comparisons: [
            validBefore.payload.comparison,
            invalid.payload.comparison,
            validAfter.payload.comparison,
          ],
        },
      ],
    };
    const storedBytes = structuredClone(withComparisons);

    const detail = decodeThreadDetail(withComparisons);

    expect(detail.turns[0].comparisons).toEqual([
      validBefore.payload.comparison,
      validAfter.payload.comparison,
    ]);
    expect(detail.turns[0].evidence_refusals).toContainEqual({
      citation_index: 1,
      error: invalid.refusal_code,
    });
    expect(withComparisons).toEqual(storedBytes);
  });

  it("treats absent history comparisons as empty without inventing a refusal", () => {
    const detail = decodeThreadDetail(structuredClone(historyBody));

    expect(detail.turns[0].comparisons).toEqual([]);
    expect(detail.turns[0].evidence_refusals).toEqual(
      refusalsFrame.data.refusals,
    );
    expect(detail.turns[0].evidence_refusals).not.toContainEqual({
      citation_index: 0,
      error: "unsupported_evidence_contract",
    });
  });

  it("a body whose token states no version is refused, not read as current", () => {
    // The transition this fixture had to make, pinned so it cannot be
    // undone: removing the version from a body that KEEPS the token turns
    // the turn's refusals into a named malformed envelope refusal
    // (ADR 0093 decision 12). The envelope is folded into the SAME
    // evidence_refusals field the history rows use, so the refusal is
    // visible rather than an empty success.
    const versionless = { ...structuredClone(historyBody) } as Record<
      string,
      unknown
    >;
    delete versionless.schema_version;
    const detail = decodeThreadDetail(versionless);
    expect(detail.turns[0].evidence_refusals).toContainEqual({
      citation_index: 0,
      error: "malformed_evidence_contract",
    });
  });

  it("an unknown envelope schema names the refusal instead of an empty success", () => {
    const future = {
      ...structuredClone(historyBody),
      evidence_schema: "fin.evidence.citation.v2",
    };
    const detail = decodeThreadDetail(future);
    const codes = detail.turns[0].evidence_refusals?.map((r) => r.error);
    expect(codes).toContain("unsupported_evidence_contract");
  });

  it("refuses a payload that is not a thread-detail object", () => {
    expect(() => decodeThreadDetail("nope")).toThrow();
    expect(() => decodeThreadDetail({ id: "t", title: "x", created_at: "y" })).toThrow();
  });

  it("keeps non-evidence turn fields intact (reload parity)", () => {
    const detail = decodeThreadDetail(structuredClone(historyBody));
    expect(detail.turns[0].run_id).toBe("r-1");
    expect(detail.turns[0].answer).toBe("a");
    expect(detail.id).toBe("t-1");
  });
});

// ----- refusal fold helpers (store ordering invariant) -----

describe("mergeEvidenceRefusals", () => {
  it("unions competing stream/history updates without duplicating or reordering citations", () => {
    const fromStream: EvidenceRefusal[] = [
      { citation_index: 3, error: "malformed_evidence_contract" },
    ];
    const fromHistory: EvidenceRefusal[] = [
      { citation_index: 3, error: "malformed_evidence_contract" },
      { citation_index: 5, error: "unsupported_evidence_contract" },
    ];
    const merged = mergeEvidenceRefusals(fromStream, fromHistory);
    expect(merged).toEqual([
      { citation_index: 3, error: "malformed_evidence_contract" },
      { citation_index: 5, error: "unsupported_evidence_contract" },
    ]);
  });

  it("treats a missing list as defined-empty", () => {
    expect(mergeEvidenceRefusals(undefined, undefined)).toEqual([]);
  });
});

// ----- presentation copy (visible named-code indicator) -----

describe("refusal / snapshot presentation text", () => {
  it("names the code and the row in the visible indicator", () => {
    const text = evidenceRefusalRowText({
      citation_index: 2,
      error: "unsupported_evidence_contract",
    });
    expect(text).toContain("unsupported_evidence_contract");
    expect(text).toContain("3"); // 1-based row the operator counts
  });

  it("the database identity line shows source and record key as text", () => {
    const label = databaseRowIdentityLabel({
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291", part: 7 },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30, counterparty: "Northwind Logistics" },
      summary: "s",
    });
    expect(label).toContain("ledger_ro.contract_expiry");
    expect(label).toContain("C-2291");
    expect(label).toContain("7");
  });

  it("field lines render scalar values as text", () => {
    const lines = databaseRowFieldLines({
      source: "v",
      record_key: { id: "1" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { notice_days: 30, flag: true, missing: null, name: "N" },
      summary: "s",
    });
    expect(lines).toContain("notice_days: 30");
    expect(lines).toContain("flag: true");
    expect(lines).toContain("missing: ");
    expect(lines).toContain("name: N");
  });
});

// ----- the SHARED schema_version case set -----
//
// This is the point of the whole exercise. Until `schema_version_cases.json`
// was committed, this file and the Python suite each proved only that
// THEY agreed with THEMSELVES: the validator against its own tests, this
// decoder against its own tests, and the comparison export against its
// own. A disagreement between the two languages was invisible to both —
// and the one value it took to disagree is the one that disappears on
// this side of the wire.
//
// The same file is read by `tests/host/test_evidence_contract.py`, which
// drives every case through the validator AND the comparison export. The
// expectations below are that file's DECLARATIONS. Nothing here asks the
// decoder what to expect, and nothing derives an expectation from an
// outcome: if this runner and that one ever disagree about a case, the
// case is a fact and one of the two implementations is wrong.

interface VersionCase {
  case: string;
  expect: "accept" | "malformed_evidence_contract" | "unsupported_evidence_contract";
  why: string;
  schema_version?: unknown;
  schema_version_absent?: boolean;
  schema_version_nonfinite?: "NaN" | "Infinity" | "-Infinity";
}

const VERSION_CASES = (
  fixture<{ cases: VersionCase[] }>("schema_version_cases.json")
).cases;

// The three non-finite spellings have no strict-JSON form, so the case
// file tags them and this runner resolves the tag. The tag is the ONLY
// way such a value enters the set: `JSON.parse` (used by `fixture` above)
// would have thrown on a bare `NaN`/`Infinity` literal, which is the
// reason the file is written this way rather than with literals.
const NONFINITE_TAGS: Record<string, number> = {
  NaN: Number.NaN,
  Infinity: Number.POSITIVE_INFINITY,
  "-Infinity": Number.NEGATIVE_INFINITY,
};

/** The `accept` class maps to the success status (a null refusal /
 * "known" envelope); the two refusal classes map to their §6 code
 * strings. Nothing maps to anything else. */
function observedVersionClass(value: unknown): VersionCase["expect"] {
  const refusal = schemaVersionRefusal(value);
  if (refusal === null) return "accept";
  // ``schemaVersionRefusal`` is declared over the whole §6 code set even
  // though it can only ever return these two, so the narrowing is a
  // RUNTIME check rather than a cast: a third code would fail here with
  // a named error instead of being quietly compared against a declared
  // class it does not belong to.
  if (
    refusal === "malformed_evidence_contract" ||
    refusal === "unsupported_evidence_contract"
  ) {
    return refusal;
  }
  throw new Error(
    `schemaVersionRefusal returned ${refusal}, which is not one of the ` +
      "two classes the version rule names",
  );
}

/** The same three classes, read off the ENVELOPE's short outcome words
 * (``known`` / ``malformed`` / ``unsupported``).
 *
 * WHY a mapping and not a direct comparison: the decoder's envelope API
 * answers with three short words while the predicate answers with §6
 * code strings, and the case file declares the CODES. Translating in
 * one place — with the ``absent`` outcome mapped to nothing, because no
 * case can reach it (every case supplies a recognized token) — keeps
 * the translation honest: an unexpected word fails the lookup rather
 * than falling through to a class it happens to resemble. */
const ENVELOPE_OUTCOME_CLASSES: Record<string, VersionCase["expect"]> = {
  known: "accept",
  malformed: "malformed_evidence_contract",
  unsupported: "unsupported_evidence_contract",
};

function envelopeVersionClass(value: unknown): VersionCase["expect"] {
  const outcome = decodeEvidenceSchemaField(CITATION_CHANNEL_SCHEMA, value);
  const mapped = ENVELOPE_OUTCOME_CLASSES[outcome];
  if (mapped === undefined) {
    throw new Error(
      `envelope outcome ${outcome} maps to no declared class; the three ` +
        "declared classes are accept / malformed_evidence_contract / " +
        "unsupported_evidence_contract",
    );
  }
  return mapped;
}

function versionCaseInput(testCase: VersionCase): {
  form: "present" | "absent" | "nonfinite";
  value: unknown;
} {
  const forms = (["schema_version", "schema_version_absent", "schema_version_nonfinite"] as const)
    .filter((key) => key in testCase);
  if (forms.length !== 1) {
    throw new Error(`${testCase.case} declares ${forms}, expected exactly one`);
  }
  if (forms[0] === "schema_version") return { form: "present", value: testCase.schema_version };
  if (forms[0] === "schema_version_absent") return { form: "absent", value: undefined };
  const tag = testCase.schema_version_nonfinite;
  if (!(tag && tag in NONFINITE_TAGS)) {
    throw new Error(`${testCase.case} names unknown non-finite ${tag}`);
  }
  return { form: "nonfinite", value: NONFINITE_TAGS[tag] };
}

describe("the shared schema_version case set (ADR 0093 decisions 7/8/10/12)", () => {
  it("is well formed: one input form per case, expectations in the closed domain", () => {
    expect(VERSION_CASES.length).toBe(18);
    expect(new Set(VERSION_CASES.map((c) => c.case)).size).toBe(VERSION_CASES.length);
    for (const testCase of VERSION_CASES) {
      expect([
        "accept",
        "malformed_evidence_contract",
        "unsupported_evidence_contract",
      ]).toContain(testCase.expect);
      expect(testCase.why.trim().length).toBeGreaterThan(0);
      versionCaseInput(testCase);
    }
    // All three non-finite spellings are present: a set carrying only one
    // would not be pinning the FINITENESS half of the rule, which a
    // magnitude test would answer differently on the two signs.
    const tagged = new Set(
      VERSION_CASES.map((c) => c.schema_version_nonfinite).filter(Boolean),
    );
    expect([...tagged].sort()).toEqual(["-Infinity", "Infinity", "NaN"]);
  });

  it.each(VERSION_CASES.map((c) => [c.case, c] as const))(
    "%s — the decoder returns the class the case declares",
    (_name, testCase) => {
      const { value } = versionCaseInput(testCase);
      // WHY the decoder is read through the ENVELOPE, not just the
      // predicate: the case set is about the rule as a boundary applies
      // it, and the envelope is the boundary. The predicate is asserted
      // too, so a decoder that reached the right class by accident
      // through a different route is still checked on the rule itself.
      const predicate = observedVersionClass(value);
      const throughEnvelope = envelopeVersionClass(value);
      expect(predicate, `predicate: ${testCase.why}`).toBe(testCase.expect);
      expect(throughEnvelope, `envelope: ${testCase.why}`).toBe(testCase.expect);
    },
  );

  it("the cross-language case: 1.0 is the current version here, not a refusal", () => {
    // `JSON.parse('1.0')` yields the integer `1`, so the spelling is gone
    // before this boundary sees it and NO test may assert a literal 1.0
    // is refused (ADR 0093 decision 8). What this asserts is the accept
    // half, from the same declaration the Python validator reads: the two
    // ends must answer the same for this value, and the Python side is
    // the one that has to work for it.
    const declared = VERSION_CASES.find((c) => c.case === "version-float-integral-current");
    expect(declared?.expect).toBe("accept");
    expect(declared?.schema_version).toBe(1.0);
    expect(schemaVersionRefusal(declared!.schema_version)).toBeNull();
  });

  it("consumed every declared case, so a case cannot be quietly skipped", () => {
    // The counterpart of the Python side's own consumption assertion: both
    // runners iterate the same committed list, and a case dropped from
    // either would be a rule nobody is proving. The count is read off the
    // file rather than a literal above, so adding a case cannot leave a
    // stale number behind.
    const ids = VERSION_CASES.map((c) => c.case);
    expect(new Set(ids).size).toBe(VERSION_CASES.length);
    expect(VERSION_CASES.filter((c) => c.expect === "accept")).toHaveLength(2);
    expect(
      VERSION_CASES.filter((c) => c.expect === "unsupported_evidence_contract"),
    ).toHaveLength(5);
    expect(
      VERSION_CASES.filter((c) => c.expect === "malformed_evidence_contract"),
    ).toHaveLength(11);
  });
});
