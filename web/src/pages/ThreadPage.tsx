// Chat page for one research thread: turn table + composer. Reload
// restores turns from GET (no re-invoke).

import { memo, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { NavLink, useParams } from "react-router-dom";
import AnswerWithCitations from "../capabilities/evidence/components/CitationChips";
import VisualClaims from "../capabilities/evidence/components/VisualClaims";
import Composer from "../components/Composer";
// The demo door back to the homepage. The provider is an ANCESTOR of
// every route (App.tsx), so the presentation it carries — the server's
// confirmed demo mode — is the fact the link is decided on.
import { useDemoPresentation } from "../components/DemoModeSurface";
// question-workspace: the task surfaces. Each renders one pure module's
// decision, so the judgement stays unit-tested in plain code.
import ArchiveList from "../components/ArchiveList";
import ClarificationCard, {
  RefusedClarificationNotice,
} from "../capabilities/clarification/components/ClarificationCard";
import TaskPanel from "../capabilities/task/components/TaskPanel";
// The document-choice card (specs/qa-chain-grounded-experience.md §GE-4).
// Rendered instead
// of ClarificationCard whenever the pending question is a document choice,
// because its candidates must show title/section/summary rather than the
// wire's opaque choice ids.
import DocumentChoiceCard from "../capabilities/clarification/components/DocumentChoiceCard";
import { documentChoiceSurface } from "../capabilities/clarification/documentChoiceSurface";
import {
  clarificationRegion,
  type ClarificationRegion,
} from "../capabilities/clarification/clarificationRegion";
import {
  restoreDecision,
  type RestoreDecision,
} from "../archiveRestore";
import { taskState, turnStatusLabel, type TaskStateView } from "../capabilities/task/taskState";
import type {
  ChoiceWindow,
  Comparison,
  PendingQuestionView,
  SearchModeDecision,
  ThreadDetail,
  ToolSummary,
  Turn,
} from "../types";
import {
  cellTitle,
  comparisonCaption,
  deltaForRow,
  directionClass,
  directionMark,
  locatorLabel,
  turnComparisons,
} from "../comparison";
import { choiceOptionDisabled, choiceRejectedText } from "../choiceWindow";
import { exportAvailability, exportUrl } from "../exportBundle";
import { resolveTurnTrack, showResearchTrackHint } from "../softTrack";
import {
  shouldRenderStreamErrorKind,
  streamErrorKindRowText,
} from "../streamErrorKind";
import { turnVisualClaims } from "../capabilities/evidence/visualClaims";
import {
  canSubmitFeedback,
  feedbackErrorVisible,
  isFeedbackActive,
  lastTerminalTurnIndex,
  showThumbsForTurn,
  type FeedbackValue,
} from "../turnFeedback";
import {
  foldOpenState,
  thinkingFoldVisibleText,
  traceItemsFromTurn,
} from "../turnTrace";
import {
  useWorkspaceStore,
  displayableWorkspaceOperation,
  operationRefusal,
  projectionUnreadableFor,
  restoreFactsFor,
  submissionNoticeFor,
  type TaskSurfaceFacts,
} from "../store";
import { occupancyPercent, parseThreadOccupancy } from "../threadOccupancy";
import { threadViewState, type ThreadViewState } from "../threadViewState";
import { threadEntryIsLive } from "../threadEntry";

const TRANSCRIPT_BOTTOM_TOLERANCE = 4;

// A wheel burst is one gesture while its ticks stay within this gap; a
// longer pause starts a new gesture with a new start position.
const TRANSCRIPT_WHEEL_GESTURE_IDLE_MS = 150;

// Width of the native scrollbar band at the right edge, in CSS pixels: a
// pointer within this band is on the vertical scrollbar, not on the
// content. Assumes an LTR layout, so the bar sits at ``bounds.right`` —
// an RTL transcript would put it at ``bounds.left``.
const TRANSCRIPT_SCROLLBAR_BAND_PX = 18;

function isAtTranscriptBottom(viewport: HTMLDivElement): boolean {
  return viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <=
    TRANSCRIPT_BOTTOM_TOLERANCE;
}

function canScrollTranscriptUpFrom(
  target: EventTarget | null,
  viewport: HTMLDivElement,
  positionBeforeGesture: number,
): boolean {
  let element = target instanceof HTMLElement ? target : null;
  while (element && element !== viewport) {
    const overflowY = window.getComputedStyle(element).overflowY;
    if (
      (overflowY === "auto" ||
        overflowY === "scroll" ||
        overflowY === "overlay") &&
      element.scrollHeight > element.clientHeight + 1 &&
      element.scrollTop > 0
    ) {
      return false;
    }
    element = element.parentElement;
  }
  // Judged against the position the gesture STARTED from, not the live
  // ``scrollTop``. The live value is unreliable here: a single upward
  // gesture can already have carried the viewport to the top by the
  // time this handler runs, reporting 0 and looking like "nothing above
  // to read" — which would leave follow armed and let the next content
  // growth snap the viewport back to the tail. How the browser schedules
  // that scroll (and the ``scroll`` event it fires) relative to this
  // handler is not guaranteed, so the callers capture the start position
  // at gesture start and pass it in; this function never reads live
  // position.
  return positionBeforeGesture > 0;
}

function isTranscriptTextEntry(target: EventTarget | null): boolean {
  return target instanceof HTMLElement &&
    (target.matches("input, textarea, select") || target.isContentEditable);
}

function isTranscriptScrollUpKey(event: KeyboardEvent): boolean {
  return (
    ["ArrowUp", "PageUp", "Home"].includes(event.key) ||
    (event.key === " " && event.shiftKey)
  );
}

function isTranscriptScrollbarPointer(
  event: PointerEvent,
  viewport: HTMLDivElement,
): boolean {
  const bounds = viewport.getBoundingClientRect();
  return event.button === 0 &&
    viewport.scrollHeight > viewport.clientHeight + 1 &&
    event.clientX >= bounds.right - TRANSCRIPT_SCROLLBAR_BAND_PX;
}

function observeTranscriptGrowth(
  viewport: HTMLDivElement,
  content: HTMLDivElement,
  shouldFollow: { current: boolean },
): () => void {
  let disposed = false;
  const followTail = () => {
    if (!disposed && shouldFollow.current) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  };
  const observer = new ResizeObserver(followTail);
  observer.observe(viewport);
  observer.observe(content);
  // ResizeObserver runs after layout, once scrollHeight reflects anchoring.
  followTail();

  return () => {
    disposed = true;
    observer.disconnect();
  };
}

function observeTranscriptScrollIntent(
  viewport: HTMLDivElement,
  shouldFollow: { current: boolean },
): () => void {
  let scrollbarPointerId: number | null = null;
  let previousScrollTop = viewport.scrollTop;
  // The position the CURRENT wheel gesture started from, captured on the
  // first tick of a burst and held until the burst goes idle. It is read
  // from ``previousScrollTop`` (the last position the scroll listener
  // saw) rather than the live ``scrollTop``, because the live position
  // may already reflect this burst's own scroll by the time the handler
  // runs. Holding it for the whole gesture is what keeps a scroll event
  // the gesture caused from overwriting the start position before the
  // next tick reads it — see ``canScrollTranscriptUpFrom``. ``null`` =
  // no wheel gesture in flight.
  let wheelStartScrollTop: number | null = null;
  let wheelIdleTimer: ReturnType<typeof setTimeout> | null = null;

  const onScroll = () => {
    const nextScrollTop = viewport.scrollTop;
    if (
      scrollbarPointerId !== null &&
      nextScrollTop < previousScrollTop
    ) {
      shouldFollow.current = false;
    }
    previousScrollTop = nextScrollTop;
    // Scroll events alone cannot distinguish user movement from content
    // growth or browser anchoring; only an explicit input pauses follow.
    if (isAtTranscriptBottom(viewport)) shouldFollow.current = true;
  };
  const onWheel = (event: WheelEvent) => {
    if (wheelStartScrollTop === null) {
      wheelStartScrollTop = previousScrollTop;
    }
    if (
      event.deltaY < 0 &&
      canScrollTranscriptUpFrom(event.target, viewport, wheelStartScrollTop)
    ) {
      shouldFollow.current = false;
    }
    // A gap longer than this starts a fresh gesture with a fresh start
    // position (trackpads and notched wheels both pace bursts this way).
    if (wheelIdleTimer !== null) clearTimeout(wheelIdleTimer);
    wheelIdleTimer = setTimeout(() => {
      wheelStartScrollTop = null;
      wheelIdleTimer = null;
    }, TRANSCRIPT_WHEEL_GESTURE_IDLE_MS);
  };
  const onKeyDown = (event: KeyboardEvent) => {
    const target = event.target;
    if (
      target instanceof HTMLElement &&
      !isTranscriptTextEntry(target) &&
      isTranscriptScrollUpKey(event) &&
      canScrollTranscriptUpFrom(target, viewport, previousScrollTop)
    ) {
      shouldFollow.current = false;
    }
  };
  const onPointerDown = (event: PointerEvent) => {
    if (isTranscriptScrollbarPointer(event, viewport)) {
      scrollbarPointerId = event.pointerId;
    }
  };
  const onPointerEnd = (event: PointerEvent) => {
    if (event.pointerId === scrollbarPointerId) scrollbarPointerId = null;
  };
  const controller = new AbortController();
  const options = { passive: true, signal: controller.signal };

  viewport.addEventListener("scroll", onScroll, options);
  viewport.addEventListener("wheel", onWheel, options);
  viewport.addEventListener("keydown", onKeyDown, options);
  viewport.addEventListener("pointerdown", onPointerDown, options);
  window.addEventListener("pointerup", onPointerEnd, options);
  window.addEventListener("pointercancel", onPointerEnd, options);

  return () => {
    controller.abort();
    if (wheelIdleTimer !== null) clearTimeout(wheelIdleTimer);
  };
}

function observeTranscriptTouchIntent(
  viewport: HTMLDivElement,
  shouldFollow: { current: boolean },
): () => void {
  let touchY: number | null = null;
  // A drag emits many touchmove events; only the position at touchstart
  // is the one the gesture started from.
  let scrollTopAtTouchStart = 0;
  const controller = new AbortController();
  const options = { passive: true, signal: controller.signal };
  viewport.addEventListener("touchstart", (event: TouchEvent) => {
    touchY = event.touches[0]?.clientY ?? null;
    scrollTopAtTouchStart = viewport.scrollTop;
  }, options);
  viewport.addEventListener("touchmove", (event: TouchEvent) => {
    const nextY = event.touches[0]?.clientY ?? null;
    if (touchY !== null && nextY !== null && nextY > touchY &&
      canScrollTranscriptUpFrom(event.target, viewport, scrollTopAtTouchStart)) {
      shouldFollow.current = false;
    }
    touchY = nextY;
  }, options);
  return () => controller.abort();
}

// T4 (spec line 38-39): the interrupted-run banner. Shown when
// the latest turn row reports ``in_flight`` after a reload —
// i.e. the process crashed mid-run and the checkpoint is still
// pending. The Resume button drives POST /resume; while the
// resumed stream is live the button shows "Resuming…" and the
// Composer stays disabled via ``resumingThreadId``.
function InterruptedRunBanner({ threadId }: { threadId: string }) {
  const resumingThreadId = useWorkspaceStore((s) => s.resumingThreadId);
  if (resumingThreadId !== threadId) return null;

  return (
    <div className="max-w-3xl mx-auto mb-4 border border-black bg-tool px-3 py-2 flex items-center justify-between">
      <p className="text-sm text-gray-700">
        上一轮运行被中断，正在从最后一个完整步骤 Resume…
      </p>
      <span className="text-xs font-mono uppercase tracking-wide text-gray-500">
        正在 Resume…
      </span>
    </div>
  );
}

// T3 (ADR 0044): thread token occupancy, from the SAME host figure
// the auto-compact gate computes. A thin unobtrusive percentage
// chip in the thread header; renders nothing for a thread with no
// checkpoint (never sent on) rather than a fabricated 0%.
function OccupancyBadge({
  occupancy,
}: {
  occupancy: ReturnType<typeof parseThreadOccupancy>;
}) {
  if (!occupancy) return null;
  return (
    <span
      className="text-xs font-mono text-gray-500 whitespace-nowrap"
      title="会话上下文占用（与自动压缩门同一口径）"
    >
      {occupancyPercent(occupancy)}
    </span>
  );
}

function searchModeDecisionForThread(
  turns: Turn[],
  inFlight: boolean,
): SearchModeDecision | null | undefined {
  if (inFlight) return undefined;
  const latestTurn = turns[turns.length - 1];
  if (!latestTurn) return undefined;
  return latestTurn.search_mode_decision ?? null;
}

/** The latest turn row, or null on a thread that has not started one. */
function latestTurnOrNull(turns: readonly Turn[]): Turn | null {
  return turns[turns.length - 1] ?? null;
}

// T2 fix: the SPA's refuse / step_cap ordinary-person copy is the
// server-emitted ``assistant_message`` (see run_payload.ORDINARY_PERSON_COPY).
// A client-side duplicate was rendering two near-identical sentences and
// leaking the raw enum reason ("(unknown_locator)"). Delete the duplicate
// so ``assistant_message`` is the sole user-facing copy on those turns.

/**
 * The demo door back to the homepage.
 *
 * It exists only on a DEMO conversation, and the fact comes from the
 * demo presentation the ancestor provider carries — true only when the
 * server confirmed demo mode. Going home must not reset the corpus,
 * delete the conversation or cancel an answer in flight: this link
 * only navigates.
 *
 * Prop-driven on purpose: the thread page's own body reads the
 * workspace store, whose state a server render cannot see, so the
 * demo-only rule is asserted on this piece directly (this project's
 * vitest setup has no DOM testing library).
 */
export function DemoHomeLink({ enabled }: { enabled: boolean }) {
  if (!enabled) return null;
  return (
    <NavLink
      to="/"
      data-testid="demo-home-link"
      className="shrink-0 border border-black px-2 py-1 text-xs hover:bg-tool hover:text-white"
    >
      返回演示首页
    </NavLink>
  );
}

export default function ThreadPage() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const activeThread = useWorkspaceStore((s) => s.activeThread);
  // The demo door back to the homepage (T3). The hook is called above
  // the early return so the hook count stays stable across renders.
  const demo = useDemoPresentation();
  const transcriptViewportRef = useRef<HTMLDivElement>(null);
  const transcriptContentRef = useRef<HTMLDivElement>(null);
  const shouldFollowTranscriptRef = useRef(true);
  const transcriptReady = Boolean(
    thread_id && activeThread && activeThread.id === thread_id,
  );

  // Re-sync from the persisted turn table when we arrive at a
  // thread (reload / switch back). GET only - no graph invoke.
  useEffect(() => {
    if (!thread_id) return;
    // T2: a thread that is already answering on THIS client keeps its
    // live answer — a re-read here would race the stream. The store
    // guard is the primary defense; this effect does not add a second
    // read of its own in that case. It asks the SAME rule the store
    // guard asks (``threadEntryIsLive``), so the read it skips and the
    // read the store skips are the same read. When the operator LEFT
    // the thread while its send was in flight, the live state is gone
    // and the read is required.
    const state = useWorkspaceStore.getState();
    if (
      threadEntryIsLive({
        threadId: thread_id,
        sendingThreadId: state.sendingThreadId,
        activeThreadId: state.activeThread?.id ?? null,
      })
    ) {
      return;
    }
    const open = useWorkspaceStore.getState().openThread;
    void open(thread_id).catch(() => undefined);
  }, [thread_id]);

  // T4 fix (code-review H2): the ``resumingThreadId`` selector
  // MUST be called above the early-return so the hook count stays
  // stable across renders (Rule of Hooks). Earlier layout
  // crashed React on every thread navigation with "Rendered more
  // hooks than during the previous render".
  const resumingThreadId = useWorkspaceStore((s) => s.resumingThreadId);

  useLayoutEffect(() => {
    const viewport = transcriptViewportRef.current;
    const content = transcriptContentRef.current;
    if (!thread_id || !transcriptReady || !viewport || !content) return;

    shouldFollowTranscriptRef.current = true;
    const stopObservingGrowth = observeTranscriptGrowth(
      viewport,
      content,
      shouldFollowTranscriptRef,
    );
    const stopObservingScrollIntent = observeTranscriptScrollIntent(
      viewport,
      shouldFollowTranscriptRef,
    );
    const stopObservingTouchIntent = observeTranscriptTouchIntent(
      viewport,
      shouldFollowTranscriptRef,
    );
    return () => {
      stopObservingTouchIntent();
      stopObservingScrollIntent();
      stopObservingGrowth();
    };
  }, [thread_id, transcriptReady]);

  if (!thread_id || !activeThread || activeThread.id !== thread_id) {
    return <main className="p-6 text-sm text-gray-500">加载对话中…</main>;
  }

  // T4 (code-review H1): the Resume button is shown when the
  // server reports a PENDING checkpoint (process died mid-run;
  // the lock has been released but the last node did not
  // finish). The previous implementation keyed off
  // ``latestTurn.in_flight`` (the live-lock signal), which made
  // the button appear DURING a process-alive refresh (when it
  // is forbidden) and never appear after a crash (when it is
  // needed). The new signal ``pending_checkpoint`` comes from
  // the server-side ``pending_nodes`` probe and is only true
  // when a checkpoint is genuinely resumable.
  const pendingCheckpoint = activeThread.pending_checkpoint === true;
  const isResuming = resumingThreadId === thread_id;
  // search-mode-host T2 reload path: a paused choice has no turn row, so
  // the host carries it on ``pending_turn``. The composed banner / card /
  // empty-copy decision lives in ``threadViewState`` (pure, DOM-free) so
  // it is unit-tested; this component only renders its result.
  const pendingTurn = activeThread.pending_turn ?? null;
  const view = threadViewState({
    pendingCheckpoint,
    isResuming,
    pendingTurn,
    turnCount: activeThread.turns.length,
  });
  // The task region reads the SAME per-thread projection the task doors
  // write, so reload recovery and live answers cannot disagree about the
  // current task. The slices below are selected as references because a
  // selector that rebuilt a derived object would re-render this page on
  // every store notification; the decisions are composed once here and
  // every surface below only renders their result.
  const taskRuntime = useWorkspaceStore((s) => s.taskRuntime);
  const workspaceOperations = useWorkspaceStore((s) => s.workspaceOperations);
  const restoreRequests = useWorkspaceStore((s) => s.restoreRequests);
  const taskProjectionRefusal = useWorkspaceStore(
    (s) => s.taskProjectionRefusal,
  );
  const taskProjectionRefreshFailed = useWorkspaceStore(
    (s) => s.taskProjectionRefreshFailed,
  );
  const pendingUploads = useWorkspaceStore((s) => s.pendingUploads);
  const clarificationDrafts = useWorkspaceStore((s) => s.clarificationDrafts);
  const surface: TaskSurfaceFacts = {
    activeThread,
    taskRuntime,
    workspaceOperations,
    restoreRequests,
    taskProjectionRefusal,
    taskProjectionRefreshFailed,
    pendingUploads,
  };
  // One operation record per thread, and the refusal it carries. Every
  // consumer below reads this same pair, so a reply's pending/accepted/
  // rejected state cannot be re-derived differently by two surfaces.
  const operation = displayableWorkspaceOperation(surface, thread_id);
  const pendingQuestion = taskRuntime[thread_id]?.pending_question ?? null;
  const region = clarificationRegion({
    pendingQuestion,
    drafts: clarificationDrafts,
    submission: operation,
    refusal: operationRefusal(operation),
  });
  const taskView = taskState({
    taskRuntime: taskRuntime[thread_id] ?? undefined,
    turns: activeThread.turns,
    inFlight: activeThread.in_flight === true,
    projectionUnreadable: projectionUnreadableFor(surface, thread_id),
    submission: submissionNoticeFor(surface, thread_id),
  });
  const searchModeDecision = searchModeDecisionForThread(
    activeThread.turns,
    activeThread.in_flight === true,
  );
  // The developer trace section describes the turn currently on screen;
  // let the same latest-row selection feed the task panel once, here.
  const currentTurn = latestTurnOrNull(activeThread.turns);
  const restore = restoreDecision(restoreFactsFor(surface, thread_id));

  return (
    <main className="flex flex-col h-full">
      <header className="border-b border-black px-4 py-3 bg-page flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold truncate">
            {activeThread.title}
          </h2>
          <p className="text-xs text-gray-500">
            创建于 {new Date(activeThread.created_at).toLocaleString()}
          </p>
        </div>
        {/* The demo door back to the homepage (T3). */}
        <DemoHomeLink enabled={demo.enabled} />
        <OccupancyBadge occupancy={parseThreadOccupancy(activeThread)} />
        <ExportLinks threadId={thread_id} turns={activeThread.turns} />
      </header>

      <ThreadTranscript
        threadId={thread_id}
        turns={activeThread.turns}
        threadInFlight={activeThread.in_flight === true}
        isResuming={isResuming}
        view={view}
        pendingTurn={pendingTurn}
        viewportRef={transcriptViewportRef}
        contentRef={transcriptContentRef}
      />

      {/* The task surfaces sit between the transcript and the composer,
          not inside the scrolled transcript: the current task, the
          pending clarification and the archive list are persistent
          chrome for the thread, and putting them in the scroll area
          would hide the pending question below the fold (the one thing
          the operator must not miss). */}
      <TaskRegion
        threadId={thread_id}
        taskView={taskView}
        region={region}
        restore={restore}
        pendingQuestion={pendingQuestion}
        searchModeDecision={searchModeDecision}
        currentTurn={currentTurn}
      />

      <Composer threadId={thread_id} />
    </main>
  );
}

