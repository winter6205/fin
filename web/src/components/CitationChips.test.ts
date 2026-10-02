// What the citation surface actually renders for the evidence-refusal
// lane and the database-row snapshot identity. The decoder guarantees a
// refusal is a TYPED visible outcome; this file pins the other half of
// that promise — that the visible named-code indicator and the stored
// snapshot identity really reach the rendered HTML, and that a refused
// row never degrades into an empty success.
//
// Rendered with ``renderToString`` (same precedent as the visual-claims
// surface tests): this project's vitest setup has no DOM testing
// library, and string output is enough to pin copy and attributes.

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

import AnswerWithCitations from "./CitationChips";
import type { Citation, DatabaseScalar, EvidenceRefusal } from "../types";

const QUOTE = "Our quarterly revenue rose 8% year over year.";

function companyCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "company",
    document_id: "AAPL-10Q-2024Q3",
    attachment_id: "",
    version: "v1",
    unit: 3,
    display: "page",
    filename: "",
    quote: QUOTE,
    ...overrides,
  };
}

function databaseRowCitation(
  fields: Record<string, DatabaseScalar> = {
    counterparty: "Northwind Logistics",
    notice_days: "30",
  },
): Citation {
  return {
    kind: "database-row",
    document_id: "",
    attachment_id: "",
    version: "2026-03-01T09:14:22Z",
    unit: 1,
    display: "record",
    filename: "",
    quote: "C-2291 Northwind Logistics, notice period 30 days",
    database_row: {
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields,
      summary: "C-2291 Northwind Logistics, notice period 30 days",
    },
  };
}

function render(answer: string, citations: Citation[], refusals?: EvidenceRefusal[]): string {
  return renderToString(
    createElement(AnswerWithCitations, { answer, citations, refusals }),
  );
}

/** Visible text only — tags and React's ``<!-- -->`` separators gone. */
function text(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ");
}

function refusalNodes(html: string): string[] {
  return html.match(/<li[^>]*data-testid="evidence-refusal"[^>]*>[\s\S]*?<\/li>/g) ?? [];
}

describe("the named-code refusal indicator", () => {
  it("renders one visible row per refusal, naming the §6 code verbatim", () => {
    const html = render(QUOTE, [], [
      { citation_index: 1, error: "malformed_evidence_contract" },
      { citation_index: 4, error: "unsupported_evidence_contract" },
    ]);
    const nodes = refusalNodes(html);
    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toContain('data-code="malformed_evidence_contract"');
    expect(text(nodes[0])).toContain("证据被拒绝：malformed_evidence_contract（第 2 条出处）");
    expect(nodes[1]).toContain('data-code="unsupported_evidence_contract"');
    expect(text(nodes[1])).toContain("第 5 条出处");
  });

  it("stays visible when every row was refused — never an empty success", () => {
    // The display policy's core case: an empty citation list alongside a
    // refusal must not read as "no sources needed". The refusal line is
    // the only evidence surface left, so it has to be present.
    const html = render("营收同比增长。", [], [
      { citation_index: 0, error: "citation_overflow" },
    ]);
    expect(html).not.toContain('aria-label="Sources"');
    expect(refusalNodes(html)).toHaveLength(1);
    expect(refusalNodes(html)[0]).toContain('data-code="citation_overflow"');
  });

  it("keeps surviving citations rendering normally next to a refusal", () => {
    const answer = `${QUOTE} 其它内容。`;
    const html = render(answer, [companyCitation()], [
      { citation_index: 1, error: "unknown_locator" },
    ]);
    // The valid row keeps its chip and its sources entry…
    expect(html).toContain("data-citation-index");
    expect(html).toContain('aria-label="Sources"');
    // …and the refused row is named beside them, not merged into the list.
    expect(refusalNodes(html)).toHaveLength(1);
    expect(refusalNodes(html)[0]).toContain('data-code="unknown_locator"');
  });

  it("emits no indicator markup for an empty or absent refusal list", () => {
    expect(render(QUOTE, [companyCitation()], [])).not.toContain("evidence-refusal");
    expect(render(QUOTE, [companyCitation()])).not.toContain("evidence-refusal");
  });
});

describe("the database-row snapshot identity", () => {
  const answer = "合同 C-2291 到期于 2026-03-14。";

  it("shows source and record key without any interaction", () => {
    const html = render(answer, [databaseRowCitation()]);
    const identity = /<p[^>]*data-testid="database-row-identity"[^>]*>[\s\S]*?<\/p>/.exec(html);
    expect(identity).not.toBeNull();
    expect(text(identity?.[0] ?? "")).toContain(
      "来源 ledger_ro.contract_expiry · 记录 contract_id=C-2291",
    );
  });

  it("shows the stored queried_at and claims no currency", () => {
    const visible = text(render(answer, [databaseRowCitation()]));
    expect(visible).toContain("as of 2026-03-01T09:14:22Z");
    expect(visible).toContain("not a live read");
    expect(visible).not.toContain("still valid");
  });

  it("renders scalar record_key values to text in the identity line", () => {
    // The snapshot's scalar widening: numbers/booleans/null reach the
    // identity line as their canonical text form, keys sorted by name —
    // the same rule the digest uses, kept in one place.
    const citation = databaseRowCitation();
    citation.database_row = {
      ...citation.database_row!,
      record_key: { notice_days: 30, frozen: true, memo: null },
    };
    const identity = /<p[^>]*data-testid="database-row-identity"[^>]*>[\s\S]*?<\/p>/.exec(
      render(answer, [citation]),
    );
    expect(text(identity?.[0] ?? "")).toContain(
      "记录 frozen=true memo= notice_days=30",
    );
  });

  it("keeps document citations free of the database identity line", () => {
    expect(render(QUOTE, [companyCitation()])).not.toContain("database-row-identity");
  });
});
