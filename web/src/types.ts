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
  // T3: one entry per tool call observed this run (dicts, not
  // strings - T1's placeholder shape).
  tool_summaries?: ToolSummary[];
  // T2: ordinary-person copy on refuse / step_cap (empty on done).
  // Mirrored on the SSE status frame and the persisted turn row so
  // reload keeps the copy.
  assistant_message?: string;
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

export interface Filing {
  document_id: string;
  company: string;
  period: string;
  version: string;
  page_count: number;

  // T1 does not render these; they exist in the payload.
  [key: string]: unknown;
}
