// The ``document_choice`` SSE frame path (specs/qa-chain-grounded-experience.md
// §GE-4).
//
// Before this, the host emitted a ``document_choice`` frame
// (``workspace_http`` stream, ``_frame("document_choice", value)``) and the
// client silently dropped it in ``consumeSseStream``'s ``default:`` branch
// — so a run suspended for a grounded document choice looked finished and
// the operator was never offered the ≤4 hit-backed candidates GE-4 requires.
//
// The frame is a REFERENCE, not a candidate list: it names the committed
// question and the run it interrupted, and carries no ``choice_details``.
// These tests pin both halves of that posture —
//
// 1. the frame is ADMITTED (no longer skipped) and narrows to its
//    reference fields, and
// 2. a pick built from the authoritative projection round-trips on the
//    reply body the host's ``_precheck_document_reply`` reads
//    (``{question_id, choice_id}``), binding to the original run.
//
// The wire is stubbed; no host process runs.

import { afterEach, describe, expect, it, vi } from "vitest";
import { getTurnSpans, sendMessage, type SendEvent } from "./api";
import {
  decodeThreadTaskRuntime,
  documentChoicePick,
  documentChoiceView,
  parsePendingQuestion,
} from "./capabilities/task/taskRuntime";
import { useWorkspaceStore } from "./store";
import { EMPTY_WORKSPACE_OPERATIONS } from "./workspaceOperation";
import { documentChoiceSurface } from "./capabilities/clarification/documentChoiceSurface";
import { openClarification, selectChoice, setOtherText } from "./capabilities/clarification/clarificationReply";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const ORIGIN_RUN_ID = "77777777-7777-4777-8777-777777777777";

/** The host's ``_document_choice_payload`` frame. NOTE what it does NOT
 * carry: any ``choice_details``. The candidate text lives only in the GET
 * projection's ``pending_question``, which is why a client that painted
 * from this frame would show the operator opaque ids. */
const DOCUMENT_CHOICE_FRAME = {
  kind: "document_choice",
  question_id: "q-doc",
  purpose: "document_choice",
  prompt: "要用哪一份资料对比？",
  choices: ["choice-1", "choice-2", "other"],
  origin_run_id: ORIGIN_RUN_ID,
  other_label: "换个问法",
};

/** The authoritative projection row — the ONLY candidate authority. */
const PENDING_DOCUMENT_CHOICE = {
  question_id: "q-doc",
  // The host publishes the choice after retrieval, where a current task
  // may not exist, and sends an EMPTY task_id in exactly that case
  // (``task_runtime.open_pending_question``). It is carried verbatim.
  task_id: "",
  task_version: 1,
  prompt: "要用哪一份资料对比？",
  choices: ["choice-1", "choice-2", "other"],
  input_version: 1,
  purpose: "document_choice",
  origin_run_id: ORIGIN_RUN_ID,
  choice_details: [
    {
      choice_id: "choice-1",
      label: "AAPL 10-Q FY2023Q1",
      summary: "第一季度的营收与利润概述。",
      source_ref: {
        source_kind: "corpus",
        source_id: "AAPL-10Q-2023Q1",
        version: "v1",
        section: null,
      },
    },
    {
      choice_id: "choice-2",
      label: "AAPL 10-Q FY2023Q2",
      summary: "第二季度的营收与利润概述。",
      source_ref: {
        source_kind: "attachment",
        source_id: "att-22",
        version: "v1",
        section: "Section 2",
      },
    },
  ],
};

type Frame = [string, unknown];

function sseFrame([event, data]: Frame): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Stub the wire: a /messages or /resume POST answers with the NEXT
 * queued frame list (one chunk per frame); anything else answers an empty
 * JSON array (the tail ``refreshThreads``). Records the parsed body of
 * each POST so the pick round-trip can be asserted. */
function stubWire(streams: Frame[][]): { body: unknown; url: string }[] {
  const calls: { body: unknown; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      if (!url.endsWith("/messages") && !url.endsWith("/resume")) {
        return new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      calls.push({
        url,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      });
      const frames = streams.shift() ?? [];
      let next = 0;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (next >= frames.length) {
            controller.close();
            return;
          }
          controller.enqueue(new TextEncoder().encode(sseFrame(frames[next])));
          next += 1;
        },
      });
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }),
  );
  return calls;
}

/** The events of one type. Narrowed by the discriminant, so a caller may
 * read the fields its own variant carries. */
