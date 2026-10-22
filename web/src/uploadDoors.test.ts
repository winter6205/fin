// The two upload doors on the workspace: the corpus-ingest door and
// the thread-attach door (ADR 0047), plus the thread-attachment
// lifecycle (uploaded / parsing / ready / failed, retry, persistence
// across sends) and the send + new-conversation gate that holds while
// an upload POST is still in flight.
//
// Acceptance core: the ingest door (store.ingestFile) is the ONLY
// path that calls POST /api/corpus/ingest; the attach door
// (store.attachFile -> sendQuestion with attachment_ids) calls
// POST /api/threads/{id}/attachments + /messages and NEVER the
// ingest endpoint (ADR 0047: the doors must not cross).
//
// The attach door's promise: the POST answers once the raw bytes are
// durable (status "uploaded") and starts NO preparation — the graph
// turn does. The id stays referenceable for the WHOLE thread (no
// single-use consumption), and a failed preparation is retryable.
// T2f adds the wait half: a send that names an uploaded/parsing
// record is accepted, the run suspends inside the graph, the chip and
// the readiness poll carry the wait, and the run resumes itself — no
// resend, no operator Resume prompt.
// These tests live at the store/api seam because the web suite runs
// in a node environment (no DOM) — the components are thin wrappers
// over exactly these actions. Timers are faked for the whole file:
// the readiness poll is driven by advancing a schedule, never by
// really waiting.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AttachFailedError,
  AttachmentNotReadyError,
  AttachmentUploadInProgressError,
  IngestFailedError,
  type SendRequest,
} from "./api";
import {
  attachmentPreparationWait,
  attachmentsForSend,
  hasUploadInFlight,
  LAST_INGEST_JOB_KEY,
  useWorkspaceStore,
} from "./store";
import { describeGraphReadiness } from "./pages/HomePage";
import { CATALOG_WARNING_COPY } from "./catalogOverview";
import type {
  GraphIndexReport,
  JobStatus,
  ThreadAttachment,
  ThreadDetail,
} from "./types";

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
/** The GENERIC catalog surface (T6) — separate from the frozen
 * financial ``GET /api/corpus`` projection. */
const OVERVIEW_URL = "/api/corpus/overview";

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

// ----- T4: the pass-through graph/C report shapes -----

// Jobs still running but the community state CONFIRMED ready: the only
// shape whose 图谱索引就绪 line reads 就绪.
const GRAPH_COMMUNITY_READY: GraphIndexReport = {
  jobs: [
    {
      job_id: "ij-1",
      source_ref: "AAPL-2022Q3",
      status: "in_progress",
      failure_code: null,
    },
  ],
  community_index_state: { status: "ready", failure_code: null },
  error: null,
};

// Community build failed with a named code, alongside per-source
// entries in different states.
const GRAPH_COMMUNITY_FAILED: GraphIndexReport = {
  jobs: [
    {
      job_id: "ij-1",
      source_ref: "AAPL-2022Q3",
      status: "ready",
      failure_code: null,
    },
    {
      job_id: "ij-2",
      source_ref: "MSFT-2022Q1",
      status: "failed",
      failure_code: "extract_crash",
    },
  ],
  community_index_state: {
    status: "failed",
    failure_code: "community_build_failed",
  },
  error: null,
};

// The sibling read itself failed: a named error and no community
// state — the code must surface, never be silently hidden.
const GRAPH_ERROR: GraphIndexReport = {
  jobs: [],
  community_index_state: null,
  error: {
    code: "index_source_unavailable",
    message: "sibling index job unreachable",
  },
};

// The POST 200 of the attach door: raw bytes stored durably, NO
// preparation started. Every attachment door answers with this same
// shape.
const ATTACH_UPLOADED: ThreadAttachment = {
  attachment_id: "att-1",
  filename: "notes.md",
  size_bytes: 6,
  scope: "thread_attach",
  status: "uploaded",
  version: 0,
  unit_count: 0,
  searchable: false,
  failure_reason: null,
};

// A record whose preparation the graph has already requested: the only
// state the readiness poll follows.
const ATTACH_PARSING: ThreadAttachment = {
  ...ATTACH_UPLOADED,
  status: "parsing",
};

const ATTACH_READY: ThreadAttachment = {
  ...ATTACH_UPLOADED,
  status: "ready",
  version: 1,
  unit_count: 2,
  searchable: true,
};

