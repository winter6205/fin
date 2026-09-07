// /: minimal home. The workbench chrome has no 对话总览 — research
// threads live in the sidebar 会话 disclosure
// (specs/workbench-sidebar-rail.md T2), so this page renders only an
// empty placeholder pointing at the composer / 知识库. What stays is
// the last-thread restore for returning visits (decideLastThreadRestore
// + the /kb, /monitor session gates): a restorable thread still
// bounces straight to /t/{id}; the gates keep the operator on the
// chrome surface they picked.

import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  decideLastThreadRestore,
  forgetLastThread,
  readLastThread,
} from "../lastThread";
import { KB_GATE_KEY, MONITOR_GATE_KEY } from "../sessionKeys";
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
      {/* No thread-card wall here (the 对话总览 the spec retired):
          threads are listed and opened in the sidebar 会话 group.
          The empty state keeps the dashed-border copy so a fresh
          visit is never a blank pane. */}
      {threads.length === 0 && !loadingThreads ? (
        <p className="text-sm text-gray-500 border border-dashed border-black p-4 bg-card">
          尚无对话 — 在下方提问，或选择一份语料（侧栏 知识库）。
        </p>
      ) : null}
      {loadingThreads && <p className="text-xs text-gray-400 mt-2">加载中…</p>}
    </main>
  );
}
