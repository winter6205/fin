// question-workspace W1 (spec specs/question-workspace.md
// §"State shown to the operator", SC-1 / SC-2): the thread surface's
// task / turn / gap / source render decision.
//
// Expected values come from the spec, not the implementation: the frozen
// wire contract (specs/question-task-runtime-contract.md §Ownership and
// §"The two rules every module inherits") is the SSOT for what an
// envelope may and may not say. The web suite has no DOM and collects
// only ``src/**/*.test.ts``, which is why the decision lives in pure
// code (``taskState.ts``).

import { describe, expect, it } from "vitest";
import {
  materialGaps,
  RUN_STATUS_LABEL,
  runStatus,
  sourceEvaluation,
  SUBMISSION_STATE_LABEL,
  taskState,
  TASK_OUTCOME_LABEL,
  TURN_RESULT_LABEL,
  turnResult,
} from "./taskState";
import type {
  CurrentTask,
  PendingQuestionView,
  RunStatus,
  TaskRuntimeView,
  Turn,
} from "../../types";

const RUN_ID = "33333333-3333-4333-8333-333333333333";
const TASK_ID = "task-1";

/** A terminal or in-flight turn carrying an answer by default. */
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
    task_id: TASK_ID,
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

/** The thread-scoped envelope as Runtime projects it. */
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

const PENDING: PendingQuestionView = {
  question_id: "q-1",
  task_id: TASK_ID,
  task_version: 1,
  prompt: "要查哪一季度的营收？",
  choices: ["最新季度", "上一财年", "other"],
  input_version: 1,
};

