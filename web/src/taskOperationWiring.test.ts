// The per-thread operation ledger as the store wires it.
//
// workspaceOperation.test.ts proves the ledger's own association rules in
// isolation; those rules mean nothing to the product until the store's read
// seams and door actions honour them. What is pinned here is that seam: which
// thread a verdict may render on, which attempt a verdict may change, which
// door's copy it names, and which record (if any) a door leaves behind for
// the surfaces to read.

import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import {
  displayableWorkspaceOperation,
  operationRefusal,
  restoreFactsFor,
  submissionNoticeFor,
  useWorkspaceStore,
} from "./store";
import {
  applyWorkspaceOperationResult,
  currentWorkspaceOperation,
  EMPTY_WORKSPACE_OPERATIONS,
} from "./workspaceOperation";
import { clarificationRegion } from "./clarificationRegion";
import {
  RESTORE_UNHEARD_COPY,
  restoreDecision,
  restoreRefusalCopy,
} from "./archiveRestore";
import { TRANSPORT_UNKNOWN_NOTICE } from "./activeInput";
import type {
  ArchivedTaskSummary,
  CurrentTask,
  PendingQuestionView,
  TaskRuntimeView,
  ThreadDetail,
} from "./types";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const RUN_ID = "33333333-3333-4333-8333-333333333333";
const QUESTION_A = "q-clarify-a";
const QUESTION_B = "q-clarify-b";
const ARCHIVE_ID = "archive-task-1-v1";
/** The task the clicked archive row names: adoption is proven against it. */
const TASK_ID = "task-1";
/** A different task, so a projection reporting it proves nothing about
 * ``TASK_ID``. */
const OTHER_TASK_ID = "task-2";

function pendingQuestion(questionId: string): PendingQuestionView {
  return {
    question_id: questionId,
    task_id: TASK_ID,
    task_version: 1,
    prompt: "请补充：要比较哪两个期间？",
    choices: ["2022", "2023", "other"],
    input_version: 1,
  };
}

function archiveRow(
  overrides: Partial<ArchivedTaskSummary> = {},
): ArchivedTaskSummary {
  return {
    archive_id: ARCHIVE_ID,
    task_id: TASK_ID,
    goal: "比较营收",
    task_version: 1,
    archived_at_input_version: 2,
    run_id: null,
    evidence_refs: [],
    outcome: "active",
    ...overrides,
  };
}

function currentTask(taskId: string): CurrentTask {
  return {
    task_id: taskId,
    goal: "当前任务",
    expected_result: "",
    task_version: 1,
    necessary_conditions: [],
    outcome: "active",
    outcome_reason: "",
    created_at_input_version: 3,
  };
}

function envelope(overrides: Partial<TaskRuntimeView> = {}): TaskRuntimeView {
  return {
    current_task: null,
    archived_tasks: [],
    pending_question: null,
    reconciliation: null,
    task_outcome: null,
    input_version: 0,
    input_history: [],
    ...overrides,
  };
}

/** The listing the archive rows are offered from at click time, with a
 * different task current — the shape a restore is asked for in. */
function offeredArchives(): TaskRuntimeView {
  return envelope({
    archived_tasks: [archiveRow()],
    current_task: currentTask(OTHER_TASK_ID),
  });
}

function threadDetail(id: string, extra: Partial<ThreadDetail> = {}): ThreadDetail {
  return {
    id,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
    ...extra,
  };
}

/** A detail body carrying ``projection``: every post-door re-read runs a
 * real ``GET /api/threads/{id}`` through ``decodeThreadDetail``, so a bare
 * envelope would fail the thread decode rather than exercise the read. */
function detailBody(id: string, projection: TaskRuntimeView): Record<string, unknown> {
  return { ...threadDetail(id), task_runtime: projection };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A terminal SSE frame: the doors answer a stream, so a well-formed one
 * keeps the store off its error path. ``reason`` is the field
 * ``midStreamTaskRefusal`` reads first for a task refusal. */
function statusFrame(payload: Record<string, unknown>): Response {
  const frame = `event: status\ndata: ${JSON.stringify(payload)}\n\n`;
  return new Response(frame, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function acceptedFrame(): Response {
  return statusFrame({ run_id: RUN_ID, status: "done", assistant_message: "" });
}

function refusedFrame(reason: string): Response {
  return statusFrame({
    run_id: RUN_ID,
    status: "refuse",
    reason,
    assistant_message: "这次输入没有被处理。",
  });
}

/** The wire: every POST is recorded and answered by ``answer``, and each
 * thread's detail GET answers the projection the host now reports. Any other
 * GET (the thread list a send's settle point refreshes) answers empty. */
function stubWire(input: {
  answer: (postNumber: number) => Response;
  projections: Record<string, TaskRuntimeView>;
}): { posts: unknown[] } {
  const posts: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body ?? "{}")));
        return input.answer(posts.length);
      }
      const thread = Object.keys(input.projections).find((id) =>
        url.includes(`/threads/${id}`),
      );
      if (thread !== undefined) {
        return jsonResponse(detailBody(thread, input.projections[thread]));
      }
      return jsonResponse([]);
    }),
  );
  return { posts };
}

