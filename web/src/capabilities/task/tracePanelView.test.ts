import { describe, expect, it } from "vitest";

import type { TaskStateView } from "./taskState";
import { turnStatusLabel } from "./taskState";
import { receiptFallbackRows, traceHintText, traceReadHint } from "./tracePanelView";
import type { TraceSpan } from "../trace/traceSpans";
import type { TurnTraceRead } from "../trace/turnTraceRead";
import type { Turn } from "../../types";

const RUN_ID = "33333333-3333-4333-8333-333333333333";

const STATE: TaskStateView = {
  hasTask: true,
  goal: "查明营收",
  expectedResult: "带出处的数字",
  taskOutcome: "active",
  taskOutcomeReason: "",
  turnResult: "answer",
  runStatus: "done",
  projectionUnreadable: false,
  gaps: { reported: false, gaps: [] },
  sources: { available: false, reason: "not_evaluated" },
  showTaskPanel: true,
};

function turn(overrides: Partial<Turn> = {}): Turn {
  return {
    run_id: RUN_ID,
    question: "营收多少？",
    status: "done",
    status_reason: "",
    answer: "391.0 亿美元",
    citations: [],
    assistant_message: "",
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function rowValue(rows: ReturnType<typeof receiptFallbackRows>, key: string): string {
  const row = rows.find((candidate) => candidate.key === key);
  if (row === undefined) throw new Error(`missing row ${key}`);
  return row.value;
}

describe("receiptFallbackRows", () => {
  it("states the run status and turn result even before stages", () => {
    const rows = receiptFallbackRows(STATE, turn());
    expect(rowValue(rows, "run-status")).toBe("运行已结束");
    expect(rowValue(rows, "turn-result")).toBe("已回答");
    expect(rowValue(rows, "turn-status")).toBe("完成");
  });

  it("renders the thinking and tool stages the turn recorded", () => {
    const rows = receiptFallbackRows(
      STATE,
      turn({
        thinking_text: "先查最新财报",
        thinking_duration_ms: 1200,
        tool_summaries: [
          { name: "retrieve", summary: "命中 2 条", tool_call_id: "c1" },
          { name: "retrieve", summary: "命中 1 条", tool_call_id: "c2" },
        ],
      }),
    );

    expect(rowValue(rows, "thinking-0")).toBe("1.20 s");
    expect(rowValue(rows, "tools-1")).toBe("2 次：retrieve");
  });

  it("says a stage was recorded when it carries no duration", () => {
    const rows = receiptFallbackRows(
      STATE,
      turn({ thinking_text: "推理", thinking_duration_ms: 0 }),
    );
    expect(rowValue(rows, "thinking-0")).toBe("已记录");
  });

  it("states an absent stage list rather than rendering an empty one", () => {
    const rows = receiptFallbackRows(STATE, turn());
    expect(rowValue(rows, "stages")).toBe("回执未记录阶段");
  });

  it("says there is no current turn when the thread has none", () => {
    const rows = receiptFallbackRows(STATE, null);
    expect(rowValue(rows, "turn-status")).toBe("没有当前轮次");
  });

  it("marks an in-flight turn as running", () => {
    const rows = receiptFallbackRows(STATE, turn({ in_flight: true }));
    expect(rowValue(rows, "turn-status")).toBe("运行中");
  });

  it("uses the shared turn-status wording, never the raw status_reason", () => {
    // The panel used to keep its own label map and append the reason
    // verbatim, so a technical token reached the operator. It reads the
    // one authority now, and the reason is not part of any row.
    const reason = "stream_error_429";
    const rows = receiptFallbackRows(
      STATE,
      turn({ status: "refuse", status_reason: reason, answer: "" }),
    );

    const value = rowValue(rows, "turn-status");
    expect(value).toBe(turnStatusLabel({ status: "refuse" }));
    expect(value).toBe("未完成");
    expect(rows.map((row) => row.value).join(" ")).not.toContain(reason);
    expect(rowValue(rows, "turn-status")).not.toContain("stream_error");
  });
});

describe("turnStatusLabel", () => {
  it("is the one wording the status line and the fallback share", () => {
    expect(turnStatusLabel({ status: "done" })).toBe("完成");
    expect(turnStatusLabel({ status: "refuse" })).toBe("未完成");
    expect(turnStatusLabel({ status: "step_cap" })).toBe("步数上限（step_cap）");
    expect(turnStatusLabel({ status: "done", in_flight: true })).toBe("运行中");
  });
});

describe("traceHintText", () => {
  it("names a disabled trace only when no exporter is selected", () => {
    expect(traceHintText("off")).toBe("trace 未开启");
  });

  it("does not call an enabled-but-empty local trace disabled", () => {
    expect(traceHintText("local")).toBe("trace 已开启，但本轮没有 span");
  });

  it("says external exporters keep their spans off this view", () => {
    expect(traceHintText("external")).toBe("trace 已开启（非本地存储）");
  });

  it("states an unreadable trace as unknown, not disabled", () => {
    expect(traceHintText("unreadable")).toBe("trace 状态不可读，显示回执");
    expect(traceHintText(null)).toBe("trace 状态不可读，显示回执");
  });
});

describe("traceReadHint", () => {
  const span: TraceSpan = {
    trace_id: "a".repeat(32),
    span_id: "s1",
    parent_span_id: null,
    name: "retrieve_tool",
    start_time_ms: 1_000,
    duration_ms: 100,
    attributes: {},
  };

  it("reads the state of a finished read", () => {
    const read: TurnTraceRead = {
      status: "ready",
      traceState: "local",
      spans: [span],
    };
    expect(traceReadHint(read)).toBe("trace 已开启，但本轮没有 span");
  });

  it("treats a failed read as unreadable, not as disabled", () => {
    expect(traceReadHint({ status: "error" })).toBe("trace 状态不可读，显示回执");
  });

  it("names the loading state while the read is out", () => {
    expect(traceReadHint({ status: "loading" })).toBe("trace 状态读取中…");
    expect(traceReadHint({ status: "idle" })).toBe("trace 状态读取中…");
  });
});
