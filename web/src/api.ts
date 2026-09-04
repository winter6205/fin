// API client for the workspace backend. All routes are same-origin
// (FastAPI serves both /api/* and, after build, the SPA itself).
//
// The send path uses `fetch` + ReadableStream reader — NOT native
// EventSource — because EventSource cannot POST a body and cannot
// carry JSON. This matches spec ADR 0026.

import type {
  Citation,
  Comparison,
  Filing,
  IngestResult,
  Notice,
  SoftTrack,
  StreamErrorKind,
  ThreadAttachment,
  ThreadDetail,
  ThreadListItem,
  TrackDecision,
  WatchEntry,
  WorkspaceStatus,
} from "./types";
import { parseComparison } from "./comparison";
import { parseSoftTrack, parseTrackDecision } from "./softTrack";
import { parseWorkspaceStatus } from "./statusStrip";
import { parseStreamErrorKind } from "./streamErrorKind";
import { foldSendResultAnswer } from "./turnStream";
import { parseNotices, parseWatchlist } from "./watchlist";

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
  firstMessage: string | null,
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

/**
 * T10 (ADR 0033): the boot-scoped status surface. The host answers
 * 200 in both retrieve states — ``unreachable`` is a field value, not
 * an HTTP error — so a rejection here means the route itself is
 * unreachable or malformed, which the store renders as its own
 * degraded strip state.
 */
export async function getStatus(): Promise<WorkspaceStatus> {
  const res = await fetch("/api/status");
  const body = await jsonOrThrow<unknown>(res);
  const parsed = parseWorkspaceStatus(body);
  if (parsed === null) throw new Error("malformed /api/status payload");
  return parsed;
}

export class FilingMonitorDegradedError extends Error {
  readonly code: string;
  readonly notices: Notice[];

  constructor(code: string, notices: Notice[]) {
    super(`filing monitor degraded: ${code}`);
    this.name = "FilingMonitorDegradedError";
    this.code = code;
    this.notices = notices;
  }
}

// ----- T4 workflow (the tracked change): watchlist, notices, stop -----

export async function listWatchlist(): Promise<WatchEntry[]> {
  const res = await fetch("/api/watchlist");
  return parseWatchlist(await jsonOrThrow<unknown>(res));
}

export async function createWatch(
  issuer: string,
  forms: string[],
): Promise<WatchEntry[]> {
  const res = await fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ issuer, forms }),
  });
  // A blank issuer is a typed 400 the caller renders as form
  // validation; ``jsonOrThrow`` surfaces the ``error`` code verbatim.
  await jsonOrThrow<unknown>(res);
  return listWatchlist();
}

export async function deleteWatch(watchId: string): Promise<void> {
  const res = await fetch(`/api/watchlist/${encodeURIComponent(watchId)}`, {
    method: "DELETE",
  });
  await jsonOrThrow<unknown>(res);
}

export async function listNotices(): Promise<Notice[]> {
  // The host reconciles the watchlist against both the local catalog
  // and the configured filing source on this read. The response stays
  // an array for compatibility; headers carry the source health.
  const res = await fetch("/api/notices");
  const notices = parseNotices(await jsonOrThrow<unknown>(res));
  const monitorStatus = res.headers.get("x-filing-monitor-status");
  if (monitorStatus === "degraded") {
    throw new FilingMonitorDegradedError(
      res.headers.get("x-filing-monitor-error") ?? "filing_monitor_error",
      notices,
    );
  }
  return notices;
}

export async function markNoticeRead(noticeId: string): Promise<void> {
  const res = await fetch(
    `/api/notices/${encodeURIComponent(noticeId)}/read`,
    { method: "POST" },
  );
  await jsonOrThrow<unknown>(res);
}

/**
 * Stop the in-flight run on a thread. Resolves ``true`` when a run was
 * signalled and ``false`` on the 204 no-op (the run ended between the
 * click and the request) — neither is an error the operator must act
 * on. The stopped run's own ``sendMessage`` promise still resolves
 * with the terminal ``refuse`` / ``stopped`` frames.
 */
