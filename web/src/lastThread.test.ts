// SC16 (ii): a returning visit restores the last /t/{thread_id}
// while that thread still exists, and never bounces the operator
// into a dead or unknown id. The retired /kb / /monitor session
// gates are gone (spec workbench-retrieve-product-seam): the
// decision now depends only on load state + the marker + the list.

import { describe, expect, it } from "vitest";
import {
  decideLastThreadRestore,
  decideStartupRestore,
  demoModeStateForOutcome,
  reduceStartupOwner,
  type StartupOwnerFacts,
  type StartupOwnerState,
  type StartupRestoreInput,
} from "./lastThread";

const LAST = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";

describe("decideLastThreadRestore", () => {
  it("restores the last thread when it is still in the list", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [OTHER, LAST],
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
      }),
    ).toEqual({ action: "wait" });
  });

  it("forgets a marker whose thread was deleted", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [OTHER],
      }),
    ).toEqual({ action: "forget" });
  });

  it("forgets a marker when the operator has no threads left", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: LAST,
        threadIds: [],
      }),
    ).toEqual({ action: "forget" });
  });

  it("stays home on a first visit with no marker", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: null,
        threadIds: [OTHER],
      }),
    ).toEqual({ action: "stay" });
  });

  it("treats an empty marker string as no marker", () => {
    expect(
      decideLastThreadRestore({
        threadsLoaded: true,
        lastThreadId: "",
        threadIds: [LAST],
      }),
    ).toEqual({ action: "stay" });
  });
});

// T3 (plans/2026-10-20-qa-design-correction.md task 3): the STARTUP
// decision is not the mount-local restore the homepage used to run.
// The one-shot opportunity belongs to an application-lifetime owner,
// so the decision below answers "may THIS application start restore
// the last thread?" — not "may this mount restore it?".
//
// The table it encodes:
//   demo disabled + cold "/" + valid marker  -> restore (once)
//   demo pending                            -> wait for the status
//   demo enabled                            -> stay (never in demo)
//   demo unavailable                        -> stay (no speculative restore)
//   initial deep link                       -> stay (not eligible)
//   opportunity already consumed            -> stay (Back stays home)
//   marker missing                          -> stay
//   marker stale                            -> forget (unchanged handling)
function startup(over: Partial<StartupRestoreInput> = {}) {
  return decideStartupRestore({
    opportunity: "available",
    demoMode: "disabled",
    startedAtHome: true,
    threadsLoaded: true,
    lastThreadId: LAST,
    threadIds: [OTHER, LAST],
    ...over,
  });
}

describe("decideStartupRestore", () => {
  it("restores the marked thread on a non-demo cold start at /", () => {
    expect(startup()).toEqual({ action: "restore", threadId: LAST });
  });

  it("waits while the demo status is unresolved", () => {
    // The mode is not known yet, so the decision is not made: a status
    // that arrives later must still be able to say "demo" and stop the
    // restore.
    expect(startup({ demoMode: "pending" })).toEqual({ action: "wait" });
  });

  it("waits while the thread list is still loading", () => {
    // A marker must not be judged against a list that has not arrived.
    expect(startup({ threadsLoaded: false })).toEqual({ action: "wait" });
  });

  it("waits for the mode before it judges a stale marker", () => {
    // The stale-marker handling must not run against an unresolved
    // mode: the marker belongs to the operator, not to this boot.
    expect(
      startup({ demoMode: "pending", threadIds: [OTHER] }),
    ).toEqual({ action: "wait" });
  });

  it("never restores in demo mode", () => {
    expect(startup({ demoMode: "enabled" })).toEqual({ action: "stay" });
  });

  it("keeps a demo marker instead of forgetting it", () => {
    // The demo boot SKIPS the opportunity. It must not destroy the
    // operator's marker for the next non-demo start.
    expect(startup({ demoMode: "enabled", threadIds: [OTHER] })).toEqual({
      action: "stay",
    });
  });

  it("never restores when the demo status is unavailable", () => {
    // "Disabled" and "unavailable" are different facts: a status the
    // server never answered cannot license a speculative restore.
    expect(startup({ demoMode: "unavailable" })).toEqual({ action: "stay" });
  });

  it("does not restore on an initial deep link", () => {
    expect(startup({ startedAtHome: false })).toEqual({ action: "stay" });
  });

  it("stays home once the opportunity is consumed", () => {
    // The consumed bit is the whole point: browser Back to "/" and
    // every later visit must stay home, however valid the marker is.
    expect(startup({ opportunity: "consumed" })).toEqual({ action: "stay" });
  });

  it("stays home with no marker", () => {
    expect(startup({ lastThreadId: null })).toEqual({ action: "stay" });
  });

  it("forgets a stale marker", () => {
    // The existing stale-marker handling, unchanged.
    expect(startup({ threadIds: [OTHER] })).toEqual({ action: "forget" });
  });

  it("forgets a marker whose thread was deleted", () => {
    expect(startup({ threadIds: [] })).toEqual({ action: "forget" });
  });
});

