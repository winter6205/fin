import { describe, expect, it } from "vitest";
import {
  EMPTY_LIVE_ANSWER,
  foldLiveAnswer,
  foldSendResultAnswer,
  mergeReconciledTurn,
  type AnswerFrame,
  type LiveAnswerState,
} from "./turnStream";
import type { Turn } from "./types";

/** Drive a frame sequence through the live fold, keeping every
 * intermediate visible string so a test can assert what the operator
 * saw between frames — not just where the bubble landed. */
function visibleTrace(frames: AnswerFrame[]): string[] {
  let state: LiveAnswerState = EMPTY_LIVE_ANSWER;
  const seen: string[] = [];
  for (const frame of frames) {
    state = foldLiveAnswer(state, frame);
    seen.push(state.visible);
  }
  return seen;
}

const PRE_BOUNCE = "根据 2024 年报，营收约为 100 亿元。";
const RETRY_PROSE = "根据已核实的 10-K 原文，营收为 391.0 亿美元。";

describe("foldLiveAnswer", () => {
  it("appends fragments while the run streams", () => {
    const state = foldLiveAnswer(
      { visible: "hel", retry: null },
      { text: "lo", statusSeen: false },
    );
    expect(state.visible).toBe("hello");
  });

  it("keeps the streamed draft after status instead of replacing", () => {
    const state = foldLiveAnswer(
      { visible: "streamed draft", retry: null },
      { text: "authoritative full", statusSeen: true },
    );
    expect(state.visible).toBe("streamed draft");
  });

  it("uses the authoritative frame when no draft accumulated", () => {
    const state = foldLiveAnswer(EMPTY_LIVE_ANSWER, {
      text: "final only",
      statusSeen: true,
    });
    expect(state.visible).toBe("final only");
  });

  it("clears streamed draft on post-status empty answer (refuse/step_cap)", () => {
    const state = foldLiveAnswer(
      { visible: "refused model prose", retry: null },
      { text: "", statusSeen: true },
    );
    expect(state.visible).toBe("");
  });

  it("clears the stale draft on the terminal empty reset flush", () => {
    const state = foldLiveAnswer(
      { visible: "failed attempt", retry: null },
      { text: "", statusSeen: true, reset: true },
    );
    expect(state.visible).toBe("");
    expect(state.retry).toBeNull();
  });

  it("never blanks the bubble on a mid-stream reset delta", () => {
    const state = foldLiveAnswer(
      { visible: PRE_BOUNCE, retry: null },
      { text: "根", statusSeen: false, reset: true },
    );
    expect(state.visible).toBe(PRE_BOUNCE);
    expect(state.retry).toBe("根");
  });

  it("never blanks the bubble on an empty mid-stream reset frame", () => {
    const state = foldLiveAnswer(
      { visible: PRE_BOUNCE, retry: null },
      { text: "", statusSeen: false, reset: true },
    );
    expect(state.visible).toBe(PRE_BOUNCE);
    expect(state.retry).toBe("");
  });

  it("converges onto the retry prose without ever shrinking", () => {
    const chunks = ["根据已核实的 ", "10-K 原文，", "营收为 391.0 亿美元。"];
    const frames: AnswerFrame[] = [
      { text: PRE_BOUNCE, statusSeen: false },
      { text: chunks[0], statusSeen: false, reset: true },
      { text: chunks[1], statusSeen: false },
      { text: chunks[2], statusSeen: false },
    ];

    const seen = visibleTrace(frames);

    for (let i = 1; i < seen.length; i += 1) {
      expect(seen[i].length).toBeGreaterThanOrEqual(seen[i - 1].length);
    }
    expect(seen.every((v) => v.length > 0)).toBe(true);
    expect(seen[seen.length - 1]).toBe(RETRY_PROSE);
  });

  it("keeps the converged retry draft after status (no repop)", () => {
    let state: LiveAnswerState = { visible: PRE_BOUNCE, retry: null };
    state = foldLiveAnswer(state, {
      text: RETRY_PROSE,
      statusSeen: false,
      reset: true,
    });
    state = foldLiveAnswer(state, { text: RETRY_PROSE, statusSeen: true });

    expect(state.visible).toBe(RETRY_PROSE);
    expect(state.retry).toBeNull();
  });

  it("replaces a stale draft the retry never caught up to at status", () => {
    // The retry answer is shorter than the bounced draft, so the live
    // bubble still shows pre-bounce prose when the run ends: the
    // authoritative terminal frame is the convergence point.
    let state: LiveAnswerState = { visible: PRE_BOUNCE, retry: null };
    state = foldLiveAnswer(state, {
      text: "无法核实。",
      statusSeen: false,
      reset: true,
    });
    expect(state.visible).toBe(PRE_BOUNCE);

    state = foldLiveAnswer(state, { text: "无法核实。", statusSeen: true });
    expect(state.visible).toBe("无法核实。");
  });

  it("restarts retry accumulation on a second bounce", () => {
    let state: LiveAnswerState = { visible: PRE_BOUNCE, retry: null };
    state = foldLiveAnswer(state, {
      text: "first retry",
      statusSeen: false,
      reset: true,
    });
    state = foldLiveAnswer(state, {
      text: "second retry",
      statusSeen: false,
      reset: true,
    });
    expect(state.retry).toBe("second retry");
    expect(state.visible).toBe(PRE_BOUNCE);
  });
});

describe("foldSendResultAnswer", () => {
  it("appends while streaming without reset", () => {
    expect(foldSendResultAnswer("hel", "lo", { statusSeen: false })).toBe(
      "hello",
    );
  });

  it("replaces with non-empty text on reset", () => {
    expect(
      foldSendResultAnswer("failed attempt", "retry chunk", {
        statusSeen: false,
        reset: true,
      }),
    ).toBe("retry chunk");
  });

  it("clears on reset with empty text", () => {
    expect(
      foldSendResultAnswer("failed attempt", "", {
        statusSeen: false,
        reset: true,
      }),
    ).toBe("");
  });

  it("replaces after status (authoritative SendResult frame)", () => {
    expect(
      foldSendResultAnswer("draft", "full answer", { statusSeen: true }),
    ).toBe("full answer");
  });
});

describe("mergeReconciledTurn", () => {
  const base: Turn = {
    run_id: "00000000-0000-4000-8000-000000000001",
    question: "q",
    status: "done",
    answer: "persisted",
    citations: [],
    created_at: "2026-01-01T00:00:00.000Z",
  };

  it("prefers streamed prose over GET replace on done turns only", () => {
    const streamed = { ...base, answer: "live draft" };
    expect(mergeReconciledTurn(streamed, base).answer).toBe("live draft");
  });

  it("falls back to persisted prose when the stream was empty", () => {
    const streamed = { ...base, answer: "" };
    expect(mergeReconciledTurn(streamed, base).answer).toBe("persisted");
  });

  it("does not restore streamed prose onto refuse/step_cap rows", () => {
    const streamed = { ...base, answer: "refused draft" };
    const persisted = { ...base, status: "refuse" as const, answer: "" };
    expect(mergeReconciledTurn(streamed, persisted).answer).toBe("");
  });
});
