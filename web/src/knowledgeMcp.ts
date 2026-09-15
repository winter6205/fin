// SC6 (specs/workbench-retrieve-product-seam.md, ADR 0051): the
// left-corner 知识 MCP line. Reads the SAME boot snapshot as
// ``GET /api/status`` and maps the three-state outcome (缺 / 坏 /
// 可达) to the one operator-visible line. Copy and decision are plain
// data so they are unit-testable without a DOM; the Sidebar renders
// the returned ``text``.
//
// Non-reachable copy (missing OR fault) is EXACTLY 「知识 MCP 故障」
// — the same string the host returns as the 409 body message, so the
// left corner and the refused conversation mouth cannot drift apart.
// Icon / expand copy are not pinned; this helper keeps the line only.

import type { KnowledgeMcpState, WorkspaceStatus } from "./types";

// Re-exported so this module is the one import a consumer needs for
// the three-state model (the canonical type lives in the shared wire
// mirror ``types.ts``).
export type { KnowledgeMcpState, WorkspaceStatus };

export const KNOWLEDGE_MCP_FAULT_COPY = "知识 MCP 故障";

/** Neutral line before the first status read lands. No accusation is
 * made while the probe is still in flight (and no silent OK either). */
const LOADING_COPY = "知识 MCP 检查中";

/** Neutral line when the status route itself never answered — the
 * route failing is distinct from a route that answered 缺 / 坏. */
const UNAVAILABLE_COPY = "知识 MCP 状态未知";

/** Healthy line for 可达. Deliberately does NOT contain the fault
 * copy, and does NOT reuse the retired fake-retrieve healthy word. */
const REACHABLE_COPY = "知识 MCP 已连接";

export interface KnowledgeMcpLine {
  /** The published three-state value, plus ``unknown`` for the
   * not-yet-loaded / route-unavailable views. */
  state: KnowledgeMcpState | "unknown";
  /** The exact one-line copy the left corner renders. Never blank. */
  text: string;
  /** Drives the line's treatment (red-ish for 缺 / 坏). */
  tone: "ok" | "degraded" | "neutral";
}

const STATES: ReadonlySet<string> = new Set(["missing", "fault", "reachable"]);

const ADAPTER_KINDS: ReadonlySet<string> = new Set(["mcp", "fake"]);

/** The one string-field guard: a member of ``values``, else ``null``. */
function asMember<T extends string>(
  value: unknown,
  values: ReadonlySet<string>,
): T | null {
  return typeof value === "string" && values.has(value) ? (value as T) : null;
}

/**
 * Defensive parse of the ``GET /api/status`` body.
 *
 * Returns ``null`` for anything that is not the pinned four-field set.
 * The retired ``retrieve_reachability`` field and any unknown
 * ``knowledge_mcp`` value are REJECTED, never coerced: a line that
 * vouches for a port it cannot read is worse than one that says
 * unknown.
 */
export function parseWorkspaceStatus(raw: unknown): WorkspaceStatus | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  // Reject the retired two-state payload outright rather than
  // falling back to it.
  if ("retrieve_reachability" in obj) return null;
  const mcp = asMember<WorkspaceStatus["knowledge_mcp"]>(
    obj.knowledge_mcp,
    STATES,
  );
  const adapterKind = asMember<WorkspaceStatus["retrieve_adapter_kind"]>(
    obj.retrieve_adapter_kind,
    ADAPTER_KINDS,
  );
  const model = obj.model_identity;
  const count = obj.corpus_filing_count;
  if (mcp === null || adapterKind === null) return null;
  if (typeof model !== "string") return null;
  if (typeof count !== "number" || !Number.isFinite(count)) return null;
  return {
    knowledge_mcp: mcp,
    retrieve_adapter_kind: adapterKind,
    model_identity: model,
    corpus_filing_count: count,
  };
}

/**
 * Map the boot snapshot to the left-corner line.
 *
 *  - ``missing`` / ``fault`` → degraded, text exactly
 *    ``KNOWLEDGE_MCP_FAULT_COPY``.
 *  - ``reachable`` → healthy, text omits the fault copy.
 *  - ``null`` / not-yet-loaded → neutral, no fault copy.
 *
 * Boot-scoped: no polling; the caller passes the store's latest
 * snapshot.
 */
export function knowledgeMcpLine(
  status: WorkspaceStatus | null,
  loaded: boolean,
): KnowledgeMcpLine {
  if (!loaded) {
    return { state: "unknown", text: LOADING_COPY, tone: "neutral" };
  }
  if (status === null) {
    return { state: "unknown", text: UNAVAILABLE_COPY, tone: "neutral" };
  }
  if (status.knowledge_mcp === "reachable") {
    return { state: "reachable", text: REACHABLE_COPY, tone: "ok" };
  }
  return {
    state: status.knowledge_mcp,
    text: KNOWLEDGE_MCP_FAULT_COPY,
    tone: "degraded",
  };
}
