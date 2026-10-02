import type { EvidenceRefusalCode } from "./types";

const COMPARISON_ENVELOPE_KEYS = [
  "schema",
  "schema_version",
  "kind",
  "axis",
  "concept",
  "unit",
  "scale",
  "columns",
  "rows",
  "deltas",
  "missing",
  "hit_count",
  "cell_count",
  "locators",
] as const;

const MAX_COMPARISON_CELLS = 64;
const HEADER_STRING_FIELDS = ["kind", "axis", "concept", "unit", "scale"] as const;

type UnknownRecord = Record<string, unknown>;

interface ValidatedCell {
  hit: boolean;
  locator: UnknownRecord | null;
}

interface GridWalk {
  gridSize: number;
  cellCount: number;
  hitCount: number;
  hitLocators: UnknownRecord[];
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: unknown, expected: readonly string[]): value is UnknownRecord {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  return (
    keys.length === expected.length &&
    expected.every((key) => Object.prototype.hasOwnProperty.call(value, key))
  );
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isColumn(value: unknown): boolean {
  return (
    hasExactKeys(value, ["key", "label"]) &&
    ["key", "label"].every((key) => isString(value[key]))
  );
}

function isRow(value: unknown): value is UnknownRecord & { cells: unknown[] } {
  return (
    hasExactKeys(value, ["key", "label", "kind", "cells"]) &&
    ["key", "label", "kind"].every((key) => isString(value[key])) &&
    Array.isArray(value.cells)
  );
}

function isDocumentLocator(value: unknown): value is UnknownRecord {
  return (
    hasExactKeys(value, ["document_id", "version", "unit"]) &&
    isString(value.document_id) &&
    value.document_id.length > 0 &&
    isString(value.version) &&
    value.version.length > 0 &&
    typeof value.unit === "number" &&
    Number.isInteger(value.unit) &&
    value.unit >= 1
  );
}

function isComparisonCell(value: unknown): ValidatedCell | null {
  const fields = ["row", "column", "display", "unit", "scale"];
  if (
    !hasExactKeys(value, [
      "row",
      "column",
      "status",
      "value",
      "display",
      "unit",
      "scale",
      "locator",
    ]) ||
    !fields.every((key) => isString(value[key]))
  )
    return null;

  if (value.status === "missing") {
    return value.value === null && value.locator === null
      ? { hit: false, locator: null }
      : null;
  }
  if (value.status !== "hit" || !isFiniteNumber(value.value)) return null;
  return isDocumentLocator(value.locator)
    ? { hit: true, locator: value.locator }
    : null;
}

function validateColumns(value: unknown): value is unknown[] {
  return Array.isArray(value) && value.every(isColumn);
}

function walkRows(value: unknown): {
  valid: boolean;
  cellCount: number;
  hitCount: number;
  hitLocators: UnknownRecord[];
} {
  if (!Array.isArray(value))
    return { valid: false, cellCount: 0, hitCount: 0, hitLocators: [] };

  const result = { valid: true, cellCount: 0, hitCount: 0, hitLocators: [] as UnknownRecord[] };
  for (const rawRow of value) {
    if (!isRow(rawRow)) return { ...result, valid: false };
    for (const rawCell of rawRow.cells) {
      const cell = isComparisonCell(rawCell);
      if (cell === null) return { ...result, valid: false };
      result.cellCount += 1;
      if (!cell.hit || cell.locator === null) continue;
      result.hitCount += 1;
      result.hitLocators.push(cell.locator);
    }
  }
  return result;
}

function walkGrid(value: UnknownRecord): GridWalk | null {
  if (!validateColumns(value.columns)) return null;
  const rows = walkRows(value.rows);
  if (!rows.valid || !Array.isArray(value.rows)) return null;
  return {
    gridSize: value.columns.length * value.rows.length,
    cellCount: rows.cellCount,
    hitCount: rows.hitCount,
    hitLocators: rows.hitLocators,
  };
}

function hasConsistentCounts(value: UnknownRecord, grid: GridWalk): boolean {
  return (
    isNonNegativeInteger(value.hit_count) &&
    isNonNegativeInteger(value.cell_count) &&
    value.hit_count === grid.hitCount &&
    value.cell_count === grid.cellCount
  );
}

function sameLocator(left: UnknownRecord, right: UnknownRecord): boolean {
  return (
    left.document_id === right.document_id &&
    left.version === right.version &&
    left.unit === right.unit
  );
}

function hasListedLocators(value: unknown, hitLocators: UnknownRecord[]): boolean {
  if (!Array.isArray(value) || !value.every(isDocumentLocator)) return false;
  const locators = value as UnknownRecord[];
  return hitLocators.every((cellLocator) =>
    locators.some((locator) => sameLocator(locator, cellLocator)),
  );
}

function isDelta(value: unknown): boolean {
  if (
    !hasExactKeys(value, ["from", "to", "absolute", "percent", "direction", "display"]) ||
    !isFiniteNumber(value.absolute) ||
    !isFiniteNumber(value.percent) ||
    !isString(value.direction) ||
    !isString(value.display)
  )
    return false;

  return [value.from, value.to].every(
    (side) =>
      hasExactKeys(side, ["row", "column"]) &&
      isString(side.row) &&
      isString(side.column),
  );
}

function validateDeltas(value: unknown): boolean {
  return Array.isArray(value) && value.every(isDelta);
}

function isMissingEntry(value: unknown): boolean {
  return (
    hasExactKeys(value, ["row", "column", "reason"]) &&
    ["row", "column", "reason"].every((key) => isString(value[key]))
  );
}

function validateMissing(value: unknown): boolean {
  return Array.isArray(value) && value.every(isMissingEntry);
}

/** The key-set check intentionally runs before schema/token recognition.
 * Keep this exact set as the sole owner of the comparison envelope keys. */
export function hasComparisonEnvelopeKeys(value: UnknownRecord): boolean {
  return hasExactKeys(value, COMPARISON_ENVELOPE_KEYS);
}

/** Validate the frozen comparison body after its token/version is known.
 * This is structural validation only; source-version admission remains a
 * server concern because the browser has no trusted run context. */
export function comparisonShapeRefusal(
  value: UnknownRecord,
): EvidenceRefusalCode | null {
  if (!HEADER_STRING_FIELDS.every((key) => isString(value[key])))
    return "malformed_evidence_contract";

  const grid = walkGrid(value);
  if (grid === null || !hasConsistentCounts(value, grid))
    return "malformed_evidence_contract";
  if (grid.gridSize > MAX_COMPARISON_CELLS) return "comparison_overflow";
  if (!hasListedLocators(value.locators, grid.hitLocators))
    return "malformed_evidence_contract";
  if (!validateDeltas(value.deltas) || !validateMissing(value.missing))
    return "malformed_evidence_contract";
  return null;
}
