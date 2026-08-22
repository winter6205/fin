// Chat page for one research thread: turn table + composer. Reload
// restores turns from GET (no re-invoke).

import { useEffect, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import CitationList from "../components/CitationChips";
import Composer from "../components/Composer";
import CorpusCard from "../components/CorpusCard";
import type { Turn } from "../types";
import { useWorkspaceStore } from "../store";

const ORDINARY_COPY: Record<string, string> = {
  refuse:
    "Could not ground this answer in the frozen 10-Qs. Try rephrasing or asking about the listed filings.",
  step_cap: "Hit the 32-step fuse before finishing. Try a narrower question.",
};

export default function ThreadPage() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const location = useLocation();
  const activeThread = useWorkspaceStore((s) => s.activeThread);
  const sendingQuestion = useWorkspaceStore((s) => s.sendingQuestion);

  // Prefill from navigation state (corpus card click).
  const navState = location.state as { prefill?: string } | null;
  const [prefill, setPrefill] = useState<string | null>(
    navState?.prefill ?? null
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
        <h2 className="text-sm font-semibold truncate">
          {activeThread.title}
        </h2>
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
    <section aria-label="Start with a filing" className="max-w-3xl mx-auto pt-4">
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
        {turn.answer ? (
          <p className="text-sm whitespace-pre-wrap">{turn.answer}</p>
        ) : null}
        <CitationList citations={turn.citations} />
      </div>
    </article>
  );
}

function StatusLine({
  status,
  reason,
}: {
  status: Turn["status"];
  reason: string;
}) {
  if (status === "done") {
    return (
      <p className="text-xs uppercase tracking-wide text-gray-400 mb-1">
        done
      </p>
    );
  }
  const copy =
    status === "refuse"
      ? ORDINARY_COPY.refuse + (reason ? ` (${reason})` : "")
      : ORDINARY_COPY.step_cap;
  return (
    <p className="text-sm text-gray-600 italic border-l-2 border-black pl-2 mb-2">
      {copy}
    </p>
  );
}
