// Shared types mirroring the backend contract (see
// src/fin/host/workspace_http.py for the SSOT of each field).

export interface ThreadListItem {
  id: string;
  title: string;
  created_at: string;
}

export interface Citation {
  document_id: string;
  version: string;
  page: number;
  quote: string;
}

export type RunStatus = "done" | "refuse" | "step_cap";

// Soft-Track (specs/soft-track.md): per-turn host contract. Optional
// on the wire until the host peer exposes SSE/GET ``track``.
export type SoftTrack = "dialogue" | "clarify" | "off_topic" | "research";

// Soft-Track RunRecord audit blob. Fields beyond ``track`` stay
// optional so older rows / partial peers still typecheck.
export interface TrackDecision {
  track: SoftTrack;
  confidence?: number;
  decision_tier?: string;
  signals?: Record<string, unknown>;
}

// T3 (spec line 32): one human-readable row per tool call observed
// in the run; the SPA renders `summary` in the tools region and
// correlates with the live SSE frames by `tool_call_id`. Mirrors
// fin.host.workspace_http.ToolSummaryPayload.
export interface ToolSummary {
  name: string;
  summary: string;
  tool_call_id: string;
}

export interface Turn {
  run_id: string;
  question: string;
  status: RunStatus;
  status_reason?: string;
  answer: string;
  citations: Citation[];
  // T3: the run's streamed thinking prose (collapsed / line-clamped
  // in the UI). Empty when the model surfaced no reasoning.
  thinking_text?: string;
  // T4 (#37): total thinking duration in ms. After the run the
  // thinking region collapses to a one-line duration label
  // ("Thought for 4.2s"); 0 / undefined when no thinking streamed.
  thinking_duration_ms?: number;
  // T3: one entry per tool call observed this run (dicts, not
  // strings - T1's placeholder shape).
  tool_summaries?: ToolSummary[];
  // T2: ordinary-person copy on refuse / step_cap (empty on done).
  // Mirrored on the SSE status frame and the persisted turn row so
  // reload keeps the copy.
  assistant_message?: string;
  // Soft-Track: optional run track from SSE status / GET turn row.
  // Missing is OK (no hint) until the host peer populates the wire.
  track?: SoftTrack;
  // Soft-Track: optional audit blob from GET history / RunRecord.
  track_decision?: TrackDecision;
  // T4 (spec line 38): GET-while-running signal on the turn row.
  // Mirrors ``fin.host.workspace_http.TurnRow.in_flight``. True
  // only on the most-recent row of a thread whose lock is held,
  // which by construction can only be the latest turn. Drives the
  // T4 banner + Resume button in ThreadPage.
  in_flight?: boolean;
  created_at: string;
}

export interface ThreadDetail extends ThreadListItem {
  turns: Turn[];
  // T4: thread-level live running signal (lock held, no RunRecord
  // persisted yet). The Composer reads this too so submission is
  // disabled BEFORE the optimistic turn surfaces. Mirrors
  // ``ThreadDetailResponse.in_flight``.
  in_flight?: boolean;
  // T4 fix (code-review H1): a checkpoint with ``next`` nodes is
  // present AND the lock is NOT held — i.e. the process died
  // mid-run and the operator should see the Resume banner. The
  // banner used to be keyed off the live ``in_flight`` flag,
  // which made it appear during a process-alive refresh (when
  // it is forbidden) and never appear after a real crash (when
  // it is needed). The server derives this from
  // ``pending_nodes(thread_id) and not is_locked``.
  pending_checkpoint?: boolean;
}

// T10 (ADR 0033): the boot-scoped workspace status surface behind
// ``GET /api/status``. Boot-scoped means configuration plus
// dependency reachability — nothing run- or turn-scoped rides here
// (no step budget, no token usage, no soft-track confidence, no
// "% of context window"). Mirrors
// fin.host.workspace_http.WorkspaceStatusResponse.

// ``unreachable`` means every retrieve call answers with the typed
// ``retrieve_unavailable`` EXIT — a value to render, never an error.
export type RetrieveReachability = "reachable" | "unreachable";

// Which port is behind the retrieve seam: the product iknow-memory
// client, or the hermetic fake that answers with no corpus.
export type RetrieveAdapterKind = "mcp" | "fake";

export interface WorkspaceStatus {
  retrieve_reachability: RetrieveReachability;
  retrieve_adapter_kind: RetrieveAdapterKind;
  // The resolved ``FIN_MODEL`` slug only — the host never puts the
  // endpoint or key material on this wire.
  model_identity: string;
  // From the same catalog that serves ``GET /api/corpus``; zero is a
  // valid value, not an error.
  corpus_filing_count: number;
}

export interface Filing {
  document_id: string;
  company: string;
  period: string;
  version: string;
  page_count: number;

  // T1 does not render these; they exist in the payload.
  [key: string]: unknown;
}