export async function stopRun(threadId: string): Promise<boolean> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/stop`,
    { method: "POST" },
  );
  if (res.status === 204) return false;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return true;
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
  // Soft-Track: optional until the host peer lands SSE ``track``.
  track?: SoftTrack;
  track_decision?: TrackDecision;
  // Mid-flight stream error taxonomy on refuse rows. Empty when
  // the run did not end on the stream-engine failure path; the
  // SPA renders a small "原因" label off this field so a 401 /
  // 429 / 504 reads distinctly.
  status_classified?: StreamErrorKind | "";
  // T3 (enterprise-kb Focus 1): the structured comparison tables
  // the run produced, in call order. Populated from the
  // ``comparison`` SSE frames, so the terminal result agrees with
  // what the live view already painted.
  comparisons: Comparison[];
}

// T3 streaming events surfaced to the caller as they arrive.
// The store threads each one into the optimistic turn so the
// tools region + scratchpad fill in while the run is live.
// T1 (#33): ``thinking`` / ``answer`` frames are DELTAS (fragments
// to concatenate); the authoritative full answer arrives on the
// terminal ``answer`` frame AFTER ``status`` (fixed server order).
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
      summary: string;
    }
  | {
      type: "status";
      run_id: string;
      status: SendResult["status"];
      reason?: string | null;
      assistant_message: string;
      // T4 (#37): the run's total thinking duration in ms, known
      // by stream end; drives the live turn's collapse label.
      thinking_ms?: number;
      // Soft-Track: optional until host SSE includes ``track``.
      track?: SoftTrack;
      track_decision?: TrackDecision;
      // Mid-flight stream error taxonomy; non-empty only on
      // refuse rows whose refusal came from the stream engine
      // (not the citation gate). Mirrors the SSE
      // ``status_classified`` field.
      status_classified?: StreamErrorKind | "";
    }
  | { type: "answer"; text: string; reset?: boolean }
  | { type: "citations"; citations: Citation[] }
  // T3 (enterprise-kb Focus 1): one structured comparison table,
  // emitted right after the ``compare_facts`` tool returns. The
  // only structured payload on this wire — tool-computed data, not
  // model prose — so the live view can render the table before the
  // answer is authored.
  | { type: "comparison"; comparison: Comparison };

export class ConcurrentRunError extends Error {}
export class IngestMissingError extends Error {}

/** Request body for the send path. ``thinking`` is the operator's
 * reasoning preference toggle (T3); the backend forwards it to the
 * model provider. */
export interface SendRequest {
  question: string;
  thinking?: boolean;
  // corpus-serve-ingest T5 (ADR 0047 **thread attach**): ids minted
  // by POST /threads/{id}/attachments, referenced ONCE by this send.
  // This is the attach door's only downstream — the attach path must
  // never touch /api/corpus/ingest.
  attachment_ids?: string[];
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
  onEvent?: (e: SendEvent) => void,
): Promise<SendResult> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    },
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
      comparisons: [],
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

  return consumeSseStream(res.body, onEvent);
}

/**
 * Shared SSE consumption loop for the send AND resume paths
 * (T4). Reads ``body`` frame-by-frame, dispatches each typed event
 * to ``onEvent``, and accumulates the terminal ``SendResult``.
 * Extracted from ``sendMessage`` so the resume path cannot drift
 * on frame parsing / error tolerance.
 */
async function consumeSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent?: (e: SendEvent) => void,
): Promise<SendResult> {
  const result: SendResult = {
    // Run id starts as a local placeholder; the ``status`` SSE
    // frame carries the server-minted id and overwrites it.
    run_id: crypto.randomUUID(),
    status: "done",
    reason: null,
    citations: [],
    answerText: "",
    assistantMessage: "",
    comparisons: [],
  };

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // T1 (#33): answer frames are DELTAS while the run streams; the
  // post-``status`` frame is the authoritative full text (replace);
  // ``reset:true`` replaces with accompanying ``text`` (bounce retry
  // first delta, or empty clear for terminal refuse flush).
  let sawStatus = false;

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
          // T1 (#33): token deltas — each frame's ``text`` is a
          // FRAGMENT of the run's thinking prose; the store
          // concatenates. The prose never participates in citation
          // matching (SC60 binding).
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
          // T3 wire shape: ``{name, summary, tool_call_id}`` — the
          // server-formatted one-liner, never raw ToolMessage JSON.
          const tr = payload as {
            tool_call_id: string;
            name: string;
            summary?: string;
          };
          onEvent?.({
            type: "tool_result",
            tool_call_id: tr.tool_call_id,
            name: tr.name,
            summary: tr.summary ?? tr.name,
          });
          break;
        }
        case "status": {
          // The status frame carries the server-minted ``run_id``
          // so the optimistic turn the store renders can match
          // the persisted RunRecord BEFORE any re-GET. T2 adds
          // ``assistant_message`` (ordinary-person copy on
          // refuse / step_cap). Soft-Track may add ``track`` /
          // ``track_decision``; both are optional until the peer
          // lands. The mid-flight stream taxonomy (``status_classified``)
          // rides the same frame on refuse rows.
          sawStatus = true;
          const s = payload as {
            run_id?: string;
            status: SendResult["status"];
            reason?: string;
            assistant_message?: string;
            thinking_ms?: number;
            track?: unknown;
            track_decision?: unknown;
            status_classified?: string;
          };
          result.status = s.status;
          result.reason = s.reason ?? null;
          result.assistantMessage = s.assistant_message ?? "";
          const trackDecision = parseTrackDecision(s.track_decision);
          const track = parseSoftTrack(s.track) ?? trackDecision?.track;
          if (track !== undefined) result.track = track;
          if (trackDecision !== undefined)
            result.track_decision = trackDecision;
          const classified = parseStreamErrorKind(s.status_classified) ?? "";
          if (classified) result.status_classified = classified;
          if (typeof s.run_id === "string" && s.run_id.length > 0) {
            result.run_id = s.run_id;
          }
          onEvent?.({
            type: "status",
            run_id: result.run_id,
            status: s.status,
            reason: s.reason ?? null,
            assistant_message: s.assistant_message ?? "",
            thinking_ms: s.thinking_ms,
            track: result.track,
            track_decision: result.track_decision,
            status_classified: classified,
          });
          break;
        }
        case "citations":
          result.citations = payload as SendResult["citations"];
          onEvent?.({ type: "citations", citations: result.citations });
          break;
        case "comparison": {
          // T3: one ``fin.comparison.v1`` envelope. Parsed at this
          // boundary (the wire is ``unknown``) so a payload from a
          // future schema renders nothing instead of a half-drawn
          // table; the frame is skipped exactly like a malformed
          // one.
          const comparison = parseComparison(payload);
          if (comparison !== null) {
            result.comparisons.push(comparison);
            onEvent?.({ type: "comparison", comparison });
          }
          break;
        }
        case "answer": {
          const a = payload as { text: string; reset?: boolean };
          result.answerText = foldSendResultAnswer(
            result.answerText,
            a.text ?? "",
            { statusSeen: sawStatus, reset: a.reset },
          );
          onEvent?.({
            type: "answer",
            text: a.text ?? "",
            reset: a.reset === true,
          });
          break;
        }
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

/**
 * T4 (spec line 39): resume an interrupted run from the last
 * complete node. Mirrors ``sendMessage``'s SSE shape exactly so
 * the store can drive the same ``onEvent`` path; the only
 * differences are the route (``POST /resume`` instead of
 * ``/messages``) and the 204 / 409 / 404 / 503 rejection handling
 * which all happen BEFORE any SSE header.
 *
 * The store is responsible for the switch-thread guard: if the
 * operator has navigated to a different thread by the time the
 * resumed stream finishes, the store must NOT paint the result
 * onto the new thread.
 */
export async function resumeThread(
  threadId: string,
  onEvent?: (e: SendEvent) => void,
): Promise<SendResult> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/resume`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );

  // Rejection order matches the route definition: every failure
  // resolves BEFORE the SSE header, so we read the response body
  // for the typed error and throw.
  if (res.status === 404) throw new Error("HTTP 404: thread_not_found");
  if (res.status === 409) throw new ConcurrentRunError("concurrent_run");
  if (res.status === 204) {
    // Nothing-to-resume no-op. Return an empty terminal result so
    // the caller still reconciles its state cleanly.
    return {
      run_id: crypto.randomUUID(),
      status: "done",
      citations: [],
      answerText: "",
      assistantMessage: "",
      comparisons: [],
    };
  }
  if (res.status === 503) throw new IngestMissingError("ingest_missing");
  if (!res.ok || !res.body) {
    throw new Error(`HTTP ${res.status}`);
  }

  return consumeSseStream(res.body, onEvent);
}

