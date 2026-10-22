import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import Sidebar from "./components/Sidebar";
import DemoModeSurface from "./components/DemoModeSurface";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";
import HomePage from "./pages/HomePage";
import NotFoundPage from "./pages/NotFoundPage";
import DemoSourcePage from "./pages/DemoSourcePage";
import ThreadPage from "./pages/ThreadPage";
import { getDemoStatus } from "./demoMode";
import { DEMO_SOURCE_ROUTE_PATH, PRODUCT_ROUTE_PATHS } from "./productRoutes";
import { useWorkspaceStore } from "./store";
import {
  classifyThreadOpenFailure,
  type ThreadEntryFault,
  threadEntryIsLive,
  threadEntryView,
} from "./threadEntry";
import {
  forgetLastThread,
  readLastThread,
  type DemoModeState,
  reduceStartupOwner,
  type StartupOwnerEvent,
  type StartupOwnerState,
} from "./lastThread";

// The product route table is pinned in ``productRoutes.ts`` so a test
// can assert the IA (no /kb, no /monitor) without a DOM. The render
// table below destructures it, so the two cannot drift.
const [HOME_ROUTE_PATH, THREAD_ROUTE_PATH] = PRODUCT_ROUTE_PATHS;

// ----- the thread route's entry decision (T2) -----
//
// The route owns the PANE. A thread that is already answering on THIS
// client owns the pane too: the entry must not paint a loader over the
// live answer, and a failing entry read must not wipe the live turn
// table. The facts come from the workspace store; the decision is a
// pure function so its boundaries (a live answer, an ordinary read in
// flight, the three refusals) are testable without a DOM.

/** What the thread route paints. */
export type ThreadEntryRouteView =
  | "redirect"
  | "live"
  | "loading"
  | "gated"
  | "pending_unavailable"
  | "missing"
  | "thread";

export interface ThreadEntryRouteFacts {
  /** The thread id in the route, or null when the route carries none. */
  threadId: string | null;
  /**
   * The thread-scoped in-flight fact: the thread whose send is running
   * on THIS client, or null. It is set when a send starts and cleared
   * when it settles.
   */
  sendingThreadId: string | null;
  /** The id of the thread the pane currently holds. */
  activeThreadId: string | null;
  /** An entry read is in flight. */
  reading: boolean;
  /** The refusal the last entry read produced. */
  fault: ThreadEntryFault | null;
}

export interface ThreadEntryRouteDecision {
  /** What the route paints. */
  view: ThreadEntryRouteView;
  /**
   * Whether a failed entry read may clear the pane's turn table. It may
   * only clear its OWN thread, and never while that thread is
   * streaming: a failing read must not wipe a live answer, and it must
   * not clear ANOTHER thread's pane either.
   */
  mayClearActiveThread: boolean;
}

export function decideThreadEntryRoute(
  facts: ThreadEntryRouteFacts,
): ThreadEntryRouteDecision {
  if (facts.threadId === null) {
    return { view: "redirect", mayClearActiveThread: false };
  }
  // The live answer owns the pane: no loader over it, and no refusal
  // card in place of it. BOTH facts are needed — the send is running on
  // this client AND the pane still holds this thread. When the operator
  // LEFT the thread while its send was in flight, the live state is
  // gone and the entry read is REQUIRED — the store performs it, and
  // the route must show the loader while it runs. The rule is the
  // SHARED one the store's guard and the thread page's re-sync ask, so
  // the three cannot disagree.
  const live = threadEntryIsLive({
    threadId: facts.threadId,
    sendingThreadId: facts.sendingThreadId,
    activeThreadId: facts.activeThreadId,
  });
  if (live) return { view: "live", mayClearActiveThread: false };
  const view = threadEntryView({
    loading: facts.reading,
    fault: facts.fault,
    threadLoaded: facts.activeThreadId === facts.threadId,
  });
  return {
    view,
    mayClearActiveThread: facts.activeThreadId === facts.threadId,
  };
}

