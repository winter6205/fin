// Zustand store for the workspace UI. T1 keeps the state minimal:
// thread list + corpus + the active thread's turns. Per-thread run
// bookkeeping (in-flight flags, streaming deltas) is T3.

import { create } from "zustand";
import * as api from "./api";
// T8: the thumbs-feedback boundary (POST .../runs/{run_id}/feedback).
import { submitFeedback, type FeedbackValue } from "./feedback";
// T5: the last opened thread id, persisted so a returning
// visit lands back on that thread.
import { forgetLastThreadIf, rememberLastThread } from "./lastThread";
import {
  hasPendingChoiceWindow,
  threadHasPendingChoice,
} from "./choiceWindow";
import type {
  CatalogOverview,
  ChoiceWindow,
  Filing,
  IngestJobState,
  IngestResult,
  JobStatus,
  Notice,
  PendingQuestionView,
  ThreadAttachment,
  ThreadDetail,
  ThreadListItem,
  ToolSummary,
  Turn,
  ArchivedTaskSummary,
  WatchEntry,
  WorkspaceStatus,
} from "./types";
import {
  EMPTY_LIVE_ANSWER,
  foldLiveAnswer,
  mergeReconciledTurn,
  type LiveAnswerState,
} from "./turnStream";
import { mergeEvidenceRefusals } from "./evidenceContract";
import {
  closeOpenBursts,
  foldTraceFrame,
  RUNNING_SUMMARY,
  type TraceFrame,
} from "./turnTrace";
import { makeLiveTurn } from "./liveTurn";
// question-workspace: the task-door decision seam. The store owns only
// the wiring — which request went out and what the host answered — while
// every judgement (is this reply still the current question? did the
// task change?) lives in these pure modules so it is testable without a
// DOM.
import {
  decideActiveInput,
  heldRefusalFor,
  type ActiveInputDecision,
  type ActiveInputFacts,
} from "./activeInput";
import {
  applyWorkspaceOperationResult,
  beginWorkspaceOperation,
  currentWorkspaceOperation,
  EMPTY_WORKSPACE_OPERATIONS,
  isWorkspaceOperationDisplayable,
  type WorkspaceOperation,
  type WorkspaceOperationClass,
  type WorkspaceOperationLedger,
  type WorkspaceOperationResult,
} from "./workspaceOperation";
import {
  restoreIsListed,
  type RestoreDecisionInput,
  type RestoreRequestRef,
} from "./archiveRestore";
import {
  CLARIFICATION_DRAFT_STORAGE_KEY,
  draftRecords,
  parseDraftRecords,
  restoreDraftText,
  type ClarificationDraftRecords,
} from "./capabilities/clarification/clarificationReply";
import {
  applyRefusal,
  dismissClarification,
  isSubmittable,
  reopenClarification,
  selectChoice,
  setOtherText,
  submitPayload,
  type ClarificationDraft,
} from "./capabilities/clarification/clarificationReply";
import {
  decodeThreadTaskRuntime,
  type DocumentChoiceFrame,
  type TaskRuntimeDecodeRefusal,
} from "./capabilities/task/taskRuntime";
import type { SubmissionNotice } from "./capabilities/task/taskState";
import { isTaskFailureKind, type TaskRuntimeView } from "./types";
import { threadEntryIsLive } from "./threadEntry";

/** The refusal the thread's current operation carries, with the door that
 * recorded it. A failure kind is only meaningful against its own door's
 * copy (a ``task_reply_expired`` is a clarification-reply fact, not a
 * restore fact), so the door travels with the kind and a surface may render
 * only its own door's refusals. Derived from the operation record, never
 * stored beside it: one association vocabulary, one source. */
export interface WorkspaceOperationRefusal {
  kind: string;
  questionId: string | null;
  door: WorkspaceOperationClass;
}

/** The thread's own record of an archive restore the operator asked for.
 *
 * It owns the host's answer about THAT restore (``refusalKind``) as well as
 * the request's own progress: the operation ledger keeps at most one current
 * operation per thread, so a restore's verdict read from there would be
 * erased by the next door the operator opens.
 *
 * Beyond the printable request facts it also carries ``seenSinceSend``:
 * whether a projection read landed AFTER this request went out and neither
 * failed on the wire nor failed to decode. That is the whole of what "fresh"
 * means to ``restoreDecision`` — the read's own success, not the mere
 * passage of time — and it is a store fact because only the store knows the
 * order of its own reads. */
interface RestoreRequestRecord extends RestoreRequestRef {
  seenSinceSend: boolean;
}

/**
 * What an ``openThread`` call did.
 *
 * T2 (plans/2026-10-20-qa-design-correction.md): the entry read is
 * NOT unconditional. Three outcomes, and the route can rely on them:
 *
 * - ``opened`` — the thread was read and the turn table reconciled.
 * - ``skipped`` — this client is sending on that thread, so the live
 *   send owns it: no read was made, no loading branch was entered and
 *   no live stream state was replaced. The route reads the same rule
 *   (``threadEntryIsLive``) and paints the live answer instead of its
 *   entry loader.
 * - ``superseded`` — a LATER entry took over while this read was in
 *   flight, so this read did not touch the active thread.
 */
export type ThreadOpenOutcome =
  | { kind: "opened"; threadId: string }
  | { kind: "skipped"; threadId: string; reason: "sending" }
  | { kind: "superseded"; threadId: string };

interface WorkspaceState {
  threads: ThreadListItem[];
  corpus: Filing[];
  activeThread: ThreadDetail | null;
  loadingThreads: boolean;
  // T5: true once the FIRST refreshThreads completed. The
  // restore-on-return logic must distinguish "not loaded yet"
  // (initial ``loadingThreads === false`` before App's effect
  // starts the fetch) from "loaded and genuinely empty" — otherwise
  // the one-shot restore treats a valid marker as stale.
  threadsLoaded: boolean;
  corpusLoaded: boolean;
  // T6: the generic catalog overview behind the home corpus surface.
  // ``null`` until the first read answers (the door renders its loading
  // copy rather than claiming an empty catalog); ``catalogOverviewLoaded``
  // keeps "not read yet" apart from "read and genuinely empty", the same
  // distinction ``corpusLoaded`` makes.
  catalogOverview: CatalogOverview | null;
  catalogOverviewLoaded: boolean;
  // T10 (ADR 0033): the boot-scoped status slice behind the
  // left-corner 知识 MCP line. ``null`` after ``statusLoaded`` means
  // the status route itself did not answer — the line renders its
  // neutral state rather than nothing.
  status: WorkspaceStatus | null;
  statusLoaded: boolean;
  // ``true`` while a GET /api/status is in flight, so a concurrent
  // second dispatch is ignored (one wire call per boot/refresh).
  loadingStatus: boolean;
  sendingQuestion: boolean;
  // T2 (plans/2026-10-20-qa-design-correction.md): the thread THIS
  // CLIENT is currently sending on. ``sendingQuestion`` says "a send is
  // running" but not WHERE, and the thread-entry contract needs the
  // thread: only the thread that is streaming may reuse the live
  // answer, and a read for it would replace that answer with a stale
  // snapshot. Set SYNCHRONOUSLY when the send starts — before its
  // first await — so a route mount right after ``sendQuestion(...)``
  // already sees it; cleared when that send settles (success, failure
  // or cancellation). ``null`` when no send is in flight.
  sendingThreadId: string | null;
  // T2: the token of the entry read that is the CURRENT one. The route
  // effect and the thread page both open the same thread; a switch
  // opens another one. A read that a LATER entry superseded must not
  // repaint the active thread the operator already left. The marker
  // holds a per-read TOKEN, not the thread id: two concurrent reads of
  // the SAME thread are only orderable with a token, and the marker is
  // released when its read settles.
  enteringReadToken: number | null;
  // T4: ``resumeThread(threadId)`` sets this while the resume SSE
  // stream is active so the Composer can disable itself and so a
  // second resume click is rejected. ``null`` when no resume is in
  // flight. Tracks the THREAD, not just a boolean, so a resume on
  // thread A does not block a fresh send on thread B.
  resumingThreadId: string | null;
  // T4 workflow: watched issuers and the in-app notices their
  // filings raised. Both are workspace-local — no outbound delivery.
  watchlist: WatchEntry[];
  notices: Notice[];
  filingMonitorStatus: "healthy" | "degraded";
  filingMonitorError: string | null;
  // ``true`` while a Stop request for the active thread is in flight,
  // so the button cannot be pressed twice before the wire closes.
  stoppingThreadId: string | null;

  // ----- question-workspace (W1-W5): the task projection seam -----
  //
  // Everything below is derived from the HOST's authoritative
  // ``task_runtime`` envelope and re-read after every task door. The
  // workspace never infers a task fact locally: an operation opens
  // ``pending`` in the per-thread ledger, and only the host's answer moves
  // the projection (specs/question-workspace.md §"Input ordering and
  // module seams").
  //
  // The task slice is per-thread keyed rather than a single object so a
  // switch cannot leave one thread's task state painted on another,
  // matching the existing turn-table guards.
  taskRuntime: Record<string, TaskRuntimeView | undefined>;
  // The clarification draft currently being composed, keyed by
  // ``question_id``. A draft belongs to its own question id: it survives a
  // rejected reply with that association intact (SC-4) and survives a reload
  // when it was persisted, and nothing ever re-labels it onto another
  // question — a draft is dropped only when its own question is answered or
  // replaced, never because a later read looks different.
  clarificationDrafts: Record<string, ClarificationDraft>;
  // Per-thread submission ledger: at most the CURRENT operation of each
  // thread, its minted attempt, and the host's verdict on that attempt.
  // A thread with no entry has nothing outstanding. This replaces the
  // single shared submission slot, which a thread switch or a late result
  // could repaint across threads.
  workspaceOperations: WorkspaceOperationLedger;
  // Per-thread restore request, snapshotted at click time from the listing
  // the row was picked from (after adoption that row is gone, so no later
  // read can restate the archive→task id mapping). It is the only place the
  // host's answer about that restore is kept, so it is retained past the
  // answer — never cleared by the send, and replaced only by a newer request
  // on the same thread.
  restoreRequests: Record<string, RestoreRequestRecord | undefined>;
  // Per-thread decode refusal from the last task-projection read
  // (openThread / reloadTaskProjection). The malformed envelope still
  // decodes to the empty view, so without this the broken channel is
  // indistinguishable from a taskless thread. Read by ArchiveList to
  // render the projection-refusal copy; keep in sync with that
  // consumer (pages/ThreadPage.tsx mounts <ArchiveList/> at its
  // TaskRegion, so the refusal is visible wherever the archive surface
  // is). Cleared when a later read of the SAME thread decodes cleanly.
  taskProjectionRefusal: Record<string, TaskRuntimeDecodeRefusal | undefined>;
  // Per-thread marker that the last projection RE-READ failed on the
  // wire (the decode never ran). Distinct from ``taskProjectionRefusal``:
  // that names unreadable CONTENT, this names an unreachable HOST — copy
  // must not claim the task record is broken when only the refresh
  // failed. Cleared on the next successful read;
  // ``openThread`` does not write it (its failure surfaces as the
  // thread-open fault, App.tsx).
  taskProjectionRefreshFailed: Record<string, boolean | undefined>;

  /** W2: open a draft for the thread's pending clarification. A no-op
   * when a draft already exists, so a re-render cannot clobber a typed
   * draft with a fresh empty one. */
  openTaskClarification: (threadId: string) => void;
  /** W2: select a choice (or the reserved Other) on a draft. */
  selectTaskChoice: (threadId: string, choiceId: string) => void;
  /** W2: write free text inside the selected Other option. */
  setTaskOtherText: (threadId: string, text: string) => void;
  /** W2: collapse the card. Presentation-only: it supplies no answer
   * and leaves the question pending (question-workspace.md). */
  dismissTaskClarification: (threadId: string) => void;
  /** Clears a collapsed card's collapse only; never changes the draft's
   * answer. W2. */
  reopenTaskClarification: (threadId: string) => void;
  /** Submit the reply. Resolves once the host has answered; a refusal
   * is recorded on the thread's operation and the draft is preserved.
   * A task door resolves on EVERY lane instead of rejecting: the card
   * states each outcome from the recorded verdict (its own refusal
   * sentence, the unknown-outcome sentence), and nothing dispatching a
   * task door awaits it — a rethrow here would float as an unhandled
   * rejection rather than reach a notice. The message door is the
   * opposite case and keeps rejecting, because the Composer puts the
   * draft back and renders that error itself. */
  submitTaskReply: (threadId: string) => Promise<void>;
  /** Ask the host to restore one listed archive. Explicit only, and
   * disposed exactly like the reply door above. */
  restoreArchivedTask: (threadId: string, archiveId: string) => Promise<void>;
  /** Re-read the authoritative task projection for a thread. Every task
   * door calls this after the host answers, because the host owns the
   * resulting state and this surface never infers it. A read failure is
   * not rethrown (the projection is presentation; a task read that
   * fails must not break the thread surface around it) but it IS
   * recorded on ``taskProjectionRefreshFailed`` rather than silently
   * ignored. */
  reloadTaskProjection: (threadId: string) => Promise<void>;

