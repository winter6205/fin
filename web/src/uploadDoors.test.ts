// The two upload doors on the workspace: the corpus-ingest door and
// the thread-attach door (ADR 0047), plus the thread-attachment
// lifecycle (ADR 0086: parsing / ready / failed, retry, persistence
// across sends).
//
// Acceptance core: the ingest door (store.ingestFile) is the ONLY
// path that calls POST /api/corpus/ingest; the attach door
// (store.attachFile -> sendQuestion with attachment_ids) calls
// POST /api/threads/{id}/attachments + /messages and NEVER the
// ingest endpoint (ADR 0047: the doors must not cross).
//
// The attach door's promise changed's promise: the POST answers while the
// host still parses (status "parsing"), the id stays referenceable
// for the WHOLE thread (no single-use consumption), and a failed
// preparation is retryable. These tests live at the store/api seam
// because the web suite runs in a node environment (no DOM) — the
// components are thin wrappers over exactly these actions. Timers
// are faked for the whole file: the readiness poll is driven by
// advancing a schedule, never by really waiting.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AttachFailedError,
  AttachmentNotReadyError,
  IngestFailedError,
  type SendRequest,
} from "./api";
import {
  attachmentsForSend,
  LAST_INGEST_JOB_KEY,
  useWorkspaceStore,
} from "./store";
import type { JobStatus, ThreadAttachment, ThreadDetail } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";

// The POST 200 of the corpus-ingest door: a DURABLE ACCEPT only. Parse,
// 切分 and push happen off the request, so nothing about the outcome is
// in this body — the state is followed through GET /api/corpus/ingest/{job_id}.
const INGEST_OK = {
  status: "accepted",
  job_id: "AAPL-2022Q3",
  document_id: "AAPL-2022Q3",
  version: "a".repeat(64),
  state: "pending",
} as const;

const STATUS_URL = "/api/corpus/ingest/AAPL-2022Q3";

/** One polled job status; ``overrides`` selects the state under test. */
function jobStatus(overrides: Partial<JobStatus> = {}): JobStatus {
  return {
    job_id: "AAPL-2022Q3",
    document_id: "AAPL-2022Q3",
    version: "a".repeat(64),
    state: "extracting",
    error_type: null,
    error_message: null,
    text_ready: false,
    graph: null,
    ...overrides,
  };
}

/** Minimal Storage stand-in: the node env has no window.localStorage. */
function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => map.get(key) ?? null,
    key: (index: number) => [...map.keys()][index] ?? null,
    removeItem: (key: string) => void map.delete(key),
    setItem: (key: string, value: string) => void map.set(key, value),
  };
}

// The POST 200 of the attach door: stored, not yet parsed. Every
// attachment door answers with this same shape.
const ATTACH_PARSED_LATER: ThreadAttachment = {
  attachment_id: "att-1",
  filename: "notes.md",
  size_bytes: 6,
  scope: "thread_attach",
  status: "parsing",
  version: 0,
  unit_count: 0,
  searchable: false,
  failure_reason: null,
};

const ATTACH_READY: ThreadAttachment = {
  ...ATTACH_PARSED_LATER,
  status: "ready",
  version: 1,
  unit_count: 2,
  searchable: true,
};

const ATTACH_FAILED: ThreadAttachment = {
  ...ATTACH_PARSED_LATER,
  status: "failed",
  unit_count: 0,
  searchable: false,
  failure_reason: "attachment_empty_text",
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
    attachments: [],
    ingestJobId: null,
    ingestAccepted: null,
    ingestJob: null,
    ingestJobFailure: null,
  });
}

const LIST_URL = `/api/threads/${encodeURIComponent(THREAD_ID)}/attachments`;

let storage: Storage;
const originalWindow = globalThis.window;

beforeEach(() => {
  vi.useFakeTimers();
  // The submitted job id is read back through window.localStorage and
  // the node env has no window: a stand-in pins the storage path.
  storage = fakeStorage();
  (globalThis as { window?: unknown }).window = { localStorage: storage };
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  (globalThis as { window?: unknown }).window = originalWindow;
});

