// The visual-claim rows on the live wire and across a reload.
//
// Three things the operator depends on:
//
// 1. The rows appear WHILE the run streams. The door emits a distinct
//    ``visual_claims`` event after the answer frames and before the
//    terminal ``citations`` event, so the lane is on screen without
//    waiting for a reload.
// 2. It is NOT a citation. The fold must never put a row into
//    ``Turn.citations`` (that is the quote channel, and a generated
//    observation has no quote), and must not disturb the terminal
//    ``citations`` frame's close behaviour.
// 3. The same field lands from history. ``openThread`` reconciles the
//    persisted turn table over the streamed one, so a reload renders the
//    rows it rendered live — identical markup, not a second shape.

import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

import VisualClaims from "./capabilities/evidence/components/VisualClaims";
import { useWorkspaceStore } from "./store";
import { turnVisualClaims } from "./capabilities/evidence/visualClaims";
import { mergeReconciledTurn } from "./turnStream";
import type { Citation, ThreadDetail, Turn, VisualClaim } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_PAGE_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  "pages",
  "ThreadPage.tsx",
);

const CLAIM: VisualClaim = {
  kind: "visual_observation",
  attachment_id: "att-1",
  version: "3",
  unit: 4,
  page: 4,
  observation_version: 1,
  scope: [0.12, 0.05, 0.41, 0.62],
  provider: "configured-vision",
  model_version: "vision-model-1",
  page_image_sha256: "a".repeat(64),
  detail: "Revenue by segment chart / Services series / FY2024",
  filename: "quarterly-notes.pdf",
  reused: true,
  outbound_calls: 0,
};

const CITATION: Citation = {
  kind: "attachment",
  document_id: "",
  attachment_id: "att-1",
  version: "3",
  unit: 4,
  display: "page",
  filename: "quarterly-notes.pdf",
  quote: "渠道库存较上季度下降。",
};

type Frame = [string, unknown];

function sseFrame([event, data]: Frame): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function stubSseRun(frames: Frame[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : String(input);
      if (!url.endsWith("/messages") && !url.endsWith("/resume")) {
        return new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
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
}

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    run_id: RUN_ID,
    question: "图表里服务业务收入是多少？",
    status: "done",
    answer: "服务业务收入见图表。",
    citations: [CITATION],
    thinking_text: "",
    tool_summaries: [],
    assistant_message: "",
    in_flight: false,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function seedThread(turns: Turn[] = []): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns,
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    threads: [],
    sendingQuestion: false,
    resumingThreadId: null,
  });
}

function latestTurn(): Turn {
  const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
  return turns[turns.length - 1];
}

/** The wire order the host fixes: answer frames, then visual_claims, then
 * the terminal citations event. */
const ANSWER_FRAMES: Frame[] = [
  ["status", { run_id: RUN_ID, status: "done", assistant_message: "" }],
  ["answer", { text: "服务业务收入见图表。" }],
];

function rendered(turn: Turn): string {
  return renderToString(
    createElement(VisualClaims, {
      threadId: THREAD_ID,
      claims: turnVisualClaims(turn),
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sendQuestion visual_claims fold", () => {
  it("puts the claim on the streamed turn", async () => {
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [CLAIM] }],
      ["citations", [CITATION]],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "服务业务收入？");

    expect(turnVisualClaims(latestTurn())).toEqual([CLAIM]);
  });

  it("renders one visual observation naming the attachment, page and target", async () => {
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [CLAIM] }],
      ["citations", [CITATION]],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "服务业务收入？");

    const html = rendered(latestTurn());
    expect(html).toContain("quarterly-notes.pdf");
    expect(html).toContain("p.4");
    expect(html).toContain("Revenue by segment chart / Services series / FY2024");
    expect(html).toContain(
      `/api/threads/${THREAD_ID}/attachments/att-1`,
    );
    expect(html).toContain(
      '{&quot;kind&quot;:&quot;attachment&quot;,&quot;attachment_id&quot;:&quot;att-1&quot;,&quot;version&quot;:&quot;3&quot;,&quot;unit&quot;:4}',
    );
  });

  it("never folds a claim into the citations", async () => {
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [CLAIM] }],
      ["citations", [CITATION]],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "服务业务收入？");

    const turn = latestTurn();
    expect(turn.citations).toEqual([CITATION]);
    // A claim is not a quote: nothing on the quote channel may carry one.
    expect(JSON.stringify(turn.citations)).not.toContain(CLAIM.detail);
    expect(turn.citations.some((c) => (c as { quote?: string }).quote === undefined)).toBe(
      false,
    );
  });

  it("keeps the terminal citations frame's close behaviour", async () => {
    // The order the host promises: visual_claims BEFORE citations. A
    // stream that carries claims must still land the terminal frame's
    // citation array verbatim — the lane may not swallow the close.
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [CLAIM] }],
      ["citations", [CITATION, { ...CITATION, unit: 9, quote: "存货周转天数 41 天。" }]],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "服务业务收入？");

    const turn = latestTurn();
    expect(turn.citations.length).toBe(2);
    expect(turn.citations[1].unit).toBe(9);
    expect(turnVisualClaims(turn)).toEqual([CLAIM]);
  });

  it("renders no visual surface for a run with no claims", async () => {
    seedThread();
    stubSseRun([...ANSWER_FRAMES, ["citations", [CITATION]]]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    const turn = latestTurn();
    expect(turnVisualClaims(turn)).toEqual([]);
    expect(rendered(turn)).toBe("");
  });

  it("renders nothing extra when the frame arrives empty", async () => {
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [] }],
      ["citations", [CITATION]],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    expect(turnVisualClaims(latestTurn())).toEqual([]);
    expect(rendered(latestTurn())).toBe("");
  });

  it("ignores a malformed visual_claims frame", async () => {
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: "not-a-list" }],
      ["citations", [CITATION]],
    ]);

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    expect(turnVisualClaims(latestTurn())).toEqual([]);
    expect(latestTurn().citations).toEqual([CITATION]);
  });
});

