// E6 runtime decoding of the evidence channel (spec
// specs/enterprise-evidence-contract.md §2/§3/§6/§7).
//
// Strict ``tsc`` types are compile-time only — history and SSE JSON still
// arrive as ``unknown`` at runtime (spec §6 web-decode row), and the
// web-decode boundary must refuse the same classes the server refuses:
// malformed shape, unsupported contract, and bound overflow, each by its
// §6 name. Following the per-frame guard precedent (comparison.ts,
// visualClaims.ts) every decoder here is a pure function returning a
// TYPED outcome — it never throws on data, never coerces, never drops a
// row silently, and never invents a default.
//
// WHY the decoders rebuild objects from the contract keys instead of
// casting: a row carrying a key the frozen shape does not list is a
// named refusal (§2.4), so there is nothing extra to preserve, and a
// rebuilt object is what makes "renders only runtime-validated evidence"
// structural rather than a promise.

import { parseChoiceWindow } from "./choiceWindow";
import { parseVisualClaim } from "./visualClaims";
import {
  comparisonShapeRefusal,
  hasComparisonEnvelopeKeys,
} from "./comparisonShape";
import type {
  Citation,
  CitationKind,
  Comparison,
  DatabaseRowSnapshot,
  DatabaseScalar,
  EvidenceRefusal,
  EvidenceRefusalCode,
  SourceDisplay,
  ThreadDetail,
  Turn,
  VisualClaim,
} from "./types";

// ----- frozen schema tokens (spec §2.1, emitted per E5) -----

export const CITATION_CHANNEL_SCHEMA = "fin.evidence.citation";
export const DATABASE_ROW_SNAPSHOT_SCHEMA =
  "fin.evidence.database_row_snapshot";
export const VISUAL_CLAIM_SCHEMA = "fin.evidence.visual_claim";
export const COMPARISON_SCHEMA = "fin.comparison";

/** §2.1: the token names the SHAPE; the version is this integer sibling,
 * and every object carrying a ``schema`` carries it too. A version is
 * minted only where the serialized shape can change — never for a stage,
 * a rollout, or a cutover. */
export const EVIDENCE_SCHEMA_VERSION = 1;

/** The one home for the version read (spec §2.1 rule 3), mirroring the
 * server's single ``_check_schema_version``: a PRESENT carrier that is
 * not the current version names the SAME ``unsupported_evidence_contract``
 * as an unrecognized token name, and a carrier that is not an integer is
 * malformed — never the nearest older parser, never a suffix strip.
 *
 * WHY a MISSING version is malformed, not waved through: a token and
 * its version are minted together (spec §2.1 rule 3), so a record that
 * names the shape and omits the number is a producer type bug on the
 * server's reading too — the two sides of the wire must not disagree
 * about the same bytes. The legacy grace is NOT the counter-argument:
 * §7 keys it on the ABSENCE of the token, so a v0 record never reaches
 * this function. Old records stay stored as they are; an active frame
 * gets no silent version backfill and no legacy exemption.
 *
 * WHY the integer test is stated over the DECODED value and not over
 * the JSON text: a JSON number written ``1.0`` parses to the integer
 * ``1`` in JavaScript, and the two are the same value with nothing left
 * to distinguish them. Rejecting "1.0" is not implementable at this
 * boundary, so the rule is deliberately the value rule — a FINITE
 * INTEGER — and no test may pretend otherwise.
 *
 * ``Number.isInteger`` is the whole test because it already excludes
 * ``NaN``, ``±Infinity``, every non-number type, and the bool: it
 * requires a number primitive whose value has no fractional part, and
 * ``Number.isInteger(true)`` is false since ``true`` is not a number.
 * That mirrors the server's ``_is_integer_value`` — the version rule's
 * OWN predicate, deliberately separate from the strict ``_is_int`` type
 * test, which would refuse ``1.0`` and make the two ends disagree about
 * identical bytes (ADR 0093 decision 8). */
export function schemaVersionRefusal(version: unknown): EvidenceRefusalCode | null {
  if (!Number.isInteger(version)) return "malformed_evidence_contract";
  return version === EVIDENCE_SCHEMA_VERSION
    ? null
    : "unsupported_evidence_contract";
}

// ----- §6 code vocabulary (mirrors the server's CONTRACT_REFUSAL_CODES
// plus the delegated gate exits). A refusal whose code is NOT in this
// closed set is itself a malformed entry — prose never rides this lane. -----

export const EVIDENCE_REFUSAL_CODES: readonly EvidenceRefusalCode[] = [
  "malformed_evidence_contract",
  "unsupported_evidence_contract",
  "citation_overflow",
  "visual_claim_overflow",
  "database_row_overflow",
  "business_field_overflow",
  "comparison_overflow",
  "corpus_not_ready",
  "insufficient_evidence",
  "sql_no_matching_rows",
  "sql_query_denied",
  "citation_trust_denied",
  "visual_claim_trust_denied",
  "business_field_trust_denied",
  "comparison_source_unverified",
  "attachment_unavailable",
  "observation_stale",
  "visual_lane_unwired",
  "retrieve_unavailable",
  "persistence_unavailable",
  "tier_not_open",
  "real_db_not_wired",
  // Delegated verifier exits (spec §6 note): gate-owned names the
  // refusal lanes legitimately carry.
  "unknown_locator",
  "citation_repair_cap",
];

const REFUSAL_CODE_SET: ReadonlySet<string> = new Set(
  EVIDENCE_REFUSAL_CODES,
);

// ----- §3 bounds (the web-decode row enforces the same numbers) -----

const MAX_CITATIONS_PER_RUN = 32;
const QUOTE_MAX_CODEPOINTS = 2000;
const SNAPSHOT_MAX_BYTES = 32 * 1024;
const RECORD_KEY_MAX_PAIRS = 8;
const KEY_MAX_CHARS = 64;
const RECORD_KEY_VALUE_MAX_CHARS = 128;
const FIELDS_MAX_ENTRIES = 16;
const FIELD_VALUE_MAX_CHARS = 512;
const SUMMARY_MAX_CODEPOINTS = 1000;
const SOURCE_MAX_CHARS = 128;
const VISUAL_DETAIL_MAX_CODEPOINTS = 4000;

