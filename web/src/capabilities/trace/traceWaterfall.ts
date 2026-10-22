// The waterfall's pure geometry: one row per span, a horizontal bar whose
// left edge is the span's offset from the earliest start and whose width is
// its duration. Kept out of the component so each percentage can be pinned
// without a DOM (the vitest environment is Node-only).
//
// A span is placed against the WHOLE turn window: the earliest start to the
// latest end. Scaling each bar against that window keeps concurrent spans
// comparable across rows, which is the point of a waterfall.

import type { TraceSpan } from "./traceSpans";

export interface WaterfallRow {
  span: TraceSpan;
  /** Offset from the turn window's earliest start, in percent (0..100). */
  leftPercent: number;
  /** Span duration as a percentage of the turn window (0..100). */
  widthPercent: number;
  /** Readable duration for the row label. */
  durationLabel: string;
}

/** Four decimals keep the inline style stable without visible rounding. */
function percent(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}

/** A duration in nanoseconds or milliseconds, rendered in a readable unit. */
export function formatDuration(durationMs: number): string {
  if (durationMs >= 1000) {
    const seconds = durationMs / 1000;
    return `${seconds.toFixed(2)} s`;
  }
  if (durationMs >= 1) {
    return `${durationMs.toFixed(1)} ms`;
  }
  const micros = durationMs * 1000;
  if (micros >= 1) {
    return `${micros.toFixed(1)} µs`;
  }
  return `${(micros * 1000).toFixed(0)} ns`;
}

/** Rows ordered like the wire: earliest start first, then longest first. */
export function traceWaterfall(spans: readonly TraceSpan[]): WaterfallRow[] {
  if (spans.length === 0) return [];
  const starts = spans.map((span) => span.start_time_ms);
  const ends = spans.map((span) => span.start_time_ms + span.duration_ms);
  const windowStart = Math.min(...starts);
  const windowEnd = Math.max(...ends);
  const window = windowEnd - windowStart;
  // A single instant-sized span still has a valid (zero-width) bar; the
  // denominator stays non-zero so the arithmetic never yields NaN.
  const denominator = window > 0 ? window : 1;

  return [...spans]
    .sort(
      (left, right) =>
        left.start_time_ms - right.start_time_ms ||
        right.duration_ms - left.duration_ms ||
        left.name.localeCompare(right.name),
    )
    .map((span) => ({
      span,
      leftPercent: percent(
        ((span.start_time_ms - windowStart) / denominator) * 100,
      ),
      widthPercent: percent((span.duration_ms / denominator) * 100),
      durationLabel: formatDuration(span.duration_ms),
    }));
}
