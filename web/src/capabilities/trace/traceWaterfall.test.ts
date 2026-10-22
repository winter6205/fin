import { describe, expect, it } from "vitest";

import type { TraceSpan } from "./traceSpans";
import { formatDuration, traceWaterfall } from "./traceWaterfall";

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    trace_id: "trace-1",
    span_id: "span-1",
    parent_span_id: null,
    name: "span",
    start_time_ms: 1_000,
    duration_ms: 10,
    attributes: {},
    ...overrides,
  };
}

describe("traceWaterfall", () => {
  it("places each bar by offset from the window start and scales by duration", () => {
    const rows = traceWaterfall([
      span({ span_id: "a", name: "a", start_time_ms: 1_000, duration_ms: 100 }),
      span({ span_id: "b", name: "b", start_time_ms: 1_050, duration_ms: 50 }),
      span({ span_id: "c", name: "c", start_time_ms: 1_100, duration_ms: 100 }),
    ]);

    expect(rows.map((row) => row.span.name)).toEqual(["a", "b", "c"]);
    expect(rows[0].leftPercent).toBe(0);
    expect(rows[0].widthPercent).toBe(50);
    expect(rows[1].leftPercent).toBe(25);
    expect(rows[1].widthPercent).toBe(25);
    expect(rows[2].leftPercent).toBe(50);
    expect(rows[2].widthPercent).toBe(50);
  });

  it("orders concurrent spans by start, then longest first", () => {
    const rows = traceWaterfall([
      span({ name: "later", start_time_ms: 1_020, duration_ms: 80 }),
      span({ name: "short", start_time_ms: 1_000, duration_ms: 20 }),
      span({ name: "long", start_time_ms: 1_000, duration_ms: 100 }),
    ]);

    expect(rows.map((row) => row.span.name)).toEqual(["long", "short", "later"]);
  });

  it("keeps zero-duration spans at zero width without NaN", () => {
    const rows = traceWaterfall([
      span({ name: "instant", start_time_ms: 5_000, duration_ms: 0 }),
      span({ name: "other", start_time_ms: 5_000, duration_ms: 0 }),
    ]);

    for (const row of rows) {
      expect(row.leftPercent).toBe(0);
      expect(row.widthPercent).toBe(0);
      expect(Number.isNaN(row.leftPercent)).toBe(false);
      expect(Number.isNaN(row.widthPercent)).toBe(false);
    }
  });

  it("ignores the absolute epoch and only uses the turn window", () => {
    const near = traceWaterfall([
      span({ start_time_ms: 1_700_000_000_000, duration_ms: 20 }),
      span({ start_time_ms: 1_700_000_000_020, duration_ms: 20 }),
    ]);
    const distant = traceWaterfall([
      span({ start_time_ms: 10, duration_ms: 20 }),
      span({ start_time_ms: 30, duration_ms: 20 }),
    ]);

    expect(near.map((row) => [row.leftPercent, row.widthPercent])).toEqual(
      distant.map((row) => [row.leftPercent, row.widthPercent]),
    );
  });

  it("returns no rows for an empty trace", () => {
    expect(traceWaterfall([])).toEqual([]);
  });
});

describe("formatDuration", () => {
  it("renders a readable unit at each magnitude", () => {
    expect(formatDuration(12.5)).toBe("12.5 ms");
    expect(formatDuration(1_500)).toBe("1.50 s");
    expect(formatDuration(0.5)).toBe("500.0 µs");
    expect(formatDuration(0.0005)).toBe("500 ns");
    expect(formatDuration(0)).toBe("0 ns");
  });
});
