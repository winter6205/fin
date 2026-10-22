import { describe, expect, it } from "vitest";

import { parseTurnSpansResponse, traceIdText } from "./traceSpans";

const THREAD_ID = "thread-1";
const TURN_ID = "turn-1";

/** One row exactly as ``trace_span_store.span_row`` publishes it: ISO
 * instants, milliseconds duration, decoded allowlisted attributes. */
function wireRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "1",
    trace_id: "0".repeat(31) + "1",
    span_id: "0".repeat(15) + "1",
    parent_span_id: null,
    thread_id: THREAD_ID,
    turn_id: TURN_ID,
    name: "retrieve_tool",
    start_time: "2026-01-01T00:00:00+00:00",
    end_time: "2026-01-01T00:00:00.012500+00:00",
    duration_ms: 12.5,
    attributes: { "fin.thread_id": THREAD_ID, "fin.turn_id": TURN_ID },
    created_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function response(rows: unknown[]): Record<string, unknown> {
  return {
    thread_id: THREAD_ID,
    turn_id: TURN_ID,
    trace_state: "local",
    spans: rows,
  };
}

describe("parseTurnSpansResponse", () => {
  it("decodes the store's rows into the waterfall's millisecond shape", () => {
    const parsed = parseTurnSpansResponse(response([wireRow()]));

    expect(parsed?.thread_id).toBe(THREAD_ID);
    expect(parsed?.turn_id).toBe(TURN_ID);
    expect(parsed?.trace_state).toBe("local");
    expect(parsed?.spans).toHaveLength(1);
    expect(parsed?.spans[0]).toMatchObject({
      trace_id: "0".repeat(31) + "1",
      span_id: "0".repeat(15) + "1",
      parent_span_id: null,
      name: "retrieve_tool",
      duration_ms: 12.5,
      attributes: { "fin.thread_id": THREAD_ID, "fin.turn_id": TURN_ID },
    });
    expect(parsed?.spans[0].start_time_ms).toBe(
      Date.parse("2026-01-01T00:00:00+00:00"),
    );
  });

  it("accepts a numeric epoch instant in milliseconds and nanoseconds", () => {
    const millis = parseTurnSpansResponse(
      response([
        wireRow({
          start_time: 1_767_225_600_000,
          end_time: "2026-01-01T00:00:00.012500+00:00",
        }),
      ]),
    );
    expect(millis?.spans[0].start_time_ms).toBe(1_767_225_600_000);

    const nanos = parseTurnSpansResponse(
      response([
        wireRow({
          start_time: 1_767_225_600_000_000_000,
          end_time: "2026-01-01T00:00:00.012500+00:00",
        }),
      ]),
    );
    expect(nanos?.spans[0].start_time_ms).toBe(1_767_225_600_000);
  });

  it("keeps a span without attributes and a parent id when present", () => {
    const parsed = parseTurnSpansResponse(
      response([
        wireRow({
          parent_span_id: "a".repeat(16),
          attributes: undefined,
        }),
      ]),
    );

    expect(parsed?.spans[0].parent_span_id).toBe("a".repeat(16));
    expect(parsed?.spans[0].attributes).toEqual({});
  });

  it("refuses the whole list when one row is malformed", () => {
    expect(
      parseTurnSpansResponse(response([wireRow(), wireRow({ duration_ms: "soon" })])),
    ).toBeNull();
    expect(
      parseTurnSpansResponse(response([wireRow({ name: "" })])),
    ).toBeNull();
    expect(
      parseTurnSpansResponse(response([wireRow({ start_time: "not a time" })])),
    ).toBeNull();
  });

  it("refuses a response whose envelope does not hold", () => {
    expect(parseTurnSpansResponse(null)).toBeNull();
    expect(parseTurnSpansResponse([])).toBeNull();
    expect(
      parseTurnSpansResponse({ ...response([]), trace_state: "on" }),
    ).toBeNull();
    expect(
      parseTurnSpansResponse({ ...response([]), trace_state: undefined }),
    ).toBeNull();
    expect(
      parseTurnSpansResponse({ ...response([]), spans: undefined }),
    ).toBeNull();
  });

  it("keeps every state the host can report", () => {
    for (const state of ["off", "local", "external", "unreadable"]) {
      const parsed = parseTurnSpansResponse({
        ...response([]),
        trace_state: state,
      });
      expect(parsed?.trace_state, state).toBe(state);
    }
  });

  it("accepts an empty trace as a normal response", () => {
    const parsed = parseTurnSpansResponse({
      ...response([]),
      trace_state: "off",
    });
    expect(parsed).toEqual({
      thread_id: THREAD_ID,
      turn_id: TURN_ID,
      trace_state: "off",
      spans: [],
    });
  });
});

describe("traceIdText", () => {
  it("prints the one trace id the spans carry", () => {
    const parsed = parseTurnSpansResponse(response([wireRow()]));
    expect(traceIdText(parsed?.spans ?? [])).toBe("0".repeat(31) + "1");
  });

  it("prints 未记录 for an empty trace instead of inventing an id", () => {
    expect(traceIdText([])).toBe("未记录");
  });
});
