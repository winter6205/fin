// Sources-list presentation decisions, kept pure so the collapse
// boundary is testable without a DOM.

import type { Citation } from "./types";

/** A turn with MORE than this many verified spans opens collapsed. */
export const SOURCES_COLLAPSE_THRESHOLD = 10;

export function shouldCollapseSourcesList(spanCount: number): boolean {
  return spanCount > SOURCES_COLLAPSE_THRESHOLD;
}

/** Sources header counts pages, not spans: one page can carry many. */
export function countUniqueSourcePages(citations: Citation[]): number {
  return new Set(citations.map((c) => `${c.document_id}:${c.page}`)).size;
}
