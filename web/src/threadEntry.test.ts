// The ``/t/{thread_id}`` render decision (ADR 0051).
//
// The two refusals that reach this route are NOT interchangeable: an
// unknown id is a 404, the 知识 MCP gate is a 409 on a thread that
// still exists. Browser verification caught the route rendering the
// second as the first (「对话不存在」 for a thread that was merely
// refused), so these tests pin the decision, not just the copy.

import { describe, expect, it } from "vitest";
import { KnowledgeMcpFaultError } from "./api";
import { classifyThreadOpenFailure, threadEntryView } from "./threadEntry";

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
});
