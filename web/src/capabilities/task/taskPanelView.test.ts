// question-workspace W1: the TaskPanel row VALUES (specs/question-workspace.md
// §"State shown to the operator"; SC-1, SC-2).
//
// These decisions used to be inline JSX in ``TaskPanel.tsx`` — untestable
// because the suite has no DOM and never collects ``.tsx``. Extracted to
// this pure module they are pinned here directly; expected values come
// from the spec sentences (absent gap report ≠ "no gaps"; saved
// restriction = intent, never grant), not from the implementation.

import { describe, expect, it } from "vitest";
import type { SourceEvaluationReason, TaskStateView } from "./taskState";
import {
  gapsText,
  restrictionIntentText,
  runStatusText,
  sourcesText,
  staleIntentText,
  submissionText,
  taskOutcomeText,
} from "./taskPanelView";

const BASE: TaskStateView = {
  hasTask: true,
  goal: "查明营收",
  expectedResult: "带出处的数字",
  taskOutcome: "active",
  taskOutcomeReason: "",
  turnResult: "answer",
  runStatus: "done",
  projectionUnreadable: false,
  gaps: { reported: false, gaps: [] },
  sources: {
    available: false,
    reason: "not_evaluated",
  },
  showTaskPanel: true,
};

const view = (over: Partial<TaskStateView>): TaskStateView => ({
  ...BASE,
  ...over,
});

describe("taskOutcomeText", () => {
  it("says 尚无任务 on a taskless thread, never a synthesized outcome", () => {
    expect(taskOutcomeText(view({ taskOutcome: null }))).toBe("尚无任务");
  });

  it("labels the envelope's outcome verbatim for all three tokens", () => {
    expect(taskOutcomeText(view({ taskOutcome: "active" }))).toBe("进行中");
    expect(taskOutcomeText(view({ taskOutcome: "complete" }))).toBe("已完成");
    expect(taskOutcomeText(view({ taskOutcome: "cancelled" }))).toBe("已取消");
  });

  it("says 未记录/无法读取 for an unreadable projection, not 尚无任务", () => {
    // A malformed projection is unknown state, so it must not render as
    // "there is no task yet" — and it must never become a completion.
    const text = taskOutcomeText(
      view({ taskOutcome: null, projectionUnreadable: true }),
    );
    expect(text).toBe("未记录（投影无法读取）");
    expect(text).not.toBe("尚无任务");
    expect(text).not.toContain("已完成");
  });

  it("still shows a reported outcome when the projection also flags unreadable", () => {
    expect(
      taskOutcomeText(view({ taskOutcome: "active", projectionUnreadable: true })),
    ).toBe("进行中");
  });
});

describe("gapsText", () => {
  it("states an ABSENT report as such (silence is not 'no gaps')", () => {
    expect(gapsText(view({ gaps: { reported: false, gaps: [] } }))).toBe(
      "Runtime 尚未报告",
    );
  });

  it("says 无 for a reported-but-empty list (an explicit report)", () => {
    expect(gapsText(view({ gaps: { reported: true, gaps: [] } }))).toBe("无");
  });

  it("joins reported gaps with the ；separator", () => {
    expect(
      gapsText(view({ gaps: { reported: true, gaps: ["缺 2023 年报", "缺季报"] } })),
    ).toBe("缺 2023 年报；缺季报");
  });
});

describe("sourcesText", () => {
  it("renders the unavailable reason copy for both refusal reasons", () => {
    expect(sourcesText(view({ sources: { available: false, reason: "not_evaluated" } }))).toBe(
      "当前没有可显示的来源评估。",
    );
    expect(
      sourcesText(
        view({ sources: { available: false, reason: "evaluation_unavailable" } }),
      ),
    ).toBe("已保存的来源限制还没有当前授权，这次不显示任何可用来源。");
  });

  it("states empty scope and empty capabilities explicitly (honest 'nothing feasible')", () => {
    expect(
      sourcesText(
        view({
          sources: {
            available: true,
            restrictionIntent: "",
            effectiveSourceIds: [],
            capabilities: [],
            staleIntentIds: [],
          },
        }),
      ),
    ).toBe("当前没有可用来源；当前没有可执行的操作");
  });

  it("joins the effective sources and capabilities when granted", () => {
    expect(
      sourcesText(
        view({
          sources: {
            available: true,
            restrictionIntent: "filing-a",
            effectiveSourceIds: ["filing-a", "filing-b"],
            capabilities: ["search"],
            staleIntentIds: [],
          },
        }),
      ),
    ).toBe("filing-a、filing-b；能力：search");
  });
});

