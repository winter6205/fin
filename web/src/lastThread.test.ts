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
      }),
    ).toEqual({ action: "stay" });
  });
});
