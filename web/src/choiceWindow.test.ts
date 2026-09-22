// search-mode-host T2 (web): the choice-window contract path.
//
// Wire contract (host-locked, specs/search-mode-host.md): the send
// stream ends on a ``choice_window`` SSE frame (kind + non-empty
// options[{id,label}]; ``opened`` / ``rejected_picks`` are additive)
// with NO terminal status/answer/citations frames; resume POSTs
// ``{"pick":"<id>"}``; a body-less resume re-emits the pending
// window. These tests drive the store through a stubbed SSE wire —
// no host process.

import { afterEach, describe, expect, it, vi } from "vitest";
import { ConcurrentRunError } from "./api";
import {
  choiceOptionDisabled,
  hasPendingChoiceWindow,
  parseChoiceWindow,
} from "./choiceWindow";
import { useWorkspaceStore } from "./store";
import type { ChoiceWindow, ThreadDetail, Turn } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "33333333-3333-4333-8333-333333333333";

/** The host's clarify payload for the 检索档 window. */
const SEARCH_MODE_WINDOW = {
  kind: "choice_window",
  title: "选择检索档",
  prompt: "为本次 grounded 提问选择检索档后继续。",
  options: [
    { id: "lexical", label: "查找", opened: true },
    { id: "graph", label: "知识问答", opened: true },
    { id: "global", label: "综述", opened: false },
    { id: "sql", label: "真数据库", opened: false },
  ],
};

type Frame = [string, unknown];

function sseFrame([event, data]: Frame): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

interface CapturedCall {
  url: string;
  method?: string;
  body: unknown;
}

/** Stub the wire: every /messages or /resume POST answers with the
 * NEXT queued frame list (one chunk per frame), anything else answers
 * an empty JSON array (the tail ``refreshThreads``). Records the
 * request URL / method / parsed body of each stream call. */
function stubSseRuns(streams: Frame[][]): CapturedCall[] {
  const calls: CapturedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      if (!url.endsWith("/messages") && !url.endsWith("/resume")) {
        return new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      calls.push({
        url,
        method: init?.method,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      });
      const frames = streams.shift() ?? [];
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
  return calls;
}

function seedThread(turns: Turn[], overrides: Partial<ThreadDetail> = {}): void {
  useWorkspaceStore.setState({
    activeThread: {
      id: THREAD_ID,
      title: "AAPL 10-Q",
      created_at: "2026-01-01T00:00:00.000Z",
      turns,
      ...overrides,
    },
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
  });
}

function windowTurn(window: ChoiceWindow, answer = "半段草稿"): Turn {
  return {
    run_id: RUN_ID,
    question: "营收多少？",
    status: "done",
    status_reason: "",
    answer,
    citations: [],
    thinking_text: "",
    tool_summaries: [],
    assistant_message: "",
    in_flight: true,
    choice_window: window,
    created_at: "2026-01-01T00:00:00.000Z",
  };
}

function parsedWindow(): ChoiceWindow {
  return parseChoiceWindow(SEARCH_MODE_WINDOW)!;
}

function activeTurn(): Turn {
  const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
  return turns[turns.length - 1];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseChoiceWindow (wire boundary)", () => {
  it("parses the host clarify payload into a typed window", () => {
    const window = parseChoiceWindow(SEARCH_MODE_WINDOW);
    expect(window).not.toBeNull();
    expect(window?.kind).toBe("choice_window");
    expect(window?.title).toBe("选择检索档");
    expect(window?.prompt).toBe("为本次 grounded 提问选择检索档后继续。");
    expect(window?.options).toEqual([
      { id: "lexical", label: "查找", opened: true },
      { id: "graph", label: "知识问答", opened: true },
      { id: "global", label: "综述", opened: false },
      { id: "sql", label: "真数据库", opened: false },
    ]);
  });

  it("keeps rejected_picks when the host re-issues after a bad pick", () => {
    const window = parseChoiceWindow({
      ...SEARCH_MODE_WINDOW,
      rejected_picks: ["Z"],
    });
    expect(window?.rejected_picks).toEqual(["Z"]);
  });

  it("drops payloads missing kind / options / with empty options", () => {
    expect(parseChoiceWindow({ ...SEARCH_MODE_WINDOW, kind: undefined })).toBeNull();
    expect(parseChoiceWindow({ ...SEARCH_MODE_WINDOW, options: [] })).toBeNull();
    expect(parseChoiceWindow({ kind: "choice_window" })).toBeNull();
    expect(parseChoiceWindow(null)).toBeNull();
    expect(parseChoiceWindow("choice_window")).toBeNull();
  });

  it("drops option entries without id or label; nulls the window when none survive", () => {
    const kept = parseChoiceWindow({
      kind: "choice_window",
      options: [{ id: "lexical", label: "查找" }, { id: 2, label: "坏" }, { label: "无id" }],
    });
    expect(kept?.options).toEqual([{ id: "lexical", label: "查找" }]);
    expect(
      parseChoiceWindow({ kind: "choice_window", options: [{ label: "无id" }] }),
    ).toBeNull();
  });
});

