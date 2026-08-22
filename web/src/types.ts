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

export interface Turn {
  run_id: string;
  question: string;
  status: RunStatus;
  status_reason?: string;
  answer: string;
  citations: Citation[];
  thinking_text?: string;
  tool_summaries?: string[];
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
