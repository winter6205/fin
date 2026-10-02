// Comparison-table presentation, kept pure so the parse boundary
// and the rendering decisions are testable without a DOM.
//
// The data is the ``fin.comparison`` envelope from the
// ``compare_facts`` host tool (SSOT:
// src/fin/host/tools/compare_facts.py). Two rules shape this
// module:
//
// 1. The SPA never computes a number. Every figure on screen is a
//    ``display`` string the tool already formatted off a fact row
//    with a locator; a client-side calculation would be a second,
//    ungated source of numbers next to the citation verifier.
// 2. The wire is ``unknown``. ``decodeComparison`` is the boundary —
//    it returns either a typed table or a named refusal. The nullable
//    ``parseComparison`` wrapper remains for presentation callers.

import { citationSourceId, formatUnitLocator } from "./sourceLocator";
import {
  COMPARISON_SCHEMA,
  decodeComparisonContract,
} from "./evidenceContract";
import type { ComparisonDecode } from "./evidenceContract";
import type {
  Comparison,
  ComparisonCell,
  ComparisonDelta,
  ComparisonLocator,
} from "./types";

/** The only envelope SHAPE this client renders (spec §2.1: the token
 * names the shape, the version is the ``schema_version`` sibling). T4's
 * export binds to the same tag, so a future envelope must opt in
 * explicitly rather than flow through this table. */
export { COMPARISON_SCHEMA };

/** Placeholder for a cell with no fact row. Mirrors the tool's
 * ``MISSING_DISPLAY``: a blank cell reads as "zero", the em dash
 * reads as "not reported". */
export const MISSING_CELL_DISPLAY = "—";

/**
 * Decode one wire payload to its typed outcome. A schema-valid empty
 * grid is retained at this boundary so malformed data stays distinct.
 *
 * The nullable compatibility wrapper below additionally hides an empty
 * grid from presentation because an empty table would read as "we looked
 * and the filings report nothing" — a claim the tool never made.
 */
export function decodeComparison(value: unknown): ComparisonDecode {
  return decodeComparisonContract(value);
}

/** Compatibility wrapper for presentation callers that only need to ask
 * whether a table can render. Wire boundaries use ``decodeComparison``
 * so refusals stay visible instead of disappearing as ``null``. */
export function parseComparison(value: unknown): Comparison | null {
  const outcome = decodeComparison(value);
  if (outcome.status !== "ok") return null;
  return outcome.comparison.columns.length > 0 && outcome.comparison.rows.length > 0
    ? outcome.comparison
    : null;
}

/** Every renderable table in a wire list; malformed entries drop
 * out rather than failing the whole turn. */
export function parseComparisons(value: unknown): Comparison[] {
  if (!Array.isArray(value)) return [];
  const out: Comparison[] = [];
  for (const entry of value) {
    const parsed = parseComparison(entry);
    if (parsed !== null) out.push(parsed);
  }
  return out;
}

/** The tables to render under a turn. Takes the field rather than
 * the whole ``Turn`` so the live optimistic row and the persisted
 * row share one accessor. */
export function turnComparisons(turn: {
  comparisons?: unknown;
}): Comparison[] {
  return parseComparisons(turn.comparisons ?? []);
}

/**
 * The caption above the table: what was compared, in what unit,
 * and how much of the grid actually resolved.
 *
 * Coverage is not decoration. A 2-of-6 table looks like a complete
 * answer once the missing cells are em dashes, so the header says
 * the ratio out loud.
 */
export function comparisonCaption(comparison: Comparison): string {
  const unit = [comparison.unit, comparison.scale].filter(Boolean).join(" ");
  const parts = [comparison.concept];
  if (unit) parts.push(unit);
  parts.push(`${comparison.hit_count}/${comparison.cell_count} 命中`);
  return parts.join(" · ");
}

/**
 * The redline whose TARGET is ``rowKey``, if the tool computed one.
 *
 * Undefined is a real answer, not a gap to fill: the baseline row
 * of a peer comparison has nothing to be measured against, and a
 * delta off a missing cell would be an invented figure. Both cases
 * leave the cell blank.
 */
export function deltaForRow(
  comparison: Comparison,
  rowKey: string,
): ComparisonDelta | undefined {
  return comparison.deltas.find((d) => d.to.row === rowKey);
}

/** Distinct glyph per direction so the trend survives a grayscale
 * print and a colour-blind reader. */
export function directionMark(
  direction: ComparisonDelta["direction"],
): string {
  if (direction === "up") return "▲";
  if (direction === "down") return "▼";
  return "→";
}

export function directionClass(
  direction: ComparisonDelta["direction"],
): string {
  if (direction === "up") return "text-emerald-700";
  if (direction === "down") return "text-red-700";
  return "text-gray-500";
}

/** ``document_id p.N`` (or ``u.N`` for a non-physical unit) for a hit —
 * the same locator shape the citation chips use, so a number in the
 * table can be traced to a source unit without leaving the turn. */
export function locatorLabel(locator: ComparisonLocator): string {
  // Identity comes from ``citationSourceId`` — the one rule the chips
  // and the sources list group by — keyed on the row's own ``kind``. The
  // old ``document_id || attachment_id`` chain was a second rule for one
  // concept: a table row and a chip could end up naming different sources
  // for the same figure, and a company row whose document_id was empty
  // read as an attachment. An absent ``kind`` is the current envelope, which
  // carries only document_id. ``quote`` / ``filename`` are inert to the
  // rule — a comparison locator has neither, and "" is what a company
  // citation sends for both.
  const source = citationSourceId({
    kind: locator.kind ?? "company",
    document_id: locator.document_id,
    attachment_id: locator.attachment_id ?? "",
    version: locator.version,
    unit: locator.unit,
    display: locator.display ?? "unit",
    filename: "",
    quote: "",
  });
  return `${source} ${formatUnitLocator(locator.display, locator.unit)}`;
}

/** Hover text for one cell. A missing cell says so in words: the
 * em dash alone is easy to read as a formatting artifact. */
export function cellTitle(cell: ComparisonCell): string {
  if (cell.status !== "hit" || cell.locator === null) return "未报告";
  return locatorLabel(cell.locator);
}
