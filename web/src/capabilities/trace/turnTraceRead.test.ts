// The developer trace read: what fetches, what each outcome is, that a
// settled turn triggers exactly ONE refetch, and that an empty local settled
// read earns exactly ONE bounded re-read — the segment's rows can land a few
// milliseconds after the status frame that cleared ``in_flight``.
//
// The web suite runs without a DOM, so the effect is pinned through the
// seams it is built from: ``turnTraceKey`` is the effect's dependency value
// (and ``useTurnTrace`` keys its single effect on it), ``readTurnTrace`` is
// the state machine, and ``startTurnTraceRead`` is the effect body whose
// returned cancel models cleanup on unmount or key change. A render
// sequence is simulated as the sequence of request snapshots the panel
// produces over one turn's life — one key per distinct dependency set,
// which is exactly one effect run.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  readTurnTrace,
  startTurnTraceRead,
  traceRetryDelayMs,
  TRACE_SETTLE_RETRY_MS,
  turnTraceKey,
  type TurnSpansRead,
  type TurnTraceRead,
  type TurnTraceRequest,
} from "./turnTraceRead";
import type { TraceSpan, TraceState, TurnSpansResponse } from "./traceSpans";

const THREAD_ID = "thread-1";
const TURN_ID = "turn-1";

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    trace_id: "a".repeat(32),
    span_id: "s1",
    parent_span_id: null,
    name: "retrieve_tool",
    start_time_ms: 1_000,
    duration_ms: 100,
    attributes: {},
    ...overrides,
  };
}

function response(overrides: Partial<TurnSpansResponse> = {}): TurnSpansResponse {
  return {
    thread_id: THREAD_ID,
    turn_id: TURN_ID,
    trace_state: "local",
    spans: [],
    ...overrides,
  };
}

function request(overrides: Partial<TurnTraceRequest> = {}): TurnTraceRequest {
  return {
    threadId: THREAD_ID,
    turnId: TURN_ID,
    enabled: true,
    settled: false,
    ...overrides,
  };
}

/** The dependency values one turn's render sequence produces. */
function keysFor(requests: readonly TurnTraceRequest[]): Array<string | null> {
  return requests.map(turnTraceKey);
}

describe("turnTraceKey", () => {
  it("reads on mount, while the turn is still in flight", () => {
    // The first read happens at turn start; it is allowed to come back
    // empty because the segment has not closed yet.
    expect(turnTraceKey(request())).not.toBeNull();
  });

  it("does not read when the panel is disabled or an id is absent", () => {
    expect(turnTraceKey(request({ enabled: false }))).toBeNull();
    expect(turnTraceKey(request({ threadId: undefined }))).toBeNull();
    expect(turnTraceKey(request({ turnId: undefined }))).toBeNull();
    expect(turnTraceKey(request({ threadId: "" }))).toBeNull();
    expect(turnTraceKey(request({ turnId: "" }))).toBeNull();
  });

  it("distinguishes one turn from the next and a live turn from a settled one", () => {
    const live = turnTraceKey(request());
    const settled = turnTraceKey(request({ settled: true }));
    const otherTurn = turnTraceKey(request({ turnId: "turn-2" }));
    const otherThread = turnTraceKey(request({ threadId: "thread-2" }));

    expect(new Set([live, settled, otherTurn, otherThread]).size).toBe(4);
  });

  it("refetches exactly once when the turn settles", () => {
    // One turn's life: mount in flight, two re-renders while streaming,
    // settle, then two re-renders of the finished turn. Every distinct key
    // is one effect run, so this sequence must produce exactly two reads.
    const keys = keysFor([
      request({ settled: false }),
      request({ settled: false }),
      request({ settled: false }),
      request({ settled: true }),
      request({ settled: true }),
      request({ settled: true }),
    ]);
    const effectRuns = new Set(keys.filter((key) => key !== null));

    expect(effectRuns.size).toBe(2);
    expect(keys[keys.length - 1]).toBe(
      turnTraceKey(request({ settled: true })),
    );
    expect(keys[0]).not.toBe(keys[keys.length - 1]);
  });
});

