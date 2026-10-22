// Sources-list presentation decisions, kept pure so the collapse
// boundary and the unit grouping are testable without a DOM.

import { citationSourceId, citationSourceLabel } from "../../sourceLocator";
import type { Citation, SourceDisplay } from "../../types";

/** A turn with MORE than this many verified spans opens collapsed. */
export const SOURCES_COLLAPSE_THRESHOLD = 10;

export function shouldCollapseSourcesList(spanCount: number): boolean {
  return spanCount > SOURCES_COLLAPSE_THRESHOLD;
}

/** Sources header counts units, not spans: one unit can carry many.
 * The identity keys the set, so the same unit number in two sources
 * never merges into one. */
export function countUniqueSourceUnits(citations: Citation[]): number {
  return new Set(citations.map((c) => `${citationSourceId(c)}:${c.unit}`)).size;
}

/** Header count word: only a source whose units are all physical
 * pages may count 页; any non-PDF unit counts as 单元 instead. */
export function sourceCountWord(
  displays: readonly SourceDisplay[],
): "页" | "单元" {
  return displays.every((display) => display === "page") ? "页" : "单元";
}

export type IndexedCitation = { citation: Citation; globalIndex: number };

export type UnitGroup = {
  unit: number;
  display: SourceDisplay;
  citations: IndexedCitation[];
};

export type SourceGroup = {
  // Stable identity: attachment_id for attachments, document_id for
  // company rows, the permitted view plus record key for a ledger row.
  // Never the filename.
  identity: string;
  label: string;
  units: UnitGroup[];
};

export function groupCitationsBySourceAndUnit(
  citations: Citation[],
): SourceGroup[] {
  const groups = new Map<string, SourceGroup>();

  for (let i = 0; i < citations.length; i += 1) {
    const citation = citations[i];
    const identity = citationSourceId(citation);
    let group = groups.get(identity);
    if (!group) {
      group = { identity, label: citationSourceLabel(citation), units: [] };
      groups.set(identity, group);
    }
    let unitGroup = group.units.find((u) => u.unit === citation.unit);
    if (!unitGroup) {
      // Display is a property of the source file, so citations sharing
      // a source and unit always agree on it; the first one defines it.
      unitGroup = {
        unit: citation.unit,
        display: citation.display,
        citations: [],
      };
      group.units.push(unitGroup);
    }
    unitGroup.citations.push({ citation, globalIndex: i + 1 });
  }

  for (const group of groups.values()) {
    group.units.sort((a, b) => a.unit - b.unit);
  }
  return Array.from(groups.values());
}
