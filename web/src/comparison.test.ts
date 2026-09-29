// The comparison table's presentation decisions, pinned without a
// DOM (this project's vitest runs no DOM testing library — see
// ThreadPage.test.ts for the same pattern).
//
// The envelope under test is ``fin.comparison.v1``, produced by
// ``src/fin/host/tools/compare_facts.py`` and carried both on the
// ``comparison`` SSE frame and on ``TurnRow.comparisons``. The
// parser here is the SPA's boundary: the wire is ``unknown``, and a
// malformed payload must render nothing rather than crash a turn.

import { describe, expect, it } from "vitest";

import {
  COMPARISON_SCHEMA,
  MISSING_CELL_DISPLAY,
  cellTitle,
  comparisonCaption,
  deltaForRow,
  directionClass,
  directionMark,
  locatorLabel,
  parseComparison,
  parseComparisons,
  turnComparisons,
} from "./comparison";
import { citationSourceId } from "./sourceLocator";
import type { Citation, Comparison, ComparisonLocator } from "./types";

function makeComparison(overrides: Partial<Comparison> = {}): Comparison {
  return {
    schema: COMPARISON_SCHEMA,
    kind: "table",
    axis: "period",
    concept: "us-gaap:Revenues",
    unit: "USD",
    scale: "millions",
    columns: [
      { key: "2022-Q3-YTD", label: "2022-Q3-YTD" },
      { key: "2023-Q3-YTD", label: "2023-Q3-YTD" },
    ],
    rows: [
      {
        key: "AAPL",
        label: "AAPL",
        kind: "issuer",
        cells: [
          {
            row: "AAPL",
            column: "2022-Q3-YTD",
            status: "hit",
            value: 194140,
            display: "194,140",
            unit: "USD",
            scale: "millions",
            locator: {
              document_id: "AAPL-2022Q3",
              version: "a".repeat(64),
              unit: 4,
              display: "page",
            },
          },
          {
            row: "AAPL",
            column: "2023-Q3-YTD",
            status: "hit",
            value: 293787,
            display: "293,787",
            unit: "USD",
            scale: "millions",
            locator: {
              document_id: "AAPL-2023Q3",
              version: "b".repeat(64),
              unit: 4,
              display: "page",
            },
          },
        ],
      },
    ],
    deltas: [
      {
        from: { row: "AAPL", column: "2022-Q3-YTD" },
        to: { row: "AAPL", column: "2023-Q3-YTD" },
        absolute: 99647,
        percent: 51.3277,
        direction: "up",
        display: "+99,647 (+51.33%)",
      },
    ],
    missing: [],
    hit_count: 2,
    cell_count: 2,
    locators: [
      {
        document_id: "AAPL-2022Q3",
        version: "a".repeat(64),
        unit: 4,
        display: "page",
      },
      {
        document_id: "AAPL-2023Q3",
        version: "b".repeat(64),
        unit: 4,
        display: "page",
      },
    ],
    ...overrides,
  };
}

describe("parseComparison", () => {
  it("accepts the envelope the host tool emits", () => {
    const parsed = parseComparison(makeComparison());
    expect(parsed?.concept).toBe("us-gaap:Revenues");
    expect(parsed?.rows[0].cells[1].value).toBe(293787);
  });

  it("keeps tool_call_id so a live frame can match its tool row", () => {
    const parsed = parseComparison({
      ...makeComparison(),
      tool_call_id: "c-compare",
    });
    expect(parsed?.tool_call_id).toBe("c-compare");
  });

  it("rejects a payload with the wrong schema tag", () => {
    // T4's export binds to this envelope by its schema string; a
    // future v2 must not silently render through the v1 table.
    expect(parseComparison({ ...makeComparison(), schema: "v2" })).toBeNull();
  });

  it("rejects non-objects and missing structure", () => {
    expect(parseComparison(null)).toBeNull();
    expect(parseComparison("fin.comparison.v1")).toBeNull();
    expect(parseComparison({ schema: COMPARISON_SCHEMA })).toBeNull();
  });

  it("rejects an envelope with no columns", () => {
    // The typed EXITs (empty_comparison / comparison_overflow) carry
    // a column-less skeleton. An empty grid on screen reads as "we
    // looked and found zero", which is a different claim.
    expect(parseComparison(makeComparison({ columns: [] }))).toBeNull();
  });

  it("drops malformed entries rather than failing the whole list", () => {
    const list = parseComparisons([makeComparison(), null, { nope: 1 }]);
    expect(list).toHaveLength(1);
  });

  it("treats a missing comparisons field as no tables", () => {
    expect(parseComparisons(undefined)).toEqual([]);
    expect(parseComparisons("not a list")).toEqual([]);
  });
});

