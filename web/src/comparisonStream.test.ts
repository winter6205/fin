// The comparison table on the live wire and across a reload.
//
// Two things must hold for the operator to trust the table:
//
// 1. It appears WHILE the run streams (the ``comparison`` SSE frame
//    lands right after the tool returns, well before the answer),
//    so the numbers are on screen while the prose is still being
//    authored.
// 2. It survives the GET reconcile. ``openThread`` folds the
//    persisted turn table over the streamed one; a fold that
//    dropped ``comparisons`` would blank a table the operator was
//    already reading.

import { afterEach, describe, expect, it, vi } from "vitest";

import { useWorkspaceStore } from "./store";
import { mergeReconciledTurn } from "./turnStream";
import type { Comparison, ThreadDetail, Turn } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "22222222-2222-4222-8222-222222222222";

const COMPARISON: Comparison = {
  schema: "fin.comparison.v1",
  kind: "table",
  axis: "period",
  concept: "us-gaap:Revenues",
  unit: "USD",
  scale: "millions",
  columns: [
    { key: "2022-Q3-YTD", label: "2022-Q3-YTD" },
    { key: "2023-Q3-YTD", label: "2023-Q3-YTD" },
  ],
  rows: [
    {
      key: "AAPL",
      label: "AAPL",
      kind: "issuer",
      cells: [
        {
          row: "AAPL",
          column: "2022-Q3-YTD",
          status: "hit",
          value: 194140,
          display: "194,140",
          unit: "USD",
          scale: "millions",
          locator: {
            document_id: "AAPL-2022Q3",
            version: "a".repeat(64),
            page: 4,
          },
        },
        {
          row: "AAPL",
          column: "2023-Q3-YTD",
          status: "hit",
          value: 293787,
          display: "293,787",
          unit: "USD",
          scale: "millions",
          locator: {
            document_id: "AAPL-2023Q3",
            version: "b".repeat(64),
            page: 4,
          },
        },
      ],
    },
  ],
  deltas: [
    {
      from: { row: "AAPL", column: "2022-Q3-YTD" },
      to: { row: "AAPL", column: "2023-Q3-YTD" },
      absolute: 99647,
      percent: 51.3277,
      direction: "up",
      display: "+99,647 (+51.33%)",
    },
  ],
  missing: [],
  hit_count: 2,
  cell_count: 2,
  locators: [
    { document_id: "AAPL-2022Q3", version: "a".repeat(64), page: 4 },
    { document_id: "AAPL-2023Q3", version: "b".repeat(64), page: 4 },
  ],
  tool_call_id: "c-compare",
};

type Frame = [string, unknown];

function sseFrame([event, data]: Frame): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

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

function seedThread(turns: Turn[] = []): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns,
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
  });
}

function latestTurn(): Turn {
  const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
  return turns[turns.length - 1];
}

const TOOL_FRAMES: Frame[] = [
  [
    "tool_call",
    {
      name: "compare_facts",
      args: { concept: "us-gaap:Revenues" },
      tool_call_id: "c-compare",
    },
  ],
  [
    "tool_result",
    {
      name: "compare_facts",
      summary: "us-gaap:Revenues 1x2 2/2",
      tool_call_id: "c-compare",
    },
  ],
  ["comparison", COMPARISON],
];

const TERMINAL_FRAMES: Frame[] = [
  ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
  ["answer", { text: "营收同比增长。" }],
  ["citations", []],
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sendQuestion comparison fold", () => {
  it("puts the table on the turn from the live frame", async () => {
    seedThread();
    stubSseRun([...TOOL_FRAMES, ...TERMINAL_FRAMES]);

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "对比 AAPL 两期营收");

    expect(latestTurn().comparisons).toEqual([COMPARISON]);
  });

  it("lands the table before the answer prose", async () => {
    seedThread();
    stubSseRun([...TOOL_FRAMES, ...TERMINAL_FRAMES]);

    // Whether the table was on screen at the moment the first
    // answer fragment painted — that ordering is the point of a
    // dedicated frame instead of waiting for the GET.
    let tableWasReadyAtFirstProse: boolean | null = null;
    const stop = useWorkspaceStore.subscribe((state) => {
      const turns = state.activeThread?.turns ?? [];
      const turn = turns[turns.length - 1];
      if (!turn || tableWasReadyAtFirstProse !== null) return;
      if (turn.answer.length > 0) {
        tableWasReadyAtFirstProse = (turn.comparisons?.length ?? 0) > 0;
      }
    });
    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "对比 AAPL 两期营收");
    stop();

    expect(tableWasReadyAtFirstProse).toBe(true);
  });

  it("keeps the table on a refused turn", async () => {
    // SC2: the numeric gate refused because the prose quoted
    // figures the table never reported. Hiding the table would hide
    // the evidence for the refusal.
    seedThread();
    stubSseRun([
      ...TOOL_FRAMES,
      [
        "status",
        {
          run_id: RUN_ID,
          status: "refuse",
          reason: "numeric_gate_fail",
          assistant_message: "无法把答案中的数字对上申报口径。",
        },
      ],
      ["answer", { text: "" }],
      ["citations", []],
    ]);

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "对比 AAPL 两期营收");

    const turn = latestTurn();
    expect(turn.status).toBe("refuse");
    expect(turn.citations).toEqual([]);
    expect(turn.comparisons).toEqual([COMPARISON]);
  });

  it("ignores a malformed comparison frame", async () => {
    seedThread();
    stubSseRun([
      ["comparison", { schema: "fin.comparison.v99", rows: [] }],
      ...TERMINAL_FRAMES,
    ]);

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "对比 AAPL 两期营收");

    expect(latestTurn().comparisons).toEqual([]);
  });

  it("leaves comparisons empty on a run that never compared", async () => {
    seedThread();
    stubSseRun(TERMINAL_FRAMES);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    expect(latestTurn().comparisons).toEqual([]);
  });
});

describe("GET reconcile", () => {
  function turnWith(comparisons: Comparison[] | undefined): Turn {
    return {
      run_id: RUN_ID,
      question: "对比 AAPL 两期营收",
      status: "done",
      answer: "营收同比增长。",
      citations: [],
      thinking_text: "",
      tool_summaries: [],
      assistant_message: "",
      in_flight: false,
      comparisons,
      created_at: "2026-01-01T00:00:00.000Z",
    };
  }

  it("takes the persisted table when the server has one", () => {
    const merged = mergeReconciledTurn(turnWith([]), turnWith([COMPARISON]));
    expect(merged.comparisons).toEqual([COMPARISON]);
  });

  it("keeps the streamed table when the server row carries none", () => {
    // A mid-run reopen: the RunRecord is appended only once the
    // stream completes, so the persisted row can legitimately lag.
    const merged = mergeReconciledTurn(
      turnWith([COMPARISON]),
      turnWith(undefined),
    );
    expect(merged.comparisons).toEqual([COMPARISON]);
  });
});
