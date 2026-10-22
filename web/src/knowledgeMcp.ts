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
//
// T4 (specs/first-closed-loop.md) adds the sibling ``boundCollectionLine``
// below: the 资料集合 identity the host publishes on the same payload.
// It is a separate line with a separate contract — the 知识 MCP line's
// copy and states are unchanged.

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

function isValidRunCount(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
  );
}

function parseWorkspaceStatusStates(
  obj: Record<string, unknown>,
): Pick<WorkspaceStatus, "knowledge_mcp" | "retrieve_adapter_kind"> | null {
  const mcp = asMember<WorkspaceStatus["knowledge_mcp"]>(
    obj.knowledge_mcp,
    STATES,
  );
  const adapterKind = asMember<WorkspaceStatus["retrieve_adapter_kind"]>(
    obj.retrieve_adapter_kind,
    ADAPTER_KINDS,
  );
  if (mcp === null || adapterKind === null) return null;
  return { knowledge_mcp: mcp, retrieve_adapter_kind: adapterKind };
}

function parseWorkspaceStatusMetadata(
  obj: Record<string, unknown>,
): Pick<
  WorkspaceStatus,
  "model_identity" | "corpus_filing_count" | "bound_collection_name"
> | null {
  const model = obj.model_identity;
  const count = obj.corpus_filing_count;
  const bound = obj.bound_collection_name;
  if (
    typeof model !== "string" ||
    typeof count !== "number" ||
    !Number.isFinite(count) ||
    typeof bound !== "string" ||
    bound.length === 0
  ) {
    return null;
  }
  return {
    model_identity: model,
    corpus_filing_count: count,
    bound_collection_name: bound,
  };
}

function parseWorkspaceRunCounts(raw: unknown): WorkspaceStatus["run_counts"] | null {
  if (
    typeof raw !== "object" ||
    raw === null ||
    Array.isArray(raw)
  ) {
    return null;
  }
  const counts = raw as Record<string, unknown>;
  const active = counts.active;
  const paused = counts.paused;
  const suspectedStuck = counts.suspected_stuck;
  if (
    !isValidRunCount(active) ||
    !isValidRunCount(paused) ||
    !isValidRunCount(suspectedStuck)
  ) {
    return null;
  }
  return { active, paused, suspected_stuck: suspectedStuck };
}

/**
 * Defensive parse of the ``GET /api/status`` body.
 *
 * Returns ``null`` for anything that is not the pinned status field set.
 * The retired ``retrieve_reachability`` field and any unknown
 * ``knowledge_mcp`` value are REJECTED, never coerced: a line that
 * vouches for a port it cannot read is worse than one that says
 * unknown.
 *
 * ``bound_collection_name`` is a free-form string, not a typed state:
 * the host mirrors iknow's collection name, so the SPA must not carry
 * its own copy of the name space. An empty string is rejected — it
 * would render a blank bind line, and the host's 未绑定 sentinel is a
 * real value, not a missing one.
 */
export function parseWorkspaceStatus(raw: unknown): WorkspaceStatus | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  // Reject the retired two-state payload outright rather than
  // falling back to it.
  if ("retrieve_reachability" in obj) return null;
  const states = parseWorkspaceStatusStates(obj);
  if (states === null) return null;
  const metadata = parseWorkspaceStatusMetadata(obj);
  if (metadata === null) return null;
  const runCounts = parseWorkspaceRunCounts(obj.run_counts);
  if (runCounts === null) return null;
  return { ...states, ...metadata, run_counts: runCounts };
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

// ----- T4: the bound 资料集合 line -----
//
// A SIBLING of the 知识 MCP line, deliberately not a second state on
// it: "the MCP answered" and "this process is bound to a 资料集合" are
// different facts, and the host models them as different fields
// (``knowledge_mcp`` vs ``bound_collection_name``). Copy lives here so
// it is unit-testable without a DOM, same as the MCP line.
//
// The line names the collection; it never claims the collection is
// 就绪. Readiness is iknow's own 资料页 (spec assumption 9), and
// ``corpus_filing_count`` is the local catalog (spec SC7) — neither is
// rendered here.

/** The host's 未绑定 sentinel (``fin.host.collection_bind``).
 * HAND-MIRRORED: the SPA carries no copy of the host's name space by
 * design, so this literal is a second spelling of
 * ``fin.host.collection_bind.BOUND_COLLECTION_UNBOUND`` — a host-side
 * rename must be applied here too (the lockstep guard in
 * ``knowledgeMcp.test.ts`` covers the field set, not this sentinel). */
export const BOUND_COLLECTION_UNBOUND = "unbound";

/** Bound: the operator reads which 资料集合 回答 will come from. */
const BOUND_COLLECTION_PREFIX = "资料集合：";

/** 未绑定: a configuration statement, not an error — a test-only
 * adapter, or a product boot whose 知识 MCP never reached 可达. */
const BOUND_COLLECTION_UNBOUND_COPY = "资料集合：未绑定";

/** Neutral line while the first status read is in flight, or when the
 * route never answered: this boot has not said which collection it
 * holds, so the line does not guess one. */
const BOUND_COLLECTION_UNKNOWN_COPY = "资料集合：未知";

export interface BoundCollectionLine {
  /** ``bound`` / ``unbound``, plus ``unknown`` for the not-yet-loaded
   * and route-unavailable views. */
  state: "bound" | "unbound" | "unknown";
  /** The exact one-line copy the left corner renders. Never blank. */
  text: string;
  /** ``ok`` when a collection is named, ``neutral`` otherwise — an
   * unbound boot is a configuration fact, not a fault. */
  tone: "ok" | "neutral";
}

/**
 * Map the boot snapshot's bind field to the left-corner line.
 *
 *  - a name → ``bound``, text ``资料集合：<name>``.
 *  - ``unbound`` → ``unbound``, text exactly the 未绑定 copy.
 *  - ``null`` / not-yet-loaded → ``unknown``, no collection named.
 *
 * The name is rendered verbatim from the wire: the SPA holds no copy
 * of the iknow name space, so a host-side rename cannot leave the two
 * sides disagreeing.
 */
export function boundCollectionLine(
  status: WorkspaceStatus | null,
  loaded: boolean,
): BoundCollectionLine {
  if (!loaded || status === null) {
    return {
      state: "unknown",
      text: BOUND_COLLECTION_UNKNOWN_COPY,
      tone: "neutral",
    };
  }
  if (status.bound_collection_name === BOUND_COLLECTION_UNBOUND) {
    return {
      state: "unbound",
      text: BOUND_COLLECTION_UNBOUND_COPY,
      tone: "neutral",
    };
  }
  return {
    state: "bound",
    text: `${BOUND_COLLECTION_PREFIX}${status.bound_collection_name}`,
    tone: "ok",
  };
}
