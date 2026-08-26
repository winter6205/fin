// SC17: a send that fails BEFORE any SSE frame must reach the
// operator. The store used to append an optimistic turn, let the
// rejection escape, and leave that row ``in_flight`` forever while
// the composer had already cleared the draft.

import { afterEach, describe, expect, it, vi } from "vitest";
import { ConcurrentRunError, IngestMissingError } from "./api";
import { describeSendFailure } from "./sendFailure";
import { useWorkspaceStore } from "./store";
import type { Citation, ThreadDetail, Turn } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";

function seedThread(): void {
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
  });
}

/** A thread whose latest turn is mid-run with prose already painted —
 * the row the resume path picks up. */
function seedResumableThread(): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [
      {
        run_id: "33333333-3333-4333-8333-333333333333",
        question: "营收多少？",
        status: "done",
        answer: "已有草稿。",
        citations: [],
        thinking_text: "",
        tool_summaries: [],
        assistant_message: "",
        in_flight: true,
        created_at: "2026-01-01T00:00:00.000Z",
      },
    ],
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
  });
}

function stubSendStatus(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

/** SSE body that emits a terminal ``status`` frame, then breaks. */
function stubSendStreamThatBreaksAfterStatus(): void {
  const frame =
    `event: status\ndata: ${JSON.stringify({
      run_id: "22222222-2222-4222-8222-222222222222",
      status: "done",
      assistant_message: "",
    })}\n\n`;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      let framesSent = false;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (!framesSent) {
            framesSent = true;
            controller.enqueue(new TextEncoder().encode(frame));
            return;
          }
          controller.error(new Error("stream broke"));
        },
      });
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }),
  );
}

// The ``citations`` frame carries a JSON array, every other frame an
// object — the wire payload is whatever the host serialises.
type Frame = [string, unknown];

function sseFrame([event, data]: Frame): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Stub a send / resume SSE run, one wire frame per stream chunk so
 * the store folds them in the order the operator would see them.
 * Every other route (the tail ``refreshThreads``) answers with an
 * empty thread list. */
function stubSseRun(frames: Frame[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : String(input);
      if (!url.endsWith("/messages") && !url.endsWith("/resume")) {
        return new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      let next = 0;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (next >= frames.length) {
            controller.close();
            return;
          }
          controller.enqueue(new TextEncoder().encode(sseFrame(frames[next])));
          next += 1;
        },
      });
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }),
  );
}

/** Every visible answer the live bubble passed through, one entry per
 * store update — the frames a screenshot could have caught. */
function recordVisibleAnswers(): { seen: string[]; stop: () => void } {
  const seen: string[] = [];
  const stop = useWorkspaceStore.subscribe((state) => {
    const turns = state.activeThread?.turns ?? [];
    if (turns.length > 0) seen.push(turns[turns.length - 1].answer);
  });
  return { seen, stop };
}

function expectNeverShrinks(seen: string[]): void {
  const first = seen.findIndex((v) => v.length > 0);
  expect(first).toBeGreaterThanOrEqual(0);
  for (let i = first + 1; i < seen.length; i += 1) {
    expect(seen[i].length).toBeGreaterThanOrEqual(seen[i - 1].length);
  }
}

