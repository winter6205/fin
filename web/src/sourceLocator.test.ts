// The unit-vs-page display rule, pinned without a DOM: the PDF
// presentation may label a physical unit as a page; every other unit
// is displayed as a unit (spec thread-attachment-source-read).

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  attachmentDisplayName,
  attachmentSourceRef,
  citationJumpUrl,
  citationSourceId,
  citationSourceLabel,
  demoSourceForCitation,
  formatCitationLocator,
  formatUnitLocator,
  threadAttachmentUrl,
} from "./sourceLocator";
import type { DemoSourceSummary } from "./demoMode";
import type { Citation, VisualClaim } from "./types";

function companyCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "company",
    document_id: "AAPL-10Q-2024Q3",
    attachment_id: "",
    version: "v1",
    unit: 31,
    display: "page",
    filename: "",
    quote: "Total net sales were $391.0 billion.",
    ...overrides,
  };
}

function attachmentCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "attachment",
    document_id: "",
    attachment_id: "att-1",
    version: "v1",
    unit: 7,
    display: "unit",
    filename: "quarterly-notes.pdf",
    quote: "渠道库存较上季度下降。",
    ...overrides,
  };
}

// A ledger row citation: a record, not a document passage. Its own
// identity is the permitted view plus the record key the query returned,
// and the retained snapshot it opens carries that round's queried_at
// (ADR 0091, spec 8.1).
function databaseRowCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "database-row",
    document_id: "",
    attachment_id: "",
    version: "2026-03-01T09:14:22Z",
    unit: 1,
    display: "record",
    filename: "",
    quote:
      "C-2291 Northwind Logistics, expires 2026-03-14, notice by 2026-02-12",
    database_row: {
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields: {
        counterparty: "Northwind Logistics",
        expiry_date: "2026-03-14",
        notice_days: "30",
        notice_by: "2026-02-12",
      },
      summary:
        "C-2291 Northwind Logistics, expires 2026-03-14, notice by 2026-02-12",
    },
    ...overrides,
  };
}

describe("formatUnitLocator", () => {
  it("labels a physical PDF unit as a page", () => {
    expect(formatUnitLocator("page", 4)).toBe("p.4");
  });

  it("labels a non-PDF unit as a unit, never as a page", () => {
    expect(formatUnitLocator("unit", 7)).toBe("u.7");
  });

  it("falls back to the unit wording when a peer sends no display", () => {
    expect(formatUnitLocator(undefined, 7)).toBe("u.7");
  });

  it("labels a ledger row as a record, never as a page or a unit", () => {
    // A record has no page and no ordinal, so "p.1" / "u.1" would name a
    // position in a document that does not exist (ADR 0091).
    expect(formatUnitLocator("record", 1)).toBe("record");
    expect(formatUnitLocator("record", 42)).toBe("record");
  });
});

describe("formatCitationLocator", () => {
  it("shows trusted section and page together", () => {
    expect(
      formatCitationLocator({ section: "Section 2", display: "page", unit: 4 }),
    ).toBe("Section 2 · p.4");
  });

  it("labels older history without a section explicitly", () => {
    expect(formatCitationLocator({ display: "page", unit: 4 })).toBe(
      "章节未注明 · p.4",
    );
  });

  it("does not apply document sections to ledger records", () => {
    expect(
      formatCitationLocator({ section: "Section 2", display: "record", unit: 1 }),
    ).toBe("record");
  });
});

