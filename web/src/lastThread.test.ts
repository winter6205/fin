// SC16 (ii): a returning visit restores the last /t/{thread_id}
// while that thread still exists, and never bounces the operator
// into a dead or unknown id.

import { describe, expect, it } from "vitest";
import { decideLastThreadRestore } from "./lastThread";

const LAST = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";

describe("decideLastThreadRestore", () => {
  it("restores the last thread when it is still in the list", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [OTHER, LAST],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "restore", threadId: LAST });
  });

  it("waits while the thread list is still loading", () => {
    // A marker must not be judged stale against a list that has
    // not arrived yet — the restore is one-shot per mount.
    expect(
      decideLastThreadRestore({
        threadsLoaded: false,
        lastThreadId: LAST,
        threadIds: [],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "wait" });
  });

  it("forgets a marker whose thread was deleted", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [OTHER],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "forget" });
  });

  it("forgets a marker when the operator has no threads left", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "forget" });
  });

  it("stays home on a first visit with no marker", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: null,
        threadIds: [OTHER],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "stay" });
  });

  it("stays home when the operator chose the corpus home", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [LAST],
        homeGateActive: true,
        kbGateActive: false,
      }),
    ).toEqual({ action: "stay" });
  });

  it("treats an empty marker string as no marker", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: "",
        threadIds: [LAST],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "stay" });
  });

  // T1 chrome + /kb (specs/workbench-chrome.md): visiting /kb this
  // session sets a gate that makes the restore bounce — same role
  // as the corpus-home gate. The decision is keyed off either gate,
  // and a kb visit alone is enough to keep the operator on /kb.
  it("stays home when the operator chose the knowledge base this session", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [LAST],
        homeGateActive: false,
        kbGateActive: true,
      }),
    ).toEqual({ action: "stay" });
  });

  it("treats the two session gates independently", () => {
    // kb set, home clear → stays.
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [LAST],
        homeGateActive: false,
        kbGateActive: true,
      }),
    ).toEqual({ action: "stay" });
    // Both set → still stays (either is enough).
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [LAST],
        homeGateActive: true,
        kbGateActive: true,
      }),
    ).toEqual({ action: "stay" });
    // Both clear → restores the marker.
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [LAST],
        homeGateActive: false,
        kbGateActive: false,
      }),
    ).toEqual({ action: "restore", threadId: LAST });
  });
});
