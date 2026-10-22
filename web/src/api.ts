// API client for the workspace backend. All routes are same-origin
// (FastAPI serves both /api/* and, after build, the SPA itself).
//
// The send path uses `fetch` + ReadableStream reader — NOT native
// EventSource — because EventSource cannot POST a body and cannot
// carry JSON. This matches spec ADR 0026.

import type {
  CatalogDimension,
  CatalogDocument,
  CatalogGroup,
  CatalogOverview,
  ChoiceWindow,
  Citation,
  Comparison,
  EvidenceRefusal,
  Filing,
  IngestResult,
  JobStatus,
  Notice,
  SoftTrack,
  SearchModeDecision,
  StreamErrorKind,
  ThreadAttachment,
  ThreadDetail,
  ThreadListItem,
  TrackDecision,
  VisualClaim,
  WatchEntry,
  WorkspaceStatus,
} from "./types";
import { parseChoiceWindow } from "./choiceWindow";
import {
  parseDocumentChoiceFrame,
  type DocumentChoiceFrame,
} from "./capabilities/task/taskRuntime";
import { decodeComparison } from "./comparison";
import {
  decodeCitationsArray,
  decodeEvidenceRefusalsFrame,
  decodeEvidenceSchemaField,
  decodeThreadDetail,
} from "./evidenceContract";
import { threadAttachmentUrl } from "./sourceLocator";
import { parseVisualClaimFrame } from "./capabilities/evidence/visualClaims";
import { parseWorkspaceStatus } from "./knowledgeMcp";
import { parseSoftTrack, parseTrackDecision } from "./softTrack";
import { parseSearchModeDecision } from "./capabilities/trace/searchModeReceipt";
import { parseStreamErrorKind } from "./streamErrorKind";
import {
  parseTurnSpansResponse,
  type TurnSpansResponse,
} from "./capabilities/trace/traceSpans";
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
  // The 知识 MCP gate refuses creation on a 缺 / 坏 boot (ADR 0051).
  // jsonOrThrow would leak the bare ``knowledge_mcp_fault`` token into
  // the operator-facing notice.
  if (res.status === 409) throw await threadConflictError(res);
  return jsonOrThrow<ThreadListItem>(res);
}

export async function getThread(threadId: string): Promise<ThreadDetail> {
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}`);
  // The knowledge-MCP gate also refuses OPENING an existing thread on a
  // 缺 / 坏 boot. Surfacing it as a plain 404 would tell the operator
  // the thread is gone when it is not (ADR 0051).
  if (res.status === 409) throw await threadConflictError(res);
  // A 503 on this route has TWO unrelated causes. The pending-question
  // projection failure answers a fixed FastAPI ``{"detail": …}`` body
  // (see ``_PENDING_QUESTION_UNAVAILABLE_DETAIL`` host-side); the thread
  // EXISTS and a plain transport error would read as not-found. But a
  // thread-store / run-list outage answers the named
  // ``{"error": "persistence_unavailable"}`` body — a DIFFERENT truth,
  // and one the route already reads as not-found. Discriminate on the
  // wire body (as the 409 branch above does) so a store outage is never
  // mislabelled as a live thread with an unreadable question. The class
  // carries the client's own fixed copy — the host refuses to echo
  // checkpoint internals and neither may this decode.
  if (res.status === 503 && (await isPendingQuestionRefusal(res.clone())))
    throw new PendingQuestionUnavailableError();
  // E6: the history body is ``unknown`` at runtime — strict tsc types
  // are compile-time only and never satisfy a decode (spec §6 web row).
  // The blind ``jsonOrThrow<ThreadDetail>`` cast is replaced by the
  // evidence guards: citation rows decode, ``rejected_citations`` rows
  // become visible refusals, and an unknown envelope token names its
  // class instead of serving an empty success.
  return decodeThreadDetail(await jsonOrThrow<unknown>(res));
}

export async function deleteThread(threadId: string): Promise<void> {
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

/**
 * One turn's trace spans (the developer TaskPanel waterfall).
 *
 * An empty trace is a 200 with ``spans: []`` — tracing was never enabled
 * is a normal state, not a refusal — so the decoder keeps the host's
 * ``trace_state`` beside the rows and the caller renders the receipt
 * fallback rather than an error.
 */
export async function getTurnSpans(
  threadId: string,
  turnId: string,
): Promise<TurnSpansResponse> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/turns/` +
      `${encodeURIComponent(turnId)}/spans`,
  );
  const parsed = parseTurnSpansResponse(await jsonOrThrow<unknown>(res));
  if (parsed === null) throw new Error("malformed /spans payload");
  return parsed;
}

export async function getCorpus(): Promise<Filing[]> {
  const res = await fetch("/api/corpus");
  const body = await jsonOrThrow<{ filings: Filing[] }>(res);
  return body.filings;
}

/** The GENERIC catalog surface (T6): documents with their title and type
 * plus the presentation grouping.
 *
 * A separate mouth from ``getCorpus``: that one is the FINANCIAL
 * compatibility projection whose six-key shape is frozen for its existing
 * callers, while an ordinary uploaded document has no company / period /
 * form at all. */
export async function getCatalogOverview(): Promise<CatalogOverview> {
  const res = await fetch("/api/corpus/overview");
  const body = await jsonOrThrow<unknown>(res);
  const parsed = parseCatalogOverview(body);
  if (parsed === null) throw new Error("malformed /api/corpus/overview payload");
  return parsed;
}

/** Where a grouping bucket puts a document with no value for that
 * dimension. Pinned on both sides so the SPA and the host cannot name the
 * same bucket differently. */
