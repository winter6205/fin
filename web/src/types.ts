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
  created_at: string;
}

export interface ThreadDetail extends ThreadListItem {
  turns: Turn[];
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
