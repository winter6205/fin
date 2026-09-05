// Pins the thumbs-feedback gating contract behind TurnView's control
// (T8). Pure helpers only — no DOM — mirroring the
// streamErrorKind.ts test pattern.

import { describe, expect, it } from "vitest";

import type { Turn } from "./types";
import {
  canSubmitFeedback,
  feedbackErrorVisible,
  isFeedbackActive,
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