/** One store state holding every slice these tests read, so a case starts
 * from a known ledger rather than from whatever a previous case left. */
function seed(input: {
  active: string;
  projections?: Record<string, TaskRuntimeView>;
  detail?: Partial<ThreadDetail>;
  pendingUploads?: Record<string, number>;
}): void {
  useWorkspaceStore.setState({
    threads: [],
    activeThread: threadDetail(input.active, input.detail ?? {}),
    sendingQuestion: false,
    resumingThreadId: null,
    taskRuntime: input.projections ?? {},
    clarificationDrafts: {},
    workspaceOperations: EMPTY_WORKSPACE_OPERATIONS,
    restoreRequests: {},
    taskProjectionRefusal: {},
    taskProjectionRefreshFailed: {},
    pendingUploads: input.pendingUploads ?? {},
  });
}

/** The store state a restore case ends in: the ledger plus the projection
 * the post-door re-read returned. */
type SeededState = ReturnType<typeof useWorkspaceStore.getState>;

/** Ask the host to restore the listed archive and settle the door, leaving
 * the store reading the projection the post-door re-read returned. */
async function restoreAgainst(postDoor: TaskRuntimeView): Promise<SeededState> {
  seed({ active: A, projections: { [A]: offeredArchives() } });
  stubWire({ answer: () => acceptedFrame(), projections: { [A]: postDoor } });
  await useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID);
  return useWorkspaceStore.getState();
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a refusal belongs to the thread that opened it", () => {
  it("keeps thread A's reply refusal off thread B's surfaces", async () => {
    stubWire({
      answer: () =>
        jsonResponse(
          { detail: { error: "task_reply_expired", question_id: QUESTION_A } },
          409,
        ),
      projections: {
        [A]: envelope({ pending_question: pendingQuestion(QUESTION_A) }),
        [B]: envelope({ pending_question: pendingQuestion(QUESTION_B) }),
      },
    });
    // Both threads projected from a real read, A answered and refused.
    await useWorkspaceStore.getState().openThread(A);
    const door = useWorkspaceStore.getState();
    door.openTaskClarification(A);
    door.selectTaskChoice(A, "2023");
    await useWorkspaceStore.getState().submitTaskReply(A);

    const onA = useWorkspaceStore.getState();
    expect(operationRefusal(displayableWorkspaceOperation(onA, A))).toEqual({
      kind: "task_reply_expired",
      questionId: QUESTION_A,
      door: "reply",
    });

    await useWorkspaceStore.getState().openThread(B);
    const state = useWorkspaceStore.getState();
    // Switching the open thread clears nothing: A's verdict still exists
    // and stays A's.
    expect(currentWorkspaceOperation(state.workspaceOperations, A)).toMatchObject({
      state: "rejected",
      reason: "task_reply_expired",
    });

    const operationB = displayableWorkspaceOperation(state, B);
    const refusalB = operationRefusal(operationB);
    // A's own record, unreadable while B is on screen.
    expect(displayableWorkspaceOperation(state, A)).toBeNull();
    expect(operationB).toBeNull();
    expect(refusalB).toBeNull();
    expect(submissionNoticeFor(state, B)).toBeNull();

    const region = clarificationRegion({
      pendingQuestion: state.taskRuntime[B]?.pending_question ?? null,
      drafts: state.clarificationDrafts,
      submission: operationB,
      refusal: refusalB,
    });
    expect(region.card.refusalKind).toBeNull();
    expect(region.refused).toBeNull();
    expect(region.answeredQuestionId).toBeNull();
  });
});