describe("turnComparisons", () => {
  it("returns the renderable tables on a turn", () => {
    expect(
      turnComparisons({ comparisons: [makeComparison()] }),
    ).toHaveLength(1);
  });

  it("is empty for a turn that never compared", () => {
    expect(turnComparisons({})).toEqual([]);
    expect(turnComparisons({ comparisons: [] })).toEqual([]);
  });
});

describe("comparisonCaption", () => {
  it("names the concept, the unit and the coverage", () => {
    const caption = comparisonCaption(makeComparison());
    expect(caption).toContain("us-gaap:Revenues");
    expect(caption).toContain("USD");
    expect(caption).toContain("millions");
    // Coverage is what tells the operator the table is complete;
    // a partial grid must not read like a full one.
    expect(caption).toContain("2/2");
  });

  it("still reads when the tool reported no unit", () => {
    const caption = comparisonCaption(
      makeComparison({ unit: "", scale: "", hit_count: 0, cell_count: 2 }),
    );
    expect(caption).toContain("us-gaap:Revenues");
    expect(caption).toContain("0/2");
    expect(caption).not.toContain("·  ·");
  });
});

describe("deltaForRow", () => {
  it("finds the redline whose target is that row", () => {
    expect(deltaForRow(makeComparison(), "AAPL")?.direction).toBe("up");
  });

  it("is undefined for the baseline row of a peer comparison", () => {
    // Peer deltas are measured AGAINST the first row, so the
    // baseline itself has no delta and its cell must stay blank
    // rather than render a fabricated 0.
    const peer = makeComparison({
      axis: "peer",
      deltas: [
        {
          from: { row: "AAPL", column: "2023-Q3-YTD" },
          to: { row: "MSFT", column: "2023-Q3-YTD" },
          absolute: -81872,
          percent: -27.87,
          direction: "down",
          display: "-81,872 (-27.87%)",
        },
      ],
    });
    expect(deltaForRow(peer, "AAPL")).toBeUndefined();
    expect(deltaForRow(peer, "MSFT")?.display).toBe("-81,872 (-27.87%)");
  });

  it("is undefined when a missing cell suppressed the arithmetic", () => {
    expect(deltaForRow(makeComparison({ deltas: [] }), "AAPL")).toBeUndefined();
  });
});

describe("direction rendering", () => {
  it("gives each direction a distinct mark", () => {
    const marks = [
      directionMark("up"),
      directionMark("down"),
      directionMark("flat"),
    ];
    expect(new Set(marks).size).toBe(3);
  });

  it("colours up and down differently so a drop is not read as a rise", () => {
    expect(directionClass("up")).not.toBe(directionClass("down"));
  });
});

