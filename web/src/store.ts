// Zustand store for the workspace UI. T1 keeps the state minimal:
// thread list + corpus + the active thread's turns. Per-thread run
// bookkeeping (in-flight flags, streaming deltas) is T3.

import { create } from "zustand";
import * as api from "./api";
// T5 (#35): the last opened thread id, persisted so a returning
// visit lands back on that thread.
import { forgetLastThreadIf, rememberLastThread } from "./lastThread";
import type {
  Filing,
  ThreadDetail,
  ThreadListItem,
  ToolSummary,
  Turn,
  WorkspaceStatus,
} from "./types";
import {
  EMPTY_LIVE_ANSWER,
  foldLiveAnswer,
  mergeReconciledTurn,
  type LiveAnswerState,
} from "./turnStream";

interface WorkspaceState {
  threads: ThreadListItem[];
  corpus: Filing[];
  activeThread: ThreadDetail | null;
  loadingThreads: boolean;
  // T5 (#35): true once the FIRST refreshThreads completed. The
  // restore-on-return logic must distinguish "not loaded yet"
  // (initial ``loadingThreads === false`` before App's effect
  // starts the fetch) from "loaded and genuinely empty" — otherwise
  // the one-shot restore treats a valid marker as stale.
  threadsLoaded: boolean;
  corpusLoaded: boolean;
  // T10 (ADR 0033): the Sidebar strip's slice. ``null`` after
  // ``statusLoaded`` means the status route itself did not answer —
  // the strip renders its unavailable copy rather than nothing.
  status: WorkspaceStatus | null;
  statusLoaded: boolean;
  sendingQuestion: boolean;
  // T4: ``resumeThread(threadId)`` sets this while the resume SSE
  // stream is active so the Composer can disable itself and so a
  // second resume click is rejected. ``null`` when no resume is in
  // flight. Tracks the THREAD, not just a boolean, so a resume on
  // thread A does not block a fresh send on thread B.
  resumingThreadId: string | null;

