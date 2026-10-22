// question-workspace: the task doors wired into the store (W2/W4/W5).
//
// These cover the STORE half of the contract — that a reply is posted
// with its ORIGINAL question association, that a stale refusal keeps the
// operator's draft and never rebases it, that a reload recovers the
// projection without submitting anything, and that a restore is
// explicit and moves nothing until the host confirms. The judgement each
// call makes lives in the pure modules and is tested there; what is
// pinned here is the wiring those judgements could drift away from.

import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { TaskInputRefusedError } from "./api";
import { restoreDecision, restoreRefusalCopy } from "./archiveRestore";
import {
  displayableWorkspaceOperation,
  operationRefusal,
  projectionUnreadableFor,
  restoreFactsFor,
  useWorkspaceStore,
} from "./store";
import type {
  ArchivedTaskSummary,
  PendingQuestionView,
  TaskRuntimeView,
  ThreadDetail,
} from "./types";
import {
  EMPTY_WORKSPACE_OPERATIONS,
  applyWorkspaceOperationResult,
  beginWorkspaceOperation,
  currentWorkspaceOperation,
} from "./workspaceOperation";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const QUESTION_ID = "q-clarify-1";

function pendingQuestion(): PendingQuestionView {
  return {
    question_id: QUESTION_ID,
    task_id: "task-1",
    task_version: 1,
    prompt: "请补充：要比较哪两个期间？",
    choices: ["2022", "2023", "other"],
    input_version: 1,
  };
}

/** The host's TERMINAL clarification with real options and no candidate
 * details. */
function terminalQuestion(): PendingQuestionView {
  return {
    question_id: QUESTION_ID,
    task_id: "task-1",
    task_version: 1,
    prompt: "请补充：公司/期间",
    choices: ["苹果公司", "2023 年度", "other"],
    input_version: 1,
    purpose: "task_clarification",
    origin_run_id: null,
    choice_details: [],
  };
}

/** The host's TERMINAL free-text-only clarification: Other is the only
 * choice, so the typed text is the whole reply. */
function freeTextOnlyQuestion(): PendingQuestionView {
  return {
    ...terminalQuestion(),
    prompt: "请补充：公司/期间",
    choices: ["other"],
  };
}

function archiveRow(overrides: Partial<ArchivedTaskSummary> = {}): ArchivedTaskSummary {
  return {
    archive_id: "archive-task-1-v1",
    task_id: "task-1",
    goal: "比较营收",
    task_version: 1,
    archived_at_input_version: 2,
    run_id: null,
    evidence_refs: [],
    outcome: "active",
    ...overrides,
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

function seedTaskThread(view: TaskRuntimeView): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
    taskRuntime: { [THREAD_ID]: view },
    clarificationDrafts: {},
    workspaceOperations: EMPTY_WORKSPACE_OPERATIONS,
    restoreRequests: {},
    taskProjectionRefusal: {},
    taskProjectionRefreshFailed: {},
  });
}

/** A decodable thread detail whose task envelope is ``projection``. The
 * post-door projection re-read runs a REAL ``GET /api/threads/{id}``
 * through ``decodeThreadDetail``, so a bare ``{ task_runtime }`` body
 * would fail the decode — the stubs answer with a full detail. */
function detailBody(projection: unknown): Record<string, unknown> {
  return {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
    task_runtime: projection,
  };
}

/** Records every POST body the store sends, and answers the GET that
 * follows each door with ``projection``. */