describe("citationSourceId", () => {
  it("keeps document_id as the company identity", () => {
    expect(citationSourceId(companyCitation())).toBe("AAPL-10Q-2024Q3");
  });

  it("uses attachment_id — not the filename — as the attachment identity", () => {
    expect(citationSourceId(attachmentCitation({ filename: "a.pdf" }))).toBe(
      "att-1",
    );
    expect(citationSourceId(attachmentCitation({ attachment_id: "att-2" }))).toBe(
      "att-2",
    );
  });

  it("uses the view plus record key for a ledger row", () => {
    // A row names no filing, so its identity must come from the record
    // the query returned — not from the empty document_id slot.
    expect(citationSourceId(databaseRowCitation())).toBe(
      "ledger_ro.contract_expiry#contract_id=C-2291",
    );
  });

  it("gives two rows of the same view two identities", () => {
    const other = databaseRowCitation({
      database_row: {
        ...databaseRowCitation().database_row!,
        record_key: { contract_id: "C-2292" },
      },
    });
    expect(citationSourceId(other)).not.toBe(citationSourceId(databaseRowCitation()));
  });

  it("groups by key order, not by JSON key order", () => {
    // The host renders the same way; a peer that serialises the key in
    // another order must not split one record into two sources.
    const a = databaseRowCitation({
      database_row: {
        ...databaseRowCitation().database_row!,
        record_key: { contract_id: "C-2291", region: "APAC" },
      },
    });
    const b = databaseRowCitation({
      database_row: {
        ...databaseRowCitation().database_row!,
        record_key: { region: "APAC", contract_id: "C-2291" },
      },
    });
    expect(citationSourceId(a)).toBe(citationSourceId(b));
  });

  it("returns a stable empty id for a row with no snapshot", () => {
    // A row the host could not project carries no identity. The old
    // binary switch defaulted every unrecognised kind into
    // document_id; a stable "" keeps an unreadable row in one bucket
    // instead of inventing an id per unknown value (ADR 0091).
    const headless = databaseRowCitation({ database_row: undefined });
    expect(citationSourceId(headless)).toBe("");
    expect(citationSourceId(headless)).toBe(citationSourceId(headless));
  });
});

describe("citationSourceLabel", () => {
  it("shows the safe filename for an attachment", () => {
    expect(citationSourceLabel(attachmentCitation())).toBe(
      "quarterly-notes.pdf",
    );
  });

  it("names a company citation readably, never by its document_id", () => {
    // PREMISE CHANGED. This assertion read the raw ``document_id`` as
    // the label, which is the leak T10 removes: a filing id is an
    // internal handle a reader cannot use (specs/qa-chain-grounded-
    // experience.md "Internal names, engine/tool names, IDs, and slot
    // labels must not leak through default source labels"). The default
    // label is now the readable noun plus the locator the row carries,
    // and the raw id is asserted on ``citationSourceId`` — the identity
    // half, which legitimately keeps it.
    const label = citationSourceLabel(companyCitation());
    expect(label).toBe("公司资料 · 第 31 页");
    expect(label).not.toContain("AAPL-10Q-2024Q3");
    expect(citationSourceId(companyCitation())).toBe("AAPL-10Q-2024Q3");
  });

  it("tells two rows of one untitled source apart by the marker index", () => {
    // The id is gone from the label, so the ``[n]`` the sources list and
    // the inline markers already share is what keeps two rows of the same
    // filing distinguishable — and it is the SAME number, not a second
    // counter.
    const first = citationSourceLabel(companyCitation(), 1);
    const second = citationSourceLabel(
      companyCitation({ unit: 7 }),
      2,
    );
    expect(first).toBe("公司资料 · 第 31 页 · 出处 1");
    expect(second).toBe("公司资料 · 第 7 页 · 出处 2");
    expect(first).not.toBe(second);
  });

  it("falls back to a readable attachment name, never the id alone", () => {
    // PREMISE CHANGED: this read ``att-1`` as the whole label.
    expect(citationSourceLabel(attachmentCitation({ filename: "" }))).toBe(
      "对话附件 att-1",
    );
  });

  it("names a ledger row readably, never its view plus record key", () => {
    // PREMISE CHANGED: this read ``ledger_ro.contract_expiry#
    // contract_id=C-2291``, which is a SQL view name and a snake_case
    // record key in the default view. The identity half keeps it.
    expect(citationSourceLabel(databaseRowCitation())).toBe("记录");
    expect(citationSourceId(databaseRowCitation())).toBe(
      "ledger_ro.contract_expiry#contract_id=C-2291",
    );
  });

  it("names the kind when a row arrived with no snapshot to label", () => {
    const headless = databaseRowCitation({ database_row: undefined });
    expect(citationSourceLabel(headless)).toBe("记录");
  });

  it("names a company citation readably, never by its document_id", () => {
    // A readable document id is not the label either: the default view
    // may not carry an internal id (spec qa-chain-grounded-experience
    // §"the default view"), readable or not.
    expect(citationSourceLabel(companyCitation())).toBe("公司资料 · 第 31 页");
    expect(
      citationSourceLabel(companyCitation({ document_id: "MSFT-2024Q1" })),
    ).toBe("公司资料 · 第 31 页");
  });

  it("never renders a 64-character hashed id as a bare label", () => {
    // A generic document's identity is a scope-bound digest —
    // ``doc_<64 hex>``. The wire carries no title for it, so the label
    // is the readable noun plus the locator: bounded, and never the
    // digest itself.
    const generic = companyCitation({
      document_id: `doc_${"b".repeat(64)}`,
    });
    const label = citationSourceLabel(generic);
    expect(label).toBe("公司资料 · 第 31 页");
    expect(label).not.toContain("doc_");
    expect(label.length).toBeLessThanOrEqual(20);
  });
});