describe("a superseded attempt", () => {
  it("cannot repaint the operation that replaced it", async () => {
    // Two restore clicks on the same thread: the first was accepted, the
    // second refused. The operator is now looking at the second. The host
    // keeps the archive listed throughout, so the second click is a real
    // door rather than an unlisted refusal.
    seed({ active: A, projections: { [A]: offeredArchives() } });
    const wire = stubWire({
      answer: (postNumber) =>
        postNumber === 1 ? acceptedFrame() : refusedFrame("task_restore_not_adopted"),
      projections: { [A]: offeredArchives() },
    });
    await useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID);
    await useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID);

    const state = useWorkspaceStore.getState();
    expect(wire.posts).toHaveLength(2);
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      attempt: 2,
      door: "restore",
      state: "rejected",
    });

    const stale = applyWorkspaceOperationResult(state.workspaceOperations, {
      threadId: A,
      attempt: 1,
      state: "accepted",
      reason: null,
    });
    expect(stale).toBe(state.workspaceOperations);
    expect(currentWorkspaceOperation(stale, A)).toMatchObject({
      attempt: 2,
      state: "rejected",
      reason: "task_restore_not_adopted",
    });
    // One record per thread, always: the ledger holds A's current attempt
    // and nothing else.
    expect(Object.keys(stale.operations)).toEqual([A]);
    expect(Object.keys(stale.attempts)).toEqual([A]);
  });
});

describe("a held send", () => {
  it("opens no operation record when the client-side hold refuses first", async () => {
    // The hold is decided from ``activeInputFactsFor``: a paused choice
    // window (the projection's own reloaded ``pending_turn``) or an upload
    // POST in flight. A pending clarification alone is not a hold, so the
    // locked half rides on the turn table in both lanes below.
    const lanes: {
      label: string;
      detail: Partial<ThreadDetail>;
      pendingUploads: Record<string, number>;
      refusal: new () => Error;
    }[] = [
      {
        label: "a paused choice window",
        detail: {
          pending_turn: {
            run_id: RUN_ID,
            question: "营收多少？",
            choice_window: {
              kind: "choice_window",
              title: "选择检索档",
              options: [{ id: "graph", label: "知识问答" }],
            },
          },
        },
        pendingUploads: {},
        refusal: api.ConcurrentRunError,
      },
      {
        label: "an upload in flight",
        detail: {},
        pendingUploads: { [A]: 1 },
        refusal: api.AttachmentUploadInProgressError,
      },
    ];

    for (const lane of lanes) {
      seed({
        active: A,
        projections: {
          [A]: envelope({ pending_question: pendingQuestion(QUESTION_A) }),
        },
        detail: lane.detail,
        pendingUploads: lane.pendingUploads,
      });
      const wire = stubWire({
        answer: () => acceptedFrame(),
        projections: { [A]: envelope() },
      });

      await expect(
        useWorkspaceStore.getState().sendQuestion(A, "对比两个期间"),
      ).rejects.toBeInstanceOf(lane.refusal);

      const state = useWorkspaceStore.getState();
      expect(wire.posts).toHaveLength(0);
      expect(displayableWorkspaceOperation(state, A)).toBeNull();
      // Neither an attempt nor a record: the guard throws before the ledger
      // is touched, so the refused send cannot surface as an operation.
      expect(state.workspaceOperations, lane.label).toBe(
        EMPTY_WORKSPACE_OPERATIONS,
      );
    }
  });
});

describe("a transport failure", () => {
  it("is its own state, unknown rather than accepted or refused", async () => {
    seed({ active: A, projections: { [A]: envelope() } });
    stubWire({
      answer: () => jsonResponse({ detail: "internal error" }, 500),
      projections: { [A]: envelope() },
    });

    // Rethrown so the composer can still show the failure's own detail line.
    await expect(
      useWorkspaceStore.getState().sendQuestion(A, "对比两个期间"),
    ).rejects.toThrow("HTTP 500");

    const state = useWorkspaceStore.getState();
    const operation = displayableWorkspaceOperation(state, A);
    expect(operation).toMatchObject({
      threadId: A,
      door: "message",
      kind: "message",
      attempt: 1,
      state: "failed",
      reason: null,
    });
    // A failure names no refusal: there is no host word to render.
    expect(operationRefusal(operation)).toBeNull();
    expect(submissionNoticeFor(state, A)).toEqual({
      state: "failed",
      text: TRANSPORT_UNKNOWN_NOTICE,
    });
  });
});

describe("a mid-stream task refusal on an ordinary send", () => {
  it("is recorded as rejected on the message door, not accepted", async () => {
    seed({ active: A, projections: { [A]: envelope() } });
    stubWire({
      answer: () => refusedFrame("task_input_invalid"),
      projections: { [A]: envelope() },
    });

    await useWorkspaceStore.getState().sendQuestion(A, "对比两个期间");

    const state = useWorkspaceStore.getState();
    const operation = displayableWorkspaceOperation(state, A);
    expect(operation).toMatchObject({
      door: "message",
      kind: "message",
      attempt: 1,
      state: "rejected",
      reason: "task_input_invalid",
    });
    expect(operationRefusal(operation)).toEqual({
      kind: "task_input_invalid",
      questionId: null,
      door: "message",
    });
    expect(submissionNoticeFor(state, A)?.state).toBe("rejected");
  });
});

