// GE-4: the ANSWER TURN the reply door RESUMES must reach the bubble.
//
// A grounded document-choice pick posted to the SEND door
// (``POST /api/threads/{id}/messages`` with ``{question:"", question_id,
// choice_id}``) continues the PAUSED run: the host streams the resumed
// remainder back on the same response and persists the row under the
// paused run's ORIGIN run id. The paused run is not a persisted
// RunRecord, so a reloaded thread has no turn row for it —
// ``reloadTaskProjection`` alone can never paint it (its contract is to
// never repaint the turn table), which is why the answered bubble used
// to appear only after a manual page reload.
//
// These pin the store half of the fix: the reply door folds the resumed
// frames onto a row keyed by the origin run id, materializing that row
// when a reload left none, so the answer arrives in ``activeThread.turns``
// WITHOUT a reload.

import { afterEach, describe, expect, it, vi } from "vitest";
import { useWorkspaceStore } from "./store";
import { EMPTY_WORKSPACE_OPERATIONS } from "./workspaceOperation";
import type { PendingQuestionView, TaskRuntimeView, ThreadDetail } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const ORIGIN_RUN_ID = "77777777-7777-4777-8777-777777777777";
const QUESTION_ID = "q-doc";
const ORIGINAL_QUESTION = "对比一下这两个季度的营收";
const RESUMED_ANSWER = "选定来源后：AAPL 2023 Q3 营收为 89,493 百万美元。";

/** The authoritative projection: a pending ``document_choice`` naming the
 * run it interrupted, plus the accepted original message under that SAME
 * run id (the row the recovered question reads). */
const PENDING_QUESTION: PendingQuestionView = {
  question_id: QUESTION_ID,
  task_id: "",
  task_version: 1,
  prompt: "要用哪一份资料对比？",
  choices: ["choice-1", "choice-2", "other"],
  input_version: 1,
  purpose: "document_choice",
  origin_run_id: ORIGIN_RUN_ID,
  choice_details: [
    {
      choice_id: "choice-1",
      label: "AAPL 10-Q FY2023Q3",
      summary: "第三季度营收概述。",
      source_ref: {
        source_kind: "corpus",
        source_id: "AAPL-10Q-2023Q3",
        version: "v1",
        section: null,
      },
    },
    {
      choice_id: "choice-2",
      label: "AAPL 10-Q FY2022Q3",
      summary: "去年同期营收概述。",
      source_ref: {
        source_kind: "corpus",
        source_id: "AAPL-10Q-2022Q3",
        version: "v1",
        section: null,
      },
    },
  ],
};

function projection(): TaskRuntimeView {
  return {
    current_task: null,
    archived_tasks: [],
    pending_question: PENDING_QUESTION,
    reconciliation: null,
    task_outcome: null,
    input_version: 1,
    input_history: [
      {
        input_version: 1,
        kind: "message",
        payload: ORIGINAL_QUESTION,
        provenance: "workspace_send",
        question_id: null,
        archive_id: null,
        choice_id: null,
        run_id: ORIGIN_RUN_ID,
      },
    ],
  };
}

type Frame = [string, unknown];

function sseFrame([event, data]: Frame): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Stub the wire for a document-choice pick: the POST answers the resumed
 * remainder's frame stream; the thread-detail GET answers ``view`` with an
 * EMPTY ``turns[]`` — so any row that appears in ``activeThread.turns`` can
 * only have been painted by the folded stream, never by the projection
 * re-read. Records each POST body. */