  refreshThreads: () => Promise<void>;
  loadCorpus: () => Promise<void>;
  // Fetched once when the workspace opens. Boot-scoped fields do not
  // change mid-session, so there is no poll.
  loadStatus: () => Promise<void>;
  openThread: (threadId: string) => Promise<void>;
  createThread: (firstMessage: string | null) => Promise<string>;
  removeThread: (threadId: string) => Promise<void>;
  sendQuestion: (
    threadId: string,
    question: string,
    thinkingOn?: boolean,
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
  threadsLoaded: false,
  corpusLoaded: false,
  status: null,
  statusLoaded: false,
  sendingQuestion: false,
  resumingThreadId: null,

  async refreshThreads() {
    set({ loadingThreads: true });
    try {
      const threads = await api.listThreads();
      set({ threads });
    } finally {
      set({ loadingThreads: false, threadsLoaded: true });
    }
  },

  async loadCorpus() {
    try {
      const corpus = await api.getCorpus();
      set({ corpus, corpusLoaded: true });
    } catch {
      set({ corpus: [], corpusLoaded: true });
    }
  },

  async loadStatus() {
    // A failed or malformed status read is a strip state, not an
    // error the operator has to act on: the workspace keeps working
    // without it. ``statusLoaded`` still flips so the strip stops
    // showing "读取中" and shows its unavailable copy instead.
    try {
      set({ status: await api.getStatus(), statusLoaded: true });
    } catch {
      set({ status: null, statusLoaded: true });
    }
  },

  async openThread(threadId: string) {
    // Unknown id -> 404 -> surface as empty active thread so the
    // route can render its not-found copy. We do NOT re-invoke the
    // graph on reload; GET only.
    const detail = await api.getThread(threadId);
    // T5 (#35): a successfully opened thread becomes the one a
    // returning visit restores.
    rememberLastThread(threadId);
    // T4 (plans/workspace-stream-fidelity.md): reconcile, do not
    // replace. Replacing the turn table retracted prose the operator
    // had already watched arrive token-by-token and repainted the
    // persisted text over it.
    set((s) => ({ activeThread: reconcileTurnTable(s.activeThread, detail) }));
  },

  async createThread(firstMessage: string | null) {
    const created = await api.createThread(firstMessage);
    set((s) => ({ threads: [created, ...s.threads] }));
    return created.id;
  },

  async removeThread(threadId: string) {
    await api.deleteThread(threadId);
    // T5 (#35): a deleted thread must never be restored on the next
    // visit — drop the marker when it pointed here.
    forgetLastThreadIf(threadId);
    set((s) => ({
      threads: s.threads.filter((t) => t.id !== threadId),
      activeThread:
        s.activeThread && s.activeThread.id === threadId
          ? null
          : s.activeThread,
    }));
  },

  async sendQuestion(threadId, question, thinkingOn = true) {
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
      thinking_duration_ms: 0,
      tool_summaries: [],
      assistant_message: "",
      // T4 (#37): the optimistic turn is LIVE until the terminal
      // status frame — the thinking region line-clamps and the tool
      // rows stay open while the run streams, then collapse.
      in_flight: true,
      created_at: new Date().toISOString(),
    };

    // Helpers bound to the latest state so ``onEvent`` callbacks
    // always patch the most-recent snapshot of the optimistic
    // turn (the array reference is stable across renders).
    const patchTurnByRunId = (
      runId: Turn["run_id"],
      patcher: (t: Turn) => Turn,
    ) => {
      const current = get().activeThread;
      if (!current || current.id !== threadId) return;
      const nextTurns = current.turns.map((t) =>
        t.run_id === runId ? patcher(t) : t,
      );
      set({ activeThread: { ...current, turns: nextTurns } });
    };
    const patchTurn = (patcher: (t: Turn) => Turn) =>
      patchTurnByRunId(optimisticRunId, patcher);

    const initial = get().activeThread;
    if (initial && initial.id === threadId) {
      set({
        activeThread: { ...initial, turns: [...initial.turns, baseTurn] },
      });
    }

    // T1 (#33): answer frames are DELTAS while the model streams; after
    // ``status`` the wire may emit one authoritative frame — on ``done``
    // runs we keep a non-empty draft; on refuse/step_cap an empty frame
    // clears the draft so SC67 copy renders from ``assistant_message``.
    // The fold state lives here rather than on the turn row because a
    // bounce retry accumulates behind the visible draft.
    let statusSeen = false;
    let answerFold: LiveAnswerState = EMPTY_LIVE_ANSWER;
    try {
      const result = await api.sendMessage(
        threadId,
        { question, thinking: thinkingOn },
        (event) => {
          switch (event.type) {
            case "thinking":
              patchTurn((t) => ({
                ...t,
                thinking_text: t.thinking_text + event.text,
              }));
              break;
            case "tool_call": {
              // Append a placeholder row so the operator sees a
              // running indicator while the tool executes. The
              // matching ``tool_result`` replaces it with the
              // server-formatted one-liner.
              const placeholder: ToolSummary = {
                name: event.name,
                summary: "运行中…",
                tool_call_id: event.tool_call_id,
              };
              patchTurn((t) => ({
                ...t,
                tool_summaries: [...(t.tool_summaries ?? []), placeholder],
              }));
              break;
            }
            case "tool_result": {
              // The server already formatted the summary (spec line
              // 32); replace the "running…" placeholder with it.
              patchTurn((t) => ({
                ...t,
                tool_summaries: (t.tool_summaries ?? []).map((s) =>
                  s.tool_call_id === event.tool_call_id
                    ? { ...s, summary: event.summary }
                    : s,
                ),
              }));
              break;
            }
            case "status": {
              statusSeen = true;
              // Patch by the CLIENT placeholder id first — updating
              // ``optimisticRunId`` before the patch would orphan the
              // optimistic row and freeze the live view until GET.
              const priorRunId = optimisticRunId;
              patchTurnByRunId(priorRunId, (t) => ({
                ...t,
                run_id: event.run_id,
                status: event.status,
                status_reason: event.reason ?? "",
                assistant_message: event.assistant_message,
                // Soft-Track: optional until host SSE includes track.
                track: event.track ?? t.track,
                track_decision: event.track_decision ?? t.track_decision,
                // T4 (#37): the run ended — thinking collapses to the
                // duration label, tool rows collapse.
                in_flight: false,
                thinking_duration_ms:
                  event.thinking_ms ?? t.thinking_duration_ms,
              }));
              optimisticRunId = event.run_id;
              break;
            }
            case "answer":
              answerFold = foldLiveAnswer(answerFold, {
                text: event.text,
                reset: event.reset,
                statusSeen,
              });
              patchTurn((t) => ({ ...t, answer: answerFold.visible }));
              break;
            case "citations":
              patchTurn((t) => ({ ...t, citations: event.citations }));
              break;
          }
        },
      );
      // The SSE stream already delivered terminal status, answer, and
      // citations. Re-GET'ing here used to replace the live turn row
      // and caused a visible retract/repop even when merge logic ran
      // immediately after — keep the streamed row; refresh the sidebar
      // list only. Reload still uses GET via ThreadPage's effect.
      const cur = get().activeThread;
      if (cur?.id === threadId) {
        patchTurn((t) => ({
          ...t,
          in_flight: false,
          citations:
            result.citations.length > 0 ? result.citations : t.citations,
          track: result.track ?? t.track,
          track_decision: result.track_decision ?? t.track_decision,
        }));
      }
      await get().refreshThreads();
      return result;
    } catch (error) {
      // The send failed (409 concurrent_run / 503 ingest_missing /
      // transport). Before this the rejection escaped into a
      // floating promise and the optimistic row stayed ``in_flight``
      // forever — a ghost turn with no question answered. Drop the
      // row when no frame ever landed on it; keep (but un-live) a
      // row the stream had already started painting, then rethrow so
      // the composer can show the failure.
      if (statusSeen) {
        patchTurn((t) => ({ ...t, in_flight: false }));
      } else {
        dropTurnByRunId(threadId, optimisticRunId);
      }
      throw error;
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
    // Resume button can render. Cleared by the status frame or
    // finalize patch below (no re-GET over the streamed row).
    const before = get().activeThread;
    if (before && before.id === threadId && before.turns.length > 0) {
      const last = before.turns.length - 1;
      const nextTurns = before.turns.map((t, i) =>
        i === last ? { ...t, in_flight: true } : t,
      );
      set({
        activeThread: { ...before, in_flight: true, turns: nextTurns },
      });
    }

    try {
      // Same delta semantics as sendQuestion (T1 #33): append while
      // streaming; post-status empty frame clears refuse/step_cap draft.
      // The fold starts from the prose already painted on the resumed
      // row so the first delta extends it instead of replacing it.
      let statusSeen = false;
      let answerFold: LiveAnswerState = {
        ...EMPTY_LIVE_ANSWER,
        visible: latestTurnAnswer(threadId),
      };
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
              thinking_text: t.thinking_text + event.text,
            }));
            break;
          case "tool_call": {
            const placeholder: ToolSummary = {
              name: event.name,
              summary: "运行中…",
              tool_call_id: event.tool_call_id,
            };
            patchLatestTurn(threadId, (t) => ({
              ...t,
              tool_summaries: [...(t.tool_summaries ?? []), placeholder],
            }));
            break;
          }
          case "tool_result": {
            patchLatestTurn(threadId, (t) => ({
              ...t,
              tool_summaries: (t.tool_summaries ?? []).map((s) =>
                s.tool_call_id === event.tool_call_id
                  ? { ...s, summary: event.summary }
                  : s,
              ),
            }));
            break;
          }
          case "status":
            statusSeen = true;
            patchLatestTurn(threadId, (t) => ({
              ...t,
              status: event.status,
              status_reason: event.reason ?? "",
              assistant_message: event.assistant_message,
              track: event.track ?? t.track,
              track_decision: event.track_decision ?? t.track_decision,
              in_flight: false,
              thinking_duration_ms: event.thinking_ms ?? t.thinking_duration_ms,
            }));
            break;
          case "answer":
            answerFold = foldLiveAnswer(answerFold, {
              text: event.text,
              reset: event.reset,
              statusSeen,
            });
            patchLatestTurn(threadId, (t) => ({
              ...t,
              answer: answerFold.visible,
            }));
            break;
          case "citations":
            patchLatestTurn(threadId, (t) => ({
              ...t,
              citations: event.citations,
            }));
            break;
        }
      });

      // Same as sendQuestion: do not re-GET over the streamed row.
      const cur = get().activeThread;
      if (cur?.id === threadId) {
        patchLatestTurn(threadId, (t) => ({
          ...t,
          in_flight: false,
          citations:
            result.citations.length > 0 ? result.citations : t.citations,
          track: result.track ?? t.track,
          track_decision: result.track_decision ?? t.track_decision,
        }));
      }
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