const CITATION_KINDS: readonly CitationKind[] = [
  "company",
  "attachment",
  "database-row",
];

// The v0 closed generative shape (§7.1): the eight frozen keys, plus the
// pre-rename ``page``, plus the one lane sibling that carries a
// ``database_row`` snapshot. Any key outside this set is a refusal — v0 is
// a closed shape, not "anything without ``schema``".
const CITATION_KEY_NAMES: readonly string[] = [
  "kind",
  "document_id",
  "attachment_id",
  "version",
  "unit",
  "display",
  "filename",
  "quote",
  "page",
  "database_row",
];

const SNAPSHOT_V0_KEYS: readonly string[] = [
  "source",
  "record_key",
  "queried_at",
  "fields",
  "summary",
];
// ``schema_version`` joins the closed set beside ``schema`` (spec §2.1:
// every object that carries ``schema`` also carries it). Without it here
// a fully-conforming current emission would be refused as a malformed
// unknown key — the closed set is what makes "unknown key is a named
// refusal" work, so a new frozen key has to be added to it.
const SNAPSHOT_KEY_NAMES: readonly string[] = [
  "schema",
  "schema_version",
  ...SNAPSHOT_V0_KEYS,
];

// §2.5 strict timestamp: pattern plus calendar ranges; ``:60`` (a leap
// second) and offsets/fractions are refused.
const TIMESTAMP_RE =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

/** The §4.4 value-text rule: strings as-is, numbers as shortest
 * round-trip decimal, booleans lowercase, null as "". Declared
 * normalization, not silent coercion — the text form is the contract's
 * canonical value representation.
 *
 * WHY ``String(number)``: the ECMAScript number-to-string algorithm is
 * the shortest round-trip form, and decimal-exponential notation is only
 * chosen past 1e21 — a ledger scalar that large is out of scope and
 * still round-trips, so no second formatter is introduced here. */
export function snapshotValueText(value: DatabaseScalar): string {
  if (value === null) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function codePoints(value: string): number {
  // §3 bounds count CODE POINTS; ``length`` counts UTF-16 units and
  // would let astral text pass a bound it breaks.
  return Array.from(value).length;
}

function isStrictTimestamp(value: unknown): boolean {
  if (!isString(value) || !TIMESTAMP_RE.test(value)) return false;
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const hour = Number(value.slice(11, 13));
  const minute = Number(value.slice(14, 16));
  const second = Number(value.slice(17, 19));
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59)
    return false;
  const year = Number(value.slice(0, 4));
  const leap =
    year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= daysInMonth[month - 1];
}

function refusal(index: number, code: EvidenceRefusalCode): EvidenceRefusal {
  return { citation_index: index, error: code };
}

// ----- envelope field (evidence_schema) -----

export type EnvelopeOutcome = "known" | "absent" | "unsupported" | "malformed";

/** The channel declaration on a response envelope / terminal status
 * frame. It is absent only when both the token and its sibling version
 * are absent, as on a peer predating E5. If either half is stated alone,
 * the envelope is malformed before token recognition; an unknown token
 * with its version present is unsupported.
 *
 * ``version`` is the envelope's ``schema_version`` sibling (spec §2.1
 * pins it beside ``evidence_schema``, not inside the citation row). It is
 * optional in this TypeScript signature so callers can decode absence
 * and malformed half-contracts without fabricating a value. The §7
 * legacy grace applies only to the fully absent pair. */
export function decodeEvidenceSchemaField(
  value: unknown,
  version?: unknown,
): EnvelopeOutcome {
  const hasToken = value !== undefined;
  const hasVersion = version !== undefined;
  if (!hasToken && !hasVersion) return "absent";
  if (hasToken !== hasVersion) return "malformed";
  if (!isString(value)) return "malformed";
  if (value !== CITATION_CHANNEL_SCHEMA) return "unsupported";
  const versionProblem = schemaVersionRefusal(version);
  if (versionProblem === null) return "known";
  return versionProblem === "malformed_evidence_contract"
    ? "malformed"
    : "unsupported";
}

// ----- evidence_refusals entries / rejected_citations rows -----

export type RefusalEntryDecode =
  | { status: "ok"; refusal: EvidenceRefusal }
  | { status: "malformed"; refusal: EvidenceRefusal };

/** One ``{citation_index, error}`` entry (the shape E5's SSE event and
 * E4's history read share). A non-integer / negative index or a code
 * outside the §6 set makes the ENTRY itself a visible malformed refusal
 * — never a swallowed entry, because a swallowed refusal is the silent
 * drop §2.4 forbids. ``position`` is the entry's place in the list, the
 * fallback index when its own is unusable. */
export function decodeEvidenceRefusalEntry(
  value: unknown,
  position: number,
): RefusalEntryDecode {
  if (!isRecord(value)) {
    return {
      status: "malformed",
      refusal: refusal(position, "malformed_evidence_contract"),
    };
  }
  const index = value.citation_index;
  const code = value.error;
  const indexUsable =
    typeof index === "number" && Number.isInteger(index) && index >= 0;
  if (!isString(code) || !REFUSAL_CODE_SET.has(code)) {
    return {
      status: "malformed",
      refusal: refusal(indexUsable ? index : position, "malformed_evidence_contract"),
    };
  }
  if (!indexUsable) {
    return {
      status: "malformed",
      refusal: refusal(position, "malformed_evidence_contract"),
    };
  }
  return {
    status: "ok",
    refusal: refusal(index, code as EvidenceRefusalCode),
  };
}