const ATTACH_FAILED: ThreadAttachment = {
  ...ATTACH_UPLOADED,
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

/** Record every fetch and answer from ``handler``. A handler may
 * answer with a promise so a test can hold one request open — the
 * in-flight upload the send gate has to see. */
function stubFetch(
  handler: (
    url: string,
    init: RequestInit | undefined,
  ) => Response | Promise<Response>,
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

// The run id the host's ``attachment_wait`` frame mints for the
// suspended run (host workspace_http: ``{"run_id", "kind",
// "attachment_ids"}``, kind ``attachment_preparation``). The store
// records it on the optimistic row so a reload reconciles against the
// persisted record instead of duplicating the turn.
const SUSPEND_RUN_ID = "22222222-2222-4222-8222-222222222222";

/** SSE run the graph SUSPENDED on ``attachment_preparation`` (T2f):
 * the exact ``attachment_wait`` frame arrives, then the stream closes
 * with NO terminal status frame — the run waits on the durable job
 * and auto-resumes. */
function sseSuspendedResponse(): Response {
  const frames = [
    `event: attachment_wait\ndata: ${JSON.stringify({
      run_id: SUSPEND_RUN_ID,
      kind: "attachment_preparation",
      attachment_ids: ["att-1"],
    })}\n\n`,
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

function seedThread(detailOverrides: Partial<ThreadDetail> = {}): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
    ...detailOverrides,
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    threads: [],
    corpus: [],
    corpusLoaded: false,
    catalogOverview: null,
    catalogOverviewLoaded: false,
    sendingQuestion: false,
    resumingThreadId: null,
    attachments: [],
    pendingUploads: {},
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
    // One upload flow: no mode flag rides the wire.
    expect(form.has("generic")).toBe(false);
    expect(useWorkspaceStore.getState().ingestJobId).toBe("AAPL-2022Q3");
    expect(useWorkspaceStore.getState().ingestAccepted).toEqual(INGEST_OK);
    // An accept is not a completion: the catalog row does not exist yet,
    // so nothing is refreshed on this response.
    expect(calls.some((c) => c.url === "/api/corpus")).toBe(false);
  });

  it("sends the optional format / doc_type only when supplied", async () => {
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
        doc_type: "financial_statement",
      },
    );
    const form = calls[0].body as FormData;
    expect(form.get("format")).toBe("md");
    expect(form.get("doc_type")).toBe("financial_statement");
    // The caller can never choose the identity.
    expect(form.has("document_id")).toBe(false);

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
    expect((failure as IngestFailedError).code).toBe("unknown_job");
    expect(useWorkspaceStore.getState().ingestJob).toBeNull();
    // A persisted id that outlived its job must not be re-read forever.
    expect(storage.getItem(LAST_INGEST_JOB_KEY)).toBeNull();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(1);
  });

  it("a transport blip on the status read surfaces and keeps following the job", async () => {
    seedThread();
    let reads = 0;
    stubFetch(() => {
      reads += 1;
      throw new TypeError("network down");
    });

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);

    // The failed read is visible (never a blank "still queued" panel)…
    const failure = useWorkspaceStore.getState().ingestJobFailure;
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(IngestFailedError);
    expect(useWorkspaceStore.getState().ingestJob).toBeNull();
    // …and a blip is no verdict on the job, so the poll must continue.
    const afterFirstRead = reads;
    await vi.advanceTimersByTimeAsync(3_000);
    expect(reads).toBeGreaterThan(afterFirstRead);
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

describe("catalog publication warning (T6: durable, polled, typed)", () => {
  it("a failed publication keeps the job cataloging and carries the typed warning", async () => {
    seedThread();
    stubFetch((url) => {
      if (url === STATUS_URL) {
        return jsonResponse(
          jobStatus({
            state: "cataloging",
            catalog_warning: {
              code: "catalog_publication_failed",
              attempts: 2,
              failure_code: "catalog_unavailable",
              failure_stage: "lock",
            },
            catalog_warning_count: 1,
          }),
        );
      }
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);

    const job = useWorkspaceStore.getState().ingestJob;
    // The job is NOT ready: the push landed but the row is not catalogued.
    expect(job?.state).toBe("cataloging");
    expect(job?.text_ready).toBe(false);
    expect(job?.catalog_warning?.code).toBe("catalog_publication_failed");
    expect(job?.catalog_warning?.failure_code).toBe("catalog_unavailable");
    expect(job?.catalog_warning_count).toBe(1);
  });

  it("a pending publication reads as recovery, not as a failure", async () => {
    seedThread();
    stubFetch((url) =>
      url === STATUS_URL
        ? jsonResponse(
            jobStatus({
              state: "cataloging",
              catalog_warning: {
                code: "catalog_publication_pending",
                attempts: 1,
                stage: "recovery",
              },
              catalog_warning_count: 1,
            }),
          )
        : jsonResponse({}, 404),
    );

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);

    const warning = useWorkspaceStore.getState().ingestJob?.catalog_warning;
    expect(warning?.code).toBe("catalog_publication_pending");
    expect(warning?.failure_code).toBeUndefined();
    expect(warning?.stage).toBe("recovery");
  });

  it("a recorded success clears the warning and the count", async () => {
    seedThread();
    stubFetch((url) =>
      url === STATUS_URL
        ? jsonResponse(
            jobStatus({
              state: "ready",
              text_ready: true,
              catalog_warning: null,
              catalog_warning_count: 0,
            }),
          )
        : jsonResponse({ filings: [] }),
    );

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);

    const job = useWorkspaceStore.getState().ingestJob;
    expect(job?.catalog_warning).toBeNull();
    expect(job?.catalog_warning_count).toBe(0);
  });

  it("the warning copy never echoes a server string", () => {
    // The safe-copy table is closed: the wire carries only codes, so a
    // persisted exception message has no path to the screen.
    for (const code of ["catalog_publication_pending", "catalog_publication_failed"] as const) {
      expect(CATALOG_WARNING_COPY[code]).not.toMatch(/[/\\]|\.py|Exception|Traceback/);
    }
  });
});

