// Zustand store for the workspace UI. T1 keeps the state minimal:
// thread list + corpus + the active thread's turns. Per-thread run
// bookkeeping (in-flight flags, streaming deltas) is T3.

import { create } from "zustand";
import * as api from "./api";
import type { Filing, ThreadDetail, ThreadListItem, Turn } from "./types";

interface WorkspaceState {
  threads: ThreadListItem[];
  corpus: Filing[];
  activeThread: ThreadDetail | null;
  loadingThreads: boolean;
  sendingQuestion: boolean;

  refreshThreads: () => Promise<void>;
  loadCorpus: () => Promise<void>;
  openThread: (threadId: string) => Promise<void>;
  createThread: (firstMessage: string | null) => Promise<string>;
  removeThread: (threadId: string) => Promise<void>;
  sendQuestion: (
    threadId: string,
    question: string
  ) => Promise<api.SendResult | null>;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  threads: [],
  corpus: [],
  activeThread: null,
  loadingThreads: false,
  sendingQuestion: false,

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

  async sendQuestion(threadId, question) {
    if (!question.trim()) return null; // Empty submit is a no-op.
    if (get().sendingQuestion) return null; // No queue in T1.
    set({ sendingQuestion: true });
    try {
      const result = await api.sendMessage(threadId, question);
      // Reconcile by re-GET'ing the thread: the SSE body carries
      // the terminal events but NOT the persisted RunRecord id
      // reliably until the status frame. ``openThread`` REPLACES
      // the optimistic turn with the server-persisted row so the
      // ``run_id`` (and any field the T1 payload omits) lines up
      // with what ``GET /api/threads/{id}`` returns. This replaces
      // the previous local ``crypto.randomUUID()`` + ``refreshThreads``
      // (sidebar-only) approach which left the optimistic turn with
      // a different id than the persisted one until reload.
      const current = get().activeThread;
      if (current && current.id === threadId) {
        const optimisticTurn: Turn = {
          // Server-minted id from the ``status`` SSE frame. The
          // api client stamps the SSE frame's run_id onto
          // ``SendResult.run_id`` so this placeholder uses the
          // SAME id the RunRecord was persisted with — no
          // reconciliation gap.
          run_id: result.run_id,
          question,
          status: result.status,
          status_reason: result.reason ?? "",
          answer: result.answerText,
          citations: result.citations,
          thinking_text: "",
          tool_summaries: [],
          created_at: new Date().toISOString(),
        };
        set({
          activeThread: { ...current, turns: [...current.turns, optimisticTurn] },
        });
        await get().openThread(threadId);
      }
      await get().refreshThreads();
      return result;
    } finally {
      set({ sendingQuestion: false });
    }
  },
}));