/** The ``evidence_refusals`` SSE frame body ``{refusals: [...]}``. An
 * empty list decodes to defined-empty (never a failure); a payload that
 * is not that shape carries no entries; every entry — valid or
 * malformed — stays visible. */
export function decodeEvidenceRefusalsFrame(payload: unknown): EvidenceRefusal[] {
  if (!isRecord(payload) || !Array.isArray(payload.refusals)) return [];
  const out: EvidenceRefusal[] = [];
  payload.refusals.forEach((entry, position) => {
    const decoded = decodeEvidenceRefusalEntry(entry, position);
    out.push(decoded.refusal);
  });
  return out;
}

// ----- database_row snapshot (§4.4; R2 scalars-plus-text) -----

export type SnapshotDecode =
  | { status: "ok"; snapshot: DatabaseRowSnapshot }
  | { status: "refusal"; error: EvidenceRefusalCode };

/** WHICH CONTRACT the snapshot bytes are read under — the caller's
 * intent, made explicit because the two ends of the wire are judged
 * under different contracts and one shared decoder serves both.
 *
 * ``emission`` — a FRESH frame the server is publishing right now. The
 * full E5 pairing applies: a snapshot naming neither marker is
 * ``malformed_evidence_contract``, exactly as
 * ``evidence_contract._validate_snapshot`` refuses it
 * (``database_row snapshot: missing ['schema']``). This is the default
 * so a new call site cannot silently acquire the legacy leniency.
 *
 * ``stored-replay`` — a snapshot read back out of ``fin_run.citations``
 * by the history read. §7 keeps those "left exactly as they are", so
 * the pre-E5 keyless v0 shape stays accepted and its half a contract is
 * dropped rather than refused.
 *
 * WHY the mode is a CALLER decision and not a payload sniff: the bytes
 * of a token-less snapshot are identical on both paths, so the decoder
 * cannot tell an active frame from a stored record — only the caller
 * knows which door the bytes came through. Guessing would either
 * re-introduce the emission divergence (lenient everywhere) or break
 * replay of pre-cutover turns (strict everywhere). */
export type SnapshotReadMode = "emission" | "stored-replay";

function isScalar(value: unknown): value is DatabaseScalar {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

/** record_key / fields per §3: an object of string keys to JSON scalars
 * (a nested mapping is never legal). Decoded in the server's two
 * passes — structure first (wrong types are malformed), entry and text
 * bounds only after the whole-snapshot byte bound has been evaluated,
 * so a snapshot breaking both names the byte bound (§3 precedence). */
function decodeScalarMapping(
  value: unknown,
  maxEntries: number,
  valueMaxChars: number,
  structuralOnly: boolean,
): { ok: true; mapping: Record<string, DatabaseScalar> } | { ok: false; error: EvidenceRefusalCode } {
  if (!isRecord(value)) return { ok: false, error: "malformed_evidence_contract" };
  const entries = Object.entries(value);
  if (!structuralOnly && entries.length > maxEntries)
    return { ok: false, error: "database_row_overflow" };
  const mapping: Record<string, DatabaseScalar> = {};
  for (const [key, raw] of entries) {
    if (!isScalar(raw)) return { ok: false, error: "malformed_evidence_contract" };
    mapping[key] = raw;
    if (structuralOnly) continue;
    if (key.length > KEY_MAX_CHARS) return { ok: false, error: "database_row_overflow" };
    if (codePoints(snapshotValueText(raw)) > valueMaxChars)
      return { ok: false, error: "database_row_overflow" };
  }
  return { ok: true, mapping };
}

/** Canonical-JSON byte length of the snapshot's identity input ``m``
 * (§4.4): the five data keys, every value first rendered to its text
 * form, object keys sorted lexicographically, no insignificant
 * whitespace, UTF-8.
 *
 * WHY this is the byte bound's only canonical use, and the web never
 * recomputes the DIGEST itself (spec §9 deferral, ADR 0092): the wire
 * carries no digest — the fin consumer mints it at admission, server
 * side, and the producer never sends one, so a browser hash would
 * verify nothing. A recomputed hex could also diverge from the
 * server's: Python renders a whole-valued float as ``30.0`` where JS
 * renders ``30``, and ADR 0092's text-only guarantee holds only
 * because ONE language renders the text. The byte bound is
 * language-safe because its inputs are already strings; the pin
 * against the committed digest_worksheet's ``canonical_json`` lives in
 * evidenceContract.test.ts. */
export function canonicalSnapshotBytes(snapshot: DatabaseRowSnapshot): number {
  const m: Record<string, unknown> = {
    source: snapshot.source,
    record_key: textMapping(snapshot.record_key),
    queried_at: snapshot.queried_at,
    fields: textMapping(snapshot.fields),
    summary: snapshot.summary,
  };
  return new TextEncoder().encode(stableStringify(m)).length;
}

function textMapping(mapping: Record<string, DatabaseScalar>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(mapping)) out[key] = snapshotValueText(value);
  return out;
}

