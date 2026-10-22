// What TaskPanel actually renders. The pure modules pin the row VALUES;
// this file pins the MARKUP, because a value that never reaches a row is
// indistinguishable from an absent fact to the operator.
//
// Rendered with ``renderToString`` (same convention as
// ``components/VisualClaims.test.ts``): this project's vitest setup runs in
// a node environment with no DOM testing library, and string output is
// enough to pin which rows exist, in what words, and that none is blank.

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

import TaskPanel from "./capabilities/task/components/TaskPanel";
import {
  gapsText,
  runStatusText,
  sourcesText,
  submissionText,
  taskOutcomeText,
} from "./capabilities/task/taskPanelView";
import { taskState, type SubmissionNotice, type TaskStateView } from "./capabilities/task/taskState";
import type { TraceSpan } from "./capabilities/trace/traceSpans";
import type { TurnTraceRead } from "./capabilities/trace/turnTraceRead";
import type {
  CurrentTask,
  PendingQuestionView,
  SearchModeDecision,
  TaskRuntimeView,
  Turn,
} from "./types";

const RUN_ID = "33333333-3333-4333-8333-333333333333";

const PENDING: PendingQuestionView = {
  question_id: "q-1",
  task_id: "task-1",
  task_version: 1,
  prompt: "要查哪一季度的营收？",
  choices: ["最新季度", "上一财年", "other"],
  input_version: 1,
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

function currentTask(overrides: Partial<CurrentTask> = {}): CurrentTask {
  return {
    task_id: "task-1",
    goal: "查明最新一季营收",
    expected_result: "带出处的营收数字",
    task_version: 1,
    necessary_conditions: [],
    outcome: "active",
    outcome_reason: "",
    created_at_input_version: 1,
    ...overrides,
  };
}

function envelope(overrides: Partial<TaskRuntimeView> = {}): TaskRuntimeView {
  return {
    current_task: currentTask(),
    archived_tasks: [],
    pending_question: null,
    reconciliation: null,
    task_outcome: "active",
    input_version: 1,
    input_history: [],
    ...overrides,
  };
}

function render(
  state: TaskStateView,
  devMode = true,
  searchModeDecision?: SearchModeDecision | null,
  extra: {
    threadId?: string;
    currentTurn?: Turn | null;
    trace?: TurnTraceRead;
  } = {},
): string {
  // ``devMode`` defaults to TRUE here because these cases pin WHAT the
  // rows say — the markup of the receipt surface itself. Whether that
  // surface is visible in the product's default view is a separate
  // question, pinned in its own describe block below. Gating the render
  // must not change a single row value, so every row assertion below is
  // asserted against the developer-mode render it was written for.
  return renderToString(
    createElement(TaskPanel, {
      state,
      devMode,
      searchModeDecision,
      ...extra,
    }),
  );
}

/** Label -> value for every rendered row, read off the panel markup. */
function rows(html: string): Map<string, string> {
  const found = new Map<string, string>();
  const pattern =
    /<span class="shrink-0 text-gray-500">([^<]*)<\/span><span class="min-w-0 break-words">([^<]*)<\/span>/g;
  for (const match of html.matchAll(pattern)) {
    found.set(match[1], match[2]);
  }
  return found;
}

function visible(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ");
}

/** The internal retrieval names and their operator labels. The rendered
 * receipt must carry NONE of them — neither in the visible text nor in
 * an attribute (docs/CONTEXT.md 检索档: "rendered UI omits internal mode
 * names and raw tokens remain in machine diagnostics"). */
const INTERNAL_MODE_TOKENS = [
  "lexical",
  "graph",
  "global",
  "sql",
  "查找",
  "知识问答",
  "综述",
  "真数据库",
] as const;

/** Assert on the WHOLE rendered markup, not on the visible text alone:
 * a raw token in an attribute is a leak too. */
function expectNoInternalMode(html: string): void {
  for (const token of INTERNAL_MODE_TOKENS) {
    expect(html, `the rendered receipt leaks the internal name "${token}"`).not.toContain(
      token,
    );
  }
}

/** A panel state built the way the thread page builds it, so the render
 * covers the composition too. `turns` defaults to one answered turn. */
function state(
  over: Omit<Parameters<typeof taskState>[0], "turns" | "inFlight"> & {
    turns?: readonly Turn[];
    inFlight?: boolean;
  },
): TaskStateView {
  return taskState({
    ...over,
    turns: over.turns ?? [turn()],
    inFlight: over.inFlight ?? false,
  });
}

describe("the five facts render as separate rows", () => {
  it("labels task outcome, turn result, run status and gaps apart", () => {
    // A stopped run is the case that used to collapse: one row tried to
    // say both "the run was stopped" and "this turn is cancelled".
    const html = render(
      state({
        taskRuntime: envelope(),
        turns: [
          turn({ status: "refuse", status_reason: "stopped", answer: "" }),
        ],
      }),
    );
    const rowMap = rows(html);
    expect(rowMap.get("任务")).toBe("进行中");
    expect(rowMap.get("本轮")).toBe("已取消");
    expect(rowMap.get("运行")).toBe("运行已停止");
    expect(rowMap.get("待补")).toBe("Runtime 尚未报告");
    // Archive status is owned by the archive list, so the panel must not
    // claim it here either.
    expect(rowMap.has("归档")).toBe(false);
  });

  it("keeps a finished run and an answered turn as two visible strings", () => {
    const rowMap = rows(render(state({ taskRuntime: envelope() })));
    expect(rowMap.get("本轮")).toBe("已回答");
    expect(rowMap.get("运行")).toBe("运行已结束");
    expect(rowMap.get("任务")).toBe("进行中");
  });
});

describe("every row says something", () => {
  it("renders no blank row value", () => {
    const cases = [
      state({ taskRuntime: envelope({ current_task: currentTask({ outcome: "complete" }), task_outcome: "complete" }) }),
      state({ taskRuntime: undefined, turns: [] }),
      state({ taskRuntime: envelope(), turns: [], projectionUnreadable: true }),
      state({
        taskRuntime: envelope(),
        turns: [],
        savedRestriction: "filing-a",
        effectiveScope: { sourceIds: ["filing-a"], capabilities: [] },
      }),
      state({ taskRuntime: envelope(), turns: [turn({ status: "step_cap", answer: "" })] }),
    ];
    for (const each of cases) {
      for (const [label, value] of rows(render(each))) {
        expect(value, `${label} row`).not.toBe("");
      }
    }
  });
});

describe("an unreadable projection", () => {
  it("renders 未记录（投影无法读取） instead of 尚无任务", () => {
    // A readable projection on a taskless thread keeps its own wording:
    // only an unreadable one says the state is unknown.
    const readable = rows(
      render(
        state({
          taskRuntime: envelope({ current_task: null, task_outcome: null, pending_question: PENDING }),
          turns: [],
        }),
      ),
    );
    const unreadable = rows(
      render(state({ taskRuntime: undefined, turns: [], projectionUnreadable: true })),
    );
    expect(readable.get("任务")).toBe("尚无任务");
    expect(unreadable.get("任务")).toBe("未记录（投影无法读取）");
  });

  it("never becomes a completion claim", () => {
    expect(
      visible(render(state({ taskRuntime: undefined, turns: [], projectionUnreadable: true }))),
    ).not.toContain("已完成");
  });
});

describe("the submission notice row", () => {
  const notice: SubmissionNotice = { state: "pending", text: "已提交，等待 Runtime 受理" };

  it("renders as its own row when given one", () => {
    const rowMap = rows(render(state({ taskRuntime: envelope(), submission: notice })));
    expect(rowMap.get("提交")).toBe("待受理：已提交，等待 Runtime 受理");
  });

  it("renders nothing at all when no notice is given", () => {
    const html = render(state({ taskRuntime: envelope() }));
    expect(rows(html).has("提交")).toBe(false);
    expect(visible(html)).not.toContain("等待 Runtime 受理");
  });
});

describe("source rows stay read-only intent", () => {
  it("states the missing evaluation instead of rendering an intent row", () => {
    // A saved restriction reaches the panel only through the source
    // evaluation, so with none projected there is no 来源限制 row to read
    // as a grant; the 来源 row says the restriction has no current
    // authorization.
    const rowMap = rows(
      render(state({ taskRuntime: envelope(), savedRestriction: "仅年报" })),
    );
    expect(rowMap.has("来源限制")).toBe(false);
    expect(rowMap.get("来源")).toBe("已保存的来源限制还没有当前授权，这次不显示任何可用来源。");
  });

  it("offers no picker: no select, radio or listbox markup", () => {
    const html = render(
      state({
        taskRuntime: envelope(),
        savedRestriction: "filing-a",
        effectiveScope: { sourceIds: ["filing-a"], capabilities: ["search"] },
      }),
    );
    expect(html).not.toMatch(/<(select|input|textarea)\b/);
    expect(html).not.toContain('role="listbox"');
    expect(rows(html).get("已失效")).toBeUndefined();
  });
});

// specs/demo-seed.md item 6 / ADR 0100 decision 6: "Run status, Runtime
// receipts and source evaluation are developer-mode or log-only, hidden
// by default." The panel is hidden AS A WHOLE, not row by row: in the
// default view the operator sees none of it, so the answer's own sources
// list is the only source surface on the page.
describe("the receipt surface is developer-mode only", () => {
  it("renders nothing at all by default — no panel, no rows", () => {
    const html = render(state({ taskRuntime: envelope() }), false);
    expect(html).not.toContain('data-testid="task-panel"');
    expect(rows(html).size).toBe(0);
    expect(html).not.toContain("运行");
  });

  it("carries none of the internal receipt strings into the default view", () => {
    // These are the machine-facing rows named by the requirement: run
    // status, the Runtime submission receipt, the answer-basis row and
    // the source-evaluation row. None may reach a user by default.
    const html = visible(
      render(
        state({
          taskRuntime: envelope(),
          turns: [],
          submission: {
            state: "accepted",
            text: "Runtime 已经接受了这次输入。",
          },
          savedRestriction: "filing-a",
        }),
        false,
      ),
    );
    for (const receipt of [
      "已受理：Runtime 已经接受了这次输入。",
      "Runtime 尚未报告",
      "当前没有可显示的来源评估。",
      "来源限制",
    ]) {
      expect(html, receipt).not.toContain(receipt);
    }
  });

  it("shows the panel in full under developer mode, rows unchanged", () => {
    const hidden = state({
      taskRuntime: envelope(),
      submission: {
        state: "accepted",
        text: "Runtime 已经接受了这次输入。",
      },
    });
    const shown = rows(render(hidden, true));
    expect(shown.get("任务")).toBe("进行中");
    expect(shown.get("本轮")).toBe("已回答");
    expect(shown.get("运行")).toBe("运行已结束");
    expect(shown.get("待补")).toBe("Runtime 尚未报告");
    expect(shown.get("来源")).toBe("当前没有可显示的来源评估。");
    expect(shown.get("提交")).toBe("已受理：Runtime 已经接受了这次输入。");
  });

  it("hides the render only — the row judgement stays intact and reachable", () => {
    // Gating the RENDER must not destroy the values: the pure view
    // module still answers for the very state the default view hides, so
    // the facts remain computable and loggable.
    const hidden = state({ taskRuntime: envelope() });
    expect(runStatusText(hidden)).toBe("运行已结束");
    expect(gapsText(hidden)).toBe("Runtime 尚未报告");
    expect(sourcesText(hidden)).toBe("当前没有可显示的来源评估。");
    expect(taskOutcomeText(hidden)).toBe("进行中");
    expect(submissionText(hidden)).toBeNull();
  });

  it("still honours the taskless rule under developer mode", () => {
    // The gate is a visibility decision, not a replacement for the
    // existing one: a taskless thread renders nothing in EITHER mode.
    const taskless = state({ taskRuntime: undefined, turns: [] });
    expect(render(taskless, true)).not.toContain('data-testid="task-panel"');
    expect(render(taskless, false)).not.toContain('data-testid="task-panel"');
  });

  it("shows the search mode only in developer mode and names old history", () => {
    const selected: SearchModeDecision = {
      search_mode: "global",
      decision_tier: "jev",
      confidence: 0.91,
    };
    const dev = render(state({ taskRuntime: undefined }), true, selected);
    const ordinary = render(state({ taskRuntime: undefined }), false, selected);
    const history = render(state({ taskRuntime: undefined }), true, null);

    expect(dev).toContain('data-testid="search-mode-receipt"');
    // The receipt states PROVENANCE, not the retrieval implementation:
    // the internal 档 name must not reach the DOM, in the text or in an
    // attribute (docs/CONTEXT.md 检索档; spec search-mode-host item 7).
    expect(dev).toContain('data-decision-tier="jev"');
    expect(dev).not.toContain("data-mode=");
    expect(visible(dev)).toContain("检索档决策：判定命中");
    expect(visible(dev)).toContain("决策置信度：0.91");
    expect(ordinary).not.toContain('data-testid="search-mode-receipt"');
    expect(visible(history)).toContain("历史记录未保存检索档决策。");
    expectNoInternalMode(dev);
  });

  it("keeps every internal retrieval name out of the rendered receipt", () => {
    // The raw token is a raw token wherever it sits: the attribute, the
    // visible text, and the judge's own label signal (which names a 档).
    const fallback: SearchModeDecision = {
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: {
        mode_jev_exit: "low_confidence",
        mode_jev_label: "graph",
        mode_jev_confidence: 0.44,
      },
    };
    const dev = render(state({ taskRuntime: undefined }), true, fallback);

    expect(dev).toContain('data-decision-tier="fallback"');
    const text = visible(dev);
    expect(text).toContain("检索档决策：自动回退");
    expect(text).toContain("判定置信度：0.44");
    expect(text).toContain("判定结果：low_confidence");
    expectNoInternalMode(dev);
  });
});

// The developer trace section: spans -> a waterfall; no spans -> the
// receipt/stages plus the trace-disabled hint. Both branches are rendered
// from INJECTED data so the network never decides a test.
const TRACE_ID = "a".repeat(32);

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    trace_id: TRACE_ID,
    span_id: "s1",
    parent_span_id: null,
    name: "span",
    start_time_ms: 1_000,
    duration_ms: 100,
    attributes: {},
    ...overrides,
  };
}