export const UNCLASSIFIED_LABEL = "未分类";

const GROUPING_KEYS = ["doc_type", "company", "period"] as const;

/** ``row[name]`` when it is a non-empty string, else ``undefined`` — one
 * rule so every identity field in this decoder is checked the same way. */
function nonEmptyString(
  row: Record<string, unknown>,
  name: string,
): string | undefined {
  const value = row[name];
  return typeof value === "string" && value ? value : undefined;
}

/** ``row[name]`` when it is an array of non-empty strings, else
 * ``undefined``. */
function nonEmptyStringList(
  row: Record<string, unknown>,
  name: string,
): string[] | undefined {
  const value = row[name];
  if (!Array.isArray(value)) return undefined;
  if (!value.every((entry) => typeof entry === "string" && entry)) return undefined;
  return value as string[];
}

/** The two OPTIONAL display fields of a document row, or ``null`` when
 * one of them is present but not a non-empty string. Both absent gives
 * the empty object, so a metadata-free document stays exactly that
 * instead of growing a ``company: null``. */
function optionalCatalogMetadata(
  row: Record<string, unknown>,
): Pick<CatalogDocument, "company" | "period"> | null {
  const metadata: Pick<CatalogDocument, "company" | "period"> = {};
  for (const field of ["company", "period"] as const) {
    const raw = row[field];
    if (raw === undefined) continue;
    const present = nonEmptyString(row, field);
    if (!present) return null;
    metadata[field] = present;
  }
  return metadata;
}

/** The five ALWAYS-present identity/display fields of a document row, or
 * ``null`` when any of them is missing or not a non-empty string. */
function requiredCatalogIdentity(
  row: Record<string, unknown>,
): Omit<CatalogDocument, "company" | "period"> | null {
  const identity = {
    document_id: nonEmptyString(row, "document_id"),
    version: nonEmptyString(row, "version"),
    title: nonEmptyString(row, "title"),
    doc_type: nonEmptyString(row, "doc_type"),
  };
  if (Object.values(identity).some((value) => value === undefined)) return null;
  if (typeof row.page_count !== "number") return null;
  return { ...identity, page_count: row.page_count } as Omit<
    CatalogDocument,
    "company" | "period"
  >;
}

/** One generic read-model row, or ``null`` when its shape does not hold. */
function parseCatalogDocument(entry: unknown): CatalogDocument | null {
  if (typeof entry !== "object" || entry === null) return null;
  const row = entry as Record<string, unknown>;
  const identity = requiredCatalogIdentity(row);
  if (!identity) return null;
  // A present-but-blank optional value is a refusal, not a silently
  // dropped field: a null would render as the word "null".
  const metadata = optionalCatalogMetadata(row);
  if (metadata === null) return null;
  return { ...identity, ...metadata };
}

/** One grouping bucket, or ``null`` when its shape does not hold. */
function parseCatalogGroup(entry: unknown): CatalogGroup | null {
  if (typeof entry !== "object" || entry === null) return null;
  const group = entry as Record<string, unknown>;
  const value = nonEmptyString(group, "value");
  const document_ids = nonEmptyStringList(group, "document_ids");
  if (!value || !document_ids) return null;
  return { value, document_ids };
}

/** One grouping dimension, or ``null`` when its shape does not hold. */
function parseCatalogDimension(entry: unknown): CatalogDimension | null {
  if (typeof entry !== "object" || entry === null) return null;
  const row = entry as Record<string, unknown>;
  const key = row.key;
  if (typeof key !== "string") return null;
  if (!(GROUPING_KEYS as readonly string[]).includes(key)) return null;
  const label = nonEmptyString(row, "label");
  const groups = parseRowList(row.groups, parseCatalogGroup);
  if (!label || !groups) return null;
  return { key: key as CatalogDimension["key"], label, groups };
}

/** Decode one list of rows through ``parse``, or ``null`` if any entry
 * fails — a list is decoded whole or not at all, so the surface never
 * renders half a corpus. */
function parseRowList<T>(
  raw: unknown,
  parse: (entry: unknown) => T | null,
): T[] | null {
  if (!Array.isArray(raw)) return null;
  const rows: T[] = [];
  for (const entry of raw) {
    const parsed = parse(entry);
    if (parsed === null) return null;
    rows.push(parsed);
  }
  return rows;
}

function parseCatalogOverview(value: unknown): CatalogOverview | null {
  if (typeof value !== "object" || value === null) return null;
  const body = value as Record<string, unknown>;
  const documents = parseRowList(body.documents, parseCatalogDocument);
  if (documents === null) return null;
  const dimensions = parseRowList(body.dimensions, parseCatalogDimension);
  if (dimensions === null) return null;
  if (!Array.isArray(body.flat_titles)) return null;
  if (!body.flat_titles.every((title) => typeof title === "string")) return null;
  return { documents, dimensions, flat_titles: body.flat_titles as string[] };
}