describe("a restore the host accepted", () => {
  it("is not adopted when the fresh projection's current task is another task", async () => {
    const state = await restoreAgainst(
      envelope({ current_task: currentTask(OTHER_TASK_ID) }),
    );
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      state: "accepted",
    });
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.state).toBe("awaiting_confirmation");
    expect(decision.state).not.toBe("restored");
    expect(decision.restoredArchiveId).toBeNull();
    expect(decision.pendingArchiveId).toBe(ARCHIVE_ID);
    // The host answered this one, so the owed confirmation names the row
    // without parking the rest of the listing.
    expect(decision.requestUnsettled).toBe(false);
  });

  it("is adopted only when the fresh projection names the clicked task and drops the archive", async () => {
    const state = await restoreAgainst(
      envelope({ current_task: currentTask(TASK_ID) }),
    );
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.state).toBe("restored");
    expect(decision.restoredArchiveId).toBe(ARCHIVE_ID);
    expect(decision.pendingArchiveId).toBeNull();
    expect(decision.currentTaskId).toBe(TASK_ID);
  });

  it("is not adopted while the fresh projection still lists the archive", async () => {
    const state = await restoreAgainst(
      envelope({
        current_task: currentTask(TASK_ID),
        archived_tasks: [archiveRow()],
      }),
    );
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.state).not.toBe("restored");
    expect(decision.state).toBe("awaiting_confirmation");
    expect(decision.restoredArchiveId).toBeNull();
  });
});

describe("a refusal against a confirmation", () => {
  it("survives a later door taking over the thread's current operation", async () => {
    // The restore is refused by name, and the operator then answers a
    // clarification on the same thread. That reply becomes the thread's one
    // current operation, and a verdict read back from the ledger would go
    // missing with it: the answer belongs to the restore's own record.
    seed({ active: A, projections: { [A]: offeredArchives() } });
    stubWire({
      answer: () => refusedFrame("task_permission_denied"),
      projections: { [A]: offeredArchives() },
    });

    await useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID);

    const refused = restoreDecision(
      restoreFactsFor(useWorkspaceStore.getState(), A),
    );
    expect(refused.state).toBe("refused");
    expect(refused.notice).toBe(restoreRefusalCopy("task_permission_denied"));

    // A pending clarification appears and the reply door answers it, so the
    // ledger now names the reply and nothing of the restore.
    const withQuestion = envelope({
      archived_tasks: [archiveRow()],
      current_task: currentTask(OTHER_TASK_ID),
      pending_question: pendingQuestion(QUESTION_A),
    });
    stubWire({
      answer: () => acceptedFrame(),
      projections: { [A]: withQuestion },
    });
    await useWorkspaceStore.getState().reloadTaskProjection(A);
    const door = useWorkspaceStore.getState();
    door.openTaskClarification(A);
    door.selectTaskChoice(A, "2022");
    await useWorkspaceStore.getState().submitTaskReply(A);

    const state = useWorkspaceStore.getState();
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      door: "reply",
      kind: "reply",
      state: "accepted",
    });
    // The restore sentence is unchanged: still the refusal this restore was
    // given, still no adoption, and still nothing holding the listing.
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.state).toBe("refused");
    expect(decision.notice).toBe(restoreRefusalCopy("task_permission_denied"));
    expect(decision.restoredArchiveId).toBeNull();
    expect(decision.awaitingConfirmation).toBe(false);
    expect(decision.requestUnsettled).toBe(false);
    expect(decision.currentTaskId).toBe(OTHER_TASK_ID);
  });

  it("outranks a fresh projection that already shows the archive adopted", async () => {
    // One store call is the real producer of this shape: the restore is
    // durably accepted, reconciliation then refuses it on the SAME 200
    // stream, and the post-door read still reports the archive adopted.
    seed({ active: A, projections: { [A]: offeredArchives() } });
    stubWire({
      answer: () => refusedFrame("task_restore_not_adopted"),
      projections: { [A]: envelope({ current_task: currentTask(TASK_ID) }) },
    });

    await useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID);

    const state = useWorkspaceStore.getState();
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      door: "restore",
      state: "rejected",
      reason: "task_restore_not_adopted",
    });
    // The named verdict is on the request, not only on the operation the
    // thread is currently holding.
    expect(state.restoreRequests[A]?.refusalKind).toBe(
      "task_restore_not_adopted",
    );
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.state).toBe("refused");
    expect(decision.restoredArchiveId).toBeNull();
  });
});

