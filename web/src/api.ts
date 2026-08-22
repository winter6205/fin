// API client for the workspace backend. All routes are same-origin
// (FastAPI serves both /api/* and, after build, the SPA itself).
//
// The send path uses `fetch` + ReadableStream reader — NOT native
// EventSource — because EventSource cannot POST a body and cannot
// carry JSON. This matches spec ADR 0026.

import type { Citation, Filing, ThreadDetail, ThreadListItem } from "./types";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail =
        typeof body === "object" && body !== null && "error" in body
          ? String((body as Record<string, unknown>).error)
          : JSON.stringify(body);
    } catch {
      detail = res.statusText;
    }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export async function listThreads(): Promise<ThreadListItem[]> {
  const res = await fetch("/api/threads");
  return jsonOrThrow<ThreadListItem[]>(res);
}

export async function createThread(
  firstMessage: string | null
): Promise<ThreadListItem> {
  const res = await fetch("/api/threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ first_message: firstMessage }),
  });
  return jsonOrThrow<ThreadListItem>(res);
}

export async function getThread(threadId: string): Promise<ThreadDetail> {
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}`);
  return jsonOrThrow<ThreadDetail>(res);
}

export async function deleteThread(threadId: string): Promise<void> {
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

export async function getCorpus(): Promise<Filing[]> {
  const res = await fetch("/api/corpus");
  const body = await jsonOrThrow<{ filings: Filing[] }>(res);
  return body.filings;
}

export interface SendResult {
  // Server-minted run id (UUID4). The same id is persisted on
  // the RunRecord and surfaces on the turn table; the client
  // uses it on the optimistic turn so it matches the persisted
  // row BEFORE any re-GET (the previous implementation minted
  // ``crypto.randomUUID()`` locally, so the displayed id
  // differed from the persisted id until reload).
  run_id: string;
  status: "done" | "refuse" | "step_cap";
  reason?: string | null;
  citations: Citation[];
  answerText: string;
  // T2: ordinary-person copy from the SSE ``status`` frame
  // (non-empty only on refuse / step_cap).
  assistantMessage: string;
}

// T3 streaming events surfaced to the caller as they arrive.
// The store threads each one into the optimistic turn so the
// tools region + scratchpad fill in while the run is live.
export type SendEvent =
  | { type: "thinking"; text: string }
  | {
      type: "tool_call";
      name: string;
      args: Record<string, unknown>;
      tool_call_id: string;
    }
  | {
      type: "tool_result";
      tool_call_id: string;
      name: string;
      result: unknown;
    }
  | {
      type: "status";
      run_id: string;
      status: SendResult["status"];
      reason?: string | null;
      assistant_message: string;
    }
  | { type: "answer"; text: string }
  | { type: "citations"; citations: Citation[] };

export class ConcurrentRunError extends Error {}
export class IngestMissingError extends Error {}

/** Request body for the send path. ``thinking`` is the operator's
 * reasoning preference toggle (T3); the backend forwards it to the
 * model provider. */
export interface SendRequest {
  question: string;
  thinking?: boolean;
}

/**
 * POST one question to a thread. Reads the SSE stream via fetch +
 * reader (NOT native EventSource) and resolves with the terminal
 * events. T3 surfaces streaming frames (thinking / tool_call /
 * tool_result) via the optional ``onEvent`` callback so the SPA
 * can render the tools region + scratchpad while the run is live.
 */
export async function sendMessage(
  threadId: string,
  request: SendRequest,
  onEvent?: (e: SendEvent) => void
): Promise<SendResult> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }
  );

  // Error paths happen BEFORE any SSE frame per spec.
  if (res.status === 409) throw new ConcurrentRunError("concurrent_run");
  if (res.status === 503) throw new IngestMissingError("ingest_missing");
  if (res.status === 204) {
    // Empty submit no-op; caller should not have called this.
    // No SSE frames arrive on a 204, so there is no server-minted
    // run_id — the placeholder uuid keeps SendResult total.
    return {
      run_id: crypto.randomUUID(),
      status: "done",
      citations: [],
      answerText: "",
      assistantMessage: "",
    };
  }
  if (!res.ok || !res.body) {
    let detail = "";
    try {
      const j = await res.json();
      detail = JSON.stringify(j);
    } catch {
      detail = res.statusText;
    }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }

  const result: SendResult = {
    // Run id starts empty; the ``status`` SSE frame is the first
    // terminal event the server emits and carries the
    // server-minted id. The id is required for the optimistic
    // turn in the store, so callers that re-resolve before any
    // event arrived will still get a uuid for the placeholder
    // (it gets reconciled on the ``status`` event).
    run_id: crypto.randomUUID(),
    status: "done",
    reason: null,
    citations: [],
    answerText: "",
    assistantMessage: "",
  };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const rawFrame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      let eventName = "";
      let dataLines: string[] = [];
      for (const line of rawFrame.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!eventName || dataLines.length === 0) continue;

      let payload: unknown;
      try {
        payload = JSON.parse(dataLines.join("\n"));
      } catch {
        continue; // Malformed frame - skip rather than crash.
      }

      switch (eventName) {
        case "thinking": {
          // T3: per-AIMessage scratchpad prose. The SPA renders
          // it collapsed / line-clamped; the prose never
          // participates in citation matching (SC60 binding).
          const t = payload as { text: string };
          onEvent?.({ type: "thinking", text: t.text ?? "" });
          break;
        }
        case "tool_call": {
          // T3 (spec line 32): ``name`` / ``args`` /
          // ``tool_call_id`` so the SPA can render a running
          // indicator while the tool runs.
          const tc = payload as {
            name: string;
            args: Record<string, unknown>;
            tool_call_id: string;
          };
          onEvent?.({
            type: "tool_call",
            name: tc.name,
            args: tc.args ?? {},
            tool_call_id: tc.tool_call_id,
          });
          break;
        }
        case "tool_result": {
          // T3: ``tool_call_id`` + JSON-decoded ``result`` so the
          // SPA can flip the running row to its final shape.
          const tr = payload as {
            tool_call_id: string;
            name: string;
            result: unknown;
          };
          onEvent?.({
            type: "tool_result",
            tool_call_id: tr.tool_call_id,
            name: tr.name,
            result: tr.result,
          });
          break;
        }
        case "status": {
          // The status frame carries the server-minted ``run_id``
          // so the optimistic turn the store renders can match
          // the persisted RunRecord BEFORE any re-GET. T2 adds
          // ``assistant_message`` (ordinary-person copy on
          // refuse / step_cap).
          const s = payload as {
            run_id?: string;
            status: SendResult["status"];
            reason?: string;
            assistant_message?: string;
          };
          result.status = s.status;
          result.reason = s.reason ?? null;
          result.assistantMessage = s.assistant_message ?? "";
          if (typeof s.run_id === "string" && s.run_id.length > 0) {
            result.run_id = s.run_id;
          }
          onEvent?.({
            type: "status",
            run_id: result.run_id,
            status: s.status,
            reason: s.reason ?? null,
            assistant_message: s.assistant_message ?? "",
          });
          break;
        }
        case "citations":
          result.citations = payload as SendResult["citations"];
          onEvent?.({ type: "citations", citations: result.citations });
          break;
        case "answer":
          result.answerText = (payload as { text: string }).text;
          onEvent?.({ type: "answer", text: result.answerText });
          break;
        default:
          // Unknown event type - skip rather than crash. Future
          // T3+ event types slot in here without breaking
          // older clients.
          break;
      }
    }
  }

  return result;
}