function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (isRecord(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** One ``database_row`` snapshot. The frozen token is accepted only
 * exactly as named, and only with the current ``schema_version``; an
 * UNKNOWN token — or a recognized name carrying an unsupported version
 * — is ``unsupported_evidence_contract``, never the nearest older parser
 * (§2.3). A PRESENT token brings the §2.1 pairing in full: a missing or
 * non-integer version beside it is ``malformed_evidence_contract``
 * (ADR 0093 decision 12). §3 precedence: the 32 KiB
 * serialized-byte bound is evaluated on the whole snapshot before
 * per-entry bounds.
 *
 * ``mode`` says WHICH contract the bytes are read under, and it is the
 * caller's to declare because the decoder cannot tell the two apart —
 * see :type:`SnapshotReadMode`. Under ``"emission"`` a snapshot naming
 * NEITHER marker is refused as ``malformed_evidence_contract``, which is
 * what the server's ``_validate_snapshot`` does to the same bytes at the
 * live frame (``_citations_wire_projection``), so the two ends answer
 * alike. Under ``"stored-replay"`` a snapshot naming no ``schema`` is the
 * pre-E5 v0 shape §7 leaves exactly as it is, so it stays accepted and
 * a version it cannot pair with a token is dropped rather than kept as
 * half a contract (ADR 0093 decision 9's surviving v0 half). */
export function decodeDatabaseRowSnapshot(
  value: unknown,
  mode: SnapshotReadMode = "emission",
): SnapshotDecode {
  if (!isRecord(value))
    return { status: "refusal", error: "malformed_evidence_contract" };
  for (const key of Object.keys(value)) {
    if (!SNAPSHOT_KEY_NAMES.includes(key))
      return { status: "refusal", error: "malformed_evidence_contract" };
  }
  // The emission half of the §2.1 pairing, enforced BEFORE the token
  // checks so the refusal cannot be attributed to them: an active frame
  // that omits its ``schema`` names no shape, and the server names the
  // very same class for the very same bytes. §7.5 scopes the grace to
  // STORED records — "the grace applies to stored records, not to fresh
  // producers" — and ADR 0093 decision 12 makes that explicit: an active
  // frame may not borrow the legacy rule to skip version validation.
  const hasSchema = "schema" in value;
  const hasSchemaVersion = "schema_version" in value;
  if (mode === "emission" && !hasSchema)
    return { status: "refusal", error: "malformed_evidence_contract" };
  // A stored replay with neither marker is the valid v0 grace shape. A
  // fresh emission always needs both markers, and a stored row which did
  // state a token cannot omit its paired version, regardless of whether
  // this reader recognizes that token.
  if (hasSchema && !hasSchemaVersion)
    return { status: "refusal", error: "malformed_evidence_contract" };
  if (hasSchema) {
    if (!isString(value.schema))
      return { status: "refusal", error: "malformed_evidence_contract" };
    if (value.schema !== DATABASE_ROW_SNAPSHOT_SCHEMA)
      return { status: "refusal", error: "unsupported_evidence_contract" };
    const versionProblem = schemaVersionRefusal(value.schema_version);
    if (versionProblem !== null)
      return { status: "refusal", error: versionProblem };
  }
  if (!isString(value.source) || !isString(value.summary))
    return { status: "refusal", error: "malformed_evidence_contract" };
  if (!isString(value.queried_at) || !isStrictTimestamp(value.queried_at))
    return { status: "refusal", error: "malformed_evidence_contract" };
  const recordKey = decodeScalarMapping(
    value.record_key,
    RECORD_KEY_MAX_PAIRS,
    RECORD_KEY_VALUE_MAX_CHARS,
    true,
  );
  const fields = decodeScalarMapping(
    value.fields,
    FIELDS_MAX_ENTRIES,
    FIELD_VALUE_MAX_CHARS,
    true,
  );
  if (!recordKey.ok) return { status: "refusal", error: recordKey.error };
  if (!fields.ok) return { status: "refusal", error: fields.error };
  const snapshot: DatabaseRowSnapshot = {
    // Both contract fields ride together or not at all (spec §2.1 rule 3):
    // reaching this line WITH the token means the guard above already
    // proved the version is the current integer, so the pair is rebuilt
    // whole rather than conditionally. A REPLAYED v0 snapshot takes the
    // empty branch and its stray version is dropped rather than kept as
    // half a contract (ADR 0093 decision 9's surviving v0 half, spec
    // §7.1 "left exactly as they are"). Under ``"emission"`` this branch
    // is unreachable — a token-less frame was already refused above.
    ...(value.schema === DATABASE_ROW_SNAPSHOT_SCHEMA
      ? { schema: DATABASE_ROW_SNAPSHOT_SCHEMA, schema_version: EVIDENCE_SCHEMA_VERSION }
      : {}),
    source: value.source,
    record_key: recordKey.mapping,
    queried_at: value.queried_at,
    fields: fields.mapping,
    summary: value.summary,
  };
  // Byte bound BEFORE the per-entry bounds (§3), so a snapshot breaking
  // both names the byte bound.
  if (canonicalSnapshotBytes(snapshot) > SNAPSHOT_MAX_BYTES)
    return { status: "refusal", error: "database_row_overflow" };
  const boundedRecordKey = decodeScalarMapping(
    value.record_key,
    RECORD_KEY_MAX_PAIRS,
    RECORD_KEY_VALUE_MAX_CHARS,
    false,
  );
  const boundedFields = decodeScalarMapping(
    value.fields,
    FIELDS_MAX_ENTRIES,
    FIELD_VALUE_MAX_CHARS,
    false,
  );
  if (!boundedRecordKey.ok) return { status: "refusal", error: boundedRecordKey.error };
  if (!boundedFields.ok) return { status: "refusal", error: boundedFields.error };
  if (sourceOrSummaryOverflow(value.source, value.summary))
    return { status: "refusal", error: "database_row_overflow" };
  return { status: "ok", snapshot };
}

export type ComparisonDecode =
  | { status: "ok"; comparison: Comparison }
  | { status: "refusal"; error: EvidenceRefusalCode };

/** Runtime contract guard shared by the live comparison frame and the
 * history read. The token/version pair is checked before token recognition
 * so an unknown half-contract remains malformed instead of hiding the
 * missing version behind an unsupported-token result. */
export function decodeComparisonContract(value: unknown): ComparisonDecode {
  if (!isRecord(value))
    return { status: "refusal", error: "malformed_evidence_contract" };

  // The server closes the whole object before it inspects the token. Keep
  // that precedence here so an unknown token with missing/extra fields is
  // malformed rather than a misleading unsupported-version result.
  if (!hasComparisonEnvelopeKeys(value))
    return { status: "refusal", error: "malformed_evidence_contract" };

  const hasSchema = "schema" in value;
  const hasSchemaVersion = "schema_version" in value;
  if (!hasSchema || !hasSchemaVersion)
    return { status: "refusal", error: "malformed_evidence_contract" };
  if (!isString(value.schema))
    return { status: "refusal", error: "malformed_evidence_contract" };
  if (value.schema !== COMPARISON_SCHEMA)
    return { status: "refusal", error: "unsupported_evidence_contract" };
  const versionProblem = schemaVersionRefusal(value.schema_version);
  if (versionProblem !== null)
    return { status: "refusal", error: versionProblem };

  const shapeProblem = comparisonShapeRefusal(value);
  if (shapeProblem !== null)
    return { status: "refusal", error: shapeProblem };
  return { status: "ok", comparison: value as unknown as Comparison };
}

function decodeComparisonRows(value: unknown): {
  comparisons: Comparison[];
  refusals: EvidenceRefusal[];
} {
  if (value === undefined) return { comparisons: [], refusals: [] };
  if (!Array.isArray(value)) {
    return {
      comparisons: [],
      refusals: [refusal(0, "malformed_evidence_contract")],
    };
  }
  const comparisons: Comparison[] = [];
  const refusals: EvidenceRefusal[] = [];
  value.forEach((entry, index) => {
    const outcome = decodeComparisonContract(entry);
    if (outcome.status === "ok") comparisons.push(outcome.comparison);
    else refusals.push(refusal(index, outcome.error));
  });
  return { comparisons, refusals };
}

function sourceOrSummaryOverflow(source: string, summary: string): boolean {
  return (
    source.length > SOURCE_MAX_CHARS ||
    codePoints(summary) > SUMMARY_MAX_CODEPOINTS
  );
}

// ----- citation rows -----

export type CitationRowDecode =
  | { status: "ok"; citation: Citation }
  | { status: "refusal"; refusal: EvidenceRefusal };

function rowRefusal(index: number, code: EvidenceRefusalCode): CitationRowDecode {
  return { status: "refusal", refusal: refusal(index, code) };
}

/** Narrow one wire row to a ``Citation``, or name the refusal.
 *
 * Two generations decode (spec §7), each through its own path: the current
 * frozen shape (every one of the eight keys stated, no legacy ``page``)
 * through :func:`decodeFullCitationShape`, and the v0 CLOSED GENERATIVE
 * shape (a key subset of the nine legacy names; absent keys take the
 * frozen v0 defaults; ``page`` reads as ``unit`` through the identity
 * rename) through :func:`decodeV0CitationShape`. The v0 kind default is
 * legal ONLY while no lane marker is present, which is what keeps it
 * from being the row→citation coercion §4.4 forbids. v0-permissive for
 * fresh emissions stays the server's job; the web decodes what the
 * server emitted.
 *
 * ``mode`` is the ROW's read contract and rides the lane marker down to
 * the snapshot: a live citations frame is an emission and a history
 * turn is a replay (see :type:`SnapshotReadMode`). It defaults to the
 * strict reading so a new call site cannot inherit the replay grace. */
export function decodeCitationRow(
  value: unknown,
  index: number,
  mode: SnapshotReadMode = "emission",
): CitationRowDecode {
  if (!isRecord(value)) return rowRefusal(index, "malformed_evidence_contract");
  for (const key of Object.keys(value)) {
    if (!CITATION_KEY_NAMES.includes(key))
      return rowRefusal(index, "malformed_evidence_contract");
  }
  const fullShaped =
    CITATION_FULL_KEYS.every((key) => key in value) && !("page" in value);
  return fullShaped
    ? decodeFullCitationShape(value, index, mode)
    : decodeV0CitationShape(value, index, mode);
}

// The eight keys a current emission states — no defaults are legal here.
// WHY this dispatch is on key SHAPE and never on a token: the citation
// element itself carries no ``schema`` (spec §2.1 rule 2 — it is versioned
// by additive sibling keys, the channel is declared once on the envelope),
// so the shape of the row is the only signal that separates the two
// generations. Renaming the identifier is for accuracy; the dispatch
// itself is unchanged.
const CITATION_FULL_KEYS: readonly string[] = [
  "kind",
  "document_id",
  "attachment_id",
  "version",
  "unit",
  "display",
  "filename",
  "quote",
];

/** The eight slots after shape decoding: strings/integers, no
 * ``unknown`` left, plus the raw (still undecoded) lane snapshot. */
interface CitationSlots {
  kind: CitationKind;
  document_id: string;
  attachment_id: string;
  version: string;
  unit: number;
  display: SourceDisplay;
  filename: string;
  quote: string;
  snapshot?: unknown;
}

function checkStringSlots(
  value: Record<string, unknown>,
  index: number,
): CitationRowDecode | null {
  for (const name of ["document_id", "attachment_id", "display", "filename", "version", "quote"]) {
    if (!isString(value[name]))
      return rowRefusal(index, "malformed_evidence_contract");
  }
  return null;
}

function decodeFullCitationShape(
  value: Record<string, unknown>,
  index: number,
  mode: SnapshotReadMode,
): CitationRowDecode {
  if (!isString(value.kind) || !CITATION_KINDS.includes(value.kind as CitationKind))
    return rowRefusal(index, "unsupported_evidence_contract");
  const kind = value.kind as CitationKind;
  const hasMarker = "database_row" in value;
  if (hasMarker && kind !== "database-row")
    return rowRefusal(index, "malformed_evidence_contract");
  if (kind === "database-row" && !hasMarker)
    return rowRefusal(index, "malformed_evidence_contract");
  const unit = value.unit;
  if (typeof unit !== "number" || !Number.isInteger(unit) || unit < 1)
    return rowRefusal(index, "malformed_evidence_contract");
  const bad = checkStringSlots(value, index);
  if (bad !== null) return bad;
  if (!value.version) return rowRefusal(index, "malformed_evidence_contract");
  return finishCitation(
    {
      kind,
      document_id: value.document_id as string,
      attachment_id: value.attachment_id as string,
      version: value.version as string,
      unit,
      display: value.display as SourceDisplay,
      filename: value.filename as string,
      quote: value.quote as string,
      ...(hasMarker ? { snapshot: value.database_row } : {}),
    },
    index,
    mode,
  );
}

function decodeV0CitationShape(
  value: Record<string, unknown>,
  index: number,
  mode: SnapshotReadMode,
): CitationRowDecode {
  const hasMarker = "database_row" in value;
  let kind: CitationKind;
  if ("kind" in value) {
    if (!isString(value.kind) || !CITATION_KINDS.includes(value.kind as CitationKind))
      return rowRefusal(index, "unsupported_evidence_contract");
    kind = value.kind as CitationKind;
  } else {
    // A kindless row carrying a lane marker is a contradiction, not
    // a v0 row.
    if (hasMarker) return rowRefusal(index, "malformed_evidence_contract");
    kind = "company";
  }
  if (hasMarker && kind !== "database-row")
    return rowRefusal(index, "malformed_evidence_contract");
  if (kind === "database-row" && !hasMarker)
    return rowRefusal(index, "malformed_evidence_contract");

  // unit / page per §7.3 and the server's frozen projection
  // (thread_store.project_stored_citation): EXACTLY ONE of the two
  // names the ordinal. BOTH is a contradiction EVEN WHEN THE VALUES
  // AGREE — one authority, the server's §7 rule, so equality never
  // rescues a row that states its ordinal twice; neither is corruption;
  // either refusal is the compatibility ``unsupported``, never a default.
  const hasUnit = "unit" in value;
  const hasPage = "page" in value;
  if (hasUnit === hasPage)
    return rowRefusal(index, "unsupported_evidence_contract");
  const ordinal = hasUnit ? value.unit : value.page;
  if (typeof ordinal !== "number" || !Number.isInteger(ordinal) || ordinal < 1)
    return rowRefusal(index, "malformed_evidence_contract");

  // v0 defaults are frozen facts of the legacy shape (§7.1): absent
  // attachment_id / filename read as "", absent display reads "page".
  // Absent kind was resolved above; every absent default here is a
  // documented shape rule, never an invented value.
  const withDefaults: Record<string, unknown> = {
    document_id: "document_id" in value ? value.document_id : "",
    attachment_id: "attachment_id" in value ? value.attachment_id : "",
    filename: "filename" in value ? value.filename : "",
    display: "display" in value ? value.display : "page",
    version: "version" in value ? value.version : "",
    quote: "quote" in value ? value.quote : "",
  };
  const bad = checkStringSlots(withDefaults, index);
  if (bad !== null) return bad;
  const document_id = withDefaults.document_id as string;
  const version = withDefaults.version as string;
  // v0 identity requirement: a blank version cannot name a source (the
  // blank-document case is named in ``finishCitation``, shared with the
  // full shape).
  if (!version) return rowRefusal(index, "malformed_evidence_contract");
  return finishCitation(
    {
      kind,
      document_id,
      attachment_id: withDefaults.attachment_id as string,
      version,
      unit: ordinal,
      display: withDefaults.display as SourceDisplay,
      filename: withDefaults.filename as string,
      quote: withDefaults.quote as string,
      ...(hasMarker ? { snapshot: value.database_row } : {}),
    },
    index,
    mode,
  );
}

/** The lane decision both shapes converge on: the ledger snapshot pair
 * or the passage (company/attachment) identity, bounds, and quote
 * ceiling. */
function finishCitation(
  slots: CitationSlots,
  index: number,
  mode: SnapshotReadMode,
): CitationRowDecode {
  const { kind, document_id, attachment_id, version, unit, display, filename, quote } = slots;
  // Identity requirement (both generations): a company row without a
  // document cannot name a source.
  if (!document_id && kind === "company")
    return rowRefusal(index, "malformed_evidence_contract");
  if (kind === "database-row") {
    // The frozen placeholders (§4.4): a record has no page, no ordinal
    // meaning, and no filing behind it.
    if (display !== "record" || unit !== 1 || document_id !== "" || attachment_id !== "")
      return rowRefusal(index, "malformed_evidence_contract");
    const snapshot = decodeDatabaseRowSnapshot(slots.snapshot, mode);
    if (snapshot.status === "refusal") {
      return rowRefusal(
        index,
        snapshot.error === "unsupported_evidence_contract"
          ? "unsupported_evidence_contract"
          : snapshot.error === "database_row_overflow"
            ? "database_row_overflow"
            : "malformed_evidence_contract",
      );
    }
    // The flat slots mirror the snapshot; a mismatch means the row
    // states two identities at once (§4.4).
    if (version !== snapshot.snapshot.queried_at || quote !== snapshot.snapshot.summary)
      return rowRefusal(index, "malformed_evidence_contract");
    return {
      status: "ok",
      citation: {
        kind,
        document_id,
        attachment_id,
        version,
        unit,
        display,
        filename,
        quote,
        database_row: snapshot.snapshot,
      },
    };
  }

  if (display !== "page" && display !== "unit")
    return rowRefusal(index, "malformed_evidence_contract");
  if (kind === "attachment") {
    if (!attachment_id || document_id !== "")
      return rowRefusal(index, "malformed_evidence_contract");
  } else if (attachment_id !== "") {
    return rowRefusal(index, "malformed_evidence_contract");
  }
  // Quote bounds ride the citation lane. WHY only the ceiling is a web
  // check: the 32-code-point floor is the text-span VERIFIER's rule
  // (spec §4.1/§6 — delegated gate exits); the web cannot re-derive it
  // without the received text, and its duty is to render what the server
  // emitted and to surface the gate's named refusals as the gate named
  // them. The 2,000-code-point ceiling is a transport bound decidable
  // from the row alone (§3), so overflow refuses the row by name. A
  // ledger row's quote is its summary and is bounded there.
  if (codePoints(quote) > QUOTE_MAX_CODEPOINTS)
    return rowRefusal(index, "citation_overflow");
  return {
    status: "ok",
    citation: { kind, document_id, attachment_id, version, unit, display, filename, quote },
  };
}

export interface CitationsDecode {
  citations: Citation[];
  refusals: EvidenceRefusal[];
  envelope?: EnvelopeOutcome;
}

/** The citations channel of one wire payload: a plain array (the SSE
 * ``citations`` frame and a history turn row) . Frame overflow refuses
 * the WHOLE frame as ``citation_overflow`` — never a truncated render of
 * the first 32 (§3: the consumer does not truncate; §6.1: over-limit is
 * shown as the named failure, not a partial success). A row that fails
 * keeps its original frame index so the indicator can point at it.
 *
 * ``mode`` is which door the array arrived through — the live SSE
 * ``citations`` frame is an emission, a history turn row is a replay
 * (see :type:`SnapshotReadMode`). Defaulting to ``"emission"`` keeps a
 * new call site strict unless it says otherwise out loud. */
export function decodeCitationsArray(
  value: unknown,
  mode: SnapshotReadMode = "emission",
): CitationsDecode {
  if (!Array.isArray(value))
    return { citations: [], refusals: [refusal(0, "malformed_evidence_contract")] };
  if (value.length > MAX_CITATIONS_PER_RUN)
    return { citations: [], refusals: [refusal(0, "citation_overflow")] };
  const citations: Citation[] = [];
  const refusals: EvidenceRefusal[] = [];
  value.forEach((row, index) => {
    const decoded = decodeCitationRow(row, index, mode);
    if (decoded.status === "ok") citations.push(decoded.citation);
    else refusals.push(decoded.refusal);
  });
  return { citations, refusals };
}

/** A conformance-envelope payload ``{evidence_schema, citations}`` (the
 * shape the committed citation fixtures carry). */
export function decodeCitationsEnvelope(payload: unknown): CitationsDecode {
  if (!isRecord(payload))
    return { citations: [], refusals: [refusal(0, "malformed_evidence_contract")], envelope: "malformed" };
  const envelope = decodeEvidenceSchemaField(
    payload.evidence_schema,
    payload.schema_version,
  );
  const frame = decodeCitationsArray(payload.citations);
  const refusals = [...frame.refusals];
  if (envelope === "unsupported")
    refusals.unshift(refusal(0, "unsupported_evidence_contract"));
  if (envelope === "malformed")
    refusals.unshift(refusal(0, "malformed_evidence_contract"));
  return { citations: frame.citations, refusals, envelope };
}

// ----- visual claims (§4.2; the lane's own schema token) -----

export type VisualClaimRowDecode =
  | { status: "ok"; claim: VisualClaim }
  | { status: "refusal"; refusal: EvidenceRefusal };

/** One ``visual_claims[]`` element. The v0 fourteen-key row stays
 * valid (§7) and, per the same keyed-on-absence rule, is never judged
 * on a version; a PRESENT schema token must be exactly
 * ``fin.evidence.visual_claim`` AND carry the current
 * ``schema_version`` — an unknown token or a wrong integer version names
 * ``unsupported_evidence_contract`` (the nearest-older-parser trap §2.3
 * forbids), while a missing or non-integer version is
 * ``malformed_evidence_contract``. Shape validation itself stays in
 * visualClaims.ts so the lane's rebuild-not-cast posture (no ``quote``
 * can enter) has one home. */
export function decodeVisualClaimRow(
  value: unknown,
  index: number,
): VisualClaimRowDecode {
  if (isRecord(value) && "schema" in value) {
    if (!isString(value.schema))
      return { status: "refusal", refusal: refusal(index, "malformed_evidence_contract") };
    if (value.schema !== VISUAL_CLAIM_SCHEMA)
      return { status: "refusal", refusal: refusal(index, "unsupported_evidence_contract") };
    const versionProblem = schemaVersionRefusal(value.schema_version);
    if (versionProblem !== null)
      return { status: "refusal", refusal: refusal(index, versionProblem) };
  }
  const claim = parseVisualClaim(value);
  if (claim === null)
    return { status: "refusal", refusal: refusal(index, "malformed_evidence_contract") };
  if (codePoints(claim.detail) > VISUAL_DETAIL_MAX_CODEPOINTS)
    return { status: "refusal", refusal: refusal(index, "visual_claim_overflow") };
  return { status: "ok", claim };
}

// ----- history seam (ThreadDetail) -----

/** Narrow the wire ``pending_turn`` to the paused choice a reload must
 * restore, else ``undefined``. The wire is ``unknown``: ``run_id`` and
 * ``question`` must be non-blank strings and ``choice_window`` must pass
 * the SAME ``parseChoiceWindow`` boundary the live SSE frame uses, so a
 * reloaded window renders identically to a streamed one. Any part that
 * fails narrows the WHOLE pending turn away — a half-built projection
 * would name a run with no renderable prompt, which is worse than no
 * card (the operator would see a stuck, unanswerable question). */
function decodePendingTurn(
  value: unknown,
): NonNullable<ThreadDetail["pending_turn"]> | undefined {
  if (!isRecord(value)) return undefined;
  const runId = value.run_id;
  if (!isString(runId) || runId.trim().length === 0) return undefined;
  const question = value.question;
  if (!isString(question) || question.trim().length === 0) return undefined;
  const choice = parseChoiceWindow(value.choice_window);
  if (choice === null) return undefined;
  return { run_id: runId, question, choice_window: choice };
}

/** Assemble the decoded detail: the turn table plus the ``pending_turn``
 * projection, which rides through untouched ONLY when it narrows; a
 * malformed or ``null`` value drops to absent so a caller never reads a
 * half-built paused turn (see ``decodePendingTurn``). ``detail`` still
 * carries the raw wire key from the blind cast, so it is removed first and
 * the key is re-added only for a valid narrowed turn — a caller can then
 * test ``"pending_turn" in detail`` to tell "absent" from "paused". */
function withPendingTurn(
  detail: ThreadDetail,
  turns: Turn[],
  pendingTurn: NonNullable<ThreadDetail["pending_turn"]> | undefined,
): ThreadDetail {
  const decoded: ThreadDetail = { ...detail, turns };
  delete decoded.pending_turn;
  if (pendingTurn !== undefined) decoded.pending_turn = pendingTurn;
  return decoded;
}

/** Decode a ``GET /api/threads/{id}`` body through the evidence guards.
 *
 * The blind cast this replaces kept ``payload as Citation[]`` valid only
 * while the server was trusted to have validated already; §6's web row
 * makes runtime decode the requirement. Non-evidence turn fields pass
 * through untouched — this seam's scope is the citation channel, the
 * envelope token, and the history read's ``rejected_citations`` rows,
 * which are folded into the SAME ``evidence_refusals`` field the live
 * SSE event fills so a reloaded turn renders identically. Comparison
 * envelopes use the same tagged decoder on history and live frames. A body that
 * is not a thread-detail object at all is the transport/decode exception
 * class and surfaces as a throw the route's error path renders — never
 * as an empty history. */
export function decodeThreadDetail(body: unknown): ThreadDetail {
  if (!isRecord(body)) throw new Error("malformed thread detail payload");
  const { id, title, created_at, turns } = body;
  if (!isString(id) || !isString(title) || !isString(created_at) || !Array.isArray(turns))
    throw new Error("malformed thread detail payload");
  const envelope = decodeEvidenceSchemaField(
    body.evidence_schema,
    body.schema_version,
  );
  const detail = body as unknown as ThreadDetail;
  const pendingTurn = decodePendingTurn(body.pending_turn);
  const decodedTurns: Turn[] = [];
  for (const raw of turns) {
    if (!isRecord(raw)) throw new Error("malformed thread detail turn");
    const turn = raw as unknown as Turn & { rejected_citations?: unknown };
    // An absent citations key is a peer predating the channel — defined
    // none, not a refusal (same posture as visual_claims).
    //
    // ``"stored-replay"`` is this seam's whole reason the mode exists:
    // a history body is stored records read back, and spec §7.1 leaves
    // pre-cutover rows "exactly as they are" — the server's
    // ``_serialize_run`` publishes a pre-E5 keyless snapshot verbatim,
    // so a strict read here would refuse a turn the server legitimately
    // served and break reload for every pre-cutover thread. The live
    // SSE frame (``api.ts``) passes ``"emission"`` and is held to the
    // full pairing, which is what keeps the two ends in agreement.
    const frame =
      "citations" in raw
        ? decodeCitationsArray(raw.citations, "stored-replay")
        : { citations: [] as Citation[], refusals: [] as EvidenceRefusal[] };
    const comparisonFrame = decodeComparisonRows(raw.comparisons);
    const historyRefusals: EvidenceRefusal[] = Array.isArray(turn.rejected_citations)
      ? turn.rejected_citations.map((entry, position) => {
          const decoded = decodeEvidenceRefusalEntry(entry, position);
          return decoded.refusal;
        })
      : [];
    const refusals = mergeEvidenceRefusals(
      historyRefusals,
      frame.refusals,
      comparisonFrame.refusals,
      envelope === "unsupported" || envelope === "malformed"
        ? [refusal(0, envelope === "unsupported" ? "unsupported_evidence_contract" : "malformed_evidence_contract")]
        : [],
    );
    const { rejected_citations: _consumed, ...rest } = turn;
    decodedTurns.push({
      ...rest,
      citations: frame.citations,
      comparisons: comparisonFrame.comparisons,
      evidence_refusals: refusals,
    });
  }
  return withPendingTurn(detail, decodedTurns, pendingTurn);
}

// ----- refusal fold (store ordering invariant) -----

/** Union refusal lists across competing stream/history updates:
 * arrival order kept, identical entries deduplicated, and NOTHING in
 * this helper touches a citations array — a refusal fold must never
 * reorder citations (plan E6 acceptance). */
export function mergeEvidenceRefusals(
  ...lists: (EvidenceRefusal[] | undefined | null)[]
): EvidenceRefusal[] {
  const seen = new Set<string>();
  const out: EvidenceRefusal[] = [];
  for (const list of lists) {
    for (const entry of list ?? []) {
      const key = `${entry.citation_index}\u0000${entry.error}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(entry);
    }
  }
  return out;
}

// ----- presentation helpers (visible named-code policy) -----

/** The indicator line beside the answer: the §6 code verbatim (it IS
 * the contract's name, and the plan's display policy says a precise
 * failure, not prose) plus the 1-based row the operator counts. */
export function evidenceRefusalRowText(refusalEntry: EvidenceRefusal): string {
  return `证据被拒绝：${refusalEntry.error}（第 ${refusalEntry.citation_index + 1} 条出处）`;
}

/** Snapshot identity line (§4.4): which view, which record — values
 * rendered to text, never re-parsed. Opening this shows the stored
 * snapshot and issues NO query; a refresh is a new query minting a new
 * evidence item, which is out of this change's scope. */
export function databaseRowIdentityLabel(snapshot: DatabaseRowSnapshot): string {
  const keyText = Object.keys(snapshot.record_key)
    .sort()
    .map((key) => `${key}=${snapshotValueText(snapshot.record_key[key])}`)
    .join(" ");
  return `来源 ${snapshot.source} · 记录 ${keyText}`;
}

/** The retained values this round returned, one ``key: text`` line per
 * field, keys sorted by name (§4.4: sorted, never nested). */
export function databaseRowFieldLines(snapshot: DatabaseRowSnapshot): string[] {
  return Object.keys(snapshot.fields)
    .sort()
    .map((key) => `${key}: ${snapshotValueText(snapshot.fields[key])}`);
}
