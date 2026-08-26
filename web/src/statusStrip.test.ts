// T10 (ADR 0033): the Sidebar status strip. The strip is the
// operator's only readable signal that retrieve is unreachable, so
// the invariant under test is "never blank": every view state — even
// a malformed payload or a status route that never answered —
// produces non-empty copy for every line.

import { describe, expect, it } from "vitest";
import {
  describeStatusStrip,
  parseWorkspaceStatus,
  workspaceStatusView,
} from "./statusStrip";
import type { WorkspaceStatus } from "./types";

const REACHABLE: WorkspaceStatus = {
  retrieve_reachability: "reachable",
  retrieve_adapter_kind: "mcp",
  model_identity: "fin/MiniMax-M3",
  corpus_filing_count: 12,
};

const UNREACHABLE: WorkspaceStatus = {
  ...REACHABLE,
  retrieve_reachability: "unreachable",
};

describe("parseWorkspaceStatus", () => {
  it("accepts the four ADR 0033 fields", () => {
    expect(parseWorkspaceStatus({ ...REACHABLE })).toEqual(REACHABLE);
  });

  it("ignores extra keys rather than failing the whole payload", () => {
    const parsed = parseWorkspaceStatus({ ...REACHABLE, future_field: 1 });
    expect(parsed).toEqual(REACHABLE);
  });

  it("rejects a non-object payload", () => {
    expect(parseWorkspaceStatus(null)).toBeNull();
    expect(parseWorkspaceStatus("reachable")).toBeNull();
    expect(parseWorkspaceStatus(undefined)).toBeNull();
  });

  it("rejects a payload missing a field", () => {
    const { corpus_filing_count: _dropped, ...partial } = REACHABLE;
    expect(parseWorkspaceStatus(partial)).toBeNull();
  });

  it("rejects an unknown reachability or adapter value", () => {
    // Never coerced to "reachable": a strip that claims a healthy
    // port it cannot vouch for is worse than one that says unknown.
    expect(
      parseWorkspaceStatus({ ...REACHABLE, retrieve_reachability: "maybe" }),
    ).toBeNull();
    expect(
      parseWorkspaceStatus({ ...REACHABLE, retrieve_adapter_kind: "bm25" }),
    ).toBeNull();
  });
});

describe("workspaceStatusView", () => {
  it("is loading before the first response lands", () => {
    expect(workspaceStatusView(null, false)).toEqual({ state: "loading" });
  });

  it("is unavailable when the load finished with no payload", () => {
    expect(workspaceStatusView(null, true)).toEqual({ state: "unavailable" });
  });

  it("is ready once a parsed payload is in the store", () => {
    expect(workspaceStatusView(REACHABLE, true)).toEqual({
      state: "ready",
      status: REACHABLE,
    });
  });
});

describe("describeStatusStrip", () => {
  const views = [
    { state: "loading" } as const,
    { state: "unavailable" } as const,
    { state: "ready", status: REACHABLE } as const,
    { state: "ready", status: UNREACHABLE } as const,
    {
      state: "ready",
      status: { ...REACHABLE, retrieve_adapter_kind: "fake" },
    } as const,
  ];

  it("never renders a blank line in any view state", () => {
    for (const view of views) {
      const copy = describeStatusStrip(view);
      for (const [key, value] of Object.entries(copy)) {
        expect(value, `${view.state}.${key}`).not.toBe("");
      }
    }
  });

  it("reads as healthy on a reachable product boot", () => {
    const copy = describeStatusStrip({ state: "ready", status: REACHABLE });
    expect(copy.retrieveTone).toBe("ok");
    expect(copy.retrieveLabel).toContain("可用");
    expect(copy.adapterLabel).toContain("iknow-memory");
    expect(copy.modelLabel).toContain("fin/MiniMax-M3");
    expect(copy.corpusLabel).toContain("12");
  });

  it("degrades readably — and actionably — when retrieve is unreachable", () => {
    const copy = describeStatusStrip({ state: "ready", status: UNREACHABLE });
    expect(copy.retrieveTone).toBe("degraded");
    expect(copy.retrieveLabel).toContain("不可用");
    // An ordinary operator has to learn two things: what they lose
    // (evidence-backed answers) and what to do next.
    expect(copy.retrieveDetail).toContain("引用");
    expect(copy.retrieveDetail).toContain("iknow-memory");
    expect(copy.retrieveDetail).toContain("FIN_RETRIEVE_ADAPTER=fake");
    // The other three fields still carry their real values.
    expect(copy.modelLabel).toContain("fin/MiniMax-M3");
    expect(copy.corpusLabel).toContain("12");
  });

  it("marks a fake-adapter boot as reachable but corpus-less", () => {
    const copy = describeStatusStrip({
      state: "ready",
      status: { ...REACHABLE, retrieve_adapter_kind: "fake" },
    });
    expect(copy.retrieveTone).toBe("ok");
    expect(copy.adapterLabel).toContain("fake");
    expect(copy.adapterLabel).not.toContain("iknow-memory");
  });

  it("says zero filings instead of hiding the count", () => {
    const copy = describeStatusStrip({
      state: "ready",
      status: { ...REACHABLE, corpus_filing_count: 0 },
    });
    expect(copy.corpusLabel).toContain("0");
  });

  it("degrades when the status route itself is unavailable", () => {
    const copy = describeStatusStrip({ state: "unavailable" });
    expect(copy.retrieveTone).toBe("degraded");
    expect(copy.retrieveLabel).toContain("未知");
  });

  it("does not accuse the port while the first fetch is in flight", () => {
    const copy = describeStatusStrip({ state: "loading" });
    expect(copy.retrieveTone).toBe("ok");
    expect(copy.retrieveLabel).not.toContain("不可用");
  });
});
