// T3 (specs/workbench-chrome.md): the 监控 page owns the status copy
// once the Sidebar footer StatusStrip is gone. The invariant under
// test is "never blank" (SC7) — loading / unavailable / unreachable /
// reachable-fake / reachable-mcp each produce non-empty copy for every
// line, and the operator copy NEVER carries the model identity (model
// stays on research-thread surfaces; not a chrome line).

import { describe, expect, it } from "vitest";
import {
  describeMonitorStatus,
  parseWorkspaceStatus,
  workspaceStatusView,
} from "./statusStrip";
import type { WorkspaceStatus } from "./types";

const REACHABLE_MCP: WorkspaceStatus = {
  retrieve_reachability: "reachable",
  retrieve_adapter_kind: "mcp",
  model_identity: "fin/MiniMax-M3",
  corpus_filing_count: 12,
};

const REACHABLE_FAKE: WorkspaceStatus = {
  ...REACHABLE_MCP,
  retrieve_adapter_kind: "fake",
};

const UNREACHABLE: WorkspaceStatus = {
  ...REACHABLE_MCP,
  retrieve_reachability: "unreachable",
};

describe("parseWorkspaceStatus", () => {
  it("accepts the four ADR 0033 fields", () => {
    expect(parseWorkspaceStatus({ ...REACHABLE_MCP })).toEqual(REACHABLE_MCP);
  });

  it("ignores extra keys rather than failing the whole payload", () => {
    const parsed = parseWorkspaceStatus({ ...REACHABLE_MCP, future_field: 1 });
    expect(parsed).toEqual(REACHABLE_MCP);
  });

  it("rejects a non-object payload", () => {
    expect(parseWorkspaceStatus(null)).toBeNull();
    expect(parseWorkspaceStatus("reachable")).toBeNull();
    expect(parseWorkspaceStatus(undefined)).toBeNull();
  });

  it("rejects a payload missing a field", () => {
    const { corpus_filing_count: _dropped, ...partial } = REACHABLE_MCP;
    expect(parseWorkspaceStatus(partial)).toBeNull();
  });

  it("rejects an unknown reachability or adapter value", () => {
    // Never coerced to "reachable": a panel that claims a healthy
    // port it cannot vouch for is worse than one that says unknown.
    expect(
      parseWorkspaceStatus({
        ...REACHABLE_MCP,
        retrieve_reachability: "maybe",
      }),
    ).toBeNull();
    expect(
      parseWorkspaceStatus({ ...REACHABLE_MCP, retrieve_adapter_kind: "bm25" }),
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
    expect(workspaceStatusView(REACHABLE_MCP, true)).toEqual({
      state: "ready",
      status: REACHABLE_MCP,
    });
  });
});

describe("describeMonitorStatus", () => {
  const views = [
    { state: "loading" } as const,
    { state: "unavailable" } as const,
    { state: "ready", status: REACHABLE_MCP } as const,
    { state: "ready", status: REACHABLE_FAKE } as const,
    { state: "ready", status: UNREACHABLE } as const,
  ];

  it("never renders a blank line in any view state", () => {
    for (const view of views) {
      const copy = describeMonitorStatus(view);
      for (const [key, value] of Object.entries(copy)) {
        expect(value, `${view.state}.${key}`).not.toBe("");
      }
    }
  });

  it("produces a degraded tone on the three observable failure paths", () => {
    // SC7: unreachable + route unavailable + a healthy probe that
    // returns zero filings still must not read as broken chrome.
    const unreachable = describeMonitorStatus({
      state: "ready",
      status: UNREACHABLE,
    });
    expect(unreachable.tone).toBe("degraded");
    const unavailable = describeMonitorStatus({ state: "unavailable" });
    expect(unavailable.tone).toBe("degraded");
    // Loading and reachable boot both stay ok / quiet.
    expect(describeMonitorStatus({ state: "loading" }).tone).toBe("ok");
    expect(
      describeMonitorStatus({ state: "ready", status: REACHABLE_MCP }).tone,
    ).toBe("ok");
  });

  it("reads as healthy on a reachable MCP boot", () => {
    const copy = describeMonitorStatus({
      state: "ready",
      status: REACHABLE_MCP,
    });
    expect(copy.tone).toBe("ok");
    expect(copy.headline).toContain("可用");
    expect(copy.headline).toContain("知识库搜索");
    expect(copy.detail).toContain("12");
  });

  it("degrades readably — and actionably — when retrieve is unreachable", () => {
    const copy = describeMonitorStatus({ state: "ready", status: UNREACHABLE });
    expect(copy.tone).toBe("degraded");
    expect(copy.headline).toContain("连不上");
    expect(copy.detail).toContain("来源");
    expect(copy.detail).toContain("重连");
  });

  it("marks a fake-adapter boot as reachable but local-placeholder", () => {
    const copy = describeMonitorStatus({
      state: "ready",
      status: REACHABLE_FAKE,
    });
    expect(copy.tone).toBe("ok");
    expect(copy.detail).toContain("演示");
  });

  it("never uses engineer jargon the operator asked to stop seeing", () => {
    for (const view of views) {
      const blob = `${describeMonitorStatus(view).headline} ${describeMonitorStatus(view).detail}`;
      expect(blob).not.toMatch(
        /Corpus|catalog|iknow-memory|FIN_|工作区状态|fake|MCP/i,
      );
    }
  });

  it("says zero filings instead of hiding the count", () => {
    const copy = describeMonitorStatus({
      state: "ready",
      status: { ...REACHABLE_MCP, corpus_filing_count: 0 },
    });
    expect(copy.detail).toContain("0");
  });

  it("degrades when the status route itself is unavailable", () => {
    const copy = describeMonitorStatus({ state: "unavailable" });
    expect(copy.tone).toBe("degraded");
    expect(copy.headline).toContain("查不到");
  });

  it("does not accuse the port while the first fetch is in flight", () => {
    const copy = describeMonitorStatus({ state: "loading" });
    expect(copy.tone).toBe("ok");
    expect(copy.headline).not.toContain("不可用");
  });

  it("never surfaces model_identity on the 监控 copy (model is not a chrome line)", () => {
    // Model stays on research-thread surfaces (composer / turn row).
    // The 监控 page must not regress into a model card, even on a
    // healthy boot where the slug is non-empty.
    for (const view of views) {
      const copy = describeMonitorStatus(view);
      expect(copy.headline).not.toContain(REACHABLE_MCP.model_identity);
      expect(copy.detail).not.toContain(REACHABLE_MCP.model_identity);
    }
  });
});
