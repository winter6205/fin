// The developer search-mode receipt (spec search-mode-host items 5-7,
// ADR 0104): the receipt states HOW the decision was made — its
// provenance, its confidence and the judge's own diagnostics — and NEVER
// WHICH internal retrieval implementation ran.
//
// Two properties are therefore pinned here, and they pull in opposite
// directions:
//
//  1. the parser must ACCEPT the ``fallback`` tier and keep a BOUNDED
//     allowlist of the host's diagnostic signals, because a fallback's
//     top-level confidence is POLICY uncertainty (0.0) and the judge's
//     own label probability lives in the signals — dropping them would
//     present the fallback as the judge being sure; and
//  2. the RENDERED receipt must carry no internal 档 name and no raw
//     mode token, in the text or in any attribute.
//
// A malformed receipt is REJECTED, never read as a valid fallback.

import { describe, expect, it } from "vitest";

import {
  describeSearchModeReceipt,
  parseSearchModeDecision,
} from "./searchModeReceipt";
import type { SearchModeDecisionTier } from "../../types";

/** The internal retrieval names and their operator labels. None of them
 * may reach a rendered receipt (docs/CONTEXT.md 检索档: "rendered UI
 * omits internal mode names and raw tokens remain in machine
 * diagnostics"). */
const INTERNAL_MODE_TOKENS = [
  "lexical",
  "graph",
  "global",
  "sql",
  "查找",
  "知识问答",
  "综述",
  "真数据库",
] as const;

function expectNoInternalMode(text: string): void {
  for (const token of INTERNAL_MODE_TOKENS) {
    expect(text, `the receipt leaks the internal name "${token}"`).not.toContain(
      token,
    );
  }
}

describe("the developer search-mode receipt", () => {
  it("states the decision provenance, never the internal retrieval name", () => {
    const decision = parseSearchModeDecision({
      search_mode: "global",
      decision_tier: "jev",
      confidence: 0.95,
    });

    // The RAW token stays in the machine receipt — it is the audit
    // record — but it is not what the receipt shows.
    expect(decision).toEqual({
      search_mode: "global",
      decision_tier: "jev",
      confidence: 0.95,
    });
    if (!decision) throw new Error("valid search-mode decision was rejected");

    const text = describeSearchModeReceipt(decision);
    expect(text).toBe("检索档决策：判定命中 · 决策置信度：0.95");
    expectNoInternalMode(text);
  });

  it("keeps the no-decision and prior-history cases distinct", () => {
    expect(describeSearchModeReceipt({})).toBe("本轮尚未进入检索档决策。");
    expect(describeSearchModeReceipt(null)).toBe("历史记录未保存检索档决策。");
  });

  it("rejects malformed and unknown mode data at the SSE boundary", () => {
    expect(parseSearchModeDecision(null)).toBeUndefined();
    expect(parseSearchModeDecision({ search_mode: "local" })).toBeUndefined();
    expect(parseSearchModeDecision({ search_mode: 4 })).toBeUndefined();
  });
});

