// The thread page's composed render decision.
//
// WHY this is a module and not inline JSX: the web suite runs in a node
// environment with no DOM and does not collect ``.tsx`` files, so the
// three-way decision between the crash-resume banner, the reloaded
// pending-choice card and the zero-turn empty copy can only be pinned
// by a test if it lives in pure code. Same split as ``choiceWindow.ts``
// / ``threadEntry.ts``: plain data in, plain booleans out.
//
// The load-bearing rule (spec search-mode-host T2): a reloaded paused
// choice is NOT a crash. Its card speaks for the suspended run, so the
// crash banner must not show above it and the zero-turn empty copy must
// yield to it.

import type { ThreadDetail } from "./types";

export interface ThreadViewState {
  /** The "previous run was interrupted" Resume banner. A paused choice
   * is not a crash, so an available card suppresses this. */
  showCrashResumeBanner: boolean;
  /** The reloaded paused choice card (the thread-level mount). */
  showPendingChoiceCard: boolean;
  /** The zero-turn "ask a question" copy. A card speaks for the empty
   * thread, so it yields while a choice is pending. */
  showEmptyCopy: boolean;
}

export function threadViewState(input: {
  /** Server's ``pending_checkpoint`` probe: a resumable checkpoint
   * exists and the lock is not held. */
  pendingCheckpoint: boolean;
  /** A resume stream for THIS thread is live. */
  isResuming: boolean;
  /** The host's ``ThreadDetail.pending_turn`` projection. Present only
   * while a choice interrupt is paused; it carries the paused run's
   * own ``choice_window``, which is why its presence IS "a card can
   * render" (the paused run has no ``turns[]`` row). */
  pendingTurn: ThreadDetail["pending_turn"];
  /** ``activeThread.turns.length``. */
  turnCount: number;
}): ThreadViewState {
  const cardAvailable = input.pendingTurn != null;
  return {
    showCrashResumeBanner:
      input.pendingCheckpoint && !input.isResuming && !cardAvailable,
    showPendingChoiceCard: cardAvailable,
    showEmptyCopy: input.turnCount === 0 && !cardAvailable,
  };
}