describe("choice option gating (pure, DOM-free)", () => {
  it("marks opened:false options disabled, others enabled", () => {
    expect(choiceOptionDisabled({ id: "global", label: "综述", opened: false })).toBe(true);
    expect(choiceOptionDisabled({ id: "lexical", label: "查找", opened: true })).toBe(false);
    // An older peer that omits ``opened`` must not render dead buttons.
    expect(choiceOptionDisabled({ id: "lexical", label: "查找" })).toBe(false);
  });

  it("hasPendingChoiceWindow reads the turn table", () => {
    const window = parsedWindow();
    expect(hasPendingChoiceWindow([windowTurn(window)])).toBe(true);
    // A picked turn has its window cleared, not flagged.
    expect(hasPendingChoiceWindow([{ ...windowTurn(window), choice_window: undefined }])).toBe(false);
    expect(hasPendingChoiceWindow([])).toBe(false);
  });
});

describe("send stream ends on a choice_window frame", () => {
  it("surfaces non-empty options on the turn and keeps it not-done", async () => {
    seedThread([]);
    stubSseRuns([
      [
        ["choice_window", SEARCH_MODE_WINDOW],
      ],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    const turn = activeTurn();
    expect((turn.choice_window?.options ?? []).length).toBeGreaterThan(0);
    expect(turn.choice_window?.options[0]).toMatchObject({ id: "lexical", label: "查找" });
    // No terminal frames arrived: the row must not read as finished.
    expect(turn.in_flight).toBe(true);
  });

  it("blocks a further send while the window is pending", async () => {
    // The local gate mirrors the host's 409 concurrent_run so the
    // composer shows the real reason instead of eating the draft.
    seedThread([windowTurn(parsedWindow())]);
    const calls = stubSseRuns([[]]);

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "再来一轮"),
    ).rejects.toBeInstanceOf(ConcurrentRunError);
    expect(calls).toHaveLength(0);
  });
});

describe("pickChoice resumes with the pick and continues the turn", () => {
  it("POSTs {pick} on /resume and folds the streamed answer", async () => {
    // The clarify interrupt suspends BEFORE any answer prose is
    // authored, so the suspended row carries an empty draft; the
    // post-resume stream authors the full answer.
    seedThread([windowTurn(parsedWindow(), "")]);
    const calls = stubSseRuns([
      [
        ["thinking", { text: "续跑" }],
        [
          "status",
          { run_id: RUN_ID, status: "done", assistant_message: "" },
        ],
        ["answer", { text: "391.0 亿美元" }],
        ["citations", []],
      ],
    ]);

    await useWorkspaceStore.getState().pickChoice(THREAD_ID, RUN_ID, "graph");

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/api/threads/${THREAD_ID}/resume`);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({ pick: "graph" });

    const turn = activeTurn();
    expect(turn.answer).toBe("391.0 亿美元");
    expect(turn.in_flight).toBe(false);
    // A terminal resume clears the window (pick 后窗口收起).
    expect(turn.choice_window ?? null).toBeNull();
  });

  it("re-opens the window with rejected_picks on an invalid pick", async () => {
    seedThread([windowTurn(parsedWindow())]);
    // The host records the RAW rejected value, so this names something
    // that resolves to no 档 at all.
    const rejected = { ...SEARCH_MODE_WINDOW, rejected_picks: ["Z"] };
    stubSseRuns([[["choice_window", rejected]]]);

    await useWorkspaceStore.getState().pickChoice(THREAD_ID, RUN_ID, "Q");

    const turn = activeTurn();
    expect(turn.choice_window?.rejected_picks).toEqual(["Z"]);
    expect((turn.choice_window?.options ?? []).length).toBeGreaterThan(0);
    // Still suspended — no terminal frames, still not done.
    expect(turn.in_flight).toBe(true);
  });

  it("ignores a pick for a turn without a pending window", async () => {
    seedThread([windowTurn(parsedWindow())]);
    const calls = stubSseRuns([]);

    const result = await useWorkspaceStore
      .getState()
      .pickChoice(THREAD_ID, "99999999-9999-4999-8999-999999999999", "lexical");

    expect(result).toBeNull();
    expect(calls).toHaveLength(0);
  });
});

describe("body-less resume re-opens a pending window (reload path)", () => {
  it("resumeThread sends no body and re-paints the window", async () => {
    seedThread([]);
    // Reload: a GET-restored suspended run resumes with no pick and
    // the host re-emits the window frame.
    const calls = stubSseRuns([
      [["choice_window", SEARCH_MODE_WINDOW]],
    ]);
    useWorkspaceStore.setState({
      activeThread: {
        id: THREAD_ID,
        title: "AAPL 10-Q",
        created_at: "2026-01-01T00:00:00.000Z",
        turns: [
          {
            run_id: RUN_ID,
            question: "营收多少？",
            status: "done",
            answer: "半段草稿",
            citations: [],
            thinking_text: "",
            tool_summaries: [],
            assistant_message: "",
            in_flight: true,
            created_at: "2026-01-01T00:00:00.000Z",
          },
        ],
      },
    });

    await useWorkspaceStore.getState().resumeThread(THREAD_ID);

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/api/threads/${THREAD_ID}/resume`);
    expect(calls[0].body).toBeNull();
    const turn = activeTurn();
    expect((turn.choice_window?.options ?? []).length).toBeGreaterThan(0);
    expect(turn.in_flight).toBe(true);
  });
});
