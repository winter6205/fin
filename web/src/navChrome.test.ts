// Workbench chrome — three-entry sidebar nav. Pin the labels and
// active-route mapping so the chrome never silently drifts back to
// "Corpus 首页" / English labels.

import { describe, expect, it } from "vitest";
import { NAV_ENTRIES, activeNavKey } from "./navChrome";

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
