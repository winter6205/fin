import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import HomePage from "./pages/HomePage";
import ThreadPage from "./pages/ThreadPage";
import { useWorkspaceStore } from "./store";

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

  useEffect(() => {
    if (!thread_id) return;
    let cancelled = false;
    setLoading(true);
    openThread(thread_id)
      .catch(() => {
        // Unknown /t/{id} is an ordinary 404 + back to list per spec.
        if (!cancelled) useWorkspaceStore.setState({ activeThread: null });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [thread_id, openThread]);

  if (!thread_id) return <Navigate to="/" replace />;
  if (loading) {
    return (
      <main className="p-6 text-sm text-gray-500">Loading thread…</main>
    );
  }
  if (!activeThread || activeThread.id !== thread_id) {
    return <NotFoundThread />;
  }
  return <ThreadPage />;
}

function NotFoundThread() {
  const navigate = useNavigate();
  return (
    <main className="p-8">
      <div className="max-w-md border border-black bg-card p-6">
        <h2 className="text-lg font-semibold mb-2">Thread not found</h2>
        <p className="text-sm text-gray-600 mb-4">
          This research thread does not exist (it may have been deleted).
        </p>
        <button
          onClick={() => navigate("/")}
          className="border border-black px-3 py-1.5 text-sm hover:bg-accent hover:text-white"
        >
          Back to threads
        </button>
      </div>
    </main>
  );
}

export default function App() {
  const refreshThreads = useWorkspaceStore((s) => s.refreshThreads);
  const loadCorpus = useWorkspaceStore((s) => s.loadCorpus);

  useEffect(() => {
    void refreshThreads();
    void loadCorpus();
  }, [refreshThreads, loadCorpus]);

  return (
    <div className="flex h-screen">
      <Sidebar />
      <div className="flex-1 overflow-y-auto">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/t/:thread_id" element={<ThreadRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  );
}
