// W4 read-side decoding of the ``task_runtime`` projection
// (specs/question-task-runtime-contract.md §Representation, §Inventory
// outcome). These tests pin the promised behaviour of the web task-
// runtime gate INDEPENDENTLY of the implementation: the valid fixture is
// authored from the contract's own JSON shape, and every refusal asserts
// the exact unrecorded reading the contract promises — never a
// fabricated field and never a half-built envelope shown as real.

import { describe, expect, it } from "vitest";
import {
  EMPTY_TASK_RUNTIME,
  TASK_RUNTIME_REFUSAL_COPY,
  decodeTaskRuntime,
  decodeThreadTaskRuntime,
  isTaskless,
  parseAcceptedInput,
  parseArchivedTask,
  parseCurrentTask,
  parsePendingQuestion,
  parseReconciliation,
  parseTaskOutcome,
} from "./taskRuntime";
import type { TaskRuntimeView } from "./types";

// ----- fixtures, straight from the contract's JSON shape -----

/** A pending clarification; ``choices`` already ends with the host's
 * single trailing ``"other"`` (contract §Representation + §Closed
 * vocabularies "Other choice"). */
const PENDING_QUESTION = {
  question_id: "q-1",
  task_id: "task-1",
  task_version: 2,
  prompt: "请选择要对比的会计口径：合并报表还是母公司报表？",
  choices: ["合并报表", "母公司报表", "other"],
  input_version: 1,
};

/** A flattened archive row (``archived_task_summaries`` wire shape). */
const ARCHIVE = {
  archive_id: "archive-1",
  task_id: "task-0",
  goal: "先看 2023 年报",
  task_version: 1,
  archived_at_input_version: 1,
  run_id: null,
  evidence_refs: [],
};

const RECONCILIATION = {
  reconciled_input_version: 3,
  task_id: "task-1",
  task_version: 2,
  disposition: "continue",
  reason: "同一目标下的进度补充",
};

/** One input-history row whose original was NEVER recoverable. */
const ROW_NULL_PAYLOAD = {
  input_version: 2,
  kind: "restore",
  payload: null,
  provenance: "restore_door",
  question_id: null,
  archive_id: "archive-1",
  choice_id: null,
  run_id: null,
} as const;

const ROW_MESSAGE = {
  input_version: 3,
  kind: "reply",
  payload: "合并报表。",
  provenance: "reply_door",
  question_id: "q-1",
  archive_id: null,
  choice_id: "mer",
  run_id: "run-1",
} as const;

/** A complete, valid envelope authored from the contract's wire shape. */
const VALID_ENVELOPE = {
  current_task: {
    task_id: "task-1",
    goal: "对比 A、B 两家公司 2023 年营收",
    expected_result: "一份带出处的对比表",
    task_version: 2,
    necessary_conditions: ["两家公司都已在资料库"],
    outcome: "active",
    outcome_reason: "",
    created_at_input_version: 1,
    pending_question: PENDING_QUESTION,
    task_outcome: "active",
  },
  archived_tasks: [ARCHIVE],
  pending_question: PENDING_QUESTION,
  reconciliation: RECONCILIATION,
  task_outcome: "active",
  input_version: 3,
  input_history: [ROW_NULL_PAYLOAD, ROW_MESSAGE],
};

const EXPECTED_VIEW: TaskRuntimeView = {
  current_task: {
    task_id: "task-1",
    goal: "对比 A、B 两家公司 2023 年营收",
    expected_result: "一份带出处的对比表",
    task_version: 2,
    necessary_conditions: ["两家公司都已在资料库"],
    outcome: "active",
    outcome_reason: "",
    created_at_input_version: 1,
    pending_question: PENDING_QUESTION,
    task_outcome: "active",
  },
  archived_tasks: [ARCHIVE],
  pending_question: PENDING_QUESTION,
  reconciliation: RECONCILIATION,
  task_outcome: "active",
  input_version: 3,
  input_history: [ROW_NULL_PAYLOAD, ROW_MESSAGE],
};

