// Home: thread list summary + corpus cards. Empty corpus shows the
// ordinary-person bound copy; clicking a card creates a thread and
// prefills the composer (NO auto-send per spec).

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import CorpusCard from "../components/CorpusCard";
import { useWorkspaceStore } from "../store";

export default function HomePage() {
  const threads = useWorkspaceStore((s) => s.threads);
  const corpus = useWorkspaceStore((s) => s.corpus);
  const loadingThreads = useWorkspaceStore((s) => s.loadingThreads);

  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">Research threads</h2>
      <p className="text-sm text-gray-600 mb-4">
        Ask about the frozen AAPL / MSFT / AMZN 10-Qs.
      </p>

      <section aria-label="Thread list" className="mb-8">
        {threads.length === 0 ? (
          <p className="text-sm text-gray-500 border border-dashed border-black p-4 bg-card">
            No threads yet — start with a question below, or pick a filing.
          </p>
        ) : (
          <ul className="grid gap-2 md:grid-cols-2">
            {threads.slice(0, 6).map((t) => (
              <li key={t.id} className="border border-black bg-card p-3">
                <a
                  href={`/t/${t.id}`}
                  className="block text-sm font-medium hover:text-accent"
                >
                  {t.title}
                </a>
                <span className="text-xs text-gray-500">
                  {new Date(t.created_at).toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        )}
        {loadingThreads && (
          <p className="text-xs text-gray-400 mt-2">Loading…</p>
        )}
      </section>

      <section aria-label="Corpus">
        <h3 className="text-base font-semibold mb-2">Corpus (frozen 12)</h3>
        {corpus.length === 0 ? (
          <div className="border border-black bg-card p-4 max-w-md">
            <p className="text-sm font-medium mb-1">Corpus is empty</p>
            <p className="text-sm text-gray-600 mb-3">
              The frozen filings are not ingested yet — run the ingest step
              to populate the extracted tree.
            </p>
            <StartEmptyThreadButton />
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {corpus.map((f) => (
              <CorpusCard key={f.document_id} filing={f} />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

function StartEmptyThreadButton() {
  const createThread = useWorkspaceStore((s) => s.createThread);
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  return (
    <button
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          const id = await createThread(null);
          if (id) navigate(`/t/${id}`);
        } finally {
          setBusy(false);
        }
      }}
      className="border border-black px-3 py-1.5 text-sm hover:bg-accent hover:text-white"
    >
      + New thread
    </button>
  );
}
