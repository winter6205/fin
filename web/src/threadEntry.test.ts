// The ``/t/{thread_id}`` render decision (ADR 0051).
//
// The two refusals that reach this route are NOT interchangeable: an
// unknown id is a 404, the 知识 MCP gate is a 409 on a thread that
// still exists. Browser verification caught the route rendering the
// second as the first (「对话不存在」 for a thread that was merely
// refused), so these tests pin the decision, not just the copy.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getThread,
  KnowledgeMcpFaultError,
  PENDING_QUESTION_UNAVAILABLE_DETAIL,
  PendingQuestionUnavailableError,
} from "./api";
import {
  classifyThreadOpenFailure,
  threadEntryIsLive,
  threadEntryView,
} from "./threadEntry";
import { decideThreadEntryRoute } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("classifyThreadOpenFailure", () => {
  it("reads the 知识 MCP gate as gated", () => {
    expect(
      classifyThreadOpenFailure(new KnowledgeMcpFaultError("知识 MCP 故障")),
    ).toBe("gated");
  });

  it.each([
    ["a plain 404 error", new Error("HTTP 404: thread_not_found")],
    ["a concurrent run", new Error("HTTP 409: concurrent_run")],
    ["a network failure", new TypeError("Failed to fetch")],
    ["a non-error rejection", "nope"],
    ["no error at all", undefined],
  ])("treats %s as missing", (_label, error) => {
    expect(classifyThreadOpenFailure(error)).toBe("missing");
  });

  it("reads the pending-question 503 as its own fault, not missing", () => {
    expect(
      classifyThreadOpenFailure(
        new PendingQuestionUnavailableError(PENDING_QUESTION_UNAVAILABLE_DETAIL),
      ),
    ).toBe("pending_unavailable");
  });
});

describe("openThread on a pending-question 503 (reload refusal)", () => {
  function stubStatus(status: number, body: unknown): void {
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

  it("classifies GET /api/threads/{id} 503 as PendingQuestionUnavailableError", async () => {
    stubStatus(503, { detail: PENDING_QUESTION_UNAVAILABLE_DETAIL });
    const error = await getThread("t").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(PendingQuestionUnavailableError);
    expect(classifyThreadOpenFailure(error)).toBe("pending_unavailable");
    expect((error as Error).message).toBe(PENDING_QUESTION_UNAVAILABLE_DETAIL);
  });

  it("carries only the fixed retry copy, never checkpoint internals", async () => {
    // A rogue / future host that put internals in the body must not have
    // them echoed to the operator: the client carries its own fixed copy.
    stubStatus(503, { detail: "CHECKPOINT-SECRET-SENTINEL" });
    const error = await getThread("t").catch((e: unknown) => e);
    expect((error as Error).message).toBe(PENDING_QUESTION_UNAVAILABLE_DETAIL);
    expect((error as Error).message).not.toContain("SENTINEL");
  });
});

// GET /api/threads/{id} has TWO unrelated 503 causes. Only the pending-
// question projection failure answers the FastAPI ``{"detail": …}`` body;
// a thread-store / run-list outage answers ``{"error":
// "persistence_unavailable"}`` (pinned host-side at
// src/fin/host/workspace_http.py). Before the pending-question branch
// landed, the store outage fell through to a plain transport error and
// the route rendered 「对话不存在」 — the store outage must keep exactly
// that reading, NOT the live-thread 「待回答的问题读不出来」 claim.
describe("openThread on a thread-store 503 (persistence outage)", () => {
  function stubStatus(status: number, body: unknown): void {
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

  it("does NOT read the store outage as a pending-question refusal", async () => {
    stubStatus(503, { error: "persistence_unavailable" });
    const error = await getThread("t").catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(PendingQuestionUnavailableError);
    const fault = classifyThreadOpenFailure(error);
    expect(fault).toBe("missing");
    expect(
      threadEntryView({ loading: false, fault, threadLoaded: false }),
    ).toBe("missing");
  });

  it("keeps the pre-existing transport error for a non-pending 503", async () => {
    stubStatus(503, { error: "persistence_unavailable" });
    const error = await getThread("t").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain("503");
    expect((error as Error).message).toContain("persistence_unavailable");
  });

  it("does not throw a parse error out of getThread on an unreadable body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("not json at all", { status: 503 })),
    );
    const error = await getThread("t").catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(PendingQuestionUnavailableError);
    expect(error).toBeInstanceOf(Error);
    expect(classifyThreadOpenFailure(error)).toBe("missing");
  });
});