describe("readTurnTrace", () => {
  it("reads one turn and reports spans as ready", async () => {
    const rows = response({ spans: [span(), span({ span_id: "s2" })] });
    const read = vi.fn(async () => rows);

    const result = await readTurnTrace(read as TurnSpansRead, THREAD_ID, TURN_ID);

    expect(read).toHaveBeenCalledWith(THREAD_ID, TURN_ID);
    expect(read).toHaveBeenCalledTimes(1);
    expect(result).toEqual({
      status: "ready",
      traceState: "local",
      spans: rows.spans,
    });
  });

  it("keeps an empty trace ready and carries the state that explains it", async () => {
    const read = vi.fn(async () => response({ trace_state: "off" }));

    const result = await readTurnTrace(read as TurnSpansRead, THREAD_ID, TURN_ID);

    expect(result).toEqual({ status: "ready", traceState: "off", spans: [] });
  });

  it("reports an unreadable store as a ready state, not an error", async () => {
    const read = vi.fn(async () =>
      response({ trace_state: "unreadable" }),
    );

    const result = await readTurnTrace(read as TurnSpansRead, THREAD_ID, TURN_ID);

    expect(result).toEqual({
      status: "ready",
      traceState: "unreadable",
      spans: [],
    });
  });

  it("turns a rejected read into the non-error fallback branch", async () => {
    const read = vi.fn(async () => {
      throw new Error("HTTP 500");
    });

    const result = await readTurnTrace(read as TurnSpansRead, THREAD_ID, TURN_ID);

    expect(result).toEqual({ status: "error" });
  });
});

const OTHER_TURN_ID = "turn-2";

/** The effect's state setter: record every read that actually applies. */
function start(
  read: TurnSpansRead,
  request: TurnTraceRequest,
  applied: TurnTraceRead[],
): () => void {
  const { threadId, turnId } = request;
  if (threadId === undefined || turnId === undefined) {
    throw new Error("a request without ids has no effect to start");
  }
  return startTurnTraceRead(read, threadId, turnId, request.settled, (result) =>
    applied.push(result),
  );
}

