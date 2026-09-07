// /: 对话. The workbench chrome's chat surface: an empty placeholder
// for operators with no threads, and the last-thread restore for
// returning visits. T1 (specs/workbench-chrome.md) split the corpus
// catalog onto its own ``/kb`` page; the card grid no longer lives
// here. T2 (specs/workbench-chrome.md) split the watchlist / notices
// onto ``/monitor``. The sidebar list remains the thread path.

import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  decideLastThreadRestore,
  forgetLastThread,
  readLastThread,
} from "../lastThread";
import {
  CORPUS_HOME_GATE_KEY,
  KB_GATE_KEY,
  MONITOR_GATE_KEY,
} from "../sessionKeys";
import { useWorkspaceStore } from "../store";

function readSessionGate(key: string): boolean {
  try {
    return window.sessionStorage.getItem(key) === "1";
  } catch {
    return false; /* storage unavailable - no gate. */
  }
}

export default function HomePage() {
  const threads = useWorkspaceStore((s) => s.threads);
  const loadingThreads = useWorkspaceStore((s) => s.loadingThreads);
  const navigate = useNavigate();

  // T5 (#35, spec line 42): a RETURNING visit restores the last
  // opened `/t/{id}` when that id still exists. First visit (no
  // stored id, or the thread was deleted / is unknown) stays on
  // home — never a blank chat. T1 /kb and T2 /monitor add their own
  // session gates so a returning visit lands back on the surface
  // the operator picked rather than bouncing onto the last thread.
  // One-shot per mount so a deliberate later navigation home does
  // not bounce the operator away again.
  const threadsLoaded = useWorkspaceStore((s) => s.threadsLoaded);
  const restoreAttempted = useRef(false);
  useEffect(() => {
    if (restoreAttempted.current) return;
    const decision = decideLastThreadRestore({
      threadsLoaded,
      lastThreadId: readLastThread(),
      threadIds: threads.map((t) => t.id),
      homeGateActive: readSessionGate(CORPUS_HOME_GATE_KEY),
      kbGateActive: readSessionGate(KB_GATE_KEY),
      monitorGateActive: readSessionGate(MONITOR_GATE_KEY),
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
        就本地语料库里的 SEC filing 提问。
      </p>

      <section aria-label="对话列表" className="mb-8">
        {threads.length === 0 ? (
          <p className="text-sm text-gray-500 border border-dashed border-black p-4 bg-card">
            尚无对话 — 在下方提问，或选择一份语料（点 1）。
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
    </main>
  );
}