  refreshThreads: () => Promise<void>;
  loadCorpus: () => Promise<void>;
  // T6: the generic catalog OVERVIEW — documents with title / type plus
  // the presentation grouping. Separate from ``loadCorpus`` because
  // ``GET /api/corpus`` is the frozen FINANCIAL projection and this is the
  // surface an ordinary metadata-free document is visible on.
  loadCatalogOverview: () => Promise<void>;
  // Re-GETs ``GET /api/status`` (boot effect). Boot-scoped fields do
  // not change mid-session, so a successful read updates the store; a
  // thrown fetch still flips ``statusLoaded`` so the left-corner line
  // renders its neutral state rather than a silent OK.
  loadStatus: () => Promise<void>;
  openThread: (threadId: string) => Promise<ThreadOpenOutcome>;
  createThread: (firstMessage: string | null) => Promise<string>;
  removeThread: (threadId: string) => Promise<void>;
  sendQuestion: (
    threadId: string,
    question: string,
    thinkingOn?: boolean,
    attachmentIds?: string[],
  ) => Promise<api.SendResult | null>;
  // T4 (spec line 39): resume an interrupted run from the last
  // complete node. Mirrors ``sendQuestion``'s streaming-event
  // contract but does NOT append a new HumanMessage or an
  // optimistic turn — it picks up the existing in-flight turn row
  // the lock probe surfaced. The switch-thread guard (resume only
  // paints if the operator is still on this thread) is preserved
  // by re-checking ``get().activeThread?.id`` against ``threadId``
  // on every patch.
  resumeThread: (threadId: string) => Promise<api.SendResult | null>;
  // search-mode-host T2: operator picks a choice-window option.
  // ``runId`` identifies the suspended turn (run_id is the turn key
  // in this store). Collapses the window locally, then POSTs
  // /resume {"pick": optionId} and keeps streaming into the SAME
  // turn; an invalid pick streams the window back with
  // ``rejected_picks``.
  pickChoice: (
    threadId: string,
    runId: string,
    optionId: string,
  ) => Promise<api.SendResult | null>;
  // T4 workflow. ``addWatch`` rejects with the host's typed
  // error so the panel can show it inline; the rest resolve quietly
  // and leave the slice consistent.
  loadWatchlist: () => Promise<void>;
  addWatch: (issuer: string, forms: string[]) => Promise<void>;
  removeWatch: (watchId: string) => Promise<void>;
  loadNotices: () => Promise<void>;
  dismissNotice: (noticeId: string) => Promise<void>;
  // Stop the in-flight run on ``threadId``. The run's own send promise
  // resolves with the terminal stopped frames, so this action only has
  // to signal and refresh the turn table.
  stopRun: (threadId: string) => Promise<void>;
  // ADR 0047 **corpus ingest**: the ingest door. This action durably
  // ACCEPTS the upload and hands back the job id — parse/切分/push run
  // off the request in a later worker, so no parse outcome arrives on
  // this response; a named failure rethrows for the control to render
  // (ingestFailure.ts).
  ingestFile: (file: File, meta: api.IngestMeta) => Promise<IngestResult>;
  // The accepted job's observable state, read through the status door.
  // ``ingestJobId`` is the id THIS browser last submitted, persisted so
  // a navigation or reload can resume following the job without
  // re-uploading; ``ingestAccepted`` is the accept body (state
  // ``pending``) shown until the first status read lands.
  ingestJobId: string | null;
  ingestAccepted: IngestResult | null;
  ingestJob: JobStatus | null;
  // A status read that refused (``unknown_job``, transport). The
  // control renders it through the same typed-copy path as a submit
  // failure — a dead job id is never a blank panel. A transport error is
  // stored as a plain ``Error``; ``describeIngestFailure`` still renders
  // it as the generic retry notice.
  ingestJobFailure: Error | null;
  // Reads one job's status and keeps reading while it is non-terminal.
  // ``jobId`` defaults to the persisted submission, so a mount after
  // reload needs no argument. ``intervalMs`` is the poll cadence,
  // injectable so tests advance the schedule without real waiting.
  loadIngestJob: (jobId?: string, intervalMs?: number) => Promise<void>;
  // ADRs 0047/0089: the attach door. Uploads raw bytes to
  // the thread-attach endpoint and folds the minted record (status
  // "uploaded") into the thread's attachment slice; NEVER touches the
  // ingest endpoint — the doors must not cross. While the POST is
  // in flight it is recorded in ``pendingUploads``, which is what
  // holds the send and new-conversation gate.
  attachFile: (threadId: string, file: File) => Promise<ThreadAttachment>;
  // Attachments live for the WHOLE thread, not one turn: the slice
  // below mirrors the thread's current records, and the ready states
  // (parsing → ready / failed) are only observable by re-reading the
  // list, hence the polling in ``loadAttachments``.
  attachments: ThreadAttachment[];
  // In-flight upload POSTs, counted per thread. A record does not exist
  // on ``attachments`` until its POST answers, so the send gate cannot
  // be read off the attachment list — this slice is the one place the
  // client knows an upload is still running. Cleared per upload when
  // its POST settles, success or refusal.
  pendingUploads: Record<string, number>;
  // Seed the slice for the thread the Composer opens and keep it
  // fresh while anything is "parsing" and while a run is suspended
  // waiting on preparation (``attachmentPreparationWait``).
  // ``intervalMs`` is the poll cadence, injectable so tests advance
  // the schedule without real
  // waiting. A failed door read ends the chain and leaves the slice as
  // it is — attachments are auxiliary to the thread view and must not
  // fail navigation. The next open or attach restarts it; a retry
  // answers a settled record and therefore never restarts it.
  loadAttachments: (threadId: string, intervalMs?: number) => Promise<void>;
  // Re-run preparation for one failed attachment. The door answers with
  // the settled record (``ready`` / ``failed``); rejects with the named
  // ``AttachFailedError`` so the control can render the reason.
  retryAttachment: (threadId: string, attachmentId: string) => Promise<void>;
  // Remove one attachment from the thread. Response-driven like every other
  // attachment door: the record leaves the slice only once the host has
  // acknowledged the deletion, because a named refusal means either nothing
  // happened (``unknown_locator``) or the attachment is voided with its bytes
  // still owed (``attachment_cleanup_failed``) — and in both cases the chip
  // must stay where the operator can act on it. Rejects with the host's
  // ``AttachFailedError`` so the control can render the reason.
  deleteAttachment: (threadId: string, attachmentId: string) => Promise<void>;
  // T8: record (or flip) the operator's thumb for one run. Response-
  // driven (matching ``dismissNotice``'s convention): the turn row is
  // patched only after the host confirms the persisted value, so the
  // UI never shows a thumb the store rejected. Rejects so the caller
  // can surface a failure; the optimistic row is not touched.
  setTurnFeedback: (
    threadId: string,
    runId: string,
    value: FeedbackValue,
  ) => Promise<void>;
}

// ----- question-workspace: task-projection helpers -----

/** The store's own ``set``, spelled once: every helper below writes one
 * slice of it, and the two argument shapes zustand accepts must stay
 * available to them. */
type StoreSetter = (
  partial:
    | Partial<WorkspaceState>
    | ((s: WorkspaceState) => Partial<WorkspaceState>),
) => void;

/** The thread's still-pending clarification, or null.
 *
 * Read from the per-thread ``taskRuntime`` map — the single source every
 * task surface reads — and guarded on the active thread, so a thread
 * switch cannot leave one thread's pending question offered on another.
 * The envelope's top-level ``pending_question`` is the authoritative
 * record; ``current_task``'s nested copy carries the same value and is
 * deliberately not consulted, so there is one source rather than two
 * that could disagree. */
function pendingQuestionFor(
  state: Pick<WorkspaceState, "activeThread" | "taskRuntime">,
  threadId: string,
): PendingQuestionView | null {
  if (state.activeThread?.id !== threadId) return null;
  return state.taskRuntime[threadId]?.pending_question ?? null;
}

/** Write one draft under its own question id and persist the recoverable
 * half. The draft is the operator's input, not derived state, so a reload
 * must not lose it (specs/question-workspace.md "preserve … the user's
 * draft"); the question it belongs to stays the host's. */
function setDraft(
  set: StoreSetter,
  get: () => WorkspaceState,
  questionId: string,
  draft: ClarificationDraft,
): void {
  set((s) => ({
    clarificationDrafts: { ...s.clarificationDrafts, [questionId]: draft },
  }));
  persistClarificationDrafts(get().clarificationDrafts);
}

/** Apply a pure draft transition to the thread's pending question.
 *
 * No-ops when the thread moved on or the question is no longer pending:
 * a stale card must not keep editing a draft the host has already
 * replaced, and the switch-thread guard matches every other action in
 * this store. */
function patchDraft(
  set: StoreSetter,
  get: () => WorkspaceState,
  threadId: string,
  transition: (draft: ClarificationDraft) => ClarificationDraft,
): void {
  const question = pendingQuestionFor(get(), threadId);
  if (question === null) return;
  const draft = get().clarificationDrafts[question.question_id];
  if (!draft) return;
  const next = transition(draft);
  if (next === draft) return;
  setDraft(set, get, question.question_id, next);
}

// ----- draft durability -----

/** Store the draft's own pick and free text, keyed by its question id —
 * the same key the in-memory map uses, so a reload cannot shift an answer
 * onto a different question. Unavailable storage only loses the resume. */
function persistClarificationDrafts(
  drafts: Record<string, ClarificationDraft>,
): void {
  try {
    window.localStorage.setItem(
      CLARIFICATION_DRAFT_STORAGE_KEY,
      JSON.stringify(draftRecords(drafts)),
    );
  } catch {
    /* storage unavailable - nothing to carry across a reload. */
  }
}

/** The persisted drafts, as far as they can be trusted. A blob that is not
 * JSON, or entries that are not draft records, read as none rather than as
 * a half-parsed answer: a corrupted record must never become a pick the
 * operator did not make. */
function readPersistedDraftRecords(): ClarificationDraftRecords {
  try {
    const raw = window.localStorage.getItem(CLARIFICATION_DRAFT_STORAGE_KEY);
    return parseDraftRecords(JSON.parse(raw ?? "{}"));
  } catch {
    return {};
  }
}

// ----- the seams a surface reads -----

/** The open thread's CURRENT operation, or ``null``.
 *
 * Two guards, one per half of the display rule: the thread must be the one
 * on screen, and the record must name that same thread. Together they make
 * an operation recorded for thread A unrenderable while thread B is open,
 * which is what a single shared slot could not express. */
export function displayableWorkspaceOperation(
  state: Pick<WorkspaceState, "activeThread" | "workspaceOperations">,
  threadId: string,
): WorkspaceOperation | null {
  if (state.activeThread?.id !== threadId) return null;
  const operation = currentWorkspaceOperation(
    state.workspaceOperations,
    threadId,
  );
  return isWorkspaceOperationDisplayable(operation, threadId) ? operation : null;
}

/** The stable store slices a task surface derives from.
 *
 * Selecting the slices rather than a derived object is a zustand rule, not
 * a style choice: a selector that rebuilt an object would fail the
 * ``Object.is`` comparison on every notification and re-render the thread
 * forever. So a surface reads references the store itself holds and derives
 * the decisions in its render body, through the helpers below. */
export type TaskSurfaceFacts = Pick<
  WorkspaceState,
  | "activeThread"
  | "pendingUploads"
  | "restoreRequests"
  | "taskProjectionRefusal"
  | "taskProjectionRefreshFailed"
  | "taskRuntime"
  | "workspaceOperations"
>;

/** The refusal a surface may render. It is derived from the operation record,
 * not a second slot beside it: it exists only while the record does, is
 * invisible once a newer attempt replaces it, and names the door that
 * produced it so a surface renders only its own door's copy. A rejected
 * verdict with no named reason names nothing, so it yields no refusal. */
export function operationRefusal(
  operation: WorkspaceOperation | null,
): WorkspaceOperationRefusal | null {
  if (operation === null || operation.state !== "rejected") return null;
  if (operation.reason === null) return null;
  return {
    kind: operation.reason,
    questionId: operation.questionId,
    door: operation.door,
  };
}

/** The one set of active-input facts for one thread.
 *
 * Computed once and read by both halves of the send door — the store's
 * guard and the composer's notice — so a disabled box and a silently
 * dropped POST cannot disagree. The pending-choice fact is ONE fact from
 * two sources: the in-memory turn table and the projection's reloaded
 * ``pending_turn``, ORed because a thread reloaded mid-window has no live
 * turns to read. ``transportFailed`` is ``false`` because the store keeps
 * no separate transport flag: a send that never got an answer is recorded
 * as that operation's own ``failed`` verdict, which the ledger already
 * reports through ``submission``. */
export function activeInputFactsFor(
  state: Pick<
    WorkspaceState,
    "activeThread" | "pendingUploads" | "workspaceOperations"
  >,
  threadId: string,
): ActiveInputFacts {
  const detail = state.activeThread;
  const operation = displayableWorkspaceOperation(state, threadId);
  const onThread = detail !== null && detail.id === threadId;
  return {
    pendingChoiceWindow:
      onThread &&
      (hasPendingChoiceWindow(detail.turns) || threadHasPendingChoice(detail)),
    uploadInFlight: hasUploadInFlight(state.pendingUploads),
    submission: operation?.state ?? "idle",
    transportFailed: false,
    refusalKind: operation?.reason ?? null,
  };
}

/** The thread's single active-input decision. */
export function activeInputFor(
  state: Pick<
    WorkspaceState,
    "activeThread" | "pendingUploads" | "workspaceOperations"
  >,
  threadId: string,
): ActiveInputDecision {
  return decideActiveInput(activeInputFactsFor(state, threadId));
}

/** The task panel's submission row: the notice the thread's one active-input
 * decision produced. Presentation-only — the submission's outcome says what
 * happened to what was sent and claims nothing about the task. ``null`` when
 * the thread has no operation, so the row renders nothing rather than an
 * empty state. */
export function submissionNoticeFor(
  state: Pick<
    WorkspaceState,
    "activeThread" | "pendingUploads" | "workspaceOperations"
  >,
  threadId: string,
): SubmissionNotice | null {
  const decision = activeInputFor(state, threadId);
  if (decision.submission === "idle" || decision.notice === null) return null;
  return { state: decision.submission, text: decision.notice };
}

/** Whether this thread's task projection can be read right now: the last
 * re-READ failed on the wire, or the envelope it returned was refused.
 * Either way an absent task is UNKNOWN, not "no task yet". */
export function projectionUnreadableFor(
  state: Pick<
    WorkspaceState,
    "taskProjectionRefusal" | "taskProjectionRefreshFailed"
  >,
  threadId: string,
): boolean {
  return (
    state.taskProjectionRefreshFailed[threadId] === true ||
    state.taskProjectionRefusal[threadId] !== undefined
  );
}

/** The archive list this thread's projection offers. No read yet offers
 * nothing — that is the absent thing, not an empty list to send against. */
function offeredArchives(
  state: Pick<WorkspaceState, "taskRuntime">,
  threadId: string,
): ArchivedTaskSummary[] {
  return state.taskRuntime[threadId]?.archived_tasks ?? [];
}

/** The restore door's facts for one thread, read off the store's own
 * records.
 *
 * Only the request record and the projection are consulted — never the
 * thread's current operation. A restore's verdict lives on the restore, so
 * an operation the operator opened afterwards cannot change what this
 * surface is allowed to say about it.
 *
 * ``fresh`` is the load-bearing field and it means exactly one thing: a
 * projection read landed AFTER this request was sent and it neither failed
 * on the wire nor failed to decode. ``seenSinceSend`` is the store's
 * ordering fact and ``readable`` is the store's verdict on that read; both
 * are needed because a later wire failure must take an already-accepted
 * request back to ``unresolved``, keeping the current task visible instead
 * of confirming nothing. */
export function restoreFactsFor(
  state: Pick<
    WorkspaceState,
    | "restoreRequests"
    | "taskProjectionRefusal"
    | "taskProjectionRefreshFailed"
    | "taskRuntime"
  >,
  threadId: string,
): RestoreDecisionInput {
  const request = state.restoreRequests[threadId] ?? null;
  const readable =
    state.taskProjectionRefreshFailed[threadId] !== true &&
    state.taskProjectionRefusal[threadId] === undefined;
  const view = state.taskRuntime[threadId];
  return {
    threadId,
    request,
    projection:
      view === undefined
        ? null
        : {
            currentTaskId: view.current_task?.task_id ?? null,
            archived: view.archived_tasks,
            fresh: request === null ? readable : request.seenSinceSend && readable,
          },
  };
}

/** The request as it was offered: the clicked archive, the task THAT row
 * named, and the listing snapshotted at click time. The snapshot is what
 * keeps adoption provable — the host removes the adopted row from the
 * listing, so after a successful restore no later read can restate the
 * archive→task mapping. The host has named no verdict yet on a request the
 * operator only just made. */