function stubTaskDoor(projection: unknown): {
  bodies: unknown[];
  urls: string[];
} {
  const bodies: unknown[] = [];
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      const url = String(_url);
      if (init?.method === "POST") {
        urls.push(url);
        bodies.push(JSON.parse(String(init.body ?? "{}")));
        // A terminal SSE status frame: the reply/restore door answers a
        // stream, so a well-formed one keeps the store off its error
        // path.
        const frame = `event: status\ndata: ${JSON.stringify({
          run_id: "22222222-2222-4222-8222-222222222222",
          status: "done",
          assistant_message: "",
        })}\n\n`;
        return new Response(frame, {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      }
      if (url.includes("/messages")) return new Response("", { status: 204 });
      return new Response(JSON.stringify(detailBody(projection)), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return { bodies, urls };
}

function stubTaskRefusal(
  status: number,
  kind: string,
  questionId?: string,
  reread?: TaskRuntimeView,
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Response(
          JSON.stringify({
            detail: { error: kind, ...(questionId ? { question_id: questionId } : {}) },
          }),
          { status, headers: { "content-type": "application/json" } },
        );
      }
      // The refusal leaves the host's own state alone, so the re-read it
      // triggers restates whatever the caller says the host now reports
      // (SC-4: the host's current state governs the display).
      return new Response(
        JSON.stringify(
          detailBody(reread ?? envelope({ pending_question: pendingQuestion() })),
        ),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("openTaskClarification", () => {
  it("opens a draft with nothing preselected and the host's wording verbatim", () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    useWorkspaceStore.getState().openTaskClarification(THREAD_ID);

    const draft = useWorkspaceStore.getState().clarificationDrafts[QUESTION_ID];
    expect(draft.prompt).toBe("请补充：要比较哪两个期间？");
    expect(draft.choices).toEqual(["2022", "2023", "other"]);
    expect(draft.selectedChoiceId).toBeNull();
  });

  it("does not clobber a typed draft on a second open", () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "other");
    store.setTaskOtherText(THREAD_ID, "2021 和 2022");

    useWorkspaceStore.getState().openTaskClarification(THREAD_ID);

    const draft = useWorkspaceStore.getState().clarificationDrafts[QUESTION_ID];
    expect(draft.selectedChoiceId).toBe("other");
    expect(draft.otherText).toBe("2021 和 2022");
  });

  it("does nothing on a thread with no pending question", () => {
    seedTaskThread(envelope());
    useWorkspaceStore.getState().openTaskClarification(THREAD_ID);
    expect(useWorkspaceStore.getState().clarificationDrafts).toEqual({});
  });
});

describe("submitTaskReply", () => {
  it("posts the exact choice, the verbatim free text, and the question it answers", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "other");
    store.setTaskOtherText(THREAD_ID, "2021 和 2022");
    const door = stubTaskDoor(envelope());

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(door.bodies).toHaveLength(1);
    expect(door.bodies[0]).toMatchObject({
      question_id: QUESTION_ID,
      choice_id: "other",
      free_text: "2021 和 2022",
    });
  });

  it("does not post a listed choice's text, and never posts an unsubmittable draft", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    // Other selected but blank: the host would refuse this as
    // task_input_invalid, so no POST is made at all.
    store.selectTaskChoice(THREAD_ID, "other");
    const door = stubTaskDoor(envelope());

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);
    expect(door.bodies).toHaveLength(0);

    store.selectTaskChoice(THREAD_ID, "2023");
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);
    expect(door.bodies[0]).toMatchObject({ choice_id: "2023" });
    expect(door.bodies[0]).not.toHaveProperty("free_text");
  });

  it("posts a listed real option with the question it answers, on the messages door", async () => {
    seedTaskThread(envelope({ pending_question: terminalQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023 年度");
    const door = stubTaskDoor(envelope());

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(door.bodies).toHaveLength(1);
    expect(door.bodies[0]).toMatchObject({
      question_id: QUESTION_ID,
      choice_id: "2023 年度",
    });
    expect(door.bodies[0]).not.toHaveProperty("free_text");
    // A terminal task clarification is answered by a fresh run on the
    // send door, never by the graph resume route.
    expect(door.urls).toHaveLength(1);
    expect(door.urls[0]).toContain(`/threads/${THREAD_ID}/messages`);
    expect(door.urls.some((url) => url.includes("/resume"))).toBe(false);
  });

  it("materializes the reply's NEW turn and folds its streamed answer", async () => {
    // The observed bad case: the host created a new turn for the reply,
    // but the SPA treated it as a resume-shaped stream with no patcher,
    // so the turn never appeared and the operator saw an unanswered card.
    seedTaskThread(
      envelope({
        current_task: {
          task_id: "task-1",
          goal: "ZEUS 年假政策是什么？",
          expected_result: "",
          task_version: 1,
          necessary_conditions: [],
          outcome: "active",
          outcome_reason: "",
          created_at_input_version: 1,
        },
        pending_question: terminalQuestion(),
      }),
    );
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023 年度");
    const runId = "22222222-2222-4222-8222-222222222222";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const href = String(url);
        if (init?.method === "POST") {
          const frames = [
            `event: status\ndata: ${JSON.stringify({ run_id: runId, status: "running" })}\n\n`,
            `event: answer\ndata: ${JSON.stringify({ text: "年假 22 天" })}\n\n`,
            `event: status\ndata: ${JSON.stringify({ run_id: runId, status: "done", assistant_message: "年假 22 天" })}\n\n`,
          ].join("");
          return new Response(frames, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        if (href.endsWith("/api/threads")) {
          return new Response("[]", {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify(detailBody(envelope())), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].run_id).toBe(runId);
    expect(turns[0].status).toBe("done");
    expect(turns[0].answer).toContain("年假 22 天");
    // The reply turn carries the original task goal beside the pick.
    expect(turns[0].question).toContain("ZEUS 年假政策是什么？");
    expect(turns[0].question).toContain("2023 年度");
  });

  it("posts a free-text-only answer without an Other click", async () => {
    seedTaskThread(envelope({ pending_question: freeTextOnlyQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    // No selectTaskChoice: the only choice is the reserved Other, so the
    // typed text is the answer.
    store.setTaskOtherText(THREAD_ID, "ZEUS，2024 年第三季度");
    const door = stubTaskDoor(envelope());

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(door.bodies).toHaveLength(1);
    expect(door.bodies[0]).toMatchObject({
      question_id: QUESTION_ID,
      choice_id: "other",
      free_text: "ZEUS，2024 年第三季度",
    });
    expect(door.urls[0]).toContain(`/threads/${THREAD_ID}/messages`);
  });
});

describe("a rejected reply", () => {
  it("keeps the draft's original association, choice, and text (SC-4)", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "other");
    store.setTaskOtherText(THREAD_ID, "2021 和 2022");
    stubTaskRefusal(409, "task_reply_expired", QUESTION_ID);

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const state = useWorkspaceStore.getState();
    expect(
      operationRefusal(displayableWorkspaceOperation(state, THREAD_ID)),
    ).toEqual({
      kind: "task_reply_expired",
      questionId: QUESTION_ID,
      door: "reply",
    });
    const draft = state.clarificationDrafts[QUESTION_ID];
    // The association is NEVER rewritten onto whatever is current now.
    expect(draft.questionId).toBe(QUESTION_ID);
    expect(draft.selectedChoiceId).toBe("other");
    expect(draft.otherText).toBe("2021 和 2022");
  });

  it("re-opens a dismissed card so the failed reply is visible", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023");
    store.dismissTaskClarification(THREAD_ID);
    expect(useWorkspaceStore.getState().clarificationDrafts[QUESTION_ID].dismissed).toBe(true);

    stubTaskRefusal(409, "task_reply_expired", QUESTION_ID);
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(useWorkspaceStore.getState().clarificationDrafts[QUESTION_ID].dismissed).toBe(false);
  });

  it("re-sends the identical reply on retry (same association, same text)", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "other");
    store.setTaskOtherText(THREAD_ID, "2021 和 2022");
    stubTaskRefusal(409, "task_reply_expired", QUESTION_ID);
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const door = stubTaskDoor(envelope());
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(door.bodies[0]).toMatchObject({
      question_id: QUESTION_ID,
      choice_id: "other",
      free_text: "2021 和 2022",
    });
  });

  it("reports the refusal without claiming the clarification was answered", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023");
    stubTaskRefusal(409, "task_reply_expired", QUESTION_ID);

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const operation = displayableWorkspaceOperation(
      useWorkspaceStore.getState(),
      THREAD_ID,
    );
    expect(operation?.state).toBe("rejected");
    // The question is still pending on the host, so nothing here may
    // read as answered.
    expect(
      useWorkspaceStore.getState().taskRuntime[THREAD_ID]?.pending_question,
    ).not.toBeNull();
  });

  it("stamps the reply door on the refusal it records", async () => {
    // A refusal kind is only meaningful against its own door's copy:
    // without the door stamp, a surface rendering restore copy could
    // read a reply refusal (task_reply_expired) as a failed restore. The
    // archive section is the exact consumer this pins (ArchiveList
    // renders restore copy only for door === "restore").
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023");
    stubTaskRefusal(409, "task_reply_expired", QUESTION_ID);

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const refusal = operationRefusal(
      displayableWorkspaceOperation(useWorkspaceStore.getState(), THREAD_ID),
    );
    expect(refusal?.kind).toBe("task_reply_expired");
    expect(refusal?.door).toBe("reply");
  });

  it("records the door on a MID-STREAM reply refusal too", async () => {
    // The reconciliation-refused lane (terminal ``refuse`` frame on a
    // 200 stream) records through the same reply-door helper, so both
    // refusal transports carry the same door.
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023");
    const frame = `event: status\ndata: ${JSON.stringify({
      run_id: "22222222-2222-4222-8222-222222222222",
      status: "refuse",
      reason: "task_reply_expired",
      assistant_message: "该问题已过期。",
      status_classified: "task_reply_expired",
    })}\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(frame, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        // A FULL detail body (not just the envelope): the post-refusal
        // reload re-reads GET /api/threads/{id} and decodes the thread
        // first, so this test exercises the reload SUCCEEDING.
        return new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
            task_runtime: envelope({ pending_question: pendingQuestion() }),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const state = useWorkspaceStore.getState();
    const operation = displayableWorkspaceOperation(state, THREAD_ID);
    expect(operation?.state).toBe("rejected");
    expect(operationRefusal(operation)?.door).toBe("reply");
    // The reload after the refusal ran for real and keeps the question
    // visible (the host still reports it in this stub).
    expect(state.taskRuntime[THREAD_ID]?.pending_question?.question_id).toBe(
      QUESTION_ID,
    );
    expect(state.taskProjectionRefreshFailed[THREAD_ID]).toBeUndefined();
  });
});

describe("dismissTaskClarification", () => {
  it("supplies no answer and submits nothing", async () => {
    seedTaskThread(envelope({ pending_question: pendingQuestion() }));
    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "2023");
    const door = stubTaskDoor(envelope());

    store.dismissTaskClarification(THREAD_ID);
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    // Dismissal is presentation-only, but an explicit submit afterwards
    // still goes out with the preserved choice — the point is that the
    // dismissal itself posted nothing.
    expect(door.bodies).toHaveLength(1);
    expect(
      displayableWorkspaceOperation(useWorkspaceStore.getState(), THREAD_ID)
        ?.questionId,
    ).toBe(QUESTION_ID);
  });
});

describe("openThread task recovery (W4)", () => {
  it("projects the task envelope from the GET without posting anything", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") bodies.push(init.body);
        return new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
            task_runtime: envelope({
              pending_question: pendingQuestion(),
              archived_tasks: [archiveRow()],
            }),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const view = useWorkspaceStore.getState().taskRuntime[THREAD_ID];
    expect(view?.pending_question?.question_id).toBe(QUESTION_ID);
    expect(view?.archived_tasks).toHaveLength(1);
    // Display recovery is separate from resuming execution: a reload
    // must not submit an empty or bodyless resume request.
    expect(bodies).toHaveLength(0);
  });

  it("keeps a terminal clarification through the GET decode, then replies with its question_id", async () => {
    // Raw wire shape: the decoder is the only thing that can carry this
    // question from the server to the store, so the reply proves the
    // decoded pending question survived.
    useWorkspaceStore.setState({
      clarificationDrafts: {},
      workspaceOperations: EMPTY_WORKSPACE_OPERATIONS,
    });
    const door = stubTaskDoor({
      current_task: null,
      archived_tasks: [],
      pending_question: {
        question_id: QUESTION_ID,
        task_id: "task-1",
        task_version: 1,
        prompt: "请补充：公司/期间",
        choices: ["苹果公司", "2023 年度", "other"],
        input_version: 1,
        purpose: "task_clarification",
        origin_run_id: null,
        choice_details: [],
      },
      reconciliation: null,
      task_outcome: null,
      input_version: 1,
      input_history: [],
    });

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    expect(
      useWorkspaceStore.getState().taskRuntime[THREAD_ID]?.pending_question?.choices,
    ).toEqual(["苹果公司", "2023 年度", "other"]);

    const store = useWorkspaceStore.getState();
    store.openTaskClarification(THREAD_ID);
    store.selectTaskChoice(THREAD_ID, "苹果公司");
    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    expect(door.bodies[0]).toMatchObject({
      question_id: QUESTION_ID,
      choice_id: "苹果公司",
    });
  });

  it("reads an absent envelope as taskless rather than broken", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const view = useWorkspaceStore.getState().taskRuntime[THREAD_ID];
    expect(view?.current_task).toBeNull();
    expect(view?.archived_tasks).toEqual([]);
  });

  it("records a MALFORMED envelope as a projection refusal, not as taskless silence", async () => {
    // A malformed envelope decodes to the empty view, so the store alone
    // cannot tell a broken task channel from a taskless thread. The
    // decode refusal must be recorded per-thread (door "projection") so
    // the surface can render the "unrecorded" copy instead of silently
    // asserting "no tasks".
    // Seeded so the archive surface below is read with no restore request
    // open, exactly as a plain thread open leaves it.
    seedTaskThread(envelope());
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
            task_runtime: { current_task: "not-an-object" },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const state = useWorkspaceStore.getState();
    expect(state.taskProjectionRefusal[THREAD_ID]).toBe("malformed_task_runtime");
    // The refusal surfaces on the seam every task read renders through, so
    // a surface never silently asserts "no tasks" for a broken channel.
    expect(projectionUnreadableFor(state, THREAD_ID)).toBe(true);
    // The refusal names unreadable CONTENT, not an unreachable host: the
    // wire-failure marker stays absent, so the two causes keep distinct
    // copy.
    expect(state.taskProjectionRefreshFailed[THREAD_ID]).toBeUndefined();
    // The unrecorded view still arrives WITH the refusal (never invented
    // content alongside it).
    expect(state.taskRuntime[THREAD_ID]?.current_task).toBeNull();
    // And with zero clicks there is no restore to confirm or to fail: the
    // broken channel is stated by the projection-refusal paragraph alone, so
    // the archive surface must claim nothing about a restore nobody asked
    // for.
    const decision = restoreDecision(restoreFactsFor(state, THREAD_ID));
    expect(decision.state).toBe("idle");
    expect(decision.notice).toBeNull();
    expect(decision.projectionUnresolved).toBe(false);
    expect(decision.restoredArchiveId).toBeNull();
  });

  it("does not record a decode refusal for a legacy thread with no envelope", async () => {
    // The absent key is the legacy-host case: taskless, but NOT broken.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    const state = useWorkspaceStore.getState();
    expect(state.taskProjectionRefusal[THREAD_ID]).toBeUndefined();
    // A legacy thread with no envelope reads as taskless, so the seam must
    // read readable even where an earlier read of this thread was refused.
    expect(projectionUnreadableFor(state, THREAD_ID)).toBe(false);
  });

  it("clears a stale projection refusal once a read decodes cleanly", async () => {
    // First open: malformed envelope, refusal recorded.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
            task_runtime: { current_task: "not-an-object" },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    await useWorkspaceStore.getState().openThread(THREAD_ID);
    expect(useWorkspaceStore.getState().taskProjectionRefusal[THREAD_ID]).toBe(
      "malformed_task_runtime",
    );

    // Second read: the host is healthy again.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            id: THREAD_ID,
            title: "AAPL 10-Q",
            created_at: "2026-01-01T00:00:00.000Z",
            turns: [],
            task_runtime: envelope(),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    await useWorkspaceStore.getState().reloadTaskProjection(THREAD_ID);

    const state = useWorkspaceStore.getState();
    expect(state.taskProjectionRefusal[THREAD_ID]).toBeUndefined();
    // The seam the surfaces render flips back: a refusal must not outlive
    // the envelope that caused it.
    expect(projectionUnreadableFor(state, THREAD_ID)).toBe(false);
  });
});

describe("reloadTaskProjection", () => {
  /** A detail GET answering ``projection`` on /threads/{id} and a plain
   * JSON body for every other URL (list, etc.), recording the GETs. */
  function stubDetail(projection: unknown): { detailGets: number } {
    const state = { detailGets: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : String(input);
        if (url.includes(`/threads/${THREAD_ID}`)) {
          state.detailGets += 1;
          return new Response(
            JSON.stringify({
              id: THREAD_ID,
              title: "AAPL 10-Q",
              created_at: "2026-01-01T00:00:00.000Z",
              turns: [],
              task_runtime: projection,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    return state;
  }

  it("clears the unreadable-projection seam once the envelope reads cleanly", async () => {
    // The stale decode refusal must not outlive the envelope that caused
    // it — and a door's own refusal must survive the same clean re-read:
    // a projection read is not the host's verdict on the door's request.
    const opened = beginWorkspaceOperation(EMPTY_WORKSPACE_OPERATIONS, {
      threadId: THREAD_ID,
      door: "reply",
      kind: "reply",
      questionId: QUESTION_ID,
    });
    if (opened.operation === null) {
      throw new Error("fixture opened nothing");
    }
    useWorkspaceStore.setState({
      workspaceOperations: applyWorkspaceOperationResult(opened.ledger, {
        threadId: THREAD_ID,
        attempt: opened.operation.attempt,
        state: "rejected",
        reason: "task_reply_expired",
      }),
      taskProjectionRefusal: { [THREAD_ID]: "malformed_task_runtime" },
      // The seam reads unreadable because of the decode refusal alone, so
      // a leftover wire-failure marker cannot be what this test clears.
      taskProjectionRefreshFailed: {},
    });
    expect(
      projectionUnreadableFor(useWorkspaceStore.getState(), THREAD_ID),
    ).toBe(true);
    stubDetail(envelope());

    await useWorkspaceStore.getState().reloadTaskProjection(THREAD_ID);

    const state = useWorkspaceStore.getState();
    expect(state.taskProjectionRefusal[THREAD_ID]).toBeUndefined();
    expect(projectionUnreadableFor(state, THREAD_ID)).toBe(false);
    expect(
      operationRefusal(
        currentWorkspaceOperation(state.workspaceOperations, THREAD_ID),
      ),
    ).toEqual({
      kind: "task_reply_expired",
      questionId: QUESTION_ID,
      door: "reply",
    });
  });

  it("leaves the previous projection AND a door refusal untouched when the re-read fails", async () => {
    // The re-read is presentation-only: its failure must neither wipe
    // the last known projection nor the operator-facing refusal a door
    // just recorded. Both stay, and the unavailability is recorded as
    // its own observable marker rather than swallowed.
    seedTaskThread(
      envelope({
        archived_tasks: [archiveRow()],
        current_task: {
          task_id: "task-keep",
          goal: "保留的任务",
          expected_result: "",
          task_version: 1,
          necessary_conditions: [],
          outcome: "active",
          outcome_reason: "",
          created_at_input_version: 3,
        },
      }),
    );
    // The named refusal and the failed re-read both come through the real
    // restore door, so the record the surface reads is the one production
    // writes rather than a hand-built fixture.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(
            JSON.stringify({ detail: { error: "task_restore_not_adopted" } }),
            { status: 400, headers: { "content-type": "application/json" } },
          );
        }
        return new Response("boom", { status: 503 });
      }),
    );

    await useWorkspaceStore
      .getState()
      .restoreArchivedTask(THREAD_ID, "archive-task-1-v1");

    const state = useWorkspaceStore.getState();
    expect(state.taskRuntime[THREAD_ID]?.current_task?.task_id).toBe("task-keep");
    expect(
      operationRefusal(displayableWorkspaceOperation(state, THREAD_ID)),
    ).toEqual({
      kind: "task_restore_not_adopted",
      questionId: null,
      door: "restore",
    });
    expect(state.taskProjectionRefreshFailed[THREAD_ID]).toBe(true);
    // The restore's answer belongs to the restore: it stays on its own
    // request record, so nothing being readable now can erase it.
    expect(state.restoreRequests[THREAD_ID]?.refusalKind).toBe(
      "task_restore_not_adopted",
    );
    // What the archive surface ends up rendering: the request's own refusal
    // stands, the listing is free again, and the task it names is still the
    // last readable one.
    const decision = restoreDecision(restoreFactsFor(state, THREAD_ID));
    expect(decision.state).toBe("refused");
    expect(decision.notice).toBe(restoreRefusalCopy("task_restore_not_adopted"));
    expect(decision.requestUnsettled).toBe(false);
    expect(decision.currentTaskId).toBe("task-keep");
  });

  it("clears the refresh-failed marker on the next successful read", async () => {
    useWorkspaceStore.setState({
      taskProjectionRefreshFailed: { [THREAD_ID]: true },
    });
    stubDetail(envelope());

    await useWorkspaceStore.getState().reloadTaskProjection(THREAD_ID);

    expect(
      useWorkspaceStore.getState().taskProjectionRefreshFailed[THREAD_ID],
    ).toBeUndefined();
  });
});

describe("restoreArchivedTask (W5)", () => {
  it("names the archive explicitly and keeps the current task until the host confirms", async () => {
    seedTaskThread(
      envelope({
        archived_tasks: [archiveRow()],
        current_task: {
          task_id: "task-2",
          goal: "现在这个问题",
          expected_result: "",
          task_version: 1,
          necessary_conditions: [],
          outcome: "active",
          outcome_reason: "",
          created_at_input_version: 3,
        },
      }),
    );
    const door = stubTaskDoor(envelope());

    await useWorkspaceStore.getState().restoreArchivedTask(THREAD_ID, "archive-task-1-v1");

    expect(door.bodies[0]).toMatchObject({ restore_archive_id: "archive-task-1-v1" });

    const state = useWorkspaceStore.getState();
    const decision = restoreDecision(restoreFactsFor(state, THREAD_ID));
    // The click is remembered as an explicit request for THIS archive...
    expect(state.restoreRequests[THREAD_ID]?.archiveId).toBe("archive-task-1-v1");
    // ...and adoption is still owed: the send was durably accepted, yet
    // acceptance is an owned state, never a confirmation, so the surface
    // may not claim 已恢复 off this record.
    expect(state.restoreRequests[THREAD_ID]?.accepted).toBe(true);
    // No verdict named yet, and the host has answered the request, so this
    // owed confirmation cannot park the listing.
    expect(state.restoreRequests[THREAD_ID]?.refusalKind).toBeNull();
    expect(decision.state).toBe("awaiting_confirmation");
    expect(decision.requestUnsettled).toBe(false);
    expect(decision.restoredArchiveId).toBeNull();
    expect(decision.pendingArchiveId).toBe("archive-task-1-v1");
    // The fresh read this stub answers reports no current task, so the
    // decision restates the host's latest word instead of keeping the
    // pre-click task visible — and still confirms nothing.
    expect(decision.currentTaskId).toBeNull();
  });

  it("records the refusal and leaves the current task exactly where it was", async () => {
    // A refused restore is the host saying no, which leaves its own state
    // untouched: the same current task, the same archive still listed. The
    // stub answers the re-read that refusal triggers with exactly that, so
    // "unchanged" is proven against a read rather than against the absence
    // of one.
    const refused = envelope({
      archived_tasks: [archiveRow()],
      current_task: {
        task_id: "task-2",
        goal: "现在这个问题",
        expected_result: "",
        task_version: 1,
        necessary_conditions: [],
        outcome: "active",
        outcome_reason: "",
        created_at_input_version: 3,
      },
    });
    seedTaskThread(refused);
    stubTaskRefusal(400, "task_restore_not_adopted", undefined, refused);

    // The named refusal resolves: the archive section renders it from the
    // recorded verdict below, and a rethrow would reach no notice path.
    await expect(
      useWorkspaceStore
        .getState()
        .restoreArchivedTask(THREAD_ID, "archive-task-1-v1"),
    ).resolves.toBeUndefined();

    const state = useWorkspaceStore.getState();
    const operation = displayableWorkspaceOperation(state, THREAD_ID);
    expect(operationRefusal(operation)).toEqual({
      kind: "task_restore_not_adopted",
      questionId: null,
      door: "restore",
    });
    expect(operation?.state).toBe("rejected");
    expect(state.taskRuntime[THREAD_ID]?.current_task?.task_id).toBe("task-2");
    // The re-read landed: ``seenSinceSend`` moves only when a projection
    // read arrives, so the current task above is the host's latest word,
    // not the click-time snapshot.
    expect(state.restoreRequests[THREAD_ID]?.seenSinceSend).toBe(true);
    // The named answer is kept on the restore's own record, which is what
    // lets the sentence outlive any later operation on this thread.
    expect(state.restoreRequests[THREAD_ID]?.refusalKind).toBe(
      "task_restore_not_adopted",
    );
    // And the archive section names the refusal rather than an adoption:
    // the row is still offered, so nothing here can read as restored.
    const decision = restoreDecision(restoreFactsFor(state, THREAD_ID));
    expect(decision.state).toBe("refused");
    expect(decision.notice).toBe(restoreRefusalCopy("task_restore_not_adopted"));
    expect(decision.restoredArchiveId).toBeNull();
  });
});

describe("a mid-stream task refusal", () => {
  it("reads a restore refused at reconciliation as rejected, not accepted", async () => {
    // The acceptance succeeds (the restore is durably recorded) and the
    // reconciliation then refuses it, which arrives as a terminal
    // ``refuse`` frame on a 200 stream rather than an HTTP error. Reading
    // only the status would report an accepted restore and show the
    // operator nothing.
    seedTaskThread(envelope({ archived_tasks: [archiveRow()] }));
    const frame = `event: status\ndata: ${JSON.stringify({
      run_id: "22222222-2222-4222-8222-222222222222",
      status: "refuse",
      reason: "task_applicability_unresolved",
      assistant_message: "适用条件尚未确认，任务保持进行中。",
      status_classified: "task_applicability_unresolved",
    })}\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(frame, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        return new Response(JSON.stringify(detailBody(envelope())), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    await useWorkspaceStore
      .getState()
      .restoreArchivedTask(THREAD_ID, "archive-task-1-v1");

    const state = useWorkspaceStore.getState();
    const operation = displayableWorkspaceOperation(state, THREAD_ID);
    expect(operation?.state).toBe("rejected");
    expect(operationRefusal(operation)).toEqual({
      kind: "task_applicability_unresolved",
      questionId: null,
      door: "restore",
    });
    // The archive surface renders this as refused even though the fresh
    // read after the door no longer lists the archive: the door's refusal
    // outranks a projection that could otherwise be mis-read as adoption.
    expect(restoreDecision(restoreFactsFor(state, THREAD_ID)).state).toBe(
      "refused",
    );
  });

  it("reads an ordinary non-task refusal as an accepted restore", async () => {
    // The converse guard: a ``refuse`` carrying a NON-task reason is a run
    // outcome, not a task-door refusal, and must not be reported as one.
    seedTaskThread(envelope({ archived_tasks: [archiveRow()] }));
    const frame = `event: status\ndata: ${JSON.stringify({
      run_id: "22222222-2222-4222-8222-222222222222",
      status: "refuse",
      reason: "step_cap",
      assistant_message: "达到步数上限。",
      status_classified: "server",
    })}\n\n`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(frame, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        return new Response(JSON.stringify(detailBody(envelope())), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    await useWorkspaceStore
      .getState()
      .restoreArchivedTask(THREAD_ID, "archive-task-1-v1");

    const state = useWorkspaceStore.getState();
    const operation = displayableWorkspaceOperation(state, THREAD_ID);
    expect(operation?.state).toBe("accepted");
    // An accepted verdict carries no refusal.
    expect(operationRefusal(operation)).toBeNull();
    // And acceptance is still not adoption: the fresh read did not report
    // the clicked row's task as current, so the request stays owed.
    expect(restoreDecision(restoreFactsFor(state, THREAD_ID)).state).toBe(
      "awaiting_confirmation",
    );
  });
});

describe("the task door's typed refusal", () => {
  it("is distinguishable from a held run on the same status", () => {
    const refusal = new TaskInputRefusedError("task_reply_expired", QUESTION_ID);
    expect(refusal.kind).toBe("task_reply_expired");
    expect(refusal.questionId).toBe(QUESTION_ID);
  });
});

// An ordinary send or pick can move the task envelope — it starts work,
// completes it, archives it — so the settle point re-reads the projection
// the same way it already refreshes the thread list. Re-reading only after
// the task doors would leave the panel and the archive list on the
// pre-send reading until an explicit reload.
describe("an ordinary send or pick refreshes the task projection", () => {
  const RUN_ID = "22222222-2222-4222-8222-222222222222";

  function threadDetail(taskRuntime: TaskRuntimeView): Record<string, unknown> {
    return {
      id: THREAD_ID,
      title: "AAPL 10-Q",
      created_at: "2026-01-01T00:00:00.000Z",
      turns: [],
      task_runtime: taskRuntime,
    };
  }

  /** Streams the given frames on POST, answers every GET with
   * ``detail`` (updated after the stream by mutating the holder), and
   * records the GET detail bodies seen. */
  function stubStreamThenDetail(frames: [string, Record<string, unknown>][]): {
    detailBodies: unknown[];
  } {
    const detailHolder: { body: Record<string, unknown> } = {
      body: threadDetail(envelope()),
    };
    const detailBodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : String(input);
        if (init?.method === "POST") {
          const body = new ReadableStream<Uint8Array>({
            pull(controller) {
              for (const [event, payload] of frames) {
                controller.enqueue(
                  new TextEncoder().encode(
                    `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`,
                  ),
                );
              }
              controller.close();
            },
          });
          return new Response(body, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        if (url.includes(`/threads/${THREAD_ID}`)) {
          detailBodies.push(detailHolder.body);
          return new Response(JSON.stringify(detailHolder.body), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    // Mutate between the POST and the post-settle GETs to prove the
    // projection the store records is the POST-SETTLE one, not the
    // pre-send one.
    queueMicrotask(() => {
      detailHolder.body = threadDetail(
        envelope({
          current_task: {
            task_id: "task-new",
            goal: "发送后的当前任务",
            expected_result: "",
            task_version: 1,
            necessary_conditions: [],
            outcome: "active",
            outcome_reason: "",
            created_at_input_version: 1,
          },
        }),
      );
    });
    return { detailBodies };
  }

  it("sendQuestion re-reads the projection after the stream settles", async () => {
    seedTaskThread(envelope());
    const { detailBodies } = stubStreamThenDetail([
      [
        "status",
        { run_id: RUN_ID, status: "done", assistant_message: "" },
      ],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "对比两个期间");

    const view = useWorkspaceStore.getState().taskRuntime[THREAD_ID];
    // The stale pre-send projection is gone; the post-settle envelope
    // the GET now serves is what the panel reads.
    expect(view?.current_task?.task_id).toBe("task-new");
    expect(detailBodies).toHaveLength(1);
  });

  it("pickChoice re-reads the projection after the pick settles", async () => {
    const pendingRunId = "44444444-4444-4444-8444-444444444444";
    useWorkspaceStore.setState({
      activeThread: {
        id: THREAD_ID,
        title: "AAPL 10-Q",
        created_at: "2026-01-01T00:00:00.000Z",
        turns: [],
        pending_turn: {
          run_id: pendingRunId,
          question: "营收多少？",
          choice_window: {
            kind: "choice_window",
            title: "选择检索档",
            options: [{ id: "graph", label: "知识问答", opened: true }],
          },
        },
      },
      threads: [],
      sendingQuestion: false,
      resumingThreadId: null,
    });
    const { detailBodies } = stubStreamThenDetail([
      [
        "status",
        { run_id: pendingRunId, status: "done", assistant_message: "" },
      ],
    ]);

    await useWorkspaceStore.getState().pickChoice(THREAD_ID, pendingRunId, "graph");

    const view = useWorkspaceStore.getState().taskRuntime[THREAD_ID];
    expect(view?.current_task?.task_id).toBe("task-new");
    expect(detailBodies).toHaveLength(1);
  });

  it("a failed send does NOT re-read the projection", async () => {
    // The re-read lives on the success path: a failed send rethrows to
    // the composer and must not spend a GET, and the projection keeps
    // whatever was last authoritative.
    seedTaskThread(envelope({ current_task: null }));
    let detailGets = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : String(input);
        if (!url.endsWith("/messages")) {
          if (url.includes(`/threads/${THREAD_ID}`)) detailGets += 1;
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ error: "concurrent_run" }), {
          status: 409,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "对比两个期间"),
    ).rejects.toBeInstanceOf(api.ConcurrentRunError);

    expect(detailGets).toBe(0);
    expect(useWorkspaceStore.getState().taskRuntime[THREAD_ID]).toEqual(
      envelope({ current_task: null }),
    );
  });
});