function traceRows(html: string): Array<{
  name: string;
  left: string;
  width: string;
}> {
  const pattern =
    /data-testid="trace-span-row" data-span-name="([^"]*)" data-left-percent="([^"]*)" data-width-percent="([^"]*)"/g;
  return [...html.matchAll(pattern)].map((match) => ({
    name: match[1],
    left: match[2],
    width: match[3],
  }));
}

describe("the developer trace section", () => {
  it("draws one waterfall row per span with its offset and duration width", () => {
    const html = render(state({ taskRuntime: envelope() }), true, undefined, {
      threadId: "thread-1",
      currentTurn: turn(),
      trace: {
        status: "ready",
        traceState: "local",
        spans: [
          span({ span_id: "s1", name: "classify_node", start_time_ms: 1_000, duration_ms: 100 }),
          span({ span_id: "s2", name: "retrieve_tool", start_time_ms: 1_050, duration_ms: 50 }),
          span({ span_id: "s3", name: "model_node", start_time_ms: 1_100, duration_ms: 100 }),
        ],
      },
    });

    expect(html).toContain('data-testid="trace-waterfall"');
    expect(traceRows(html)).toEqual([
      { name: "classify_node", left: "0", width: "50" },
      { name: "retrieve_tool", left: "25", width: "25" },
      { name: "model_node", left: "50", width: "50" },
    ]);
    expect(html.match(/data-testid="trace-span-bar"/g)).toHaveLength(3);
    expect(visible(html)).toContain("100.0 ms");
    expect(visible(html)).toContain("50.0 ms");
    expect(visible(html)).toContain("thread_id=thread-1");
    expect(visible(html)).toContain("turn_id=" + RUN_ID);
    expect(visible(html)).toContain("trace_id=" + TRACE_ID);
  });

  it("falls back to the receipt stages/status and says trace 未开启", () => {
    const html = render(state({ taskRuntime: envelope() }), true, undefined, {
      threadId: "thread-1",
      currentTurn: turn({
        thinking_text: "先查最新财报",
        thinking_duration_ms: 1_000,
        tool_summaries: [
          { name: "retrieve", summary: "命中 1 条", tool_call_id: "c1" },
        ],
      }),
      trace: { status: "ready", traceState: "off", spans: [] },
    });

    expect(html).not.toContain('data-testid="trace-waterfall"');
    expect(html).toContain('data-testid="trace-receipt-fallback"');
    const stages = [...html.matchAll(/data-testid="trace-stage-row" data-stage="([^"]*)"/g)]
      .map((match) => match[1]);
    expect(stages).toEqual([
      "run-status",
      "turn-result",
      "turn-status",
      "thinking-0",
      "tools-1",
    ]);
    const hint = html.match(
      /data-testid="trace-disabled-hint"[^>]*>([^<]*)</,
    );
    expect(hint?.[1]).toBe("trace 未开启");
    const text = visible(html);
    expect(text).toContain("运行已结束");
    expect(text).toContain("已回答");
    expect(text).toContain("1.00 s");
    expect(text).toContain("1 次：retrieve");
    expect(text).not.toContain("错误");
  });

  it("keeps the receipt fallback when the trace read fails, without an error", () => {
    const html = render(state({ taskRuntime: envelope() }), true, undefined, {
      threadId: "thread-1",
      currentTurn: turn(),
      trace: { status: "error" },
    });

    expect(html).toContain('data-testid="trace-receipt-fallback"');
    expect(visible(html)).toContain("trace 状态不可读，显示回执");
    expect(visible(html)).not.toContain("错误");
  });

  it("does not call an enabled-but-empty trace disabled", () => {
    const html = render(state({ taskRuntime: envelope() }), true, undefined, {
      threadId: "thread-1",
      currentTurn: turn(),
      trace: { status: "ready", traceState: "local", spans: [] },
    });

    expect(visible(html)).toContain("trace 已开启，但本轮没有 span");
    expect(visible(html)).not.toContain("trace 未开启");
  });

  it("says an external exporter keeps its spans off this view", () => {
    // console / otlp ARE tracing: the panel must not claim tracing is off
    // just because nothing landed in the local span table.
    const html = render(state({ taskRuntime: envelope() }), true, undefined, {
      threadId: "thread-1",
      currentTurn: turn(),
      trace: { status: "ready", traceState: "external", spans: [] },
    });

    expect(html).toContain('data-testid="trace-receipt-fallback"');
    expect(visible(html)).toContain("trace 已开启（非本地存储）");
    expect(visible(html)).not.toContain("trace 未开启");
  });

  it("renders an unreadable store with the receipt, not an empty-trace claim", () => {
    const html = render(state({ taskRuntime: envelope() }), true, undefined, {
      threadId: "thread-1",
      currentTurn: turn(),
      trace: { status: "ready", traceState: "unreadable", spans: [] },
    });

    expect(html).toContain('data-testid="trace-receipt-fallback"');
    expect(visible(html)).toContain("trace 状态不可读，显示回执");
    expect(visible(html)).not.toContain("trace 已开启，但本轮没有 span");
  });

  it("adds nothing to the ordinary (non-developer) view", () => {
    const html = render(state({ taskRuntime: envelope() }), false, undefined, {
      threadId: "thread-1",
      currentTurn: turn(),
      trace: {
        status: "ready",
        traceState: "local",
        spans: [span({ name: "retrieve_tool" })],
      },
    });

    expect(html).toBe("");
    expect(html).not.toContain('data-testid="trace-panel"');
    expect(html).not.toContain('data-testid="task-panel"');
    expect(visible(html)).not.toContain("trace");
  });
});
