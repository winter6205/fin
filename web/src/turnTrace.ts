// turn-trace-folds: pure helpers that fold the SSE stream into
// the per-turn burst timeline. Extracted from the store so the burst
// contract (arrival-order sibling thinking/tool folds, auto-collapse
// at burst end) can be unit-tested without Zustand, and so send and
// resume paths share one implementation instead of drifting.

import type { ToolSummary, TraceItem, Turn } from "./types";

/** One streaming wire frame as the burst folder sees it. Only the
 * frame types that shape the trace are modeled; citations/
 * comparison/status are handled by the store as before. The
 * ``answer`` frame is the burst-end signal for the trace: spec
 * Boundaries line 33 — "the next ``thinking`` or first ``answer``
 * closes that tool burst (auto-collapse)". */
export type TraceFrame =
  | { type: "thinking"; text: string }
  | { type: "tool_call"; name: string; tool_call_id: string }
  | { type: "tool_result"; summary: string; tool_call_id: string }
  | { type: "answer" };

/** The live-tool-row placeholder. Single source: the store, the
 * folder, and the UI all show the same running copy. */
export const RUNNING_SUMMARY = "运行中…";

function runningRow(name: string, toolCallId: string): ToolSummary {
  return { name, summary: RUNNING_SUMMARY, tool_call_id: toolCallId };
}

/** Optimistic send chrome: same thinking-fold slot, no burst body. */
export function thinkingPendingItems(): TraceItem[] {
  return [{ kind: "thinking", text: "", live: true }];
}

/** First tool_call / answer / burst-end must not leave an empty header. */
function dropEmptyThinking(items: TraceItem[]): TraceItem[] {
  if (
    !items.some((item) => item.kind === "thinking" && item.text.length === 0)
  ) {
    return items;
  }
  return items.filter(
    (item) => item.kind !== "thinking" || item.text.length > 0,
  );
}

/** Close every still-live burst, returning the list unchanged when
 * nothing is live (keeps the common all-closed path allocation-free). */
function closeLive(items: TraceItem[]): TraceItem[] {
  const closed = items.some((item) => item.live)
    ? items.map((item) => (item.live ? { ...item, live: false } : item))
    : items;
  return dropEmptyThinking(closed);
}

/** The tool burst holding ``toolCallId``, else null. */
function findToolBurst(
  items: TraceItem[],
  toolCallId: string,
): { item: Extract<TraceItem, { kind: "tools" }>; index: number } | null {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (
      item.kind === "tools" &&
      item.calls.some((call) => call.tool_call_id === toolCallId)
    ) {
      return { item, index };
    }
  }
  return null;
}

function appendToolCall(
  items: TraceItem[],
  frame: TraceFrame & { type: "tool_call" },
): TraceItem[] {
  const last = items.length > 0 ? items[items.length - 1] : null;
  if (last !== null && last.kind === "tools" && last.live) {
    return [
      ...items.slice(0, -1),
      {
        ...last,
        calls: [...last.calls, runningRow(frame.name, frame.tool_call_id)],
      },
    ];
  }
  // A new tool burst closes an open thinking burst by arrival.
  return [
    ...closeLive(items),
    {
      kind: "tools",
      calls: [runningRow(frame.name, frame.tool_call_id)],
      live: true,
    },
  ];
}

function applyToolResult(
  items: TraceItem[],
  frame: TraceFrame & { type: "tool_result" },
): TraceItem[] {
  const found = findToolBurst(items, frame.tool_call_id);
  if (found === null) {
    // A result for an id no ``tool_call`` frame announced is
    // dropped — there is no burst to render it in, and a phantom
    // row would confuse the timeline (the store still reconciles
    // the call via tool_summaries on GET).
    return items;
  }
  const { item, index } = found;
  const calls = item.calls.map((call) =>
    call.tool_call_id === frame.tool_call_id
      ? { ...call, summary: frame.summary }
      : call,
  );
  const updated: TraceItem = { ...item, calls };
  return [...items.slice(0, index), updated, ...items.slice(index + 1)];
}

/** Fold one frame into the trace-item list for the current turn.
 *
 * Burst rules (spec Boundaries lines 33-35):
 * - consecutive ``thinking`` frames append to the current thinking
 *   burst; a ``tool_call`` closes that thinking burst (live=false);
 * - consecutive ``tool_call``/``tool_result`` frames belong to the
 *   current tool burst; the next ``thinking`` or first ``answer``
 *   frame closes it.
 * The terminal ``status`` frame is NOT folded here — the store closes
 * any still-live burst at run end via ``closeOpenBursts``.
 *
 * The list is treated immutably (a new array + copied items) so it
 * can live in Zustand state without mutation aliasing.
 */
export function foldTraceFrame(
  items: TraceItem[],
  frame: TraceFrame,
): TraceItem[] {
  if (frame.type === "thinking") {
    const last = items.length > 0 ? items[items.length - 1] : null;
    if (last !== null && last.kind === "thinking" && last.live) {
      const merged: TraceItem = { ...last, text: last.text + frame.text };
      return [...items.slice(0, -1), merged];
    }
    // A new thinking burst closes an open tool burst by arrival.
    return [
      ...closeLive(items),
      { kind: "thinking", text: frame.text, live: true },
    ];
  }
  if (frame.type === "tool_call") {
    return appendToolCall(items, frame);
  }
  if (frame.type === "answer") {
    // First answer closes any still-live burst (spec line 33): the
    // operator is now reading prose, not watching a burst stream.
    return closeLive(items);
  }
  return applyToolResult(items, frame);
}

/** Close any still-live burst (run ended / terminal frame arrived).
 * Auto-collapse runs here — burst end, per spec line 34. */
export function closeOpenBursts(items: TraceItem[]): TraceItem[] {
  return closeLive(items);
}

/** Visible burst prose for a thinking fold. Open (live or expanded)
 * → full burst text (latest lines included). Collapsed → header-only,
 * no three-line tail preview. */
export function thinkingFoldVisibleText(
  open: boolean,
  text: string,
): string | null {
  return open ? text : null;
}

/** Open state for one burst fold (spec line 34): a manual toggle
 * wins over the auto state (live) UNTIL the burst's liveness flips —
 * auto-collapse at burst end always runs, but it cannot fight an
 * operator's mid-burst open, and a user-opened completed burst stays
 * open across re-renders. Extracted pure so the transition matrix is
 * unit-testable without a DOM.
 *
 * (manualOpen, live) -> open; ``liveChanged`` says the burst just
 * transitioned between live and ended (the caller then clears the
 * override).
 */
export function foldOpenState(
  manualOpen: boolean | null,
  live: boolean,
  liveChanged: boolean,
): boolean {
  return liveChanged ? live : (manualOpen ?? live);
}

/** Build the GET-restored trace for one persisted turn. The server
 * keeps no burst history (one thinking blob + one tool_summaries
 * list per run), so restore is one collapsed thinking fold (when
 * thinking streamed) + one collapsed tool fold (when calls exist) —
 * explicitly allowed by spec line 41. */
export function traceItemsFromTurn(turn: Turn): TraceItem[] {
  const items: TraceItem[] = [];
  const thinking = turn.thinking_text ?? "";
  if (thinking.length > 0) {
    items.push({ kind: "thinking", text: thinking, live: false });
  }
  if ((turn.tool_summaries?.length ?? 0) > 0) {
    items.push({
      kind: "tools",
      calls: turn.tool_summaries ?? [],
      live: false,
    });
  }
  return items;
}