function eventsOf<T extends SendEvent["type"]>(
  list: SendEvent[],
  type: T,
): Extract<SendEvent, { type: T }>[] {
  return list.filter(
    (e): e is Extract<SendEvent, { type: T }> => e.type === type,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the document_choice frame is admitted, not skipped", () => {
  it("emits a document_choice event carrying the question and its originating run", async () => {
    stubWire([[["document_choice", DOCUMENT_CHOICE_FRAME]]]);
    const events: SendEvent[] = [];

    await sendMessage(THREAD_ID, { question: "对比一下营收" }, (e) => events.push(e));

    const admitted = eventsOf(events, "document_choice");
    expect(admitted).toHaveLength(1);
    // The reference names the question and the run it interrupted; the
    // operator's pick must bind to that same run (GE-4's "preserves the
    // original run/receipt").
    expect(admitted[0]).toEqual({
      type: "document_choice",
      frame: {
        question_id: "q-doc",
        origin_run_id: ORIGIN_RUN_ID,
        prompt: "要用哪一份资料对比？",
        choices: ["choice-1", "choice-2", "other"],
        other_label: "换个问法",
      },
    });
  });

  it("carries no terminal frames: the run is waiting on the operator", async () => {
    stubWire([[["document_choice", DOCUMENT_CHOICE_FRAME]]]);
    const events: SendEvent[] = [];

    await sendMessage(THREAD_ID, { question: "对比一下营收" }, (e) => events.push(e));

    // No status / answer / citations frames arrive while the run waits.
    // Suspension itself is NOT read off ``SendResult.status`` (that field
    // is the terminal aggregate and stays at its initializer — the same
    // for a choice window); the store decides "suspended, not done" from
    // the frame it saw, which store.test.ts covers.
    expect(eventsOf(events, "status")).toHaveLength(0);
    expect(eventsOf(events, "answer")).toHaveLength(0);
    expect(eventsOf(events, "citations")).toHaveLength(0);
  });

  it("skips a malformed frame rather than crashing the stream", async () => {
    // Same skip-don't-crash boundary as choice_window / attachment_wait.
    stubWire([
      [
        ["document_choice", { ...DOCUMENT_CHOICE_FRAME, origin_run_id: null }],
        ["document_choice", { ...DOCUMENT_CHOICE_FRAME, kind: "choice_window" }],
        ["answer", { text: "答案" }],
      ],
    ]);
    const events: SendEvent[] = [];

    await sendMessage(THREAD_ID, { question: "对比" }, (e) => events.push(e));

    expect(eventsOf(events, "document_choice")).toHaveLength(0);
    // The stream keeps going: an unreadable frame does not end the run.
    expect(eventsOf(events, "answer")).toHaveLength(1);
  });
});

describe("the frame yields a renderable question with candidates", () => {
  it("renders candidates from the projection, never from the frame's ids", () => {
    const question = parsePendingQuestion(PENDING_DOCUMENT_CHOICE);
    expect(question).not.toBeNull();

    const surface = documentChoiceSurface(
      question,
      openClarification(question!),
      false,
    );
    expect(surface).not.toBeNull();
    // GE-4: "≤4 hit-backed candidates have title/section/summary and
    // trailing rephrase" — the operator reads the document's title, the
    // section it was admitted from, and a one-line summary. NOT the opaque
    // ``choice-1`` the wire list carries.
    expect(surface?.rows.map((r) => r.title)).toEqual([
      "AAPL 10-Q FY2023Q1",
      "AAPL 10-Q FY2023Q2",
    ]);
    expect(surface?.rows.map((r) => r.section)).toEqual([null, "Section 2"]);
    expect(surface?.rows[0].summary).toBe("第一季度的营收与利润概述。");
    // The trailing rephrase carries the contract's own label.
    expect(surface?.rephrase.label).toBe("换个问法");
    expect(surface?.origin_run_id).toBe(ORIGIN_RUN_ID);
    // Nothing is preselected.
    expect(surface?.rows.every((r) => !r.selected)).toBe(true);
  });

  it("decodes the host's empty task_id and still renders the question", () => {
    // The document choice is published after retrieval, where no current
    // task exists; the host sends task_id="". The client renders on the
    // identity the host DOES supply — question_id + origin_run_id.
    const question = parsePendingQuestion(PENDING_DOCUMENT_CHOICE);
    expect(question?.task_id).toBe("");
    expect(documentChoiceView(question)).not.toBeNull();
  });
});

