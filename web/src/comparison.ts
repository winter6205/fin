// Comparison-table presentation, kept pure so the parse boundary
// and the rendering decisions are testable without a DOM.
//
// The data is the ``fin.comparison.v1`` envelope from the
// ``compare_facts`` host tool (SSOT:
// src/fin/host/tools/compare_facts.py). Two rules shape this
// module:
//
// 1. The SPA never computes a number. Every figure on screen is a
//    ``display`` string the tool already formatted off a fact row
//    with a locator; a client-side calculation would be a second,
//    ungated source of numbers next to the citation verifier.
// 2. The wire is ``unknown``. ``parseComparison`` is the boundary —
//    a payload that does not carry the v1 schema tag and a
//    non-empty grid renders nothing rather than crashing the turn.

import type {
  Comparison,
  ComparisonCell,
  ComparisonDelta,
  ComparisonLocator,
} from "./types";

/** The only envelope version this client renders. T4's export
 * binds to the same tag, so a future v2 must opt in explicitly
 * rather than flow through the v1 table. */
export const COMPARISON_SCHEMA = "fin.comparison.v1";

/** Placeholder for a cell with no fact row. Mirrors the tool's
 * ``MISSING_DISPLAY``: a blank cell reads as "zero", the em dash
 * reads as "not reported". */
export const MISSING_CELL_DISPLAY = "—";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Narrow one wire payload to a renderable ``Comparison``, else null.
 *
 * Null covers the two typed EXITs as well as malformed input: both
 * ``empty_comparison`` and ``comparison_overflow`` carry a
 * column-less skeleton, and an empty grid on screen would read as
 * "we looked and the filings report nothing" — a claim the tool
 * never made.
 */
export function parseComparison(value: unknown): Comparison | null {
  if (!isRecord(value)) return null;
  if (value.schema !== COMPARISON_SCHEMA) return null;
  const { columns, rows, deltas, locators, missing } = value;
  if (!Array.isArray(columns) || columns.length === 0) return null;
  if (!Array.isArray(rows) || rows.length === 0) return null;
  if (!Array.isArray(deltas) || !Array.isArray(locators)) return null;
  if (!Array.isArray(missing)) return null;
  return value as unknown as Comparison;
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

/** ``document_id p.N`` for a hit — the same locator shape the
 * citation chips use, so a number in the table can be traced to a
 * page without leaving the turn. */
export function locatorLabel(locator: ComparisonLocator): string {
  return `${locator.document_id} p.${locator.page}`;
}

/** Hover text for one cell. A missing cell says so in words: the
 * em dash alone is easy to read as a formatting artifact. */
export function cellTitle(cell: ComparisonCell): string {
  if (cell.status !== "hit" || cell.locator === null) return "未报告";
  return locatorLabel(cell.locator);
}