const RUN_ID = "22222222-2222-4222-8222-222222222222";
const PRE_BOUNCE = "根据 2024 年报，营收约为 100 亿元。";
const RETRY_HEAD = "根据已核实的 ";
const RETRY_TAIL = "10-K 原文，营收为 391.0 亿美元。";
const RETRY_PROSE = RETRY_HEAD + RETRY_TAIL;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sendQuestion failure surfacing", () => {
  it("rejects with the typed 409 error instead of swallowing it", async () => {
    seedThread();
    stubSendStatus(409, { error: "concurrent_run" });

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "现金流怎么样？"),
    ).rejects.toBeInstanceOf(ConcurrentRunError);
  });

  it("rejects with the typed 503 error instead of swallowing it", async () => {
    seedThread();
    stubSendStatus(503, { error: "ingest_missing" });

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "现金流怎么样？"),
    ).rejects.toBeInstanceOf(IngestMissingError);
  });

  it("leaves no ghost in_flight turn behind on 409", async () => {
    seedThread();
    stubSendStatus(409, { error: "concurrent_run" });

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .catch(() => undefined);

    expect(useWorkspaceStore.getState().activeThread?.turns).toEqual([]);
    expect(useWorkspaceStore.getState().sendingQuestion).toBe(false);
  });

  it("leaves no ghost in_flight turn behind on 503", async () => {
    seedThread();
    stubSendStatus(503, { error: "ingest_missing" });

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .catch(() => undefined);

    expect(useWorkspaceStore.getState().activeThread?.turns).toEqual([]);
  });

  it("hands the composer visible copy for the rejection", async () => {
    seedThread();
    stubSendStatus(409, { error: "concurrent_run" });

    const rejection = await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .then(() => null)
      .catch((error: unknown) => error);

    const notice = describeSendFailure(rejection);
    expect(notice.kind).toBe("concurrent_run");
    expect(notice.message.length).toBeGreaterThan(0);
  });

  it("keeps a partially streamed turn but clears its live flag", async () => {
    seedThread();
    stubSendStreamThatBreaksAfterStatus();

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .catch(() => undefined);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].in_flight).toBe(false);
  });
});

// T3 (plans/workspace-stream-fidelity.md): the host arms a pending
// reset on the hidden bounce marker and carries ``reset: true`` on the
// next non-empty delta. The live bubble must ride that boundary
// without a blank or a shrink frame.
describe("live answer fold across a bounce", () => {
  it("never blanks or shrinks the bubble on bounce-then-retry", async () => {
    seedThread();
    stubSseRun([
      ["answer", { text: PRE_BOUNCE }],
      ["answer", { text: RETRY_HEAD, reset: true }],
      ["answer", { text: RETRY_TAIL }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: RETRY_PROSE }],
      ["citations", []],
    ]);
    const recorder = recordVisibleAnswers();

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    recorder.stop();

    expect(recorder.seen).toContain(PRE_BOUNCE);
    expectNeverShrinks(recorder.seen);
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].answer).toBe(RETRY_PROSE);
    expect(turns[0].status).toBe("done");
  });

  it("clears the stale draft when the bounce ends in a refuse", async () => {
    seedThread();
    const copy = "这次没能给出可核对的答案。";
    stubSseRun([
      ["answer", { text: PRE_BOUNCE }],
      [
        "status",
        {
          run_id: RUN_ID,
          status: "refuse",
          reason: "citation_unverified",
          assistant_message: copy,
        },
      ],
      ["answer", { text: "", reset: true }],
      ["citations", []],
    ]);
    const recorder = recordVisibleAnswers();

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    recorder.stop();

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].answer).toBe("");
    expect(turns[0].assistant_message).toBe(copy);
    expect(turns[0].status).toBe("refuse");
    // The draft stayed on screen right up to the terminal flush.
    expect(recorder.seen).toContain(PRE_BOUNCE);
  });

  it("keeps the streamed draft on a normal done run (that pull request)", async () => {
    seedThread();
    stubSseRun([
      ["answer", { text: "streamed " }],
      ["answer", { text: "draft" }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: "streamed draft" }],
      ["citations", []],
    ]);
    const recorder = recordVisibleAnswers();

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    recorder.stop();

    expectNeverShrinks(recorder.seen);
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].answer).toBe("streamed draft");
    expect(turns[0].run_id).toBe(RUN_ID);
  });

  it("rides the bounce boundary on the resume path too", async () => {
    seedResumableThread();
    stubSseRun([
      ["answer", { text: PRE_BOUNCE }],
      ["answer", { text: RETRY_HEAD, reset: true }],
      ["answer", { text: RETRY_TAIL }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: RETRY_PROSE }],
      ["citations", []],
    ]);
    const recorder = recordVisibleAnswers();

    await useWorkspaceStore.getState().resumeThread(THREAD_ID);
    recorder.stop();

    // The resume fold starts from the prose already on the row, so the
    // pre-bounce draft appends instead of replacing it.
    expect(recorder.seen).toContain("已有草稿。" + PRE_BOUNCE);
    expectNeverShrinks(recorder.seen);
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[turns.length - 1].answer).toBe(RETRY_PROSE);
  });
});