describe("resumeThread visual_claims fold", () => {
  it("folds the frame into the row the resume is continuing", async () => {
    seedThread([makeTurn({ in_flight: true, visual_claims: [] })]);
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [CLAIM] }],
      ["citations", [CITATION]],
    ]);

    await useWorkspaceStore.getState().resumeThread(THREAD_ID);

    expect(turnVisualClaims(latestTurn())).toEqual([CLAIM]);
    expect(latestTurn().citations).toEqual([CITATION]);
  });
});

describe("history-loaded turn", () => {
  function stubThreadDetail(turn: Turn): void {
    const detail: ThreadDetail = {
      id: THREAD_ID,
      title: "AAPL 10-Q",
      created_at: "2026-01-01T00:00:00.000Z",
      turns: [turn],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(detail), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
  }

  it("populates the same field from the GET row", async () => {
    seedThread();
    stubThreadDetail(makeTurn({ visual_claims: [CLAIM] }));

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    expect(turnVisualClaims(latestTurn())).toEqual([CLAIM]);
  });

  it("renders identically to the streamed turn", async () => {
    // The streamed row and the reloaded row are the same surface: a
    // reload must not reword, relink or reorder anything.
    seedThread();
    stubSseRun([
      ...ANSWER_FRAMES,
      ["visual_claims", { visual_claims: [CLAIM] }],
      ["citations", [CITATION]],
    ]);
    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "服务业务收入？");
    const streamedHtml = rendered(latestTurn());

    seedThread();
    stubThreadDetail(makeTurn({ visual_claims: [CLAIM] }));
    await useWorkspaceStore.getState().openThread(THREAD_ID);

    expect(rendered(latestTurn())).toBe(streamedHtml);
  });

  it("renders nothing for a history row whose claim array is empty", async () => {
    seedThread();
    stubThreadDetail(makeTurn({ visual_claims: [] }));

    await useWorkspaceStore.getState().openThread(THREAD_ID);

    expect(rendered(latestTurn())).toBe("");
  });

  it("takes the persisted rows on a reconcile", () => {
    const merged = mergeReconciledTurn(
      makeTurn({ visual_claims: [] }),
      makeTurn({ visual_claims: [CLAIM] }),
    );
    expect(merged.visual_claims).toEqual([CLAIM]);
  });

  it("keeps the streamed rows when the server row carries none", () => {
    // A mid-run reopen: the RunRecord is appended only once the stream
    // completes, so the persisted row can legitimately lag — dropping the
    // claims here would blank a lane the operator was already reading.
    const merged = mergeReconciledTurn(
      makeTurn({ visual_claims: [CLAIM] }),
      makeTurn({ visual_claims: [] }),
    );
    expect(merged.visual_claims).toEqual([CLAIM]);
  });
});

describe("the frame is not a citation at the wire boundary", () => {
  function framesFor(payload: unknown): Frame[] {
    return [
      ...ANSWER_FRAMES,
      ["visual_claims", payload],
      ["citations", [CITATION]],
    ];
  }

  it("keeps a malformed payload off the turn instead of half-rendering it", async () => {
    // Rows missing the identity / page / target cannot carry a
    // provenance-backed observation, and a whole-page row whose scope is
    // a bogus list must not print a region it never read.
    seedThread();
    stubSseRun(
      framesFor({
        visual_claims: [
          { ...CLAIM, detail: "" },
          { ...CLAIM, attachment_id: "att-9", scope: [0.1, 0.2] },
          CLAIM,
        ],
      }),
    );

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "服务业务收入？");

    expect(turnVisualClaims(latestTurn())).toEqual([CLAIM]);
  });

  it("never lets a claim reach the terminal citations record", async () => {
    seedThread();
    stubSseRun(framesFor({ visual_claims: [CLAIM] }));

    const result = await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "服务业务收入？");

    expect(result?.citations).toEqual([CITATION]);
    expect(JSON.stringify(result?.citations)).not.toContain(CLAIM.detail);
  });
});

describe("the turn's page wiring", () => {
  // A source-level guard, the pattern ``productChrome.test.ts`` uses: this
  // project's vitest setup has no DOM testing library for a routed page, so
  // the alternative to pinning the lane is leaving it fold-tested but never
  // actually shown. Two promises: the operator sees the rows, and the quote
  // channel cannot receive them.
  const source = readFileSync(THREAD_PAGE_PATH, "utf8");

  it("renders the turn's claims through the one projector", () => {
    // ``turnVisualClaims`` is the same function the history load reads, so
    // a live frame and a reload cannot split into two shapes.
    expect(source).toContain("turnVisualClaims(turn)");
    expect(source).toContain(
      "<VisualClaims threadId={threadId} claims={visualClaims} />",
    );
  });

  it("feeds the chip layer from citations only", () => {
    const chipCalls = source.match(/<AnswerWithCitations[\s\S]*?\/>/g) ?? [];
    expect(chipCalls.length).toBeGreaterThan(0);
    for (const call of chipCalls) {
      expect(call.toLowerCase()).not.toContain("visual");
    }
  });
});