describe("decodeTaskRuntime — absence vs refusal", () => {
  it("reads an ABSENT key as the empty envelope with NO refusal", () => {
    const decoded = decodeThreadTaskRuntime({});
    expect(decoded.view).toEqual(EMPTY_TASK_RUNTIME);
    expect(decoded.refusal).toBeNull();
  });

  it("reads an explicit undefined the same way: empty, no refusal", () => {
    const decoded = decodeTaskRuntime(undefined);
    expect(decoded.view).toEqual(EMPTY_TASK_RUNTIME);
    expect(decoded.refusal).toBeNull();
    // The legacy-thread case is specifically NOT a refusal.
    expect(decoded.refusal).not.toBe("malformed_task_runtime");
  });

  it("reads a null task_runtime value as empty, no refusal", () => {
    const decoded = decodeThreadTaskRuntime({ task_runtime: null });
    expect(decoded.view).toEqual(EMPTY_TASK_RUNTIME);
    expect(decoded.refusal).toBeNull();
  });

  it("reads a null / undefined body as empty, no refusal", () => {
    expect(decodeThreadTaskRuntime(null)).toEqual({
      view: EMPTY_TASK_RUNTIME,
      refusal: null,
    });
    expect(decodeThreadTaskRuntime(undefined)).toEqual({
      view: EMPTY_TASK_RUNTIME,
      refusal: null,
    });
  });
});

describe("decodeTaskRuntime — a valid envelope decodes verbatim", () => {
  it("decodes every field verbatim, including a payload:null row", () => {
    const decoded = decodeTaskRuntime(VALID_ENVELOPE);
    expect(decoded.refusal).toBeNull();
    expect(decoded.view).toEqual(EXPECTED_VIEW);
  });

  it("keeps the trailing Other choice and never adds a second", () => {
    const decoded = decodeTaskRuntime(VALID_ENVELOPE);
    expect(decoded.view.pending_question?.choices).toEqual([
      "合并报表",
      "母公司报表",
      "other",
    ]);
    expect(
      decoded.view.pending_question?.choices.filter((c) => c === "other"),
    ).toHaveLength(1);
  });

  it("carries an unrecoverable original as payload:null, never reconstructed", () => {
    const decoded = decodeTaskRuntime(VALID_ENVELOPE);
    expect(decoded.view.input_history[0].payload).toBeNull();
  });
});

describe("decodeTaskRuntime — a present-but-wrong shape refuses", () => {
  it("refuses a malformed current_task with an EMPTY view (no half-build)", () => {
    const decoded = decodeTaskRuntime({ current_task: 42 });
    expect(decoded.refusal).toBe("malformed_task_runtime");
    expect(decoded.view).toEqual(EMPTY_TASK_RUNTIME);
  });

  it("refuses a non-object envelope (array / string / number)", () => {
    for (const bad of [[], "task_runtime", 3, true]) {
      const decoded = decodeTaskRuntime(bad);
      expect(decoded.refusal).toBe("malformed_task_runtime");
      expect(decoded.view).toEqual(EMPTY_TASK_RUNTIME);
    }
  });

  it("refuses a malformed pending_question and reads it as absent", () => {
    const decoded = decodeTaskRuntime({
      pending_question: { question_id: "q", task_id: "t", prompt: "", choices: ["a"] },
    });
    expect(decoded.refusal).toBe("malformed_task_runtime");
    expect(decoded.view.pending_question).toBeNull();
  });

  it("does not leak a wrong-typed input_version through as a number", () => {
    const decoded = decodeTaskRuntime({ input_version: "3" });
    expect(decoded.refusal).toBe("malformed_task_runtime");
    expect(decoded.view.input_version).toBe(0);
    expect(decoded.view.input_version).not.toBe(3);
  });

  it("refuses a malformed reconciliation and reads it as absent", () => {
    const decoded = decodeTaskRuntime({ reconciliation: { disposition: "switch" } });
    expect(decoded.refusal).toBe("malformed_task_runtime");
    expect(decoded.view.reconciliation).toBeNull();
  });
});