// The task panel, the pending clarification card, and the archive list, in
// that order — the clarification sits between them because a question
// awaiting an answer outranks a summary of what has already happened. Every
// fact this region renders is already associated with THIS thread by the
// page: a pending question from the projection, an operation from the
// ledger, a restore from its own request record.
function TaskRegion({
  threadId,
  taskView,
  region,
  restore,
  pendingQuestion,
  searchModeDecision,
  currentTurn,
}: {
  threadId: string;
  taskView: TaskStateView;
  region: ClarificationRegion;
  restore: RestoreDecision;
  pendingQuestion: PendingQuestionView | null;
  searchModeDecision: SearchModeDecision | null | undefined;
  currentTurn: Turn | null;
}) {
  const openTaskClarification = useWorkspaceStore(
    (s) => s.openTaskClarification,
  );

  // Opening the draft is a store write, so it belongs in an effect rather
  // than in the render body. It is idempotent — the store keeps an
  // existing draft — so re-running it cannot overwrite typed text.
  useEffect(() => {
    if (pendingQuestion) openTaskClarification(threadId);
  }, [threadId, pendingQuestion, openTaskClarification]);

  // A ``document_choice`` gets its OWN card (§GE-4).
  // The two are mutually exclusive by construction — this returns null for
  // anything that is not a document choice, so an ordinary clarification can
  // never acquire document-choice control state, and a document choice never
  // falls back to the id-printing clarification card.
  const documentChoice = documentChoiceSurface(
    pendingQuestion,
    region.card.draft,
    region.card.submitting,
  );

  return (
    <div className="shrink-0 space-y-2 border-t border-black bg-page px-4 py-2 max-h-72 overflow-y-auto">
      {documentChoice ? (
        <DocumentChoiceCard
          threadId={threadId}
          surface={documentChoice}
          submitting={region.card.submitting}
          refusalKind={region.card.refusalKind}
        />
      ) : region.card.draft ? (
        <ClarificationCard
          threadId={threadId}
          draft={region.card.draft}
          submitting={region.card.submitting}
          refusalKind={region.card.refusalKind}
        />
      ) : null}
      {region.refused ? (
        <RefusedClarificationNotice
          prompt={region.refused.draft.prompt}
          refusalKind={region.refused.refusalKind}
          outcome={region.refused.outcome}
        />
      ) : null}
      <TaskPanel
        state={taskView}
        searchModeDecision={searchModeDecision}
        threadId={threadId}
        currentTurn={currentTurn}
      />
      <ArchiveList threadId={threadId} decision={restore} />
    </div>
  );
}

