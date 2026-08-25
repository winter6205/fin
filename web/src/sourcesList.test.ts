// SC16 (i): a turn whose verified spans exceed the threshold shows
// its sources list collapsed; at or below it the list stays open.

import { describe, expect, it } from "vitest";
import {
  SOURCES_COLLAPSE_THRESHOLD,
  countUniqueSourcePages,
  shouldCollapseSourcesList,
} from "./sourcesList";
import type { Citation } from "./types";

function citation(page: number, documentId = "AAPL-10Q-2024Q3"): Citation {
  return {
    document_id: documentId,
    version: "v1",
    page,
    quote: `span on page ${page} long enough to be a verified quote`,
  };
}

describe("shouldCollapseSourcesList", () => {
  it("keeps an empty sources list open", () => {
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

describe("countUniqueSourcePages", () => {
  it("counts pages, not spans", () => {
    const citations = [citation(3), citation(3), citation(4)];
    expect(citations).toHaveLength(3);
    expect(countUniqueSourcePages(citations)).toBe(2);
  });

  it("keeps same-numbered pages of different documents apart", () => {
    const citations = [citation(3, "AAPL-10Q"), citation(3, "MSFT-10Q")];
    expect(countUniqueSourcePages(citations)).toBe(2);
  });

  it("counts nothing for a turn with no citations", () => {
    expect(countUniqueSourcePages([])).toBe(0);
  });
});