function restoreRequestFor(
  archiveId: string,
  taskId: string,
  offered: readonly ArchivedTaskSummary[],
): RestoreRequestRecord {
  return {
    archiveId,
    taskId,
    offered,
    accepted: false,
    failedToSend: false,
    refusalKind: null,
    seenSinceSend: false,
  };
}

/** Move one field of this thread's restore request. A patch for a thread
 * with no open request describes nothing, so nothing is written: a record
 * is never manufactured by an update. */
function patchRestoreRequest(
  set: StoreSetter,
  threadId: string,
  patch: Partial<RestoreRequestRecord>,
): void {
  set((s) => {
    const current = s.restoreRequests[threadId];
    if (current === undefined) return {};
    return {
      restoreRequests: {
        ...s.restoreRequests,
        [threadId]: { ...current, ...patch },
      },
    };
  });
}

/** Mark that THIS thread's projection read landed readable. Only a read
 * that both happened after the send and decoded cleanly makes the
 * projection fresh for the request open on this thread. */
function markRestoreProjectionSeen(
  set: StoreSetter,
  threadId: string,
  readable: boolean,
): void {
  if (!readable) return;
  patchRestoreRequest(set, threadId, { seenSinceSend: true });
}

/** The archive the clicked row holds. Adoption is proven against THIS id
 * and never against whatever is current later, so a row that cannot name
 * its task is not an offered action. */
function listedRestoreTarget(
  archives: readonly ArchivedTaskSummary[],
  archiveId: string,
): string | null {
  if (!restoreIsListed(archives, archiveId)) return null;
  const taskId = archives.find(
    (archive) => archive.archive_id === archiveId,
  )?.task_id;
  return taskId === undefined || taskId.trim() === "" ? null : taskId;
}

/** Refuse a restore the surface cannot see through, BEFORE it goes on the
 * wire: the host would answer ``task_restore_unknown`` for an id it never
 * listed, and an action offered by nothing must not look like an action
 * taken. The refusal is recorded as this thread's own rejected operation
 * AND on the request record, because that record is where a restore's
 * verdict is kept: the ledger holds only the thread's current operation, so
 * a verdict stored nowhere else would be replaced by the next door opened.
 * The record names no task id — a row the listing does not offer cannot
 * name one, and nothing can be adopted against an empty id. */
function recordUnlistedRestoreRefusal(
  set: StoreSetter,
  get: () => WorkspaceState,
  threadId: string,
  archiveId: string,
): void {
  const opened = beginWorkspaceOperation(get().workspaceOperations, {
    threadId,
    door: "restore",
    kind: "restore",
    archiveId,
  });
  if (opened.operation === null) return;
  const archives = offeredArchives(get(), threadId);
  set((s) => ({
    workspaceOperations: opened.ledger,
    restoreRequests: {
      ...s.restoreRequests,
      [threadId]: restoreRequestFor(archiveId, "", archives),
    },
  }));
  patchRestoreRequest(set, threadId, { refusalKind: "task_restore_unknown" });
  recordOperationVerdict(set, {
    threadId,
    attempt: opened.operation.attempt,
    state: "rejected",
    reason: "task_restore_unknown",
    archiveId,
  });
}

/** The restore door's own identity: which thread, which attempt, which
 * archive, which task. Every verdict the door writes carries all four, so
 * a result can only ever be applied to the record that asked for it. */
interface RestoreDoorFacts {
  threadId: string;
  attempt: number;
  archiveId: string;
  taskId: string;
}

/** What a restore RESPONSE means for the request. A named mid-stream
 * refusal outranks the acceptance — a restore refused at reconciliation was
 * never adopted — and otherwise the request is durably recorded, which
 * moves exactly ONE field (``accepted``). Neither lane clears the request:
 * it is the only place the archive→task id mapping survives once the host
 * adopts the row, and it is where this restore's own verdict is kept, so
 * dropping it when the response arrives would strand both. */
function recordRestoreOutcome(
  set: StoreSetter,
  facts: RestoreDoorFacts,
  refusal: string | null,
): void {
  if (refusal !== null) {
    patchRestoreRequest(set, facts.threadId, { refusalKind: refusal });
    recordOperationVerdict(set, {
      ...facts,
      state: "rejected",
      reason: refusal,
    });
    return;
  }
  patchRestoreRequest(set, facts.threadId, { accepted: true });
  recordOperationVerdict(set, { ...facts, state: "accepted" });
}

/** The host's own named answer, when a thrown error IS one. Every task door
 * reads its failure through here so ``instanceof`` cannot be asked
 * differently in one door than in another: a named refusal is the host's word
 * about a request that reached it, anything else is no answer at all, which
 * the doors record as a failure or an unheard send. */
function namedTaskRefusal(
  error: unknown,
): { kind: string; questionId: string | null } | null {
  return error instanceof api.TaskInputRefusedError
    ? { kind: error.kind, questionId: error.questionId }
    : null;
}

/** The restore door's two failure lanes, in one place: a NAMED refusal is
 * the host's word about a request that DID reach it, and it is kept on that
 * request record; anything else means no authoritative answer arrived at
 * all, which the request records as ``failedToSend``. The existing current
 * task keeps rendering either way, and the request survives both. */
function recordRestoreFailure(
  set: StoreSetter,
  facts: RestoreDoorFacts,
  error: unknown,
): void {
  const refusal = namedTaskRefusal(error);
  if (refusal !== null) {
    patchRestoreRequest(set, facts.threadId, { refusalKind: refusal.kind });
    recordOperationVerdict(set, {
      ...facts,
      state: "rejected",
      reason: refusal.kind,
      questionId: refusal.questionId,
    });
    return;
  }
  patchRestoreRequest(set, facts.threadId, { failedToSend: true });
  recordOperationVerdict(set, { ...facts, state: "failed" });
}

// turn-trace-folds: fold one wire frame into the turn's burst
// timeline. Shared by send and resume so the two paths cannot drift
// on burst semantics (arrival-order folds, auto-collapse at burst
// end). Plain helpers — no state access.
function foldTraceFrameOntoTurn(t: Turn, frame: TraceFrame): Turn {
  return {
    ...t,
    trace_items: foldTraceFrame(t.trace_items ?? [], frame),
  };
}

function closeOpenBurstsOnTurn(t: Turn): Turn {
  return { ...t, trace_items: closeOpenBursts(t.trace_items ?? []) };
}

// Streaming render saturation (follow-up): at provider chunk
// granularity every SSE frame triggers one store patch → one full
// transcript re-render. A long thread (round 3+) re-renders EVERY
// historical turn row per frame; with a fast provider the frames
// arrive faster than React can commit and the live fold's updates
// coalesce visually into a freeze — the operator sees thinking
// "disappear" mid-run and the answer dump at the end.
//
// Fix: coalesce the per-frame delta patchers (thinking / answer) into
// one animation-frame flush. The callbacks run against the LATEST
// turn snapshot in order, so the folded trace is identical to
// unbatched folding — only the number of intermediate renders
// shrinks. UI-only: the send promise's result/fold state still
// tracks every wire frame exactly.
//
// Ordering invariant: a non-delta frame (tool_call / tool_result /
// status / citations / the finalize patch) must NEVER overtake
// deltas queued in the same animation frame — otherwise a tool_call
// landing between a queued thinking delta and its flush would fold
// the tools burst BEFORE the thinking burst, inverting the arrival
// order the folder guarantees. ``flushThen`` drains the queue first
// and only then applies the immediate patcher; the scheduled rAF
// later fires on an empty queue and is a no-op.
function makeTurnPatcherQueue(patch: (patcher: (t: Turn) => Turn) => void) {
  const queue: Array<(t: Turn) => Turn> = [];
  let scheduled = false;
  const flush = () => {
    scheduled = false;
    if (queue.length === 0) return;
    const batch = queue.splice(0, queue.length);
    // Sequential fold onto the freshest snapshot: the same contract
    // as unbatched onEvent processing.
    patch((t) => batch.reduce((acc, fn) => fn(acc), t));
  };
  return {
    coalesce: (patcher: (t: Turn) => Turn) => {
      queue.push(patcher);
      if (scheduled) return;
      scheduled = true;
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(flush);
      } else {
        // Non-browser fallback (tests): synchronous flush keeps the
        // batch boundary deterministic.
        flush();
      }
    },
    flushThen: (patcher: (t: Turn) => Turn) => {
      flush();
      patch(patcher);
    },
  };
}

type TurnPatcherQueue = ReturnType<typeof makeTurnPatcherQueue>;

// Mutable per-stream fold shared by the resume / pick paths: the
// answer delta accumulator plus the two end-of-stream signals.
// ``windowPending`` set at stream end = the run suspended again
// (invalid pick) and the row must stay not-done.
interface StreamFold {
  statusSeen: boolean;
  windowPending: ChoiceWindow | null;
  /**
   * the ``document_choice`` frame this
   * stream suspended on, or null. The host emits NO terminal frames on a
   * suspended document choice, so a set fold is what keeps the row live
   * instead of letting the finalize patch read it as done — the same
   * posture ``windowPending`` gives a choice window, for the same reason.
   *
   * The fold carries the REFERENCE (question id + originating run id),
   * never candidates: the frame carries no ``choice_details``, so the
   * candidate authority is the projection the store re-reads on receipt.
   */
  documentChoicePending: DocumentChoiceFrame | null;
  answerFold: LiveAnswerState;
}

// Fold one wire frame into the turn a resume stream is continuing.
// Identical semantics for the crash-resume (``resumeThread``) and the
// choice-window pick (``pickChoice``) paths — only the patch target
// differs, and that is already bound inside ``patches`` — so the two
// cannot drift on frame handling (same reasoning as
// ``consumeSseStream``).
function foldResumeFrame(
  patches: TurnPatcherQueue,
  fold: StreamFold,
  event: api.SendEvent,
): void {
  if (foldStreamTraceFrame(patches, fold, event)) return;
  switch (event.type) {
    case "status":
      fold.statusSeen = true;
      fold.windowPending = null;
      fold.documentChoicePending = null;
      patches.flushThen((t) => ({
        ...closeOpenBurstsOnTurn(t),
        status: event.status,
        status_reason: event.reason ?? "",
        assistant_message: event.assistant_message,
        track: event.track ?? t.track,
        track_decision: event.track_decision ?? t.track_decision,
        search_mode_decision:
          event.search_mode_decision ?? t.search_mode_decision,
        // Mid-flight stream taxonomy (see sendQuestion's status
        // branch for the same handling rationale).
        status_classified:
          event.status_classified !== undefined
            ? event.status_classified
            : t.status_classified,
        in_flight: false,
        thinking_duration_ms: event.thinking_ms ?? t.thinking_duration_ms,
        semantic_outcome: event.semantic_outcome ?? t.semantic_outcome,
      }));
      break;
    case "citations":
      patches.flushThen((t) => ({ ...t, citations: event.citations }));
      break;
    case "evidence_refusals":
      // E6 refusal fold: appended onto the turn's refusal list through
      // the shared union helper (arrival order kept, duplicates dropped)
      // and folded via ``flushThen`` like every non-delta frame — the
      // invariant that no queued delta may overtake it. Nothing here
      // touches ``citations``: a refusal fold never reorders them.
      patches.flushThen((t) => ({
        ...t,
        evidence_refusals: mergeEvidenceRefusals(
          t.evidence_refusals,
          event.refusals,
        ),
      }));
      break;
    case "visual_claims":
      // Same fold, same field, same reason as ``sendQuestion``: the visual
      // lane is not a citation, so a resumed run lands its rows here
      // rather than on the quote channel.
      patches.flushThen((t) => ({ ...t, visual_claims: event.visual_claims }));
      break;
    case "comparison":
      patches.flushThen((t) => ({
        ...t,
        comparisons: [...(t.comparisons ?? []), event.comparison],
      }));
      break;
    case "choice_window":
      // Either a reload re-emission (body-less resume) or the host
      // refusing the pick (``rejected_picks`` set). The row goes back
      // to suspended.
      fold.windowPending = event.window;
      patches.flushThen((t) => ({ ...t, choice_window: event.window }));
      break;
    case "document_choice":
      // The run suspended for a document choice — either the first
      // interrupt or the host re-opening the question after a refused
      // pick. Nothing is patched onto the turn:
      // the frame is a REFERENCE carrying no candidates, and the turn row
      // is not where a question lives. Only the fold moves, so the row
      // stays suspended (see the ``in_flight`` sites).
      fold.documentChoicePending = event.frame;
      break;
  }
}

// The four high-frequency trace frames — thinking and answer prose, plus
// the tool rows — folded onto the optimistic turn. Both streams call this:
// a send and a crash-resume must not drift on how a frame becomes a row,
// so the four cases live here once rather than once per path. ``false``
// means the frame is not a trace frame and the caller handles it. The
// answer fold mutates ``fold`` in place, so both streams fold answer
// deltas through the same shape.
function foldStreamTraceFrame(
  patches: TurnPatcherQueue,
  fold: StreamFold,
  event: api.SendEvent,
): boolean {
  switch (event.type) {
    case "thinking":
      patches.coalesce((t) => ({
        ...foldTraceFrameOntoTurn(t, event),
        thinking_text: t.thinking_text + event.text,
      }));
      return true;
    case "tool_call": {
      // Append a placeholder row so the operator sees a
      // running indicator while the tool executes. The
      // matching ``tool_result`` replaces it with the
      // server-formatted one-liner.
      const placeholder: ToolSummary = {
        name: event.name,
        summary: RUNNING_SUMMARY,
        tool_call_id: event.tool_call_id,
      };
      patches.flushThen((t) => ({
        ...foldTraceFrameOntoTurn(t, event),
        tool_summaries: [...(t.tool_summaries ?? []), placeholder],
      }));
      return true;
    }
    case "tool_result":
      // The server already formatted the summary (spec line
      // 32); replace the "running…" placeholder with it.
      patches.flushThen((t) => ({
        ...foldTraceFrameOntoTurn(t, event),
        tool_summaries: (t.tool_summaries ?? []).map((s) =>
          s.tool_call_id === event.tool_call_id
            ? { ...s, summary: event.summary }
            : s,
        ),
      }));
      return true;
    case "answer":
      fold.answerFold = foldLiveAnswer(fold.answerFold, {
        text: event.text,
        reset: event.reset,
        statusSeen: fold.statusSeen,
      });
      patches.coalesce((t) => ({
        // turn-trace-folds spec line 33: the first answer frame
        // closes the still-live burst — the fold must collapse
        // when prose starts, not only at terminal status.
        ...foldTraceFrameOntoTurn(t, { type: "answer" }),
        answer: fold.answerFold.visible,
      }));
      return true;
    default:
      return false;
  }
}

interface SendQuestionStreamState {
  fold: StreamFold;
  patches: TurnPatcherQueue;
  run: {
    optimisticRunId: Turn["run_id"];
    attachmentWaitRunId: string | null;
  };
}