/**
 * T10 (ADR 0033, amended by ADR 0051): the boot-scoped status
 * surface. The host answers 200 for every **知识 MCP** state — 缺 /
 * 坏 / 可达 is a field value, not an HTTP error — so a rejection here
 * means the route itself is unreachable or malformed, which the
 * left-corner line renders as its own neutral state.
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

// ----- T4 workflow: watchlist, notices, stop -----

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
  const res = await fetch(`/api/notices/${encodeURIComponent(noticeId)}/read`, {
    method: "POST",
  });
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
  const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/stop`, {
    method: "POST",
  });
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
  // E6 (spec §6 web row): this run's per-row evidence refusals — the
  // decoded ``evidence_refusals`` frame entries plus anything a runtime
  // decode refused at this boundary. Visible, named, never a silent drop.
  evidence_refusals: EvidenceRefusal[];
  answerText: string;
  // T2: ordinary-person copy from the SSE ``status`` frame
  // (non-empty only on refuse / step_cap).
  assistantMessage: string;
  // Soft-Track: optional until the host peer lands SSE ``track``.
  track?: SoftTrack;
  track_decision?: TrackDecision;
  search_mode_decision?: SearchModeDecision;
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
// T1: ``thinking`` / ``answer`` frames are DELTAS (fragments
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
      // T4: the run's total thinking duration in ms, known
      // by stream end; drives the live turn's collapse label.
      thinking_ms?: number;
      // Soft-Track: optional until host SSE includes ``track``.
      track?: SoftTrack;
      track_decision?: TrackDecision;
      search_mode_decision?: SearchModeDecision;
      // Mid-flight stream error taxonomy; non-empty only on
      // refuse rows whose refusal came from the stream engine
      // (not the citation gate). Mirrors the SSE
      // ``status_classified`` field.
      status_classified?: StreamErrorKind | "";
      // A5: present only when the run stored insufficient_evidence.
      semantic_outcome?: string | null;
    }
  | { type: "answer"; text: string; reset?: boolean }
  | { type: "citations"; citations: Citation[] }
  // E6: the run's named per-row evidence refusals. New
  // event vocabulary minted at E5 — ``{"refusals": [{citation_index,
  // error}]}`` emitted BEFORE the terminal ``citations`` frame, which
  // closes the reader — entry shape mirroring the history read's
  // ``rejected_citations`` so stream and history fill one field.
  | { type: "evidence_refusals"; refusals: EvidenceRefusal[] }
  // The run's visual-claim rows, emitted on their OWN event after
  // the answer frames and before the terminal ``citations`` event (and
  // repeated on the GET history row). Provenance only — the row carries
  // no answer text, so a generated visual observation can never be placed
  // as a verbatim span by the chip logic.
  | { type: "visual_claims"; visual_claims: VisualClaim[] }
  // T3 (enterprise-kb Focus 1): one structured comparison table,
  // emitted right after the ``compare_facts`` tool returns. The
  // only structured payload on this wire — tool-computed data, not
  // model prose — so the live view can render the table before the
  // answer is authored.
  | { type: "comparison"; comparison: Comparison }
  // search-mode-host T2 (ADR 0062 HITL): the run suspended for an
  // operator pick. NO terminal status/answer/citations frames follow
  // on this stream — the turn resumes on POST /resume with a pick.
  // Dispatch is by the payload's own ``kind`` (carried on the window)
  // so a future Fast-propose window (T4) rides the same frame.
  | { type: "choice_window"; window: ChoiceWindow }
  // T2f graph job gate (review C4): the run suspended on the graph's
  // ``attachment_preparation`` interrupt. NO terminal frames follow on
  // this stream either, but unlike a choice window the wait needs no
  // operator action — the durable prep job auto-resumes the SAME run,
  // whose server-minted ``run_id`` the frame carries. Wire shape
  // (host ``workspace_http`` stream): ``{run_id, kind, attachment_ids}``.
  | {
      type: "attachment_wait";
      run_id: string;
      kind: string;
      attachment_ids: string[];
    }
  | {
      type: "document_choice";
      frame: DocumentChoiceFrame;
    }
  // The run suspended for a document choice (specs/qa-chain-grounded-experience.md
  // §GE-4: "≤4 hit-backed candidates have title/section/summary and
  // trailing rephrase"). NO terminal status/answer/citations frames follow
  // on this stream — the run resumes on the document-choice reply.
  //
  // This frame is a REFERENCE, not a second candidate list: it names the
  // committed ``question_id`` and the ``origin_run_id`` it interrupted, so
  // the client can mark THAT run suspended. The candidates themselves come
  // only from the authoritative projection
  // (``task_runtime.pending_question.choice_details``) — re-read on
  // receipt, so a reload and a live frame paint the identical question and
  // the reply binds to the original run/receipt.
  | { type: "document_choice"; frame: DocumentChoiceFrame };

/** The one interrupt kind the host's attachment wait rides — shared by
 * the SSE ``attachment_wait`` frame and the GET detail's
 * ``pending_interrupt_kind`` (mirrors
 * ``fin.host.graph.ATTACHMENT_PREPARATION_INTERRUPT_KIND``). */
export const ATTACHMENT_PREPARATION_KIND = "attachment_preparation";

export class ConcurrentRunError extends Error {}
export class IngestMissingError extends Error {}

/** The host's 409 knowledge-MCP conversation gate (create / open /
 * send). A DISTINCT type from ``ConcurrentRunError``: on a 缺 / 坏
 * boot the send is refused because the knowledge MCP cannot answer,
 * NOT because another run holds the thread — rendering it as a
 * concurrency conflict would be a wrong claim. Carries the host's
 * operator ``message`` so the copy can surface it verbatim. */
export class KnowledgeMcpFaultError extends Error {
  readonly code = "knowledge_mcp_fault";

  constructor(message: string) {
    super(message || "knowledge_mcp_fault");
    this.name = "KnowledgeMcpFaultError";
  }
}

/** The host's fixed copy for the pending-question 503 (mirrors
 * ``fin.host.workspace_http._PENDING_QUESTION_UNAVAILABLE_DETAIL``). The
 * SPA renders THIS string, never the wire body: the host deliberately
 * refuses to leak checkpoint / exception detail, and a client that
 * echoed the body would reintroduce the leak. */