function ThreadRoute() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const openThread = useWorkspaceStore((s) => s.openThread);
  const activeThread = useWorkspaceStore((s) => s.activeThread);
  // T2: the thread-scoped in-flight fact. A thread that is already
  // answering on this client keeps its live answer — the entry must
  // not cover it, and a failing read must not clear it.
  const sendingThreadId = useWorkspaceStore((s) => s.sendingThreadId);
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
    // The read itself is the STORE's decision: it knows whether this
    // thread is already being answered on this client and may skip a
    // redundant GET. The route owns the presentation around it.
    openThread(thread_id)
      .catch((error: unknown) => {
        if (cancelled) return;
        const kind = classifyThreadOpenFailure(error);
        // The same decision the render uses: a live thread is never
        // cleared and never refused, and only the pane's OWN thread
        // may be cleared at all.
        const entry = decideThreadEntryRoute({
          threadId: thread_id,
          sendingThreadId: useWorkspaceStore.getState().sendingThreadId,
          activeThreadId: useWorkspaceStore.getState().activeThread?.id ?? null,
          reading: false,
          fault: null,
        });
        if (entry.view === "live") return;
        if (kind === "missing" && entry.mayClearActiveThread) {
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

  const decision = decideThreadEntryRoute({
    threadId: thread_id ?? null,
    sendingThreadId,
    activeThreadId: activeThread?.id ?? null,
    reading: loading,
    fault,
  });

  if (decision.view === "redirect") return <Navigate to="/" replace />;
  // A thread that is already streaming on this client renders its live
  // answer — never the entry loader over it, never a refusal card in
  // place of it.
  if (decision.view === "live") return <ThreadPage />;
  if (decision.view === "loading") {
    return <main className="p-6 text-sm text-gray-500">加载对话中…</main>;
  }
  if (decision.view === "gated") return <GatedThreadNotice />;
  if (decision.view === "pending_unavailable") return <PendingQuestionNotice />;
  if (decision.view === "missing") return <NotFoundThread />;
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

/**
 * The application-lifetime startup owner (T3).
 *
 * WHY HERE: the hook is called by ``App``, the router root. App is
 * rebuilt only by a real browser reload and it survives every route
 * change, so the one-shot restore opportunity cannot be re-armed by a
 * HomePage remount — browser Back to "/" used to bounce the operator
 * straight back into the thread. The consumed bit lives in a REF: in
 * memory, never in browser storage, so a reload grants a fresh
 * opportunity while a route change does not.
 *
 * The rules themselves live in the PURE reducer (``reduceStartupOwner``):
 * the opportunity, the navigation that consumes it and the demo status
 * that gates it are one state machine, so they can be exercised
 * behaviourally instead of hiding in hook refs.
 */
function useStartupRestore(): { demoMode: DemoModeState } {
  const threads = useWorkspaceStore((s) => s.threads);
  const threadsLoaded = useWorkspaceStore((s) => s.threadsLoaded);
  const navigate = useNavigate();
  const location = useLocation();
  // The owner's state: the one-shot opportunity and the demo fact.
  const stateRef = useRef<StartupOwnerState>({
    opportunity: "available",
    demoMode: "pending",
  });
  // The path the application STARTED on: an initial deep link is not
  // eligible for the restore.
  const initialPathRef = useRef(location.pathname);
  // The last location the owner saw. A NEW location object means the
  // operator navigated, which consumes a pending opportunity so a late
  // demo status or a late thread list cannot redirect them afterwards.
  const seenLocationRef = useRef(location);
  // The demo fact for the chrome. The reducer owns the DECISION; this
  // mirrors the resolved fact for rendering.
  const [demoMode, setDemoMode] = useState<DemoModeState>("pending");

  const dispatch = useCallback(
    (event: StartupOwnerEvent) => {
      // The facts are read per dispatch, not captured: a stale closure
      // would decide against a thread list that has already answered.
      const state = useWorkspaceStore.getState();
      const result = reduceStartupOwner(stateRef.current, event, {
        startedAtHome: initialPathRef.current === HOME_ROUTE_PATH,
        threadsLoaded: state.threadsLoaded,
        lastThreadId: readLastThread(),
        threadIds: state.threads.map((t) => t.id),
      });
      stateRef.current = result.state;
      setDemoMode(result.state.demoMode);
      // "wait" keeps the opportunity open: the demo status or the thread
      // list has not answered yet. Every other outcome CONSUMES it, so
      // the decision is made exactly once per application start.
      if (result.decision.action === "restore") {
        navigate(`/t/${result.decision.threadId}`, { replace: true });
      } else if (result.decision.action === "forget") {
        forgetLastThread();
      }
    },
    [navigate],
  );

  // This effect MUST be declared before the decision effect below: on a
  // navigation both run in the same commit, and the decision must see
  // the consumed bit.
  useEffect(() => {
    if (seenLocationRef.current === location) return;
    seenLocationRef.current = location;
    dispatch({ type: "navigated" });
  }, [location, dispatch]);

  // The demo status is the owner's other input: the server's answer, or
  // its silence. A failed fetch is "unavailable", never "disabled" — a
  // server that never answered may not license a speculative restore.
  useEffect(() => {
    let mounted = true;
    void getDemoStatus()
      .then((status) => {
        if (!mounted) return;
        dispatch({
          type: "demo_status",
          outcome: { kind: "resolved", enabled: status.enabled },
        });
      })
      .catch(() => {
        // EXIT: hide demo-only context and controls unless the server confirms demo mode.
        // The startup decision reads this as "unavailable", NOT
        // "disabled": a server that never answered may not license a
        // speculative restore.
        if (!mounted) return;
        dispatch({ type: "demo_status", outcome: { kind: "failed" } });
      });
    return () => {
      mounted = false;
    };
  }, [dispatch]);

  // The decision effect: the owner re-reads its facts (the thread list
  // arrived, …) and decides.
  useEffect(() => {
    dispatch({ type: "checked" });
  }, [dispatch, threads, threadsLoaded]);

  return { demoMode };
}

export default function App() {
  const refreshThreads = useWorkspaceStore((s) => s.refreshThreads);
  // T10 (ADR 0033): the status fields are boot-scoped, so the
  // left-corner line fetches on open rather than polling.
  const loadStatus = useWorkspaceStore((s) => s.loadStatus);

  // The one-shot startup restore belongs to the application lifetime,
  // not to the homepage mount. The demo mode is a THREE-state fact: the
  // server's answer (enabled / disabled) and its silence (unavailable)
  // are different facts, and the startup decision must tell them apart.
  // The chrome still shows no demo context unless the server CONFIRMED
  // demo mode, so "unavailable" and "disabled" look the same to the
  // chrome and not the same to the startup decision.
  const { demoMode } = useStartupRestore();
  const demoModeEnabled = demoMode === "enabled";
  const demoModeStatusLoaded = demoMode !== "pending";

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
        <DemoModeSurface enabled={demoModeEnabled}>
          <Routes>
            <Route path={DEMO_SOURCE_ROUTE_PATH} element={<DemoSourcePage />} />
            <Route
              path={HOME_ROUTE_PATH}
              element={
                <HomePage
                  demoModeEnabled={demoModeEnabled}
                  demoModeStatusLoaded={demoModeStatusLoaded}
                />
              }
            />
            <Route path={THREAD_ROUTE_PATH} element={<ThreadRoute />} />
            {/* No-match: the retired ``/kb`` / ``/monitor`` deep links
                (and any other unknown path) render a 404. Deliberately
                NOT a redirect home — a silent bounce would hide the
                retired route (spec SC7). */}
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </DemoModeSurface>
      </div>
    </div>
  );
}
