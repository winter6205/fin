// Last-opened-thread marker (specs/workspace.md: "Returning visit
// restores last /t/{id} when that id still exists").
//
// The decision is a pure function so its boundaries — list not
// fetched yet, marker pointing at a deleted thread, first visit with
// no marker — are testable without a DOM. The localStorage wrappers
// around it degrade to "no restore" when storage is unavailable.
//
// The retired ``fin.kb`` / ``fin.monitor`` session gates left with the
// kb/monitor chrome (spec workbench-retrieve-product-seam): there is
// no chrome surface for a returning visit to stay on.

export const LAST_THREAD_KEY = "fin.lastThread";

export interface LastThreadRestoreInput {
  /** False until the FIRST thread-list fetch resolved. */
  threadsLoaded: boolean;
  lastThreadId: string | null;
  threadIds: readonly string[];
}

export type LastThreadRestoreDecision =
  | { action: "wait" }
  | { action: "stay" }
  | { action: "restore"; threadId: string }
  | { action: "forget" };

export function decideLastThreadRestore(
  input: LastThreadRestoreInput,
): LastThreadRestoreDecision {
  // "Loaded and genuinely empty" must not look like "not fetched
  // yet", or the one-shot restore burns itself on a valid marker.
  if (!input.threadsLoaded) return { action: "wait" };
  if (!input.lastThreadId) return { action: "stay" };
  if (input.threadIds.includes(input.lastThreadId)) {
    return { action: "restore", threadId: input.lastThreadId };
  }
  // Marker for a thread deleted elsewhere / an unknown id: drop it
  // so later visits stop re-checking a dead id.
  return { action: "forget" };
}

// ----- the STARTUP decision (T3) -----
//
// The restore above answers "may this MOUNT restore the last thread?".
// That question is wrong for a returning visit: a HomePage mount dies
// with the route, so every later visit to "/" re-armed the restore and
// browser Back bounced the operator straight back into the thread.
//
// The decision below answers the application-lifetime question instead:
// "may THIS APPLICATION START restore the last thread?". Its owner
// lives at the application root, consumes the opportunity once, and
// keeps the consumed bit in memory only — never in browser storage —
// so a real reload grants a fresh opportunity (specs/demo-seed.md
// "Restore ownership").

/**
 * The demo-mode fact, as three states and not two.
 *
 * "disabled" is the server's answer; "unavailable" is the server's
 * SILENCE. They are different facts: a boot that never learned whether
 * it is a demo may not restore speculatively, and it may not pretend
 * the operator is in an ordinary session either. "pending" is the
 * unresolved state — the decision waits for it rather than guessing.
 */
export type DemoModeState = "pending" | "enabled" | "disabled" | "unavailable";

/** The one-shot startup opportunity. */
export type StartupOpportunity = "available" | "consumed";

export interface StartupRestoreInput {
  /** Whether the one-shot opportunity is still open. */
  opportunity: StartupOpportunity;
  /** The demo-mode fact (three states). */
  demoMode: DemoModeState;
  /**
   * Whether the application STARTED at the eligible home route "/".
   * An initial deep link ("/t/{id}") is not eligible: the operator
   * asked for a thread.
   */
  startedAtHome: boolean;
  /** False until the FIRST thread-list fetch resolved. */
  threadsLoaded: boolean;
  lastThreadId: string | null;
  threadIds: readonly string[];
}

/**
 * The startup restore decision.
 *
 * Order is the contract:
 *   1. a consumed opportunity never restores again (browser Back and
 *      every later visit to "/" stay home);
 *   2. an initial deep link is not eligible;
 *   3. an unresolved demo status is WAITED for — the decision is not
 *      made on a half-known mode;
 *   4. demo mode never restores, and its boot SKIPS the opportunity
 *      without touching the operator's marker;
 *   5. an unavailable demo status never restores either — a silent
 *      server may not license a speculative restore, and the decision
 *      is bounded (it is not deferred until the status arrives);
 *   6. only a non-demo start reaches the marker, where the existing
 *      stale-marker handling applies unchanged.
 */
export function decideStartupRestore(
  input: StartupRestoreInput,
): LastThreadRestoreDecision {
  if (input.opportunity === "consumed") return { action: "stay" };
  if (!input.startedAtHome) return { action: "stay" };
  if (input.demoMode === "pending") return { action: "wait" };
  if (input.demoMode === "enabled") return { action: "stay" };
  if (input.demoMode === "unavailable") return { action: "stay" };
  return decideLastThreadRestore({
    threadsLoaded: input.threadsLoaded,
    lastThreadId: input.lastThreadId,
    threadIds: input.threadIds,
  });
}