function foldSendQuestionStatus(
  event: Extract<api.SendEvent, { type: "status" }>,
  stream: SendQuestionStreamState,
): void {
  stream.fold.statusSeen = true;
  stream.fold.windowPending = null;
  stream.fold.documentChoicePending = null;
  stream.patches.flushThen((turn) => ({
    ...closeOpenBurstsOnTurn(turn),
    run_id: event.run_id,
    status: event.status,
    status_reason: event.reason ?? "",
    assistant_message: event.assistant_message,
    track: event.track ?? turn.track,
    track_decision: event.track_decision ?? turn.track_decision,
    search_mode_decision:
      event.search_mode_decision ?? turn.search_mode_decision,
    status_classified:
      event.status_classified !== undefined
        ? event.status_classified
        : turn.status_classified,
    in_flight: false,
    thinking_duration_ms: event.thinking_ms ?? turn.thinking_duration_ms,
    semantic_outcome: event.semantic_outcome ?? turn.semantic_outcome,
  }));
  stream.run.optimisticRunId = event.run_id;
}

function foldSendQuestionFrame(
  event: api.SendEvent,
  stream: SendQuestionStreamState,
): void {
  if (foldStreamTraceFrame(stream.patches, stream.fold, event)) return;
  if (event.type === "status") {
    foldSendQuestionStatus(event, stream);
    return;
  }
  switch (event.type) {
    case "citations":
      stream.patches.flushThen((turn) => ({
        ...turn,
        citations: event.citations,
      }));
      break;
    case "evidence_refusals":
      stream.patches.flushThen((turn) => ({
        ...turn,
        evidence_refusals: mergeEvidenceRefusals(
          turn.evidence_refusals,
          event.refusals,
        ),
      }));
      break;
    case "visual_claims":
      stream.patches.flushThen((turn) => ({
        ...turn,
        visual_claims: event.visual_claims,
      }));
      break;
    case "comparison":
      stream.patches.flushThen((turn) => ({
        ...turn,
        comparisons: [...(turn.comparisons ?? []), event.comparison],
      }));
      break;
    case "choice_window":
      stream.fold.windowPending = event.window;
      stream.patches.flushThen((turn) => ({
        ...turn,
        choice_window: event.window,
      }));
      break;
    case "attachment_wait":
      stream.run.attachmentWaitRunId = event.run_id;
      stream.patches.flushThen((turn) => ({ ...turn, run_id: event.run_id }));
      stream.run.optimisticRunId = event.run_id;
      break;
    case "document_choice":
      stream.fold.documentChoicePending = event.frame;
      stream.patches.flushThen((turn) => ({
        ...turn,
        run_id: event.frame.origin_run_id,
      }));
      stream.run.optimisticRunId = event.frame.origin_run_id;
      break;
  }
}

interface SendQuestionOperation {
  threadId: string;
  attempt: number;
  stream: SendQuestionStreamState;
}

interface SendQuestionCompletion extends SendQuestionOperation {
  result: api.SendResult;
}

function beginMessageSend(
  set: StoreSetter,
  get: StoreGetter,
  threadId: string,
  question: string,
): number | null {
  if (!question.trim()) return null;
  const decision = activeInputFor(get(), threadId);
  if (decision.holdReason !== null) throw heldRefusalFor(decision.holdReason);
  const opened = beginWorkspaceOperation(get().workspaceOperations, {
    threadId,
    door: "message",
    kind: "message",
  });
  if (opened.operation === null) return null;
  set({ workspaceOperations: opened.ledger });
  return opened.operation.attempt;
}

function createSendQuestionStream(
  get: StoreGetter,
  set: StoreSetter,
  threadId: string,
  question: string,
): SendQuestionStreamState {
  const run = {
    optimisticRunId: crypto.randomUUID() as Turn["run_id"],
    attachmentWaitRunId: null as string | null,
  };
  const baseTurn = makeLiveTurn({ runId: run.optimisticRunId, question });
  const patchTurnByRunId = (
    runId: Turn["run_id"],
    patcher: (turn: Turn) => Turn,
  ) => {
    const current = get().activeThread;
    if (!current || current.id !== threadId) return;
    const nextTurns = current.turns.map((turn) =>
      turn.run_id === runId ? patcher(turn) : turn,
    );
    set({ activeThread: { ...current, turns: nextTurns } });
  };
  const patchTurn = (patcher: (turn: Turn) => Turn) =>
    patchTurnByRunId(run.optimisticRunId, patcher);
  const patches = makeTurnPatcherQueue(patchTurn);
  const initial = get().activeThread;
  if (initial && initial.id === threadId) {
    set({ activeThread: { ...initial, turns: [...initial.turns, baseTurn] } });
  }
  return {
    fold: {
      statusSeen: false,
      windowPending: null,
      documentChoicePending: null,
      answerFold: EMPTY_LIVE_ANSWER,
    },
    patches,
    run,
  };
}

function makeSendRequest(
  question: string,
  thinkingOn: boolean,
  attachmentIds?: string[],
): api.SendRequest {
  const request: api.SendRequest = { question, thinking: thinkingOn };
  if (attachmentIds && attachmentIds.length > 0) {
    request.attachment_ids = attachmentIds;
  }
  return request;
}

function finalizeSendQuestionTurn(
  get: StoreGetter,
  completion: SendQuestionCompletion,
): void {
  const { threadId, result, stream } = completion;
  if (get().activeThread?.id !== threadId) return;
  const { fold, patches, run } = stream;
  patches.flushThen((turn) => ({
    ...closeOpenBurstsOnTurn(turn),
    in_flight:
      fold.windowPending !== null ||
      fold.documentChoicePending !== null ||
      run.attachmentWaitRunId !== null,
    citations: result.citations.length > 0 ? result.citations : turn.citations,
    evidence_refusals: mergeEvidenceRefusals(
      turn.evidence_refusals,
      result.evidence_refusals,
    ),
    track: result.track ?? turn.track,
    track_decision: result.track_decision ?? turn.track_decision,
    search_mode_decision:
      result.search_mode_decision ?? turn.search_mode_decision,
    status_classified: result.status_classified ?? turn.status_classified,
  }));
}

async function settleSendQuestion(
  set: StoreSetter,
  get: StoreGetter,
  completion: SendQuestionCompletion,
): Promise<void> {
  const { threadId, attempt, stream, result } = completion;
  finalizeSendQuestionTurn(get, completion);
  if (stream.run.attachmentWaitRunId !== null) {
    startAttachmentPolling(threadId, ATTACHMENT_POLL_INTERVAL_MS, true);
  }
  await get().refreshThreads();
  const refusal = midStreamTaskRefusal(result);
  recordOperationVerdict(set, {
    threadId,
    attempt,
    state: refusal === null ? "accepted" : "rejected",
    reason: refusal,
  });
  await get().reloadTaskProjection(threadId);
}

function messageStreamStarted(stream: SendQuestionStreamState): boolean {
  const { fold, run } = stream;
  return (
    fold.statusSeen ||
    fold.windowPending !== null ||
    fold.documentChoicePending !== null ||
    run.attachmentWaitRunId !== null
  );
}

/** Roll back the optimistic row of a send-shaped stream after a failure. */
function rollbackSendStreamTurn(
  threadId: string,
  stream: SendQuestionStreamState,
): void {
  const { fold, patches, run } = stream;
  if (messageStreamStarted(stream)) {
    patches.flushThen((turn) => ({
      ...closeOpenBurstsOnTurn(turn),
      in_flight:
        fold.windowPending !== null ||
        fold.documentChoicePending !== null ||
        run.attachmentWaitRunId !== null,
    }));
  } else {
    dropTurnByRunId(threadId, run.optimisticRunId);
  }
}

function failSendQuestion(
  set: StoreSetter,
  operation: SendQuestionOperation,
  error: unknown,
): void {
  const { threadId, attempt, stream } = operation;
  rollbackSendStreamTurn(threadId, stream);
  if (error instanceof api.TaskInputRefusedError) {
    recordOperationVerdict(set, {
      threadId,
      attempt,
      state: "rejected",
      reason: error.kind,
      questionId: error.questionId,
    });
    return;
  }
  recordOperationVerdict(set, { threadId, attempt, state: "failed" });
}

// The finalize patch both resume-shaped streams run once their SSE
// body closes: keep the row suspended when a window is pending, and
// never re-GET over the streamed row.
function finalizeResumedTurn(
  patches: TurnPatcherQueue,
  threadId: string,
  fold: StreamFold,
  result: api.SendResult,
): void {
  const cur = useWorkspaceStore.getState().activeThread;
  if (cur?.id !== threadId) return;
  // Drain queued deltas before the finalize patch (same ghost-burst
  // risk as the status branch).
  patches.flushThen((t) => ({
    ...closeOpenBurstsOnTurn(t),
    // A suspended choice window OR document choice means the stream ended
    // without a terminal frame: the run waits on the operator, so the row
    // is suspended, not done (GE-4).
    in_flight: fold.windowPending !== null || fold.documentChoicePending !== null,
    citations: result.citations.length > 0 ? result.citations : t.citations,
    // Same refusal union as the send finalize: the resume stream's
    // accumulated refusals fold onto the row without touching the
    // citations order.
    evidence_refusals: mergeEvidenceRefusals(
      t.evidence_refusals,
      result.evidence_refusals,
    ),
    track: result.track ?? t.track,
    track_decision: result.track_decision ?? t.track_decision,
    search_mode_decision:
      result.search_mode_decision ?? t.search_mode_decision,
    status_classified: result.status_classified ?? t.status_classified,
  }));
}

/** The task-door refusal a TERMINAL stream frame carried, if any. Both
 * door streams (reply and restore) can be durably accepted and then
 * refused at reconciliation, which arrives as a ``refuse`` frame on a
 * 200 stream rather than as an HTTP error — reading only the status
 * would call that an accepted door action. One resolver, shared by both
 * doors, so the two cannot drift apart on which frame field names the
 * failure (the reason first, then the stream-engine taxonomy). */
function midStreamTaskRefusal(result: api.SendResult): string | null {
  if (isTaskFailureKind(result.reason ?? undefined)) {
    return result.reason ?? null;
  }
  if (isTaskFailureKind(result.status_classified)) {
    return result.status_classified ?? null;
  }
  return null;
}

/** The projection read's decode refusal, made observable per thread. A
 * malformed or unsupported envelope still decodes to the empty view, so
 * without this record a broken task channel looks IDENTICAL to a taskless
 * thread — an honest "unrecorded" must be distinguishable from "nothing to
 * record" (taskRuntime.ts: a refusal is always accompanied by the
 * unrecorded reading, never by invented content). Cleared when the later
 * decode of the SAME thread succeeds. A reply- or restore-door refusal is
 * NEVER touched here: it lives on that door's own operation record, and the
 * projection re-read that follows a refused door decodes cleanly, so wiping
 * the door's fresh refusal would hide the operator's rejection. */
function recordProjectionRefusal(
  set: StoreSetter,
  threadId: string,
  refusal: TaskRuntimeDecodeRefusal | null,
): void {
  set((s) => ({
    taskProjectionRefusal: {
      ...s.taskProjectionRefusal,
      // ``null`` (a clean decode) clears the key rather than storing a
      // third state: the record means "threads whose LAST decode was
      // refused", so clean is absent, same as never-refused.
      [threadId]: refusal ?? undefined,
    },
  }));
}

/** Move one thread's operation to the verdict the host gave the attempt
 * that opened it. One writer, so no door can bypass the ledger's own
 * rules: a verdict for a superseded attempt is dropped, and a recorded
 * refusal stays final. */
function recordOperationVerdict(
  set: StoreSetter,
  result: WorkspaceOperationResult,
): void {
  set((s) => ({
    workspaceOperations: applyWorkspaceOperationResult(
      s.workspaceOperations,
      result,
    ),
  }));
}

/** Record the reply door's refusal in one place: the draft is reconciled
 * by ``applyRefusal`` (association kept, card re-opened, SC-4) and the
 * thread's own operation moves to ``rejected`` carrying the named reason.
 * The identical sequence used to be written out twice inside
 * ``submitTaskReply`` (mid-stream and HTTP lanes) — one copy here so the
 * SC-4 rules cannot diverge between them. The attempt stamp is what makes
 * a late refusal harmless: it is written only to the operation that opened
 * it, so an operator who already moved on never sees it repaint. */
function recordReplyRefusal(
  set: StoreSetter,
  get: () => WorkspaceState,
  input: {
    threadId: string;
    attempt: number;
    draftQuestionId: string;
    refusal: { kind: string; questionId: string | null };
  },
): void {
  const draft = get().clarificationDrafts[input.draftQuestionId];
  if (draft) {
    setDraft(set, get, input.draftQuestionId, applyRefusal(draft, input.refusal).draft);
  }
  recordOperationVerdict(set, {
    threadId: input.threadId,
    attempt: input.attempt,
    state: "rejected",
    reason: input.refusal.kind,
    questionId: input.refusal.questionId,
  });
}

/** What a reply door's thrown error means for the thread's operation, the
 * same two lanes the restore door has. A named refusal keeps the reply's
 * question association and its draft (SC-4): routing through
 * ``recordReplyRefusal`` is what leaves the question id unwritten, the choice
 * and free text intact, and a collapsed card re-opened, and it is an answer
 * about a request that reached the host — so the host still owns the state on
 * screen and its projection is re-read rather than guessed. Anything else is
 * a send whose outcome the browser never learned: a failure verdict, draft
 * untouched, and nothing to re-read. */
async function settleReplyFailure(
  set: StoreSetter,
  get: () => WorkspaceState,
  input: {
    threadId: string;
    attempt: number;
    questionId: string;
    error: unknown;
  },
): Promise<void> {
  const refusal = namedTaskRefusal(input.error);
  if (refusal === null) {
    recordOperationVerdict(set, {
      threadId: input.threadId,
      attempt: input.attempt,
      state: "failed",
      questionId: input.questionId,
    });
    return;
  }
  recordReplyRefusal(set, get, {
    threadId: input.threadId,
    attempt: input.attempt,
    draftQuestionId: input.questionId,
    refusal,
  });
  await get().reloadTaskProjection(input.threadId);
}

type TaskReplyPayload = NonNullable<ReturnType<typeof submitPayload>>;
type StoreGetter = () => WorkspaceState;

interface TaskReplyPreparation {
  question: PendingQuestionView;
  payload: TaskReplyPayload;
}

/** A terminal clarification reply opens a NEW turn on the normal send
 * door, so it streams through the same optimistic-row machinery as
 * ``sendQuestion``: one owner holds the fold, the patcher and the run. */
interface TaskReplySendStream {
  kind: "send";
  send: SendQuestionStreamState;
}

/** A document-choice reply continues the paused run: the row already
 * exists, so this stream only folds the remainder onto it. */
interface TaskReplyResumeStream {
  kind: "resume";
  resumeRunId: string;
  materialized: boolean;
  fold: StreamFold;
  patches: TurnPatcherQueue;
}

/** Which of the two reply mechanisms this submission uses.
 *
 * A discriminated union rather than parallel nullable fields: the fold
 * and patcher have exactly one owner per kind, so no reader can pick the
 * wrong pair out of two aliases over the same objects. */
