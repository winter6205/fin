// The unit-vs-page display rule, pinned without a DOM: the PDF
// presentation may label a physical unit as a page; every other unit
// is displayed as a unit (spec thread-attachment-source-read).

import { describe, expect, it } from "vitest";
import {
  attachmentDisplayName,
  attachmentSourceRef,
  citationSourceId,
  citationSourceLabel,
  formatUnitLocator,
  threadAttachmentUrl,
} from "./sourceLocator";
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
});

describe("citationSourceLabel", () => {
  it("shows the safe filename for an attachment", () => {
    expect(citationSourceLabel(attachmentCitation())).toBe(
      "quarterly-notes.pdf",
    );
  });

  it("shows the document_id for a company citation", () => {
    expect(citationSourceLabel(companyCitation())).toBe("AAPL-10Q-2024Q3");
  });

  it("falls back to the attachment_id when no filename arrived", () => {
    expect(citationSourceLabel(attachmentCitation({ filename: "" }))).toBe(
      "att-1",
    );
  });
});

describe("attachmentDisplayName", () => {
  it("shows the safe filename when the host sent one", () => {
    expect(attachmentDisplayName("att-1", "quarterly-notes.pdf")).toBe(
      "quarterly-notes.pdf",
    );
  });

  it("falls back to the id, never to blank", () => {
    expect(attachmentDisplayName("att-1", "")).toBe("att-1");
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
