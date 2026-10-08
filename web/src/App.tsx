import { type ReactNode, useEffect, useState } from "react";
import {
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import Sidebar from "./components/Sidebar";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";
import HomePage from "./pages/HomePage";
import NotFoundPage from "./pages/NotFoundPage";
import ThreadPage from "./pages/ThreadPage";
import { PRODUCT_ROUTE_PATHS } from "./productRoutes";
import { useWorkspaceStore } from "./store";
import {
  classifyThreadOpenFailure,
  type ThreadEntryFault,
  threadEntryView,
} from "./threadEntry";

// The product route table is pinned in ``productRoutes.ts`` so a test
// can assert the IA (no /kb, no /monitor) without a DOM. The render
// table below destructures it, so the two cannot drift.
const [HOME_ROUTE_PATH, THREAD_ROUTE_PATH] = PRODUCT_ROUTE_PATHS;

function ThreadRoute() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const openThread = useWorkspaceStore((s) => s.openThread);
  const activeThread = useWorkspaceStore((s) => s.activeThread);
  // Per-navigation loading flag. WHY: on a hard reload the GET is
  // still in flight when the first render happens, and
  // ``activeThread`` has not been populated yet — rendering the
  // not-found state at that moment would flash "Thread not found"
  // for every valid thread before the response lands (spec #3
  // reload race). Only a RESOLVED 404 renders NotFoundThread.
  const [loading, setLoading] = useState(false);
  // A 知识 MCP gate refusal is NOT a missing thread (ADR 0051): the
  // thread exists and is refused because the MCP cannot answer. Keep
  // them apart so opening an old thread on a 缺 / 坏 boot does not
  // claim the thread was deleted. The classification lives in
  // ``threadEntry.ts`` so it is testable without a DOM.
  const [fault, setFault] = useState<ThreadEntryFault | null>(null);

  useEffect(() => {
    if (!thread_id) return;
    let cancelled = false;
    setLoading(true);
    setFault(null);
    openThread(thread_id)
      .catch((error: unknown) => {
        if (cancelled) return;
        const kind = classifyThreadOpenFailure(error);
        // A missing thread still just clears the pane (the render
        // decision below reads that as "missing"); a gate refusal is
        // carried explicitly so it cannot be confused with it.
        if (kind === "missing") {
          useWorkspaceStore.setState({ activeThread: null });
        }
        setFault(kind);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [thread_id, openThread]);

  if (!thread_id) return <Navigate to="/" replace />;
  const view = threadEntryView({
    loading,
    fault,
    threadLoaded: activeThread?.id === thread_id,
  });
  if (view === "loading") {
    return <main className="p-6 text-sm text-gray-500">加载对话中…</main>;
  }
  if (view === "gated") return <GatedThreadNotice />;
  if (view === "pending_unavailable") return <PendingQuestionNotice />;
  if (view === "missing") return <NotFoundThread />;
  return <ThreadPage />;
}

// T4.5 fix (spec line 41 + SC line 66): the SC says "/t/{unknown} is
// not a blank chat (404 + back to list)". The earlier layout placed
// this card against `<main className="p-8">` so it pinned to the
// top-left and left ~90% of the viewport blank - functionally a blank
// chat surface. Center the card vertically with a min-height so the
// "404 + back" presentation has visual weight, then point the user
// home; other threads stay reachable in the sidebar 会话 group.
//
// All three /t/{id} refusals (unknown id, the 知识 MCP gate, and the
// pending-question 503) use this one card: same shell, different claim.
// Sharing it keeps the three presentations from drifting apart the way
// the copy already would.
function ThreadNoticeCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  return (
    <main className="flex items-center justify-center min-h-[60vh] p-8">
      <div className="max-w-lg border border-black bg-card p-6">
        <h2 className="text-lg font-semibold mb-2">{title}</h2>
        <p className="text-sm text-gray-600 mb-4">{children}</p>
        <button
          onClick={() => navigate("/")}
          className="border border-black px-3 py-1.5 text-sm hover:bg-accent hover:text-white"
        >
          返回首页
        </button>
      </div>
    </main>
  );
}

// The 缺 / 坏 boot refusal when opening an existing thread. Copy is the
// shared host string plus what the operator can do about it.
function GatedThreadNotice() {
  return (
    <ThreadNoticeCard title={KNOWLEDGE_MCP_FAULT_COPY}>
      这个对话还在，但知识检索服务连不上，暂时打不开。等服务恢复后重试。
    </ThreadNoticeCard>
  );
}

// The pending-question 503: the thread EXISTS but its paused choice could
// not be read this moment (host fixed detail, never checkpoint internals).
// The operator is told to retry opening — NOT that the thread is gone.
function PendingQuestionNotice() {
  return (
    <ThreadNoticeCard title="暂时无法打开">
      这个对话还在，但其中待回答的问题暂时读不出来。请重新打开这个
      thread 重试。
    </ThreadNoticeCard>
  );
}

function NotFoundThread() {
  return (
    <ThreadNoticeCard title="对话不存在">
      此研究 thread 不存在（可能已删除，或服务重启后未保留持久化
      checkpoint）。其他 thread 可在侧栏「会话」列表打开。
    </ThreadNoticeCard>
  );
}

export default function App() {
  const refreshThreads = useWorkspaceStore((s) => s.refreshThreads);
  // T10 (ADR 0033): the status fields are boot-scoped, so the
  // left-corner line fetches on open rather than polling.
  const loadStatus = useWorkspaceStore((s) => s.loadStatus);

  // workbench-retrieve-product-seam: the corpus-card chrome left the
  // product surface, so the boot-time ``GET /api/corpus`` has no
  // consumer here — the ingest store slice stays for the out-of-chrome
  // 入库 path and is loaded on demand by ``ingestFile``.
  useEffect(() => {
    void refreshThreads();
    void loadStatus();
  }, [refreshThreads, loadStatus]);

  return (
    <div className="flex h-screen">
      <Sidebar />
      <div className="flex-1 overflow-y-auto">
        <Routes>
          <Route path={HOME_ROUTE_PATH} element={<HomePage />} />
          <Route path={THREAD_ROUTE_PATH} element={<ThreadRoute />} />
          {/* No-match: the retired ``/kb`` / ``/monitor`` deep links
              (and any other unknown path) render a 404. Deliberately
              NOT a redirect home — a silent bounce would hide the
              retired route (spec SC7). */}
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </div>
    </div>
  );
}
