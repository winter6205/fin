// SC17: a send that fails BEFORE any SSE frame must reach the
// operator. The store used to append an optimistic turn, let the
// rejection escape, and leave that row ``in_flight`` forever while
// the composer had already cleared the draft.

import { afterEach, describe, expect, it, vi } from "vitest";
import { ConcurrentRunError, IngestMissingError } from "./api";
import { describeSendFailure } from "./sendFailure";
import { useWorkspaceStore } from "./store";
import type { ThreadDetail } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";

function seedThread(): void {
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
  });
}

function stubSendStatus(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

/** SSE body that emits a terminal ``status`` frame, then breaks. */
function stubSendStreamThatBreaksAfterStatus(): void {
  const frame =
    `event: status\ndata: ${JSON.stringify({
      run_id: "22222222-2222-4222-8222-222222222222",
      status: "done",
      assistant_message: "",
    })}\n\n`;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      let framesSent = false;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (!framesSent) {
            framesSent = true;
            controller.enqueue(new TextEncoder().encode(frame));
            return;
          }
          controller.error(new Error("stream broke"));
        },
      });
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sendQuestion failure surfacing", () => {
  it("rejects with the typed 409 error instead of swallowing it", async () => {
    seedThread();
    stubSendStatus(409, { error: "concurrent_run" });

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "现金流怎么样？"),
    ).rejects.toBeInstanceOf(ConcurrentRunError);
  });

  it("rejects with the typed 503 error instead of swallowing it", async () => {
    seedThread();
    stubSendStatus(503, { error: "ingest_missing" });

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "现金流怎么样？"),
    ).rejects.toBeInstanceOf(IngestMissingError);
  });

  it("leaves no ghost in_flight turn behind on 409", async () => {
    seedThread();
    stubSendStatus(409, { error: "concurrent_run" });

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .catch(() => undefined);

    expect(useWorkspaceStore.getState().activeThread?.turns).toEqual([]);
    expect(useWorkspaceStore.getState().sendingQuestion).toBe(false);
  });

  it("leaves no ghost in_flight turn behind on 503", async () => {
    seedThread();
    stubSendStatus(503, { error: "ingest_missing" });

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .catch(() => undefined);

    expect(useWorkspaceStore.getState().activeThread?.turns).toEqual([]);
  });

  it("hands the composer visible copy for the rejection", async () => {
    seedThread();
    stubSendStatus(409, { error: "concurrent_run" });

    const rejection = await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .then(() => null)
      .catch((error: unknown) => error);

    const notice = describeSendFailure(rejection);
    expect(notice.kind).toBe("concurrent_run");
    expect(notice.message.length).toBeGreaterThan(0);
  });

  it("keeps a partially streamed turn but clears its live flag", async () => {
    seedThread();
    stubSendStreamThatBreaksAfterStatus();

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "现金流怎么样？")
      .catch(() => undefined);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].in_flight).toBe(false);
  });
});
