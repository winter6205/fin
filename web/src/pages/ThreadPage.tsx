// Chat page for one research thread: turn table + composer. Reload
// restores turns from GET (no re-invoke).

import { useEffect, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import AnswerWithCitations from "../components/CitationChips";
import Composer from "../components/Composer";
import CorpusCard from "../components/CorpusCard";
import type { Comparison, Turn } from "../types";
import {
  cellTitle,
  comparisonCaption,
  deltaForRow,
  directionClass,
  directionMark,
  locatorLabel,
  turnComparisons,
} from "../comparison";
import { exportAvailability, exportUrl } from "../exportBundle";
import { resolveTurnTrack, showResearchTrackHint } from "../softTrack";
import {
  shouldRenderStreamErrorKind,
  streamErrorKindRowText,
} from "../streamErrorKind";
import {
  canSubmitFeedback,
  feedbackErrorVisible,
  isFeedbackActive,
  type FeedbackValue,
} from "../turnFeedback";
import { useWorkspaceStore } from "../store";

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

// T2 fix: the SPA's refuse / step_cap ordinary-person copy is the
// server-emitted ``assistant_message`` (see run_payload.ORDINARY_PERSON_COPY).
// A client-side duplicate was rendering two near-identical sentences and
// leaking the raw enum reason ("(unknown_locator)"). Delete the duplicate
// so ``assistant_message`` is the sole user-facing copy on those turns.

export default function ThreadPage() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const location = useLocation();
  const activeThread = useWorkspaceStore((s) => s.activeThread);
  const sendingQuestion = useWorkspaceStore((s) => s.sendingQuestion);

  // Prefill from navigation state (corpus card click).
  const navState = location.state as { prefill?: string } | null;
  const [prefill, setPrefill] = useState<string | null>(
    navState?.prefill ?? null,
  );

  // Re-sync from the persisted turn table when we arrive at a
  // thread (reload / switch back). GET only - no graph invoke.
  useEffect(() => {
    if (!thread_id) return;
    const open = useWorkspaceStore.getState().openThread;
    void open(thread_id).catch(() => undefined);
  }, [thread_id]);

  // T4 fix (code-review H2): the ``resumingThreadId`` selector
  // MUST be called above the early-return so the hook count stays
  // stable across renders (Rule of Hooks). Earlier layout
  // crashed React on every thread navigation with "Rendered more
  // hooks than during the previous render".
  const resumingThreadId = useWorkspaceStore((s) => s.resumingThreadId);

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
        <ExportLinks threadId={thread_id} turns={activeThread.turns} />
      </header>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* T4: crash banner shown when GET reports a PENDING
            checkpoint (process died mid-run). The lock is no
            longer held; only a checkpoint is left. Resume button
            drives ``resumeThread`` which POSTs /resume. */}
        {pendingCheckpoint && !isResuming ? (
          <CrashResumeBanner threadId={thread_id} />
        ) : null}
        {isResuming ? <InterruptedRunBanner threadId={thread_id} /> : null}

        {activeThread.turns.map((turn, index) => (
          // Turn rows are append-only; a stable index key avoids
          // remounting when the server-minted ``run_id`` reconciles.
          <TurnView key={index} turn={turn} threadId={thread_id} />
        ))}

        {/* Spec #2: a freshly-created (zero-turn) thread must still
            surface the corpus cards so the operator can pick a
            filing without going back to home. Same createThread
            ({prefill}) flow as the home grid; never auto-sends. */}
        {activeThread.turns.length === 0 ? <EmptyThreadCorpusGrid /> : null}
      </div>

      <Composer
        threadId={thread_id}
        prefill={sendingQuestion ? null : prefill}
        onPrefillConsumed={() => setPrefill(null)}
      />
    </main>
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

// T4 workflow (the tracked change): download this thread's comparison or citation
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

