// Chat page for one research thread: turn table + composer. Reload
// restores turns from GET (no re-invoke).

import { useEffect, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import AnswerWithCitations from "../components/CitationChips";
import Composer from "../components/Composer";
import CorpusCard from "../components/CorpusCard";
import type { Turn } from "../types";
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
      <header className="border-b border-black px-4 py-3 bg-page">
        <h2 className="text-sm font-semibold truncate">{activeThread.title}</h2>
        <p className="text-xs text-gray-500">
          创建于 {new Date(activeThread.created_at).toLocaleString()}
        </p>
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
          <TurnView key={index} turn={turn} />
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

function TurnView({ turn }: { turn: Turn }) {
  // T4 (#37): while THIS turn is live (optimistic send or a
  // lock-held latest row) the tool rows stay open and the thinking
  // region line-clamps; after the run both collapse.
  const live = turn.in_flight === true;
  const hasTrace =
    Boolean(turn.thinking_text) ||
    (turn.tool_summaries != null && turn.tool_summaries.length > 0);
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
        </div>
      )}
    </article>
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
}: {
  status: Turn["status"];
  reason: string;
}) {
  if (status === "done") {
    return (
      <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">完成</p>
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
    </p>
  );
}
