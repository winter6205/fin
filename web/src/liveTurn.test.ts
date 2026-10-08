// search-mode-host T2 fix: ONE constructor for the live / in-flight
// turn row. ``sendQuestion`` (optimistic send) and ``pickChoice``
// (materializing a reloaded ``ThreadDetail.pending_turn``) must build
// the SAME shape, so the two construction sites cannot drift again.

import { describe, expect, it } from "vitest";
import { makeLiveTurn } from "./liveTurn";
import type { ChoiceWindow } from "./types";

const WINDOW: ChoiceWindow = {
  kind: "choice_window",
  title: "选择检索档",
  options: [
    { id: "lexical", label: "查找", opened: true },
    { id: "graph", label: "知识问答", opened: true },
  ],
};

// The full optimistic field set both paths must carry.
const FULL_FIELD_SET = [
  "run_id",
  "question",
  "status",
  "status_reason",
  "answer",
  "citations",
  "evidence_refusals",
  "thinking_text",
  "thinking_duration_ms",
  "tool_summaries",
  "assistant_message",
  "status_classified",
  "comparisons",
  "visual_claims",
  "in_flight",
  "trace_items",
  "created_at",
].sort();

describe("makeLiveTurn", () => {
  it("builds the full live/in-flight field set", () => {
    const turn = makeLiveTurn({ runId: "run-1", question: "营收多少？" });

    expect(turn).toMatchObject({
      run_id: "run-1",
      question: "营收多少？",
      status: "done",
      status_reason: "",
      answer: "",
      citations: [],
      evidence_refusals: [],
      thinking_text: "",
      thinking_duration_ms: 0,
      tool_summaries: [],
      assistant_message: "",
      status_classified: "",
      comparisons: [],
      visual_claims: [],
      in_flight: true,
    });
    // Pending-thinking chrome: the same empty live slot sendQuestion shows.
    expect(turn.trace_items).toEqual([
      { kind: "thinking", text: "", live: true },
    ]);
    // No window unless the pick path supplies one.
    expect("choice_window" in turn).toBe(false);
    expect(typeof turn.created_at).toBe("string");
  });

  it("produces the same field set with and without a choice window", () => {
    const plain = makeLiveTurn({ runId: "run-1", question: "q" });
    const picked = makeLiveTurn({
      runId: "run-1",
      question: "q",
      choice: WINDOW,
    });

    expect(picked.choice_window).toEqual(WINDOW);
    // ``choice_window`` is the ONLY extra key; every other field matches
    // the live send row, so the two construction sites cannot drift.
    const plainKeys = Object.keys(plain).sort();
    const pickedKeys = Object.keys(picked)
      .filter((k) => k !== "choice_window")
      .sort();
    expect(pickedKeys).toEqual(plainKeys);
    expect(plainKeys).toEqual(FULL_FIELD_SET);
  });
});
