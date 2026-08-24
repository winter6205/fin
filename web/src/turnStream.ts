// Pure helpers for threading SSE deltas into a Turn row. Extracted
// so the streaming contract (T1 #33) can be unit-tested without
// pulling in Zustand.

import type { Turn } from "./types";

/** Apply one ``answer`` SSE fragment to the turn's accumulated text.
 *
 * While the run streams, fragments append. After ``status``, the wire
 * may emit one authoritative full-text frame — but when the live view
 * already accumulated the prose, keeping the draft avoids a visible
 * retract-and-repop flicker (the draft IS the final answer).
 */
export function mergeAnswerDelta(
  currentAnswer: string,
  delta: string,
  opts: { statusSeen: boolean; reset?: boolean },
): string {
  if (opts.reset) return delta;
  if (opts.statusSeen) {
    // Authoritative post-status empty frame clears refuse/step_cap draft
    // (SC67: ordinary-person copy lives in assistant_message, not answer).
    if (delta.length === 0) return "";
    // Done run: keep streamed draft to avoid retract/repop flicker.
    return currentAnswer.length > 0 ? currentAnswer : delta;
  }
  return currentAnswer + delta;
}

/** Fold one ``answer`` SSE into ``SendResult.answerText`` (non-live).
 *
 * Unlike ``mergeAnswerDelta`` (live bubble keeps draft after status),
 * post-status frames replace so the returned result matches the wire.
 * ``reset`` always replaces with ``text`` (empty = clear).
 */
export function foldSendResultAnswer(
  currentAnswer: string,
  text: string,
  opts: { statusSeen: boolean; reset?: boolean },
): string {
  if (opts.reset) return text;
  if (opts.statusSeen) return text;
  return currentAnswer + text;
}

/** After GET reconcile, prefer the streamed row's visible prose when
 * the operator already watched it arrive token-by-token. Persisted
 * fields (citations with quotes, ids) still come from the server row.
 * Tested helper for the openThread GET-reconcile path (not wired yet).
 */
export function mergeReconciledTurn(streamed: Turn, persisted: Turn): Turn {
  const keepStreamedAnswer =
    persisted.status === "done" && streamed.answer.length > 0;
  return {
    ...persisted,
    answer: keepStreamedAnswer ? streamed.answer : persisted.answer,
    thinking_text:
      streamed.thinking_text && streamed.thinking_text.length > 0
        ? streamed.thinking_text
        : persisted.thinking_text,
    tool_summaries:
      (streamed.tool_summaries?.length ?? 0) > 0
        ? streamed.tool_summaries
        : persisted.tool_summaries,
  };
}
