// search-mode-host T2: the thread page's composed render decision.
//
// This is the acceptance the pending-choice basis pins ("generic crash
// banner is hidden when the actual pending choice card is already
// available"). The gate that feeds it (``threadHasPendingChoice``) is
// unit-tested in choiceWindow.test.ts; the THREE-WAY composition —
// banner vs card vs empty copy — is what these cases pin. The web suite
// has no DOM, so the decision lives in pure code (threadViewState.ts).

import { describe, expect, it } from "vitest";
import { threadViewState, type ThreadViewState } from "./threadViewState";
import type { ChoiceWindow, ThreadDetail } from "./types";

const RUN_ID = "33333333-3333-4333-8333-333333333333";

const WINDOW: ChoiceWindow = {
  kind: "choice_window",
  title: "选择检索档",
  options: [{ id: "lexical", label: "查找", opened: true }],
};

const PENDING_TURN: NonNullable<ThreadDetail["pending_turn"]> = {
  run_id: RUN_ID,
  question: "营收多少？",
  choice_window: WINDOW,
};

/** A plain crash checkpoint with no pending turn, no resume in flight,
 * on a thread that already has turns — the pre-choice baseline. */
const CRASH_NO_CARD: ThreadViewState = {
  showCrashResumeBanner: true,
  showPendingChoiceCard: false,
  showEmptyCopy: false,
};

describe("threadViewState (composed banner / card / empty-copy decision)", () => {
  it("hides the banner, shows the card, and drops the empty copy for a pending choice", () => {
    expect(
      threadViewState({
        pendingCheckpoint: true,
        isResuming: false,
        pendingTurn: PENDING_TURN,
        turnCount: 0,
      }),
    ).toEqual({
      showCrashResumeBanner: false,
      showPendingChoiceCard: true,
      showEmptyCopy: false,
    });
  });

  it("shows the banner and no card for a plain crash checkpoint with no pending turn", () => {
    expect(
      threadViewState({
        pendingCheckpoint: true,
        isResuming: false,
        pendingTurn: null,
        turnCount: 3,
      }),
    ).toEqual(CRASH_NO_CARD);
  });

  it("isResuming still suppresses the banner", () => {
    // A live resume owns the screen (the InterruptedRunBanner mounts
    // separately); the crash banner must not show beside it.
    expect(
      threadViewState({
        pendingCheckpoint: true,
        isResuming: true,
        pendingTurn: null,
        turnCount: 3,
      }).showCrashResumeBanner,
    ).toBe(false);
  });

  it("keeps a card visible even while a resume is in flight", () => {
    expect(
      threadViewState({
        pendingCheckpoint: true,
        isResuming: true,
        pendingTurn: PENDING_TURN,
        turnCount: 0,
      }).showPendingChoiceCard,
    ).toBe(true);
  });

  it("shows nothing extra on a thread with turns and no pending turn", () => {
    expect(
      threadViewState({
        pendingCheckpoint: false,
        isResuming: false,
        pendingTurn: null,
        turnCount: 2,
      }),
    ).toEqual({
      showCrashResumeBanner: false,
      showPendingChoiceCard: false,
      showEmptyCopy: false,
    });
  });

  it("shows the empty copy on a zero-turn thread with no pending turn", () => {
    expect(
      threadViewState({
        pendingCheckpoint: false,
        isResuming: false,
        pendingTurn: null,
        turnCount: 0,
      }),
    ).toEqual({
      showCrashResumeBanner: false,
      showPendingChoiceCard: false,
      showEmptyCopy: true,
    });
  });

  it("lets the card, not the empty copy, speak for a zero-turn pending choice", () => {
    const view = threadViewState({
      pendingCheckpoint: true,
      isResuming: false,
      pendingTurn: PENDING_TURN,
      turnCount: 0,
    });
    expect(view.showPendingChoiceCard).toBe(true);
    expect(view.showEmptyCopy).toBe(false);
    expect(view.showCrashResumeBanner).toBe(false);
  });
});