describe("decodeTaskRuntime — one bad element among good rows", () => {
  it("keeps the good archive rows and records the refusal", () => {
    const decoded = decodeTaskRuntime({
      archived_tasks: [ARCHIVE, { archive_id: "", task_id: "task-x" }],
    });
    expect(decoded.refusal).toBe("malformed_task_runtime");
    expect(decoded.view.archived_tasks).toHaveLength(1);
    expect(decoded.view.archived_tasks[0]).toEqual(ARCHIVE);
  });

  it("keeps the good input-history rows and records the refusal", () => {
    const decoded = decodeTaskRuntime({
      input_history: [ROW_MESSAGE, { input_version: 1, kind: "telepathy" }],
    });
    expect(decoded.refusal).toBe("malformed_task_runtime");
    expect(decoded.view.input_history).toHaveLength(1);
    expect(decoded.view.input_history[0]).toEqual(ROW_MESSAGE);
  });
});

describe("parseTaskOutcome", () => {
  it("accepts exactly the three frozen tokens", () => {
    expect(parseTaskOutcome("active")).toBe("active");
    expect(parseTaskOutcome("complete")).toBe("complete");
    expect(parseTaskOutcome("cancelled")).toBe("cancelled");
  });

  it("refuses an unknown token and never widens it to active", () => {
    expect(parseTaskOutcome("bogus")).toBeNull();
    expect(parseTaskOutcome("bogus")).not.toBe("active");
    expect(parseTaskOutcome("")).toBeNull();
    expect(parseTaskOutcome(null)).toBeNull();
  });
});

describe("parsePendingQuestion", () => {
  it("accepts the contract's own example shape", () => {
    expect(parsePendingQuestion(PENDING_QUESTION)).toEqual(PENDING_QUESTION);
  });

  it("refuses a blank prompt (the spec requires the COMPLETE question)", () => {
    expect(parsePendingQuestion({ ...PENDING_QUESTION, prompt: "   " })).toBeNull();
    expect(parsePendingQuestion({ ...PENDING_QUESTION, prompt: "" })).toBeNull();
  });

  it("refuses a missing or empty choices list", () => {
    const { choices: _drop, ...noChoices } = PENDING_QUESTION;
    expect(parsePendingQuestion(noChoices)).toBeNull();
    expect(parsePendingQuestion({ ...PENDING_QUESTION, choices: [] })).toBeNull();
  });

  it("refuses a choices list carrying a blank entry", () => {
    expect(
      parsePendingQuestion({ ...PENDING_QUESTION, choices: ["合并报表", " "] }),
    ).toBeNull();
  });

  it("defaults a missing task_version to 1 and input_version to 0", () => {
    const { task_version: _t, input_version: _i, ...rest } = PENDING_QUESTION;
    const decoded = parsePendingQuestion(rest);
    expect(decoded?.task_version).toBe(1);
    expect(decoded?.input_version).toBe(0);
  });

  it("refuses a present non-integer counter rather than defaulting it", () => {
    expect(parsePendingQuestion({ ...PENDING_QUESTION, task_version: "2" })).toBeNull();
  });
});