describe("catalog overview read (T6: the generic surface)", () => {
  it("refreshes the overview exactly when the catalog row exists", async () => {
    seedThread();
    const { calls } = stubFetch((url) => {
      if (url === STATUS_URL) return jsonResponse(jobStatus({ state: "cataloging" }));
      if (url === "/api/corpus") return jsonResponse({ filings: [] });
      if (url === OVERVIEW_URL) return jsonResponse({ documents: [], dimensions: [], flat_titles: [] });
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);
    // Not catalogued yet: the operator must not see the row as visible.
    expect(calls.some((c) => c.url === OVERVIEW_URL)).toBe(false);

    stubFetch((url) => {
      if (url === STATUS_URL) return jsonResponse(jobStatus({ state: "ready", text_ready: true }));
      if (url === "/api/corpus") return jsonResponse({ filings: [] });
      if (url === OVERVIEW_URL) {
        return jsonResponse({
          documents: [
            {
              document_id: "doc_a",
              version: "v1",
              title: "Team Handbook",
              doc_type: "narrative",
              page_count: 2,
            },
          ],
          dimensions: [],
          flat_titles: ["Team Handbook"],
        });
      }
      return jsonResponse({}, 404);
    });
    await vi.advanceTimersByTimeAsync(1000);

    const overview = useWorkspaceStore.getState().catalogOverview;
    expect(overview?.documents).toHaveLength(1);
    expect(overview?.documents[0].title).toBe("Team Handbook");
    expect(useWorkspaceStore.getState().catalogOverviewLoaded).toBe(true);
  });

  it("a metadata-free document decodes with its company absent, not null", async () => {
    seedThread();
    stubFetch((url) =>
      url === OVERVIEW_URL
        ? jsonResponse({
            documents: [
              {
                document_id: "doc_a",
                version: "v1",
                title: "Handbook",
                doc_type: "narrative",
                page_count: 1,
              },
            ],
            dimensions: [],
            flat_titles: ["Handbook"],
          })
        : jsonResponse({}, 404),
    );

    await useWorkspaceStore.getState().loadCatalogOverview();

    const document = useWorkspaceStore.getState().catalogOverview?.documents[0];
    expect(document?.company).toBeUndefined();
    expect(document?.period).toBeUndefined();
  });

  it("a malformed overview is an empty overview, not a thrown parse", async () => {
    seedThread();
    stubFetch((url) =>
      url === OVERVIEW_URL
        ? jsonResponse({ documents: [{ document_id: "", version: "v" }] })
        : jsonResponse({}, 404),
    );

    await useWorkspaceStore.getState().loadCatalogOverview();

    // Empty rather than half-decoded: a surface must not render a
    // document whose identity it could not validate.
    expect(useWorkspaceStore.getState().catalogOverview).toEqual({
      documents: [],
      dimensions: [],
      flat_titles: [],
    });
    expect(useWorkspaceStore.getState().catalogOverviewLoaded).toBe(true);
  });
});

describe("graph/C report (pass-through + the two readiness lines)", () => {
  /** Poll one status carrying ``graph`` and hand back the stored job. */
  async function pollOnce(graph: GraphIndexReport | null): Promise<JobStatus> {
    seedThread();
    stubFetch((url) => {
      if (url === STATUS_URL) {
        return jsonResponse(
          jobStatus({ state: "ready", text_ready: true, graph }),
        );
      }
      if (url === "/api/corpus") return jsonResponse({ filings: [] });
      return jsonResponse({}, 404);
    });
    await useWorkspaceStore.getState().loadIngestJob("AAPL-2022Q3", 1000);
    const job = useWorkspaceStore.getState().ingestJob;
    expect(job).not.toBeNull();
    return job as JobStatus;
  }

  it("passes the sibling report through untouched — null and shaped", async () => {
    // Not reported yet: the graph stays its own null, even though the
    // TEXT line is ready — the two readinesses never merge.
    const unreported = await pollOnce(null);
    expect(unreported.text_ready).toBe(true);
    expect(unreported.graph).toBeNull();

    const ready = await pollOnce(GRAPH_COMMUNITY_READY);
    expect(ready.graph).toEqual(GRAPH_COMMUNITY_READY);

    const failed = await pollOnce(GRAPH_COMMUNITY_FAILED);
    expect(failed.graph).toEqual(GRAPH_COMMUNITY_FAILED);

    const errored = await pollOnce(GRAPH_ERROR);
    expect(errored.graph).toEqual(GRAPH_ERROR);
  });

  it("says 未报告 while the door has no sibling report at all", () => {
    expect(describeGraphReadiness(null)).toEqual({
      communityLabel: "未报告",
      jobs: [],
      errorLabel: null,
    });
  });

  it("reads 就绪 ONLY from community_index_state, never from jobs[]", () => {
    expect(
      describeGraphReadiness(GRAPH_COMMUNITY_READY).communityLabel,
    ).toBe("就绪");
    // The inverse guard: every per-source entry says ready but the
    // community state is absent — per-source progress must not read as
    // corpus-wide readiness.
    const allSourcesReady: GraphIndexReport = {
      jobs: [
        {
          job_id: "ij-1",
          source_ref: "AAPL-2022Q3",
          status: "ready",
          failure_code: null,
        },
      ],
      community_index_state: null,
      error: null,
    };
    expect(describeGraphReadiness(allSourcesReady).communityLabel).toBe(
      "未确认",
    );
    // Unmapped sibling statuses stay 未确认 — fin does not invent the
    // sibling vocabulary.
    expect(
      describeGraphReadiness({
        ...allSourcesReady,
        community_index_state: { status: "pending", failure_code: null },
      }).communityLabel,
    ).toBe("未确认");
  });

  it("shows the actual community state: 进行中 / 失败 + code", () => {
    expect(
      describeGraphReadiness({
        jobs: [],
        community_index_state: { status: "in_progress", failure_code: null },
        error: null,
      }).communityLabel,
    ).toBe("进行中");
    expect(
      describeGraphReadiness(GRAPH_COMMUNITY_FAILED).communityLabel,
    ).toBe("失败：community_build_failed");
    // A failed state without a code is still named as unnamed, never
    // hidden behind a generic label.
    expect(
      describeGraphReadiness({
        jobs: [],
        community_index_state: { status: "failed", failure_code: null },
        error: null,
      }).communityLabel,
    ).toBe("失败：未命名");
  });

  it("lists per-source entries compactly with their own states", () => {
    expect(describeGraphReadiness(GRAPH_COMMUNITY_READY).jobs).toEqual([
      { source_ref: "AAPL-2022Q3", label: "进行中" },
    ]);
    expect(describeGraphReadiness(GRAPH_COMMUNITY_FAILED).jobs).toEqual([
      { source_ref: "AAPL-2022Q3", label: "就绪" },
      { source_ref: "MSFT-2022Q1", label: "失败：extract_crash" },
    ]);
  });

  it("names the error code instead of hiding the failed sibling read", () => {
    const view = describeGraphReadiness(GRAPH_ERROR);
    expect(view.errorLabel).toContain("index_source_unavailable");
    // The error says nothing about readiness: the community line keeps
    // its own 未确认 answer.
    expect(view.communityLabel).toBe("未确认");
  });
});

describe("attach door (store.attachFile)", () => {
  it("POSTs raw bytes with the filename query param (never multipart)", async () => {
    seedThread();
    const { calls } = stubFetch((url) =>
      url === LIST_URL
        ? jsonResponse({ attachments: [ATTACH_READY] })
        : jsonResponse(ATTACH_UPLOADED),
    );

    const file = new File(["hello!"], "notes.md", { type: "text/markdown" });
    const attachment = await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, file);

    expect(attachment.attachment_id).toBe("att-1");
    expect(attachment.scope).toBe("thread_attach");
    // The door answers once the bytes are durable. Preparation has
    // NOT started: the graph turn requests it, so the record lands in
    // the thread slice as ``uploaded``.
    expect(attachment.status).toBe("uploaded");
    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_UPLOADED,
    ]);
    const call = calls[0];
    expect(call.url).toBe(`${LIST_URL}?filename=notes.md`);
    expect(call.method).toBe("POST");
    expect(call.body).not.toBeInstanceOf(FormData);
    expect(new TextDecoder().decode(call.body as Uint8Array)).toBe("hello!");
  });

  it("stores non-text bytes instead of refusing them", async () => {
    // The upload door does not classify content, so a PDF — bytes the
    // old door rejected with 400 — is stored and reported
    // ``uploaded``; a typed rejection, if any, comes later from
    // preparation.
    seedThread();
    const pdf = { ...ATTACH_UPLOADED, filename: "10-K.pdf" };
    const { calls } = stubFetch((url) =>
      url === LIST_URL
        ? jsonResponse({ attachments: [] })
        : jsonResponse(pdf),
    );

    const attachment = await useWorkspaceStore
      .getState()
      .attachFile(
        THREAD_ID,
        new File([new Uint8Array([0x25, 0x50, 0x44, 0x46, 0xff])], "10-K.pdf"),
      );

    expect(attachment.status).toBe("uploaded");
    expect(attachment.filename).toBe("10-K.pdf");
    const body = calls[0].body as Uint8Array;
    expect(body).not.toBeInstanceOf(FormData);
    expect(useWorkspaceStore.getState().attachments).toEqual([attachment]);
  });

  it("throws the typed error carrying the named failure code", async () => {
    seedThread();
    stubFetch((url) =>
      url.startsWith(LIST_URL) && url !== LIST_URL
        ? jsonResponse({ error: "attachment_too_large" }, 413)
        : jsonResponse({}, 404),
    );

    const rejection = await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["\xff"], "huge-10k.pdf"))
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(AttachFailedError);
    expect((rejection as AttachFailedError).code).toBe("attachment_too_large");
  });
});