function stubReplyWire(
  frames: Frame[],
  view: TaskRuntimeView,
  detailTurns: ThreadDetail["turns"] = [],
): { bodies: unknown[]; urls: string[] } {
  const bodies: unknown[] = [];
  const urls: string[] = [];
  const detail: Record<string, unknown> = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: detailTurns,
    task_runtime: view,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      if (init?.method === "POST") {
        urls.push(url);
        bodies.push(
          typeof init.body === "string" ? JSON.parse(init.body) : null,
        );
        let next = 0;
        const body = new ReadableStream<Uint8Array>({
          pull(controller) {
            if (next >= frames.length) {
              controller.close();
              return;
            }
            controller.enqueue(
              new TextEncoder().encode(sseFrame(frames[next])),
            );
            next += 1;
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      }
      if (url.includes(`/threads/${THREAD_ID}`)) {
        return new Response(JSON.stringify(detail), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("[]", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return { bodies, urls };
}

function seedDocumentChoiceThread(): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
    taskRuntime: { [THREAD_ID]: projection() },
    clarificationDrafts: {},
    workspaceOperations: EMPTY_WORKSPACE_OPERATIONS,
    restoreRequests: {},
    taskProjectionRefusal: {},
    taskProjectionRefreshFailed: {},
  });
}

function pickFirstCandidate(): void {
  const store = useWorkspaceStore.getState();
  store.openTaskClarification(THREAD_ID);
  store.selectTaskChoice(THREAD_ID, "choice-1");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Stub the wire for the WHOLE live lane: the send's POST answers the
 * pausing ``document_choice`` frame; the reply's POST answers the resumed
 * remainder; the detail GET answers ``view`` with empty ``turns[]``. The
 * two POST streams are consumed in order. */
function stubLiveLaneWire(
  pauseFrames: Frame[],
  resumeFrames: Frame[],
  view: TaskRuntimeView,
): void {
  const queue = [pauseFrames, resumeFrames];
  const detail: Record<string, unknown> = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
    task_runtime: view,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      if (init?.method === "POST") {
        const frames = queue.shift() ?? [];
        let next = 0;
        const body = new ReadableStream<Uint8Array>({
          pull(controller) {
            if (next >= frames.length) {
              controller.close();
              return;
            }
            controller.enqueue(
              new TextEncoder().encode(sseFrame(frames[next])),
            );
            next += 1;
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      }
      if (url.includes(`/threads/${THREAD_ID}`)) {
        return new Response(JSON.stringify(detail), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("[]", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

const DOCUMENT_CHOICE_FRAME: Frame = [
  "document_choice",
  {
    kind: "document_choice",
    question_id: QUESTION_ID,
    prompt: "要用哪一份资料对比？",
    choices: ["choice-1", "choice-2", "other"],
    origin_run_id: ORIGIN_RUN_ID,
    other_label: "换个问法",
  },
];

describe("reply door resumes a document choice into the bubble", () => {
  it("keeps ONE row when the pick follows the live pause with no reload", async () => {
    // The live lane: the send that paused left an optimistic row under a
    // client placeholder id. The ``document_choice`` frame names the run it
    // interrupted, so the row is reconciled onto that ORIGIN id — the reply
    // then folds into it. Without the reconcile the reply would materialize
    // a SECOND row and orphan the placeholder one in_flight forever.
    seedDocumentChoiceThread();
    useWorkspaceStore.setState({ taskRuntime: {} });
    stubLiveLaneWire(
      [DOCUMENT_CHOICE_FRAME],
      [
        [
          "status",
          { run_id: ORIGIN_RUN_ID, status: "done", assistant_message: "" },
        ],
        ["answer", { text: RESUMED_ANSWER, reset: true }],
        ["citations", []],
      ],
      projection(),
    );

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, ORIGINAL_QUESTION);
    const afterPause = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(afterPause).toHaveLength(1);
    expect(afterPause[0].run_id).toBe(ORIGIN_RUN_ID);
    expect(afterPause[0].in_flight).toBe(true);

    pickFirstCandidate();
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].run_id).toBe(ORIGIN_RUN_ID);
    expect(turns[0].answer).toBe(RESUMED_ANSWER);
    expect(turns[0].in_flight).toBe(false);
  });

  it("folds the resumed answer onto a row keyed by the origin run id, with no reload", async () => {
    seedDocumentChoiceThread();
    pickFirstCandidate();
    const { bodies } = stubReplyWire(
      [
        [
          "status",
          { run_id: ORIGIN_RUN_ID, status: "done", assistant_message: "" },
        ],
        ["answer", { text: RESUMED_ANSWER, reset: true }],
        ["citations", []],
      ],
      projection(),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    // The pick went out on the reply body the host validates.
    expect(bodies[0]).toMatchObject({
      question: "",
      question_id: QUESTION_ID,
      choice_id: "choice-1",
    });

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    const row = turns.find((t) => t.run_id === ORIGIN_RUN_ID);
    expect(row).toBeDefined();
    expect(row?.answer).toBe(RESUMED_ANSWER);
    expect(row?.in_flight).toBe(false);
    // The recovered prompt is the OPERATOR'S original question, read from
    // the accepted-input history under the run the reply continues — not
    // the host's question wording.
    expect(row?.question).toBe(ORIGINAL_QUESTION);
  });

  it("sends the pick on the messages door, not the graph resume route", async () => {
    seedDocumentChoiceThread();
    pickFirstCandidate();
    const { urls } = stubReplyWire(
      [
        [
          "status",
          { run_id: ORIGIN_RUN_ID, status: "done", assistant_message: "" },
        ],
      ],
      projection(),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(urls).toHaveLength(1);
    expect(urls[0]).toContain(`/threads/${THREAD_ID}/messages`);
    expect(urls.some((url) => url.includes("/resume"))).toBe(false);
  });

  it("materializes no second row when the paused row already carries the origin run id", async () => {
    // The live (no-reload) path: the send that paused already put a row on
    // the thread, reconciled onto the origin run id by the
    // ``document_choice`` frame. The reply must fold into THAT row rather
    // than materializing a duplicate.
    seedDocumentChoiceThread();
    useWorkspaceStore.setState((s) => ({
      activeThread: {
        ...(s.activeThread as ThreadDetail),
        turns: [
          {
            run_id: ORIGIN_RUN_ID,
            question: ORIGINAL_QUESTION,
            status: "done",
            answer: "",
            citations: [],
            thinking_text: "",
            tool_summaries: [],
            assistant_message: "",
            in_flight: true,
            created_at: "2026-01-01T00:00:00.000Z",
          },
        ],
      },
    }));
    pickFirstCandidate();
    stubReplyWire(
      [
        [
          "status",
          { run_id: ORIGIN_RUN_ID, status: "done", assistant_message: "" },
        ],
        ["answer", { text: RESUMED_ANSWER, reset: true }],
        ["citations", []],
      ],
      projection(),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].run_id).toBe(ORIGIN_RUN_ID);
    expect(turns[0].answer).toBe(RESUMED_ANSWER);
  });

  it("leaves the row suspended (not done) when the resumed stream re-opens the question", async () => {
    // An invalid / stale pick the host did not pre-refuse re-raises the
    // interrupt: the resumed stream ends on a fresh ``document_choice``
    // frame with NO terminal status, so the row must stay live.
    seedDocumentChoiceThread();
    pickFirstCandidate();
    stubReplyWire(
      [
        [
          "document_choice",
          {
            kind: "document_choice",
            question_id: QUESTION_ID,
            prompt: "要用哪一份资料对比？",
            choices: ["choice-1", "choice-2", "other"],
            origin_run_id: ORIGIN_RUN_ID,
            other_label: "换个问法",
          },
        ],
      ],
      projection(),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    const row = turns.find((t) => t.run_id === ORIGIN_RUN_ID);
    expect(row).toBeDefined();
    expect(row?.in_flight).toBe(true);
  });
});

describe("a resumed document-choice reply that fails leaves no ghost", () => {
  it("drops the materialized row when the resumed stream refuses mid-flight", async () => {
    seedDocumentChoiceThread();
    pickFirstCandidate();
    stubReplyWire(
      [
        [
          "status",
          {
            run_id: ORIGIN_RUN_ID,
            status: "refuse",
            reason: "task_reply_expired",
            assistant_message: "该问题已过期。",
          },
        ],
        ["answer", { text: "" }],
        ["citations", []],
      ],
      projection(),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns.find((t) => t.run_id === ORIGIN_RUN_ID)).toBeUndefined();
  });
});