export const PENDING_QUESTION_UNAVAILABLE_DETAIL =
  "Pending question is temporarily unavailable. Retry opening the thread.";

/** A 503 opening a thread whose paused choice-window checkpoint cannot be
 * projected. A DISTINCT type from a plain transport failure: the thread
 * still EXISTS and merely cannot be read this moment, so rendering it as
 * not-found (or echoing server internals) would both be wrong claims.
 * The caller is told to retry. Mirrors the ``KnowledgeMcpFaultError``
 * pattern (a named refusal, not a bare ``Error``). */
export class PendingQuestionUnavailableError extends Error {
  readonly code = "pending_question_unavailable";

  constructor(message = PENDING_QUESTION_UNAVAILABLE_DETAIL) {
    super(message);
    this.name = "PendingQuestionUnavailableError";
  }
}

/** True when a 503 body is the pending-question refusal shape. That
 * refusal rides FastAPI's ``HTTPException``, which serialises as
 * ``{"detail": …}``; the store-outage 503 answers ``{"error":
 * "persistence_unavailable"}`` instead and is NOT this shape. Defensive:
 * an unreadable / non-JSON body reads as not-pending, so the caller's
 * ordinary transport error still fires rather than a parse error. */
async function isPendingQuestionRefusal(res: Response): Promise<boolean> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body?.detail === "string";
  } catch {
    return false;
  }
}

/** The ``error`` token of a 409 body, or ``""`` when unreadable. */
// question-workspace W2/W5: the typed task refusal the send door
// raises before any run starts (``_task_input_http_error``). Two facts
// this surface must be able to keep apart, which a bare HTTP error
// cannot carry: WHICH question the rejected reply belonged to, and
// WHICH named failure refused it. The kind rides
// ``TASK_FAILURE_KINDS`` (specs/question-task-runtime-contract.md), so
// a stale reply is never rendered as a transport hiccup.
export class TaskInputRefusedError extends Error {
  readonly kind: string;
  /** ``detail.question_id`` — the question the rejected reply named,
   * preserved so a stale reply stays associated with its ORIGINAL
   * question instead of being rebased onto whatever is current now. */
  readonly questionId: string | null;

  constructor(kind: string, questionId: string | null = null) {
    super(`task input refused: ${kind}`);
    this.name = "TaskInputRefusedError";
    this.kind = kind;
    this.questionId = questionId;
  }
}

/** True when a 400/409 body is the task-door refusal shape: FastAPI's
 * ``HTTPException`` serialises as ``{"detail": {"error": <kind>}}``,
 * which is a DIFFERENT shape from the plain ``{"error": …}`` the lock
 * and 知识 MCP refusals use. Returns the refusal or ``null``, so the
 * caller keeps its historical handling of every other status. */
async function readTaskRefusal(
  res: Response,
): Promise<TaskInputRefusedError | null> {
  try {
    const body = (await res.json()) as {
      detail?: { error?: unknown; question_id?: unknown };
    };
    const detail = body?.detail;
    if (typeof detail?.error !== "string") return null;
    const questionId = detail.question_id;
    return new TaskInputRefusedError(
      detail.error,
      typeof questionId === "string" ? questionId : null,
    );
  } catch {
    return null;
  }
}

/** The ``error`` token of a 409 body, or ``""`` when unreadable. */async function conflictToken(res: Response): Promise<[string, string]> {
  try {
    const body = (await res.json()) as { error?: unknown; message?: unknown };
    return [
      typeof body?.error === "string" ? body.error : "",
      typeof body?.message === "string" ? body.message : "",
    ];
  } catch {
    return ["", ""];
  }
}

/** 409 on the SEND doors (messages / resume). Two refusals share the
 * status: a held run and the 知识 MCP gate. Every other (or
 * unparseable) 409 keeps the historical concurrent-run meaning — that
 * reading is true of these doors. */
async function sendConflictError(res: Response): Promise<Error> {
  const [token, message] = await conflictToken(res);
  if (token === "knowledge_mcp_fault") {
    return new KnowledgeMcpFaultError(message);
  }
  return new ConcurrentRunError("concurrent_run");
}

/** 409 on the THREAD doors (create / get). Only the gate is known to
 * refuse here, so an unrecognised token must not be reported as a
 * held run — 「还有一轮在运行」 would be a false attribution about a
 * brand-new thread. Falls back to the generic HTTP error instead. */
async function threadConflictError(res: Response): Promise<Error> {
  const [token, message] = await conflictToken(res);
  if (token === "knowledge_mcp_fault") {
    return new KnowledgeMcpFaultError(message);
  }
  return new Error(`HTTP ${res.status}: ${token || res.statusText}`);
}

/** Request body for the send path. ``thinking`` is the operator's
 * reasoning preference toggle (T3); the backend forwards it to the
 * model provider. */
export interface SendRequest {
  question: string;
  thinking?: boolean;
  // ADR 0086 / T2f: ids minted by the thread-attach
  // door. They are referenceable for the whole thread lifetime, so a
  // later send reuses the same ids. A reference to a durable
  // ``uploaded``/``parsing`` record is ACCEPTED: the graph suspends
  // the run on the attachment_preparation interrupt and resumes it
  // automatically when the durable job settles. The pre-T2f 400
  // ``attachment_not_ready`` refusal below is kept only for an older
  // host. This is the attach door's only
  // downstream — the attach path must never touch /api/corpus/ingest.
  attachment_ids?: string[];

