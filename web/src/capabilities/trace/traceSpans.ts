// The wire decoder for one turn's trace spans (GET
// /api/threads/{thread}/turns/{turn}/spans).
//
// The host's store publishes rows with ISO-8601 instants, a duration in
// milliseconds and the allowlisted attributes. This module normalizes that
// wire shape into the ONE representation the waterfall needs — an offset
// from the first span and a duration, both in milliseconds — and refuses a
// malformed list whole. A view that silently dropped a broken row would
// draw a trace that no longer matches the store.
//
// Why a decoder rather than a blind cast: the web socket is ``unknown`` at
// runtime (strict tsc is compile-time only), and a missing instant would
// become ``NaN`` in the bar geometry. Keeping the decision pure also lets
// it be pinned without a DOM.

export interface TraceSpan {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  /** Milliseconds since the Unix epoch, normalized for positioning. */
  start_time_ms: number;
  duration_ms: number;
  attributes: Record<string, unknown>;
}

/** What the host can say about one turn's trace.
 *
 * It is stated by the route rather than inferred from an empty span list,
 * because an empty list alone cannot tell "tracing is off" from "tracing
 * is on but the store could not be read":
 *
 * - ``off`` — no exporter is selected, so nothing is written;
 * - ``local`` — the local sink is selected and the read worked;
 * - ``external`` — console/otlp is selected, so spans leave the process;
 * - ``unreadable`` — the local sink is selected and the read failed.
 */
export type TraceState = "off" | "local" | "external" | "unreadable";

export const TRACE_STATES: readonly TraceState[] = [
  "off",
  "local",
  "external",
  "unreadable",
];

export interface TurnSpansResponse {
  thread_id: string;
  turn_id: string;
  /** Why the span list looks the way it does — never inferred locally. */
  trace_state: TraceState;
  spans: TraceSpan[];
}

/** A non-empty string, else null. */
function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** A finite, non-negative number from a number or a numeric string. */
function nonNegativeNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    return value;
  }
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  }
  return null;
}

/** One wire instant as epoch milliseconds.
 *
 * The store writes ISO-8601 strings. A numeric value is also accepted so a
 * peer that hands back epoch milliseconds (or nanoseconds, which are
 * distinguishable by magnitude) keeps working: the waterfall only needs a
 * consistent offset, never the absolute value.
 */
function instantMs(value: unknown): number | null {
  if (typeof value === "string") {
    const asNumber = nonNegativeNumber(value);
    if (asNumber !== null) return instantMs(asNumber);
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  const number = nonNegativeNumber(value);
  if (number === null) return null;
  // Unix nanoseconds exceed 1e15; epoch milliseconds sit near 1e12.
  return number > 1e15 ? number / 1e6 : number;
}

function attributesOf(value: unknown): Record<string, unknown> | null {
  if (value === undefined) return {};
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return { ...(value as Record<string, unknown>) };
}

interface SpanIdentity {
  traceId: string;
  spanId: string;
  name: string;
  parentSpanId: string | null;
}

interface SpanTiming {
  start_time_ms: number;
  duration_ms: number;
}

function spanIdentity(row: Record<string, unknown>): SpanIdentity | null {
  const traceId = nonEmptyString(row.trace_id);
  const spanId = nonEmptyString(row.span_id);
  const name = nonEmptyString(row.name);
  if (traceId === null || spanId === null || name === null) return null;
  const parent = row.parent_span_id;
  return {
    traceId,
    spanId,
    name,
    parentSpanId: typeof parent === "string" ? parent : null,
  };
}

function spanTiming(row: Record<string, unknown>): SpanTiming | null {
  const startMs = instantMs(row.start_time);
  const duration = nonNegativeNumber(row.duration_ms);
  if (startMs === null || duration === null) return null;
  return { start_time_ms: startMs, duration_ms: duration };
}

function parseSpan(entry: unknown): TraceSpan | null {
  if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
    return null;
  }
  const row = entry as Record<string, unknown>;
  const identity = spanIdentity(row);
  const timing = spanTiming(row);
  const attributes = attributesOf(row.attributes);
  if (identity === null || timing === null || attributes === null) return null;
  return {
    trace_id: identity.traceId,
    span_id: identity.spanId,
    parent_span_id: identity.parentSpanId,
    name: identity.name,
    ...timing,
    attributes,
  };
}

function parseSpans(value: unknown): TraceSpan[] | null {
  if (!Array.isArray(value)) return null;
  const spans: TraceSpan[] = [];
  for (const entry of value) {
    const parsed = parseSpan(entry);
    if (parsed === null) return null;
    spans.push(parsed);
  }
  return spans;
}

/** One of the four states, else null: an unknown token is a payload this
 * build cannot describe, not a state it guesses at. */
function traceStateOf(value: unknown): TraceState | null {
  return TRACE_STATES.find((state) => state === value) ?? null;
}

/** Decode one span response, or null when its shape does not hold. */
export function parseTurnSpansResponse(value: unknown): TurnSpansResponse | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const body = value as Record<string, unknown>;
  const threadId = nonEmptyString(body.thread_id);
  const turnId = nonEmptyString(body.turn_id);
  if (threadId === null || turnId === null) return null;
  const traceState = traceStateOf(body.trace_state);
  if (traceState === null) return null;
  const spans = parseSpans(body.spans);
  if (spans === null) return null;
  return {
    thread_id: threadId,
    turn_id: turnId,
    trace_state: traceState,
    spans,
  };
}

/** The trace id(s) a turn's spans carry, or 未记录 when there are none.
 *
 * A span always carries its trace id; with no rows there is no id the
 * server published, and inventing one would claim a correlation the store
 * never made.
 */
export function traceIdText(spans: readonly TraceSpan[]): string {
  const ids: string[] = [];
  for (const span of spans) {
    if (!ids.includes(span.trace_id)) ids.push(span.trace_id);
  }
  return ids.length > 0 ? ids.join("、") : "未记录";
}