describe("the fallback tier is a receipt, not a malformed one", () => {
  it("accepts the tier and keeps the bounded diagnostic signals", () => {
    const decision = parseSearchModeDecision({
      search_mode: "graph",
      confidence: 0,
      decision_tier: "fallback",
      signals: {
        mode_jev_exit: "low_confidence",
        mode_jev_label: "graph",
        mode_jev_confidence: 0.44,
        mode_jev_error_type: "JevChoiceError",
      },
    });

    expect(decision).toEqual({
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: {
        mode_jev_exit: "low_confidence",
        mode_jev_label: "graph",
        mode_jev_confidence: 0.44,
        mode_jev_error_type: "JevChoiceError",
      },
    });
  });

  it("separates the fallback's policy uncertainty from the judge's own probability", () => {
    // The distinction the correction exists for: the top-level 0.0 says
    // "the policy did not know", the judge's 0.44 says "the judge was
    // 44% sure of its own label". They are shown under different labels
    // so the fallback's 0.0 is never read as the judge's probability.
    const decision = parseSearchModeDecision({
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: {
        mode_jev_exit: "low_confidence",
        mode_jev_confidence: 0.44,
      },
    });
    if (!decision) throw new Error("the fallback receipt was rejected");

    const text = describeSearchModeReceipt(decision);
    expect(text).toBe(
      "检索档决策：自动回退 · 决策置信度：0.00 · 判定置信度：0.44 · 判定结果：low_confidence",
    );
    expectNoInternalMode(text);
  });

  it("shows the judge's error type when the judge itself failed", () => {
    const decision = parseSearchModeDecision({
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: {
        mode_jev_exit: "unexpected_error",
        mode_jev_error_type: "JevChoiceError",
      },
    });
    if (!decision) throw new Error("the fallback receipt was rejected");

    const text = describeSearchModeReceipt(decision);
    expect(text).toBe(
      "检索档决策：自动回退 · 决策置信度：0.00 · 判定结果：unexpected_error · 错误类型：JevChoiceError",
    );
    expectNoInternalMode(text);
  });

  it("ignores signal keys outside the bounded allowlist", () => {
    // The host records more than the four named signals (a named mode, a
    // pin, an inherited mode). Those stay in the host's machine record;
    // the browser keeps only the bounded set.
    const decision = parseSearchModeDecision({
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: {
        mode_jev_exit: "transport",
        requested_mode: "graph",
        inherited_mode: "sql",
        named_failure: "tier_unavailable",
        nested: { deep: 1 },
      },
    });

    expect(decision).toEqual({
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: { mode_jev_exit: "transport" },
    });
  });

  it("rejects a receipt whose known signal carries the wrong type", () => {
    // A malformed receipt is REJECTED rather than treated as a valid
    // fallback: a signal read as the wrong type is a broken audit record.
    const base = { search_mode: "graph", decision_tier: "fallback", confidence: 0 };
    expect(
      parseSearchModeDecision({ ...base, signals: { mode_jev_exit: 7 } }),
    ).toBeUndefined();
    expect(
      parseSearchModeDecision({ ...base, signals: { mode_jev_label: 4 } }),
    ).toBeUndefined();
    expect(
      parseSearchModeDecision({
        ...base,
        signals: { mode_jev_error_type: false },
      }),
    ).toBeUndefined();
    expect(
      parseSearchModeDecision({
        ...base,
        signals: { mode_jev_confidence: "0.44" },
      }),
    ).toBeUndefined();
  });

  it("rejects a judge confidence outside the unit range", () => {
    const base = { search_mode: "graph", decision_tier: "fallback", confidence: 0 };
    for (const bad of [1.5, -0.1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(
        parseSearchModeDecision({ ...base, signals: { mode_jev_confidence: bad } }),
      ).toBeUndefined();
    }
    for (const good of [0, 0.44, 1]) {
      expect(
        parseSearchModeDecision({ ...base, signals: { mode_jev_confidence: good } }),
      ).toEqual({ ...base, signals: { mode_jev_confidence: good } });
    }
  });

  it("rejects a signals bag that is not a record", () => {
    const base = { search_mode: "graph", decision_tier: "fallback", confidence: 0 };
    expect(parseSearchModeDecision({ ...base, signals: "low_confidence" })).toBeUndefined();
    expect(parseSearchModeDecision({ ...base, signals: ["low_confidence"] })).toBeUndefined();
    expect(parseSearchModeDecision({ ...base, signals: null })).toBeUndefined();
  });
});

describe("the legacy receipt tiers stay readable", () => {
  it("parses the tiers older receipts carry", () => {
    expect(parseSearchModeDecision({ decision_tier: "clarify", confidence: 0.4 })).toEqual({
      decision_tier: "clarify",
      confidence: 0.4,
    });
    expect(parseSearchModeDecision({ decision_tier: "operator_pick", confidence: 1 })).toEqual({
      decision_tier: "operator_pick",
      confidence: 1,
    });
  });

  it("renders the legacy clarify and typed-fail receipts", () => {
    expect(describeSearchModeReceipt({ decision_tier: "clarify", confidence: 0.4 })).toBe(
      "检索档需要用户选择。",
    );
    expect(
      describeSearchModeReceipt({
        search_mode: null,
        decision_tier: "typed_fail",
        confidence: 0,
        failure_code: "tier_unavailable",
      }),
    ).toBe("检索档失败：tier_unavailable");
  });
});

describe("every decision tier is labelled by provenance", () => {
  const CASES: readonly (readonly [SearchModeDecisionTier, string])[] = [
    ["rules", "规则命中"],
    ["jev", "判定命中"],
    ["inherit", "沿用上次"],
    ["fallback", "自动回退"],
    ["clarify", "需要选择"],
    ["typed_fail", "具名失败"],
    ["operator_pick", "人工选择"],
  ];

  it("names each tier without naming the retrieval implementation", () => {
    for (const [tier, label] of CASES) {
      const text = describeSearchModeReceipt({
        search_mode: "lexical",
        decision_tier: tier,
        confidence: 1,
      });
      expect(text).toContain(`检索档决策：${label}`);
      expectNoInternalMode(text);
    }
  });

  it("falls back to the raw tier for a tier it does not know", () => {
    // A tier is provenance, not a retrieval name: an unknown one may be
    // shown as it arrived rather than dropped.
    const text = describeSearchModeReceipt({
      search_mode: "lexical",
      decision_tier: "future_tier" as SearchModeDecisionTier,
      confidence: 1,
    });
    expect(text).toContain("检索档决策：future_tier");
    expectNoInternalMode(text);
  });
});
