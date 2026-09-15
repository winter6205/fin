// SC17: a send that fails BEFORE any SSE frame must reach the
// operator. The store used to append an optimistic turn, let the
// rejection escape, and leave that row ``in_flight`` forever while
// the composer had already cleared the draft.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ConcurrentRunError,
  IngestMissingError,
  KnowledgeMcpFaultError,
} from "./api";
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
  const frame = `event: status\ndata: ${JSON.stringify({
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

/** Hold `/messages` or `/resume` until `release` so the optimistic
 * turn can be inspected before any SSE frame is parsed. */
function stubHeldSse(release: Promise<void>, frames: Frame[] = []): void {
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
      await release;
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

  it("maps a 409 knowledge_mcp_fault body to the typed MCP fault, not concurrency", async () => {
    seedThread();
    stubSendStatus(409, {
      error: "knowledge_mcp_fault",
      message: "知识 MCP 故障",
    });

    const rejection = await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(KnowledgeMcpFaultError);
    expect(rejection).not.toBeInstanceOf(ConcurrentRunError);
    expect((rejection as KnowledgeMcpFaultError).message).toBe("知识 MCP 故障");
    expect(describeSendFailure(rejection).kind).toBe("knowledge_mcp_fault");
  });

  it("resume maps the same knowledge_mcp_fault 409", async () => {
    seedResumableThread();
    stubSendStatus(409, {
      error: "knowledge_mcp_fault",
      message: "知识 MCP 故障",
    });

    await expect(
      useWorkspaceStore.getState().resumeThread(THREAD_ID),
    ).rejects.toBeInstanceOf(KnowledgeMcpFaultError);
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

// thinking pending (turn-trace-folds): optimistic send shows the same
// 深度思考 chrome with no burst body until the first real frame.
describe("thinking pending on send", () => {
  function pendingThinking(items: unknown): boolean {
    return (
      Array.isArray(items) &&
      items.length === 1 &&
      items[0] !== null &&
      typeof items[0] === "object" &&
      (items[0] as { kind?: string; text?: string; live?: boolean }).kind ===
        "thinking" &&
      (items[0] as { text?: string }).text === "" &&
      (items[0] as { live?: boolean }).live === true
    );
  }

  it("seeds empty live thinking chrome on the optimistic turn before any SSE frame", async () => {
    seedThread();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    stubHeldSse(gate);

    const sendP = useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "营收多少？");
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0]?.trace_items;
    expect(pendingThinking(items)).toBe(true);

    release();
    await sendP.catch(() => undefined);
  });

  it("seeds thinking pending even when the thinking toggle is off", async () => {
    seedThread();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    stubHeldSse(gate);

    const sendP = useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "营收多少？", false);
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0]?.trace_items;
    expect(pendingThinking(items)).toBe(true);

    release();
    await sendP.catch(() => undefined);
  });

  it("merges the first thinking frame into the pending fold, not a second card", async () => {
    seedThread();
    const seen: unknown[][] = [];
    const stop = useWorkspaceStore.subscribe((state) => {
      const turns = state.activeThread?.turns ?? [];
      if (turns.length > 0) seen.push(turns[0].trace_items ?? []);
    });
    stubSseRun([
      ["thinking", { text: "first burst" }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: "a" }],
      ["citations", []],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    stop();

    expect(seen.some(pendingThinking)).toBe(true);
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0].trace_items ?? [];
    expect(items.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "thinking",
      text: "first burst",
      live: false,
    });
  });

  it("drops empty pending when the first frame is tool_call", async () => {
    seedThread();
    const seen: unknown[][] = [];
    const stop = useWorkspaceStore.subscribe((state) => {
      const turns = state.activeThread?.turns ?? [];
      if (turns.length > 0) seen.push(turns[0].trace_items ?? []);
    });
    stubSseRun([
      [
        "tool_call",
        { name: "retrieve", args: { query: "q" }, tool_call_id: "c1" },
      ],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: "a" }],
      ["citations", []],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    stop();

    expect(seen.some(pendingThinking)).toBe(true);
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0].trace_items ?? [];
    expect(items.filter((i) => i.kind === "thinking")).toEqual([]);
    expect(items.some((i) => i.kind === "tools")).toBe(true);
  });

  it("drops empty pending when the first frame is answer", async () => {
    seedThread();
    const seen: unknown[][] = [];
    const stop = useWorkspaceStore.subscribe((state) => {
      const turns = state.activeThread?.turns ?? [];
      if (turns.length > 0) seen.push(turns[0].trace_items ?? []);
    });
    stubSseRun([
      ["answer", { text: "a" }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["citations", []],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    stop();

    expect(seen.some(pendingThinking)).toBe(true);
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0].trace_items ?? [];
    expect(items.filter((i) => i.kind === "thinking")).toEqual([]);
  });

  it("does not seed thinking pending on resumeThread", async () => {
    seedResumableThread();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    stubHeldSse(gate);

    const resumeP = useWorkspaceStore.getState().resumeThread(THREAD_ID);
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0]?.trace_items;
    expect(pendingThinking(items)).toBe(false);

    release();
    await resumeP.catch(() => undefined);
  });
});

// turn-trace-folds (the tracked change): the store folds the SSE stream into the
// turn's burst timeline — arrival-order sibling thinking/tool folds,
// auto-collapse at burst end.
describe("trace burst folding on send", () => {
  it("renders thinking-fold -> tool-fold -> thinking-fold in arrival order", async () => {
    seedThread();
    stubSseRun([
      ["thinking", { text: "first burst" }],
      [
        "tool_call",
        { name: "retrieve", args: { query: "q" }, tool_call_id: "c1" },
      ],
      [
        "tool_result",
        { name: "retrieve", summary: "'q': 1 hit", tool_call_id: "c1" },
      ],
      ["thinking", { text: "second burst" }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: "a" }],
      ["citations", []],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    const items = turns[0].trace_items ?? [];
    expect(items.map((i) => i.kind)).toEqual(["thinking", "tools", "thinking"]);
    // All bursts collapsed once the run ended.
    expect(items.every((i) => i.live === false)).toBe(true);
    // Tool one-liner replaced the placeholder inside the tool burst.
    const tools = items[1];
    if (tools.kind === "tools") {
      expect(tools.calls[0].summary).toBe("'q': 1 hit");
    } else {
      expect.unreachable("expected a tools burst");
    }
    // tool_summaries (persist SSOT) still maintained in parallel.
    expect(turns[0].tool_summaries?.[0]?.summary).toBe("'q': 1 hit");
  });

  // Spec line 33 + #252 follow-up: the SECOND thinking burst must
  // collapse when the first answer frame streams, NOT stay open until
  // terminal status — the operator watches prose while the fold is
  // already folded (with its tail preview).
  it("collapses the live thinking burst at the first answer frame, BEFORE status arrives", async () => {
    seedThread();
    // Wire order matters: the burst must already be closed when the
    // answer streams, NOT only at the terminal status frame.
    stubSseRun([
      ["thinking", { text: "burst one" }],
      ["answer", { text: "part" }],
      ["citations", []],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
    ]);

    // Capture the store state after every update. The contract
    // under test: after the answer frame (and before status) there
    // is a state where the thinking burst is closed.
    const midStates: string[][] = [];
    const unsub = useWorkspaceStore.subscribe((s) => {
      const turns = s.activeThread?.turns ?? [];
      if (turns.length === 0) return;
      midStates.push(
        (turns[0].trace_items ?? []).map(
          (i) => `${i.kind}:${i.live ? "live" : "closed"}`,
        ),
      );
    });

    let statusSeenAt = -1;
    try {
      await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
      // Re-run the fold against a bare wire to find where status
      // landed: in THIS stub status is the 4th frame, i.e. the LAST
      // state captured. Everything up to the citations state came
      // from thinking/answer handling alone.
      statusSeenAt = midStates.length - 1;
    } finally {
      unsub();
    }
    // While the burst streamed it was live...
    expect(midStates).toContainEqual(["thinking:live"]);
    // ...and by the time the answer frame landed (before status's
    // closeOpenBursts could act) it was closed.
    expect(
      midStates
        .slice(0, Math.max(statusSeenAt, 1))
        .some((kinds) => kinds.includes("thinking:closed")),
    ).toBe(true);
    const items =
      useWorkspaceStore.getState().activeThread?.turns[0].trace_items ?? [];
    expect(items.every((i) => i.live === false)).toBe(true);
  });

  it("closes the live thinking burst when a tool_call arrives mid-run", async () => {
    seedThread();
    stubSseRun([
      ["thinking", { text: "burst one" }],
      [
        "tool_call",
        { name: "retrieve", args: { query: "q" }, tool_call_id: "c1" },
      ],
    ]);
    // Drive the stream manually: stubSseRun closes after all frames,
    // so capture state after the tool_call frame via subscribe.
    const seen: string[] = [];
    const stop = useWorkspaceStore.subscribe((state) => {
      const turns = state.activeThread?.turns ?? [];
      if (turns.length > 0) {
        const items = turns[0].trace_items ?? [];
        seen.push(
          items.map((i) => `${i.kind}:${i.live ? "live" : "closed"}`).join(","),
        );
      }
    });

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
    stop();

    // Before the status frame the thinking burst must already be
    // closed while the tool burst is live.
    expect(seen.some((s) => s === "thinking:closed,tools:live")).toBe(true);
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    const items = turns[0].trace_items ?? [];
    expect(items.every((i) => i.live === false)).toBe(true);
  });

  // #252 follow-up (render saturation): at provider chunk granularity
  // every SSE frame used to trigger one store patch → one full
  // transcript re-render; on a long thread the live fold froze and
  // the answer dumped at run end. The store now coalesces delta
  // patches into one animation-frame flush. With rAF stubbed, a
  // burst of thinking frames must land as ONE patch whose folded
  // trace is identical to unbatched folding.
  it("coalesces a burst of thinking deltas into one rAF flush", async () => {
    seedThread();
    stubSseRun([
      ["thinking", { text: "a" }],
      ["thinking", { text: "b" }],
      ["thinking", { text: "c" }],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: "x" }],
      ["citations", []],
    ]);

    // Stub rAF BEFORE the send: capture the callback and run it
    // manually so the flush boundary is deterministic.
    let rafCallback: FrameRequestCallback | null = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      rafCallback = cb;
      return 1;
    });
    // One entry per store update carrying a thinking burst — the
    // batched fold must produce ONE mid-stream update ("a" through
    // "c" in a single flush), not one per wire frame.
    const patchTexts: string[] = [];
    const stop = useWorkspaceStore.subscribe((state) => {
      const turns = state.activeThread?.turns ?? [];
      if (turns.length === 0) return;
      const first = turns[0].trace_items?.[0];
      if (first && first.kind === "thinking") patchTexts.push(first.text);
    });

    try {
      await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
      // Deltas queued while rAF was stubbed never flushed mid-run:
      // no intermediate state ever showed a partial fold ("a",
      // "ab", "bc"). Later updates re-push the same burst text (the
      // status/citations patches ride the same turn), so dedupe
      // consecutive entries before comparing.
      const streamed = patchTexts.filter((text) => text.length > 0);
      const distinct = streamed.filter(
        (text, i) => i === 0 || text !== streamed[i - 1],
      );
      expect(distinct).toEqual(["abc"]);
      // The scheduled rAF then fires on an empty queue (no-op).
      if (rafCallback) (rafCallback as FrameRequestCallback)(0);
      rafCallback = null;
      expect(
        streamed.filter((text, i) => i === 0 || text !== streamed[i - 1]),
      ).toEqual(["abc"]);
    } finally {
      stop();
      vi.unstubAllGlobals();
    }

    // The flushed fold is identical to unbatched folding.
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    const items = turns[0].trace_items ?? [];
    expect(items).toEqual([{ kind: "thinking", text: "abc", live: false }]);
    expect(turns[0].thinking_text).toBe("abc");
  });

  // Ordering invariant of the patch queue: a non-delta frame must
  // never overtake — or land after — deltas queued in the same
  // animation frame. Here the thinking delta is queued (rAF stubbed),
  // then tool_call arrives; flushThen must drain the queued delta
  // BEFORE folding the tool_call, preserving wire arrival order.
  it("flushes queued deltas before folding an immediately-arriving tool_call", async () => {
    seedThread();
    stubSseRun([
      ["thinking", { text: "burst one" }],
      [
        "tool_call",
        { name: "retrieve", args: { query: "q" }, tool_call_id: "c1" },
      ],
      ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
      ["answer", { text: "x" }],
      ["citations", []],
    ]);

    let rafCallback: FrameRequestCallback | null = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      rafCallback = cb;
      return 1;
    });
    try {
      await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");
      if (rafCallback) (rafCallback as FrameRequestCallback)(0);
    } finally {
      vi.unstubAllGlobals();
    }

    const items =
      useWorkspaceStore.getState().activeThread?.turns[0].trace_items ?? [];
    // Wire order preserved: thinking burst FIRST, tools after it.
    expect(items.map((i) => i.kind)).toEqual(["thinking", "tools"]);
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

  // T3 (ADR 0044): the detail GET carries the occupancy figure the
  // auto-compact gate computes. ``openThread`` stores it verbatim
  // (reconcile never drops a server field it does not know).
  it("stores the server occupancy figure on the active thread", async () => {
    seedStreamedThread([]);
    stubGetThread(
      threadWith([], {
        occupancy: {
          estimated_tokens: 3072,
          budget_tokens: 256000,
          ratio: 0.012,
        },
      }),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const active = useWorkspaceStore.getState().activeThread;
    expect(active?.occupancy).toEqual({
      estimated_tokens: 3072,
      budget_tokens: 256000,
      ratio: 0.012,
    });
  });

  it("keeps occupancy absent for a thread with no checkpoint", async () => {
    seedStreamedThread([]);
    stubGetThread(threadWith([]));

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    expect(
      useWorkspaceStore.getState().activeThread?.occupancy,
    ).toBeUndefined();
  });
});

// T3 (specs/workbench-chrome.md): the 监控 重连 control re-GETs
// /api/status. SC7 forbids a double-click while the first GET is in
// flight from "looking like success twice" — it must issue ONE wire
// call. The store flips a ``loadingStatus`` flag the button can
// disable, and ``loadStatus`` ignores concurrent calls until the
// in-flight one resolves. The existing exception tests above prove
// a fetch throw still produces the unavailable view.
describe("loadStatus 重连 in-flight guard", () => {
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

  it("ignores a concurrent 重连 while the first GET is still in flight", async () => {
    useWorkspaceStore.setState({
      status: null,
      statusLoaded: false,
      loadingStatus: false,
    });

    let resolveFetch: ((value: Response) => void) | null = null;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = useWorkspaceStore.getState().loadStatus();
    // The second dispatch happens BEFORE the first promise resolves.
    const second = useWorkspaceStore.getState().loadStatus();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(useWorkspaceStore.getState().loadingStatus).toBe(true);

    resolveFetch!(
      new Response(
        JSON.stringify({
          knowledge_mcp: "reachable",
          retrieve_adapter_kind: "mcp",
          model_identity: "fin/MiniMax-M3",
          corpus_filing_count: 12,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await Promise.all([first, second]);

    const state = useWorkspaceStore.getState();
    expect(state.loadingStatus).toBe(false);
    expect(state.statusLoaded).toBe(true);
    expect(state.status?.knowledge_mcp).toBe("reachable");
  });

  it("flips loadingStatus true while the fetch is pending and false after", async () => {
    useWorkspaceStore.setState({
      status: null,
      statusLoaded: false,
      loadingStatus: false,
    });
    stubStatus(200, {
      knowledge_mcp: "reachable",
      retrieve_adapter_kind: "mcp",
      model_identity: "fin/MiniMax-M3",
      corpus_filing_count: 12,
    });

    expect(useWorkspaceStore.getState().loadingStatus).toBe(false);
    await useWorkspaceStore.getState().loadStatus();
    expect(useWorkspaceStore.getState().loadingStatus).toBe(false);
    expect(useWorkspaceStore.getState().statusLoaded).toBe(true);
  });

  it("clears loadingStatus even when the fetch throws (no silent OK)", async () => {
    useWorkspaceStore.setState({
      status: null,
      statusLoaded: false,
      loadingStatus: false,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network down");
      }),
    );

    await useWorkspaceStore.getState().loadStatus();

    const state = useWorkspaceStore.getState();
    expect(state.loadingStatus).toBe(false);
    expect(state.statusLoaded).toBe(true);
    expect(state.status).toBeNull();
  });
});