function EmptyThreadCorpusGrid() {
  const corpus = useWorkspaceStore((s) => s.corpus);
  if (corpus.length === 0) return null;
  return (
    <section aria-label="从 filing 开始" className="max-w-3xl mx-auto pt-4">
      <h3 className="text-sm font-semibold text-gray-500 mb-2">
        或从一份 filing 开始
      </h3>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {corpus.map((f) => (
          <CorpusCard key={f.document_id} filing={f} />
        ))}
      </div>
    </section>
  );
}

function TurnView({ turn, threadId }: { turn: Turn; threadId: string }) {
  // T4 (#37): while THIS turn is live (optimistic send or a
  // lock-held latest row) the tool rows stay open and the thinking
  // region line-clamps; after the run both collapse.
  const live = turn.in_flight === true;
  const hasTrace =
    Boolean(turn.thinking_text) ||
    (turn.tool_summaries != null && turn.tool_summaries.length > 0);
  // T3 (spec Focus 1): structured comparison tables from the
  // ``compare_facts`` tool. Rendered between the trace and the
  // answer card — the order they arrived in, and the one place
  // they stay visible on a refuse row where the answer card holds
  // only the ordinary-person copy.
  const comparisons = turnComparisons(turn);
  return (
    <article className="max-w-3xl mx-auto space-y-2">
      <div className="flex justify-end">
        <div className="border border-black bg-tool px-3 py-2 max-w-[80%] text-sm whitespace-pre-wrap">
          {turn.question}
        </div>
      </div>
      {/* Trace (spec lines 31–32, SC66): thinking + tools grouped
          unframed on page bg between user bubble and answer card —
          assistant-ui chain-of-thought style; not inside bg-card. */}
      {hasTrace ? (
        <div data-testid="turn-trace" className="space-y-1">
          {/* T4 (spec line 31): thinking is unframed; while the run is
              live it line-clamps to a few lines; after the run it
              collapses to a one-line duration label that expands the
              full text. Region exists only when thinking streamed. */}
          {turn.thinking_text ? (
            <ThinkingAccordion
              text={turn.thinking_text}
              durationMs={turn.thinking_duration_ms}
              live={live}
            />
          ) : null}
          {/* T4 (spec line 32): tool rows are separate light-gray
              (#F5F5F4) details — visible while that call runs, then
              collapse after the run. Correlated by tool_call_id. */}
          {turn.tool_summaries && turn.tool_summaries.length > 0 ? (
            <div className="space-y-1">
              {turn.tool_summaries.map((s, i) => (
                <details
                  key={`${s.tool_call_id}-${i}`}
                  className="bg-tool px-2 py-1 text-xs"
                  open={live}
                >
                  <summary className="cursor-pointer select-none font-mono">
                    {s.name}: {s.summary}
                  </summary>
                </details>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      {comparisons.map((comparison, i) => (
        <ComparisonTable
          key={`${comparison.tool_call_id ?? "compare"}-${i}`}
          comparison={comparison}
        />
      ))}
      {live && !turn.answer && !turn.assistant_message ? null : (
        <div
          data-testid="turn-answer-card"
          className="border border-black bg-card p-3"
        >
          {/* T4 (#37): while THIS turn streams, the optimistic status
              placeholder would read "done" (the wire carries no
              running status per spec line 30) — hide the status line
              until the run actually ends. */}
          {!live ? (
            <StatusLine
              status={turn.status}
              reason={turn.status_reason ?? ""}
              track={resolveTurnTrack(turn)}
            />
          ) : null}
          {turn.answer ? (
            <AnswerWithCitations
              answer={turn.answer}
              citations={turn.citations}
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
      {/* T8: the thumbs tail card — the operator's verdict on this
          turn, rendered after the answer card so it reads as a
          per-turn action, not part of the answer. Gated to terminal
          rows (canSubmitFeedback) so a live optimistic turn — whose
          run_id is still a client placeholder — shows nothing. */}
      {canSubmitFeedback(turn) ? (
        <FeedbackControls threadId={threadId} turn={turn} />
      ) : null}
    </article>
  );
}

// T8: thumbs up/down for one terminal run. Clicking submits through
// the store's ``setTurnFeedback`` (response-driven: the row's
// ``feedback`` updates only after the host confirms the persisted
// value) and re-clicking the active thumb re-affirms it — the host
// has no "clear" verb, last write wins. A rejected submit (400/503)
// surfaces as an inline "提交失败，请重试" hint next to the thumbs;
// the hint clears on the next submit (transition pinned in
// ``turnFeedback.feedbackErrorVisible``).
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
    setSubmitFailed((current) => feedbackErrorVisible(current, "submit"));
    setTurnFeedback(threadId, turn.run_id, value).catch(() => {
      setSubmitFailed((current) => feedbackErrorVisible(current, "failure"));
    });
  };
  const buttonClass = (active: boolean) =>
    active
      ? "border border-black bg-accent px-2 py-1 text-xs text-white"
      : "border border-black bg-card px-2 py-1 text-xs hover:bg-accent hover:text-white";
  return (
    <div className="flex max-w-3xl mx-auto justify-end gap-2 items-center" data-testid="turn-feedback">
      {submitFailed ? (
        <span data-testid="feedback-error" className="text-xs text-red-700">
          提交失败，请重试
        </span>
      ) : null}
      <button
        type="button"
        data-testid="feedback-up"
        aria-pressed={isFeedbackActive(turn, "up")}
        onClick={() => submit("up")}
        className={buttonClass(isFeedbackActive(turn, "up"))}
      >
        有帮助
      </button>
      <button
        type="button"
        data-testid="feedback-down"
        aria-pressed={isFeedbackActive(turn, "down")}
        onClick={() => submit("down")}
        className={buttonClass(isFeedbackActive(turn, "down"))}
      >
        没帮助
      </button>
    </div>
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
      {/* The pages every number came from. A figure in this table
          is not a citation — the verifier only fills verified page
          spans — but the operator still needs the trail back to
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

// T4 (#37): the thinking region — unframed (no border/background,
// per spec line 31). While the run is LIVE the prose line-clamps to
// a few lines (click expands); AFTER the run it collapses to a
// one-line duration label ("Thought for 4.2s") that expands the
// full text on click. When no duration was measured (0ms / legacy
// rows) a completed run falls back to the plain collapsed label.
function formatThinkingDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const totalSeconds = Math.round(ms / 1000);
  return `${Math.floor(totalSeconds / 60)}m ${totalSeconds % 60}s`;
}

function ThinkingAccordion({
  text,
  durationMs,
  live,
}: {
  text: string;
  durationMs?: number;
  live: boolean;
}) {
  const [open, setOpen] = useState(false);
  const showDurationLabel =
    !live && typeof durationMs === "number" && durationMs > 0;
  const label = showDurationLabel
    ? `Thinking · ${formatThinkingDuration(durationMs as number)}`
    : "Thinking";
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-xs font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600"
      >
        {label} {open ? "−" : "+"}
      </button>
      {open || live ? (
        <p
          className={
            open
              ? "mt-1 text-xs text-gray-600 whitespace-pre-wrap break-words"
              : "mt-1 text-xs text-gray-600 whitespace-pre-wrap break-words line-clamp-3"
          }
        >
          {text}
        </p>
      ) : null}
    </div>
  );
}

function StatusLine({
  status,
  reason: _reason,
  track,
}: {
  status: Turn["status"];
  reason: string;
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

  if (status === "done") {
    return (
      <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">
        完成
        {hint}
      </p>
    );
  }
  // T2 fix: refuse / step_cap ordinary-person copy is the server's
  // ``assistant_message`` rendered by the parent ``TurnView``. ``StatusLine``
  // is now a small status label only — no client-side duplicate copy and
  // no raw ``reason`` enum leak.
  const label = status === "refuse" ? "未完成" : "步数上限（step_cap）";
  return (
    <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">
      {label}
      {hint}
    </p>
  );
}