// ----- corpus-serve-ingest T5: the two upload doors (ADR 0047) -----

/**
 * Named failure of the corpus-ingest door. Every ingest failure
 * answers a stable ``error`` code (4xx input, 413 too large, 415
 * unsupported format, 503 push/invalidation outage) with NO
 * partial-success 200 (SC7/SC8); the SPA renders copy keyed off
 * ``code`` (see ingestFailure.ts) instead of a bare enum.
 */
export class IngestFailedError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(`HTTP ${status}: ${code}`);
    this.name = "IngestFailedError";
    this.code = code;
    this.status = status;
  }
}

/** Named failure of the thread-attach door (``empty_attachment`` /
 * ``attachment_too_large`` / ``attachment_not_text`` / ...). Kept a
 * DISTINCT class from IngestFailedError so an attach failure can
 * never render as ingest business (SC4). */
export class AttachFailedError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(`HTTP ${status}: ${code}`);
    this.name = "AttachFailedError";
    this.code = code;
    this.status = status;
  }
}

/** Read the named ``error`` code off a failed response body; an
 * unparseable body still yields a non-empty code (statusText is
 * often empty on fetch errors, so the status itself is the code). */
async function namedErrorCode(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: unknown };
    if (typeof body?.error === "string" && body.error) return body.error;
  } catch {
    // fall through to the status-derived code.
  }
  return `http_${res.status}`;
}

