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