// T4 (plans/workspace-stream-fidelity.md): reopening a thread whose
// reply the operator already watched arrive must not retract the prose
// and repaint it from the persisted row. ``openThread`` reconciles
// through ``mergeReconciledTurn`` instead of replacing the turn table.
const OTHER_THREAD_ID = "44444444-4444-4444-8444-444444444444";
const OPTIMISTIC_RUN_ID = "55555555-5555-4555-8555-555555555555";
const WATCHED_PROSE = "根据 10-K 原文，营收为 391.0 亿美元。";
const PERSISTED_PROSE = "服务器重排后的同一段回答。";
const SERVER_CITATION: Citation = {
  document_id: "0000320193-24-000123",
  version: "v1",
  page: 31,
  quote: "Total net sales were $391.0 billion.",
};

function watchedTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    run_id: RUN_ID,
    question: "营收多少？",
    status: "done",
    status_reason: "",
    answer: WATCHED_PROSE,
    citations: [],
    thinking_text: "已看过的推理草稿",
    thinking_duration_ms: 0,
    tool_summaries: [],
    assistant_message: "",
    in_flight: false,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function threadWith(
  turns: Turn[],
  overrides: Partial<ThreadDetail> = {},
): ThreadDetail {
  return {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns,
    ...overrides,
  };
}

/** Put the rows the operator watched stream in on the active thread. */
function seedStreamedThread(
  turns: Turn[],
  overrides: Partial<ThreadDetail> = {},
): void {
  useWorkspaceStore.setState({
    activeThread: threadWith(turns, overrides),
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
  });
}

/** The turn table ``GET /api/threads/{id}`` answers with. */
function stubGetThread(detail: ThreadDetail): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(detail), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