describe("ingest door (store.ingestFile)", () => {
  it("POSTs the file plus company and period as multipart and keeps the accepted job", async () => {
    seedThread();
    const { calls } = stubFetch((url) =>
      url === "/api/corpus/ingest" ? jsonResponse(INGEST_OK) : jsonResponse({}, 404),
    );

    const file = new File(["# hello"], "aapl-10q.md", {
      type: "text/markdown",
    });
    const result = await useWorkspaceStore
      .getState()
      .ingestFile(file, { company: "aapl", period: "2022q3" });

    expect(result.status).toBe("accepted");
    expect(result.job_id).toBe("AAPL-2022Q3");
    const ingestCall = calls.find((c) => c.url === "/api/corpus/ingest");
    expect(ingestCall).toBeDefined();
    expect(ingestCall?.method).toBe("POST");
    expect(ingestCall?.body).toBeInstanceOf(FormData);
    const form = ingestCall?.body as FormData;
    const sent = form.get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("aapl-10q.md");
    expect(form.get("company")).toBe("aapl");
    expect(form.get("period")).toBe("2022q3");
    expect(useWorkspaceStore.getState().ingestJobId).toBe("AAPL-2022Q3");
    expect(useWorkspaceStore.getState().ingestAccepted).toEqual(INGEST_OK);
    // An accept is not a completion: the catalog row does not exist yet,
    // so nothing is refreshed on this response.
    expect(calls.some((c) => c.url === "/api/corpus")).toBe(false);
  });

  it("sends the optional format / document_id / doc_type only when supplied", async () => {
    seedThread();
    const { calls } = stubFetch((url) =>
      url === "/api/corpus/ingest" ? jsonResponse(INGEST_OK) : jsonResponse({}, 404),
    );

    await useWorkspaceStore.getState().ingestFile(
      new File(["x"], "aapl-10q.md"),
      {
        company: "aapl",
        period: "2022q3",
        format: "md",
        document_id: "0000320193-23-000123",
        doc_type: "financial_statement",
      },
    );
    const form = calls[0].body as FormData;
    expect(form.get("format")).toBe("md");
    expect(form.get("document_id")).toBe("0000320193-23-000123");
    expect(form.get("doc_type")).toBe("financial_statement");

    await useWorkspaceStore
      .getState()
      .ingestFile(new File(["y"], "aapl-10q.md"), {
        company: "aapl",
        period: "2022q4",
      });
    const bare = calls[1].body as FormData;
    expect(bare.has("format")).toBe(false);
    expect(bare.has("document_id")).toBe(false);
    expect(bare.has("doc_type")).toBe(false);
  });

  it("persists the submitted job id so a reload resumes the same job", async () => {
    seedThread();
    const { calls } = stubFetch((url) => {
      if (url === "/api/corpus/ingest") return jsonResponse(INGEST_OK);
      if (url === STATUS_URL) return jsonResponse(jobStatus());
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore
      .getState()
      .ingestFile(new File(["# hello"], "aapl-10q.md"), {
        company: "aapl",
        period: "2022q3",
      });
    expect(storage.getItem(LAST_INGEST_JOB_KEY)).toBe("AAPL-2022Q3");

    // The reload itself: a fresh page whose only trace of the job is the
    // persisted id — no file, no in-memory submission.
    useWorkspaceStore.setState({
      ingestJobId: null,
      ingestAccepted: null,
      ingestJob: null,
    });
    await useWorkspaceStore.getState().loadIngestJob();
    expect(calls.some((c) => c.url === STATUS_URL)).toBe(true);
    expect(useWorkspaceStore.getState().ingestJobId).toBe("AAPL-2022Q3");
    expect(useWorkspaceStore.getState().ingestJob?.state).toBe("extracting");
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
      .ingestFile(new File(["x"], "report.weird"), {
        company: "aapl",
        period: "2022q3",
      })
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(IngestFailedError);
    expect((rejection as IngestFailedError).code).toBe("unsupported_format");
    // A named failure must not half-refresh the corpus list either.
    expect(useWorkspaceStore.getState().corpusLoaded).toBe(false);
  });

  it("starts no job on the 409 concurrent-run conflict", async () => {
    seedThread();
    const { calls } = stubFetch((url) =>
      url === "/api/corpus/ingest"
        ? jsonResponse({ error: "concurrent_run" }, 409)
        : jsonResponse({}, 404),
    );

    const rejection = await useWorkspaceStore
      .getState()
      .ingestFile(new File(["x"], "aapl-10q.md"), {
        company: "aapl",
        period: "2022q3",
      })
      .then(() => null)
      .catch((error: unknown) => error);

    expect((rejection as IngestFailedError).code).toBe("concurrent_run");
    // Nothing was accepted, so nothing is remembered to poll.
    expect(useWorkspaceStore.getState().ingestJobId).toBeNull();
    expect(storage.getItem(LAST_INGEST_JOB_KEY)).toBeNull();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(calls.filter((c) => c.url === STATUS_URL)).toEqual([]);
  });
});

describe("ingest job status (store.loadIngestJob)", () => {
  it("reads the accepted job's state and keeps polling until it is ready", async () => {
    seedThread();
    const filing = {
      document_id: "AAPL-2022Q3",
      company: "Apple",
      period: "2022Q3",
      version: "a".repeat(64),
      page_count: 3,
    };
    let reads = 0;
    const { calls } = stubFetch((url) => {
      if (url === STATUS_URL) {
        reads += 1;
        return jsonResponse(
          reads >= 2
            ? jobStatus({ state: "ready", text_ready: true })
            : jobStatus({ state: "cataloging" }),
        );
      }
      if (url === "/api/corpus") return jsonResponse({ filings: [filing] });
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);
    expect(reads).toBe(1);
    expect(useWorkspaceStore.getState().ingestJob?.state).toBe("cataloging");
    // The list the operator is looking at refreshes when the source is
    // actually catalogued, not on accept.
    expect(calls.some((c) => c.url === "/api/corpus")).toBe(false);

    await vi.advanceTimersByTimeAsync(1000);
    expect(reads).toBe(2);
    expect(useWorkspaceStore.getState().ingestJob?.state).toBe("ready");
    expect(useWorkspaceStore.getState().ingestJob?.text_ready).toBe(true);
    expect(useWorkspaceStore.getState().corpus).toEqual([filing]);

    // Terminal: the chain stopped on its own.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(2);
  });

  it("stops at a failed job and shows its typed reason without touching the corpus", async () => {
    seedThread();
    let reads = 0;
    stubFetch((url) => {
      if (url === STATUS_URL) {
        reads += 1;
        return jsonResponse(
          jobStatus({
            state: "failed",
            error_type: "malformed_source",
            error_message: "source bytes unreadable",
          }),
        );
      }
      if (url === "/api/corpus") return jsonResponse({ filings: [] });
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);
    const job = useWorkspaceStore.getState().ingestJob;
    expect(job?.state).toBe("failed");
    expect(job?.error_type).toBe("malformed_source");
    expect(useWorkspaceStore.getState().corpusLoaded).toBe(false);

    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(1);
  });

  it("a status read that refuses is a named miss and drops the dead marker", async () => {
    seedThread();
    storage.setItem(LAST_INGEST_JOB_KEY, "AAPL-2022Q3");
    let reads = 0;
    stubFetch(() => {
      reads += 1;
      return jsonResponse({ error: "unknown_job" }, 404);
    });

    await useWorkspaceStore.getState().loadIngestJob(undefined, 1000);

    const failure = useWorkspaceStore.getState().ingestJobFailure;
    expect(failure).toBeInstanceOf(IngestFailedError);
    expect(failure?.code).toBe("unknown_job");
    expect(useWorkspaceStore.getState().ingestJob).toBeNull();
    // A persisted id that outlived its job must not be re-read forever.
    expect(storage.getItem(LAST_INGEST_JOB_KEY)).toBeNull();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(1);
  });

  it("reads nothing when this browser never submitted a job", async () => {
    seedThread();
    const { calls } = stubFetch(() => jsonResponse({}, 404));

    await useWorkspaceStore.getState().loadIngestJob();

    expect(calls).toEqual([]);
    expect(useWorkspaceStore.getState().ingestJob).toBeNull();
    expect(useWorkspaceStore.getState().ingestJobFailure).toBeNull();
  });

  it("a newer submission owns the chain, so a stale read cannot paint the old job", async () => {
    seedThread();
    const otherUrl = "/api/corpus/ingest/MSFT-2022Q1";
    stubFetch((url) => {
      if (url === "/api/corpus/ingest") {
        return jsonResponse({ ...INGEST_OK, job_id: "MSFT-2022Q1" });
      }
      if (url === otherUrl) {
        return jsonResponse(
          jobStatus({ job_id: "MSFT-2022Q1", document_id: "MSFT-2022Q1" }),
        );
      }
      return jsonResponse(jobStatus({ state: "cataloging" }));
    });

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);
    expect(useWorkspaceStore.getState().ingestJob?.state).toBe("cataloging");

    // Second upload while the first chain is still ticking.
    await useWorkspaceStore
      .getState()
      .ingestFile(new File(["# ms"], "msft-10q.md"), {
        company: "msft",
        period: "2022q1",
      });
    await vi.advanceTimersByTimeAsync(10_000);

    const job = useWorkspaceStore.getState().ingestJob;
    expect(job?.job_id).toBe("MSFT-2022Q1");
    expect(job?.state).toBe("extracting");
  });
});

describe("attach door (store.attachFile)", () => {
  it("POSTs raw bytes with the filename query param (never multipart)", async () => {
    seedThread();
    const { calls } = stubFetch((url) =>
      url === LIST_URL
        ? jsonResponse({ attachments: [ATTACH_READY] })
        : jsonResponse(ATTACH_PARSED_LATER),
    );

    const file = new File(["hello!"], "notes.md", { type: "text/markdown" });
    const attachment = await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, file);

    expect(attachment.attachment_id).toBe("att-1");
    expect(attachment.scope).toBe("thread_attach");
    // The door answers BEFORE preparation finished, and the
    // record lands in the thread slice as ``parsing``.
    expect(attachment.status).toBe("parsing");
    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_PARSED_LATER,
    ]);
    const call = calls[0];
    expect(call.url).toBe(`${LIST_URL}?filename=notes.md`);
    expect(call.method).toBe("POST");
    expect(call.body).not.toBeInstanceOf(FormData);
    expect(new TextDecoder().decode(call.body as Uint8Array)).toBe("hello!");
  });

  it("throws the typed error carrying the named failure code", async () => {
    seedThread();
    stubFetch((url) =>
      url.startsWith(LIST_URL) && url !== LIST_URL
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
      if (url === LIST_URL) return jsonResponse({ attachments: [] });
      if (url.includes("/attachments")) {
        return jsonResponse(ATTACH_PARSED_LATER);
      }
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

describe("send gate: what a send may reference", () => {
  it("references only READY records, so an unrelated question is never refused", () => {
    const selection = attachmentsForSend([
      { ...ATTACH_READY, attachment_id: "ready-1" },
      { ...ATTACH_PARSED_LATER, attachment_id: "parsing-1" },
      { ...ATTACH_FAILED, attachment_id: "failed-1" },
    ]);
    expect(selection.ids).toEqual(["ready-1"]);
    // What was left out stays visible with its reason — the rule may never
    // drop a record silently.
    expect(selection.withheld.map((a) => a.attachment_id)).toEqual([
      "parsing-1",
      "failed-1",
    ]);
  });

  it("names nothing for a thread that holds no attachment", () => {
    expect(attachmentsForSend([])).toEqual({ ids: [], withheld: [] });
  });

  it("the ids the gate selects are the ids that reach the wire", async () => {
    seedThread();
    useWorkspaceStore.setState({
      attachments: [
        ATTACH_READY,
        { ...ATTACH_PARSED_LATER, attachment_id: "att-2" },
        ATTACH_FAILED,
      ],
    });
    const { calls } = stubFetch((url) =>
      url.endsWith("/messages") ? sseRunResponse() : jsonResponse("[]"),
    );

    await useWorkspaceStore
      .getState()
      .sendQuestion(
        THREAD_ID,
        "读这份",
        true,
        attachmentsForSend(useWorkspaceStore.getState().attachments).ids,
      );

    const request = JSON.parse(
      calls.find((c) => c.url.endsWith("/messages"))?.body as string,
    ) as SendRequest;
    expect(request.attachment_ids).toEqual(["att-1"]);
  });
});

describe("attachment lifecycle (ADR 0086)", () => {
  it("loadAttachments lists the thread's records", async () => {
    seedThread();
    stubFetch((url) =>
      url === LIST_URL
        ? jsonResponse({ attachments: [ATTACH_READY, ATTACH_FAILED] })
        : jsonResponse({}, 404),
    );

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID);

    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_READY,
      ATTACH_FAILED,
    ]);
  });

  it("polls the list while a record is parsing and stops once ready", async () => {
    seedThread();
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        // First read still parsing, second read ready.
        return jsonResponse({
          attachments: [reads >= 2 ? ATTACH_READY : ATTACH_PARSED_LATER],
        });
      }
      return jsonResponse(ATTACH_PARSED_LATER);
    });

    await useWorkspaceStore
      .getState()
      .loadAttachments(THREAD_ID, 1000);
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("parsing");
    expect(reads).toBe(1);

    await vi.advanceTimersByTimeAsync(1000);
    expect(reads).toBe(2);
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("ready");

    // Nothing is parsing anymore: the chain stopped on its own.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(2);
  });

  it("a stale list read cannot erase the just-attached parsing chip", async () => {
    seedThread();
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        // Read 1 answers WITHOUT the fresh record (issued before the
        // POST committed); read 2 catches up.
        return jsonResponse({
          attachments: reads >= 2 ? [ATTACH_READY] : [],
        });
      }
      return jsonResponse(ATTACH_PARSED_LATER);
    });

    await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["hello!"], "notes.md"));
    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_PARSED_LATER,
    ]);

    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments).toEqual([ATTACH_READY]);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(2);
  });

  it("retry re-runs preparation, follows the record to ready, and names an unknown id", async () => {
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_FAILED] });
    const retryUrl = `${LIST_URL}/att-1/retry`;
    const { calls } = stubFetch((url) => {
      if (url === retryUrl) {
        return jsonResponse({ ...ATTACH_PARSED_LATER, version: 2 });
      }
      if (url === LIST_URL) {
        return jsonResponse({
          attachments: [{ ...ATTACH_READY, version: 2 }],
        });
      }
      return jsonResponse({ error: "attachment_not_found" }, 400);
    });

    await useWorkspaceStore.getState().retryAttachment(THREAD_ID, "att-1");
    const retried = calls.find((c) => c.url === retryUrl);
    expect(retried?.method).toBe("POST");
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("parsing");

    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments[0]).toEqual({
      ...ATTACH_READY,
      version: 2,
    });

    // A deleted / foreign attachment id answers the named 400.
    const rejection = await useWorkspaceStore
      .getState()
      .retryAttachment(THREAD_ID, "gone")
      .then(() => null)
      .catch((error: unknown) => error);
    expect(rejection).toBeInstanceOf(AttachFailedError);
    expect((rejection as AttachFailedError).code).toBe("attachment_not_found");
  });

  it("a send that requires a still-parsing attachment surfaces the named refusal", async () => {
    seedThread();
    stubFetch((url) =>
      url.endsWith("/messages")
        ? jsonResponse({ error: "attachment_not_ready" }, 400)
        : jsonResponse("[]"),
    );

    const rejection = await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "读附件", true, ["att-1"])
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(AttachmentNotReadyError);
    expect((rejection as AttachFailedError).code).toBe("attachment_not_ready");
  });

  it("attachments stay referenceable on every later send (no consumption)", async () => {
    seedThread();
    const { calls } = stubFetch((url) => {
      if (url === LIST_URL) return jsonResponse({ attachments: [ATTACH_READY] });
      if (url.endsWith("/messages")) return sseRunResponse();
      return jsonResponse("[]"); // refreshThreads tail
    });

    await useWorkspaceStore
      .getState()
      .loadAttachments(THREAD_ID, 600_000);
    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "第一轮", true, ["att-1"]);
    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "第二轮", true, ["att-1"]);

    const sends = calls
      .filter((c) => c.url.endsWith("/messages"))
      .map((c) => JSON.parse(c.body as string) as SendRequest);
    expect(sends).toHaveLength(2);
    // The same id rides BOTH turns — the before this contract single-use promise
    // is gone.
    expect(sends[0].attachment_ids).toEqual(["att-1"]);
    expect(sends[1].attachment_ids).toEqual(["att-1"]);
    expect(useWorkspaceStore.getState().attachments).toEqual([ATTACH_READY]);
  });

  it("a failed list read leaves navigation working and the chain stopped", async () => {
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_READY] });
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ error: "thread_not_found" }, 404);
      }
      return jsonResponse({}, 404);
    });

    await expect(
      useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000),
    ).resolves.toBeUndefined();
    await vi.advanceTimersByTimeAsync(10_000);
    // One read only: the failed door ended the chain, it does not
    // spin on a dead endpoint.
    expect(reads).toBe(1);
    expect(useWorkspaceStore.getState().attachments).toEqual([ATTACH_READY]);
  });

  it("opening another thread clears the slice and stops its poll", async () => {
    seedThread();
    const OTHER_ID = "22222222-2222-4222-8222-222222222222";
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [ATTACH_PARSED_LATER] });
      }
      if (url === `/api/threads/${OTHER_ID}`) {
        return jsonResponse({
          id: OTHER_ID,
          title: "other",
          created_at: "2026-01-01T00:00:00.000Z",
          turns: [],
        });
      }
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000);
    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_PARSED_LATER,
    ]);
    await useWorkspaceStore.getState().openThread(OTHER_ID);
    expect(useWorkspaceStore.getState().attachments).toEqual([]);

    await vi.advanceTimersByTimeAsync(10_000);
    // The old thread's chain stopped with the switch: no further
    // list reads for it.
    expect(reads).toBe(1);
  });
});
