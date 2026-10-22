// The developer TaskPanel's span read: fetch ONE turn's trace when the
// panel is actually visible. It is a hook rather than a store slice because
// the trace is per-turn developer chrome — it must not poll or touch the
// conversation store, and an empty trace (200 + trace_state) is a normal
// result the renderer turns into the receipt fallback.
//
// WHY the read keys on the turn's settled flag: a segment's spans are only
// written when the segment CLOSES, so a read taken while the turn is still
// in flight is guaranteed to be empty. Keying the effect on (ids, enabled,
// settled) gives one read at turn start and exactly one refetch when the
// turn settles — never one per render, and never a fallback frozen for the
// life of the panel.
//
// WHY a settled, local, empty read gets one more read: clearing ``in_flight``
// and committing the segment's rows are two different writes, and the status
// frame can win that race by a few dozen milliseconds. The key never changes
// again once the turn settles, so without one bounded re-read the panel would
// keep saying "no span" for a turn that has them. It is a single retry, not a
// poll: any other outcome is final and a re-read is never itself re-read.
//
// The fetch is injectable, the decision is a plain function and the effect
// body is callable directly, so every outcome is pinned without a renderer
// (the web suite runs in a node environment with no DOM).

import { useEffect, useState } from "react";

import { getTurnSpans } from "../../api";
import type { TraceSpan, TraceState, TurnSpansResponse } from "./traceSpans";

export type TurnTraceRead =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; traceState: TraceState; spans: TraceSpan[] };

/** Everything the read depends on. ``settled`` is the turn's own
 * terminal/in-flight state: false while it is still running. */
export interface TurnTraceRequest {
  threadId: string | undefined;
  turnId: string | undefined;
  /** The panel is visible and no read was injected. */
  enabled: boolean;
  /** The turn is no longer in flight, so its spans (if any) exist. */
  settled: boolean;
}

/** The transport this hook uses; injectable so a test never hits the wire. */
export type TurnSpansRead = (
  threadId: string,
  turnId: string,
) => Promise<TurnSpansResponse>;

/** How long the write-after-close race gets before its one re-read.
 *
 * Measured on the local sink: the first send inserts its rows about 105 ms
 * after the segment closes and the read itself takes about 19 ms, so one
 * second covers the gap with room to spare. It is a bound, not a poll
 * interval: a settled key is re-read exactly once.
 */
export const TRACE_SETTLE_RETRY_MS = 1000;

/** The ONE value the read effect keys on, or null for "do not read".
 *
 * Encoding every input in one string is what makes "one read per state
 * change" observable: the effect's dependencies are this key plus the two
 * ids, so a re-render with the same facts cannot re-fire it.
 */
export function turnTraceKey(request: TurnTraceRequest): string | null {
  const { threadId, turnId, enabled, settled } = request;
  if (!enabled || !threadId || !turnId) return null;
  return `${threadId}\u0000${turnId}\u0000${settled ? "settled" : "live"}`;
}

/** One read, as the three outcomes the panel renders.
 *
 * A rejected read is NOT an error surface: the receipt fallback is the
 * non-error answer to an unreadable trace, and the operator is never shown
 * a transport toast.
 */
export async function readTurnTrace(
  read: TurnSpansRead,
  threadId: string,
  turnId: string,
): Promise<TurnTraceRead> {
  try {
    const response = await read(threadId, turnId);
    return {
      status: "ready",
      traceState: response.trace_state,
      spans: response.spans,
    };
  } catch {
    return { status: "error" };
  }
}

/** The delay before the one re-read a settled read is allowed, or null for
 * "this result is final".
 *
 * Only a settled, local, empty read can be a lie: ``local`` means the sink
 * was selected and the read worked, so an empty list is the write-after-
 * close race and nothing else. ``off`` and ``external`` say there is no
 * local waterfall to wait for, ``unreadable`` already states that the store
 * failed, a rejected read stays the receipt fallback, and a non-empty list
 * is the answer. A live read is allowed to be empty and must not wait.
 */
export function traceRetryDelayMs(
  read: TurnTraceRead,
  settled: boolean,
): number | null {
  if (!settled) return null;
  if (read.status !== "ready") return null;
  if (read.traceState !== "local") return null;
  if (read.spans.length > 0) return null;
  return TRACE_SETTLE_RETRY_MS;
}

/** One key's read: the fetch, plus at most one bounded re-read.
 *
 * The returned cancel is what the effect hands React, so an unmount or a
 * key change drops both the late result and the pending timer — a stale
 * turn can never overwrite the next turn's read. The re-read deliberately
 * does not set a loading state: the first read was a real answer, and a
 * second spinner for a maybe-empty correction would flicker more than it
 * explains.
 */
export function startTurnTraceRead(
  read: TurnSpansRead,
  threadId: string,
  turnId: string,
  settled: boolean,
  apply: (read: TurnTraceRead) => void,
): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  void readTurnTrace(read, threadId, turnId).then((result) => {
    if (cancelled) return;
    apply(result);
    const delay = traceRetryDelayMs(result, settled);
    if (delay === null) return;
    timer = setTimeout(() => {
      timer = null;
      void readTurnTrace(read, threadId, turnId).then((retried) => {
        // The first read already stated this turn's state; a failed
        // verification must not retract it into "unreadable". A second
        // empty answer is final — the re-read is never re-read.
        if (!cancelled && retried.status !== "error") apply(retried);
      });
    }, delay);
  });

  return () => {
    cancelled = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };
}

/** The read for one turn. ``enabled`` gates the request, not the render. */
export function useTurnTrace(request: TurnTraceRequest): TurnTraceRead {
  const { threadId, turnId, settled } = request;
  const key = turnTraceKey(request);
  const [read, setRead] = useState<TurnTraceRead>({ status: "idle" });

  useEffect(() => {
    if (key === null || threadId === undefined || turnId === undefined) {
      setRead({ status: "idle" });
      return;
    }
    setRead({ status: "loading" });
    return startTurnTraceRead(getTurnSpans, threadId, turnId, settled, setRead);
    // ``key`` already encodes both ids, the gate and the settled flag, so
    // the extra values are the same facts and cannot add a re-run.
  }, [key, threadId, turnId, settled]);

  return read;
}