describe("attachmentDisplayName", () => {
  it("shows the safe filename when the host sent one", () => {
    expect(attachmentDisplayName("att-1", "quarterly-notes.pdf")).toBe(
      "quarterly-notes.pdf",
    );
  });

  it("falls back to a readable name, never to blank", () => {
    // PREMISE CHANGED: this asserted the bare ``att-1``.
    expect(attachmentDisplayName("att-1", "")).toBe("对话附件 att-1");
  });
});

describe("attachmentSourceRef", () => {
  function claim(overrides: Partial<VisualClaim> = {}): VisualClaim {
    return {
      kind: "visual_observation",
      attachment_id: "att-1",
      version: "3",
      unit: 4,
      page: 4,
      observation_version: 1,
      scope: null,
      provider: "configured-vision",
      model_version: "vision-model-1",
      page_image_sha256: "a".repeat(64),
      detail: "Revenue by segment chart / Services series / FY2024",
      filename: "quarterly-notes.pdf",
      reused: false,
      outbound_calls: 1,
      ...overrides,
    };
  }

  it("builds the attachment locator the read resolves", () => {
    expect(attachmentSourceRef(claim())).toEqual({
      kind: "attachment",
      attachment_id: "att-1",
      version: "3",
      unit: 4,
    });
  });

  it("gives a citation and a visual claim naming one unit one locator", () => {
    // Same identity fields on both rows must produce the same locator, so
    // the two surfaces cannot name the same source differently — nor
    // collapse into one entry in the sources list.
    const citation = attachmentCitation({ version: "3", unit: 4 });
    expect(attachmentSourceRef(citation)).toEqual(
      attachmentSourceRef({ ...claim(), version: "3", unit: 4 }),
    );
  });

  it("keeps the physical page out of the locator — unit is the ordinal", () => {
    expect(Object.keys(attachmentSourceRef(claim({ page: 9, unit: 4 })))).not.toContain(
      "page",
    );
  });
});

describe("threadAttachmentUrl", () => {
  it("names the host's one attachment door for this thread and id", () => {
    expect(threadAttachmentUrl("t1", "att-1")).toBe(
      "/api/threads/t1/attachments/att-1",
    );
  });

  it("encodes both ids so neither escapes its path segment", () => {
    expect(threadAttachmentUrl("t h/1", "a/b")).toBe(
      "/api/threads/t%20h%2F1/attachments/a%2Fb",
    );
  });
});

