// The 知识 MCP gate surfaces on the SPA entry points (ADR 0051).
//
// Browser verification found two silent / wrong-attribution failures
// that the unit suite could not see, because they live on the fetch
// path the store and the sidebar share:
//
//   1. NEW THREAD on a 缺 / 坏 boot: POST /api/threads answers 409
//      {error: knowledge_mcp_fault}. ``NewThreadButton`` had no catch
//      at all, so the click looked like a no-op.
//   2. OPEN THREAD on the same boot: GET /api/threads/{id} answers the
//      same 409, but ``getThread`` let it fall through ``jsonOrThrow``
//      as a generic Error. ``ThreadRoute`` read that as "unknown id"
//      and told the operator the thread did not exist — a claim that
//      is wrong about a thread which is merely refused.
//
// These tests drive the real ``api`` functions against a stubbed
// fetch, so they fail if either 409 stops being classified.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ConcurrentRunError,
  createThread,
  getThread,
  IngestMissingError,
  KnowledgeMcpFaultError,
} from "./api";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";
import {
  classifySendFailure,
  describeSendFailure,
  describeThreadCreateFailure,
} from "./sendFailure";

const THREAD_ID = "33333333-3333-4333-8333-333333333333";

/** Answer every request with the host's 409 refusal — the exact body
 * from src/fin/host/http.py. A fresh Response per call: a body can
 * only be read once. */
function stubGateRefusal(): void {
  stubConflict({ error: "knowledge_mcp_fault", message: "知识 MCP 故障" });
}

/** Answer every request with an arbitrary 409 body. */
function stubConflict(body: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status: 409,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("knowledge-MCP gate classification", () => {
  it("classifies POST /api/threads 409 as KnowledgeMcpFaultError", async () => {
    stubGateRefusal();
    await expect(createThread(null)).rejects.toBeInstanceOf(
      KnowledgeMcpFaultError,
    );
  });

  it("classifies GET /api/threads/{id} 409 as KnowledgeMcpFaultError", async () => {
    stubGateRefusal();
    await expect(getThread(THREAD_ID)).rejects.toBeInstanceOf(
      KnowledgeMcpFaultError,
    );
  });

  it("carries the host message, not the bare error token", async () => {
    stubGateRefusal();
    const error = await getThread(THREAD_ID).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(KnowledgeMcpFaultError);
    expect((error as Error).message).toBe(KNOWLEDGE_MCP_FAULT_COPY);
    expect((error as Error).message).not.toContain("knowledge_mcp_fault");
  });

  it("is NOT mistaken for a concurrent run", () => {
    const kind = classifySendFailure(
      new KnowledgeMcpFaultError(KNOWLEDGE_MCP_FAULT_COPY),
    );
    expect(kind).toBe("knowledge_mcp_fault");
  });
});

describe("the two entry doors name the refusal differently", () => {
  it("send says the question went back to the composer", () => {
    const notice = describeSendFailure(
      new KnowledgeMcpFaultError(KNOWLEDGE_MCP_FAULT_COPY),
    );
    expect(notice.message).toContain(KNOWLEDGE_MCP_FAULT_COPY);
    expect(notice.message).toContain("放回输入框");
  });

  it("create does NOT claim a draft was restored (there is none)", () => {
    // The Sidebar renders exactly this string under 新建 thread.
    const notice = describeThreadCreateFailure(
      new KnowledgeMcpFaultError(KNOWLEDGE_MCP_FAULT_COPY),
    );
    expect(notice.kind).toBe("knowledge_mcp_fault");
    expect(notice.message).toContain(KNOWLEDGE_MCP_FAULT_COPY);
    expect(notice.message).toContain("新建不了");
    expect(notice.message).not.toContain("放回输入框");
  });
});

describe("the create notice never claims a draft (that door has none)", () => {
  // EVERY failure on 新建 thread — typed, unrecognised, partial — must
  // stay clear of the send path's 「已经放回输入框」 sentence. The
  // earlier fix guarded only the gate branch, so a 500 rendered
  // 「你的问题已经放回输入框」 about a textarea that does not exist.
  const failures: unknown[] = [
    new KnowledgeMcpFaultError(KNOWLEDGE_MCP_FAULT_COPY),
    new ConcurrentRunError("concurrent_run"),
    new IngestMissingError("ingest_missing"),
    new Error("HTTP 500: boom"),
    new TypeError("Failed to fetch"),
    "not an error",
    undefined,
  ];

  it.each(failures.map((e, i) => [i, e] as const))(
    "failure #%i avoids the restore claim and never ends up blank",
    (_index, error) => {
      const notice = describeThreadCreateFailure(error);
      expect(notice.message.length).toBeGreaterThan(0);
      expect(notice.message).not.toContain("放回输入框");
      expect(notice.message).not.toContain("你的问题");
    },
  );

  it("still names the gate when the gate is the cause", () => {
    const notice = describeThreadCreateFailure(
      new KnowledgeMcpFaultError(KNOWLEDGE_MCP_FAULT_COPY),
    );
    expect(notice.kind).toBe("knowledge_mcp_fault");
    expect(notice.message).toContain(KNOWLEDGE_MCP_FAULT_COPY);
    expect(notice.message).toContain("新建不了");
  });
});

describe("a non-gate 409 on the thread doors is not a held run", () => {
  // Only the send doors can answer 409 concurrent_run. If a thread
  // door ever answers an unrecognised 409, reporting 「还有一轮在
  // 运行」 would be a false attribution about a brand-new thread.
  it("create surfaces a generic error, never ConcurrentRunError", async () => {
    stubConflict({ error: "something_else" });
    const error = await createThread(null).catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(ConcurrentRunError);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain("409");
  });

  it("get surfaces a generic error, never ConcurrentRunError", async () => {
    stubConflict({ error: "something_else" });
    const error = await getThread(THREAD_ID).catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(ConcurrentRunError);
  });

  it("still classifies the gate token on both doors", async () => {
    for (const call of [() => createThread(null), () => getThread(THREAD_ID)]) {
      stubGateRefusal();
      await expect(call()).rejects.toBeInstanceOf(KnowledgeMcpFaultError);
      vi.unstubAllGlobals();
    }
  });
});
