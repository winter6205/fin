// Zustand store for the workspace UI. T1 keeps the state minimal:
// thread list + corpus + the active thread's turns. Per-thread run
// bookkeeping (in-flight flags, streaming deltas) is T3.

import { create } from "zustand";
import * as api from "./api";
import type {
  Filing,
  ThreadDetail,
  ThreadListItem,
  ToolSummary,
  Turn,
} from "./types";

interface WorkspaceState {
  threads: ThreadListItem[];
  corpus: Filing[];
  activeThread: ThreadDetail | null;
  loadingThreads: boolean;
  sendingQuestion: boolean;
  // T4: ``resumeThread(threadId)`` sets this while the resume SSE
  // stream is active so the Composer can disable itself and so a
  // second resume click is rejected. ``null`` when no resume is in
  // flight. Tracks the THREAD, not just a boolean, so a resume on
  // thread A does not block a fresh send on thread B.
  resumingThreadId: string | null;

  refreshThreads: () => Promise<void>;
  loadCorpus: () => Promise<void>;
  openThread: (threadId: string) => Promise<void>;
  createThread: (firstMessage: string | null) => Promise<string>;
  removeThread: (threadId: string) => Promise<void>;
  sendQuestion: (
    threadId: string,
    question: string,
    thinkingOn?: boolean
  ) => Promise<api.SendResult | null>;
  // T4 (spec line 39): resume an interrupted run from the last
  // complete node. Mirrors ``sendQuestion``'s streaming-event
  // contract but does NOT append a new HumanMessage or an
  // optimistic turn — it picks up the existing in-flight turn row
  // the lock probe surfaced. The switch-thread guard (resume only
  // paints if the operator is still on this thread) is preserved
  // by re-checking ``get().activeThread?.id`` against ``threadId``
  // on every patch.
  resumeThread: (threadId: string) => Promise<api.SendResult | null>;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  threads: [],
  corpus: [],
  activeThread: null,
  loadingThreads: false,
  sendingQuestion: false,
  resumingThreadId: null,

  async refreshThreads() {
    set({ loadingThreads: true });
    try {
      const threads = await api.listThreads();
      set({ threads });
    } finally {
      set({ loadingThreads: false });
    }
  },

  async loadCorpus() {
    const corpus = await api.getCorpus();
    set({ corpus });
  },

  async openThread(threadId: string) {
    // Unknown id -> 404 -> surface as empty active thread so the
    // route can render its not-found copy. We do NOT re-invoke the
    // graph on reload; GET only.
    const detail = await api.getThread(threadId);
    set({ activeThread: detail });
  },

  async createThread(firstMessage: string | null) {
    const created = await api.createThread(firstMessage);
    set((s) => ({ threads: [created, ...s.threads] }));
    return created.id;
  },

  async removeThread(threadId: string) {
    await api.deleteThread(threadId);
    set((s) => ({
      threads: s.threads.filter((t) => t.id !== threadId),
      activeThread:
        s.activeThread && s.activeThread.id === threadId
          ? null
          : s.activeThread,
    }));
  },