// T4 GET reconcile (plans/workspace-stream-fidelity.md): fold the
// persisted turn table into the one on screen. The server row stays
// authoritative for ids, status, citation quotes and thinking
// duration; ``mergeReconciledTurn`` keeps the prose (and trace) the
// operator already watched stream in so the bubble never retracts and
// repaints. Rows this session never streamed come straight from the
// server. The still-running row is the one exception the server cannot
// send: a RunRecord is appended only once its stream completes, so a
// mid-run reopen carries the live row over — dropping it would blank
// the bubble AND orphan the patches the running stream still makes.
function reconcileTurnTable(
  streamed: ThreadDetail | null,
  persisted: ThreadDetail,
): ThreadDetail {
  if (!streamed || streamed.id !== persisted.id) return persisted;
  const streamedByRunId = new Map<string, Turn>(
    streamed.turns.map((t) => [t.run_id, t]),
  );
  const persistedRunIds = new Set(persisted.turns.map((t) => t.run_id));
  const reconciled = persisted.turns.map((row) => {
    const live = streamedByRunId.get(row.run_id);
    return live ? mergeReconciledTurn(live, row) : row;
  });
  const stillRunning = streamed.turns.filter(
    (t) => t.in_flight === true && !persistedRunIds.has(t.run_id),
  );
  return { ...persisted, turns: [...reconciled, ...stillRunning] };
}

