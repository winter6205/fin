import { describe, expect, it } from "vitest";
import { exportAvailability, exportUrl } from "./exportBundle";
import type { Comparison, Turn } from "./types";

const COMPARISON: Comparison = {
  schema: "fin.comparison.v1",
  kind: "table",
  axis: "period",
  concept: "us-gaap:Revenues",
  unit: "USD",
  scale: "millions",
  columns: [{ key: "2023-Q3", label: "2023-Q3" }],
  rows: [],
  deltas: [],
  missing: [],
  hit_count: 0,
  cell_count: 0,
  locators: [],
};

function turn(overrides: Partial<Turn>): Turn {
  return {
    run_id: "r1",
    question: "q",
    status: "done",
    answer: "a",
    citations: [],
    created_at: "2026-08-27T00:00:00Z",
    ...overrides,
  };
}

describe("exportUrl", () => {
  it("builds the CSV download URL for a thread", () => {
    expect(exportUrl("t-1", "comparison")).toBe(
      "/api/threads/t-1/export?kind=comparison&format=csv",
    );
  });

  it("encodes the thread id and carries an explicit run id", () => {
    expect(exportUrl("t 1", "citations", { runId: "r 1", format: "json" })).toBe(
      "/api/threads/t%201/export?kind=citations&format=json&run_id=r+1",
    );
  });
});

describe("exportAvailability", () => {
  it("offers nothing on a thread with no turns", () => {
    expect(exportAvailability([])).toEqual({
      comparison: false,
      citations: false,
    });
  });

  it("offers the comparison export once a turn produced one", () => {
    expect(
      exportAvailability([turn({ comparisons: [COMPARISON] })]),
    ).toEqual({ comparison: true, citations: false });
  });

  it("offers the citation export once a turn has verified spans", () => {
    const citation = {
      document_id: "AAPL-2022Q3",
      version: "a7a0",
      page: 4,
      quote: "x".repeat(40),
    };
    expect(exportAvailability([turn({ citations: [citation] })])).toEqual({
      comparison: false,
      citations: true,
    });
  });

  it("ignores a still-running turn: the server has no row to export yet", () => {
    expect(
      exportAvailability([
        turn({ comparisons: [COMPARISON], in_flight: true }),
      ]),
    ).toEqual({ comparison: false, citations: false });
  });
});
