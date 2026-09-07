import { describe, expect, it } from "vitest";
import {
  closeOpenBursts,
  foldTraceFrame,
  thinkingTailPreview,
  traceItemsFromTurn,
} from "./turnTrace";
import type { Turn } from "./types";

describe("foldTraceFrame", () => {
  it("returns an empty list for empty input", () => {
    expect(
      foldTraceFrame([], {
        type: "tool_result",
        summary: "s",
        tool_call_id: "x",
      }),
    ).toEqual([]);
  });

  it("merges consecutive thinking frames into one live burst", () => {
    let items = foldTraceFrame([], { type: "thinking", text: "a" });
    items = foldTraceFrame(items, { type: "thinking", text: "b" });
    expect(items).toEqual([{ kind: "thinking", text: "ab", live: true }]);
  });

  it("closes the thinking burst when a tool_call arrives", () => {
    let items = foldTraceFrame([], { type: "thinking", text: "a" });
    items = foldTraceFrame(items, {
      type: "tool_call",
      name: "retrieve",
      tool_call_id: "c1",
    });
    expect(items).toEqual([
      { kind: "thinking", text: "a", live: false },
      {
        kind: "tools",
        calls: [{ name: "retrieve", summary: "运行中…", tool_call_id: "c1" }],
        live: true,
      },
    ]);
  });

  it("groups consecutive tool frames into the same tool burst", () => {
    let items = foldTraceFrame([], {
      type: "tool_call",
      name: "retrieve",
      tool_call_id: "c1",
    });
    items = foldTraceFrame(items, {
      type: "tool_result",
      summary: "'q': 1 hit",
      tool_call_id: "c1",
    });
    items = foldTraceFrame(items, {
      type: "tool_call",
      name: "read_file",
      tool_call_id: "c2",
    });
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("tools");
    if (items[0].kind === "tools") {
      expect(items[0].calls.map((c) => c.summary)).toEqual([
        "'q': 1 hit",
        "运行中…",
      ]);
      expect(items[0].live).toBe(true);
    }
  });

  it("opens a new thinking burst that closes the open tool burst", () => {
    let items = foldTraceFrame([], {
      type: "tool_call",
      name: "retrieve",
      tool_call_id: "c1",
    });
    items = foldTraceFrame(items, { type: "thinking", text: "think again" });
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "tools", live: false });
    expect(items[1]).toMatchObject({ kind: "thinking", live: true });
  });

  it("replaces the tool_result summary on the matching call", () => {
    let items = foldTraceFrame([], {
      type: "tool_call",
      name: "retrieve",
      tool_call_id: "c1",
    });
    items = foldTraceFrame(items, {
      type: "tool_result",
      summary: "'q': 1 hit",
      tool_call_id: "c1",
    });
    if (items[0].kind === "tools") {
      expect(items[0].calls[0].summary).toBe("'q': 1 hit");
      // The burst is still live: the next frame may join it.
      expect(items[0].live).toBe(true);
    } else {
      expect.unreachable("expected a tools burst");
    }
  });

  it("drops a result whose tool_call_id was never announced", () => {
    const items = foldTraceFrame([], {
      type: "tool_result",
      summary: "orphan",
      tool_call_id: "ghost",
    });
    expect(items).toEqual([]);
  });

  it("does not mutate the input list", () => {
    const before: ReturnType<typeof foldTraceFrame> = [
      { kind: "thinking", text: "a", live: true },
    ];
    const after = foldTraceFrame(before, { type: "thinking", text: "b" });
    expect(before[0]).toMatchObject({ text: "a", live: true });
    expect(after).not.toBe(before);
  });
});

describe("closeOpenBursts", () => {
  it("closes all live bursts (run ended)", () => {
    const items = closeOpenBursts([
      { kind: "thinking", text: "a", live: false },
      { kind: "thinking", text: "b", live: true },
    ]);
    expect(items.every((item) => !item.live)).toBe(true);
  });

  it("is a no-op when nothing is live", () => {
    const items: Parameters<typeof closeOpenBursts>[0] = [
      { kind: "tools", calls: [], live: false },
    ];
    expect(closeOpenBursts(items)).toEqual(items);
  });
});

describe("thinkingTailPreview", () => {
  it("returns the LAST three lines for long text", () => {
    const text = ["l1", "l2", "l3", "l4", "l5"].join("\n");
    expect(thinkingTailPreview(text)).toEqual(["l3", "l4", "l5"]);
  });

  it("returns all lines when the burst is shorter than three", () => {
    expect(thinkingTailPreview("only line")).toEqual(["only line"]);
  });

  it("skips leading blank lines", () => {
    expect(thinkingTailPreview("\n\na\nb")).toEqual(["a", "b"]);
  });

  it("handles empty text", () => {
    expect(thinkingTailPreview("")).toEqual([""]);
  });
});

describe("traceItemsFromTurn", () => {
  const base: Turn = {
    run_id: "00000000-0000-4000-8000-000000000001",
    question: "q",
    status: "done",
    answer: "a",
    citations: [],
    created_at: "2026-01-01T00:00:00.000Z",
  };

  it("restores one collapsed thinking fold + one collapsed tool fold", () => {
    const items = traceItemsFromTurn({
      ...base,
      thinking_text: "blob",
      tool_summaries: [
        { name: "retrieve", summary: "1 hit", tool_call_id: "c1" },
      ],
    });
    expect(items).toEqual([
      { kind: "thinking", text: "blob", live: false },
      {
        kind: "tools",
        calls: [{ name: "retrieve", summary: "1 hit", tool_call_id: "c1" }],
        live: false,
      },
    ]);
  });

  it("produces nothing for a turn with no trace", () => {
    expect(traceItemsFromTurn(base)).toEqual([]);
  });
});