// ----- the startup OWNER's state machine (T3) -----
//
// The decision above is pure, but the OWNER is not: the one-shot
// opportunity, the navigation that consumes it and the demo status that
// gates it live in hook refs, so the rules could not be exercised
// behaviourally — in demo mode the decision says "stay" whatever the
// owner does, so deleting the consuming effect left every check green.
//
// The reducer below is that state machine, as a pure function: it owns
// the opportunity and the demo fact, and it answers with the owner's
// decision. The hook drives it; the tests read it.

/**
 * The demo-status fetch outcome, as the owner sees it: the server's
 * answer, or its silence.
 */
export type DemoStatusOutcome =
  | { kind: "resolved"; enabled: boolean }
  | { kind: "failed" };

/**
 * The demo-mode fact a fetch outcome maps to.
 *
 * A failed fetch is the server's SILENCE ("unavailable"), never its
 * "disabled" answer: a server that never answered may not license a
 * speculative restore, and the decision must not stay deferred waiting
 * for a status that will never arrive.
 */
export function demoModeStateForOutcome(
  outcome: DemoStatusOutcome,
): DemoModeState {
  if (outcome.kind === "failed") return "unavailable";
  return outcome.enabled ? "enabled" : "disabled";
}

/** The startup owner's state: the one-shot opportunity and the demo fact. */
export interface StartupOwnerState {
  /** The one-shot opportunity. */
  opportunity: StartupOpportunity;
  /** The demo-mode fact, as it is known so far. */
  demoMode: DemoModeState;
}

/** What happened to the owner. */
export type StartupOwnerEvent =
  /** The operator navigated: a pending opportunity is consumed. */
  | { type: "navigated" }
  /** The demo status resolved — or never answered. */
  | { type: "demo_status"; outcome: DemoStatusOutcome }
  /** The owner re-reads its facts (the thread list arrived, …). */
  | { type: "checked" };

/** The restore facts the owner reads when it decides. */
export interface StartupOwnerFacts {
  /** Whether the application STARTED at the eligible home route "/". */
  startedAtHome: boolean;
  /** False until the FIRST thread-list fetch resolved. */
  threadsLoaded: boolean;
  lastThreadId: string | null;
  threadIds: readonly string[];
}

export interface StartupOwnerResult {
  /** The owner's state after the event. */
  state: StartupOwnerState;
  /** What the owner does with that state. */
  decision: LastThreadRestoreDecision;
}

/**
 * The startup owner's state machine.
 *
 * The three inputs are the opportunity, the navigation and the demo
 * status; the output is the owner's next state and its decision:
 *
 * - a NAVIGATION consumes a pending opportunity, so a late demo status
 *   or a late thread list can no longer redirect the operator;
 * - a demo status maps to the demo fact — a failed fetch to
 *   "unavailable", which decides "stay" instead of deferring;
 * - every decision but "wait" CONSUMES the opportunity, so the restore
 *   is made exactly once per application start and a later event can
 *   only ever answer "stay".
 */
export function reduceStartupOwner(
  state: StartupOwnerState,
  event: StartupOwnerEvent,
  facts: StartupOwnerFacts,
): StartupOwnerResult {
  let next = state;
  if (event.type === "navigated") {
    next = { ...state, opportunity: "consumed" };
  } else if (event.type === "demo_status") {
    next = { ...state, demoMode: demoModeStateForOutcome(event.outcome) };
  }
  const decision = decideStartupRestore({
    opportunity: next.opportunity,
    demoMode: next.demoMode,
    startedAtHome: facts.startedAtHome,
    threadsLoaded: facts.threadsLoaded,
    lastThreadId: facts.lastThreadId,
    threadIds: facts.threadIds,
  });
  return {
    state:
      decision.action === "wait" ? next : { ...next, opportunity: "consumed" },
    decision,
  };
}

export function readLastThread(): string | null {
  try {
    return window.localStorage.getItem(LAST_THREAD_KEY);
  } catch {
    return null; /* storage unavailable - no restore. */
  }
}

export function rememberLastThread(threadId: string): void {
  try {
    window.localStorage.setItem(LAST_THREAD_KEY, threadId);
  } catch {
    /* storage unavailable - no restore on next visit. */
  }
}

export function forgetLastThread(): void {
  try {
    window.localStorage.removeItem(LAST_THREAD_KEY);
  } catch {
    /* storage unavailable - nothing to clear. */
  }
}

/** Drop the marker only when it still points at ``threadId``. */
export function forgetLastThreadIf(threadId: string): void {
  if (readLastThread() === threadId) forgetLastThread();
}