describe("turnResult", () => {
  it("is running while work is in flight, and never a completion", () => {
    const result = turnResult({
      turns: [turn()],
      inFlight: true,
      pendingQuestion: null,
    });
    expect(result).toBe("running");
    // A running turn must not be read as the task or turn being done.
    expect(result).not.toBe("answer");
    expect(result).not.toBe("partial");
  });

  it("stays running even when the latest turn already carries an answer", () => {
    // The turn has answer prose, but the run is still open: work in
    // flight wins over the finished-turn classification.
    expect(
      turnResult({
        turns: [turn({ status: "done", answer: "半段草稿" })],
        inFlight: true,
        pendingQuestion: null,
      }),
    ).toBe("running");
  });

  it("classifies a done turn carrying answer text as answer", () => {
    expect(
      turnResult({
        turns: [turn({ status: "done", answer: "391.0 亿美元" })],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("answer");
  });

  it("lets a pending clarification override a finished turn", () => {
    expect(
      turnResult({
        turns: [turn({ status: "done", answer: "391.0 亿美元" })],
        inFlight: false,
        pendingQuestion: PENDING,
      }),
    ).toBe("clarification");
  });

  it("keeps a pending clarification as clarification while work is active", () => {
    // A thread waiting on the operator is not "running".
    expect(
      turnResult({
        turns: [turn()],
        inFlight: true,
        pendingQuestion: PENDING,
      }),
    ).toBe("clarification");
  });

  it("reads a step_cap turn as partial", () => {
    expect(
      turnResult({
        turns: [turn({ status: "step_cap", answer: "" })],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("partial");
  });

  it("reads a refuse turn with no answer text as failure", () => {
    expect(
      turnResult({
        turns: [
          turn({
            status: "refuse",
            answer: "",
            assistant_message: "未能确认答案。",
          }),
        ],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("failure");
  });

  it("reads a done turn carrying a stream error and no answer as failure", () => {
    expect(
      turnResult({
        turns: [turn({ status: "done", answer: "", status_classified: "timeout" })],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("failure");
  });

  it("reads an operator stop as cancelled, not failure", () => {
    // The host lands a stop on refuse with status_reason "stopped"
    // (workspace_http STOPPED_STATUS_REASON) — a deliberate action, so
    // it must not read as a failure.
    expect(
      turnResult({
        turns: [
          turn({
            status: "refuse",
            status_reason: "stopped",
            answer: "",
            assistant_message: "本轮已停止。可以直接再发一条。",
          }),
        ],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("cancelled");
  });

  it("is none when there are no turns", () => {
    expect(
      turnResult({ turns: [], inFlight: false, pendingQuestion: null }),
    ).toBe("none");
  });

  it("does not read the stream-error taxonomy as an operator stop", () => {
    // ``status_classified`` is the stream-error taxonomy and cannot carry
    // a stop (see the STOP_REASON_TOKENS comment): only what run control
    // reports on ``status_reason`` may read as cancelled. The value below
    // is deliberately outside the wire enum to pin that a synonym landing
    // on the classification field stays a failure rather than silently
    // becoming an operator action.
    const hostile = { status_classified: "cancel_requested" } as unknown as Partial<
      Turn
    >;
    expect(
      turnResult({
        turns: [turn({ status: "refuse", answer: "", ...hostile })],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("failure");
  });

  it("does not call a done turn with insufficient evidence a complete answer", () => {
    expect(
      turnResult({
        turns: [
          turn({
            status: "done",
            answer: "只有部分数字",
            semantic_outcome: "insufficient_evidence",
          }),
        ],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("partial");
  });

  it("reads a stop only from what run control reports", () => {
    expect(
      turnResult({
        turns: [turn({ status: "refuse", status_reason: "user_stopped", answer: "" })],
        inFlight: false,
        pendingQuestion: null,
      }),
    ).toBe("cancelled");
  });
});

describe("runStatus", () => {
  it("is a distinct fact from the turn result for the same row", () => {
    const turns = [turn({ status: "done", answer: "391.0 亿美元" })];
    expect(runStatus({ turns, inFlight: false })).toBe("done");
    expect(turnResult({ turns, inFlight: false, pendingQuestion: null })).toBe("answer");
  });

  it("is running while a stream is open, even with a finished last turn", () => {
    expect(runStatus({ turns: [turn()], inFlight: true })).toBe("running");
  });

  it("reports the wire status of the latest run verbatim", () => {
    const statuses: RunStatus[] = ["done", "refuse", "step_cap"];
    for (const status of statuses) {
      expect(runStatus({ turns: [turn({ status })], inFlight: false })).toBe(status);
    }
  });

  it("reads an operator stop as stopped, not as a plain refusal", () => {
    expect(
      runStatus({
        turns: [turn({ status: "refuse", status_reason: "stopped", answer: "" })],
        inFlight: false,
      }),
    ).toBe("stopped");
  });

  it("is none when no run has been started", () => {
    expect(runStatus({ turns: [], inFlight: false })).toBe("none");
  });

  it("never uses task-completion wording (SC-1)", () => {
    // The run's own status must not read as the task being complete: a run
    // ending is a turn/run fact, never a task outcome.
    for (const label of Object.values(RUN_STATUS_LABEL)) {
      expect(label).not.toContain("已完成");
    }
  });
});

describe("materialGaps", () => {
  it("is not reported for an absent envelope", () => {
    expect(materialGaps(undefined)).toEqual({ reported: false, gaps: [] });
  });

  it("is not reported for a present envelope, and never claims 'no gaps'", () => {
    const result = materialGaps(envelope());
    expect(result).toEqual({ reported: false, gaps: [] });
    // Explicitly NOT a reported empty list: absence of a report is
    // unknown, not an assertion that the task has no unresolved gaps.
    expect(result.reported).toBe(false);
    expect(result).not.toEqual({ reported: true, gaps: [] });
  });
});

describe("sourceEvaluation", () => {
  it("is unavailable with no evaluation and no saved restriction", () => {
    expect(
      sourceEvaluation({ savedRestriction: null, effectiveScope: null }),
    ).toEqual({ available: false, reason: "not_evaluated" });
  });

  it("is unavailable but names the missing evaluation when a restriction exists", () => {
    expect(
      sourceEvaluation({ savedRestriction: "filing-a", effectiveScope: undefined }),
    ).toEqual({ available: false, reason: "evaluation_unavailable" });
  });

  it("can never be available true without a fresh evaluation", () => {
    // A saved restriction alone is user intent, never authority.
    expect(
      sourceEvaluation({ savedRestriction: "filing-a,filing-b", effectiveScope: null })
        .available,
    ).toBe(false);
  });

  it("treats empty capabilities as available, not unavailable", () => {
    expect(
      sourceEvaluation({
        savedRestriction: null,
        effectiveScope: { sourceIds: ["filing-a"], capabilities: [] },
      }),
    ).toEqual({
      available: true,
      restrictionIntent: "",
      effectiveSourceIds: ["filing-a"],
      capabilities: [],
      staleIntentIds: [],
    });
  });

  it("reports an intent id outside the grant as stale intent, never as a grant", () => {
    const result = sourceEvaluation({
      savedRestriction: "filing-a, filing-z",
      effectiveScope: { sourceIds: ["filing-a"], capabilities: ["search"] },
    });
    expect(result.available).toBe(true);
    if (!result.available) throw new Error("expected an available evaluation");
    expect(result.staleIntentIds).toEqual(["filing-z"]);
    // The stale intent is NOT promoted into the effective source set.
    expect(result.effectiveSourceIds).toEqual(["filing-a"]);
    expect(result.restrictionIntent).toBe("filing-a, filing-z");
  });
});

describe("taskState", () => {
  it("reads the task outcome and goal from the envelope", () => {
    const view = taskState({
      taskRuntime: envelope({
        current_task: currentTask({ outcome: "complete", outcome_reason: "已带出处回答" }),
        task_outcome: "complete",
      }),
      turns: [turn()],
      inFlight: false,
    });
    expect(view.hasTask).toBe(true);
    expect(view.taskOutcome).toBe("complete");
    expect(view.goal).toBe("查明最新一季营收");
    expect(view.expectedResult).toBe("带出处的营收数字");
    expect(view.taskOutcomeReason).toBe("已带出处回答");
  });

  it("is taskless with a null outcome and no synthesized goal", () => {
    const view = taskState({
      taskRuntime: undefined,
      turns: [turn({ question: "营收多少？" })],
      inFlight: false,
    });
    expect(view.hasTask).toBe(false);
    expect(view.taskOutcome).toBeNull();
    expect(view.taskOutcomeReason).toBe("");
    // Never synthesized from a turn's question text.
    expect(view.goal).toBe("");
    expect(view.expectedResult).toBe("");
  });

  it("never infers task completion from a run that ended with an answer (SC-1)", () => {
    // The run produced an answer and is no longer in flight, but Runtime
    // has not moved the task off "active": the surface must keep showing
    // "active". A run ending is a turn fact, not a task fact.
    const view = taskState({
      taskRuntime: envelope({ task_outcome: "active" }),
      turns: [turn({ status: "done", answer: "391.0 亿美元" })],
      inFlight: false,
    });
    expect(view.turnResult).toBe("answer");
    expect(view.taskOutcome).toBe("active");
    expect(view.taskOutcome).not.toBe("complete");
  });

  it("falls back to the task record's outcome when the envelope level omits one", () => {
    const view = taskState({
      taskRuntime: envelope({
        current_task: currentTask({ outcome: "cancelled" }),
        task_outcome: null,
      }),
      turns: [],
      inFlight: false,
    });
    expect(view.taskOutcome).toBe("cancelled");
  });

  it("composes the source evaluation from the restriction and fresh scope", () => {
    const view = taskState({
      taskRuntime: envelope(),
      turns: [],
      inFlight: false,
      savedRestriction: "filing-a",
      effectiveScope: { sourceIds: [], capabilities: [] },
    });
    expect(view.sources).toEqual({
      available: true,
      restrictionIntent: "filing-a",
      effectiveSourceIds: [],
      capabilities: [],
      staleIntentIds: ["filing-a"],
    });
  });

  it("shows no task panel for a bare taskless thread", () => {
    const view = taskState({
      taskRuntime: undefined,
      turns: [],
      inFlight: false,
    });
    expect(view.hasTask).toBe(false);
    expect(view.showTaskPanel).toBe(false);
  });

  it("shows the task panel when a task exists", () => {
    expect(
      taskState({ taskRuntime: envelope(), turns: [], inFlight: false }).showTaskPanel,
    ).toBe(true);
  });

  it("shows the task panel when a pending question exists without a task", () => {
    const view = taskState({
      taskRuntime: envelope({ current_task: null, task_outcome: null, pending_question: PENDING }),
      turns: [],
      inFlight: false,
    });
    expect(view.hasTask).toBe(false);
    expect(view.showTaskPanel).toBe(true);
    // A pending question on a taskless envelope still reads clarification.
    expect(view.turnResult).toBe("clarification");
  });

  it("exposes the run status as its own fact beside the turn result", () => {
    const view = taskState({
      taskRuntime: envelope(),
      turns: [turn({ status: "done", answer: "391.0 亿美元" })],
      inFlight: false,
    });
    expect(view.runStatus).toBe("done");
    expect(view.turnResult).toBe("answer");
    expect(view.taskOutcome).toBe("active");
  });
});

describe("taskState with an unreadable projection", () => {
  it("defaults to readable when the caller passes nothing", () => {
    const view = taskState({ taskRuntime: undefined, turns: [], inFlight: false });
    expect(view.projectionUnreadable).toBe(false);
  });

  it("carries the unreadable fact and still claims no task outcome", () => {
    // A malformed projection is unknown, never a completion: the outcome
    // stays null and no goal is synthesized.
    const view = taskState({
      taskRuntime: undefined,
      turns: [turn()],
      inFlight: false,
      projectionUnreadable: true,
    });
    expect(view.projectionUnreadable).toBe(true);
    expect(view.hasTask).toBe(false);
    expect(view.taskOutcome).toBeNull();
    expect(view.goal).toBe("");
    expect(view.taskOutcome).not.toBe("complete");
  });

  it("opens the panel so an unreadable projection is visible, not silent", () => {
    expect(
      taskState({
        taskRuntime: undefined,
        turns: [],
        inFlight: false,
        projectionUnreadable: true,
      }).showTaskPanel,
    ).toBe(true);
  });
});

describe("taskState submission notice", () => {
  const notice = { state: "pending", text: "已提交，等待 Runtime 受理" } as const;

  it("stays empty when no notice is given (backward-compatible call)", () => {
    const view = taskState({ taskRuntime: envelope(), turns: [], inFlight: false });
    expect(view.submission ?? null).toBeNull();
  });

  it("passes the given notice through verbatim", () => {
    const view = taskState({
      taskRuntime: envelope(),
      turns: [],
      inFlight: false,
      submission: notice,
    });
    expect(view.submission).toEqual({ state: "pending", text: "已提交，等待 Runtime 受理" });
  });

  it("opens the panel for a submission on a taskless thread", () => {
    // The operator is owed the outcome of what they just sent; a
    // submission is not a task fact, so it opens the notice row without
    // claiming a task exists.
    const view = taskState({
      taskRuntime: undefined,
      turns: [],
      inFlight: false,
      submission: notice,
    });
    expect(view.hasTask).toBe(false);
    expect(view.showTaskPanel).toBe(true);
  });
});

describe("taskState saved restriction intent", () => {
  it("states a saved restriction as an evaluation that is not available", () => {
    // The intent rides the source evaluation and nowhere else: no second
    // copy of the saved string is projected onto the view, so there is
    // nothing for a surface to promote into a grant by accident.
    const view = taskState({
      taskRuntime: envelope(),
      turns: [],
      inFlight: false,
      savedRestriction: "filing-a",
    });
    expect(view.sources).toEqual({
      available: false,
      reason: "evaluation_unavailable",
    });
  });

  it("reports no evaluation at all when nothing is saved", () => {
    const view = taskState({
      taskRuntime: envelope(),
      turns: [],
      inFlight: false,
    });
    expect(view.sources).toEqual({ available: false, reason: "not_evaluated" });
  });
});

describe("copy constants", () => {
  it("labels every task outcome and turn result in Chinese", () => {
    expect(TASK_OUTCOME_LABEL.active).toBe("进行中");
    expect(TASK_OUTCOME_LABEL.complete).toBe("已完成");
    expect(TASK_OUTCOME_LABEL.cancelled).toBe("已取消");
    expect(TURN_RESULT_LABEL.failure).toBe("未完成");
    expect(TURN_RESULT_LABEL.answer).toBe("已回答");
    expect(TURN_RESULT_LABEL.clarification).toBe("待澄清");
    expect(TURN_RESULT_LABEL.partial).toBe("部分回答");
    expect(TURN_RESULT_LABEL.cancelled).toBe("已取消");
    expect(TURN_RESULT_LABEL.running).toBe("运行中");
    expect(TURN_RESULT_LABEL.none).toBe("尚未开始");
  });

  it("labels every run status and submission state with non-empty copy", () => {
    // An unmapped union member must never reach the panel as an empty
    // row, so the maps are exhaustive by type and checked non-empty here.
    expect(Object.keys(RUN_STATUS_LABEL)).toEqual([
      "none",
      "running",
      "done",
      "refuse",
      "step_cap",
      "stopped",
    ]);
    expect(Object.keys(SUBMISSION_STATE_LABEL).sort()).toEqual(
      ["accepted", "failed", "pending", "rejected"].sort(),
    );
    for (const label of Object.values(RUN_STATUS_LABEL)) expect(label.length).toBeGreaterThan(0);
    for (const label of Object.values(SUBMISSION_STATE_LABEL)) {
      expect(label.length).toBeGreaterThan(0);
    }
  });
});
