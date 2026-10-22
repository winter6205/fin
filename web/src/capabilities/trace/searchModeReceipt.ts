// The developer search-mode receipt (spec search-mode-host items 5-7,
// ADR 0104).
//
// TWO SEPARATE JOBS live here, and the difference between them is the
// whole point of the correction:
//
//  1. The PARSER keeps the audit record: the decision's tier, its
//     confidence and a BOUNDED allowlist of the host's diagnostic
//     signals. On a fallback the top-level confidence is POLICY
//     uncertainty (0.0) and the judge's own label probability is in the
//     signals, so discarding them would present the fallback as the
//     judge being sure.
//
//  2. The RENDERER states PROVENANCE, never the retrieval
//     implementation. The internal 档 name and the raw mode token are
//     machine diagnostics: they stay in the parsed decision and the logs,
//     and they never reach the rendered receipt (docs/CONTEXT.md 检索档:
//     "rendered UI omits internal mode names and raw tokens remain in
//     machine diagnostics").
//
// A malformed receipt is REJECTED, never read as a valid fallback.

import type {
  RetrieveMode,
  SearchModeDecision,
  SearchModeDecisionSignals,
  SearchModeDecisionTier,
} from "../../types";

const MODES: readonly RetrieveMode[] = ["lexical", "graph", "global", "sql"];
const DECISION_TIERS: readonly SearchModeDecisionTier[] = [
  "rules",
  "jev",
  "inherit",
  "fallback",
  "clarify",
  "typed_fail",
  "operator_pick",
];

/** Provenance labels. A tier says WHO decided — never WHICH retrieval
 * implementation ran, so no label here may name a 档. */
const TIER_LABELS: Partial<Record<SearchModeDecisionTier, string>> = {
  rules: "规则命中",
  jev: "判定命中",
  inherit: "沿用上次",
  fallback: "自动回退",
  clarify: "需要选择",
  typed_fail: "具名失败",
  operator_pick: "人工选择",
};

/** The bounded diagnostic allowlist (spec search-mode-host item 6): the
 * judge's EXIT, the label it returned, the confidence it returned and the
 * error type of a failed call. A key outside this set is IGNORED — the
 * host records more than the receipt shows. */
const SIGNAL_STRING_KEYS = [
  "mode_jev_exit",
  "mode_jev_label",
  "mode_jev_error_type",
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validMode(value: unknown): boolean {
  return value === undefined || value === null || MODES.includes(value as RetrieveMode);
}

function validDecisionTier(value: unknown): boolean {
  return (
    value === undefined ||
    (typeof value === "string" &&
      DECISION_TIERS.includes(value as SearchModeDecisionTier))
  );
}

function validConfidence(value: unknown): value is number | undefined {
  return (
    value === undefined ||
    (typeof value === "number" &&
      Number.isFinite(value) &&
      value >= 0 &&
      value <= 1)
  );
}

function validFailureCode(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}

/**
 * Keep the bounded diagnostic signals, and nothing else.
 *
 * Returns ``null`` for a MALFORMED bag — a known signal carrying the
 * wrong type, or a bag that is not a record — because a receipt read
 * with the wrong types is a broken audit record and must not pass as a
 * valid one. An unknown key is simply not kept.
 */
function parseSignals(value: unknown): SearchModeDecisionSignals | null {
  if (value === undefined) return {};
  if (!isRecord(value)) return null;
  const kept: SearchModeDecisionSignals = {};
  for (const key of SIGNAL_STRING_KEYS) {
    const raw = value[key];
    if (raw === undefined) continue;
    if (typeof raw !== "string") return null;
    kept[key] = raw;
  }
  const confidence = value.mode_jev_confidence;
  if (confidence !== undefined) {
    // The judge's own probability is bounded like the decision's.
    if (!validConfidence(confidence)) return null;
    kept.mode_jev_confidence = confidence;
  }
  return kept;
}

/** The top-level fields, validated together. A receipt that fails any
 * one of them is malformed and is rejected whole. */
function validDecision(value: Record<string, unknown>): boolean {
  return (
    validMode(value.search_mode) &&
    validDecisionTier(value.decision_tier) &&
    validConfidence(value.confidence) &&
    validFailureCode(value.failure_code)
  );
}

/** The validated decision, with only the fields the receipt carries. */
function assembleDecision(
  value: Record<string, unknown>,
  signals: SearchModeDecisionSignals,
): SearchModeDecision {
  const decision: SearchModeDecision = {};
  const mode = value.search_mode;
  const tier = value.decision_tier;
  const confidence = value.confidence;
  const failureCode = value.failure_code;
  if (mode !== undefined) decision.search_mode = mode as RetrieveMode | null;
  if (tier !== undefined) decision.decision_tier = tier as SearchModeDecisionTier;
  if (typeof confidence === "number") decision.confidence = confidence;
  if (Object.keys(signals).length > 0) decision.signals = signals;
  if (failureCode !== undefined) decision.failure_code = failureCode as string | null;
  return decision;
}

/** Parse only the bounded fields the developer receipt displays. */
export function parseSearchModeDecision(
  value: unknown,
): SearchModeDecision | undefined {
  if (!isRecord(value)) return undefined;
  const signals = parseSignals(value.signals);
  if (signals === null) return undefined;
  if (!validDecision(value)) return undefined;
  return assembleDecision(value, signals);
}

/** How the decision was made. An unrecognized tier is shown as it
 * arrived: a tier is provenance, not a retrieval name. */
function tierLabel(tier: SearchModeDecisionTier | undefined): string {
  if (tier === undefined) return "未记录";
  return TIER_LABELS[tier] ?? tier;
}

function formatConfidence(value: number): string {
  return value.toFixed(2);
}

/** The receipt's parts, in a fixed order: what decided, how sure the
 * decision was, how sure the JUDGE was, and how the judge ended. */
function receiptParts(decision: SearchModeDecision): string[] {
  const parts = [`检索档决策：${tierLabel(decision.decision_tier)}`];
  const signals = decision.signals ?? {};
  const confidence = decision.confidence;
  if (confidence !== undefined) parts.push(`决策置信度：${formatConfidence(confidence)}`);
  const judgeConfidence = signals.mode_jev_confidence;
  if (judgeConfidence !== undefined) {
    parts.push(`判定置信度：${formatConfidence(judgeConfidence)}`);
  }
  const exit = signals.mode_jev_exit;
  if (exit !== undefined) parts.push(`判定结果：${exit}`);
  const errorType = signals.mode_jev_error_type;
  if (errorType !== undefined) parts.push(`错误类型：${errorType}`);
  return parts;
}

/** Human-readable receipt text; null means history predates this field. */
export function describeSearchModeReceipt(
  decision: SearchModeDecision | null,
): string {
  if (decision === null) return "历史记录未保存检索档决策。";
  if (!decision.search_mode) {
    if (decision.failure_code) return `检索档失败：${decision.failure_code}`;
    if (decision.decision_tier === "clarify") return "检索档需要用户选择。";
    return "本轮尚未进入检索档决策。";
  }
  return receiptParts(decision).join(" · ");
}