/** Optional locator metadata for the ingest door. All optional: the
 * endpoint derives ``document_id`` from company+period or the
 * filename, and classifies doc_type per unit when omitted. */
export interface IngestMeta {
  document_id?: string;
  company?: string;
  period?: string;
  doc_type?: string;
  format?: string;
}

/**
 * The corpus-ingest door (ADR 0047): multipart POST /api/corpus/ingest
 * — the ONLY client surface that triggers 切分 + MCP push. Named
 * failures throw ``IngestFailedError`` with the wire code; success
 * resolves the push summary (the store refreshes the corpus list).
 */
export async function ingestCorpus(
  file: File,
  meta: IngestMeta = {},
): Promise<IngestResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (meta.document_id) form.append("document_id", meta.document_id);
  if (meta.company) form.append("company", meta.company);
  if (meta.period) form.append("period", meta.period);
  if (meta.doc_type) form.append("doc_type", meta.doc_type);
  if (meta.format) form.append("format", meta.format);
  const res = await fetch("/api/corpus/ingest", {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    throw new IngestFailedError(await namedErrorCode(res), res.status);
  }
  return (await res.json()) as IngestResult;
}

/**
 * The thread-attach door (ADR 0047): raw-bytes POST with the filename
 * as a query parameter — deliberately NOT multipart (the host serves
 * this door without the python-multipart dependency). The returned
 * ``attachment_id`` is referenced ONCE on the next send via
 * ``SendRequest.attachment_ids``; nothing is pushed to the corpus.
 */
export async function attachToThread(
  threadId: string,
  file: File,
): Promise<ThreadAttachment> {
  // Raw bytes, not file.text(): the host decodes UTF-8 itself and
  // answers the named 400 attachment_not_text for undecodable bytes.
  const bytes = new Uint8Array(await file.arrayBuffer());
  const url =
    `/api/threads/${encodeURIComponent(threadId)}/attachments` +
    `?filename=${encodeURIComponent(file.name)}`;
  const res = await fetch(url, { method: "POST", body: bytes });
  if (!res.ok) {
    throw new AttachFailedError(await namedErrorCode(res), res.status);
  }
  return (await res.json()) as ThreadAttachment;
}
