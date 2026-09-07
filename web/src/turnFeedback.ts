// Pure helpers for the T8 thumbs-feedback control on a turn row.
// Extracted so the gating / active-state contract can be unit-tested
// without a DOM (this project's vitest setup runs without a DOM
// testing library) — the same pattern as streamErrorKind.ts.

import type { Turn } from "./types";

export type FeedbackValue = "up" | "down";

/** Whether the thumbs control should render on this turn.
 *
 * Only terminal rows get thumbs: a live (in-flight) turn has no
 * persisted run to point feedback at yet — its optimistic run_id is
 * a client placeholder until the terminal ``status`` frame. A blank
 * run_id (defensive) hides the control too.
 */
export function canSubmitFeedback(turn: Turn): boolean {
  return turn.in_flight !== true && turn.run_id.trim().length > 0;
}

/** Whether the given thumb is the one currently recorded on the turn.
 *
 * Drives the pressed state of each button; a turn with no feedback
 * (null / undefined) has neither pressed.
 */
export function isFeedbackActive(
  turn: Turn,
  value: FeedbackValue,
): boolean {
  return turn.feedback === value;
}

// Finding (T8 review): the store's ``setTurnFeedback`` rejects so the
// caller can surface a failure — this tiny state machine is that
// surface for the inline "提交失败，请重试" hint next to the thumbs.

export type FeedbackSubmitEvent = "submit" | "failure";

/** Whether the submit-failure hint should be visible.
 *
 * "failure" shows the hint; "submit" clears it — a new attempt is
 * under way, so a stale failure must not linger next to the thumbs.
 */
export function feedbackErrorVisible(
  _current: boolean,
  event: FeedbackSubmitEvent,
): boolean {
  return event === "failure";
}

// T3 (#251): thumbs chrome moves to the LAST terminal turn of a
// thread that is not in_flight — older turns show nothing. Split
// into two helpers so the "which row is last-terminal" rule and
// the render gate stay independently testable (no DOM, house
// style above).

/** Index of the last terminal (not in-flight) turn, or -1 when
 * every row is live or the list is empty.
 *
 * Scans from the tail because a thread's live optimistic row can
 * only ever be the latest row — the first non-live row from the
 * end is the last terminal turn.
 */
export function lastTerminalTurnIndex(turns: readonly Turn[]): number {
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].in_flight !== true) return i;
  }
  return -1;
}

/** Whether the thumbs control should render on turn ``index``.
 *
 * Composes the persist gate (``canSubmitFeedback``: not in-flight
 * + a persisted run_id) with the T3 chrome rule: this turn is the
 * last terminal one AND the thread as a whole is not in_flight
 * (the thread-level flag covers a mid-run window where the last
 * row does not yet carry its own ``in_flight``).
 */
export function showThumbs(
  index: number,
  turns: readonly Turn[],
  threadInFlight: boolean,
): boolean {
  if (threadInFlight) return false;
  if (!canSubmitFeedback(turns[index])) return false;
  return lastTerminalTurnIndex(turns) === index;
}