// The transcript area: the scroll viewport that bottom-follow tracks, plus
// every thread-level overlay and the turn rows. Lifted out of ``ThreadPage``
// so the composed render decision (``view``) and its conditionals live in one
// presentational component, keyed off props only. The viewport / content refs
// are threaded through as props so ``ThreadPage``'s follow effect keeps
// observing the same nodes.
function ThreadTranscript({
  threadId,
  turns,
  threadInFlight,
  isResuming,
  view,
  pendingTurn,
  viewportRef,
  contentRef,
}: {
  threadId: string;
  turns: Turn[];
  threadInFlight: boolean;
  isResuming: boolean;
  view: ThreadViewState;
  pendingTurn: NonNullable<ThreadDetail["pending_turn"]> | null;
  viewportRef: RefObject<HTMLDivElement>;
  contentRef: RefObject<HTMLDivElement>;
}) {
  return (
    <div
      ref={viewportRef}
      className="flex-1 overflow-y-auto p-4"
      tabIndex={0}
    >
      <div ref={contentRef} className="space-y-4">
        {/* T4: crash banner shown when GET reports a PENDING
            checkpoint (process died mid-run). The lock is no
            longer held; only a checkpoint is left. Resume button
            drives ``resumeThread`` which POSTs /resume. A paused
            CHOICE also leaves a pending checkpoint — but the choice
            card below owns that case, so ``view`` suppresses the
            banner. */}
        {view.showCrashResumeBanner ? (
          <CrashResumeBanner threadId={threadId} />
        ) : null}
        {isResuming ? <InterruptedRunBanner threadId={threadId} /> : null}

        {/* The reloaded paused choice. It has no turn row yet, so the
            card mounts at thread level, next to the banners.
            ``pendingTurn`` here only narrows the type for the render;
            whether the card shows is ``view.showPendingChoiceCard``. */}
        {view.showPendingChoiceCard && pendingTurn ? (
          <div className="max-w-3xl mx-auto">
            <ChoiceWindowCard
              threadId={threadId}
              runId={pendingTurn.run_id}
              choice={pendingTurn.choice_window}
            />
          </div>
        ) : null}

        {turns.map((turn, index) => (
          // Turn rows are append-only; a stable index key avoids
          // remounting when the server-minted ``run_id`` reconciles.
          // ``TurnView`` is memoized: streaming patches touch one
          // row, the other rows' props are unchanged (turn identity
          // + scalars), so a live round on a long thread no longer
          // re-renders every historical row per SSE frame (the
          // render-saturation freeze). The thumbs scalar keeps the
          // T3 "last TERMINAL turn" rule — computed once per render,
          // not per row.
          <TurnView
            key={index}
            turn={turn}
            isLastTerminalTurn={
              index === lastTerminalTurnIndex(turns)
            }
            threadId={threadId}
            threadInFlight={threadInFlight}
          />
        ))}

        {/* Zero-turn thread: a plain empty state. The corpus-card
            grid was retired with the /kb chrome
            (workbench-retrieve-product-seam): the catalog lives
            outside fin, so the composer is the only entry. A reloaded
            paused choice is exactly the zero-turn case — the card
            speaks for it, so ``view`` yields the copy. */}
        {view.showEmptyCopy ? (
          <p className="max-w-3xl mx-auto pt-4 text-sm text-gray-500">
            还没有提问。在下方输入框写下你的问题，开始这个 research thread。
          </p>
        ) : null}
      </div>
    </div>
  );
}