type TaskReplyStream = TaskReplySendStream | TaskReplyResumeStream;

interface TaskReplySubmission extends TaskReplyPreparation {
  threadId: string;
  attempt: number;
  stream: TaskReplyStream;
}

function prepareTaskReply(
  get: StoreGetter,
  threadId: string,
): TaskReplyPreparation | null {
  const question = pendingQuestionFor(get(), threadId);
  if (question === null) return null;
  const draft = get().clarificationDrafts[question.question_id];
  if (!draft || !isSubmittable(draft)) return null;
  const payload = submitPayload(draft);
  if (payload === null) return null;
  return { question, payload };
}

function beginTaskReplyOperation(
  set: StoreSetter,
  get: StoreGetter,
  threadId: string,
  payload: TaskReplyPayload,
): number | null {
  const opened = beginWorkspaceOperation(get().workspaceOperations, {
    threadId,
    door: "reply",
    kind: "reply",
    questionId: payload.questionId,
  });
  if (opened.operation === null) return null;
  set({ workspaceOperations: opened.ledger });
  return opened.operation.attempt;
}

/** The message the host will open as the reply turn's question.
 *
 * A terminal clarification reply is a new turn, and the host owns the
 * composed text (`_clarify_reply_turn_question`): the committed task
 * goal (or the pending question's prompt when there is none) beside the
 * operator's answer, trimmed. The optimistic row renders before any host
 * frame arrives, so it mirrors that ONE composition exactly — same
 * fallback, same format — and the persisted GET row then replaces it
 * with the identical string. Pinned by tests on both surfaces. */
function replyTurnQuestion(
  get: StoreGetter,
  threadId: string,
  question: PendingQuestionView,
  payload: TaskReplyPayload,
): string {
  const goal =
    get().taskRuntime[threadId]?.current_task?.goal ?? question.prompt;
  const answer = (payload.freeText ?? payload.choiceId ?? "").trim();
  return `原问题：${goal}\n补充回答：${answer}`;
}

function prepareTaskReplyStream(
  get: StoreGetter,
  set: StoreSetter,
  threadId: string,
  question: PendingQuestionView,
  payload: TaskReplyPayload,
): TaskReplyStream {
  const resumeRunId = resumeOriginRunId(question);
  if (resumeRunId === null) {
    // A terminal clarification has no paused run. Its reply is a NEW
    // turn on the send door, so it materializes and folds exactly like
    // an ordinary question instead of dropping every stream frame.
    return {
      kind: "send",
      send: createSendQuestionStream(
        get,
        set,
        threadId,
        replyTurnQuestion(get, threadId, question, payload),
      ),
    };
  }
  const materialized = ensureReplyTurn(
    threadId,
    resumeRunId,
    recoveredReplyQuestion(get(), threadId, resumeRunId),
  );
  const fold: StreamFold = {
    statusSeen: false,
    windowPending: null,
    documentChoicePending: null,
    answerFold: EMPTY_LIVE_ANSWER,
  };
  const patches = makeTurnPatcherQueue((patcher) =>
    patchTurnByRunIdOn(threadId, resumeRunId, patcher),
  );
  return { kind: "resume", resumeRunId, materialized, fold, patches };
}

function foldTaskReplyFrame(
  get: StoreGetter,
  threadId: string,
  stream: TaskReplyStream,
  event: api.SendEvent,
): void {
  if (stream.kind === "send") {
    foldSendQuestionFrame(event, stream.send);
    return;
  }
  const current = get().activeThread;
  if (!current || current.id !== threadId) return;
  foldResumeFrame(stream.patches, stream.fold, event);
}

async function sendTaskReply(
  submission: TaskReplySubmission,
  get: StoreGetter,
): Promise<api.SendResult> {
  const { threadId, payload, stream } = submission;
  const onEvent = (event: api.SendEvent) =>
    foldTaskReplyFrame(get, threadId, stream, event);
  const result = await api.sendMessage(
    threadId,
    {
      question: payload.freeText ?? "",
      question_id: payload.questionId,
      choice_id: payload.choiceId,
      ...(payload.freeText !== null ? { free_text: payload.freeText } : {}),
    },
    onEvent,
  );
  if (stream.kind === "send") {
    // The NEW turn's row is the streamed one; the finalize patch is the
    // same one an ordinary send uses.
    finalizeSendQuestionTurn(get, {
      threadId,
      attempt: submission.attempt,
      stream: stream.send,
      result,
    });
  } else {
    // Drain queued deltas, then keep the row suspended if the stream ended
    // on a re-opened question. The streamed row remains authoritative.
    finalizeResumedTurn(stream.patches, threadId, stream.fold, result);
  }
  return result;
}

async function settleTaskReply(
  set: StoreSetter,
  get: StoreGetter,
  submission: TaskReplySubmission,
  result: api.SendResult,
): Promise<void> {
  const streamRefusal = midStreamTaskRefusal(result);
  if (streamRefusal !== null) {
    // A NEW turn's refusal row is a real streamed turn: keep it on
    // screen (the earlier silent drop hid a failed continuation). Only
    // a row materialized for a resume is rolled back.
    if (submission.stream.kind === "resume") {
      dropMaterializedTurn(
        submission.threadId,
        submission.stream.resumeRunId,
        submission.stream.materialized,
      );
    }
    recordReplyRefusal(set, get, {
      threadId: submission.threadId,
      attempt: submission.attempt,
      draftQuestionId: submission.payload.questionId,
      refusal: { kind: streamRefusal, questionId: submission.payload.questionId },
    });
    if (submission.stream.kind === "send") await get().refreshThreads();
    await get().reloadTaskProjection(submission.threadId);
    return;
  }
  recordOperationVerdict(set, {
    threadId: submission.threadId,
    attempt: submission.attempt,
    state: "accepted",
    questionId: submission.payload.questionId,
  });
  if (submission.stream.kind === "send") await get().refreshThreads();
  await get().reloadTaskProjection(submission.threadId);
}

async function failTaskReply(
  set: StoreSetter,
  get: StoreGetter,
  submission: TaskReplySubmission,
  error: unknown,
): Promise<void> {
  if (submission.stream.kind === "send") {
    rollbackSendStreamTurn(submission.threadId, submission.stream.send);
  } else {
    dropMaterializedTurn(
      submission.threadId,
      submission.stream.resumeRunId,
      submission.stream.materialized,
    );
  }
  await settleReplyFailure(set, get, {
    threadId: submission.threadId,
    attempt: submission.attempt,
    questionId: submission.payload.questionId,
    error,
  });
}

/** A monotonic token for the entry reads.
 *
 * The entry marker below holds one of these, NOT the thread id: two
 * concurrent reads of the SAME thread carry the same id, so an id cannot
 * say which of them is newer. A token can, and it is per read, so the
 * marker is also released when its read settles. */