describe("two doors never cross (acceptance)", () => {
  it("attach-then-send calls the attach endpoint and NEVER the ingest endpoint", async () => {
    seedThread();
    const { calls } = stubFetch((url) => {
      if (url === LIST_URL) return jsonResponse({ attachments: [] });
      if (url.includes("/attachments")) {
        return jsonResponse(ATTACH_UPLOADED);
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
  it("names READY plus the durable uploaded/parsing records the graph waits on", () => {
    // T2f: a grounded send referencing a durable ``uploaded`` or
    // ``parsing`` record is ACCEPTED — the run suspends inside the
    // graph until preparation completes and resumes itself — so those
    // states ride the message instead of being withheld.
    const selection = attachmentsForSend([
      { ...ATTACH_READY, attachment_id: "ready-1" },
      { ...ATTACH_UPLOADED, attachment_id: "uploaded-1" },
      { ...ATTACH_PARSING, attachment_id: "parsing-1" },
      { ...ATTACH_FAILED, attachment_id: "failed-1" },
    ]);
    expect(selection.ids).toEqual(["ready-1", "uploaded-1", "parsing-1"]);
    // Only a record whose reference the host would refuse stays out —
    // and it stays visible: the rule may never drop a record silently.
    expect(selection.withheld.map((a) => a.attachment_id)).toEqual([
      "failed-1",
    ]);
  });

  it("withholds a ready record the host does not report searchable", () => {
    const selection = attachmentsForSend([
      { ...ATTACH_READY, attachment_id: "ready-1", searchable: false },
    ]);
    expect(selection.ids).toEqual([]);
    expect(selection.withheld.map((a) => a.attachment_id)).toEqual([
      "ready-1",
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
        { ...ATTACH_PARSING, attachment_id: "att-2" },
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
    // READY and the still-parsing one both ride; only the failed one
    // is left off (and rendered as withheld).
    expect(request.attachment_ids).toEqual(["att-1", "att-2"]);
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
          attachments: [reads >= 2 ? ATTACH_READY : ATTACH_PARSING],
        });
      }
      return jsonResponse(ATTACH_PARSING);
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
      return jsonResponse(ATTACH_PARSING);
    });

    await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["hello!"], "notes.md"));
    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_PARSING,
    ]);

    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments).toEqual([ATTACH_READY]);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(2);
  });

  it("retry answers a settled record, and a settled record starts no poll", async () => {
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_FAILED] });
    const retryUrl = `${LIST_URL}/att-1/retry`;
    let listReads = 0;
    const { calls } = stubFetch((url) => {
      if (url === retryUrl) {
        return jsonResponse({ ...ATTACH_READY, version: 2 });
      }
      if (url === LIST_URL) {
        listReads += 1;
        return jsonResponse({ attachments: [{ ...ATTACH_READY, version: 2 }] });
      }
      return jsonResponse({ error: "attachment_not_found" }, 400);
    });

    await useWorkspaceStore.getState().retryAttachment(THREAD_ID, "att-1");
    const retried = calls.find((c) => c.url === retryUrl);
    expect(retried?.method).toBe("POST");
    // The door runs preparation INSIDE the request, so the record it
    // hands back is already FINAL: citable, at the new version, patched
    // straight into the slice.
    expect(useWorkspaceStore.getState().attachments[0]).toEqual({
      ...ATTACH_READY,
      version: 2,
    });
    // And nothing left to follow: a settled record cannot move by
    // itself, so retry issues no list read behind it.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(listReads).toBe(0);

    // The other settled answer the door gives: a re-drive that fails
    // again is the record's own named failure, never a stuck chip.
    stubFetch((url) =>
      url === retryUrl
        ? jsonResponse(ATTACH_FAILED)
        : jsonResponse({ error: "attachment_not_found" }, 400),
    );
    await useWorkspaceStore.getState().retryAttachment(THREAD_ID, "att-1");
    expect(useWorkspaceStore.getState().attachments[0]).toEqual(ATTACH_FAILED);

    // A deleted / foreign attachment id answers the named 400.
    const rejection = await useWorkspaceStore
      .getState()
      .retryAttachment(THREAD_ID, "gone")
      .then(() => null)
      .catch((error: unknown) => error);
    expect(rejection).toBeInstanceOf(AttachFailedError);
    expect((rejection as AttachFailedError).code).toBe("attachment_not_found");
  });

  it("a pre-T2f host refusal of a parsing reference still surfaces the named error", async () => {
    // T2f moved this outcome into the graph (the host now ACCEPTS the
    // reference and waits), so 400 ``attachment_not_ready`` arrives
    // only from an older peer. The client's typed mapping stays: a
    // refusal on the wire must never read as a transport failure.
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
        return jsonResponse({ attachments: [ATTACH_PARSING] });
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
      ATTACH_PARSING,
    ]);
    await useWorkspaceStore.getState().openThread(OTHER_ID);
    expect(useWorkspaceStore.getState().attachments).toEqual([]);

    await vi.advanceTimersByTimeAsync(10_000);
    // The old thread's chain stopped with the switch: no further
    // list reads for it.
    expect(reads).toBe(1);
  });

  it("an uploaded upload starts no readiness poll", async () => {
    // Preparation is requested by a graph turn, not by the browser,
    // so an ``uploaded`` record NO RUN REFERENCES will not change on
    // its own; polling it would spin forever. The T2f wait poll is
    // started by the waiting send (or a load seeing the pending
    // checkpoint), never by the upload itself.
    seedThread();
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [] });
      }
      return jsonResponse(ATTACH_UPLOADED);
    });

    await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["hello!"], "10-K.pdf"));
    await vi.advanceTimersByTimeAsync(10_000);

    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_UPLOADED,
    ]);
    expect(reads).toBe(0);
  });

  it("a list of only uploaded records does not keep the poll running", async () => {
    // No pending checkpoint on this thread either: nothing references
    // the upload, so the one seeding read is all the chain gets.
    seedThread();
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [ATTACH_UPLOADED] });
      }
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000);
    expect(useWorkspaceStore.getState().attachments).toEqual([
      ATTACH_UPLOADED,
    ]);

    await vi.advanceTimersByTimeAsync(10_000);
    // The one seeding read, then the chain stopped itself.
    expect(reads).toBe(1);
  });

  it("a stale list read cannot erase the just-uploaded chip", async () => {
    // The upload answers ``uploaded`` and starts no poll of its own, so
    // a tick issued before the POST committed must still carry the fresh
    // record — dropping it would hide a live attachment until the next
    // manual open.
    seedThread();
    const parsing = { ...ATTACH_PARSING, attachment_id: "att-0" };
    const uploaded = { ...ATTACH_UPLOADED, attachment_id: "att-1" };
    stubFetch((url) => {
      if (url === LIST_URL) return jsonResponse({ attachments: [parsing] });
      return jsonResponse(uploaded);
    });

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000);
    await useWorkspaceStore
      .getState()
      .attachFile(THREAD_ID, new File(["x"], "10-K.pdf"));
    expect(useWorkspaceStore.getState().attachments).toEqual([
      parsing,
      uploaded,
    ]);

    await vi.advanceTimersByTimeAsync(1000);
    expect(useWorkspaceStore.getState().attachments).toEqual([
      parsing,
      uploaded,
    ]);
  });
});