  async sendQuestion(threadId, question, thinkingOn = false) {
    if (!question.trim()) return null; // Empty submit is a no-op.
    if (get().sendingQuestion) return null; // No queue in T1.
    set({ sendingQuestion: true });

    // T3: optimistic turn the SSE frames thread into. Allocated
    // up front so the api client can ``onEvent`` updates against
    // the SAME turn (matched by ``run_id``). On the first
    // ``status`` frame we reconcile the run_id with the
    // server-minted one and patch the turn.
    // ``run_id`` is typed as the UUID template literal in types.ts;
    // crypto.randomUUID() already satisfies it. The template-literal
    // type is what makes the reconcile-on-status patch typecheck.
    let optimisticRunId: Turn["run_id"] = crypto.randomUUID();
    const baseTurn: Turn = {
      run_id: optimisticRunId,
      question,
      status: "done",
      status_reason: "",
      answer: "",
      citations: [],
      thinking_text: "",
      tool_summaries: [],
      assistant_message: "",
      created_at: new Date().toISOString(),
    };

    // Helpers bound to the latest state so ``onEvent`` callbacks
    // always patch the most-recent snapshot of the optimistic
    // turn (the array reference is stable across renders).
    const patchTurn = (patcher: (t: Turn) => Turn) => {
      const current = get().activeThread;
      if (!current || current.id !== threadId) return;
      const nextTurns = current.turns.map((t) =>
        t.run_id === optimisticRunId ? patcher(t) : t
      );
      set({ activeThread: { ...current, turns: nextTurns } });
    };

    const initial = get().activeThread;
    if (initial && initial.id === threadId) {
      set({
        activeThread: { ...initial, turns: [...initial.turns, baseTurn] },
      });
    }

    try {
      const result = await api.sendMessage(
        threadId,
        { question, thinking: thinkingOn },
        (event) => {
        switch (event.type) {
          case "thinking":
            patchTurn((t) => ({
              ...t,
              thinking_text: t.thinking_text
                ? `${t.thinking_text}\n${event.text}`
                : event.text,
            }));
            break;
          case "tool_call": {
            // Append a placeholder row so the operator sees a
            // running indicator while the tool executes. The
            // matching ``tool_result`` either fills the summary
            // (when the tool returns a structured payload) or
            // leaves it as a "running" placeholder.
            const placeholder: ToolSummary = {
              name: event.name,
              summary: "running…",
              tool_call_id: event.tool_call_id,
            };
            patchTurn((t) => ({
              ...t,
              tool_summaries: [...(t.tool_summaries ?? []), placeholder],
            }));
            break;
          }
          case "tool_result": {
            // Replace the matching "running…" placeholder with
            // the formatted one-liner. The server already
            // formatted it (spec line 32) so the SPA does not
            // duplicate the formatter.
            const formatted = formatToolResultSummary(
              event.name,
              event.result,
            );
            patchTurn((t) => ({
              ...t,
              tool_summaries: (t.tool_summaries ?? []).map((s) =>
                s.tool_call_id === event.tool_call_id
                  ? { ...s, summary: formatted }
                  : s,
              ),
            }));
            break;
          }
          case "status":
            // Server-minted run id wins over the local placeholder.
            optimisticRunId = event.run_id;
            patchTurn((t) => ({
              ...t,
              run_id: event.run_id,
              status: event.status,
              status_reason: event.reason ?? "",
              assistant_message: event.assistant_message,
            }));
            break;
          case "answer":
            patchTurn((t) => ({ ...t, answer: event.text }));
            break;
          case "citations":
            patchTurn((t) => ({ ...t, citations: event.citations }));
            break;
        }
        },
      );
      // Reconcile by re-GET'ing the thread: the SSE body carries
      // the terminal events but the persisted RunRecord id and
      // any field the streaming payload omits (e.g. citation
      // ``quote`` strings) only surface on a follow-up GET.
      // ``openThread`` REPLACES the optimistic turn with the
      // server-persisted row so the ``run_id`` lines up with
      // what ``GET /api/threads/{id}`` returns.
      await get().openThread(threadId);
      await get().refreshThreads();
      return result;
    } finally {
      set({ sendingQuestion: false });
    }
  },