describe("a pick round-trips on the reply body the host reads", () => {
  it("submits {question_id, choice_id} for the picked candidate", async () => {
    const question = parsePendingQuestion(PENDING_DOCUMENT_CHOICE)!;
    const pick = documentChoicePick(question, "choice-2");
    expect(pick).not.toBeNull();

    // Exactly the body ``_precheck_document_reply`` reads: a named
    // question plus a named choice, with NO free text on a click.
    const calls = stubWire([[]]);
    await sendMessage(THREAD_ID, {
      question: "",
      question_id: pick!.question_id,
      choice_id: pick!.choice_id,
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/api/threads/${THREAD_ID}/messages`);
    expect(calls[0].body).toEqual({
      question: "",
      question_id: "q-doc",
      choice_id: "choice-2",
    });
    // A click carries the id only — the label the operator read is never
    // sent back as the answer (payload stays null on the wire).
    expect((calls[0].body as Record<string, unknown>).free_text).toBeUndefined();
  });

  it("binds the rephrase to the same question with its verbatim free text", () => {
    const question = parsePendingQuestion(PENDING_DOCUMENT_CHOICE)!;
    let draft = openClarification(question);
    draft = selectChoice(draft, "other");
    draft = setOtherText(draft, "换个角度，用现金流量表");

    expect(draft.selectedChoiceId).toBe("other");
    expect(draft.otherText).toBe("换个角度，用现金流量表");
    // The rephrase names the SAME question — a pick can never escape the
    // question it was asked by.
    expect(draft.questionId).toBe("q-doc");
    // And it is not a candidate pick: the Other id resolves to no admitted
    // candidate, so it submits text rather than a choice_id.
    expect(documentChoicePick(question, "other")).toBeNull();
  });

  it("refuses a pick naming a candidate the question never offered", () => {
    const question = parsePendingQuestion(PENDING_DOCUMENT_CHOICE)!;
    expect(documentChoicePick(question, "choice-9")).toBeNull();
    expect(documentChoicePick(question, "")).toBeNull();
  });
});

describe("the store keeps a document-choice run suspended, then answers it", () => {
  /** A detail body carrying ``task_runtime``: every post-door re-read runs
   * a real ``GET /api/threads/{id}`` through ``decodeThreadDetail``, so a
   * bare envelope would fail the thread decode rather than exercise the
   * read. */
  function detailBody(runtime: Record<string, unknown>): Record<string, unknown> {
    return {
      id: THREAD_ID,
      title: "AAPL 10-Q",
      created_at: "2026-01-01T00:00:00.000Z",
      turns: [],
      task_runtime: runtime,
    };
  }

  const PROJECTION = {
    current_task: null,
    archived_tasks: [],
    pending_question: PENDING_DOCUMENT_CHOICE,
    reconciliation: null,
    task_outcome: null,
    input_version: 1,
    input_history: [],
  };

  /** Stub the whole wire: a POST answers the NEXT queued SSE stream, a
   * thread detail GET answers ``projection``, and any other GET (the
   * thread list a send's settle point refreshes) answers empty. */
  function stubStoreWire(
    streams: Frame[][],
    projection: Record<string, unknown>,
  ): { body: unknown; url: string }[] {
    const calls: { body: unknown; url: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : String(input);
        if (init?.method === "POST") {
          calls.push({
            url,
            body: typeof init.body === "string" ? JSON.parse(init.body) : null,
          });
          const frames = streams.shift() ?? [];
          let next = 0;
          const body = new ReadableStream<Uint8Array>({
            pull(controller) {
              if (next >= frames.length) {
                controller.close();
                return;
              }
              controller.enqueue(new TextEncoder().encode(sseFrame(frames[next])));
              next += 1;
            },
          });
          return new Response(body, {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        if (url.includes(`/threads/${THREAD_ID}`)) {
          return new Response(JSON.stringify(detailBody(projection)), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    return calls;
  }

  function seedActiveThread(): void {
    useWorkspaceStore.setState({
      threads: [],
      activeThread: {
        id: THREAD_ID,
        title: "AAPL 10-Q",
        created_at: "2026-01-01T00:00:00.000Z",
        turns: [],
      },
      sendingQuestion: false,
      resumingThreadId: false as never,
      taskRuntime: {},
      clarificationDrafts: {},
      workspaceOperations: EMPTY_WORKSPACE_OPERATIONS,
      restoreRequests: {},
      taskProjectionRefusal: {},
      taskProjectionRefreshFailed: {},
    });
  }

  it("leaves the turn suspended (in_flight) instead of finalizing it as done", async () => {
    // The defect this pins: the host suspends the run on a document
    // choice and emits NO terminal frames. Before the fix the frame was
    // skipped, so the store saw a stream with nothing on it and finalized
    // the row as a finished run — the operator saw a "done" answer with
    // no question and nothing to pick.
    seedActiveThread();
    stubStoreWire(
      [[["document_choice", DOCUMENT_CHOICE_FRAME]]],
      PROJECTION,
    );

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "对比一下营收");

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    const turn = turns[turns.length - 1];
    expect(turn.in_flight).toBe(true);
  });

  it("re-reads the projection on the frame, so the candidates are the host's", async () => {
    // The frame carries no choice_details, so the settle-point re-read is
    // the ONLY thing that can put the candidates on screen. GE-4's
    // "browser reload / pick / follow-up preserves the original run": the
    // question read back is the same one the frame named.
    seedActiveThread();
    stubStoreWire(
      [[["document_choice", DOCUMENT_CHOICE_FRAME]]],
      PROJECTION,
    );

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "对比一下营收");

    const pending = useWorkspaceStore.getState().taskRuntime[THREAD_ID]?.pending_question;
    expect(pending).not.toBeNull();
    expect(pending?.question_id).toBe("q-doc");
    expect(pending?.purpose).toBe("document_choice");
    expect(pending?.origin_run_id).toBe(ORIGIN_RUN_ID);
    // The host's empty task_id survives the round trip verbatim.
    expect(pending?.task_id).toBe("");
    expect(pending?.choice_details).toHaveLength(2);
  });

  it("answers the question through the reply door with the picked id", async () => {
    // End to end: the operator picks a candidate on the decoded question,
    // and the reply door posts exactly the body
    // ``_precheck_document_reply`` validates — a named question and a
    // named candidate, both from the ORIGINAL question.
    seedActiveThread();
    useWorkspaceStore.setState({
      taskRuntime: {
        [THREAD_ID]: decodeThreadTaskRuntime(detailBody(PROJECTION)).view,
      },
    });
    useWorkspaceStore.getState().openTaskClarification(THREAD_ID);
    useWorkspaceStore.getState().selectTaskChoice(THREAD_ID, "choice-2");

    const calls = stubStoreWire(
      [
        [
          ["status", { run_id: ORIGIN_RUN_ID, status: "done", assistant_message: "" }],
        ],
      ],
      PROJECTION,
    );

    await useWorkspaceStore.getState().submitTaskReply(THREAD_ID);

    const post = calls.find((c) => c.url.endsWith("/messages"));
    expect(post?.body).toEqual({
      question: "",
      question_id: "q-doc",
      choice_id: "choice-2",
    });
  });
});

// spec search-mode-host item 7: "Project the full decision through the
// existing graph state, invoke result, and terminal SSE/status receipt
// paths." This is the projection half of that contract: the receipt the
// host sends on the terminal status frame must SURVIVE the client's
// parser, or the run loses its provenance and the judge's diagnostics.
describe("a fallback receipt survives the status-frame projection", () => {
  it("keeps the tier, the mode, the confidence and the allowlisted signals", async () => {
    // The regression this pins: the ``fallback`` tier was not in the
    // client's tier set, so the whole receipt was dropped as malformed.
    // A dropped receipt leaves the fallback's 0.0 looking like the
    // judge's own (unrecorded) probability, and loses the EXIT that
    // explains WHY the cascade fell back.
    const FALLBACK_DECISION = {
      search_mode: "graph",
      confidence: 0,
      decision_tier: "fallback",
      signals: {
        mode_jev_exit: "low_confidence",
        mode_jev_label: "graph",
        mode_jev_confidence: 0.44,
        mode_jev_error_type: "JevChoiceError",
        // Outside the bounded allowlist: the host records it, the
        // browser does not keep it.
        requested_mode: "graph",
      },
      options: [],
      failure_code: null,
    };

    stubWire([
      [
        [
          "status",
          {
            run_id: "run-fallback",
            status: "done",
            assistant_message: "",
            search_mode_decision: FALLBACK_DECISION,
          },
        ],
      ],
    ]);
    const events: SendEvent[] = [];

    const result = await sendMessage(
      THREAD_ID,
      { question: "年假怎么算" },
      (e) => events.push(e),
    );

    const expected = {
      search_mode: "graph",
      decision_tier: "fallback",
      confidence: 0,
      signals: {
        mode_jev_exit: "low_confidence",
        mode_jev_label: "graph",
        mode_jev_confidence: 0.44,
        mode_jev_error_type: "JevChoiceError",
      },
      // The host's state dict always carries the key; a null one is
      // "no named failure", not a failure.
      failure_code: null,
    };
    // The terminal aggregate keeps it ...
    expect(result.search_mode_decision).toEqual(expected);
    // ... and so does the streamed status event, which is what the live
    // turn is built from.
    const status = eventsOf(events, "status");
    expect(status).toHaveLength(1);
    expect(status[0].search_mode_decision).toEqual(expected);
  });
});

describe("the span view read", () => {
  it("addresses one turn and keeps an empty trace a success", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          thread_id: "thread 1",
          turn_id: "turn/1",
          trace_state: "off",
          spans: [],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await getTurnSpans("thread 1", "turn/1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads/thread%201/turns/turn%2F1/spans",
    );
    expect(result).toEqual({
      thread_id: "thread 1",
      turn_id: "turn/1",
      trace_state: "off",
      spans: [],
    });
  });

  it("throws on a malformed payload rather than rendering it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ spans: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    await expect(getTurnSpans("thread-1", "turn-1")).rejects.toThrow(
      "malformed /spans payload",
    );
  });
});