describe("parseAcceptedInput", () => {
  it("accepts a row whose payload is null (unrecoverable original)", () => {
    const decoded = parseAcceptedInput(ROW_NULL_PAYLOAD);
    expect(decoded).not.toBeNull();
    expect(decoded?.payload).toBeNull();
    expect(decoded?.kind).toBe("restore");
  });

  it("refuses an unknown kind instead of defaulting to message", () => {
    const decoded = parseAcceptedInput({ ...ROW_MESSAGE, kind: "telepathy" });
    expect(decoded).toBeNull();
    expect(decoded).not.toEqual(expect.objectContaining({ kind: "message" }));
  });

  it("accepts all three frozen kinds", () => {
    for (const kind of ["message", "reply", "restore"] as const) {
      expect(parseAcceptedInput({ ...ROW_MESSAGE, kind })?.kind).toBe(kind);
    }
  });

  it("refuses a present wrong-typed payload instead of repairing it", () => {
    // The field most likely to be "helpfully" repaired: a present
    // non-string payload refuses the row — it must never read as a
    // fabricated ``null`` (unknown) nor as a stringified object.
    expect(parseAcceptedInput({ ...ROW_MESSAGE, payload: 42 })).toBeNull();
    expect(parseAcceptedInput({ ...ROW_MESSAGE, payload: 42 })?.payload).toBeUndefined();
  });

  it("refuses a row with an off-contract optional field", () => {
    // The four string|null fields refuse together as one family: a
    // numeric question_id fails the whole row rather than silently
    // reading as null.
    expect(parseAcceptedInput({ ...ROW_MESSAGE, question_id: 5 })).toBeNull();
    expect(parseAcceptedInput({ ...ROW_MESSAGE, run_id: true })).toBeNull();
  });
});

describe("parseCurrentTask", () => {
  it("accepts a task with an empty expected_result", () => {
    const decoded = parseCurrentTask({ task_id: "t-1", goal: "看看年报", expected_result: "" });
    expect(decoded).not.toBeNull();
    expect(decoded?.expected_result).toBe("");
  });

  it("refuses a blank task_id or goal", () => {
    expect(parseCurrentTask({ task_id: "", goal: "看看年报" })).toBeNull();
    expect(parseCurrentTask({ task_id: "t-1", goal: "   " })).toBeNull();
  });

  it("refuses an unknown outcome token rather than presenting it as active", () => {
    expect(
      parseCurrentTask({ task_id: "t-1", goal: "看看年报", outcome: "bogus" }),
    ).toBeNull();
  });

  it("refuses a present wrong-typed scalar instead of defaulting it", () => {
    // ``expected_result: 3`` is present-but-off-contract — distinct from
    // an absent field (which reads ""). Same for the version counter.
    expect(
      parseCurrentTask({ task_id: "t-1", goal: "看看年报", expected_result: 3 }),
    ).toBeNull();
    expect(
      parseCurrentTask({ task_id: "t-1", goal: "看看年报", task_version: "2" }),
    ).toBeNull();
  });

  it("defaults a missing outcome to active and keeps it distinct from a refused token", () => {
    // §Ownership: no recorded assessment leaves the task ACTIVE — and a
    // PRESENT unknown token is refused, never repaired into active.
    expect(parseCurrentTask({ task_id: "t-1", goal: "看看年报" })?.outcome).toBe(
      "active",
    );
    expect(
      parseCurrentTask({ task_id: "t-1", goal: "看看年报", outcome: "bogus" }),
    ).not.toEqual(expect.objectContaining({ outcome: "active" }));
  });

  it("refuses the whole task when the envelope-level task_outcome is unparseable", () => {
    // The optional outcome family: absent → not set; unknown token →
    // refusal (not silently dropped like the best-effort nested copy).
    expect(
      parseCurrentTask({ task_id: "t-1", goal: "看看年报", task_outcome: "bogus" }),
    ).toBeNull();
    expect(parseCurrentTask({ task_id: "t-1", goal: "看看年报" })?.task_outcome).toBeUndefined();
  });

  it("omits a malformed nested pending_question without failing the task", () => {
    // Best-effort nested copy: the top-level field is authoritative and
    // carries its own refusal, so a bad nested copy just doesn't attach.
    const decoded = parseCurrentTask({
      task_id: "t-1",
      goal: "看看年报",
      pending_question: { question_id: "q", prompt: "" },
    });
    expect(decoded).not.toBeNull();
    expect(decoded?.pending_question).toBeUndefined();
  });
});

