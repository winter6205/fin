// SC16 (i): a turn whose verified spans exceed the threshold shows
// its sources list collapsed; at or below it the list stays open.
// The unit grouping lives here too — it keys on the citation's source
// unit, not any generic page field.

import { describe, expect, it } from "vitest";
import {
  SOURCES_COLLAPSE_THRESHOLD,
  countUniqueSourceUnits,
  groupCitationsBySourceAndUnit,
  shouldCollapseSourcesList,
  sourceCountWord,
} from "./sourcesList";
import type { Citation, VisualClaim } from "./types";

function citation(unit: number, documentId = "AAPL-10Q-2024Q3"): Citation {
  return {
    kind: "company",
    document_id: documentId,
    attachment_id: "",
    version: "v1",
    unit,
    display: "page",
    filename: "",
    quote: `span on unit ${unit} long enough to be a verified quote`,
  };
}

function attachmentCitation(
  unit: number,
  attachmentId = "att-1",
  filename = "quarterly-notes.pdf",
  display: Citation["display"] = "unit",
): Citation {
  return {
    kind: "attachment",
    document_id: "",
    attachment_id: attachmentId,
    version: "v1",
    unit,
    display,
    filename,
    quote: `span on unit ${unit} long enough to be a verified quote`,
  };
}

// A ledger row citation: a record from the ``sql`` 档, verified by its
// record key and this round's returned fields rather than by a text span
// (ADR 0091). It names no filing and has no unit ordinal.
function databaseRowCitation(contractId: string): Citation {
  return {
    kind: "database-row",
    document_id: "",
    attachment_id: "",
    version: "2026-03-01T09:14:22Z",
    unit: 1,
    display: "record",
    filename: "",
    quote: `${contractId} Northwind Logistics, notice period 30 days`,
    database_row: {
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: contractId },
      queried_at: "2026-03-01T09:14:22Z",
      fields: { counterparty: "Northwind Logistics", notice_days: "30" },
      summary: `${contractId} Northwind Logistics, notice period 30 days`,
    },
  };
}

describe("shouldCollapseSourcesList", () => {  it("keeps an empty sources list open", () => {
    expect(shouldCollapseSourcesList(0)).toBe(false);
  });

  it("keeps a single span open", () => {
    expect(shouldCollapseSourcesList(1)).toBe(false);
  });

  it("keeps exactly the threshold open", () => {
    expect(shouldCollapseSourcesList(SOURCES_COLLAPSE_THRESHOLD)).toBe(false);
    expect(SOURCES_COLLAPSE_THRESHOLD).toBe(10);
  });

  it("collapses one span past the threshold", () => {
    expect(shouldCollapseSourcesList(SOURCES_COLLAPSE_THRESHOLD + 1)).toBe(
      true,
    );
  });

  it("collapses a turn with many spans", () => {
    const citations = Array.from({ length: 24 }, (_, i) => citation(i + 1));
    expect(shouldCollapseSourcesList(citations.length)).toBe(true);
  });
});

describe("countUniqueSourceUnits", () => {
  it("counts units, not spans", () => {
    const citations = [citation(3), citation(3), citation(4)];
    expect(citations).toHaveLength(3);
    expect(countUniqueSourceUnits(citations)).toBe(2);
  });

  it("keeps same-numbered units of different documents apart", () => {
    const citations = [citation(3, "AAPL-10Q"), citation(3, "MSFT-10Q")];
    expect(countUniqueSourceUnits(citations)).toBe(2);
  });

  it("keeps same-numbered units of different attachments apart", () => {
    const citations = [
      attachmentCitation(2, "att-1"),
      attachmentCitation(2, "att-2"),
    ];
    expect(countUniqueSourceUnits(citations)).toBe(2);
  });

  it("does not merge an attachment and a filing by unit number", () => {
    const citations = [citation(2), attachmentCitation(2)];
    expect(countUniqueSourceUnits(citations)).toBe(2);
  });

  it("counts nothing for a turn with no citations", () => {
    expect(countUniqueSourceUnits([])).toBe(0);
  });
});

describe("sourceCountWord", () => {
  it("counts pages only when every unit is a physical page", () => {
    expect(sourceCountWord(["page", "page"])).toBe("页");
  });

  it("counts units when any unit is not a page", () => {
    expect(sourceCountWord(["page", "unit"])).toBe("单元");
    expect(sourceCountWord(["unit"])).toBe("单元");
  });
});