  // question-workspace W2: answer the thread's still-pending
  // clarification. ``question_id`` names WHICH question is being
  // answered so the reply keeps its ORIGINAL association — the host
  // refuses a reply to a superseded question as ``task_reply_expired``
  // rather than answering whatever is current, so this is never
  // optionalised away by "the current one". ``choice_id`` is a real
  // choice id, and the reserved ``"other"`` token is one of them
  // rather than a sentinel; ``free_text`` carries what the operator
  // typed alongside Other, preserved verbatim. All three are absent on
  // an ordinary message.
  question_id?: string;
  choice_id?: string;
  free_text?: string;

  // question-workspace W5: an EXPLICIT restore of a listed archive.
  // Restore is never inferred from message text on this surface — the
  // archived results stay historical until the host's recheck passes,
  // and until the host confirms the current task is simply unchanged.
  restore_archive_id?: string;
}

/**
 * POST one question to a thread. Reads the SSE stream via fetch +
 * reader (NOT native EventSource) and resolves with the terminal
 * events. T3 surfaces streaming frames (thinking / tool_call /
 * tool_result) via the optional ``onEvent`` callback so the SPA
 * can render the tools region + scratchpad while the run is live.
 */
/** The pre-SSE named refusals of the send door, checked in wire order:
 * 409 (task refusal first, then lock/gate), 503 (ingest missing), 400
 * (task refusal first, then the pre-T2f still-parsing attachment).
 * Returns the error to throw, or null when the status needs the caller's
 * generic handling (204 no-op / non-OK transport error). Extracted from
 * ``sendMessage`` so the door's branch order stays one family. */