  async resumeThread(threadId) {
    // Guard: no double-resume on the same thread. Sending on a
    // different thread is fine — it is a different lock. Empty /
    // completed threads resolve to 204 from the server; we still
    // re-GET to keep the turn table honest.
    if (get().resumingThreadId === threadId) return null;
    set({ resumingThreadId: threadId });

    // Optimistic local patch: while the resume is streaming, mark
    // the most-recent persisted turn as in_flight so the banner +
    // Resume button can render. The server is the source of truth
    // — the openThread() reconcile at the end overwrites this.
    const before = get().activeThread;
    if (before && before.id === threadId && before.turns.length > 0) {
      const last = before.turns.length - 1;
      const nextTurns = before.turns.map((t, i) =>
        i === last ? { ...t, in_flight: true } : t
      );
      set({
        activeThread: { ...before, in_flight: true, turns: nextTurns },
      });
    }

    try {
      const result = await api.resumeThread(threadId, (event) => {
        // Switch-thread guard: if the operator navigated away
        // mid-resume, drop the patches rather than painting them
        // onto a different thread. Same guard as sendQuestion's
        // patchTurn.
        const cur = get().activeThread;
        if (!cur || cur.id !== threadId) return;

        switch (event.type) {
          case "thinking":
            patchLatestTurn(threadId, (t) => ({
              ...t,
              thinking_text: t.thinking_text
                ? `${t.thinking_text}\n${event.text}`
                : event.text,
            }));
            break;
          case "tool_call": {
            const placeholder: ToolSummary = {
              name: event.name,
              summary: "running…",
              tool_call_id: event.tool_call_id,
            };
            patchLatestTurn(threadId, (t) => ({
              ...t,
              tool_summaries: [...(t.tool_summaries ?? []), placeholder],
            }));
            break;
          }
          case "tool_result": {
            const formatted = formatToolResultSummary(
              event.name,
              event.result,
            );
            patchLatestTurn(threadId, (t) => ({
              ...t,
              tool_summaries: (t.tool_summaries ?? []).map((s) =>
                s.tool_call_id === event.tool_call_id
                  ? { ...s, summary: formatted }
                  : s
              ),
            }));
            break;
          }
          case "status":
            patchLatestTurn(threadId, (t) => ({
              ...t,
              status: event.status,
              status_reason: event.reason ?? "",
              assistant_message: event.assistant_message,
              in_flight: false,
            }));
            break;
          case "answer":
            patchLatestTurn(threadId, (t) => ({ ...t, answer: event.text }));
            break;
          case "citations":
            patchLatestTurn(threadId, (t) => ({
              ...t,
              citations: event.citations,
            }));
            break;
        }
      });

      // Reconcile with persisted state. Same shape as
      // sendQuestion's tail — openThread() REPLACES the optimistic
      // patches with the server-persisted row.
      await get().openThread(threadId);
      await get().refreshThreads();
      return result;
    } finally {
      // Only clear if still pointing at THIS thread — never clobber
      // a freshly-started resume on another thread.
      if (get().resumingThreadId === threadId) {
        set({ resumingThreadId: null });
      }
    }
  },
}));

// T4 helper: patch the most-recent turn on ``threadId`` (the one
// the resume is working on). No-op when the operator has navigated
// away so the resume cannot paint onto the wrong thread.
function patchLatestTurn(
  threadId: string,
  patcher: (t: Turn) => Turn,
): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return;
  if (current.turns.length === 0) return;
  const idx = current.turns.length - 1;
  const nextTurns = current.turns.map((t, i) =>
    i === idx ? patcher(t) : t
  );
  useWorkspaceStore.setState({
    activeThread: { ...current, turns: nextTurns },
  });
}

// Format a tool_result event into the same one-liner the server
// would have produced via ``format_tool_summary`` (defensive: the
// server already shipped ``tool_summaries`` on the persisted row
// after the run, but the LIVE tool_result frame carries the raw
// JSON; this lets the optimistic row flip from "running…" to a
// stable label WITHOUT waiting for the persisted re-GET).
function formatToolResultSummary(
  name: string,
  result: unknown,
): string {
  if (result && typeof result === "object") {
    const r = result as Record<string, unknown>;
    if (name === "list_files") {
      const filings = Array.isArray(r.filings) ? r.filings : [];
      return `${filings.length} filing${filings.length === 1 ? "" : "s"}`;
    }
    if (name === "retrieve") {
      const chunks = Array.isArray(r.chunks) ? r.chunks : [];
      const query = typeof r.query === "string" ? r.query : "";
      return `${query ? `'${query}'` : ""} -> ${chunks.length} hit${
        chunks.length === 1 ? "" : "s"
      }`;
    }
    if (name === "read_file") {
      const doc = typeof r.document_id === "string" ? r.document_id : "";
      const page = typeof r.page === "number" ? r.page : 1;
      return `${doc} p.${page}`;
    }
  }
  if (typeof result === "object" && result !== null) {
    const err = (result as Record<string, unknown>).error;
    if (typeof err === "string") return `error: ${err}`;
  }
  return name || "tool";
}