let entryReadSeq = 0;

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  threads: [],
  corpus: [],
  activeThread: null,
  loadingThreads: false,
  threadsLoaded: false,
  corpusLoaded: false,
  catalogOverview: null,
  catalogOverviewLoaded: false,
  status: null,
  statusLoaded: false,
  loadingStatus: false,
  sendingQuestion: false,
  sendingThreadId: null,
  enteringReadToken: null,
  resumingThreadId: null,
  watchlist: [],
  notices: [],
  filingMonitorStatus: "healthy",
  filingMonitorError: null,
  stoppingThreadId: null,
  // question-workspace: empty until a thread GET projects an envelope.
  // An ABSENT key decodes to the empty envelope, so a taskless thread
  // and a legacy thread read identically (contract §Inventory: no
  // backfill, read as empty).
  taskRuntime: {},
  clarificationDrafts: {},
  workspaceOperations: EMPTY_WORKSPACE_OPERATIONS,
  restoreRequests: {},
  taskProjectionRefusal: {},
  taskProjectionRefreshFailed: {},
  attachments: [],
  pendingUploads: {},
  ingestJobId: null,
  ingestAccepted: null,
  ingestJob: null,
  ingestJobFailure: null,

  async refreshThreads() {
    set({ loadingThreads: true });
    try {
      const threads = await api.listThreads();
      set({ threads });
    } finally {
      set({ loadingThreads: false, threadsLoaded: true });
    }
  },

  async loadCorpus() {
    try {
      const corpus = await api.getCorpus();
      set({ corpus, corpusLoaded: true });
    } catch {
      set({ corpus: [], corpusLoaded: true });
    }
  },

  // A failing overview read is an EMPTY overview, never a stale list: the
  // surface shows nothing rather than documents that may no longer exist.
  // ``catalogOverviewLoaded`` flips either way so the panel stops saying
  // 加载中 and starts saying the truth about what it has.
  async loadCatalogOverview() {
    try {
      const catalogOverview = await api.getCatalogOverview();
      set({ catalogOverview, catalogOverviewLoaded: true });
    } catch {
      set({
        catalogOverview: { documents: [], dimensions: [], flat_titles: [] },
        catalogOverviewLoaded: true,
      });
    }
  },

  async loadStatus() {
    // A failing or malformed status read is a left-corner line state,
    // not an error the operator has to act on: the workspace keeps
    // working without it. ``statusLoaded`` still flips so the line
    // stops showing "检查中" and shows its unknown copy instead (no
    // silent OK on a probe miss).
    //
    // In-flight guard: a concurrent caller MUST NOT issue a second
    // wire call. The store tracks ``loadingStatus`` and treats a
    // concurrent call as a no-op while it is set.
    const existing = get();
    if (existing.loadingStatus) {
      return;
    }
    set({ loadingStatus: true });
    try {
      set({ status: await api.getStatus(), statusLoaded: true });
    } catch {
      set({ status: null, statusLoaded: true });
    } finally {
      set({ loadingStatus: false });
    }
  },

  async openThread(threadId: string): Promise<ThreadOpenOutcome> {
    // T2 (plans/2026-10-20-qa-design-correction.md): the entry guard.
    // While THIS client is sending on the thread AND the pane still
    // holds it, the live send owns it. A read here would be redundant
    // AND harmful: its snapshot predates the RunRecord the running
    // answer is about to write, so the reconcile could drop the live
    // turn and take the streamed prose with it. The route would also
    // show its entry loader over an answer that is already arriving.
    // The three entry paths (the demo action, the route effect, the
    // thread page re-sync) all reach this one guard, so they cannot
    // disagree about it.
    //
    // The rule itself is the SHARED one (``threadEntryIsLive``): the
    // route's entry decision and the thread page's re-sync ask the same
    // predicate, so what the guard skips is exactly what the route
    // paints as live.
    //
    // The second condition keeps a REQUIRED read alive: when the
    // operator left the thread (the pane holds another one), the live
    // state is gone and this thread must still be read.
    if (
      threadEntryIsLive({
        threadId,
        sendingThreadId: get().sendingThreadId,
        activeThreadId: get().activeThread?.id ?? null,
      })
    ) {
      return { kind: "skipped", threadId, reason: "sending" };
    }
    // The newest entry wins: a read a LATER entry superseded must not
    // repaint the active thread the operator already left. The marker
    // holds this read's own TOKEN: a thread id cannot order two
    // concurrent reads of the SAME thread, and the marker is released
    // when the read settles.
    const readToken = (entryReadSeq += 1);
    set({ enteringReadToken: readToken });
    // Unknown id -> 404 -> surface as empty active thread so the
    // route can render its not-found copy. We do NOT re-invoke the
    // graph on reload; GET only.
    // Attach door: the attachment slice belongs to the thread
    // being opened — stop the previous thread's readiness poll and
    // clear its records before the new detail lands.
    stopAttachmentPolling();
    set({ attachments: [] });
    // The live turn table as it stands BEFORE the read. If it moves
    // while the read is in flight, the read's snapshot is older than
    // what the operator is watching, and the reconcile must not drop
    // or blank the live rows it does not know about.
    const liveAtRead = get().activeThread;
    const detail = await api.getThread(threadId);
    const superseded = get().enteringReadToken !== readToken;
    const stale = get().activeThread !== liveAtRead;
    if (!superseded) {
      // T5: a successfully opened thread becomes the one a
      // returning visit restores.
      rememberLastThread(threadId);
      // T4 (plans/workspace-stream-fidelity.md): reconcile, do not
      // replace. Replacing the turn table retracted prose the operator
      // had already watched arrive token-by-token and repainted the
      // persisted text over it.
      set((s) => ({
        activeThread: reconcileTurnTable(s.activeThread, detail, { stale }),
      }));
      // question-workspace W4: the GET is the recovery path. The task
      // projection, the pending clarification and the archive list come
      // back from authoritative state, so a reload restores them WITHOUT
      // submitting empty input and WITHOUT re-invoking the graph —
      // display recovery is separate from resuming execution
      // (question-workspace.md: "Reopening a pending question does not
      // require an empty or bodyless resume request").
      const decoded = decodeThreadTaskRuntime(detail);
      set((s) => ({
        taskRuntime: { ...s.taskRuntime, [threadId]: decoded.view },
      }));
      recordProjectionRefusal(set, threadId, decoded.refusal);
    }
    // A SUPERSEDED read wrote nothing: its snapshot is older than the
    // read that took over, so it must not replace that read's turn
    // table, its marker or its projection.
    //
    // The read has settled: the marker is released — but only if this
    // read still owns it, so a later read's token survives.
    if (get().enteringReadToken === readToken) {
      set({ enteringReadToken: null });
    }
    return superseded
      ? { kind: "superseded", threadId }
      : { kind: "opened", threadId };
  },

  async createThread(firstMessage: string | null) {
    // The same client-wide gate as a send: an upload in flight on ANY
    // thread holds this one composer, and starting a conversation is
    // part of it — the host, by contrast, holds only the thread that
    // upload is landing in.
    refuseUploadInFlight(get().pendingUploads);
    const created = await api.createThread(firstMessage);
    set((s) => ({ threads: [created, ...s.threads] }));
    return created.id;
  },

  async removeThread(threadId: string) {
    await api.deleteThread(threadId);
    // T5: a deleted thread must never be restored on the next
    // visit — drop the marker when it pointed here.
    forgetLastThreadIf(threadId);
    // Attach door: the host drops the thread's attachment data
    // with the thread, so the slice and its poll retire too.
    if (get().activeThread?.id === threadId) stopAttachmentPolling();
    set((s) => ({
      attachments: s.activeThread?.id === threadId ? [] : s.attachments,
      threads: s.threads.filter((t) => t.id !== threadId),
      activeThread:
        s.activeThread && s.activeThread.id === threadId
          ? null
          : s.activeThread,
    }));
  },

  async sendQuestion(threadId, question, thinkingOn = true, attachmentIds) {
    const attempt = beginMessageSend(set, get, threadId, question);
    if (attempt === null) return null;
    // T2: the thread-scoped in-flight fact, set SYNCHRONOUSLY — before
    // the first await of this send — so a route mount right after
    // ``sendQuestion(...)`` already sees it and the entry guard can
    // reuse the live answer instead of reading the thread again.
    set({ sendingQuestion: true, sendingThreadId: threadId });
    const stream = createSendQuestionStream(get, set, threadId, question);
    const operation = { threadId, attempt, stream };
    const request = makeSendRequest(question, thinkingOn, attachmentIds);
    try {
      const result = await api.sendMessage(threadId, request, (event) =>
        foldSendQuestionFrame(event, stream),
      );
      await settleSendQuestion(set, get, { ...operation, result });
      return result;
    } catch (error) {
      failSendQuestion(set, operation, error);
      throw error;
    } finally {
      // The send settled — success, failure or cancellation. The
      // thread-scoped fact is released, but only if this send still
      // owns it: a later send on another thread keeps its own id.
      set((s) => ({
        sendingQuestion: false,
        sendingThreadId:
          s.sendingThreadId === threadId ? null : s.sendingThreadId,
      }));
    }
  },

  async resumeThread(threadId) {
    // Guard: no double-resume on the same thread. Sending on a
    // different thread is fine — it is a different lock. Empty /
    // completed threads resolve to 204 from the server; we still
    // re-GET to keep the turn table honest.
    if (get().resumingThreadId === threadId) return null;
    set({ resumingThreadId: threadId });

    // Optimistic local patch: while the resume is streaming, mark
    // the most-recent persisted turn as in_flight so the banner +
    // Resume button can render. Cleared by the status frame or
    // finalize patch below (no re-GET over the streamed row).
    const before = get().activeThread;
    if (before && before.id === threadId && before.turns.length > 0) {
      const last = before.turns.length - 1;
      const nextTurns = before.turns.map((t, i) =>
        i === last ? { ...t, in_flight: true } : t,
      );
      set({
        activeThread: { ...before, in_flight: true, turns: nextTurns },
      });
    }

    try {
      // Same delta semantics as sendQuestion (T1): append while
      // streaming; post-status empty frame clears refuse/step_cap draft.
      // The fold starts from the prose already painted on the resumed
      // row so the first delta extends it instead of replacing it.
      const fold: StreamFold = {
        statusSeen: false,
        windowPending: null,
        documentChoicePending: null,
        answerFold: {
          ...EMPTY_LIVE_ANSWER,
          visible: latestTurnAnswer(threadId),
        },
      };
      // Coalesced patcher (see sendQuestion): SSE deltas land in one
      // animation-frame flush instead of one patch per wire frame.
      // Non-delta frames go through ``flushThen`` so a queued delta
      // can never be overtaken — or applied after — the terminal
      // patch.
      const latestTurnPatches = makeTurnPatcherQueue((fn) =>
        patchLatestTurn(threadId, fn),
      );
      const result = await api.resumeThread(threadId, (event) => {
        // Switch-thread guard: if the operator navigated away
        // mid-resume, drop the patches rather than painting them
        // onto a different thread. Same guard as sendQuestion's
        // patchTurn.
        const cur = get().activeThread;
        if (!cur || cur.id !== threadId) return;
        foldResumeFrame(latestTurnPatches, fold, event);
      });

      // Same as sendQuestion: do not re-GET over the streamed row.
      finalizeResumedTurn(latestTurnPatches, threadId, fold, result);
      await get().refreshThreads();
      return result;
    } finally {
      // Only clear if still pointing at THIS thread — never clobber
      // a freshly-started resume on another thread.
      if (get().resumingThreadId === threadId) {
        set({ resumingThreadId: null });
      }
    }
  },

  async pickChoice(threadId, runId, optionId) {
    // Local gates: one resume/pick per thread, and a send cannot slip
    // in while a pick stream is open. The host answers 409 for both;
    // the SPA blocks first so the operator never sees a doomed POST.
    if (get().resumingThreadId === threadId || get().sendingQuestion) {
      return null;
    }
    const current = get().activeThread;
    if (!current || current.id !== threadId) return null;
    // Find the row the pick continues, or materialize one from a reloaded
    // ``pending_turn`` projection (see ``resolvePickTurn``). ``setTo``
    // installs the materialized row; the ordinary live-turn path passes
    // ``null`` here, so its behavior is unchanged.
    const resolved = resolvePickTurn(current, runId);
    if (resolved === null) return null;
    const { turn, materialized, setTo } = resolved;
    if (setTo !== null) set({ activeThread: setTo });

    set({ resumingThreadId: threadId });
    // Collapse the window immediately: pick 后窗口收起. An invalid
    // pick re-emits a fresh ``choice_window`` frame (with
    // ``rejected_picks``) and re-opens this same turn.
    const patchTarget = (patcher: (t: Turn) => Turn) =>
      patchTurnByRunIdOn(threadId, runId, patcher);
    patchTarget((t) => ({ ...t, choice_window: undefined, in_flight: true }));

    try {
      const fold: StreamFold = {
        statusSeen: false,
        windowPending: null,
        documentChoicePending: null,
        answerFold: { ...EMPTY_LIVE_ANSWER, visible: turn.answer },
      };
      const patches = makeTurnPatcherQueue(patchTarget);
      const result = await api.resumeThread(
        threadId,
        (event) => {
          const cur = get().activeThread;
          if (!cur || cur.id !== threadId) return;
          foldResumeFrame(patches, fold, event);
        },
        optionId,
      );
      finalizeResumedTurn(patches, threadId, fold, result);
      await get().refreshThreads();
      // Same post-settle re-read as sendQuestion: a pick can change the
      // task envelope, and the panel must not keep the pre-pick
      // projection.
      await get().reloadTaskProjection(threadId);
      return result;
    } catch (error) {
      // The pick failed (409 concurrent_run / 503 ingest_missing /
      // transport). Roll back only a row THIS call materialized,
      // re-arming the pre-pick ``pending_turn`` (see
      // ``rollbackMaterializedPick``); a pre-existing live-path row is
      // left exactly as it was, matching the behavior before this
      // rollback existed.
      rollbackMaterializedPick(threadId, runId, current.pending_turn, materialized);
      throw error;
    } finally {
      if (get().resumingThreadId === threadId) {
        set({ resumingThreadId: null });
      }
    }
  },

  async loadWatchlist() {
    // A watchlist that cannot be read is an empty panel with its own
    // copy, not a broken workspace: the thread surface keeps working.
    try {
      set({ watchlist: await api.listWatchlist() });
    } catch {
      set({ watchlist: [] });
    }
  },

  async addWatch(issuer, forms) {
    // Rethrows: a blank issuer is a typed 400 the panel renders inline
    // next to the input, so it must not be swallowed here.
    set({ watchlist: await api.createWatch(issuer, forms) });
    await get().loadNotices();
  },

  async removeWatch(watchId) {
    await api.deleteWatch(watchId);
    // The host cascades the watch's notices; mirror that locally so the
    // panel does not keep rows the operator can no longer act on.
    set((s) => ({
      watchlist: s.watchlist.filter((w) => w.id !== watchId),
      notices: s.notices.filter((n) => n.watch_id !== watchId),
    }));
  },

  async loadNotices() {
    try {
      set({
        notices: await api.listNotices(),
        filingMonitorStatus: "healthy",
        filingMonitorError: null,
      });
    } catch (error) {
      if (error instanceof api.FilingMonitorDegradedError) {
        set({
          notices: error.notices,
          filingMonitorStatus: "degraded",
          filingMonitorError: error.code,
        });
        return;
      }
      set({ notices: [] });
    }
  },

  async dismissNotice(noticeId) {
    await api.markNoticeRead(noticeId);
    set((s) => ({
      notices: s.notices.map((n) =>
        n.id === noticeId ? { ...n, read: true } : n,
      ),
    }));
  },

  async stopRun(threadId) {
    if (get().stoppingThreadId === threadId) return;
    set({ stoppingThreadId: threadId });
    try {
      await api.stopRun(threadId);
    } finally {
      if (get().stoppingThreadId === threadId) {
        set({ stoppingThreadId: null });
      }
    }
  },

  // ----- question-workspace: the task doors -----

  openTaskClarification(threadId) {
    const question = pendingQuestionFor(get(), threadId);
    // A draft already exists for this question: a re-render must not
    // replace a typed draft with a fresh empty one. The operator's text
    // is their own input, not derived state (specs/question-workspace.md
    // "Preserve … the user's draft").
    if (question === null) return;
    if (get().clarificationDrafts[question.question_id]) return;
    // A missing draft is recovered from storage under THIS question's id,
    // so the association cannot shift onto another question. Only the
    // operator's own pick and free text come back: the prompt, the choices
    // and their order stay authoritative from the projection, and a pick
    // the host no longer offers is dropped rather than sent.
    const record = readPersistedDraftRecords()[question.question_id];
    setDraft(set, get, question.question_id, restoreDraftText(question, record));
  },

  selectTaskChoice(threadId, choiceId) {
    patchDraft(set, get, threadId, (draft) => selectChoice(draft, choiceId));
  },

  setTaskOtherText(threadId, text) {
    patchDraft(set, get, threadId, (draft) => setOtherText(draft, text));
  },

  dismissTaskClarification(threadId) {
    // Presentation-only. The question stays PENDING on the host and
    // remains recoverable from the authoritative projection, so this
    // never submits anything (question-workspace.md).
    patchDraft(set, get, threadId, dismissClarification);
  },

  reopenTaskClarification(threadId) {
    // Presentation-only, and the inverse of the dismiss above: it clears
    // only the collapse so a typed answer survives. It deliberately does
    // NOT go through ``selectChoice``, which would make a card the reader
    // never answered look answered (SC-3).
    patchDraft(set, get, threadId, reopenClarification);
  },

  async submitTaskReply(threadId) {
    const prepared = prepareTaskReply(get, threadId);
    if (prepared === null) return;
    const attempt = beginTaskReplyOperation(set, get, threadId, prepared.payload);
    if (attempt === null) return;
    const submission: TaskReplySubmission = {
      ...prepared,
      threadId,
      attempt,
      stream: prepareTaskReplyStream(
        get,
        set,
        threadId,
        prepared.question,
        prepared.payload,
      ),
    };
    try {
      const result = await sendTaskReply(submission, get);
      await settleTaskReply(set, get, submission, result);
    } catch (error) {
      await failTaskReply(set, get, submission, error);
    }
  },

  async restoreArchivedTask(threadId, archiveId) {
    // Explicit only: nothing on this surface ever proposes a restore from
    // message text (specs/question-workspace.md §"Active work, outcomes,
    // and history"), and nothing is sent for an archive this thread's
    // projection does not offer.
    const archives = offeredArchives(get(), threadId);
    const taskId = listedRestoreTarget(archives, archiveId);
    if (taskId === null) {
      recordUnlistedRestoreRefusal(set, get, threadId, archiveId);
      return;
    }
    const opened = beginWorkspaceOperation(get().workspaceOperations, {
      threadId,
      door: "restore",
      kind: "restore",
      archiveId,
      taskId,
    });
    if (opened.operation === null) return;
    set((s) => ({
      workspaceOperations: opened.ledger,
      // The request is remembered as the row was OFFERED, listing included.
      // While the host rechecks, the EXISTING current task stays current:
      // the request is a record of intent, never a change of state.
      restoreRequests: {
        ...s.restoreRequests,
        [threadId]: restoreRequestFor(archiveId, taskId, archives),
      },
    }));
    const attempt = opened.operation.attempt;
    const facts: RestoreDoorFacts = { threadId, attempt, archiveId, taskId };
    try {
      const result = await api.sendMessage(threadId, {
        question: "",
        restore_archive_id: archiveId,
      });
      // A task door can refuse MID-STREAM: the acceptance succeeded (the
      // restore is durably recorded) and the reconciliation then refuses
      // it, which arrives as a terminal ``refuse`` frame carrying the
      // named failure rather than as an HTTP error. Reading only the HTTP
      // status would call that an accepted restore and show nothing to the
      // operator, so the named failure is checked here too — through the
      // same resolver the reply door uses, so the two cannot drift.
      recordRestoreOutcome(set, facts, midStreamTaskRefusal(result));
      await get().reloadTaskProjection(threadId);
    } catch (error) {
      const named = namedTaskRefusal(error) !== null;
      recordRestoreFailure(set, facts, error);
      // The refusal is the host's own answer about a request that reached
      // it, so the host still owns the state on screen: re-read rather
      // than leave the archive listing as it was at click time. A
      // transport failure has no such answer to read, and the request
      // already records it as unpaid.
      if (named) await get().reloadTaskProjection(threadId);
    }
  },

  async reloadTaskProjection(threadId) {
    try {
      const detail = await api.getThread(threadId);
      const decoded = decodeThreadTaskRuntime(detail);
      // Only the task projection is replaced. ``activeThread`` keeps its
      // streamed turns — a task re-read must never repaint the turn
      // table the operator is watching arrive (the same reconcile-not-
      // replace rule ``openThread`` follows).
      set((s) => ({
        taskRuntime: { ...s.taskRuntime, [threadId]: decoded.view },
      }));
      recordProjectionRefusal(set, threadId, decoded.refusal);
      // A successful read also clears the wire-failure marker a previous
      // re-read left: the marker names the LAST read, not the worst one.
      set((s) => ({
        taskProjectionRefreshFailed: {
          ...s.taskProjectionRefreshFailed,
          [threadId]: undefined,
        },
      }));
      // This read is the one a pending restore request gets judged
      // against — but only if it actually landed readable. A decode refusal
      // leaves the request owed rather than answered, so the archive stays
      // ``unresolved`` with the current task visible instead of being
      // confirmed by a read that read nothing.
      markRestoreProjectionSeen(set, threadId, decoded.refusal === null);
    } catch {
      // EXIT: presentation-only refresh — the GET failed (transport /
      // 5xx / host restart), but the caller is a post-door refresh or a
      // mount effect, not the operator's direct action, so rethrowing
      // would fail an already-answered door or crash an effect. Instead
      // the previous projection STAYS, and the unavailability itself is
      // recorded observably (``taskProjectionRefreshFailed``) rather
      // than swallowed: a stale-but-kept panel must not read as "the
      // host confirmed this is current". Not a decode refusal (nothing
      // was read), so the projection refusal slot is deliberately
      // untouched.
      set((s) => ({
        taskProjectionRefreshFailed: {
          ...s.taskProjectionRefreshFailed,
          [threadId]: true,
        },
      }));
    }
  },

  async ingestFile(file, meta) {
    // The corpus-ingest door (ADR 0047): it durably ACCEPTS the upload
    // and starts polling the job; parse/切分/push happen off the request
    // in a later worker. Named failures rethrow so the control can render
    // them (ingestFailure.ts) — no half-success here. A success is an
    // ACCEPT, not a completed ingest, so the corpus list is deliberately
    // NOT refreshed on this response: the catalog row only exists once
    // the job has pushed and registered the source, which is when
    // ``applyIngestJobStatus`` refreshes it.
    const accepted = await api.ingestCorpus(file, meta);
    persistIngestJobId(accepted.job_id);
    set({
      ingestJobId: accepted.job_id,
      ingestAccepted: accepted,
      ingestJob: null,
      ingestJobFailure: null,
    });
    startIngestJobPolling(accepted.job_id);
    return accepted;
  },

  async loadIngestJob(jobId, intervalMs = INGEST_JOB_POLL_INTERVAL_MS) {
    // The job to follow: an explicit id, else the one this session is
    // already on, else the id persisted before a reload. With nothing
    // ever submitted this is a no-op, so a first visit to home shows no
    // job panel instead of an empty read. The immediate read and the
    // "keep following while non-terminal" decision share the one tick
    // body so a transport blip is handled identically on first paint and
    // every poll.
    const target = jobId ?? get().ingestJobId ?? readPersistedIngestJobId();
    if (!target) return;
    stopIngestJobPolling();
    set({ ingestJobId: target });
    await ingestJobPollTick(target, intervalMs);
  },

  async attachFile(threadId: string, file: File) {
    // ADR 0089: the POST answers once the raw bytes are durable, so the
    // minted record enters the slice as ``uploaded`` and NO readiness
    // poll starts — that row only moves when a graph turn asks for
    // preparation. Deliberately does NOT touch /api/corpus/ingest: the
    // two doors must not cross.
    //
    // The upload is counted in ``pendingUploads`` for exactly the window
    // in which no record exists yet to be read off the attachment list,
    // which is the window the send gate has to cover.
    set((s) => ({
      pendingUploads: {
        ...s.pendingUploads,
        [threadId]: (s.pendingUploads[threadId] ?? 0) + 1,
      },
    }));
    try {
      const attachment = await api.attachToThread(threadId, file);
      if (get().activeThread?.id === threadId) {
        set((s) => ({
          attachments: [
            // A re-upload of the same id (retry races a poll) replaces
            // rather than duplicates the record.
            ...s.attachments.filter(
              (a) => a.attachment_id !== attachment.attachment_id,
            ),
            attachment,
          ],
        }));
      }
      // Only a host on the superseded upload-time-parse contract reaches
      // this: it answers ``parsing`` from the POST, and that chip must
      // keep moving instead of sitting unpolled. The door in this repo
      // answers ``uploaded`` and starts nothing.
      if (attachment.status === "parsing") {
        startAttachmentPolling(threadId);
      }
      return attachment;
    } finally {
      set((s) => {
        const next = { ...s.pendingUploads };
        const remaining = (next[threadId] ?? 1) - 1;
        // Drop the key at zero: a settled upload must never leave a
        // count that keeps the composer locked.
        if (remaining > 0) next[threadId] = remaining;
        else delete next[threadId];
        return { pendingUploads: next };
      });
    }
  },

  async loadAttachments(threadId, intervalMs = ATTACHMENT_POLL_INTERVAL_MS) {
    // Seed the thread's attachment slice (Composer open) and keep it
    // fresh while a preparation can still move a record: anything
    // ``parsing``, or the whole uploaded → ready window of a run
    // suspended in the graph (``attachmentPreparationWait``). This
    // read OWNS the poll cadence: the running chain (if any) is
    // replaced so the caller's ``intervalMs`` cannot be silently
    // ignored. Attachments are auxiliary to the thread view: a failed
    // door read leaves the slice as-is and ENDS the chain rather than
    // failing navigation or spinning on a dead endpoint — the next
    // open or attach restarts it; a retry answers a settled record
    // and therefore never restarts it.
    stopAttachmentPolling();
    const attachments = await readAttachmentList(threadId);
    if (attachments === null) return;
    if (get().activeThread?.id !== threadId) return;
    set({ attachments });
    const wait = attachmentPreparationWait(get().activeThread);
    if (wait) {
      // A suspended preparation is not a crashed run: the wait state
      // is the chip, never the Resume banner (see the helper).
      suppressResumePromptForWait();
    }
    if (wait || attachments.some((a) => a.status === "parsing")) {
      startAttachmentPolling(threadId, intervalMs, wait);
    }
  },

  async retryAttachment(threadId, attachmentId) {
    // This door drives preparation INSIDE the request, so the record it
    // hands back is already settled — ``ready`` on success, ``failed``
    // with its named reason. Patching the slice with it is the whole
    // job: a settled record cannot change by itself, so retry starts no
    // readiness poll (a list read owns the chain).
    const updated = await api.retryThreadAttachment(threadId, attachmentId);
    if (get().activeThread?.id !== threadId) return;
    set((s) => ({
      attachments: s.attachments.map((a) =>
        a.attachment_id === attachmentId ? updated : a,
      ),
    }));
  },

  async deleteAttachment(threadId, attachmentId) {
    // Await the receipt before dropping the chip: a refusal is a fact about
    // this attachment's state (still owed, or not this thread's) that the
    // slice has to keep visible.
    await api.deleteThreadAttachment(threadId, attachmentId);
    if (get().activeThread?.id !== threadId) return;
    set((s) => ({
      attachments: s.attachments.filter(
        (a) => a.attachment_id !== attachmentId,
      ),
    }));
  },

  async setTurnFeedback(threadId, runId, value) {
    // The host is authoritative for the persisted value (last write
    // wins on a flip), so the confirmed record patches the row — not
    // the optimistic request value.
    const record = await submitFeedback(threadId, runId, value);
    const current = get().activeThread;
    if (!current || current.id !== threadId) return;
    set({
      activeThread: {
        ...current,
        turns: current.turns.map((t) =>
          t.run_id === runId ? { ...t, feedback: record.value } : t,
        ),
      },
    });
  },
}));