async function sendPreSseError(res: Response): Promise<Error | null> {
  // Error paths happen BEFORE any SSE frame per spec. The 409 body
  // carries the token: ``knowledge_mcp_fault`` on a 缺 / 坏 boot is a
  // different refusal from a held run and must not read as one.
  if (res.status === 409) {
    // question-workspace W2/W5: a refused clarification reply or an
    // un-adoptable restore rides the SAME status as a held run, so the
    // body discriminates them. Checked BEFORE the lock/gate reading so
    // a stale reply is never reported as 「还有一轮在运行」.
    const refusal = await readTaskRefusal(res.clone());
    if (refusal) return refusal;
    return sendConflictError(res);
  }
  if (res.status === 503) return new IngestMissingError("ingest_missing");
  // Attach door: the pre-T2f refusal of a run that requires a
  // still-parsing attachment is this one named pre-SSE refusal; every
  // other 400 keeps the generic transport error below. The task-door
  // refusal is the OTHER named 400, so it is read first and by shape:
  // an expired or malformed reply must never be reported as a
  // still-parsing attachment.
  if (res.status === 400) {
    const refusal = await readTaskRefusal(res.clone());
    if (refusal) return refusal;
    if ((await namedErrorCode(res)) === "attachment_not_ready") {
      return new AttachmentNotReadyError();
    }
  }
  return null;
}

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

  const preSseError = await sendPreSseError(res);
  if (preSseError) throw preSseError;
  if (res.status === 204) {
    // Empty submit no-op; caller should not have called this.
    // No SSE frames arrive on a 204, so there is no server-minted
    // run_id — the placeholder uuid keeps SendResult total.
    return {
      run_id: crypto.randomUUID(),
      status: "done",
      citations: [],
      evidence_refusals: [],
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

function insufficientEvidenceToken(value: unknown): string | undefined {
  return value === "insufficient_evidence" ? value : undefined;
}

/**
 * Shared SSE consumption loop for the send AND resume paths
 * (T4). Reads ``body`` frame-by-frame, dispatches each typed event
 * to ``onEvent``, and accumulates the terminal ``SendResult``.
 * Extracted from ``sendMessage`` so the resume path cannot drift
 * on frame parsing / error tolerance.
 */
type ParsedSseFrame = { eventName: string; payload: unknown };

interface SseStreamContext {
  result: SendResult;
  emit: (event: SendEvent) => void;
  sawStatus: boolean;
}

type SseFrameHandler = (payload: unknown, context: SseStreamContext) => void;

interface StatusFramePayload {
  run_id?: string;
  status: SendResult["status"];
  reason?: string;
  assistant_message?: string;
  thinking_ms?: number;
  track?: unknown;
  track_decision?: unknown;
  search_mode_decision?: unknown;
  status_classified?: string;
  semantic_outcome?: unknown;
  evidence_schema?: unknown;
  schema_version?: unknown;
}

function handleThinkingFrame(payload: unknown, { emit }: SseStreamContext): void {
  const thinking = payload as { text: string };
  emit({ type: "thinking", text: thinking.text ?? "" });
}

function handleToolCallFrame(payload: unknown, { emit }: SseStreamContext): void {
  const call = payload as {
    name: string;
    args: Record<string, unknown>;
    tool_call_id: string;
  };
  emit({
    type: "tool_call",
    name: call.name,
    args: call.args ?? {},
    tool_call_id: call.tool_call_id,
  });
}

function handleToolResultFrame(payload: unknown, { emit }: SseStreamContext): void {
  const result = payload as {
    tool_call_id: string;
    name: string;
    summary?: string;
  };
  emit({
    type: "tool_result",
    tool_call_id: result.tool_call_id,
    name: result.name,
    summary: result.summary ?? result.name,
  });
}

function applyStatusMetadata(
  status: StatusFramePayload,
  context: SseStreamContext,
): StreamErrorKind | "" {
  const { result } = context;
  const trackDecision = parseTrackDecision(status.track_decision);
  const track = parseSoftTrack(status.track) ?? trackDecision?.track;
  if (track !== undefined) result.track = track;
  if (trackDecision !== undefined) result.track_decision = trackDecision;
  result.search_mode_decision = parseSearchModeDecision(
    status.search_mode_decision,
  );
  const classified: StreamErrorKind | "" =
    parseStreamErrorKind(status.status_classified) ?? "";
  if (classified) result.status_classified = classified;
  emitEvidenceEnvelopeRefusal(status, context);
  return classified;
}

function emitEvidenceEnvelopeRefusal(
  status: StatusFramePayload,
  context: SseStreamContext,
): void {
  const envelope = decodeEvidenceSchemaField(
    status.evidence_schema,
    status.schema_version,
  );
  if (envelope !== "unsupported" && envelope !== "malformed") return;
  const refusal: EvidenceRefusal = {
    citation_index: 0,
    error:
      envelope === "unsupported"
        ? "unsupported_evidence_contract"
        : "malformed_evidence_contract",
  };
  context.result.evidence_refusals.push(refusal);
  context.emit({ type: "evidence_refusals", refusals: [refusal] });
}

function updateStatusRunId(status: StatusFramePayload, result: SendResult): void {
  if (typeof status.run_id === "string" && status.run_id.length > 0) {
    result.run_id = status.run_id;
  }
}

function handleStatusFrame(
  payload: unknown,
  context: SseStreamContext,
): void {
  const status = payload as StatusFramePayload;
  const { result, emit } = context;
  context.sawStatus = true;
  result.status = status.status;
  result.reason = status.reason ?? null;
  result.assistantMessage = status.assistant_message ?? "";
  const classified = applyStatusMetadata(status, context);
  updateStatusRunId(status, result);
  emit({
    type: "status",
    run_id: result.run_id,
    status: status.status,
    reason: status.reason ?? null,
    assistant_message: status.assistant_message ?? "",
    thinking_ms: status.thinking_ms,
    track: result.track,
    track_decision: result.track_decision,
    search_mode_decision: result.search_mode_decision,
    status_classified: classified,
    semantic_outcome: insufficientEvidenceToken(status.semantic_outcome),
  });
}

function handleCitationsFrame(
  payload: unknown,
  context: SseStreamContext,
): void {
  const frame = decodeCitationsArray(payload);
  context.result.citations = frame.citations;
  context.result.evidence_refusals = context.result.evidence_refusals.concat(
    frame.refusals,
  );
  context.emit({ type: "citations", citations: frame.citations });
  if (frame.refusals.length > 0) {
    context.emit({ type: "evidence_refusals", refusals: frame.refusals });
  }
}

function handleEvidenceRefusalsFrame(
  payload: unknown,
  context: SseStreamContext,
): void {
  const refusals = decodeEvidenceRefusalsFrame(payload);
  if (refusals.length === 0) return;
  context.result.evidence_refusals =
    context.result.evidence_refusals.concat(refusals);
  context.emit({ type: "evidence_refusals", refusals });
}

function handleVisualClaimsFrame(
  payload: unknown,
  context: SseStreamContext,
): void {
  context.emit({
    type: "visual_claims",
    visual_claims: parseVisualClaimFrame(payload),
  });
}

function handleComparisonFrame(
  payload: unknown,
  context: SseStreamContext,
): void {
  const outcome = decodeComparison(payload);
  if (outcome.status === "ok") {
    context.result.comparisons.push(outcome.comparison);
    context.emit({ type: "comparison", comparison: outcome.comparison });
    return;
  }
  const refusal: EvidenceRefusal = {
    citation_index: 0,
    error: outcome.error,
  };
  context.result.evidence_refusals.push(refusal);
  context.emit({ type: "evidence_refusals", refusals: [refusal] });
}

function handleChoiceWindowFrame(
  payload: unknown,
  { emit }: SseStreamContext,
): void {
  const window = parseChoiceWindow(payload);
  if (window !== null) emit({ type: "choice_window", window });
}

function handleAttachmentWaitFrame(
  payload: unknown,
  { emit }: SseStreamContext,
): void {
  const wait = payload as {
    run_id?: unknown;
    kind?: unknown;
    attachment_ids?: unknown;
  };
  if (
    typeof wait.run_id !== "string" ||
    wait.run_id.length === 0 ||
    wait.kind !== ATTACHMENT_PREPARATION_KIND
  ) {
    return;
  }
  emit({
    type: "attachment_wait",
    run_id: wait.run_id,
    kind: ATTACHMENT_PREPARATION_KIND,
    attachment_ids: Array.isArray(wait.attachment_ids)
      ? wait.attachment_ids.map((id) => String(id))
      : [],
  });
}

function handleAnswerFrame(
  payload: unknown,
  context: SseStreamContext,
): void {
  const answer = payload as { text: string; reset?: boolean };
  context.result.answerText = foldSendResultAnswer(
    context.result.answerText,
    answer.text ?? "",
    { statusSeen: context.sawStatus, reset: answer.reset },
  );
  context.emit({
    type: "answer",
    text: answer.text ?? "",
    reset: answer.reset === true,
  });
}

function handleDocumentChoiceFrame(
  payload: unknown,
  { emit }: SseStreamContext,
): void {
  const frame = parseDocumentChoiceFrame(payload);
  if (frame !== null) emit({ type: "document_choice", frame });
}

const SSE_FRAME_HANDLERS = new Map<string, SseFrameHandler>([
  ["thinking", handleThinkingFrame],
  ["tool_call", handleToolCallFrame],
  ["tool_result", handleToolResultFrame],
  ["status", handleStatusFrame],
  ["citations", handleCitationsFrame],
  ["evidence_refusals", handleEvidenceRefusalsFrame],
  ["visual_claims", handleVisualClaimsFrame],
  ["comparison", handleComparisonFrame],
  ["choice_window", handleChoiceWindowFrame],
  ["attachment_wait", handleAttachmentWaitFrame],
  ["answer", handleAnswerFrame],
  ["document_choice", handleDocumentChoiceFrame],
]);

function parseSseFrame(rawFrame: string): ParsedSseFrame | null {
  let eventName = "";
  const dataLines: string[] = [];
  for (const line of rawFrame.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!eventName || dataLines.length === 0) return null;
  try {
    return { eventName, payload: JSON.parse(dataLines.join("\n")) as unknown };
  } catch {
    // Malformed frames are skipped; the rest of the stream remains readable.
    return null;
  }
}

function takeCompleteSseFrames(buffer: string): {
  frames: string[];
  remainder: string;
} {
  const frames: string[] = [];
  let separator: number;
  while ((separator = buffer.indexOf("\n\n")) >= 0) {
    frames.push(buffer.slice(0, separator));
    buffer = buffer.slice(separator + 2);
  }
  return { frames, remainder: buffer };
}

function dispatchSseFrame(
  frame: ParsedSseFrame,
  context: SseStreamContext,
): void {
  const handler = SSE_FRAME_HANDLERS.get(frame.eventName);
  if (handler !== undefined) handler(frame.payload, context);
}

async function consumeSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent?: (event: SendEvent) => void,
): Promise<SendResult> {
  const context: SseStreamContext = {
    result: {
      run_id: crypto.randomUUID(),
      status: "done",
      reason: null,
      citations: [],
      evidence_refusals: [],
      answerText: "",
      assistantMessage: "",
      comparisons: [],
    },
    emit: onEvent ?? (() => {}),
    sawStatus: false,
  };
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const completed = takeCompleteSseFrames(buffer);
    buffer = completed.remainder;
    for (const rawFrame of completed.frames) {
      const frame = parseSseFrame(rawFrame);
      if (frame !== null) dispatchSseFrame(frame, context);
    }
  }

  return context.result;
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
 *
 * search-mode-host T2: ``pick`` is the choice-window option id, sent
 * as the JSON body ``{"pick": "<id>"}`` (host reads it
 * case-insensitively). OMITTING it keeps the historical body-less
 * call: with a window pending the host re-emits the ``choice_window``
 * frame — that is how a SPA reload re-opens the window.
 */
export async function resumeThread(
  threadId: string,
  onEvent?: (e: SendEvent) => void,
  pick?: string,
): Promise<SendResult> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/resume`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: pick === undefined ? undefined : JSON.stringify({ pick }),
    },
  );

  // Rejection order matches the route definition: every failure
  // resolves BEFORE the SSE header, so we read the response body
  // for the typed error and throw.
  if (res.status === 404) throw new Error("HTTP 404: thread_not_found");
  if (res.status === 409) throw await sendConflictError(res);
  if (res.status === 204) {
    // Nothing-to-resume no-op. Return an empty terminal result so
    // the caller still reconciles its state cleanly.
    return {
      run_id: crypto.randomUUID(),
      status: "done",
      citations: [],
      evidence_refusals: [],
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
 * answers a stable ``error`` code (422 metadata, 413 too large, 415
 * unsupported format, 409 concurrent_run, 404 unknown_job) with NO
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

/** A pre-T2f host refusing a run that requires a still-parsing
 * attachment (400 ``attachment_not_ready``, decided before any SSE
 * frame); a T2f host accepts the reference and waits inside the graph
 * instead, so this arrives only from an older peer. Subclasses
 * ``AttachFailedError`` so the composer renders it
 * through the attach door's own copy — the failure is about an
 * ATTACHMENT, not about the question. */
export class AttachmentNotReadyError extends AttachFailedError {
  constructor() {
    super("attachment_not_ready", 400);
    this.name = "AttachmentNotReadyError";
  }
}

/** A conversation start or send refused because an attachment upload is
 * still in flight (``attachment_upload_in_progress``). The two doors
 * cover different scopes on purpose: the client holds the composer while
 * an upload is in flight on ANY thread, because its composer is global —
 * one box that sends into whichever thread is open; the host refuses
 * only the thread whose bytes are landing, which is what stops a second
 * tab from slipping a send past that upload. The client is the stricter
 * side. Subclasses ``AttachFailedError`` for the same reason as
 * ``AttachmentNotReadyError``: the refusal is about an attachment. */
export class AttachmentUploadInProgressError extends AttachFailedError {
  constructor() {
    super("attachment_upload_in_progress", 409);
    this.name = "AttachmentUploadInProgressError";
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
    return `http_${res.status}`;
  }
  return `http_${res.status}`;
}

/** Optional metadata for the ingest door.
 *
 * Every field is descriptive: the upload identity is derived from the
 * trusted corpus scope and the content bytes, never from the filename or
 * this metadata, so the client has no mode flag and no way to supply a
 * caller-chosen ``document_id``. ``title`` names the document (otherwise
 * the sanitized filename does); ``doc_type`` and ``format`` override the
 * server's classification. */
export interface IngestMeta {
  company?: string;
  period?: string;
  doc_type?: string;
  format?: string;
  title?: string;
}

/**
 * The corpus-ingest door (ADR 0047): multipart POST /api/corpus/ingest
 * — the ONLY client surface that triggers 切分 + MCP push. Named
 * failures throw ``IngestFailedError`` with the wire code; success is
 * an ACCEPT plus a ``job_id``, NOT a completed ingest — parse / 切分 /
 * push run off the request, so the outcome is followed with
 * ``getIngestJobStatus``.
 */
export async function ingestCorpus(
  file: File,
  meta: IngestMeta,
): Promise<IngestResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  // Only NON-EMPTY optional metadata is appended: a blank string would
  // otherwise be sent as ``company=""`` and read server-side as a
  // present-but-blank value rather than "not provided".
  if (meta.company) form.append("company", meta.company);
  if (meta.period) form.append("period", meta.period);
  if (meta.format) form.append("format", meta.format);
  if (meta.doc_type) form.append("doc_type", meta.doc_type);
  if (meta.title) form.append("title", meta.title);
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
 * Status read for one accepted corpus job: GET /api/corpus/ingest/{job_id}.
 * The accept response only says the upload is durable, so this is the
 * single place the SPA learns the job's real state. An id with no job
 * is the named ``unknown_job`` miss — never an empty status that looks
 * like a still-queued job.
 */
export async function getIngestJobStatus(jobId: string): Promise<JobStatus> {
  const res = await fetch(`/api/corpus/ingest/${encodeURIComponent(jobId)}`);
  if (!res.ok) {
    throw new IngestFailedError(await namedErrorCode(res), res.status);
  }
  return (await res.json()) as JobStatus;
}

/**
 * The thread-attach door (ADR 0047): raw-bytes POST with the filename
 * as a query parameter — deliberately NOT multipart: this door ships
 * one undecorated blob (no form fields), so a plain ``fetch`` body is
 * the whole contract and neither side needs multipart parsing. The
 * minted ``attachment_id`` is referenceable for the WHOLE thread
 * lifetime (ADR 0089): the POST answers once the bytes are DURABLE and
 * starts nothing — no classification, OCR, layout, vision or indexing —
 * so the 200 carries ``status: "uploaded"``. Preparation begins from a
 * grounded graph turn; the caller follows the lifecycle through the GET
 * doors below. Nothing is pushed to the corpus.
 */
export async function attachToThread(
  threadId: string,
  file: File,
): Promise<ThreadAttachment> {
  // Raw bytes, not file.text(): the door stores whatever it is given.
  // Content the parsers cannot read fails LATER, in preparation, with
  // its own named code — never as an upload refusal.
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

/**
 * The thread's attachment list — the authoritative readiness view
 * (statuses, versions, failure reasons) the SPA polls while anything
 * is still ``parsing``. An unknown thread answers a named failure,
 * not an empty list pretending the thread was clean.
 */
export async function listThreadAttachments(
  threadId: string,
): Promise<ThreadAttachment[]> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/attachments`,
  );
  if (!res.ok) {
    throw new AttachFailedError(await namedErrorCode(res), res.status);
  }
  const body = (await res.json()) as { attachments: ThreadAttachment[] };
  return body.attachments;
}