describe("preparation wait (T2f: graph job gate)", () => {
  /** A thread detail as the GET reports it: pending-checkpoint flag
   * plus the T2f ``pending_interrupt_kind`` that says WHAT the pause
   * waits for. */
  function checkpointDetail(
    pending?: boolean,
    kind?: string | null,
  ): ThreadDetail {
    return {
      id: THREAD_ID,
      title: "AAPL 10-Q",
      created_at: "2026-01-01T00:00:00.000Z",
      turns: [],
      pending_checkpoint: pending,
      pending_interrupt_kind: kind,
    };
  }

  it("the wait signal keys off the server's interrupt kind, not the chip status", () => {
    // The exact reload-path signal (review C4): the host names the
    // pause, so no attachment-list reading is involved.
    expect(
      attachmentPreparationWait(
        checkpointDetail(true, "attachment_preparation"),
      ),
    ).toBe(true);
    // A choice window owns its resume through the operator's pick:
    // the banner behavior is untouched.
    expect(
      attachmentPreparationWait(checkpointDetail(true, "choice_window")),
    ).toBe(false);
    // A plain crash checkpoint (kind null / absent): the banner stays.
    expect(attachmentPreparationWait(checkpointDetail(true, null))).toBe(false);
    expect(
      attachmentPreparationWait(checkpointDetail(true, undefined)),
    ).toBe(false);
    // A kind without the pending flag cannot be a wait either — the
    // kind only rides a genuinely paused thread.
    expect(
      attachmentPreparationWait(
        checkpointDetail(false, "attachment_preparation"),
      ),
    ).toBe(false);
    expect(
      attachmentPreparationWait(
        checkpointDetail(undefined, "attachment_preparation"),
      ),
    ).toBe(false);
    expect(attachmentPreparationWait(null)).toBe(false);
  });

  it("a send the graph suspends keeps the turn live and follows the chip to ready", async () => {
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_UPLOADED] });
    let reads = 0;
    stubFetch((url) => {
      if (url.endsWith("/messages")) return sseSuspendedResponse();
      if (url === LIST_URL) {
        reads += 1;
        // Tick 1: the durable job has marked the row parsing. Tick 2:
        // it settled — the poll must stop here, not spin.
        return jsonResponse({
          attachments: [reads >= 2 ? ATTACH_READY : ATTACH_PARSING],
        });
      }
      if (url === "/api/threads") return jsonResponse("[]");
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore
      .getState()
      .sendQuestion(
        THREAD_ID,
        "读附件",
        true,
        attachmentsForSend(useWorkspaceStore.getState().attachments).ids,
      );

    // No terminal frame arrived: the run is suspended in the graph,
    // NOT done — the row stays live while the wait holds.
    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    expect(turns[0].in_flight).toBe(true);
    // The ``attachment_wait`` frame's server-minted run id is recorded
    // on the row: after the auto-resume persists the run, a reload
    // reconciles this row against THAT record instead of flashing a
    // duplicate (review C4).
    expect(turns[0].run_id).toBe(SUSPEND_RUN_ID);

    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("parsing");
    await vi.advanceTimersByTimeAsync(2000);
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("ready");
    // Settled: the chain stopped on its own.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(2);
  });

  it("a stream that ends with NO attachment_wait frame is not a wait", async () => {
    // Review C4's false-positive fix: the old heuristic read ANY
    // stream that ended without a terminal frame — a crash, a
    // transport close — as a preparation wait whenever the send
    // happened to name an uploaded/parsing record. Only the exact
    // frame starts the wait now.
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_UPLOADED] });
    let reads = 0;
    stubFetch((url) => {
      if (url.endsWith("/messages")) {
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.close();
          },
        }), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      }
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [ATTACH_UPLOADED] });
      }
      if (url === "/api/threads") return jsonResponse("[]");
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "读附件", true, ["att-1"]);

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns).toHaveLength(1);
    // Without the frame the row is not live and no wait chain starts.
    expect(turns[0].in_flight).toBe(false);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(0);
  });

  it("a completed send that referenced an uploaded record starts no wait poll", async () => {
    // The terminal status frame answers the run — the stream ending
    // without one is the ONLY signal of the suspend, so a normal run
    // over an uploaded reference must not open a poll either.
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_UPLOADED] });
    let reads = 0;
    stubFetch((url) => {
      if (url.endsWith("/messages")) return sseRunResponse();
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [ATTACH_UPLOADED] });
      }
      if (url === "/api/threads") return jsonResponse("[]");
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore
      .getState()
      .sendQuestion(
        THREAD_ID,
        "读附件",
        true,
        attachmentsForSend(useWorkspaceStore.getState().attachments).ids,
      );

    const turns = useWorkspaceStore.getState().activeThread?.turns ?? [];
    expect(turns[0].in_flight).toBe(false);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(0);
  });

  it("opening a thread mid-wait hides the Resume prompt and follows the row through", async () => {
    // GET reports the released lock plus pending nodes exactly like a
    // crash; the named kind is what keeps the operator from being
    // asked to press Resume for a preparation.
    seedThread({
      pending_checkpoint: true,
      pending_interrupt_kind: "attachment_preparation",
    });
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({
          attachments: [
            reads >= 3
              ? ATTACH_READY
              : reads >= 2
                ? ATTACH_PARSING
                : ATTACH_UPLOADED,
          ],
        });
      }
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000);

    expect(useWorkspaceStore.getState().activeThread?.pending_checkpoint).toBe(
      false,
    );
    // The uploaded row IS work in progress here: a run waits on it,
    // so the chain follows uploaded → parsing → ready and stops.
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("uploaded");
    await vi.advanceTimersByTimeAsync(1000);
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("parsing");
    await vi.advanceTimersByTimeAsync(1000);
    expect(useWorkspaceStore.getState().attachments[0].status).toBe("ready");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(3);
  });

  it("a pending checkpoint over only settled records keeps the Resume prompt", async () => {
    // The crash case stays the crash case: nothing to prepare, so the
    // flag the banner keys off is left exactly as the server said,
    // and no chain starts.
    seedThread({ pending_checkpoint: true });
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [ATTACH_READY, ATTACH_FAILED] });
      }
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000);

    expect(useWorkspaceStore.getState().activeThread?.pending_checkpoint).toBe(
      true,
    );
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(1);
  });

  it("a crash checkpoint with a stray uploaded record keeps the Resume prompt", async () => {
    // Review C4's false-suppression fix: the old heuristic read ANY
    // pending checkpoint plus an uploaded/parsing row as a preparation
    // wait, so a genuine crash on a thread that happened to hold an
    // unreferenced record lost its Resume prompt. The named kind says
    // plainly that this pause is not a preparation wait — the flag
    // stays exactly as the server said.
    seedThread({ pending_checkpoint: true, pending_interrupt_kind: null });
    let reads = 0;
    stubFetch((url) => {
      if (url === LIST_URL) {
        reads += 1;
        return jsonResponse({ attachments: [ATTACH_UPLOADED] });
      }
      return jsonResponse({}, 404);
    });

    await useWorkspaceStore.getState().loadAttachments(THREAD_ID, 1000);

    expect(useWorkspaceStore.getState().activeThread?.pending_checkpoint).toBe(
      true,
    );
    // An unreferenced upload is idle, not work in progress: no chain.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(reads).toBe(1);
  });
});

