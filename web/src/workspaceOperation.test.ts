import { describe, expect, it } from "vitest";
import {
  EMPTY_WORKSPACE_OPERATIONS,
  applyWorkspaceOperationResult,
  beginWorkspaceOperation,
  currentWorkspaceOperation,
  isWorkspaceOperationDisplayable,
} from "./workspaceOperation";
import type {
  WorkspaceOperation,
  WorkspaceOperationLedger,
} from "./workspaceOperation";

interface OpenedFixture {
  ledger: WorkspaceOperationLedger;
  operation: WorkspaceOperation;
}

function open(
  ledger: WorkspaceOperationLedger,
  input: {
    threadId: string;
    door?: string;
    kind?: string;
    questionId?: string | null;
    archiveId?: string | null;
    taskId?: string | null;
  },
): OpenedFixture {
  const opened = beginWorkspaceOperation(ledger, {
    threadId: input.threadId,
    door: input.door ?? "reply",
    kind: input.kind ?? "reply",
    ...(input.questionId !== undefined ? { questionId: input.questionId } : {}),
    ...(input.archiveId !== undefined ? { archiveId: input.archiveId } : {}),
    ...(input.taskId !== undefined ? { taskId: input.taskId } : {}),
  });
  if (opened.operation === null) throw new Error("fixture opened nothing");
  return { ledger: opened.ledger, operation: opened.operation };
}

describe("beginWorkspaceOperation", () => {
  it("mints the first attempt for a thread and keeps the request association", () => {
    const { operation } = open(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t1",
      door: "restore",
      kind: "restore",
      archiveId: "arch-7",
    });
    expect(operation).toMatchObject({
      threadId: "t1",
      door: "restore",
      kind: "restore",
      attempt: 1,
      archiveId: "arch-7",
      questionId: null,
      taskId: null,
      state: "pending",
      reason: null,
    });
  });

  it("advances the attempt for a repeated request on the same thread", () => {
    const first = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const second = open(first.ledger, { threadId: "t1" });
    expect(first.operation?.attempt).toBe(1);
    expect(second.operation?.attempt).toBe(2);
    expect(currentWorkspaceOperation(second.ledger, "t1")?.attempt).toBe(2);
  });

  it("keeps one attempt counter per thread", () => {
    const a = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const b = open(a.ledger, { threadId: "t2" });
    expect(a.operation?.attempt).toBe(1);
    expect(b.operation?.attempt).toBe(1);
  });

  it("opening another thread leaves the first thread's record untouched", () => {
    const first = open(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t1",
      questionId: "q-1",
    });
    const second = open(first.ledger, {
      threadId: "t2",
      door: "restore",
      kind: "restore",
      archiveId: "arch-1",
    });
    const t1 = currentWorkspaceOperation(second.ledger, "t1");
    expect(t1).toMatchObject({ attempt: 1, state: "pending", questionId: "q-1" });
    expect(second.operation?.threadId).toBe("t2");
  });

  it("mints nothing for an empty or unknown input class", () => {
    for (const input of [
      { threadId: "", door: "reply", kind: "reply" },
      { threadId: "   ", door: "reply", kind: "reply" },
      { threadId: "t1", door: "delete-thread", kind: "reply" },
      { threadId: "t1", door: "reply", kind: "guess" },
    ]) {
      const opened = beginWorkspaceOperation(EMPTY_WORKSPACE_OPERATIONS, input);
      expect(opened.operation).toBeNull();
      expect(opened.ledger).toBe(EMPTY_WORKSPACE_OPERATIONS);
      expect(currentWorkspaceOperation(opened.ledger, input.threadId)).toBeNull();
    }
  });
});