describe("restrictionIntentText", () => {
  it("is null without an available evaluation (intent never shown as a grant)", () => {
    expect(restrictionIntentText(BASE)).toBeNull();
  });

  it("is null when the evaluation is available but no restriction is saved", () => {
    expect(
      restrictionIntentText(
        view({
          sources: {
            available: true,
            restrictionIntent: "",
            effectiveSourceIds: ["filing-a"],
            capabilities: [],
            staleIntentIds: [],
          },
        }),
      ),
    ).toBeNull();
  });

  it("shows the restriction annotated as intent, not authorization", () => {
    const text = restrictionIntentText(
      view({
        sources: {
          available: true,
          restrictionIntent: "仅年报",
          effectiveSourceIds: ["filing-a"],
          capabilities: [],
          staleIntentIds: [],
        },
      }),
    );
    expect(text).toBe("仅年报（用户意图，不代表当前授权）");
  });
});

describe("staleIntentText", () => {
  it("is null when no saved intent went ungranted", () => {
    expect(
      staleIntentText(
        view({
          sources: {
            available: true,
            restrictionIntent: "filing-a",
            effectiveSourceIds: ["filing-a"],
            capabilities: [],
            staleIntentIds: [],
          },
        }),
      ),
    ).toBeNull();
  });

  it("lists ungranted intent ids annotated as not currently authorized", () => {
    const text = staleIntentText(
      view({
        sources: {
          available: true,
          restrictionIntent: "filing-a, filing-z",
          effectiveSourceIds: ["filing-a"],
          capabilities: [],
          staleIntentIds: ["filing-z"],
        },
      }),
    );
    expect(text).toBe("filing-z（当前权限下不可用）");
  });

  it("is null when the evaluation is unavailable (nothing to compare)", () => {
    expect(staleIntentText(BASE)).toBeNull();
  });
});

describe("runStatusText", () => {
  it("renders its own row value, distinct from the turn result", () => {
    // The run ended, the turn answered, the task is still active: three
    // separate facts, three separate strings.
    const state = view({ runStatus: "done", turnResult: "answer", taskOutcome: "active" });
    expect(runStatusText(state)).toBe("运行已结束");
    expect(runStatusText(state)).not.toBe(taskOutcomeText(state));
  });

  it("labels every run status with non-empty copy", () => {
    const statuses = ["none", "running", "done", "refuse", "step_cap", "stopped"] as const;
    for (const runStatus of statuses) {
      expect(runStatusText(view({ runStatus })).length).toBeGreaterThan(0);
    }
    expect(runStatusText(view({ runStatus: "stopped" }))).toBe("运行已停止");
    expect(runStatusText(view({ runStatus: "none" }))).toBe("尚无运行");
  });

  it("never words the run as a completed task", () => {
    for (const runStatus of ["done", "refuse", "step_cap", "stopped"] as const) {
      expect(runStatusText(view({ runStatus }))).not.toContain("已完成");
    }
  });
});

describe("submissionText", () => {
  it("is null when no notice is given (the row renders nothing)", () => {
    expect(submissionText(BASE)).toBeNull();
    expect(submissionText(view({ submission: null }))).toBeNull();
  });

  it("labels the notice state and keeps the owner's text", () => {
    expect(
      submissionText(view({ submission: { state: "accepted", text: "Runtime 已受理" } })),
    ).toBe("已受理：Runtime 已受理");
  });

  it("renders the state label alone when the text is empty (no blank row)", () => {
    expect(submissionText(view({ submission: { state: "pending", text: "" } }))).toBe("待受理");
  });

  it("labels every submission state distinctly", () => {
    const states = ["pending", "accepted", "rejected", "failed"] as const;
    const labels = states.map((state) => submissionText(view({ submission: { state, text: "" } })));
    expect(new Set(labels).size).toBe(states.length);
    for (const label of labels) expect(label).not.toBeNull();
  });
});

describe("restrictionIntentText without a fresh evaluation", () => {
  it("states the evaluation's absence instead of a second copy of the intent", () => {
    // The saved restriction reaches the panel through the source
    // evaluation and nowhere else, so with no evaluation there is no
    // intent row to mistake for one — the 来源 row says why.
    const state = view({ sources: { available: false, reason: "evaluation_unavailable" } });
    expect(restrictionIntentText(state)).toBeNull();
    expect(sourcesText(state)).toBe(
      "已保存的来源限制还没有当前授权，这次不显示任何可用来源。",
    );
  });

  it("never promotes the saved intent into an available source", () => {
    const state = view({ sources: { available: false, reason: "not_evaluated" } });
    expect(state.sources.available).toBe(false);
    expect(sourcesText(state)).toBe("当前没有可显示的来源评估。");
  });
});

describe("unmapped union members never render blank", () => {
  it("gives every source-evaluation reason its own copy", () => {
    const reasons: SourceEvaluationReason[] = ["evaluation_unavailable", "not_evaluated"];
    for (const reason of reasons) {
      expect(sourcesText(view({ sources: { available: false, reason } }))).not.toBe("");
    }
  });
});
