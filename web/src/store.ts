// Zustand store for the workspace UI. T1 keeps the state minimal:
// thread list + corpus + the active thread's turns. Per-thread run
// bookkeeping (in-flight flags, streaming deltas) is T3.

import { create } from "zustand";
import * as api from "./api";
// T8: the thumbs-feedback boundary (POST .../runs/{run_id}/feedback).
import { submitFeedback, type FeedbackValue } from "./feedback";
// T5 (#35): the last opened thread id, persisted so a returning
// visit lands back on that thread.
import { forgetLastThreadIf, rememberLastThread } from "./lastThread";
import {
  hasPendingChoiceWindow,
} from "./choiceWindow";
import type {
  ChoiceWindow,
  Filing,
  IngestJobState,
  IngestResult,
  JobStatus,
  Notice,
  ThreadAttachment,
  ThreadDetail,
  ThreadListItem,
  ToolSummary,
  Turn,
  WatchEntry,
  WorkspaceStatus,
} from "./types";
import {
  EMPTY_LIVE_ANSWER,
  foldLiveAnswer,
  mergeReconciledTurn,
  type LiveAnswerState,
} from "./turnStream";
import {
  closeOpenBursts,
  foldTraceFrame,
  thinkingPendingItems,
  RUNNING_SUMMARY,
  type TraceFrame,
} from "./turnTrace";

interface WorkspaceState {
  threads: ThreadListItem[];
  corpus: Filing[];
  activeThread: ThreadDetail | null;
  loadingThreads: boolean;
  // T5 (#35): true once the FIRST refreshThreads completed. The
  // restore-on-return logic must distinguish "not loaded yet"
  // (initial ``loadingThreads === false`` before App's effect
  // starts the fetch) from "loaded and genuinely empty" — otherwise
  // the one-shot restore treats a valid marker as stale.
  threadsLoaded: boolean;
  corpusLoaded: boolean;
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
  // T4: ``resumeThread(threadId)`` sets this while the resume SSE
  // stream is active so the Composer can disable itself and so a
  // second resume click is rejected. ``null`` when no resume is in
  // flight. Tracks the THREAD, not just a boolean, so a resume on
  // thread A does not block a fresh send on thread B.
  resumingThreadId: string | null;
  // T4 workflow (#138): watched issuers and the in-app notices their
  // filings raised. Both are workspace-local — no outbound delivery.
  watchlist: WatchEntry[];
  notices: Notice[];
  filingMonitorStatus: "healthy" | "degraded";
  filingMonitorError: string | null;
  // ``true`` while a Stop request for the active thread is in flight,
  // so the button cannot be pressed twice before the wire closes.
  stoppingThreadId: string | null;