describe("a restore click on an archive the projection does not list", () => {
  it("sends nothing and is refused", async () => {
    seed({
      active: A,
      projections: {
        [A]: envelope({
          archived_tasks: [archiveRow({ archive_id: "archive-other" })],
          current_task: currentTask(OTHER_TASK_ID),
        }),
      },
    });
    const wire = stubWire({
      answer: () => acceptedFrame(),
      projections: { [A]: envelope() },
    });

    await useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID);

    const state = useWorkspaceStore.getState();
    expect(wire.posts).toHaveLength(0);
    // Nothing was sent and nothing is owed a confirmation: the click is
    // answered where it was made, by the listing check itself, and that
    // answer lives on the restore's own record rather than on the thread's
    // replaceable current operation.
    expect(state.restoreRequests[A]).toMatchObject({
      archiveId: ARCHIVE_ID,
      accepted: false,
      failedToSend: false,
      refusalKind: "task_restore_unknown",
    });
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      door: "restore",
      state: "rejected",
      reason: "task_restore_unknown",
    });
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.state).toBe("refused");
    expect(decision.pendingArchiveId).toBeNull();
    expect(decision.awaitingConfirmation).toBe(false);
    expect(decision.requestUnsettled).toBe(false);
    expect(decision.restoredArchiveId).toBeNull();
  });
});

describe("a task door whose send never got an answer", () => {
  // Both surfaces dispatch with a floating promise, so a rejection on this
  // lane would reach the operator as nothing at all. The recorded verdict is
  // the notice path, and the door has to resolve for it to be the only one.
  it("leaves the reply as its own unknown outcome, not a rejection", async () => {
    stubWire({
      answer: () => jsonResponse({ detail: "internal error" }, 500),
      projections: {
        [A]: envelope({ pending_question: pendingQuestion(QUESTION_A) }),
      },
    });
    await useWorkspaceStore.getState().openThread(A);
    const door = useWorkspaceStore.getState();
    door.openTaskClarification(A);
    door.selectTaskChoice(A, "2023");

    await expect(
      useWorkspaceStore.getState().submitTaskReply(A),
    ).resolves.toBeUndefined();

    const state = useWorkspaceStore.getState();
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      door: "reply",
      kind: "reply",
      state: "failed",
      reason: null,
    });
    expect(submissionNoticeFor(state, A)).toEqual({
      state: "failed",
      text: TRANSPORT_UNKNOWN_NOTICE,
    });
    // Unknown is not an answer: the question stays pending on the host and
    // the draft keeps its association, so the identical reply can be sent
    // again.
    const region = clarificationRegion({
      pendingQuestion: state.taskRuntime[A]?.pending_question ?? null,
      drafts: state.clarificationDrafts,
      submission: displayableWorkspaceOperation(state, A),
      refusal: operationRefusal(displayableWorkspaceOperation(state, A)),
    });
    expect(region.card.draft?.selectedChoiceId).toBe("2023");
    expect(region.answeredQuestionId).toBeNull();
  });

  it("keeps the restore request as not heard, with the current task on screen", async () => {
    seed({ active: A, projections: { [A]: offeredArchives() } });
    stubWire({
      answer: () => jsonResponse({ detail: "internal error" }, 500),
      projections: { [A]: offeredArchives() },
    });

    await expect(
      useWorkspaceStore.getState().restoreArchivedTask(A, ARCHIVE_ID),
    ).resolves.toBeUndefined();

    const state = useWorkspaceStore.getState();
    expect(state.restoreRequests[A]).toMatchObject({
      archiveId: ARCHIVE_ID,
      accepted: false,
      failedToSend: true,
    });
    // A transport failure names no verdict: there is no host word about
    // this restore to record on it.
    expect(state.restoreRequests[A]?.refusalKind).toBeNull();
    expect(displayableWorkspaceOperation(state, A)).toMatchObject({
      door: "restore",
      state: "failed",
    });
    const decision = restoreDecision(restoreFactsFor(state, A));
    expect(decision.notice).toBe(RESTORE_UNHEARD_COPY);
    // The attempt is answered — by its own absence of an answer — so it
    // does not hold the listing still.
    expect(decision.awaitingConfirmation).toBe(false);
    expect(decision.requestUnsettled).toBe(false);
    // Nothing was adopted: the task that was current before the click is
    // still what the surface shows.
    expect(decision.restoredArchiveId).toBeNull();
    expect(decision.currentTaskId).toBe(OTHER_TASK_ID);
  });
});
