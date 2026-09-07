// Workbench chrome — two-entry sidebar nav (specs/workbench-sidebar-rail.md).
// T1 retires the 对话 chrome entry: 知识库 and 监控 are the only chrome
// rows now; research threads live in a 会话 disclosure below. The
// helper pins labels so the chrome never silently drifts back to
// "Corpus 首页" / English / a 对话 row, and pins active-route mapping
// so / and /t/{id} light no chrome row (a thread page is a list item,
// not a chrome entry). The mark helper decides whether the 监控 entry
// carries a non-text-only degraded glyph, derived from a view that
// only the store can produce.

import { describe, expect, it } from "vitest";
import { NAV_ENTRIES, activeNavKey, monitorNavMark } from "./navChrome";

describe("NAV_ENTRIES", () => {
  it("renders exactly two entries, in spec order", () => {
    expect(NAV_ENTRIES.map((e) => e.label)).toEqual(["知识库", "监控"]);
  });

  it("points each entry at its spec route", () => {
    expect(NAV_ENTRIES.map((e) => e.route)).toEqual(["/kb", "/monitor"]);
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

  it("does not expose a 对话 chrome entry (retired in T1)", () => {
    // The 对话 row was the overview the operator rejected; sessions
    // live in the 会话 disclosure under the chrome, not in the chrome.
    expect(NAV_ENTRIES.map((e) => e.label)).not.toContain("对话");
    expect(NAV_ENTRIES.map((e) => e.route)).not.toContain("/");
  });
});

describe("activeNavKey", () => {
  it("does not light any entry on the home route", () => {
    // / is no chrome — it is either a placeholder composer or a
    // last-thread restore, not a 对话总览.
    expect(activeNavKey("/")).toBeNull();
  });

  it("does not light any entry on a thread route", () => {
    // A /t/{id} page is a research thread, not a chrome entry — the
    // 会话 list item lights up, not the chrome.
    expect(activeNavKey("/t/abc-123")).toBeNull();
    expect(activeNavKey("/t/some-thread-id-with-many-segments")).toBeNull();
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

  it("does not mis-light /t without a slash as any entry", () => {
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