/**
 * Re-run preparation for one failed attachment. Preparation runs INSIDE
 * this request, so the 200 is the settled single record — ``"ready"``
 * when the re-drive worked, ``"failed"`` with its named reason when it
 * did not; this door never answers ``"parsing"``. Its named refusals:
 *
 * * 400 ``invalid_locator`` — a malformed path identity is refused before any
 *   lookup or preparation, so garbage text never reaches the store as a query.
 * * 404 ``thread_not_found`` — no such thread, so nothing was re-driven.
 * * 400 ``attachment_not_found`` — an unknown or foreign attachment id, or a
 *   thread that went away while the re-drive ran. One code for all of them,
 *   so this door cannot probe what another thread holds.
 * * 500 ``attachment_store_failed`` — the durable store refused the write.
 *
 * ``invalid_locator`` is the shared locator guard of the host's three
 * single-attachment doors: its status door answers it the same way, and the
 * DELETE below answers it instead of ``attachment_not_found``.
 */
export async function retryThreadAttachment(
  threadId: string,
  attachmentId: string,
): Promise<ThreadAttachment> {
  const res = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/attachments` +
      `/${encodeURIComponent(attachmentId)}/retry`,
    { method: "POST" },
  );
  if (!res.ok) {
    throw new AttachFailedError(await namedErrorCode(res), res.status);
  }
  return (await res.json()) as ThreadAttachment;
}

/** The host's acknowledgement of a completed single-attachment deletion. */
export interface AttachmentDeleteReceipt {
  ok: true;
  attachment_id: string;
}

/**
 * Remove ONE attachment from a thread: DELETE
 * /api/threads/{thread_id}/attachments/{attachment_id}. The host voids the
 * attachment's access before it touches any byte, so each named refusal says
 * something the operator acts on differently:
 *
 * * 400 ``invalid_locator`` — a malformed path identity, refused before any
 *   lookup, as the host's status and retry doors both do; nothing was voided
 *   and nothing is owed.
 * * 404 ``thread_not_found`` — the call never named a real thread; likewise
 *   nothing was voided and nothing is owed.
 * * 400 ``unknown_locator`` — nothing left to delete here: the id was never
 *   this thread's, or its deletion already finished. This door's own miss;
 *   the host's status and retry doors say ``attachment_not_found`` instead.
 * * 500 ``attachment_cleanup_failed`` — access IS voided, the raw bytes are
 *   still owed, and retrying this same door is what finishes it.
 * * 500 ``attachment_store_failed`` — the host could not even record the
 *   void.
 *
 * The caller must not treat a refusal as "deleted": only the 200 receipt is
 * confirmation.
 */
export async function deleteThreadAttachment(
  threadId: string,
  attachmentId: string,
): Promise<AttachmentDeleteReceipt> {
  const res = await fetch(threadAttachmentUrl(threadId, attachmentId), {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new AttachFailedError(await namedErrorCode(res), res.status);
  }
  return (await res.json()) as AttachmentDeleteReceipt;
}