describe("locatorLabel", () => {
  it("keeps the page wording for a physical PDF unit", () => {
    expect(
      locatorLabel({
        document_id: "AAPL-2022Q3",
        version: "a".repeat(64),
        unit: 4,
        display: "page",
      }),
    ).toBe("AAPL-2022Q3 p.4");
  });

  it("labels a non-PDF unit as a unit, never as a page", () => {
    expect(
      locatorLabel({
        document_id: "AAPL-2022Q3",
        version: "a".repeat(64),
        unit: 7,
        display: "unit",
      }),
    ).toBe("AAPL-2022Q3 u.7");
  });

  it("defaults to the unit wording when a peer sends no display", () => {
    expect(
      locatorLabel({
        document_id: "AAPL-2022Q3",
        version: "a".repeat(64),
        unit: 7,
      }),
    ).toBe("AAPL-2022Q3 u.7");
  });

  it("resolves an attachment row by its attachment_id", () => {
    // ``citationSourceId`` in sourceLocator.ts is the single identity
    // authority (spec thread-attachment-source-read). The table used to
    // carry its own ``document_id || attachment_id`` chain, so a table
    // row and a citation chip over the same source could be named
    // differently — which is what the shared rule exists to prevent.
    expect(
      locatorLabel({
        kind: "attachment",
        document_id: "",
        attachment_id: "ATT-1",
        version: "a".repeat(64),
        unit: 7,
        display: "unit",
      }),
    ).toBe("ATT-1 u.7");
  });

  it("never lets a company row borrow a sibling attachment_id", () => {
    // An ``||`` over the ids cannot tell these two rows apart: an empty
    // document_id on a company row is a missing identity, not an
    // attachment. Guessing one would trace a table number to a source
    // the fact row never came from.
    const label = locatorLabel({
      kind: "company",
      document_id: "",
      attachment_id: "ATT-1",
      version: "a".repeat(64),
      unit: 7,
      display: "unit",
    });
    expect(label).not.toContain("ATT-1");
    expect(label).toBe(" u.7");
  });

  it("names the same source identity the citation chips group by", () => {
    // The invariant behind the two rules: one identity, two renderings.
    // Asserted against the shared authority rather than a literal so a
    // future re-fork inside the table shows up as a mismatch instead of
    // passing a hard-coded string.
    const locator: ComparisonLocator = {
      kind: "attachment",
      document_id: "",
      attachment_id: "ATT-1",
      version: "a".repeat(64),
      unit: 7,
      display: "unit",
    };
    const citation: Citation = {
      kind: "attachment",
      document_id: "",
      attachment_id: "ATT-1",
      version: "a".repeat(64),
      unit: 7,
      display: "unit",
      filename: "q3-notes.pdf",
      quote: "Revenue grew",
    };
    const [identity] = locatorLabel(locator).split(" ");
    expect(identity).toBe(citationSourceId(citation));
  });
});

describe("cellTitle", () => {
  it("names the filing page a hit came from", () => {
    const cell = makeComparison().rows[0].cells[0];
    expect(cellTitle(cell)).toBe("AAPL-2022Q3 p.4");
  });

  it("names the source unit when the hit is not a physical page", () => {
    const cell = {
      row: "AAPL",
      column: "2022-Q3-YTD",
      status: "hit" as const,
      value: 194140,
      display: "194,140",
      unit: "USD",
      scale: "millions",
      // The attachment row states its discriminator: identity comes
      // from the shared rule, which keys on ``kind`` and never guesses
      // it from which id is filled.
      locator: {
        kind: "attachment" as const,
        document_id: "",
        attachment_id: "att-1",
        version: "a".repeat(64),
        unit: 7,
        display: "unit" as const,
      },
    };
    expect(cellTitle(cell)).toBe("att-1 u.7");
  });

  it("says not-reported for a missing cell", () => {
    const cell = {
      row: "AAPL",
      column: "1999-Q1-YTD",
      status: "missing" as const,
      value: null,
      display: MISSING_CELL_DISPLAY,
      unit: "",
      scale: "",
      locator: null,
    };
    // A blank cell reads as zero; the operator must be able to tell
    // "not reported" from "reported as nothing".
    expect(cellTitle(cell)).toBe("未报告");
  });
});