// specs/demo-seed.md item 6: "is clickable to jump to that location".
// The host has exactly ONE door that names where an attachment lives —
// ``threadAttachmentUrl`` — so that is the only destination a jump may
// spell, and a citation with nothing behind it has no jump to make. A
// null is the honest answer for those rows, never a placeholder href.
describe("citationJumpUrl", () => {
  it("names the host's one attachment door, the one the fetch itself uses", () => {
    const citation = attachmentCitation();
    expect(citationJumpUrl("t1", citation)).toBe(
      threadAttachmentUrl("t1", citation.attachment_id),
    );
    expect(citationJumpUrl("t1", citation)).toBe(
      "/api/threads/t1/attachments/att-1",
    );
  });

  it("has no target for a ledger row — a record is not a place in a document", () => {
    // Not merely an empty attachment_id (the explicit rule below would
    // catch that too): a ledger row has no filing behind it whatever it
    // is handed, so it never jumps.
    expect(citationJumpUrl("t1", databaseRowCitation())).toBeNull();
    expect(
      citationJumpUrl("t1", databaseRowCitation({ attachment_id: "att-1" })),
    ).toBeNull();
  });

  it("has no target for a filing citation with no attachment behind it", () => {
    // A company citation rides its document_id, and the host exposes no
    // document door — inventing a second URL scheme here would be a link
    // the client's own fetch would never recognize.
    expect(citationJumpUrl("t1", companyCitation())).toBeNull();
  });

  it("has no target without a thread — an attachment id alone names no address", () => {
    expect(citationJumpUrl("", attachmentCitation())).toBeNull();
    expect(citationJumpUrl(undefined, attachmentCitation())).toBeNull();
  });

  it("encodes both ids so neither escapes its path segment", () => {
    const citation = attachmentCitation({ attachment_id: "a/b" });
    expect(citationJumpUrl("t h/1", citation)).toBe(
      threadAttachmentUrl("t h/1", "a/b"),
    );
  });
});

// The demo citation resolution rule, pinned against the shipped fixture's
// own locator. The host's allowlist keys a row by the document id, the
// fixture file's sha256 as the version, the unit (the page) and the page --
// the triple the seed pushed. A citation the seed produced carries that same
// triple, so it resolves to its own source; a citation whose unit is not the
// row's page resolves to nothing, because the demo's unit IS the page.
//
// The version is read from the fixture file rather than frozen as a literal,
// so the case keeps naming the locator the seed actually pushes.
const DEMO_FIXTURE = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "fixtures",
  "demo-corpus",
  "zeus-handbook.json",
);

const HANDBOOK_VERSION = createHash("sha256")
  .update(readFileSync(DEMO_FIXTURE, "utf8"))
  .digest("hex");

const HANDBOOK_SOURCE: DemoSourceSummary = {
  document_id: "zeus-handbook",
  version: HANDBOOK_VERSION,
  unit: 2,
  page: 2,
  title: "ZEUS Employee Handbook",
  section: "Section 2",
};

function demoCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "company",
    document_id: "zeus-handbook",
    attachment_id: "",
    version: HANDBOOK_VERSION,
    unit: 2,
    display: "page",
    filename: "",
    quote: "The 2025 annual compensation review cycle runs from 2025-02-03.",
    ...overrides,
  };
}

describe("demoSourceForCitation", () => {
  it("resolves a citation the seed pushed to its own page", () => {
    expect(demoSourceForCitation(demoCitation(), [HANDBOOK_SOURCE])).toEqual(
      HANDBOOK_SOURCE,
    );
  });

  it("refuses a citation whose unit is not the source's page", () => {
    expect(
      demoSourceForCitation(demoCitation(), [{ ...HANDBOOK_SOURCE, page: 5 }]),
    ).toBeNull();
  });

  it("refuses a citation that names a version the seed never pushed", () => {
    expect(
      demoSourceForCitation(demoCitation({ version: "0".repeat(64) }), [
        HANDBOOK_SOURCE,
      ]),
    ).toBeNull();
  });
});