  refreshThreads: () => Promise<void>;
  loadCorpus: () => Promise<void>;
  // Re-GETs ``GET /api/status`` (boot effect). Boot-scoped fields do
  // not change mid-session, so a successful read updates the store; a
  // thrown fetch still flips ``statusLoaded`` so the left-corner line
  // renders its neutral state rather than a silent OK.
  loadStatus: () => Promise<void>;
  openThread: (threadId: string) => Promise<void>;
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
  // T4 workflow (#138). ``addWatch`` rejects with the host's typed
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
  // fresh while anything is "parsing". ``intervalMs`` is the poll
  // cadence, injectable so tests advance the schedule without real
  // waiting. A failed door read ends the chain and leaves the slice as
  // it is — attachments are auxiliary to the thread view and must not
  // fail navigation. The next open or attach restarts it; a retry
  // answers a settled record and therefore never restarts it.
  loadAttachments: (threadId: string, intervalMs?: number) => Promise<void>;
  // Re-run preparation for one failed attachment. The door answers with
  // the settled record (``ready`` / ``failed``); rejects with the named
  // ``AttachFailedError`` so the control can render the reason.
  retryAttachment: (threadId: string, attachmentId: string) => Promise<void>;
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

// turn-trace-folds (#250): fold one wire frame into the turn's burst
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

// Streaming render saturation (#252 follow-up): at provider chunk
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
  switch (event.type) {
    case "thinking":
      patches.coalesce((t) => ({
        ...foldTraceFrameOntoTurn(t, event),
        thinking_text: t.thinking_text + event.text,
      }));
      break;
    case "tool_call": {
      const placeholder: ToolSummary = {
        name: event.name,
        summary: RUNNING_SUMMARY,
        tool_call_id: event.tool_call_id,
      };
      patches.flushThen((t) => ({
        ...foldTraceFrameOntoTurn(t, event),
        tool_summaries: [...(t.tool_summaries ?? []), placeholder],
      }));
      break;
    }
    case "tool_result":
      patches.flushThen((t) => ({
        ...foldTraceFrameOntoTurn(t, event),
        tool_summaries: (t.tool_summaries ?? []).map((s) =>
          s.tool_call_id === event.tool_call_id
            ? { ...s, summary: event.summary }
            : s,
        ),
      }));
      break;
    case "status":
      fold.statusSeen = true;
      fold.windowPending = null;
      patches.flushThen((t) => ({
        ...closeOpenBurstsOnTurn(t),
        status: event.status,
        status_reason: event.reason ?? "",
        assistant_message: event.assistant_message,
        track: event.track ?? t.track,
        track_decision: event.track_decision ?? t.track_decision,
        // Mid-flight stream taxonomy (see sendQuestion's status
        // branch for the same handling rationale).
        status_classified:
          event.status_classified !== undefined
            ? event.status_classified
            : t.status_classified,
        in_flight: false,
        thinking_duration_ms: event.thinking_ms ?? t.thinking_duration_ms,
      }));
      break;
    case "answer":
      fold.answerFold = foldLiveAnswer(fold.answerFold, {
        text: event.text,
        reset: event.reset,
        statusSeen: fold.statusSeen,
      });
      patches.coalesce((t) => ({
        // Same answer-closes-burst contract as sendQuestion.
        ...foldTraceFrameOntoTurn(t, { type: "answer" }),
        answer: fold.answerFold.visible,
      }));
      break;
    case "citations":
      patches.flushThen((t) => ({ ...t, citations: event.citations }));
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
  }
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
    in_flight: fold.windowPending !== null,
    citations: result.citations.length > 0 ? result.citations : t.citations,
    track: result.track ?? t.track,
    track_decision: result.track_decision ?? t.track_decision,
    status_classified: result.status_classified ?? t.status_classified,
  }));
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  threads: [],
  corpus: [],
  activeThread: null,
  loadingThreads: false,
  threadsLoaded: false,
  corpusLoaded: false,
  status: null,
  statusLoaded: false,
  loadingStatus: false,
  sendingQuestion: false,
  resumingThreadId: null,
  watchlist: [],
  notices: [],
  filingMonitorStatus: "healthy",
  filingMonitorError: null,
  stoppingThreadId: null,
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

  async openThread(threadId: string) {
    // Unknown id -> 404 -> surface as empty active thread so the
    // route can render its not-found copy. We do NOT re-invoke the
    // graph on reload; GET only.
    // Attach door: the attachment slice belongs to the thread
    // being opened — stop the previous thread's readiness poll and
    // clear its records before the new detail lands.
    stopAttachmentPolling();
    set({ attachments: [] });
    const detail = await api.getThread(threadId);
    // T5 (#35): a successfully opened thread becomes the one a
    // returning visit restores.
    rememberLastThread(threadId);
    // T4 (plans/workspace-stream-fidelity.md): reconcile, do not
    // replace. Replacing the turn table retracted prose the operator
    // had already watched arrive token-by-token and repainted the
    // persisted text over it.
    set((s) => ({ activeThread: reconcileTurnTable(s.activeThread, detail) }));
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
    // T5 (#35): a deleted thread must never be restored on the next
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
    if (!question.trim()) return null; // Empty submit is a no-op.
    if (get().sendingQuestion) return null; // No queue in T1.
    // search-mode-host T2: while a choice window is pending the run
    // holds the thread — the host answers 409 concurrent_run, so the
    // SPA rejects with the SAME typed error instead of firing a
    // doomed POST. Surfacing it (not a silent no-op) keeps the
    // composer's notice + draft-restoration path honest.
    const gate = get().activeThread;
    if (gate && gate.id === threadId && hasPendingChoiceWindow(gate.turns)) {
      throw new api.ConcurrentRunError("concurrent_run");
    }
    // Upload gate: no conversation sends while ANY composer upload is in
    // flight, including a file the question does not name (its record
    // does not exist yet). This client scope is the stricter one: the
    // composer is global, so it holds for every thread's upload, while
    // the host refuses only the thread its own upload is landing in.
    // Deciding it here keeps a doomed POST off the wire.
    refuseUploadInFlight(get().pendingUploads);
    set({ sendingQuestion: true });

    // T3: optimistic turn the SSE frames thread into. Allocated
    // up front so the api client can ``onEvent`` updates against
    // the SAME turn (matched by ``run_id``). On the first
    // ``status`` frame we reconcile the run_id with the
    // server-minted one and patch the turn.
    // ``run_id`` is typed as the UUID template literal in types.ts;
    // crypto.randomUUID() already satisfies it. The template-literal
    // type is what makes the reconcile-on-status patch typecheck.
    let optimisticRunId: Turn["run_id"] = crypto.randomUUID();
    const baseTurn: Turn = {
      run_id: optimisticRunId,
      question,
      status: "done",
      status_reason: "",
      answer: "",
      citations: [],
      thinking_text: "",
      thinking_duration_ms: 0,
      tool_summaries: [],
      assistant_message: "",
      // Mid-flight stream error taxonomy: empty until the
      // terminal ``status`` SSE frame arrives; the SPA renders
      // the "原因" label off this field on refuse rows.
      status_classified: "",
      // T3 (enterprise-kb Focus 1): filled by ``comparison`` frames
      // as each ``compare_facts`` call returns, so the table is on
      // screen while the answer is still being authored.
      comparisons: [],
      // T4 (#37): the optimistic turn is LIVE until the terminal
      // status frame — the thinking region line-clamps and the tool
      // rows stay open while the run streams, then collapse.
      in_flight: true,
      // thinking pending: same 深度思考 chrome until the first
      // thinking frame (or drop on first tool_call / answer).
      trace_items: thinkingPendingItems(),
      created_at: new Date().toISOString(),
    };

    // Helpers bound to the latest state so ``onEvent`` callbacks
    // always patch the most-recent snapshot of the optimistic
    // turn (the array reference is stable across renders).
    const patchTurnByRunId = (
      runId: Turn["run_id"],
      patcher: (t: Turn) => Turn,
    ) => {
      const current = get().activeThread;
      if (!current || current.id !== threadId) return;
      const nextTurns = current.turns.map((t) =>
        t.run_id === runId ? patcher(t) : t,
      );
      set({ activeThread: { ...current, turns: nextTurns } });
    };
    const patchTurn = (patcher: (t: Turn) => Turn) =>
      patchTurnByRunId(optimisticRunId, patcher);
    // Coalesced patcher for high-frequency SSE deltas (thinking /
    // answer): one store patch per animation frame instead of one
    // per wire frame. Non-delta frames go through ``flushThen`` so a
    // queued delta can never be overtaken — or applied after — the
    // terminal patch.
    const turnPatches = makeTurnPatcherQueue(patchTurn);

    const initial = get().activeThread;
    if (initial && initial.id === threadId) {
      set({
        activeThread: { ...initial, turns: [...initial.turns, baseTurn] },
      });
    }

    // T1 (#33): answer frames are DELTAS while the model streams; after
    // ``status`` the wire may emit one authoritative frame — on ``done``
    // runs we keep a non-empty draft; on refuse/step_cap an empty frame
    // clears the draft so SC67 copy renders from ``assistant_message``.
    // The fold state lives here rather than on the turn row because a
    // bounce retry accumulates behind the visible draft.
    let statusSeen = false;
    // search-mode-host T2: the window a ``choice_window`` frame
    // painted on this turn, or null. The host sends NO terminal
    // frames on a suspended stream — while this is set the run is
    // waiting on a pick, so the row must not be finalized (not done,
    // still in_flight).
    let windowPending: ChoiceWindow | null = null;
    let answerFold: LiveAnswerState = EMPTY_LIVE_ANSWER;
    // ADR 0086: the ids the attach door minted
    // ride this send — and stay referenceable on every later one.
    // The attach path never touches /api/corpus/ingest.
    const request: api.SendRequest = { question, thinking: thinkingOn };
    if (attachmentIds && attachmentIds.length > 0) {
      request.attachment_ids = attachmentIds;
    }
    try {
      const result = await api.sendMessage(threadId, request, (event) => {
        switch (event.type) {
          case "thinking":
            turnPatches.coalesce((t) => ({
              ...foldTraceFrameOntoTurn(t, event),
              thinking_text: t.thinking_text + event.text,
            }));
            break;
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
            turnPatches.flushThen((t) => ({
              ...foldTraceFrameOntoTurn(t, event),
              tool_summaries: [...(t.tool_summaries ?? []), placeholder],
            }));
            break;
          }
          case "tool_result": {
            // The server already formatted the summary (spec line
            // 32); replace the "running…" placeholder with it.
            turnPatches.flushThen((t) => ({
              ...foldTraceFrameOntoTurn(t, event),
              tool_summaries: (t.tool_summaries ?? []).map((s) =>
                s.tool_call_id === event.tool_call_id
                  ? { ...s, summary: event.summary }
                  : s,
              ),
            }));
            break;
          }
          case "status": {
            statusSeen = true;
            // A terminal status ends any earlier suspension: the run
            // (possibly after a resume in another stream) is over.
            windowPending = null;
            // turn-trace-folds: the run ended — close any still-live
            // burst so the folds auto-collapse (spec line 34).
            // Drain queued deltas first so a pending thinking/answer
            // patch can never land after this terminal patch and
            // re-open a live burst on an ended run. Patch by the
            // CLIENT placeholder id (``optimisticRunId`` is not
            // reassigned until after this patch) so the optimistic
            // row is never orphaned until GET.
            turnPatches.flushThen((t) => ({
              ...closeOpenBurstsOnTurn(t),
              run_id: event.run_id,
              status: event.status,
              status_reason: event.reason ?? "",
              assistant_message: event.assistant_message,
              // Soft-Track: optional until host SSE includes track.
              track: event.track ?? t.track,
              track_decision: event.track_decision ?? t.track_decision,
              // Mid-flight stream taxonomy: only set on refuse
              // rows whose refusal came from the stream engine;
              // an empty value from the wire keeps the prior
              // row's value (a fresh turn starts as ``""``).
              status_classified:
                event.status_classified !== undefined
                  ? event.status_classified
                  : t.status_classified,
              // T4 (#37): the run ended — thinking collapses to the
              // duration label, tool rows collapse.
              in_flight: false,
              thinking_duration_ms: event.thinking_ms ?? t.thinking_duration_ms,
            }));
            optimisticRunId = event.run_id;
            break;
          }
          case "answer":
            answerFold = foldLiveAnswer(answerFold, {
              text: event.text,
              reset: event.reset,
              statusSeen,
            });
            turnPatches.coalesce((t) => ({
              // turn-trace-folds spec line 33: the first answer frame
              // closes the still-live burst — the fold must collapse
              // when prose starts, not only at terminal status.
              ...foldTraceFrameOntoTurn(t, { type: "answer" }),
              answer: answerFold.visible,
            }));
            break;
          case "citations":
            turnPatches.flushThen((t) => ({
              ...t,
              citations: event.citations,
            }));
            break;
          case "comparison":
            turnPatches.flushThen((t) => ({
              ...t,
              comparisons: [...(t.comparisons ?? []), event.comparison],
            }));
            break;
          case "choice_window":
            // The run suspended on this frame; the stream closes
            // without terminal frames. The row keeps its live flag
            // (see the finalize patch) and the window renders as the
            // operator's next action.
            windowPending = event.window;
            turnPatches.flushThen((t) => ({
              ...t,
              choice_window: event.window,
            }));
            break;
        }
      });
      // The SSE stream already delivered terminal status, answer, and
      // citations. Re-GET'ing here used to replace the live turn row
      // and caused a visible retract/repop even when merge logic ran
      // immediately after — keep the streamed row; refresh the sidebar
      // list only. Reload still uses GET via ThreadPage's effect.
      const cur = get().activeThread;
      if (cur?.id === threadId) {
        // Drain the queue first: a queued delta patch must not land
        // after the finalize patch (same ghost-burst risk as the
        // status branch).
        turnPatches.flushThen((t) => ({
          ...closeOpenBurstsOnTurn(t),
          // A pending window means the stream ended WITHOUT a
          // terminal frame — the run is suspended, not done.
          in_flight: windowPending !== null,
          citations:
            result.citations.length > 0 ? result.citations : t.citations,
          track: result.track ?? t.track,
          track_decision: result.track_decision ?? t.track_decision,
          status_classified: result.status_classified ?? t.status_classified,
        }));
      }
      await get().refreshThreads();
      return result;
    } catch (error) {
      // The send failed (409 concurrent_run / 503 ingest_missing /
      // transport). Before this the rejection escaped into a
      // floating promise and the optimistic row stayed ``in_flight``
      // forever — a ghost turn with no question answered. Drop the
      // row when no frame ever landed on it; keep (but un-live) a
      // row the stream had already started painting, then rethrow so
      // the composer can show the failure.
      if (statusSeen || windowPending !== null) {
        // Drain any queued deltas before un-living the row so a late
        // flush cannot patch a row the stream already abandoned; the
        // closeOpenBursts also guarantees a queued delta folded after
        // the terminal status cannot leave a live burst on the row.
        // A suspended row (window painted, then transport error)
        // stays in_flight — the pick path can still resume it.
        turnPatches.flushThen((t) => ({
          ...closeOpenBurstsOnTurn(t),
          in_flight: windowPending !== null,
        }));
      } else {
        dropTurnByRunId(threadId, optimisticRunId);
      }
      throw error;
    } finally {
      set({ sendingQuestion: false });
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
      // Same delta semantics as sendQuestion (T1 #33): append while
      // streaming; post-status empty frame clears refuse/step_cap draft.
      // The fold starts from the prose already painted on the resumed
      // row so the first delta extends it instead of replacing it.
      const fold: StreamFold = {
        statusSeen: false,
        windowPending: null,
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
    const turn = current.turns.find((t) => t.run_id === runId);
    if (!turn || turn.choice_window === undefined) return null;

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
      return result;
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
    // fresh while anything is ``parsing``. This read OWNS the poll
    // cadence: the running chain (if any) is replaced so the caller's
    // ``intervalMs`` cannot be silently ignored. Attachments are
    // auxiliary to the thread view: a failed door read leaves the
    // slice as-is and ENDS the chain rather than failing navigation
    // or spinning on a dead endpoint — the next open or attach
    // restarts it.
    stopAttachmentPolling();
    const attachments = await readAttachmentList(threadId);
    if (attachments === null) return;
    if (get().activeThread?.id !== threadId) return;
    set({ attachments });
    if (attachments.some((a) => a.status === "parsing")) {
      startAttachmentPolling(threadId, intervalMs);
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
// server. The still-running row is the one exception the server cannot
// send: a RunRecord is appended only once its stream completes, so a
// mid-run reopen carries the live row over — dropping it would blank
// the bubble AND orphan the patches the running stream still makes.
function reconcileTurnTable(
  streamed: ThreadDetail | null,
  persisted: ThreadDetail,
): ThreadDetail {
  if (!streamed || streamed.id !== persisted.id) return persisted;
  const streamedByRunId = new Map<string, Turn>(
    streamed.turns.map((t) => [t.run_id, t]),
  );
  const persistedRunIds = new Set(persisted.turns.map((t) => t.run_id));
  const reconciled = persisted.turns.map((row) => {
    const live = streamedByRunId.get(row.run_id);
    return live ? mergeReconciledTurn(live, row) : row;
  });
  const stillRunning = streamed.turns.filter(
    (t) => t.in_flight === true && !persistedRunIds.has(t.run_id),
  );
  return { ...persisted, turns: [...reconciled, ...stillRunning] };
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

// ----- ADR 0089: readiness polling -----
//
// Preparation runs off the request, so the only way the SPA learns
// parsing → ready / failed is re-reading the thread's list. An
// ``uploaded`` record is NOT work in progress: preparation begins when a
// grounded graph turn asks for it, so polling an upload row would spin on
// a state that cannot change by itself. One poller chain exists at a time,
// keyed by thread: opening another thread replaces it, and a read that
// finds nothing ``parsing`` stops the chain (the next open or attach
// restarts it). ``intervalMs`` threads through the chain as a
// parameter so tests advance a fake schedule instead of really
// waiting.

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
 * rather than stacking one, so every entry point (open, attach)
 * provably owns the next read.
 */
function startAttachmentPolling(
  threadId: string,
  intervalMs = ATTACHMENT_POLL_INTERVAL_MS,
): void {
  stopAttachmentPolling();
  scheduleAttachmentPollTick(threadId, intervalMs);
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
  if (merged.some((a) => a.status === "parsing")) {
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
 * yet, so the list cannot answer. The three client surfaces share it —
 * ``sendQuestion`` / ``createThread`` refuse with the named
 * ``attachment_upload_in_progress`` failure, and the Composer holds the
 * send on it separately from ``busy``, so the button and Enter are
 * blocked, not only the picker.
 * A file the question never mentions still blocks the send, because the
 * gate is about the upload, not about the reference.
 */
export function hasUploadInFlight(
  pendingUploads: Readonly<Record<string, number>>,
): boolean {
  return Object.values(pendingUploads).some((count) => count > 0);
}

/** One throw for both doors, so the refusal cannot drift between them. */
function refuseUploadInFlight(
  pendingUploads: Readonly<Record<string, number>>,
): void {
  if (hasUploadInFlight(pendingUploads)) {
    throw new api.AttachmentUploadInProgressError();
  }
}

/**
 * The one rule for what a send may reference, and what it deliberately
 * did not.
 *
 * Only a ``ready`` attachment is named: the thread keeps every attachment
 * for its whole life, so a question that never mentioned a not-yet-citable
 * one must not be refused on its account (spec: an unrelated question can
 * still use ready sources). ``uploaded`` is withheld for the same reason
 * ``parsing`` is — its bytes are stored but nothing is parsed or indexed
 * yet. ``withheld`` is the other half of the contract —
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
    if (attachment.status === "ready" && attachment.searchable) {
      ids.push(attachment.attachment_id);
    } else {
      withheld.push(attachment);
    }
  }
  return { ids, withheld };
}
