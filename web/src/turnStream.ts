// Pure helpers for threading SSE deltas into a Turn row. Extracted
// so the streaming contract (T1 #33) can be unit-tested without
// pulling in Zustand.

import type { Turn } from "./types";

/** One ``answer`` SSE frame as the live fold sees it. */
export interface AnswerFrame {
  text: string;
  reset?: boolean;
  statusSeen: boolean;
}

/** Live-bubble answer state.
 *
 * ``visible`` is the prose on screen. ``retry`` is the answer being
 * re-authored after a hidden-bounce reset armed on the wire; it is
 * ``null`` on runs that never bounced. Keeping the two apart is what
 * lets the retry accumulate BEHIND the stale draft: replacing the
 * bubble with the bounce's first fragment blanks a paragraph down to
 * a few characters mid-stream (R3).
 */
export interface LiveAnswerState {
  visible: string;
  retry: string | null;
}

export const EMPTY_LIVE_ANSWER: LiveAnswerState = { visible: "", retry: null };

/** Fold one ``answer`` SSE frame into the live bubble.
 *
 * Invariant across a bounce: the visible prose never shrinks between
 * the reset frame and the terminal one. The retry text takes over the
 * bubble the moment it is at least as long as the draft it replaces,
 * so the operator sees a convergence, not a retract.
 *
 * The terminal frame is the one convergence point that may shorten the
 * bubble: an empty post-``status`` frame (with or without ``reset``)
 * clears a refuse / step_cap draft per SC67, and a run whose retry
 * prose never caught up to the bounced draft takes the authoritative
 * text rather than keeping prose the citation gate rejected.
 */
export function foldLiveAnswer(
  state: LiveAnswerState,
  frame: AnswerFrame,
): LiveAnswerState {
  if (frame.statusSeen) {
    if (frame.reset || frame.text.length === 0) {
      return { visible: frame.text, retry: null };
    }
    const staleDraft = state.retry !== null && state.retry !== state.visible;
    if (staleDraft) return { visible: frame.text, retry: null };
    // Done run: keep the streamed draft to avoid retract/repop flicker.
    return {
      visible: state.visible.length > 0 ? state.visible : frame.text,
      retry: null,
    };
  }
  if (frame.reset) return converge(state.visible, frame.text);
  if (state.retry !== null) {
    return converge(state.visible, state.retry + frame.text);
  }
  return { visible: state.visible + frame.text, retry: null };
}

/** Show the retry prose once it is at least as long as the draft it
 * supersedes; until then the draft stays put so no frame is shorter
 * than the one before it. */
function converge(visible: string, retry: string): LiveAnswerState {
  return { visible: retry.length >= visible.length ? retry : visible, retry };
}

/** Fold one ``answer`` SSE into ``SendResult.answerText`` (non-live).
 *
 * Unlike ``foldLiveAnswer`` (which trades wire fidelity for a bubble
 * that never retracts), post-status and ``reset`` frames replace so
 * the returned result matches the wire exactly.
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
 * Wired into ``openThread``'s GET reconcile (store.ts): the persisted
 * row is folded into the streamed one row-by-row rather than replacing
 * the turn table.
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