describe("groupCitationsBySourceAndUnit", () => {
  it("groups a document's citations by unit, sorted, keeping input order in globalIndex", () => {
    const groups = groupCitationsBySourceAndUnit([
      citation(4),
      citation(2),
      citation(4),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].identity).toBe("AAPL-10Q-2024Q3");
    expect(groups[0].units.map((u) => u.unit)).toEqual([2, 4]);
    expect(groups[0].units[1].citations.map((c) => c.globalIndex)).toEqual([
      1, 3,
    ]);
  });

  it("keys an attachment group by attachment_id and labels it with the filename", () => {
    const groups = groupCitationsBySourceAndUnit([
      attachmentCitation(3, "att-9", "渠道复盘.docx"),
    ]);
    expect(groups[0].identity).toBe("att-9");
    expect(groups[0].label).toBe("渠道复盘.docx");
  });

  it("keeps two attachments with the same filename separate", () => {
    const groups = groupCitationsBySourceAndUnit([
      attachmentCitation(1, "att-1", "notes.pdf"),
      attachmentCitation(1, "att-2", "notes.pdf"),
    ]);
    expect(groups.map((g) => g.identity)).toEqual(["att-1", "att-2"]);
  });

  it("carries the unit's display so only PDF units read as pages", () => {
    const groups = groupCitationsBySourceAndUnit([
      citation(4),
      attachmentCitation(4, "att-1", "notes.txt", "unit"),
    ]);
    expect(groups[0].units[0].display).toBe("page");
    expect(groups[1].units[0].display).toBe("unit");
  });

  it("keys a ledger row by its view and record key, not by unit", () => {
    // A row has no ordinal: two rows of the same view are two records,
    // and grouping them under a shared "u.1" would merge two answers'
    // evidence into one (ADR 0091).
    const groups = groupCitationsBySourceAndUnit([
      databaseRowCitation("C-2291"),
      databaseRowCitation("C-2292"),
    ]);
    expect(groups.map((g) => g.identity)).toEqual([
      "ledger_ro.contract_expiry#contract_id=C-2291",
      "ledger_ro.contract_expiry#contract_id=C-2292",
    ]);
    expect(groups[0].units[0].display).toBe("record");
  });

  it("never files a ledger row under a filing's units", () => {
    // The old binary identity switch defaulted every unrecognised kind
    // to document_id, which is "" for a row — so rows would have merged
    // into one group, and that one with any filing that also had an
    // empty id.
    const groups = groupCitationsBySourceAndUnit([
      citation(1),
      databaseRowCitation("C-2291"),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0].identity).toBe("AAPL-10Q-2024Q3");
    expect(groups[1].identity).toBe("ledger_ro.contract_expiry#contract_id=C-2291");
  });
});

describe("a visual claim never enters the quoted-source list", () => {
  const claim: VisualClaim = {
    kind: "visual_observation",
    attachment_id: "att-1",
    version: "v1",
    unit: 4,
    page: 4,
    observation_version: 1,
    scope: null,
    provider: "configured-vision",
    model_version: "vision-model-1",
    page_image_sha256: "a".repeat(64),
    detail: "Revenue by segment chart / Services series / FY2024",
    filename: "quarterly-notes.pdf",
    reused: true,
    outbound_calls: 0,
  };

  it("groups by citations alone when the same unit also carries a claim", () => {
    // The claim names the SAME attachment and unit as the citation. The
    // list is built from citations only, so it gains neither a group nor a
    // span, and the two surfaces stay separate (spec line 65).
    const citations = [
      attachmentCitation(4, "att-1", "quarterly-notes.pdf", "page"),
    ];
    const groups = groupCitationsBySourceAndUnit(citations);
    expect(groups).toHaveLength(1);
    expect(groups[0].units).toHaveLength(1);
    expect(groups[0].units[0].citations).toHaveLength(1);
    expect(countUniqueSourceUnits(citations)).toBe(1);
  });

  it("has no quote on a claim row to group by", () => {
    // The list's entries are verified spans. A claim carries provenance
    // and a target, never a quote, so it cannot become a source entry.
    expect((claim as unknown as Partial<Citation>).quote).toBeUndefined();
    expect(claim.kind).not.toBe("attachment");
  });
});
