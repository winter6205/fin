// Workbench chrome — three-entry sidebar nav. Pin the labels and
// active-route mapping so the chrome never silently drifts back to
// "Corpus 首页" / English labels. The mark helper decides whether the
// 监控 entry carries a non-text-only degraded glyph, derived from a
// view that only the store can produce.

import { describe, expect, it } from "vitest";
import { NAV_ENTRIES, activeNavKey, monitorNavMark } from "./navChrome";

describe("NAV_ENTRIES", () => {
  it("renders exactly three entries, in spec order", () => {
    expect(NAV_ENTRIES.map((e) => e.label)).toEqual(["对话", "知识库", "监控"]);
  });

  it("points each entry at its spec route", () => {
    expect(NAV_ENTRIES.map((e) => e.route)).toEqual(["/", "/kb", "/monitor"]);
  });

  it("never carries an English word in any label", () => {
    // Spec language: English labels are forbidden in the chrome.
    for (const entry of NAV_ENTRIES) {
      expect(/^[A-Za-z\s]+$/.test(entry.label)).toBe(false);
    }
  });

  it("never carries the retired 'Corpus' word in any label", () => {
    for (const entry of NAV_ENTRIES) {
      expect(entry.label).not.toContain("Corpus");
      expect(entry.label).not.toContain("关注");
    }
  });
});

describe("activeNavKey", () => {
  it("lights 对话 on the home route", () => {
    expect(activeNavKey("/")).toBe("chat");
  });

  it("lights 对话 on a thread route", () => {
    expect(activeNavKey("/t/abc-123")).toBe("chat");
    expect(activeNavKey("/t/some-thread-id-with-many-segments")).toBe("chat");
  });

  it("lights 知识库 on /kb", () => {
    expect(activeNavKey("/kb")).toBe("kb");
  });

  it("lights 监控 on /monitor", () => {
    expect(activeNavKey("/monitor")).toBe("monitor");
  });

  it("does not light any entry for an unknown route", () => {
    expect(activeNavKey("/nope")).toBeNull();
    expect(activeNavKey("/kb/something")).toBeNull();
    expect(activeNavKey("/monitor/anything")).toBeNull();
  });

  it("does not mis-light /t as 对话 without a slash", () => {
    // ``/t`` alone (no thread id) is not a valid thread route.
    expect(activeNavKey("/t")).toBeNull();
  });
});

describe("monitorNavMark", () => {
  it("does not mark while the status route is still loading", () => {
    expect(monitorNavMark({ state: "loading" })).toBeNull();
  });

  it("does not mark when the route itself is unavailable (route-level failure is a /monitor problem, not a sidebar mark)", () => {
    // The unavailable view shows degraded copy on /monitor itself.
    // Painting the sidebar red for a status-fetch failure is the
    // exact "always-on sidebar diagnose" the spec retired.
    expect(monitorNavMark({ state: "unavailable" })).toBeNull();
  });

  it("does not mark on a reachable MCP boot", () => {
    expect(
      monitorNavMark({
        state: "ready",
        status: {
          retrieve_reachability: "reachable",
          retrieve_adapter_kind: "mcp",
          model_identity: "fin/MiniMax-M3",
          corpus_filing_count: 12,
        },
      }),
    ).toBeNull();
  });

  it("does not mark on a reachable fake boot", () => {
    expect(
      monitorNavMark({
        state: "ready",
        status: {
          retrieve_reachability: "reachable",
          retrieve_adapter_kind: "fake",
          model_identity: "fin/MiniMax-M3",
          corpus_filing_count: 12,
        },
      }),
    ).toBeNull();
  });

  it("returns a degraded mark on a reachable-mcp boot that reports unreachable", () => {
    const mark = monitorNavMark({
      state: "ready",
      status: {
        retrieve_reachability: "unreachable",
        retrieve_adapter_kind: "mcp",
        model_identity: "fin/MiniMax-M3",
        corpus_filing_count: 12,
      },
    });
    expect(mark).not.toBeNull();
    // Non-text-only: a glyph + non-empty aria text. The operator
    // has to learn this without reading the entry label.
    expect(mark?.glyph.length).toBeGreaterThan(0);
    expect(mark?.aria.length).toBeGreaterThan(0);
  });

  it("returns a degraded mark when the port is unreachable regardless of adapter kind", () => {
    // A fake port that reports unreachable means the local placeholder
    // is the live surface too — still worth marking.
    const mark = monitorNavMark({
      state: "ready",
      status: {
        retrieve_reachability: "unreachable",
        retrieve_adapter_kind: "fake",
        model_identity: "fin/MiniMax-M3",
        corpus_filing_count: 12,
      },
    });
    expect(mark).not.toBeNull();
  });
});