// Remove an optimistic turn that never received a single SSE frame.
// No-op when the operator has navigated away, so a failed send on
// thread A cannot delete a row on thread B.
function dropTurnByRunId(threadId: string, runId: Turn["run_id"]): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return;
  useWorkspaceStore.setState({
    activeThread: {
      ...current,
      turns: current.turns.filter((t) => t.run_id !== runId),
    },
  });
}

// Prose already on the resumed thread's most-recent turn — the live
// fold's starting point. Empty when the operator has navigated away
// or the thread has no turns yet.
function latestTurnAnswer(threadId: string): string {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return "";
  if (current.turns.length === 0) return "";
  return current.turns[current.turns.length - 1].answer;
}

// T4 helper: patch the most-recent turn on ``threadId`` (the one
// the resume is working on). No-op when the operator has navigated
// away so the resume cannot paint onto the wrong thread.
function patchLatestTurn(threadId: string, patcher: (t: Turn) => Turn): void {
  const current = useWorkspaceStore.getState().activeThread;
  if (!current || current.id !== threadId) return;
  if (current.turns.length === 0) return;
  const idx = current.turns.length - 1;
  const nextTurns = current.turns.map((t, i) => (i === idx ? patcher(t) : t));
  useWorkspaceStore.setState({
    activeThread: { ...current, turns: nextTurns },
  });
}