describe("upload in flight gates send and new conversation", () => {
  /** A fetch answer the test releases by hand: the upload POST stays
   * in flight until ``resolve`` runs. */
  function deferredResponse(): {
    promise: Promise<Response>;
    resolve: (res: Response) => void;
  } {
    let resolve!: (res: Response) => void;
    const promise = new Promise<Response>((res) => {
      resolve = res;
    });
    return { promise, resolve };
  }

  /** Starts one upload and waits until the store has it as in flight. */
  async function startUpload(
    threadId: string,
    filename: string,
  ): Promise<{
    uploading: Promise<ThreadAttachment>;
    release: (res: Response) => void;
    calls: RecordedCall[];
  }> {
    const { promise, resolve } = deferredResponse();
    const attachHandler = (url: string): Response | Promise<Response> => {
      if (url === LIST_URL) {
        return jsonResponse({
          attachments: useWorkspaceStore.getState().attachments,
        });
      }
      if (url.includes("/attachments")) return promise;
      if (url.endsWith("/messages")) return sseRunResponse();
      if (url === "/api/threads") return jsonResponse("[]");
      return jsonResponse({}, 404);
    };
    const { calls } = stubFetch(attachHandler);
    const uploading = useWorkspaceStore
      .getState()
      .attachFile(threadId, new File(["bytes"], filename));
    await vi.advanceTimersByTimeAsync(0);
    return { uploading, release: resolve, calls };
  }

  it("reads the gate off the in-flight uploads, not the attachment list", () => {
    expect(hasUploadInFlight({})).toBe(false);
    // A settled upload leaves no zero counter behind that could keep
    // the composer locked.
    expect(hasUploadInFlight({ [THREAD_ID]: 0 })).toBe(false);
    expect(hasUploadInFlight({ [THREAD_ID]: 1 })).toBe(true);
    // ANY thread's pending upload gates the composer: the record is
    // not on the attachment list yet, so the list cannot answer this.
    expect(
      hasUploadInFlight({ "22222222-2222-4222-8222-222222222222": 1 }),
    ).toBe(true);
  });

  it("refuses a send while an upload is in flight, naming the typed gate", async () => {
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_READY] });
    const { uploading, release, calls } = await startUpload(
      THREAD_ID,
      "10-K.pdf",
    );
    expect(
      hasUploadInFlight(useWorkspaceStore.getState().pendingUploads),
    ).toBe(true);

    // The question names the READY att-1 and never mentions the file
    // being uploaded — that unreferenced upload is exactly what must
    // still block the send.
    const rejection = await useWorkspaceStore
      .getState()
      .sendQuestion(THREAD_ID, "营收多少？", true, ["att-1"])
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(AttachmentUploadInProgressError);
    expect((rejection as AttachFailedError).code).toBe(
      "attachment_upload_in_progress",
    );
    // Refused before the wire: no doomed run was started.
    expect(calls.filter((c) => c.url.endsWith("/messages"))).toEqual([]);

    release(jsonResponse(ATTACH_UPLOADED));
    await uploading;
    expect(
      hasUploadInFlight(useWorkspaceStore.getState().pendingUploads),
    ).toBe(false);
  });

  it("sends the same question once the upload has landed", async () => {
    seedThread();
    useWorkspaceStore.setState({ attachments: [ATTACH_READY] });
    const { uploading, release, calls } = await startUpload(
      THREAD_ID,
      "10-K.pdf",
    );
    release(jsonResponse(ATTACH_UPLOADED));
    await uploading;

    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？", true, [
        "att-1",
      ]),
    ).resolves.toBeTruthy();
    expect(calls.filter((c) => c.url.endsWith("/messages"))).toHaveLength(1);
  });

  it("refuses to start a new conversation while an upload is in flight", async () => {
    // Spec: no conversation STARTS or sends while any composer upload
    // is in flight — 新建 thread is a conversation start.
    seedThread();
    const { uploading, release, calls } = await startUpload(
      THREAD_ID,
      "10-K.pdf",
    );

    const rejection = await useWorkspaceStore
      .getState()
      .createThread(null)
      .then(() => null)
      .catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(AttachmentUploadInProgressError);
    expect((rejection as AttachFailedError).code).toBe(
      "attachment_upload_in_progress",
    );
    expect(
      calls.filter((c) => c.url === "/api/threads" && c.method === "POST"),
    ).toEqual([]);

    release(jsonResponse(ATTACH_UPLOADED));
    await uploading;
  });

  it("clears the gate when the upload is refused, so the composer is never wedged", async () => {
    seedThread();
    stubFetch((url) =>
      url.includes("/attachments") && url !== LIST_URL
        ? jsonResponse({ error: "attachment_too_large" }, 413)
        : jsonResponse({ attachments: [] }),
    );

    await expect(
      useWorkspaceStore
        .getState()
        .attachFile(THREAD_ID, new File(["x"], "huge.pdf")),
    ).rejects.toBeInstanceOf(AttachFailedError);
    expect(
      hasUploadInFlight(useWorkspaceStore.getState().pendingUploads),
    ).toBe(false);

    useWorkspaceStore.setState({ attachments: [ATTACH_READY] });
    stubFetch((url) =>
      url.endsWith("/messages") ? sseRunResponse() : jsonResponse("[]"),
    );
    await expect(
      useWorkspaceStore.getState().sendQuestion(THREAD_ID, "营收多少？"),
    ).resolves.toBeTruthy();
  });
});
