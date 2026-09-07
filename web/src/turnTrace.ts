// turn-trace-folds (#250): pure helpers that fold the SSE stream into
// the per-turn burst timeline. Extracted from the store so the burst
// contract (arrival-order sibling thinking/tool folds, auto-collapse
// at burst end) can be unit-tested without Zustand, and so send and
// resume paths share one implementation instead of drifting.

import type { ToolSummary, TraceItem, Turn } from "./types";

/** One streaming wire frame as the burst folder sees it. Only the
 * frame types that shape the trace are modeled; answer/citations/
 * comparison/status are handled by the store as before. */
export type TraceFrame =
  | { type: "thinking"; text: string }
  | { type: "tool_call"; name: string; tool_call_id: string }
  | { type: "tool_result"; summary: string; tool_call_id: string };

/** Fold one frame into the trace-item list for the current turn.
 *
 * Burst rules (spec Boundaries lines 33-35):
 * - consecutive ``thinking`` frames append to the current thinking
 *   burst; a ``tool_call`` closes that thinking burst (live=false);
 * - consecutive ``tool_call``/``tool_result`` frames belong to the
 *   current tool burst; the next ``thinking`` frame closes it.
 * The first ``answer``/terminal frame is NOT folded here — the store
 * closes any still-live burst when it handles ``status``/run end via
 * ``closeOpenBursts``.
 *
 * The list is treated immutably (a new array + copied items) so it
 * can live in Zustand state without mutation aliasing.
 */
export function foldTraceFrame(
  items: TraceItem[],
  frame: TraceFrame,
): TraceItem[] {
  const last = items.length > 0 ? items[items.length - 1] : null;
  if (frame.type === "thinking") {
    if (last !== null && last.kind === "thinking" && last.live) {
      const merged: TraceItem = { ...last, text: last.text + frame.text };
      return [...items.slice(0, -1), merged];
    }
    // A new thinking burst closes an open tool burst by arrival.
    const closed = items.map((item) =>
      item.live ? { ...item, live: false } : item,
    );
    return [...closed, { kind: "thinking", text: frame.text, live: true }];
  }
  if (frame.type === "tool_call") {
    if (last !== null && last.kind === "tools" && last.live) {
      const calls: ToolSummary[] = [
        ...last.calls,
        {
          name: frame.name,
          summary: "运行中…",
          tool_call_id: frame.tool_call_id,
        },
      ];
      return [...items.slice(0, -1), { ...last, calls }];
    }
    // A new tool burst closes an open thinking burst by arrival.
    const closed = items.map((item) =>
      item.live ? { ...item, live: false } : item,
    );
    return [
      ...closed,
      {
        kind: "tools",
        calls: [
          {
            name: frame.name,
            summary: "运行中…",
            tool_call_id: frame.tool_call_id,
          },
        ],
        live: true,
      },
    ];
  }
  // tool_result: replace the matching placeholder inside a tool
  // burst. A result for an id no ``tool_call`` frame announced is
  // dropped — there is no burst to render it in, and a phantom row
  // would confuse the timeline (the store still reconciles the call
  // via tool_summaries on GET). The burst a result lands in stays
  // live: more calls may join it.
  const changed = items.some(
    (item) =>
      item.kind === "tools" &&
      item.calls.some((call) => call.tool_call_id === frame.tool_call_id),
  );
  if (!changed) {
    return items;
  }
  return items.map((item, index) => {
    if (item.kind !== "tools") return item;
    const hasCall = item.calls.some(
      (call) => call.tool_call_id === frame.tool_call_id,
    );
    if (!hasCall) return item;
    const calls = item.calls.map((call) =>
      call.tool_call_id === frame.tool_call_id
        ? { ...call, summary: frame.summary }
        : call,
    );
    return item === items[index]
      ? { ...item, calls, live: index === items.length - 1 && item.live }
      : item;
  });
}

/** Close any still-live burst (run ended / terminal frame arrived).
 * Auto-collapse runs here — burst end, per spec line 34. */
export function closeOpenBursts(items: TraceItem[]): TraceItem[] {
  if (!items.some((item) => item.live)) return items;
  return items.map((item) => (item.live ? { ...item, live: false } : item));
}

/** The last three lines of a burst's text, for the collapsed preview
 * (spec: latest lines, not the first). Fewer lines → all lines. */
export function thinkingTailPreview(text: string, lines = 3): string[] {
  const split = text.split("\n");
  const kept: string[] = [];
  for (const line of split) {
    if (line.trim().length === 0 && kept.length === 0) continue;
    kept.push(line);
  }
  if (kept.length === 0) return [""];
  return kept.slice(-lines);
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
