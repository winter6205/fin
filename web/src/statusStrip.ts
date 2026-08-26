// T10 (ADR 0033): copy for the Sidebar status strip.
//
// The strip is the only place an ordinary operator learns that
// retrieve is unreachable, so the rule it enforces is "never blank":
// every view state — including a status route that never answered —
// yields non-empty copy for every line. Rendering lives in
// components/Sidebar.tsx; the wording and the two-state decision
// live here so they are unit-testable without a DOM.

import type { WorkspaceStatus } from "./types";

const REACHABILITIES: ReadonlySet<string> = new Set([
  "reachable",
  "unreachable",
]);

const ADAPTER_KINDS: ReadonlySet<string> = new Set(["mcp", "fake"]);

/**
 * Defensive parse of the ``GET /api/status`` body.
 *
 * Returns ``null`` for anything that is not the full ADR 0033 field
 * set. An unknown reachability value is NOT coerced to
 * ``reachable``: a strip that vouches for a port it cannot read is
 * worse than one that says it does not know.
 */
export function parseWorkspaceStatus(raw: unknown): WorkspaceStatus | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const reachability = obj.retrieve_reachability;
  const adapterKind = obj.retrieve_adapter_kind;
  const model = obj.model_identity;
  const count = obj.corpus_filing_count;
  if (typeof reachability !== "string" || !REACHABILITIES.has(reachability)) {
    return null;
  }
  if (typeof adapterKind !== "string" || !ADAPTER_KINDS.has(adapterKind)) {
    return null;
  }
  if (typeof model !== "string") return null;
  if (typeof count !== "number" || !Number.isFinite(count)) return null;
  return {
    retrieve_reachability: reachability as WorkspaceStatus["retrieve_reachability"],
    retrieve_adapter_kind: adapterKind as WorkspaceStatus["retrieve_adapter_kind"],
    model_identity: model,
    corpus_filing_count: count,
  };
}

/** What the strip knows right now. ``unavailable`` is the route
 * itself failing — distinct from a route that answered
 * ``unreachable``, which is a healthy route reporting a sick port. */
export type WorkspaceStatusView =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; status: WorkspaceStatus };

export function workspaceStatusView(
  status: WorkspaceStatus | null,
  loaded: boolean,
): WorkspaceStatusView {
  if (!loaded) return { state: "loading" };
  if (status === null) return { state: "unavailable" };
  return { state: "ready", status };
}

export interface StatusStripCopy {
  retrieveLabel: string;
  // ``degraded`` is what the Sidebar paints in the warning treatment.
  retrieveTone: "ok" | "degraded";
  retrieveDetail: string;
  adapterLabel: string;
  modelLabel: string;
  corpusLabel: string;
}

const UNREACHABLE_DETAIL =
  "找不到 iknow-memory 检索服务，回答不会带引用来源。" +
  "启动该服务，或设置 FIN_RETRIEVE_ADAPTER=fake 以占位检索启动。";

export function describeStatusStrip(view: WorkspaceStatusView): StatusStripCopy {
  if (view.state === "loading") {
    return {
      retrieveLabel: "检索：读取中",
      // Not an accusation: the first fetch has not answered yet.
      retrieveTone: "ok",
      retrieveDetail: "正在读取工作区状态…",
      adapterLabel: "检索来源：读取中",
      modelLabel: "模型：读取中",
      corpusLabel: "语料：读取中",
    };
  }
  if (view.state === "unavailable") {
    return {
      retrieveLabel: "检索：未知",
      retrieveTone: "degraded",
      retrieveDetail: "暂时读不到工作区状态，刷新页面可重试。",
      adapterLabel: "检索来源：未知",
      modelLabel: "模型：未知",
      corpusLabel: "语料：未知",
    };
  }

  const { status } = view;
  const unreachable = status.retrieve_reachability === "unreachable";
  const fake = status.retrieve_adapter_kind === "fake";
  return {
    retrieveLabel: unreachable ? "检索：不可用" : "检索：可用",
    retrieveTone: unreachable ? "degraded" : "ok",
    retrieveDetail: unreachable
      ? UNREACHABLE_DETAIL
      : fake
        ? "本地占位检索：能回答，但没有真实语料。"
        : "已连接 iknow-memory 检索服务。",
    adapterLabel: fake
      ? "检索来源：本地占位（fake）"
      : "检索来源：iknow-memory（mcp）",
    modelLabel: `模型：${status.model_identity || "未配置"}`,
    corpusLabel:
      status.corpus_filing_count === 0
        ? "语料：0 份（未导入）"
        : `语料：${status.corpus_filing_count} 份`,
  };
}