// T4 GET reconcile (plans/workspace-stream-fidelity.md): fold the
// persisted turn table into the one on screen. The server row stays
// authoritative for ids, status, citation quotes and thinking
// duration; ``mergeReconciledTurn`` keeps the prose (and trace) the
// operator already watched stream in so the bubble never retracts and
// repaints. Rows this session never streamed come straight from the
// server.
//
// ``stale`` says the read is OLDER than what is on screen: the live
// turn table moved while the read was in flight. The still-running row
// is then the exception the server cannot send — a RunRecord is
// appended only once its stream completes, so a mid-run reopen carries
// the live row over — and a read that lands AFTER the terminal status
// frame (which already cleared the in-flight flag) must carry it over
// too, or the streamed prose disappears with the row.
function reconcileTurnTable(
  streamed: ThreadDetail | null,
  persisted: ThreadDetail,
  options: { stale: boolean },
): ThreadDetail {
  if (!streamed || streamed.id !== persisted.id) return persisted;
  const streamedByRunId = new Map<string, Turn>(
    streamed.turns.map((t) => [t.run_id, t]),
  );
  const persistedRunIds = new Set(persisted.turns.map((t) => t.run_id));
  const reconciled = persisted.turns.map((row) => {
    const live = streamedByRunId.get(row.run_id);
    if (!live) return row;
    const merged = mergeReconciledTurn(live, row);
    // E6 refusal fold across the two doors: mergeReconciledTurn is
    // server-row authoritative and knows nothing about this live-only
    // field, so the union happens HERE — stream-observed refusals and
    // the history read's rejected_citations rows keep one entry per
    // (index, code), and neither list may reorder the merged row's
    // citations (plan E6 acceptance).
    return {
      ...merged,
      evidence_refusals: mergeEvidenceRefusals(
        row.evidence_refusals,
        live.evidence_refusals,
      ),
    };
  });
  // The live rows the persisted table does not know about. A row is
  // absent from the persisted table when its RunRecord has not been
  // written yet — the server appends it when the run's stream ends.
  // While the read is FRESH the in-flight flag is the signal that the
  // row is still the server's to write; when the read is STALE its
  // snapshot predates the live table, so the row is kept whatever its
  // flag says: a terminal status frame already cleared it, and
  // dropping the row would take the streamed prose with it.
  const liveOnlyRows = streamed.turns.filter(
    (t) =>
      !persistedRunIds.has(t.run_id) &&
      (options.stale || t.in_flight === true),
  );
  return { ...persisted, turns: [...reconciled, ...liveOnlyRows] };
}

// Remove an optimistic turn that never received a single SSE frame.
// No-op when the operator has navigated away, so a failed send on
// thread A cannot delete a row on thread B.
function dropTurnByRunId(threadId: string, runId: Turn["run_id"]): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return;
  useWorkspaceStore.setState({
    activeThread: {
      ...current,
      turns: current.turns.filter((t) => t.run_id !== runId),
    },
  });
}

// Prose already on the resumed thread's most-recent turn — the live
// fold's starting point. Empty when the operator has navigated away
// or the thread has no turns yet.
function latestTurnAnswer(threadId: string): string {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return "";
  if (current.turns.length === 0) return "";
  return current.turns[current.turns.length - 1].answer;
}

// T4 helper: patch the most-recent turn on ``threadId`` (the one
// the resume is working on). No-op when the operator has navigated
// away so the resume cannot paint onto the wrong thread.
function patchLatestTurn(threadId: string, patcher: (t: Turn) => Turn): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return;
  if (current.turns.length === 0) return;
  const idx = current.turns.length - 1;
  const nextTurns = current.turns.map((t, i) => (i === idx ? patcher(t) : t));
  useWorkspaceStore.setState({
    activeThread: { ...current, turns: nextTurns },
  });
}

// search-mode-host T2 helper: patch ONE turn by run_id — the pick
// continues the turn that showed the window, which is not necessarily
// the latest row after a reload. No-op when the operator has
// navigated away.
function patchTurnByRunIdOn(
  threadId: string,
  runId: string,
  patcher: (t: Turn) => Turn,
): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return;
  if (!current.turns.some((t) => t.run_id === runId)) return;
  useWorkspaceStore.setState({
    activeThread: {
      ...current,
      turns: current.turns.map((t) =>
        t.run_id === runId ? patcher(t) : t,
      ),
    },
  });
}

// The origin run id a reply would RESUME, or ``null``. Only a
// ``document_choice`` question names the run it interrupted; a
// ``task_clarification`` (and any question without an origin run) is
// answered by a FRESH run on the send door and carries no resume identity,
// so it takes the door's original path.
function resumeOriginRunId(question: PendingQuestionView): string | null {
  if (question.purpose !== "document_choice") return null;
  const origin = question.origin_run_id;
  return typeof origin === "string" && origin.length > 0 ? origin : null;
}

// The reply door's target row for a RESUMED document choice. The host
// answers the pick by continuing the run the question names
// (``origin_run_id``) rather than minting a fresh one, and it persists the
// resumed remainder under that same id — but a paused run has no
// RunRecord yet, so after a reload ``turns[]`` holds no row for it. The
// resumed stream would then have nothing to paint into (and
// ``reloadTaskProjection`` deliberately never repaints turns). Materialize
// a live row under the origin run id when none exists, using the same
// shape the optimistic send row uses (``makeLiveTurn``) — the precedent
// ``resolvePickTurn`` set for a pick. Returns whether THIS call built the
// row, the signal the failure rollback keys on.
function ensureReplyTurn(
  threadId: string,
  runId: string,
  question: string,
): boolean {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return false;
  if (current.turns.some((t) => t.run_id === runId)) return false;
  const turn = makeLiveTurn({ runId, question });
  useWorkspaceStore.setState({
    activeThread: { ...current, turns: [...current.turns, turn] },
  });
  return true;
}

// Drop a reply row THIS call materialized when the reply failed (a typed
// pre-SSE refusal, a transport error, or a mid-stream task refusal). A
// materialized row is not yet a server record, so leaving it would strand
// a permanently in-flight ghost; a row that already existed (the live send
// path) is left exactly as it was, matching the pick path's rule.
function dropMaterializedTurn(
  threadId: string,
  runId: string,
  materialized: boolean,
): void {
  if (!materialized) return;
  const cur = useWorkspaceStore.getState().activeThread;
  if (!cur || cur.id !== threadId) return;
  useWorkspaceStore.setState({
    activeThread: {
      ...cur,
      turns: cur.turns.filter((t) => t.run_id !== runId),
    },
  });
}

// The original question the resumed reply answers, recovered from the
// accepted-input history by the run it continues. The original send
// accepted a ``message`` row carrying the operator's own words under that
// run id, so this is the one honest source for the row's prompt — the
// pending question's ``prompt`` is the question the HOST asks the
// operator, not the operator's question. Absent history (an older host)
// reads as no question rather than inventing one.
function recoveredReplyQuestion(
  state: Pick<WorkspaceState, "taskRuntime">,
  threadId: string,
  runId: string,
): string {
  const rows = state.taskRuntime[threadId]?.input_history ?? [];
  const row = rows.find((r) => r.run_id === runId && r.kind === "message");
  return row?.payload ?? "";
}

// search-mode-host T2 pick resolution: the row a pick continues, or — when
// a reloaded paused turn has no row yet — a fresh row built from the
// ``pending_turn`` projection. Returns ``null`` when there is nothing to
// pick: no such run, or an existing row that owns no window. ``setTo`` is
// the activeThread to install for the materialized case (the appended row
// and the dropped projection); it is ``null`` for the ordinary live path,
// whose row already exists. ``materialized`` is ``true`` only when THIS
// call built the row from ``pending_turn`` — the signal the failure
// rollback keys on, because a row that already existed must be left
// exactly as it was.
function resolvePickTurn(
  current: ThreadDetail,
  runId: string,
): { turn: Turn; materialized: boolean; setTo: ThreadDetail | null } | null {
  const existing = current.turns.find((t) => t.run_id === runId);
  if (existing !== undefined) {
    // A live-path row must own the window; without one there is no pick.
    if (existing.choice_window === undefined) return null;
    return { turn: existing, materialized: false, setTo: null };
  }
  // A reloaded paused turn has no row yet: the host keeps it OFF
  // ``turns[]`` and carries it on ``pending_turn`` instead
  // (ThreadDetail.pending_turn). Materialize the row from that projection
  // so the resumed stream paints its answer into a row exactly as the
  // live path does, then drop the projection — the window belongs to the
  // row now, so a stale thread-level card cannot survive the pick.
  const pending = current.pending_turn;
  if (!pending || pending.run_id !== runId) return null;
  // Same SHAPE as the optimistic send row (makeLiveTurn), plus the window
  // the pick was waiting on.
  const turn = makeLiveTurn({
    runId: pending.run_id,
    question: pending.question,
    choice: pending.choice_window,
  });
  return {
    turn,
    materialized: true,
    setTo: {
      ...current,
      turns: [...current.turns, turn],
      pending_turn: undefined,
    },
  };
}

// Roll back a pick that failed (409 concurrent_run / 503 ingest_missing /
// transport). A row THIS call materialized from ``pending_turn`` is not a
// server record — leaving it would strand a permanently ``in_flight``
// ghost with a collapsed window and no answer. Drop it and re-arm the
// ``pending_turn`` projection so the operator is back to the pre-pick
// state and can pick again. A pre-existing (live-path) row is left exactly
// as it was, matching the behavior before this rollback existed.
function rollbackMaterializedPick(
  threadId: string,
  runId: string,
  restorePendingTurn: ThreadDetail["pending_turn"],
  materialized: boolean,
): void {
  if (!materialized) return;
  const cur = useWorkspaceStore.getState().activeThread;
  if (!cur || cur.id !== threadId) return;
  useWorkspaceStore.setState({
    activeThread: {
      ...cur,
      turns: cur.turns.filter((t) => t.run_id !== runId),
      pending_turn: restorePendingTurn,
    },
  });
}