// T3 (plans/2026-10-20-qa-design-correction.md task 3): the startup
// OWNER's state machine. The opportunity, the navigation that consumes
// it and the demo status that gates it lived in hook refs, so the rules
// could not be exercised behaviourally — in demo mode the decision
// answers "stay" whatever the owner does, so a deleted consuming effect
// left every check green. The reducer is that machine, as a pure
// function: the hook drives it, these tests read it.
//
// The table:
//   navigation during the wait   -> the opportunity is consumed
//   a late status after that      -> "stay" (no late redirect)
//   a failed status fetch         -> "stay" (never deferred)
//   a resolved non-demo status    -> "restore" once, then "stay"
//   a demo boot                   -> "stay", the opportunity consumed
function ownerFacts(over: Partial<StartupOwnerFacts> = {}): StartupOwnerFacts {
  return {
    startedAtHome: true,
    threadsLoaded: true,
    lastThreadId: LAST,
    threadIds: [OTHER, LAST],
    ...over,
  };
}

const OWNER: StartupOwnerState = { opportunity: "available", demoMode: "pending" };

describe("reduceStartupOwner", () => {
  it("consumes the opportunity when the operator navigates during the wait", () => {
    // The status has not answered yet, so the decision is "wait" — and
    // the navigation that happens meanwhile must burn the opportunity,
    // so the status that arrives later cannot redirect the operator.
    const waiting = reduceStartupOwner(OWNER, { type: "checked" }, ownerFacts());
    expect(waiting.decision).toEqual({ action: "wait" });
    expect(waiting.state.opportunity).toBe("available");

    const navigated = reduceStartupOwner(
      waiting.state,
      { type: "navigated" },
      ownerFacts(),
    );
    expect(navigated.state.opportunity).toBe("consumed");
    expect(navigated.decision).toEqual({ action: "stay" });
  });

  it("stays home when the status resolves after that navigation", () => {
    const navigated = reduceStartupOwner(
      OWNER,
      { type: "navigated" },
      ownerFacts(),
    );
    const late = reduceStartupOwner(
      navigated.state,
      { type: "demo_status", outcome: { kind: "resolved", enabled: false } },
      ownerFacts(),
    );
    expect(late.decision).toEqual({ action: "stay" });
    expect(late.state.opportunity).toBe("consumed");
  });

  it("stays home on a failed status fetch, without deferring", () => {
    // A server that never answered may not license a speculative
    // restore, and the decision must not wait for a status that will
    // never arrive.
    const failed = reduceStartupOwner(
      OWNER,
      { type: "demo_status", outcome: { kind: "failed" } },
      ownerFacts(),
    );
    expect(failed.state.demoMode).toBe("unavailable");
    expect(failed.decision).toEqual({ action: "stay" });
    expect(failed.decision.action).not.toBe("wait");
  });

  it("restores once on a resolved non-demo status, and stays home after it", () => {
    const resolved = reduceStartupOwner(
      OWNER,
      { type: "demo_status", outcome: { kind: "resolved", enabled: false } },
      ownerFacts(),
    );
    expect(resolved.decision).toEqual({ action: "restore", threadId: LAST });
    expect(resolved.state.opportunity).toBe("consumed");

    const again = reduceStartupOwner(
      resolved.state,
      { type: "checked" },
      ownerFacts(),
    );
    expect(again.decision).toEqual({ action: "stay" });
  });

  it("skips the opportunity on a demo boot without touching the marker", () => {
    const demo = reduceStartupOwner(
      OWNER,
      { type: "demo_status", outcome: { kind: "resolved", enabled: true } },
      ownerFacts(),
    );
    expect(demo.decision).toEqual({ action: "stay" });
    expect(demo.state.opportunity).toBe("consumed");
  });

  it("keeps the opportunity open while the thread list has not answered", () => {
    const waiting = reduceStartupOwner(
      OWNER,
      { type: "checked" },
      ownerFacts({ threadsLoaded: false }),
    );
    expect(waiting.decision).toEqual({ action: "wait" });
    expect(waiting.state.opportunity).toBe("available");
  });

  it("stays home on an initial deep link", () => {
    const deep = reduceStartupOwner(
      OWNER,
      { type: "demo_status", outcome: { kind: "resolved", enabled: false } },
      ownerFacts({ startedAtHome: false }),
    );
    expect(deep.decision).toEqual({ action: "stay" });
  });
});

describe("demoModeStateForOutcome", () => {
  it("reads the server's answer as enabled or disabled", () => {
    expect(demoModeStateForOutcome({ kind: "resolved", enabled: true })).toBe(
      "enabled",
    );
    expect(demoModeStateForOutcome({ kind: "resolved", enabled: false })).toBe(
      "disabled",
    );
  });

  it("reads a failed fetch as unavailable, not as disabled", () => {
    // The server's silence is a different fact from its "disabled"
    // answer: only the answer licenses an ordinary boot.
    expect(demoModeStateForOutcome({ kind: "failed" })).toBe("unavailable");
  });
});