// T4 (spec line 39): the "previous run was interrupted" banner
// with a Resume button. Replaces the T1 sending-state visual for
// the crash-resume case. Click triggers ``resumeThread(threadId)``
// which POSTs /resume; the banner flips to the
// ``InterruptedRunBanner`` while the resume stream is live, then
// the openThread() reconcile fetches the terminal turn and the
// banner disappears.
function CrashResumeBanner({ threadId }: { threadId: string }) {
  const resumeThread = useWorkspaceStore((s) => s.resumeThread);
  return (
    <div className="max-w-3xl mx-auto border border-black bg-tool px-3 py-2 flex items-center justify-between">
      <p className="text-sm text-gray-700">
        上一轮运行中断。从最后一个完整步骤 Resume。
      </p>
      <button
        type="button"
        onClick={() => {
          void resumeThread(threadId);
        }}
        className="border border-black bg-card px-3 py-1 text-sm font-medium hover:bg-accent hover:text-white"
      >
        Resume
      </button>
    </div>
  );
}

// search-mode-host T2 (ADR 0062 HITL): the choice window. The host
// suspended this run on a clarify interrupt; the payload IS the
// contract (title / prompt / structured options), so the card renders
// exactly what arrived — no client-side option table, no invented
// copy. opened:false options stay visible but disabled (档未开放); a
// re-issued window after an invalid pick shows the rejected ids. The
// click drives ``pickChoice`` → POST /resume {"pick": id}.
//
// Takes the fields the pick needs, NOT a whole ``Turn``: a reloaded
// paused turn has no row, so the thread-level mount passes the
// ``pending_turn`` projection's own ``runId`` / ``choice`` and the
// per-turn mount passes the row's. One card, both doors — a refactor
// cannot make the reloaded case render differently from the live one.
function ChoiceWindowCard({
  threadId,
  runId,
  choice,
}: {
  threadId: string;
  runId: string;
  choice: ChoiceWindow;
}) {
  const pickChoice = useWorkspaceStore((s) => s.pickChoice);
  // While the pick stream is open the buttons go dead: the window
  // already collapsed on click, but a re-issued window (invalid
  // pick) must not accept a second concurrent pick.
  const busy = useWorkspaceStore((s) => s.resumingThreadId === threadId);
  const rejected = choiceRejectedText(choice);
  return (
    <section
      data-testid="choice-window"
      className="border border-black bg-tool px-3 py-2"
    >
      <p className="text-sm font-semibold text-gray-800">
        {choice.title || "请选择"}
      </p>
      {choice.prompt ? (
        <p className="mt-0.5 text-xs text-gray-600">{choice.prompt}</p>
      ) : null}
      {rejected ? (
        <p data-testid="choice-window-rejected" className="mt-1 text-xs text-red-700">
          {rejected}
        </p>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2">
        {choice.options.map((option) => {
          const unopened = choiceOptionDisabled(option);
          return (
            <button
              key={option.id}
              type="button"
              data-testid={`choice-option-${option.id}`}
              disabled={busy || unopened}
              onClick={() => {
                void pickChoice(threadId, runId, option.id);
              }}
              className={
                unopened
                  ? "border border-gray-300 bg-card px-3 py-1 text-sm text-gray-400 cursor-not-allowed"
                  : "border border-black bg-card px-3 py-1 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
              }
            >
              {option.label}
              <span className="ml-1 font-mono text-xs text-gray-500">
                {option.id}
              </span>
              {unopened ? (
                <span data-testid="choice-option-unopened" className="ml-1 text-xs">
                  （未开放）
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
    </section>
  );
}

// T4 workflow: download this thread's comparison or citation
// bundle. Plain anchors — the browser performs the GET and saves the
// file, so there is no blob juggling and no second copy of the data in
// the SPA. A kind the thread never produced is not rendered at all
// rather than offered as a link that would answer a typed 404.
function ExportLinks({ threadId, turns }: { threadId: string; turns: Turn[] }) {
  const available = exportAvailability(turns);
  if (!available.comparison && !available.citations) return null;
  return (
    <nav aria-label="导出" className="flex shrink-0 items-center gap-2">
      {available.comparison ? (
        <a
          data-testid="export-comparison"
          href={exportUrl(threadId, "comparison")}
          download
          className="border border-black bg-card px-2 py-1 text-xs hover:bg-accent hover:text-white"
        >
          导出对比 CSV
        </a>
      ) : null}
      {available.citations ? (
        <a
          data-testid="export-citations"
          href={exportUrl(threadId, "citations")}
          download
          className="border border-black bg-card px-2 py-1 text-xs hover:bg-accent hover:text-white"
        >
          导出引用 CSV
        </a>
      ) : null}
    </nav>
  );
}

// Memoized row: a streaming round patches one turn; historical rows
// re-render only when THEIR turn object changes (see the TurnView
// call site for the prop-narrowing rationale).
const TurnView = memo(function TurnView({
  turn,
  isLastTerminalTurn,
  threadId,
  threadInFlight,
}: {
  turn: Turn;
  isLastTerminalTurn: boolean;
  threadId: string;
  threadInFlight: boolean;
}) {
  // T4: while THIS turn is live (optimistic send or a
  // lock-held latest row) the tool rows stay open and the live
  // thinking burst stays expanded; after the run both collapse.
  const live = turn.in_flight === true;
  // turn-trace-folds: the burst timeline. A live run carries
  // the folded bursts from the store; a GET-restored row (or legacy
  // state without trace_items) builds one thinking fold + one tool
  // fold from the persisted blob — spec line 41 allows this.
  const traceItems =
    turn.trace_items && turn.trace_items.length > 0
      ? turn.trace_items
      : traceItemsFromTurn(turn);
  const hasTrace = traceItems.length > 0;
  // T3 (spec Focus 1): structured comparison tables from the
  // ``compare_facts`` tool. Rendered between the trace and the
  // answer card — the order they arrived in, and the one place
  // they stay visible on a refuse row where the answer card holds
  // only the ordinary-person copy.
  const comparisons = turnComparisons(turn);
  // The run's visual observations, beside the answer rather than inside it.
  // A generated observation is not a source quote, so it never joins the
  // chips or the sources list (spec thread-attachment-source-read lines 58 /
  // 65). Projected through the one function the history load also uses, so a
  // reload renders what the live stream rendered.
  const visualClaims = turnVisualClaims(turn);
  return (
    <article className="max-w-3xl mx-auto space-y-2">
      <div className="flex justify-end">
        <div className="border border-black bg-tool px-3 py-2 max-w-[80%] text-sm whitespace-pre-wrap">
          {turn.question}
        </div>
      </div>
      {/* Trace (turn-trace-folds): sibling thinking/tool bursts
          in arrival order, unframed on page bg between user bubble
          and answer card. Each burst streams expanded while live and
          auto-collapses at burst end; a manual toggle wins over the
          auto state until the burst's liveness changes again. */}
      {hasTrace ? (
        <div data-testid="turn-trace" className="space-y-1">
          {traceItems.map((item, i) =>
            item.kind === "thinking" ? (
              <ThinkingFold
                key={`thinking-${i}`}
                text={item.text}
                durationMs={
                  i === traceItems.length - 1
                    ? turn.thinking_duration_ms
                    : undefined
                }
                live={item.live}
              />
            ) : (
              <ToolsFold
                key={`tools-${i}`}
                calls={item.calls}
                live={item.live}
              />
            ),
          )}
        </div>
      ) : null}
      {comparisons.map((comparison, i) => (
        <ComparisonTable
          key={`comparison-${i}`}
          comparison={comparison}
        />
      ))}
      {/* search-mode-host T2: the run suspended for an operator pick.
          The window takes the answer-bubble slot's attention while it
          is pending; a pick collapses it and the resumed stream
          renders into this same row. */}
      {turn.choice_window ? (
        <ChoiceWindowCard
          threadId={threadId}
          runId={turn.run_id}
          choice={turn.choice_window}
        />
      ) : null}
      {live && !turn.answer && !turn.assistant_message ? null : (
        <div
          data-testid="turn-answer-card"
          className="border border-black bg-card p-3"
        >
          {/* T4: while THIS turn streams, the optimistic status
              placeholder would read "done" (the wire carries no
              running status per spec line 30) — hide the status line
              until the run actually ends. */}
          {!live ? (
            <StatusLine turn={turn} track={resolveTurnTrack(turn)} />
          ) : null}
          {turn.answer ? (
            <AnswerWithCitations
              answer={turn.answer}
              citations={turn.citations}
              refusals={turn.evidence_refusals ?? []}
            />
          ) : null}
          {/* T2 spec SC67: refuse / step_cap turns show the
              ordinary-person copy in the answer-bubble slot with an
              empty sources list underneath - not a blank crash. */}
          {!turn.answer && turn.assistant_message ? (
            <p className="text-sm whitespace-pre-wrap text-gray-700 italic">
              {turn.assistant_message}
            </p>
          ) : null}
          {/* Mid-flight stream error taxonomy: on refuse rows whose
              refusal came from the stream engine (not the citation
              gate), render a small "原因: <label>" line so a 401 /
              429 / 504 reads distinctly instead of as one opaque
              English sentence. The conditional is a pure helper so
              a unit test (no DOM) can pin both the gate and the
              label text. */}
          {shouldRenderStreamErrorKind(turn) ? (
            <p
              data-testid="stream-error-kind"
              data-kind={turn.status_classified}
              className="text-xs text-gray-500 mt-1"
            >
              {streamErrorKindRowText(turn)}
            </p>
          ) : null}
        </div>
      )}
      {/* The visual-observation surface. A sibling of the answer card, not a
          child of the chips, so a claim is visible on a refuse row too and
          can never be placed as a span in the prose. The component renders
          nothing when the turn made no claim. */}
      <VisualClaims threadId={threadId} claims={visualClaims} />
      {/* T8: the thumbs tail card — the operator's verdict on this
          turn, rendered after the answer card so it reads as a
          per-turn action. T3: gated to the LAST terminal
          turn of a thread that is not in_flight (showThumbs) —
          older turns show nothing; canSubmitFeedback stays the
          persist gate inside FeedbackControls. */}
      {showThumbsForTurn(isLastTerminalTurn, turn, threadInFlight) ? (
        <FeedbackControls threadId={threadId} turn={turn} />
      ) : null}
    </article>
  );
});

// T8: thumbs up/down for one terminal run. Clicking submits through
// the store's ``setTurnFeedback`` (response-driven: the row's
// ``feedback`` updates only after the host confirms the persisted
// value) and re-clicking the active thumb re-affirms it — the host
// has no "clear" verb, last write wins. A rejected submit (400/503)
// surfaces as an inline "提交失败，请重试" hint next to the thumbs;
// the hint clears on the next submit (transition pinned in
// ``turnFeedback.feedbackErrorVisible``).
// T3: the word buttons (有帮助 / 没帮助) become stroke SVG
// thumbs (no emoji, no words) — same stroke idiom as Sidebar's
// 16x16 nav icons. testids / aria-pressed / inline retry copy keep
// their existing contract; canSubmitFeedback stays the persist
// gate so a turn that lost its run_id can never POST.
function FeedbackControls({
  threadId,
  turn,
}: {
  threadId: string;
  turn: Turn;
}) {
  const setTurnFeedback = useWorkspaceStore((s) => s.setTurnFeedback);
  const [submitFailed, setSubmitFailed] = useState(false);
  const submit = (value: FeedbackValue) => {
    if (!canSubmitFeedback(turn)) return;
    setSubmitFailed((current) => feedbackErrorVisible(current, "submit"));
    setTurnFeedback(threadId, turn.run_id, value).catch(() => {
      setSubmitFailed((current) => feedbackErrorVisible(current, "failure"));
    });
  };
  const buttonClass = (active: boolean) =>
    active
      ? "border border-black bg-accent px-1.5 py-1 text-white"
      : "border border-black bg-card px-1.5 py-1 hover:bg-accent hover:text-white";
  return (
    <div
      className="flex max-w-3xl mx-auto justify-end gap-2 items-center"
      data-testid="turn-feedback"
    >
      {submitFailed ? (
        <span data-testid="feedback-error" className="text-xs text-red-700">
          提交失败，请重试
        </span>
      ) : null}
      <button
        type="button"
        data-testid="feedback-up"
        aria-pressed={isFeedbackActive(turn, "up")}
        aria-label="有帮助"
        onClick={() => submit("up")}
        className={buttonClass(isFeedbackActive(turn, "up"))}
      >
        <ThumbIcon direction="up" />
      </button>
      <button
        type="button"
        data-testid="feedback-down"
        aria-pressed={isFeedbackActive(turn, "down")}
        aria-label="没帮助"
        onClick={() => submit("down")}
        className={buttonClass(isFeedbackActive(turn, "down"))}
      >
        <ThumbIcon direction="down" />
      </button>
    </div>
  );
}

// T3: stroke thumbs-up / thumbs-down icon, one path set
// mirrored vertically for "down" — matches the Sidebar nav-icon
// idiom (16x16 viewBox, stroke currentColor, no fill).
function ThumbIcon({ direction }: { direction: "up" | "down" }) {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="square"
      strokeLinejoin="miter"
      style={direction === "down" ? { transform: "rotate(180deg)" } : undefined}
    >
      <path d="M2 7h2.5v7H2z" />
      <path d="M4.5 8l3-5.5c.8 0 1.5.7 1.5 1.5V6.5h3.2c.9 0 1.5.8 1.3 1.6l-1 4.4c-.1.6-.7 1-1.3 1H4.5" />
    </svg>
  );
}

// T3 (spec Focus 1): the comparison the host tool computed, as a
// table rather than prose. Every figure here is a ``display``
// string off a fact row with a locator — this component formats
// nothing and computes nothing, so the numbers on screen are
// exactly the numbers the numeric gate holds the answer to.
//
// The trailing delta column is the redline: for a cross-period
// table it is the latest period-over-period change, for a peer
// table each row against the baseline. A row with no delta (the
// peer baseline, or any row where a missing cell suppressed the
// arithmetic) leaves the cell blank rather than showing a zero.
function ComparisonTable({ comparison }: { comparison: Comparison }) {
  const showDeltas = comparison.deltas.length > 0;
  return (
    <section
      data-testid="turn-comparison"
      data-axis={comparison.axis}
      className="border border-black bg-card"
    >
      <p className="border-b border-black px-3 py-1.5 text-xs font-mono text-gray-600">
        {comparisonCaption(comparison)}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="text-xs uppercase tracking-wide text-gray-500">
              <th scope="col" className="px-3 py-1.5 text-left font-medium">
                {comparison.rows[0]?.kind === "document" ? "文件" : "主体"}
              </th>
              {comparison.columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className="px-3 py-1.5 text-right font-medium whitespace-nowrap"
                >
                  {column.label}
                </th>
              ))}
              {showDeltas ? (
                <th
                  scope="col"
                  className="px-3 py-1.5 text-right font-medium whitespace-nowrap"
                >
                  变化
                </th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {comparison.rows.map((row) => {
              const delta = deltaForRow(comparison, row.key);
              return (
                <tr key={row.key} className="border-t border-gray-300">
                  <th
                    scope="row"
                    className="px-3 py-1.5 text-left font-medium whitespace-nowrap"
                  >
                    {row.label}
                  </th>
                  {row.cells.map((cell) => (
                    <td
                      key={`${cell.row}-${cell.column}`}
                      title={cellTitle(cell)}
                      data-status={cell.status}
                      className={
                        cell.status === "hit"
                          ? "px-3 py-1.5 text-right font-mono tabular-nums whitespace-nowrap"
                          : "px-3 py-1.5 text-right font-mono text-gray-400 whitespace-nowrap"
                      }
                    >
                      {cell.display}
                    </td>
                  ))}
                  {showDeltas ? (
                    <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap">
                      {delta ? (
                        <span className={directionClass(delta.direction)}>
                          {directionMark(delta.direction)} {delta.display}
                        </span>
                      ) : null}
                    </td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {/* The source units every number came from. A figure in this
          table is not a citation — the verifier only fills verified
          unit spans — but the operator still needs the trail back to
          the filing. */}
      {comparison.locators.length > 0 ? (
        <p className="border-t border-gray-300 px-3 py-1.5 text-xs text-gray-500">
          来源：
          {comparison.locators.map((l) => locatorLabel(l)).join("、")}
        </p>
      ) : null}
    </section>
  );
}

// thinking fold: live burst is expanded full text; collapsed is
// header-only (深度思考 + duration when measured). No line-clamp
// preview. When no duration was measured (0ms / legacy rows) a
// completed burst falls back to the plain collapsed label.
function formatThinkingDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const totalSeconds = Math.round(ms / 1000);
  return `${Math.floor(totalSeconds / 60)}m ${totalSeconds % 60}s`;
}

// turn-trace-folds: one thinking burst fold. Live = expanded
// with the FULL burst text (viewport pinned to the tail — no
// start-of-text clamp as the live view). Collapsed = 「深度思考」
// header (+ duration when the run measured one), burst body hidden.
// A manual toggle wins over the auto state until the burst's
// liveness changes: the override resets when ``live`` flips, so
// auto-collapse at burst end always runs (spec) but cannot fight
// an operator's mid-burst open.
function ThinkingFold({
  text,
  durationMs,
  live,
}: {
  text: string;
  durationMs?: number;
  live: boolean;
}) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  // Reset the manual override when the burst transitions live <-> ended.
  const [wasLive, setWasLive] = useState(live);
  const liveChanged = wasLive !== live;
  if (liveChanged) {
    setWasLive(live);
    setManualOpen(null);
  }
  const open = foldOpenState(manualOpen, live, liveChanged);
  const showDurationLabel =
    !live && typeof durationMs === "number" && durationMs > 0;
  const label =
    "深度思考" +
    (showDurationLabel
      ? ` · ${formatThinkingDuration(durationMs as number)}`
      : "");
  const visibleText = thinkingFoldVisibleText(open, text);
  return (
    <div className="mb-1">
      <button
        type="button"
        data-testid="trace-thinking-toggle"
        onClick={() => setManualOpen(!open)}
        className="text-xs font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600"
      >
        {label} {open ? "−" : "+"}
      </button>
      {visibleText !== null ? (
        <div
          className="mt-1 text-xs text-gray-600 whitespace-pre-wrap break-words"
          data-testid="trace-thinking-full"
        >
          {visibleText}
        </div>
      ) : null}
    </div>
  );
}

// Live-thinking tail pinning: keep the full text reachable — the
// expanded body is plain flow content; the page's own scroll area
// already follows the streaming tail because each frame appends at
// the bottom of the transcript.

// turn-trace-folds: one tool burst fold. Live = expanded,
// each call a compact row (name + human one-liner; running rows show
// the 运行中… placeholder). Collapsed = 「工具」 header + call count.
// Same manual-override semantics as ThinkingFold; the default never
// dumps raw JSON — a row's summary is the whole payload.
function ToolsFold({ calls, live }: { calls: ToolSummary[]; live: boolean }) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const [wasLive, setWasLive] = useState(live);
  const liveChanged = wasLive !== live;
  if (liveChanged) {
    setWasLive(live);
    setManualOpen(null);
  }
  const open = foldOpenState(manualOpen, live, liveChanged);
  return (
    <div className="mb-1">
      <button
        type="button"
        data-testid="trace-tools-toggle"
        onClick={() => setManualOpen(!open)}
        className="text-xs font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600"
      >
        工具{calls.length > 0 ? ` · ${calls.length}` : ""}
      </button>
      {open ? (
        <ul className="mt-1 space-y-1" data-testid="trace-tools-list">
          {calls.map((s, i) => (
            <li
              key={`${s.tool_call_id}-${i}`}
              className="bg-tool px-2 py-1 text-xs font-mono"
            >
              {s.name}: {s.summary}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function StatusLine({
  turn,
  track,
}: {
  turn: Pick<Turn, "status" | "in_flight">;
  track?: Turn["track"];
}) {
  // Soft-Track T5: light research-only hint on the status line.
  // No Chat|Research mode picker; non-research / missing track = no hint.
  const researchHint = showResearchTrackHint(track);
  const hint = researchHint ? (
    <span
      data-testid="research-track-hint"
      className="ml-1.5 normal-case tracking-normal font-normal text-gray-400"
    >
      · 研究
    </span>
  ) : null;

  // T2 fix: refuse / step_cap ordinary-person copy is the server's
  // ``assistant_message`` rendered by the parent ``TurnView``. ``StatusLine``
  // is now a small status label only — no client-side duplicate copy and
  // no raw ``reason`` enum leak. The wording comes from ``taskState`` so the
  // developer trace fallback reads the SAME strings.
  return (
    <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">
      {turnStatusLabel(turn)}
      {hint}
    </p>
  );
}
