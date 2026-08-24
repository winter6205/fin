import { describe, expect, it } from "vitest";
import {
  foldSendResultAnswer,
  mergeAnswerDelta,
  mergeReconciledTurn,
} from "./turnStream";
import type { Turn } from "./types";

describe("mergeAnswerDelta", () => {
  it("appends fragments while the run streams", () => {
    expect(mergeAnswerDelta("hel", "lo", { statusSeen: false })).toBe("hello");
  });

  it("keeps the streamed draft after status instead of replacing", () => {
    expect(
      mergeAnswerDelta("streamed draft", "authoritative full", {
        statusSeen: true,
      }),
    ).toBe("streamed draft");
  });

  it("uses the authoritative frame when no draft accumulated", () => {
    expect(mergeAnswerDelta("", "final only", { statusSeen: true })).toBe(
      "final only",
    );
  });

  it("clears on bounce retry reset with empty text", () => {
    expect(
      mergeAnswerDelta("failed attempt", "", { statusSeen: true, reset: true }),
    ).toBe("");
  });

  it("replaces with non-empty text on bounce retry reset", () => {
    expect(
      mergeAnswerDelta("failed attempt", "retry chunk", {
        statusSeen: false,
        reset: true,
      }),
    ).toBe("retry chunk");
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

  it("clears streamed draft on post-status empty answer (refuse/step_cap)", () => {
    expect(
      mergeAnswerDelta("refused model prose", "", { statusSeen: true }),
    ).toBe("");
  });

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
