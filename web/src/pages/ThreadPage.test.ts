// Mid-flight stream error taxonomy renders a "原因: <label>" line
// on refuse rows (T11). Mirrors the closed seven-kind enum the
// Python side emits; the SPA never accepts a kind outside the
// enum — anything else renders as no label.
//
// We pin the label via two pure helpers exported from
// ``streamErrorKind.ts`` (``shouldRenderStreamErrorKind`` +
// ``streamErrorKindRowText``) because this project's vitest
// setup runs without a DOM testing library. The JSX in
// ``ThreadPage.TurnView`` is a 1-line use of those helpers — a
// refactor that drops the prefix or the colon surfaces here.

import { describe, expect, it } from "vitest";

import {
  shouldRenderStreamErrorKind,
  streamErrorKindRowText,
  STREAM_ERROR_LABEL,
  describeStreamErrorKind,
  parseStreamErrorKind,
} from "../streamErrorKind";
import type { Turn } from "../types";

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    run_id: "22222222-2222-4222-8222-222222222222",
    question: "营收多少？",
    status: "refuse",
    status_reason: "stream raised mid-flight",
    status_classified: "auth",
    answer: "",
    citations: [],
    thinking_text: "",
    tool_summaries: [],
    assistant_message: "鉴权失败：服务返回 401。",
    in_flight: false,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

describe("STREAM_ERROR_LABEL", () => {
  it("has a distinct Chinese label for every closed-enum kind", () => {
    const labels = Object.values(STREAM_ERROR_LABEL);
    // Seven kinds, seven distinct labels — duplicates would let
    // a 401 and a 429 render indistinguishably.
    expect(labels.length).toBe(7);
    expect(new Set(labels).size).toBe(labels.length);
    for (const label of labels) {
      expect(label.trim().length).toBeGreaterThan(0);
    }
  });

  it("auth label mentions auth so a 401 row reads as auth", () => {
    expect(STREAM_ERROR_LABEL.auth).toBe("鉴权失败");
  });

  it("rate_limit label mentions rate-limit so a 429 row reads as rate-limit", () => {
    expect(STREAM_ERROR_LABEL.rate_limit).toBe("服务限流");
  });
});

describe("describeStreamErrorKind", () => {
  it("returns the label for any closed-enum kind", () => {
    for (const [kind, label] of Object.entries(STREAM_ERROR_LABEL)) {
      expect(describeStreamErrorKind(kind)).toBe(label);
    }
  });

  it("returns empty string for missing / unknown kinds (defensive)", () => {
    expect(describeStreamErrorKind(undefined)).toBe("");
    expect(describeStreamErrorKind("")).toBe("");
    expect(describeStreamErrorKind("nonsense")).toBe("");
  });
});

describe("parseStreamErrorKind", () => {
  it("accepts every closed-enum kind", () => {
    for (const kind of Object.keys(STREAM_ERROR_LABEL)) {
      expect(parseStreamErrorKind(kind)).toBe(kind);
    }
  });

  it("rejects anything outside the closed enum", () => {
    expect(parseStreamErrorKind(undefined)).toBeUndefined();
    expect(parseStreamErrorKind(null)).toBeUndefined();
    expect(parseStreamErrorKind(42)).toBeUndefined();
    expect(parseStreamErrorKind("")).toBeUndefined();
    expect(parseStreamErrorKind("not_a_kind")).toBeUndefined();
  });
});

describe("shouldRenderStreamErrorKind", () => {
  it("renders the label on a refuse row with a closed-enum kind", () => {
    const turn = makeTurn({ status: "refuse", status_classified: "auth" });
    expect(shouldRenderStreamErrorKind(turn)).toBe(true);
  });

  it("does NOT render on a done row even if classification is set", () => {
    const turn = makeTurn({ status: "done", status_classified: "auth" });
    expect(shouldRenderStreamErrorKind(turn)).toBe(false);
  });

  it("does NOT render on a step_cap row (gate-driven, not stream)", () => {
    const turn = makeTurn({ status: "step_cap", status_classified: "auth" });
    expect(shouldRenderStreamErrorKind(turn)).toBe(false);
  });

  it("does NOT render on a refuse row with empty classification", () => {
    const turn = makeTurn({ status: "refuse", status_classified: "" });
    expect(shouldRenderStreamErrorKind(turn)).toBe(false);
  });

  it("does NOT render on a refuse row with an unknown kind (defensive)", () => {
    const turn = makeTurn({
      status: "refuse",
      // ``as`` cast here is only for the test fixture; the
      // parser would never accept this from the wire.
      status_classified: "not_a_kind" as Turn["status_classified"],
    });
    expect(shouldRenderStreamErrorKind(turn)).toBe(false);
  });
});

describe("streamErrorKindRowText", () => {
  it("renders the auth label with the '原因：' prefix", () => {
    const turn = makeTurn({ status_classified: "auth" });
    expect(streamErrorKindRowText(turn)).toBe("原因：鉴权失败");
  });

  it("renders every closed-enum kind with the same prefix", () => {
    const expected: Array<[Turn["status_classified"], string]> = [
      ["auth", "原因：鉴权失败"],
      ["network", "原因：网络错误"],
      ["timeout", "原因：调用超时"],
      ["rate_limit", "原因：服务限流"],
      ["not_found", "原因：接口/模型不存在"],
      ["server", "原因：服务内部错误"],
      ["unknown", "原因：未知错误"],
    ];
    for (const [kind, want] of expected) {
      const turn = makeTurn({ status_classified: kind });
      expect(streamErrorKindRowText(turn)).toBe(want);
    }
  });

  it("does NOT prefix an empty string when kind is missing", () => {
    // The conditional in the JSX should hide the row entirely;
    // this helper returns a label-less string for the no-label
    // path so the conditional logic stays single-sourced.
    const turn = makeTurn({ status_classified: "" });
    expect(streamErrorKindRowText(turn)).toBe("原因：");
  });
});
