// corpus-serve-ingest T5: the two upload doors on the workspace.
//
// Acceptance core: the ingest door (store.ingestFile) is the ONLY
// path that calls POST /api/corpus/ingest; the attach door
// (store.attachFile -> sendQuestion with attachment_ids) calls
// POST /api/threads/{id}/attachments + /messages and NEVER the
// ingest endpoint (ADR 0047: the doors must not cross).
//
// These tests live at the store/api seam because the web suite runs
// in a node environment (no DOM) — the components are thin wrappers
// over exactly these actions.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AttachFailedError,
  IngestFailedError,
  type SendRequest,
} from "./api";
import { useWorkspaceStore } from "./store";
import type { ThreadDetail } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";

const INGEST_OK = {
  status: "ingested",
  document_id: "AAPL-2024-Q3",
  version: "abc123",
  fmt: "md",
  doc_type: "financial_statement",
  unit_count: 3,
  block_count: 7,
  written: 7,
  duplicates: 0,
  block_ids: ["b1", "b2"],
  invalidation: null,
};

const ATTACH_OK = {
  attachment_id: "att-1",
  filename: "notes.md",
  size_bytes: 6,
  scope: "thread_attach",
};

interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
}

/** Record every fetch and answer from ``handler``. */
function stubFetch(
  handler: (url: string, init: RequestInit | undefined) => Response,
): { calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      calls.push({ url, method: init?.method ?? "GET", body: init?.body });
      return handler(url, init);
    }),
  );
  return { calls };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** SSE run with a terminal done status — the store's send tail. */
function sseRunResponse(): Response {
  const frames = [
    `event: status\ndata: ${JSON.stringify({
      run_id: "22222222-2222-4222-8222-222222222222",
      status: "done",
      assistant_message: "",
    })}\n\n`,
    `event: answer\ndata: ${JSON.stringify({ text: "已读完附件。" })}\n\n`,
    `event: citations\ndata: []\n\n`,
  ];
  let next = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (next >= frames.length) {
        controller.close();
        return;
      }
      controller.enqueue(new TextEncoder().encode(frames[next]));
      next += 1;
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

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
    corpus: [],
    corpusLoaded: false,
    sendingQuestion: false,
    resumingThreadId: null,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ingest door (store.ingestFile)", () => {
  it("POSTs the file as multipart to /api/corpus/ingest and reloads the corpus", async () => {
    seedThread();
    const filing = {
      document_id: "AAPL-2024-Q3",
      company: "Apple",
      period: "2024-Q3",
      version: "abc123",
      page_count: 3,
    };
    const { calls } = stubFetch((url) => {
      if (url === "/api/corpus/ingest") return jsonResponse(INGEST_OK);
      if (url === "/api/corpus") return jsonResponse({ filings: [filing] });
      return jsonResponse({}, 404);
    });

    const file = new File(["# hello"], "aapl-10q.md", {
      type: "text/markdown",
    });
    const result = await useWorkspaceStore.getState().ingestFile(file);

    expect(result.document_id).toBe("AAPL-2024-Q3");
    const ingestCall = calls.find((c) => c.url === "/api/corpus/ingest");
    expect(ingestCall).toBeDefined();
    expect(ingestCall?.method).toBe("POST");
    expect(ingestCall?.body).toBeInstanceOf(FormData);
    const form = ingestCall?.body as FormData;
    const sent = form.get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("aapl-10q.md");
    // Success refreshes the list the operator is looking at.
    expect(calls.some((c) => c.url === "/api/corpus")).toBe(true);
    expect(useWorkspaceStore.getState().corpus).toEqual([filing]);
    expect(useWorkspaceStore.getState().corpusLoaded).toBe(true);
  });

  it("throws the typed error carrying the named failure code", async () => {
    seedThread();
    stubFetch((url) =>
      url === "/api/corpus/ingest"
        ? jsonResponse({ error: "unsupported_format", detail: "nope" }, 415)
        : jsonResponse({}, 404),
    );

    const rejection = await useWorkspaceStore
      .getState()
      .ingestFile(new File(["x"], "report.weird"))
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(IngestFailedError);
    expect((rejection as IngestFailedError).code).toBe("unsupported_format");
    // A named failure must not half-refresh the corpus list either.
    expect(useWorkspaceStore.getState().corpusLoaded).toBe(false);
  });
});

describe("attach door (store.attachFile)", () => {
  it("POSTs raw bytes with the filename query param (never multipart)", async () => {
    seedThread();
    const { calls } = stubFetch((url) =>
      url.includes("/attachments") ? jsonResponse(ATTACH_OK) : jsonResponse({}, 404),
    );

    const file = new File(["hello!"], "notes.md", { type: "text/markdown" });
    const attachment = await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, file);

    expect(attachment.attachment_id).toBe("att-1");
    expect(attachment.scope).toBe("thread_attach");
    const call = calls[0];
    expect(call.url).toBe(
      `/api/threads/${encodeURIComponent(THREAD_ID)}/attachments?filename=notes.md`,
    );
    expect(call.method).toBe("POST");
    expect(call.body).not.toBeInstanceOf(FormData);
    expect(new TextDecoder().decode(call.body as Uint8Array)).toBe("hello!");
  });

  it("throws the typed error carrying the named failure code", async () => {
    seedThread();
    stubFetch((url) =>
      url.includes("/attachments")
        ? jsonResponse({ error: "attachment_not_text" }, 400)
        : jsonResponse({}, 404),
    );

    const rejection = await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["\xff"], "image.bin"))
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(AttachFailedError);
    expect((rejection as AttachFailedError).code).toBe("attachment_not_text");
  });
});

describe("two doors never cross (acceptance)", () => {
  it("attach-then-send calls the attach endpoint and NEVER the ingest endpoint", async () => {
    seedThread();
    const { calls } = stubFetch((url) => {
      if (url.includes("/attachments")) return jsonResponse(ATTACH_OK);
      if (url.endsWith("/messages")) return sseRunResponse();
      return jsonResponse("[]"); // refreshThreads tail
    });

    // Door 2: attach into the thread, then send with the reference.
    const attachment = await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["hello!"], "notes.md"));
    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "总结附件", true, [attachment.attachment_id]);

    const urls = calls.map((c) => c.url);
    expect(urls.some((u) => u.includes("/attachments"))).toBe(true);
    // THE acceptance assertion: the attach path must not ingest.
    expect(urls.some((u) => u.includes("/corpus/ingest"))).toBe(false);

    // The send body references the attachment the attach door minted.
    const sendCall = calls.find((c) => c.url.endsWith("/messages"));
    expect(sendCall).toBeDefined();
    const request = JSON.parse(sendCall?.body as string) as SendRequest;
    expect(request.question).toBe("总结附件");
    expect(request.attachment_ids).toEqual([attachment.attachment_id]);
  });

  it("a plain send without attachments carries no attachment_ids", async () => {
    seedThread();
    const { calls } = stubFetch((url) => {
      if (url.endsWith("/messages")) return sseRunResponse();
      return jsonResponse("[]");
    });

    await useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？");

    const sendCall = calls.find((c) => c.url.endsWith("/messages"));
    const request = JSON.parse(sendCall?.body as string) as SendRequest;
    expect(request.attachment_ids).toBeUndefined();
  });
});
