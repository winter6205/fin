// Chat page for one research thread: turn table + composer. Reload
// restores turns from GET (no re-invoke).

import { useEffect, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import AnswerWithCitations from "../components/CitationChips";
import Composer from "../components/Composer";
import CorpusCard from "../components/CorpusCard";
import type { Turn } from "../types";
import { useWorkspaceStore } from "../store";

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

  if (!thread_id || !activeThread || activeThread.id !== thread_id) {
    return <main className="p-6 text-sm text-gray-500">Loading thread…</main>;
  }

  return (
    <main className="flex flex-col h-full">
      <header className="border-b border-black px-4 py-3 bg-page">
        <h2 className="text-sm font-semibold truncate">{activeThread.title}</h2>
        <p className="text-xs text-gray-500">
          Created {new Date(activeThread.created_at).toLocaleString()}
        </p>
      </header>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {activeThread.turns.map((turn) => (
          <TurnView key={turn.run_id} turn={turn} />
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

function EmptyThreadCorpusGrid() {
  const corpus = useWorkspaceStore((s) => s.corpus);
  if (corpus.length === 0) return null;
  return (
    <section
      aria-label="Start with a filing"
      className="max-w-3xl mx-auto pt-4"
    >
      <h3 className="text-sm font-semibold text-gray-500 mb-2">
        Or start from a filing
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
  return (
    <article className="max-w-3xl mx-auto space-y-2">
      <div className="flex justify-end">
        <div className="border border-black bg-tool px-3 py-2 max-w-[80%] text-sm whitespace-pre-wrap">
          {turn.question}
        </div>
      </div>
      <div className="border border-black bg-card p-3">
        <StatusLine status={turn.status} reason={turn.status_reason ?? ""} />
        {/* T3 (spec): the model's reasoning prose, unframed and
            line-clamped to 3 lines; click to expand. Empty when the
            model surfaced no reasoning (SC60 binding: this prose
            never participates in citation matching server-side). */}
        {turn.thinking_text ? (
          <ThinkingAccordion text={turn.thinking_text} />
        ) : null}
        {/* T3 (spec): one collapsed row per tool call observed this
            run, ``bg-tool`` (#F5F5F4) panel per spec. Rows arrive
            live via SSE (``tool_call`` -> "running…" placeholder,
            ``tool_result`` -> formatted one-liner) and persist on
            the turn as ``tool_summaries`` after reload. */}
        {turn.tool_summaries && turn.tool_summaries.length > 0 ? (
          <div className="mb-2 space-y-1">
            {turn.tool_summaries.map((s, i) => (
              <details
                key={`${s.tool_call_id}-${i}`}
                className="bg-tool px-2 py-1 text-xs"
              >
                <summary className="cursor-pointer select-none font-mono">
                  {s.name}: {s.summary}
                </summary>
              </details>
            ))}
          </div>
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
    </article>
  );
}

// T3: "Thinking" region — unframed (no border/background, per spec),
// collapsed to 3 lines via ``line-clamp-3``; click toggles full prose.
function ThinkingAccordion({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-xs font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600"
      >
        Thinking {open ? "−" : "+"}
      </button>
      <p
        className={
          open
            ? "mt-1 text-xs text-gray-600 whitespace-pre-wrap break-words"
            : "mt-1 text-xs text-gray-600 whitespace-pre-wrap break-words line-clamp-3"
        }
      >
        {text}
      </p>
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
      <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">done</p>
    );
  }
  // T2 fix: refuse / step_cap ordinary-person copy is the server's
  // ``assistant_message`` rendered by the parent ``TurnView``. ``StatusLine``
  // is now a small status label only — no client-side duplicate copy and
  // no raw ``reason`` enum leak.
  const label = status === "refuse" ? "did not finish" : "step fuse hit";
  return (
    <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">
      {label}
    </p>
  );
}
