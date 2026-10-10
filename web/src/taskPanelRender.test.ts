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

import TaskPanel from "./components/TaskPanel";
import { taskState, type SubmissionNotice, type TaskStateView } from "./taskState";
import type {
  CurrentTask,
  PendingQuestionView,
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

function render(state: TaskStateView): string {
  return renderToString(createElement(TaskPanel, { state }));
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
