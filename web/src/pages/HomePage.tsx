// Home: thread list summary + corpus cards. Empty corpus shows the
// ordinary-person bound copy; clicking a card creates a thread and
// prefills the composer (NO auto-send per spec).

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import CorpusCard from "../components/CorpusCard";
import {
  decideLastThreadRestore,
  forgetLastThread,
  readLastThread,
} from "../lastThread";
import { CORPUS_HOME_GATE_KEY } from "../sessionKeys";
import { useWorkspaceStore } from "../store";

function readCorpusHomeGate(): boolean {
  try {
    return window.sessionStorage.getItem(CORPUS_HOME_GATE_KEY) === "1";
  } catch {
    return false; /* storage unavailable - no gate. */
  }
}

export default function HomePage() {
  const threads = useWorkspaceStore((s) => s.threads);
  const corpus = useWorkspaceStore((s) => s.corpus);
  const loadingThreads = useWorkspaceStore((s) => s.loadingThreads);
  const corpusLoaded = useWorkspaceStore((s) => s.corpusLoaded);
  const navigate = useNavigate();

  // T5 (#35, spec line 42): a RETURNING visit restores the last
  // opened `/t/{id}` when that id still exists. First visit (no
  // stored id, or the thread was deleted / is unknown) stays on
  // home/corpus — never a blank chat. One-shot per mount so a
  // deliberate later navigation home does not bounce the operator
  // away again.
  const threadsLoaded = useWorkspaceStore((s) => s.threadsLoaded);
  const restoreAttempted = useRef(false);
  useEffect(() => {
    if (restoreAttempted.current) return;
    const decision = decideLastThreadRestore({
      threadsLoaded,
      lastThreadId: readLastThread(),
      threadIds: threads.map((t) => t.id),
      homeGateActive: readCorpusHomeGate(),
    });
    if (decision.action === "wait") return;
    restoreAttempted.current = true;
    if (decision.action === "restore") {
      navigate(`/t/${decision.threadId}`, { replace: true });
    } else if (decision.action === "forget") {
      forgetLastThread();
    }
  }, [threadsLoaded, threads, navigate]);

  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">研究对话</h2>
      <p className="text-sm text-gray-600 mb-4">
        就冻结的 AAPL / MSFT / AMZN 10-Q 提问。
      </p>

      <section aria-label="对话列表" className="mb-8">
        {threads.length === 0 ? (
          <p className="text-sm text-gray-500 border border-dashed border-black p-4 bg-card">
            尚无对话 — 在下方提问，或选择一份 filing。
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
          <p className="text-xs text-gray-400 mt-2">加载中…</p>
        )}
      </section>

      <section aria-label="Corpus">
        {!corpusLoaded ? (
          <>
            <h3 className="text-base font-semibold mb-1">正在加载 Corpus…</h3>
            <p className="text-sm text-gray-600 mb-3">
              从服务读取已冻结的 12 份 10-Q 索引
            </p>
            <div
              className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
              aria-busy="true"
            >
              <CorpusSkeletonCard />
              <CorpusSkeletonCard />
              <CorpusSkeletonCard />
            </div>
          </>
        ) : corpus.length === 0 ? (
          <div className="border border-black bg-card p-4 max-w-md">
            <h3 className="text-base font-semibold mb-1">Corpus 尚未 ingest</h3>
            <p className="text-sm text-gray-600 mb-3">
              索引已就绪，但提取树为空。请运行 ingest 后再提问。
            </p>
            <StartEmptyThreadButton />
          </div>
        ) : (
          <>
            <h3 className="text-base font-semibold mb-2">Corpus（冻结 12）</h3>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {corpus.map((f) => (
                <CorpusCard key={f.document_id} filing={f} />
              ))}
            </div>
          </>
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
      新建对话（Corpus 为空也可试）
    </button>
  );
}

function CorpusSkeletonCard() {
  return (
    <div
      aria-hidden="true"
      className="border border-black bg-card p-3 animate-pulse"
    >
      <div className="h-4 bg-gray-200 border border-gray-300 mb-2 w-3/4" />
      <div className="h-3 bg-gray-200 border border-gray-300 mb-1 w-1/2" />
      <div className="h-3 bg-gray-200 border border-gray-300 w-1/4" />
    </div>
  );
}