// ----- ADR 0089 / T2f: readiness polling and the preparation wait -----
//
// Preparation runs off the request, so the only way the SPA learns
// parsing → ready / failed is re-reading the thread's list. An
// ``uploaded`` record with no run waiting on it is NOT work in
// progress: the graph turn starts preparation, so polling an idle
// upload row would spin on a state that cannot change by itself.
// While a run IS waiting (``attachmentPreparationWait``) the poll
// covers the whole window — the row moves uploaded → parsing →
// ready/failed and the chain stops cleanly at a terminal status.
// One poller chain exists at a time, keyed by thread: opening another
// thread replaces it, and a read that finds nothing left to follow
// stops the chain (the next open, attach, or waiting send restarts
// it). ``intervalMs`` threads through the chain as a parameter so
// tests advance a fake schedule instead of really waiting.

const ATTACHMENT_POLL_INTERVAL_MS = 2000;

/** True for a record this client holds that a server list read may not
 * have reported yet (an upload it just minted, or a preparation the host
 * says is running) — the statuses a stale list can still be behind. */
function isRecordAwaitingServerList(attachment: ThreadAttachment): boolean {
  return (
    attachment.status === "uploaded" || attachment.status === "parsing"
  );
}

/**
 * T2f: is a run on this thread suspended inside the graph waiting for
 * attachment preparation?
 *
 * The host contract: a grounded send that references a durable
 * ``uploaded``/``parsing`` attachment is accepted, the run suspends on
 * the ``attachment_preparation`` interrupt, the thread lock is
 * released, and the SAME run auto-resumes when the durable job
 * finishes — the operator must never be asked to resend or press
 * Resume. GET /threads/{id} reports the pause as
 * ``pending_checkpoint: true`` PLUS what it waits for as
 * ``pending_interrupt_kind`` (SSOT:
 * src/fin/host/workspace_http.py get_thread — the same key the stream
 * carries on the ``attachment_wait`` frame). The wait holds for the
 * preparation kind ONLY: a ``choice_window`` pause or a ``null``
 * crash checkpoint keeps the Resume banner's behavior untouched, so
 * the pre-C4 misclassification — a genuine crash on a thread that
 * happened to hold an unreferenced uploaded/parsing record losing its
 * Resume prompt — is gone.
 */
export function attachmentPreparationWait(
  detail: ThreadDetail | null,
): boolean {
  return (
    detail?.pending_checkpoint === true &&
    detail?.pending_interrupt_kind === api.ATTACHMENT_PREPARATION_KIND
  );
}

/**
 * Paint the wait as what it is: a run in progress, not a checkpoint
 * the operator has to restart. ThreadPage's Resume banner keys off
 * ``activeThread.pending_checkpoint``, so while
 * ``attachmentPreparationWait`` holds the flag is cleared on the
 * stored detail — the chip (解析中…) and the poll are the wait's only
 * visible state. Re-derived from the server on every open, so a
 * genuine crash re-arms the banner on the next GET.
 */
function suppressResumePromptForWait(): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (current && current.pending_checkpoint === true) {
    useWorkspaceStore.setState({
      activeThread: { ...current, pending_checkpoint: false },
    });
  }
}

/**
 * The thread's attachment list, or ``null`` when the door refused.
 *
 * Both entry points that re-read the list (an open, a poll tick) treat a
 * refusal the same way: stop, keep what is on screen, let the next action
 * restart the chain. One place catches so the named door error is never
 * dropped twice.
 */
async function readAttachmentList(
  threadId: string,
): Promise<ThreadAttachment[] | null> {
  try {
    return await api.listThreadAttachments(threadId);
  } catch {
    return null;
  }
}

let attachmentPollTimer: ReturnType<typeof setTimeout> | null = null;
// Set when the CURRENT chain was started to follow a suspended run
// (a waiting send, or a load during the wait): then an ``uploaded``
// record is pending work too — the durable job has been told to move
// it and a stale list read may not show ``parsing`` yet. An upload
// no run references never starts a chain at all, so it still gets no
// poll.
let attachmentPollFollowsWait = false;

function stopAttachmentPolling(): void {
  if (attachmentPollTimer !== null) clearTimeout(attachmentPollTimer);
  attachmentPollTimer = null;
}

function scheduleAttachmentPollTick(
  threadId: string,
  intervalMs: number,
): void {
  attachmentPollTimer = setTimeout(
    () => void attachmentPollTick(threadId, intervalMs),
    intervalMs,
  );
}

/**
 * (Re)start the chain for ``threadId``. Replaces any running chain
 * rather than stacking one, so every entry point (open, attach,
 * waiting send) provably owns the next read.
 */
function startAttachmentPolling(
  threadId: string,
  intervalMs = ATTACHMENT_POLL_INTERVAL_MS,
  followsWait = false,
): void {
  stopAttachmentPolling();
  attachmentPollFollowsWait = followsWait;
  scheduleAttachmentPollTick(threadId, intervalMs);
}

/** True for a record a running or waiting preparation will move. */
function hasPreparationWork(attachments: readonly ThreadAttachment[]): boolean {
  return attachments.some(isRecordAwaitingServerList);
}

async function attachmentPollTick(
  threadId: string,
  intervalMs: number,
): Promise<void> {
  attachmentPollTimer = null;
  const state = useWorkspaceStore.getState();
  if (state.activeThread?.id !== threadId) {
    stopAttachmentPolling();
    return;
  }
  const attachments = await readAttachmentList(threadId);
  if (attachments === null) {
    // A failed read ENDS the chain: the next open / attach / retry
    // restarts it, and a dead or deleted door must not leave a
    // background loop spinning on timers.
    stopAttachmentPolling();
    return;
  }
  const current = useWorkspaceStore.getState();
  if (current.activeThread?.id !== threadId) {
    stopAttachmentPolling();
    return;
  }
  // A list read issued before a concurrent attach can answer WITHOUT
  // the just-minted record; dropping its chip would hide a live
  // attachment until the next manual open, so locally-known records the
  // stale response has not seen are carried over. ``uploaded`` counts:
  // an upload that starts no poll of its own would otherwise vanish
  // until the next open. Terminal statuses are never carried — the
  // server list is authoritative for them.
  const seen = new Set(attachments.map((a) => a.attachment_id));
  const carried = current.attachments.filter(
    (a) => isRecordAwaitingServerList(a) && !seen.has(a.attachment_id),
  );
  const merged = [...attachments, ...carried];
  useWorkspaceStore.setState({ attachments: merged });
  // The flag re-read on every open means a GET landing mid-wait can
  // re-arm the Resume banner while this chain is still following the
  // preparation; clear it again for as long as the wait holds.
  if (attachmentPreparationWait(current.activeThread)) {
    suppressResumePromptForWait();
  }
  if (
    merged.some((a) => a.status === "parsing") ||
    (attachmentPollFollowsWait && hasPreparationWork(merged))
  ) {
    scheduleAttachmentPollTick(threadId, intervalMs);
    return;
  }
  stopAttachmentPolling();
}

// ----- corpus-ingest job status polling -----
//
// The POST returns as soon as the upload is durable, so every later
// state is observable only through the status door. The chain mirrors
// the attachment one: one owner at a time, a failed read ends it rather
// than spinning on a dead endpoint, and it stops by itself at a terminal
// state. Only the state STRING is surfaced — no parse-progress UI, and
// the graph/C field is never rendered as text readiness.

const INGEST_JOB_POLL_INTERVAL_MS = 2000;

/** ``ready`` (per-source text) and ``failed`` (typed terminal) are the
 * two states the poll must not keep reading. */
function isIngestJobTerminal(state: IngestJobState): boolean {
  return state === "ready" || state === "failed";
}

/**
 * One job's status, or ``null`` when the door named the miss. A named
 * refusal (``unknown_job`` and friends) is recorded for the control to
 * render and stops the chain — the job is gone. A transport error is NOT
 * a verdict on the job, so it rethrows for the caller to keep following
 * it rather than freeze the panel at "pending".
 */
async function readIngestJobStatus(jobId: string): Promise<JobStatus | null> {
  try {
    return await api.getIngestJobStatus(jobId);
  } catch (error) {
    if (!(error instanceof api.IngestFailedError)) throw error;
    useWorkspaceStore.setState({ ingestJobFailure: error });
    if (error.code === "unknown_job") {
      // The submitted id outlived its job, so drop the marker the
      // same way a last-thread pointer to a deleted thread is
      // dropped: a later reload stops re-reading a dead id.
      forgetPersistedIngestJobIdIf(jobId);
    }
    return null;
  }
}

/**
 * Paint one confirmed status. Response-driven, like every other action
 * here: the store never guesses a state between reads.
 */
function applyIngestJobStatus(status: JobStatus): void {
  useWorkspaceStore.setState({ ingestJob: status, ingestJobFailure: null });
  if (status.state === "ready") {
    void useWorkspaceStore.getState().loadCorpus();
    // T6: the catalog row exists once the publication succeeded, so the
    // generic overview refreshes on the same signal the financial
    // projection does.
    void useWorkspaceStore.getState().loadCatalogOverview();
  }
}

let ingestJobPollTimer: ReturnType<typeof setTimeout> | null = null;

function stopIngestJobPolling(): void {
  if (ingestJobPollTimer !== null) clearTimeout(ingestJobPollTimer);
  ingestJobPollTimer = null;
}

function scheduleIngestJobPollTick(jobId: string, intervalMs: number): void {
  ingestJobPollTimer = setTimeout(
    () => void ingestJobPollTick(jobId, intervalMs),
    intervalMs,
  );
}

/** (Re)start the chain for ``jobId``, replacing any running one. */
function startIngestJobPolling(
  jobId: string,
  intervalMs = INGEST_JOB_POLL_INTERVAL_MS,
): void {
  stopIngestJobPolling();
  scheduleIngestJobPollTick(jobId, intervalMs);
}

async function ingestJobPollTick(
  jobId: string,
  intervalMs: number,
): Promise<void> {
  ingestJobPollTimer = null;
  if (useWorkspaceStore.getState().ingestJobId !== jobId) return;
  let status: JobStatus | null;
  try {
    status = await readIngestJobStatus(jobId);
  } catch (error) {
    // A transport blip says nothing about the job, so the chain must
    // not die on it: surface the failed read and keep following.
    useWorkspaceStore.setState({
      ingestJobFailure:
        error instanceof Error ? error : new Error(String(error)),
    });
    if (useWorkspaceStore.getState().ingestJobId === jobId) {
      scheduleIngestJobPollTick(jobId, intervalMs);
    }
    return;
  }
  if (status === null) return;
  if (useWorkspaceStore.getState().ingestJobId !== jobId) return;
  applyIngestJobStatus(status);
  if (isIngestJobTerminal(status.state)) return;
  scheduleIngestJobPollTick(jobId, intervalMs);
}

/**
 * The last submitted job id. Kept in storage so the home door can show
 * the job again after navigation or a reload, without re-uploading;
 * unavailable storage just means nothing is resumed.
 */
export const LAST_INGEST_JOB_KEY = "fin.lastIngestJob";

function readPersistedIngestJobId(): string | null {
  try {
    return window.localStorage.getItem(LAST_INGEST_JOB_KEY);
  } catch {
    return null; /* storage unavailable - nothing to resume. */
  }
}

function persistIngestJobId(jobId: string): void {
  try {
    window.localStorage.setItem(LAST_INGEST_JOB_KEY, jobId);
  } catch {
    /* storage unavailable - the job simply is not resumed on reload. */
  }
}

function forgetPersistedIngestJobIdIf(jobId: string): void {
  try {
    if (window.localStorage.getItem(LAST_INGEST_JOB_KEY) === jobId) {
      window.localStorage.removeItem(LAST_INGEST_JOB_KEY);
    }
  } catch {
    /* storage unavailable - nothing to clear. */
  }
}

/**
 * The one rule for whether a conversation may start or send.
 *
 * ``true`` while any upload POST is in flight on any thread — the
 * client's widest reading of the gate, because the composer is one box
 * for every thread, whereas the host only ever holds the thread its own
 * upload is landing in. It reads the store's ``pendingUploads`` slice,
 * not the attachment list: during that window the record does not exist
 * yet, so the list cannot answer. It is one half of the single
 * active-input decision (``activeInputFactsFor`` →
 * ``decideActiveInput``), shared by ``sendQuestion``, ``createThread`` and
 * the Composer's control, so the button, Enter and the POST cannot
 * disagree about whether a send is held — and a held send raises the named
 * refusal instead of returning nothing.
 * A file the question never mentions still blocks the send, because the
 * gate is about the upload, not about the reference.
 */
export function hasUploadInFlight(
  pendingUploads: Readonly<Record<string, number>>,
): boolean {
  return Object.values(pendingUploads).some((count) => count > 0);
}

/** One throw for both doors, so the refusal cannot drift between them. It
 * asks the SAME decision a send asks (``decideActiveInput``) with only the
 * upload half populated: a new conversation has no open thread yet, so the
 * pending-choice lock cannot apply and the submission has no state to
 * report. The refusal raised is therefore the upload one, named exactly as
 * the send door names it. */
function refuseUploadInFlight(
  pendingUploads: Readonly<Record<string, number>>,
): void {
  const decision = decideActiveInput({
    pendingChoiceWindow: false,
    uploadInFlight: hasUploadInFlight(pendingUploads),
    submission: "idle",
    transportFailed: false,
    refusalKind: null,
  });
  if (decision.holdReason !== null) throw heldRefusalFor(decision.holdReason);
}

/**
 * The one rule for what a send may reference, and what it deliberately
 * did not.
 *
 * Only records the host accepts ride the message (see the rule in the
 * loop below). The thread keeps every attachment for its whole life,
 * so a question that never mentioned one must not be refused on its
 * account either — a referenceable record is named automatically.
 * ``withheld`` is the other half of the contract —
 * the control renders it, so a record left out of a run is always said out
 * loud rather than silently dropped.
 */
export function attachmentsForSend(attachments: readonly ThreadAttachment[]): {
  ids: string[];
  withheld: ThreadAttachment[];
} {
  const ids: string[] = [];
  const withheld: ThreadAttachment[] = [];
  for (const attachment of attachments) {
    // T2f: a grounded send naming a durable ``uploaded``/``parsing``
    // record is accepted — the graph suspends the run until
    // preparation completes and resumes it automatically — so those
    // states ride the message. Only a refused record stays out: a
    // ``failed`` one, or a ``ready`` one the host does not report
    // searchable.
    const referenceable =
      attachment.status === "uploaded" ||
      attachment.status === "parsing" ||
      (attachment.status === "ready" && attachment.searchable);
    if (referenceable) {
      ids.push(attachment.attachment_id);
    } else {
      withheld.push(attachment);
    }
  }
  return { ids, withheld };
}