/** Let the already-resolved read settle without moving the fake clock. */
async function flushReads(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function readsFor(read: ReturnType<typeof vi.fn>, turnId: string): number {
  return read.mock.calls.filter((call) => call[1] === turnId).length;
}

describe("traceRetryDelayMs", () => {
  const localEmpty: TurnTraceRead = {
    status: "ready",
    traceState: "local",
    spans: [],
  };
  const EMPTY_STATES: readonly TraceState[] = ["off", "external", "unreadable"];

  it("waits once for a settled, local, empty read", () => {
    expect(traceRetryDelayMs(localEmpty, true)).toBe(TRACE_SETTLE_RETRY_MS);
  });

  it("never waits on a live read, even an empty local one", () => {
    expect(traceRetryDelayMs(localEmpty, false)).toBeNull();
  });

  it("never waits on a state that already explains itself", () => {
    for (const traceState of EMPTY_STATES) {
      expect(
        traceRetryDelayMs({ status: "ready", traceState, spans: [] }, true),
      ).toBeNull();
    }
  });

  it("never waits on spans that already arrived or on a failed read", () => {
    expect(
      traceRetryDelayMs(
        { status: "ready", traceState: "local", spans: [span()] },
        true,
      ),
    ).toBeNull();
    expect(traceRetryDelayMs({ status: "error" }, true)).toBeNull();
    expect(traceRetryDelayMs({ status: "loading" }, true)).toBeNull();
    expect(traceRetryDelayMs({ status: "idle" }, true)).toBeNull();
  });
});

// The re-read is the fix for the write-after-close race: the terminal status
// frame can clear ``in_flight`` before the segment's rows are committed. It
// is one bounded wait per settled key, never a poll, and cleanup must take
// the timer with it.
describe("the one settled re-read", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("re-reads exactly once after the bound when the settled read is local and empty", async () => {
    const populated = response({ spans: [span()] });
    const read = vi
      .fn()
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(populated);
    const applied: TurnTraceRead[] = [];

    start(read as TurnSpansRead, request({ settled: true }), applied);
    await flushReads();

    expect(read).toHaveBeenCalledTimes(1);
    expect(applied).toEqual([
      { status: "ready", traceState: "local", spans: [] },
    ]);

    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS - 1);
    expect(read).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(2);
    expect(applied).toEqual([
      { status: "ready", traceState: "local", spans: [] },
      { status: "ready", traceState: "local", spans: populated.spans },
    ]);

    // The re-read is the end of the chain, not the first tick of a poll.
    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS * 5);
    expect(read).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("stops after the one re-read even when it is still empty", async () => {
    const read = vi.fn(async () => response());
    const applied: TurnTraceRead[] = [];

    start(read as TurnSpansRead, request({ settled: true }), applied);
    await flushReads();
    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS);
    expect(read).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS * 5);
    expect(read).toHaveBeenCalledTimes(2);
    expect(applied).toHaveLength(2);
  });

  it.each([
    ["off", response({ trace_state: "off" })],
    ["external", response({ trace_state: "external" })],
    ["unreadable", response({ trace_state: "unreadable" })],
    ["non-empty local", response({ spans: [span()] })],
  ])("does not re-read a settled %s result", async (_case, rows) => {
    const read = vi.fn(async () => rows);
    const applied: TurnTraceRead[] = [];

    start(read as TurnSpansRead, request({ settled: true }), applied);
    await flushReads();
    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS * 2);

    expect(read).toHaveBeenCalledTimes(1);
    expect(applied).toHaveLength(1);
  });

  it("does not re-read when the settled read itself failed", async () => {
    const read = vi.fn(async () => {
      throw new Error("HTTP 500");
    });
    const applied: TurnTraceRead[] = [];

    start(read as TurnSpansRead, request({ settled: true }), applied);
    await flushReads();
    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS * 2);

    expect(read).toHaveBeenCalledTimes(1);
    expect(applied).toEqual([{ status: "error" }]);
  });

  it("keeps the first settled read when the re-read fails", async () => {
    const read = vi
      .fn()
      .mockResolvedValueOnce(response())
      .mockRejectedValueOnce(new Error("HTTP 500"));
    const applied: TurnTraceRead[] = [];

    start(read as TurnSpansRead, request({ settled: true }), applied);
    await flushReads();
    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS);

    expect(read).toHaveBeenCalledTimes(2);
    expect(applied).toEqual([
      { status: "ready", traceState: "local", spans: [] },
    ]);
  });

  it("drops the pending re-read when the effect is torn down", async () => {
    const read = vi.fn(async () => response());
    const applied: TurnTraceRead[] = [];

    const cancel = start(
      read as TurnSpansRead,
      request({ settled: true }),
      applied,
    );
    await flushReads();
    expect(vi.getTimerCount()).toBe(1);

    cancel();
    expect(vi.getTimerCount()).toBe(0);

    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS * 2);
    expect(read).toHaveBeenCalledTimes(1);
    expect(applied).toHaveLength(1);
  });

  it("cancels the old turn's timer when the key changes", async () => {
    const readsByTurn = new Map<string, number>();
    const read = vi.fn(async (_threadId: string, turnId: string) => {
      const count = (readsByTurn.get(turnId) ?? 0) + 1;
      readsByTurn.set(turnId, count);
      return count === 1
        ? response({ turn_id: turnId })
        : response({ turn_id: turnId, spans: [span()] });
    });
    const firstApplied: TurnTraceRead[] = [];
    const nextApplied: TurnTraceRead[] = [];

    // A key change is exactly the effect cleanup followed by the next key's
    // effect run; the old timer must not survive the cleanup.
    const cancelFirst = start(
      read as TurnSpansRead,
      request({ settled: true }),
      firstApplied,
    );
    await flushReads();
    expect(vi.getTimerCount()).toBe(1);

    cancelFirst();
    expect(vi.getTimerCount()).toBe(0);
    start(
      read as TurnSpansRead,
      request({ turnId: OTHER_TURN_ID, settled: true }),
      nextApplied,
    );
    await flushReads();

    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS);
    expect(read).toHaveBeenCalledTimes(3);
    expect(readsFor(read, TURN_ID)).toBe(1);
    expect(readsFor(read, OTHER_TURN_ID)).toBe(2);
    expect(firstApplied).toHaveLength(1);
    expect(nextApplied).toHaveLength(2);
  });

  it("never schedules the re-read for a live turn", async () => {
    const read = vi.fn(async () => response());
    const applied: TurnTraceRead[] = [];

    start(read as TurnSpansRead, request(), applied);
    await flushReads();

    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(TRACE_SETTLE_RETRY_MS * 2);
    expect(read).toHaveBeenCalledTimes(1);
    expect(applied).toEqual([
      { status: "ready", traceState: "local", spans: [] },
    ]);
  });
});