describe("parseArchivedTask", () => {
  it("refuses a blank archive_id or task_id", () => {
    expect(parseArchivedTask({ ...ARCHIVE, archive_id: "" })).toBeNull();
    expect(parseArchivedTask({ ...ARCHIVE, task_id: "" })).toBeNull();
  });

  it("accepts a null run_id and an empty evidence_refs", () => {
    const decoded = parseArchivedTask(ARCHIVE);
    expect(decoded?.run_id).toBeNull();
    expect(decoded?.evidence_refs).toEqual([]);
  });

  it("retains the outcome the task had when it was archived", () => {
    // Archiving is separate from completing or cancelling (W5), so an
    // archive of a still-active task must read ``active`` — not a
    // defaulted value.
    expect(parseArchivedTask({ ...ARCHIVE, outcome: "active" })?.outcome).toBe(
      "active",
    );
    expect(parseArchivedTask({ ...ARCHIVE, outcome: "cancelled" })?.outcome).toBe(
      "cancelled",
    );
  });

  it("refuses a present wrong-typed scalar instead of defaulting it", () => {
    // Absent ``run_id`` reads as null (real "no run ref"); a present
    // number is off-contract and refuses the row.
    expect(parseArchivedTask({ ...ARCHIVE, run_id: 7 })).toBeNull();
    expect(parseArchivedTask({ ...ARCHIVE, evidence_refs: "refs" })).toBeNull();
  });

  it("leaves an absent or unrecognised outcome undefined rather than defaulting it", () => {
    // An absent peer fact stays absent, so the surface can say 「未记录」
    // instead of asserting a task fact it was not given.
    expect(parseArchivedTask(ARCHIVE)?.outcome).toBeUndefined();
    expect(parseArchivedTask({ ...ARCHIVE, outcome: "bogus" })?.outcome).toBeUndefined();
  });
});

describe("parseReconciliation", () => {
  it("requires an integer reconciled_input_version and a string disposition", () => {
    expect(parseReconciliation({ reconciled_input_version: "3", disposition: "none" })).toBeNull();
    expect(parseReconciliation({ reconciled_input_version: 3 })).toBeNull();
    expect(parseReconciliation({ reconciled_input_version: 3, disposition: "" })).toBeNull();
  });

  it("accepts null task_id / task_version (a taskless reconciliation)", () => {
    const decoded = parseReconciliation({
      reconciled_input_version: 2,
      task_id: null,
      task_version: null,
      disposition: "none",
      reason: "",
    });
    expect(decoded?.task_id).toBeNull();
    expect(decoded?.task_version).toBeNull();
  });
});

describe("isTaskless", () => {
  it("is true for the empty envelope", () => {
    expect(isTaskless(EMPTY_TASK_RUNTIME)).toBe(true);
  });

  it("is true for a view carrying only archives (an archive is not a current task)", () => {
    const archivesOnly: TaskRuntimeView = {
      ...EMPTY_TASK_RUNTIME,
      archived_tasks: [ARCHIVE],
    };
    expect(isTaskless(archivesOnly)).toBe(true);
  });

  it("is false once a current task is present", () => {
    const withTask: TaskRuntimeView = {
      ...EMPTY_TASK_RUNTIME,
      current_task: EXPECTED_VIEW.current_task,
    };
    expect(isTaskless(withTask)).toBe(false);
  });
});

describe("TASK_RUNTIME_REFUSAL_COPY", () => {
  it("has non-empty copy for both refusal tokens", () => {
    expect(TASK_RUNTIME_REFUSAL_COPY.malformed_task_runtime.length).toBeGreaterThan(0);
    expect(TASK_RUNTIME_REFUSAL_COPY.unsupported_task_runtime.length).toBeGreaterThan(0);
  });

  it("never turns an operational failure into a clarification", () => {
    for (const copy of Object.values(TASK_RUNTIME_REFUSAL_COPY)) {
      expect(copy).not.toContain("？");
      expect(copy).not.toContain("吗");
      // The copy states the state is unrecorded, not that it is broken.
      expect(copy).toContain("未记录");
    }
  });
});