describe("applyWorkspaceOperationResult", () => {
  it("marks the current attempt accepted and records the host's task fact", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const operation = opened.operation;
    const applied = applyWorkspaceOperationResult(opened.ledger, {
      threadId: "t1",
      attempt: operation.attempt,
      state: "accepted",
      taskId: "task-9",
    });
    expect(currentWorkspaceOperation(applied, "t1")).toMatchObject({
      attempt: 1,
      state: "accepted",
      taskId: "task-9",
      questionId: null,
    });
  });

  it("drops a late result that names no known operation", () => {
    const applied = applyWorkspaceOperationResult(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t-unknown",
      attempt: 1,
      state: "accepted",
    });
    expect(applied).toBe(EMPTY_WORKSPACE_OPERATIONS);
    expect(currentWorkspaceOperation(applied, "t-unknown")).toBeNull();
  });

  it("lands a late result on its own thread and never on the thread in view", () => {
    const t1 = open(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t1",
      door: "restore",
      kind: "restore",
      archiveId: "arch-1",
    });
    const t2 = open(t1.ledger, {
      threadId: "t2",
      door: "restore",
      kind: "restore",
      archiveId: "arch-2",
    });
    const stale = applyWorkspaceOperationResult(t2.ledger, {
      threadId: "t1",
      attempt: t1.operation.attempt,
      state: "rejected",
      reason: "archive_not_restorable",
    });
    expect(currentWorkspaceOperation(stale, "t1")).toMatchObject({
      state: "rejected",
      reason: "archive_not_restorable",
      archiveId: "arch-1",
    });
    const inView = currentWorkspaceOperation(stale, "t2");
    expect(inView).toMatchObject({ state: "pending", archiveId: "arch-2" });
    // t1's verdict exists, but the surface showing t2 may not render it.
    expect(isWorkspaceOperationDisplayable(inView, "t1")).toBe(false);
    expect(
      isWorkspaceOperationDisplayable(currentWorkspaceOperation(stale, "t1"), "t2"),
    ).toBe(false);
  });

  it("drops a result aimed at a thread that has no operation open", () => {
    const t1 = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const applied = applyWorkspaceOperationResult(t1.ledger, {
      threadId: "t2",
      attempt: 1,
      state: "accepted",
    });
    expect(applied).toBe(t1.ledger);
    expect(currentWorkspaceOperation(applied, "t2")).toBeNull();
  });

  it("drops a result for a superseded attempt", () => {
    const first = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const second = open(first.ledger, { threadId: "t1" });
    const applied = applyWorkspaceOperationResult(second.ledger, {
      threadId: "t1",
      attempt: 1,
      state: "accepted",
      taskId: "task-stale",
    });
    expect(applied).toBe(second.ledger);
    expect(currentWorkspaceOperation(applied, "t1")).toMatchObject({
      attempt: 2,
      state: "pending",
      taskId: null,
    });
  });

  it("keeps a refusal recorded for the current attempt ahead of an older confirmation", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const operation = opened.operation;
    const refused = applyWorkspaceOperationResult(opened.ledger, {
      threadId: "t1",
      attempt: operation.attempt,
      state: "rejected",
      reason: "task_reply_expired",
    });
    const later = applyWorkspaceOperationResult(refused, {
      threadId: "t1",
      attempt: operation.attempt,
      state: "accepted",
    });
    expect(currentWorkspaceOperation(later, "t1")).toMatchObject({
      state: "rejected",
      reason: "task_reply_expired",
    });
  });

  it("lets a refusal arriving after an accepted verdict win", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t1",
      door: "restore",
      kind: "restore",
      archiveId: "arch-1",
    });
    const operation = opened.operation;
    const accepted = applyWorkspaceOperationResult(opened.ledger, {
      threadId: "t1",
      attempt: operation.attempt,
      state: "accepted",
    });
    const refused = applyWorkspaceOperationResult(accepted, {
      threadId: "t1",
      attempt: operation.attempt,
      state: "rejected",
      reason: "archive_stale_source",
    });
    expect(currentWorkspaceOperation(refused, "t1")).toMatchObject({
      state: "rejected",
      reason: "archive_stale_source",
      archiveId: "arch-1",
    });
  });

  it("never rewrites the request-time question association", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t1",
      questionId: "q-original",
    });
    const operation = opened.operation;
    const applied = applyWorkspaceOperationResult(opened.ledger, {
      threadId: "t1",
      attempt: operation.attempt,
      state: "rejected",
      questionId: "q-newer",
      reason: "task_reply_expired",
    });
    expect(currentWorkspaceOperation(applied, "t1")).toMatchObject({
      questionId: "q-original",
      state: "rejected",
    });
  });

  it("applies nothing for an empty or unknown result", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const operation = opened.operation;
    for (const result of [
      { threadId: "", attempt: operation.attempt, state: "accepted" },
      { threadId: "  ", attempt: operation.attempt, state: "accepted" },
      { threadId: "t1", attempt: operation.attempt, state: "maybe" },
      { threadId: "t1", attempt: operation.attempt, state: "pending" },
      { threadId: "t1", attempt: 0, state: "accepted" },
      { threadId: "t1", attempt: 1.5, state: "accepted" },
    ]) {
      expect(applyWorkspaceOperationResult(opened.ledger, result)).toBe(
        opened.ledger,
      );
    }
  });

  it("does not mutate the ledger it was given", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const before = opened.operation!;
    applyWorkspaceOperationResult(opened.ledger, {
      threadId: "t1",
      attempt: before.attempt,
      state: "accepted",
    });
    expect(before.state).toBe("pending");
    expect(currentWorkspaceOperation(opened.ledger, "t1")?.state).toBe("pending");
  });
});

describe("currentWorkspaceOperation", () => {
  it("reads nothing for an empty thread id", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    expect(currentWorkspaceOperation(opened.ledger, "")).toBeNull();
    expect(currentWorkspaceOperation(opened.ledger, "   ")).toBeNull();
  });
});

describe("the attempt counter", () => {
  it("advances on a thread so a late result cannot match the attempt it superseded", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    const stale = opened.operation!;
    const reopened = open(opened.ledger, { threadId: "t1", questionId: "q-2" });
    expect(reopened.operation?.attempt).toBe(2);
    const applied = applyWorkspaceOperationResult(reopened.ledger, {
      threadId: "t1",
      attempt: stale.attempt,
      state: "accepted",
    });
    expect(applied).toBe(reopened.ledger);
    expect(currentWorkspaceOperation(applied, "t1")).toMatchObject({
      attempt: 2,
      state: "pending",
      questionId: "q-2",
    });
  });
});

describe("isWorkspaceOperationDisplayable", () => {
  it("shows an operation only on the thread that opened it", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    expect(
      isWorkspaceOperationDisplayable(opened.operation, "t1"),
    ).toBe(true);
  });

  it("hides another thread's operation from the active thread", () => {
    const t1 = open(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: "t1",
      door: "restore",
      kind: "restore",
      archiveId: "arch-1",
    });
    expect(isWorkspaceOperationDisplayable(t1.operation, "t2")).toBe(false);
  });

  it("hides nothing-shaped and blank-thread records", () => {
    const opened = open(EMPTY_WORKSPACE_OPERATIONS, { threadId: "t1" });
    expect(isWorkspaceOperationDisplayable(null, "t1")).toBe(false);
    expect(isWorkspaceOperationDisplayable(undefined, "t1")).toBe(false);
    expect(isWorkspaceOperationDisplayable(opened.operation, "")).toBe(false);
    expect(isWorkspaceOperationDisplayable(opened.operation, "  ")).toBe(false);
  });
});