describe("openThread GET reconcile", () => {
  it("keeps the watched prose while server fields win", async () => {
    seedStreamedThread([watchedTurn()]);
    stubGetThread(
      threadWith([
        watchedTurn({
          answer: PERSISTED_PROSE,
          citations: [SERVER_CITATION],
          thinking_text: "",
          thinking_duration_ms: 4200,
        }),
      ]),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].answer).toBe(WATCHED_PROSE);
    expect(turns[0].citations).toEqual([SERVER_CITATION]);
    expect(turns[0].run_id).toBe(RUN_ID);
    expect(turns[0].thinking_duration_ms).toBe(4200);
  });

  it("never paints the persisted prose over the watched bubble", async () => {
    seedStreamedThread([watchedTurn()]);
    stubGetThread(
      threadWith([
        watchedTurn({ answer: PERSISTED_PROSE, citations: [SERVER_CITATION] }),
      ]),
    );
    const recorder = recordVisibleAnswers();

    await useWorkspaceStore.getState().openThread(THREAD_ID);
    recorder.stop();

    // Every frame a screenshot could have caught still reads the prose
    // the operator watched arrive — no retract, no repaint.
    expect(recorder.seen.length).toBeGreaterThan(0);
    for (const visible of recorder.seen) expect(visible).toBe(WATCHED_PROSE);
  });

  it("takes the persisted row for turns this session never streamed", async () => {
    seedStreamedThread([]);
    stubGetThread(
      threadWith([
        watchedTurn({ answer: PERSISTED_PROSE, citations: [SERVER_CITATION] }),
      ]),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].answer).toBe(PERSISTED_PROSE);
    expect(turns[0].citations).toEqual([SERVER_CITATION]);
  });

  it("does not carry one thread's streamed prose onto another", async () => {
    seedStreamedThread([watchedTurn()]);
    stubGetThread(
      threadWith([watchedTurn({ answer: PERSISTED_PROSE })], {
        id: OTHER_THREAD_ID,
      }),
    );

    await useWorkspaceStore.getState().openThread(OTHER_THREAD_ID);

    const active = useWorkspaceStore.getState().activeThread;
    expect(active?.id).toBe(OTHER_THREAD_ID);
    expect(active?.turns[0].answer).toBe(PERSISTED_PROSE);
  });

  it("does not restore streamed prose onto a refuse row", async () => {
    const copy = "这次没能给出可核对的答案。";
    seedStreamedThread([watchedTurn()]);
    stubGetThread(
      threadWith([
        watchedTurn({
          status: "refuse",
          status_reason: "citation_unverified",
          answer: "",
          assistant_message: copy,
        }),
      ]),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].answer).toBe("");
    expect(turns[0].assistant_message).toBe(copy);
    expect(turns[0].status).toBe("refuse");
  });

  it("reopens a thread the operator just watched without a repaint", async () => {
    seedStreamedThread([]);
    stubSseRun([
      ["answer", { text: "根据 10-K 原文，" }],
      ["answer", { text: "营收为 391.0 亿美元。" }],
      [
        "status",
        {
          run_id: RUN_ID,
          status: "done",
          assistant_message: "",
          thinking_ms: 4200,
        },
      ],
      ["answer", { text: WATCHED_PROSE }],
      ["citations", []],
    ]);
    const recorder = recordVisibleAnswers();

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    // The operator leaves and comes back: the ThreadPage mount effect
    // re-opens the thread and the persisted row lands on the bubble
    // still holding the prose they watched arrive.
    stubGetThread(
      threadWith([
        watchedTurn({
          answer: PERSISTED_PROSE,
          citations: [SERVER_CITATION],
          thinking_duration_ms: 4200,
        }),
      ]),
    );
    await useWorkspaceStore.getState().openThread(THREAD_ID);
    recorder.stop();

    expectNeverShrinks(recorder.seen);
    expect(recorder.seen).not.toContain(PERSISTED_PROSE);
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].answer).toBe(WATCHED_PROSE);
    expect(turns[0].citations).toEqual([SERVER_CITATION]);
    expect(turns[0].thinking_duration_ms).toBe(4200);
  });

  it("keeps the running row and the live flag when reopened mid-run", async () => {
    // A run only becomes a persisted RunRecord when its stream ends,
    // so a mid-run GET carries the lock flag and no row for it.
    seedStreamedThread([
      watchedTurn({
        run_id: OPTIMISTIC_RUN_ID,
        answer: "半段草稿",
        in_flight: true,
      }),
    ]);
    stubGetThread(threadWith([], { in_flight: true }));

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const active = useWorkspaceStore.getState().activeThread;
    expect(active?.in_flight).toBe(true);
    expect(active?.turns).toHaveLength(1);
    expect(active?.turns[0].answer).toBe("半段草稿");
    expect(active?.turns[0].in_flight).toBe(true);
  });
});

// T10 (ADR 0033): the Sidebar strip reads this slice. A status load
// that fails must still leave the strip renderable — the failure is
// a view state, not an unhandled rejection.
describe("loadStatus", () => {
  function stubStatus(status: number, body: unknown): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(body), {
            status,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
  }

  it("stores the typed payload from the status route", async () => {
    useWorkspaceStore.setState({ status: null, statusLoaded: false });
    stubStatus(200, {
      retrieve_reachability: "unreachable",
      retrieve_adapter_kind: "mcp",
      model_identity: "fin/MiniMax-M3",
      corpus_filing_count: 12,
    });

    await useWorkspaceStore.getState().loadStatus();

    const state = useWorkspaceStore.getState();
    expect(state.statusLoaded).toBe(true);
    expect(state.status?.retrieve_reachability).toBe("unreachable");
    expect(state.status?.corpus_filing_count).toBe(12);
  });

  it("marks the load finished with no payload when the route fails", async () => {
    useWorkspaceStore.setState({ status: null, statusLoaded: false });
    stubStatus(500, { error: "boom" });

    await expect(
      useWorkspaceStore.getState().loadStatus(),
    ).resolves.toBeUndefined();

    const state = useWorkspaceStore.getState();
    expect(state.statusLoaded).toBe(true);
    expect(state.status).toBeNull();
  });

  it("treats a malformed payload as no payload", async () => {
    useWorkspaceStore.setState({ status: null, statusLoaded: false });
    stubStatus(200, { retrieve_reachability: "yes" });

    await useWorkspaceStore.getState().loadStatus();

    const state = useWorkspaceStore.getState();
    expect(state.statusLoaded).toBe(true);
    expect(state.status).toBeNull();
  });
});