describe("threadEntryView", () => {
  it("waits while the GET is in flight", () => {
    expect(
      threadEntryView({ loading: true, fault: null, threadLoaded: false }),
    ).toBe("loading");
  });

  it("does not flash not-found while loading", () => {
    // The reload race: activeThread is still empty on the first render
    // of a perfectly valid thread.
    expect(
      threadEntryView({ loading: true, fault: null, threadLoaded: false }),
    ).not.toBe("missing");
  });

  it("shows the gate notice for a refused thread", () => {
    expect(
      threadEntryView({ loading: false, fault: "gated", threadLoaded: false }),
    ).toBe("gated");
  });

  it("prefers the gate over a stale previous thread", () => {
    // ``threadLoaded`` is false here (activeThread holds ANOTHER id),
    // and the gate must still win over the missing branch.
    expect(
      threadEntryView({ loading: false, fault: "gated", threadLoaded: false }),
    ).not.toBe("missing");
  });

  it("shows not-found for an unknown id", () => {
    expect(
      threadEntryView({
        loading: false,
        fault: "missing",
        threadLoaded: false,
      }),
    ).toBe("missing");
  });

  it("renders the thread once it is loaded", () => {
    expect(
      threadEntryView({ loading: false, fault: null, threadLoaded: true }),
    ).toBe("thread");
  });

  it("shows not-found when nothing loaded and nothing failed", () => {
    expect(
      threadEntryView({ loading: false, fault: null, threadLoaded: false }),
    ).toBe("missing");
  });

  it("keeps the gate ahead of a loaded thread (refusal wins)", () => {
    expect(
      threadEntryView({ loading: false, fault: "gated", threadLoaded: true }),
    ).toBe("gated");
  });

  it("shows the retry notice for a pending-question refusal, not not-found", () => {
    expect(
      threadEntryView({
        loading: false,
        fault: "pending_unavailable",
        threadLoaded: false,
      }),
    ).toBe("pending_unavailable");
  });

  it("keeps the pending-question refusal ahead of a stale previous thread", () => {
    expect(
      threadEntryView({
        loading: false,
        fault: "pending_unavailable",
        threadLoaded: false,
      }),
    ).not.toBe("missing");
  });

  it("does not flash the retry notice while the GET is still loading", () => {
    expect(
      threadEntryView({
        loading: true,
        fault: "pending_unavailable",
        threadLoaded: false,
      }),
    ).toBe("loading");
  });
});

// T2 (plans/2026-10-20-qa-design-correction.md, task 2; the demo
// amendment's "Streaming entry" bullet): the ONE in-flight rule.
//
// A thread THIS client is streaming on keeps its live answer — the live
// send owns it, and a loader over it would cover an answer that is
// already arriving. It is live only while the pane STILL HOLDS it:
// when the operator left the thread, the live state is gone and the
// entry read is required.
//
// The store's entry guard, the route's entry decision and the thread
// page's re-sync all ask this one predicate, so they cannot disagree
// about what "live" means.
describe("threadEntryIsLive", () => {
  it("is live while the pane holds the streaming thread", () => {
    expect(
      threadEntryIsLive({
        threadId: "t1",
        sendingThreadId: "t1",
        activeThreadId: "t1",
      }),
    ).toBe(true);
  });

  it("is not live for a thread this client is not sending on", () => {
    expect(
      threadEntryIsLive({
        threadId: "t1",
        sendingThreadId: null,
        activeThreadId: "t1",
      }),
    ).toBe(false);
    expect(
      threadEntryIsLive({
        threadId: "t1",
        sendingThreadId: "t2",
        activeThreadId: "t1",
      }),
    ).toBe(false);
  });

  it("is not live once the operator left the streaming thread", () => {
    // The pane holds another thread: the live state is gone, so the
    // entry read is REQUIRED.
    expect(
      threadEntryIsLive({
        threadId: "t1",
        sendingThreadId: "t1",
        activeThreadId: "t2",
      }),
    ).toBe(false);
  });

  it("agrees with the route about a thread the operator left", () => {
    const route = decideThreadEntryRoute({
      threadId: "t1",
      sendingThreadId: "t1",
      activeThreadId: "t2",
      reading: true,
      fault: null,
    });
    // The route shows its loader (the read is required) and the shared
    // rule says the thread is not live — the same answer, from one rule.
    expect(route.view).toBe("loading");
    expect(
      threadEntryIsLive({
        threadId: "t1",
        sendingThreadId: "t1",
        activeThreadId: "t2",
      }),
    ).toBe(false);
  });
});
