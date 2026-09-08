// T3 (specs/workbench-chrome.md): the StatusStrip footer left the
// Sidebar; the boot-scoped status copy is now the 监控 page's job.
// The invariant under test is "never blank" (SC7): every view state
// — including a status route that never answered — yields non-empty
// ``headline`` + ``detail`` copy. The model identity never rides
// here (model is not a chrome line; it stays on research-thread
// surfaces like the composer / turn row).
//
// Rendering lives in components/MonitorStatusPanel.tsx; the wording
// and the two-state decision live here so they are unit-testable
// without a DOM.

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
 * ``reachable``: a panel that vouches for a port it cannot read is
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
    retrieve_reachability:
      reachability as WorkspaceStatus["retrieve_reachability"],
    retrieve_adapter_kind:
      adapterKind as WorkspaceStatus["retrieve_adapter_kind"],
    model_identity: model,
    corpus_filing_count: count,
  };
}

/** What the panel knows right now. ``unavailable`` is the route
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

export interface MonitorStatusCopy {
  /** One-line top label: 知识库搜索：检查中 / 查不到 / 连不上 / 可用. */
  headline: string;
  /** Ordinary-person explanation under the headline. Never empty. */
  detail: string;
  /** Drives the panel's treatment (red background for degraded). */
  tone: "ok" | "degraded";
}

const UNREACHABLE_DETAIL =
  "现在搜不到知识库里的内容，回答不会带来源。把搜索服务准备好后，点「重连」。";

/** Copy for the 监控 page's status panel. The five view states each
 * produce non-empty copy; ``degraded`` is what the panel paints in
 * the warning treatment. Operator copy names 知识库搜索, not MCP /
 * env vars / 工作区. */
export function describeMonitorStatus(
  view: WorkspaceStatusView,
): MonitorStatusCopy {
  if (view.state === "loading") {
    return {
      headline: "知识库搜索：检查中",
      tone: "ok",
      detail: "正在检查能不能搜到知识库…",
    };
  }
  if (view.state === "unavailable") {
    return {
      headline: "知识库搜索：查不到",
      tone: "degraded",
      detail: "暂时查不到搜索是否可用，点「重连」再试。",
    };
  }

  const { status } = view;
  const unreachable = status.retrieve_reachability === "unreachable";
  const fake = status.retrieve_adapter_kind === "fake";
  if (unreachable) {
    return {
      headline: "知识库搜索：连不上",
      tone: "degraded",
      detail: UNREACHABLE_DETAIL,
    };
  }
  if (fake) {
    return {
      headline: "知识库搜索：可用",
      tone: "ok",
      detail: `现在是演示搜索，没有真实文件；已添加 ${status.corpus_filing_count} 份。`,
    };
  }
  return {
    headline: "知识库搜索：可用",
    tone: "ok",
    detail: `可以搜索知识库；已添加 ${status.corpus_filing_count} 份。`,
  };
}
