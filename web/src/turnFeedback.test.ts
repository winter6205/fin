// Pins the thumbs-feedback gating contract behind TurnView's control
// (T8). Pure helpers only — no DOM — mirroring the
// streamErrorKind.ts test pattern.

import { describe, expect, it } from "vitest";

import type { Turn } from "./types";
import {
  canSubmitFeedback,
  feedbackErrorVisible,
  isFeedbackActive,
  lastTerminalTurnIndex,
  showThumbs,
} from "./turnFeedback";

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    run_id: "33333333-3333-4333-8333-333333333333",
    question: "营收多少？",
    status: "done",
    status_reason: "",
    answer: "营收 1941.4 亿美元。",
    citations: [],
    thinking_text: "",
    tool_summaries: [],
    in_flight: false,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

describe("canSubmitFeedback", () => {
  it("renders on a terminal row with a run id", () => {
    expect(canSubmitFeedback(makeTurn())).toBe(true);
  });

  it("does NOT render on an in-flight (live) row", () => {
    // The optimistic row's run_id is a client placeholder until the
    // terminal status frame; there is no persisted run to thumb yet.
    expect(canSubmitFeedback(makeTurn({ in_flight: true }))).toBe(false);
  });

  it("does NOT render on a blank run_id (defensive)", () => {
    expect(canSubmitFeedback(makeTurn({ run_id: "" }))).toBe(false);
    expect(canSubmitFeedback(makeTurn({ run_id: "   " }))).toBe(false);
  });
});

describe("isFeedbackActive", () => {
  it("marks only the recorded thumb active", () => {
    const up = makeTurn({ feedback: "up" });
    expect(isFeedbackActive(up, "up")).toBe(true);
    expect(isFeedbackActive(up, "down")).toBe(false);
  });

  it("has neither thumb active on a turn without feedback", () => {
    const none = makeTurn({ feedback: null });
    expect(isFeedbackActive(none, "up")).toBe(false);
    expect(isFeedbackActive(none, "down")).toBe(false);
    expect(isFeedbackActive(makeTurn({}), "up")).toBe(false);
  });
});

// Finding (T8 review): the store's setTurnFeedback rejects on 400/503
// and the control must surface it — the hint shows on "failure" and
// clears on the next "submit" so a stale failure never lingers.
describe("feedbackErrorVisible", () => {
  it("shows the hint when a submit fails", () => {
    expect(feedbackErrorVisible(false, "failure")).toBe(true);
    expect(feedbackErrorVisible(true, "failure")).toBe(true);
  });

  it("clears the hint when a new submit starts", () => {
    expect(feedbackErrorVisible(true, "submit")).toBe(false);
    expect(feedbackErrorVisible(false, "submit")).toBe(false);
  });
});

// T3 (the tracked change): thumbs chrome only on the LAST terminal turn of a
// thread that is not in_flight. Older turns show nothing, so the
// control can't sit on every historical row. Pure helpers again —
// no DOM — with ``lastTerminalTurnIndex`` split out so the "which
// row is last-terminal" rule is testable on its own.

function makeTurns(...overrides: Partial<Turn>[]): Turn[] {
  return overrides.map((o, i) =>
    makeTurn({
      run_id: `${33333333 + i}-3333-4333-8333-333333333333`,
      ...o,
    }),
  );
}

describe("lastTerminalTurnIndex", () => {
  it("points at the final row when it is terminal", () => {
    const turns = makeTurns({ in_flight: false }, { in_flight: false });
    expect(lastTerminalTurnIndex(turns)).toBe(1);
  });

  it("skips the live last row and finds the prior terminal one", () => {
    const turns = makeTurns({ in_flight: false }, { in_flight: true });
    expect(lastTerminalTurnIndex(turns)).toBe(0);
  });

  it("is -1 when no row is terminal", () => {
    expect(lastTerminalTurnIndex(makeTurns({ in_flight: true }))).toBe(-1);
    expect(lastTerminalTurnIndex([])).toBe(-1);
  });
});

describe("showThumbs", () => {
  it("shows thumbs on the last terminal turn", () => {
    const turns = makeTurns({ in_flight: false }, { in_flight: false });
    expect(showThumbs(1, turns, false)).toBe(true);
  });

  it("does NOT show thumbs on an older terminal turn (two-turn thread)", () => {
    const turns = makeTurns({ in_flight: false }, { in_flight: false });
    expect(showThumbs(0, turns, false)).toBe(false);
  });

  it("does NOT show thumbs when the thread is in_flight (thread-level)", () => {
    // Thread-level in_flight covers the gap the per-turn flag
    // misses: a thread mid-run where the last row may not yet
    // carry ``in_flight`` — no thumbs until the run ends.
    const turns = makeTurns({ in_flight: false });
    expect(showThumbs(0, turns, true)).toBe(false);
  });

  it("does NOT show thumbs on a turn missing run_id (persist gate)", () => {
    const turns = makeTurns({ run_id: "   " });
    expect(showThumbs(0, turns, false)).toBe(false);
  });

  it("does NOT show thumbs on the live optimistic row", () => {
    const turns = makeTurns({ in_flight: false }, { in_flight: true });
    expect(showThumbs(1, turns, false)).toBe(false);
  });
});
