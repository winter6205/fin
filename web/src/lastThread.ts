// Last-opened-thread marker (specs/workspace.md: "Returning visit
// restores last /t/{id} when that id still exists").
//
// The decision is a pure function so its boundaries — list not
// fetched yet, marker pointing at a deleted thread, operator who
// deliberately chose the knowledge base OR the monitor surface this
// session — are testable without a DOM. The localStorage wrappers
// around it degrade to "no restore" when storage is unavailable.

export const LAST_THREAD_KEY = "fin.lastThread";

export interface LastThreadRestoreInput {
  /** False until the FIRST thread-list fetch resolved. */
  threadsLoaded: boolean;
  lastThreadId: string | null;
  threadIds: readonly string[];
  /**
   * T1 (specs/workbench-chrome.md): operator visited ``/kb`` this
   * session. The chrome's nav click is the single thing the gate
   * keys off, so a returning visit must not bounce the operator
   * off the surface they picked.
   */
  kbGateActive: boolean;
  /**
   * T2 (specs/workbench-chrome.md): operator visited ``/monitor``
   * this session. Same role as ``kbGateActive`` — a returning
   * visit must not bounce the operator off the monitor surface
   * they picked. Set independently so each chrome nav click is the
   * single thing that gate keys off.
   */
  monitorGateActive: boolean;
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
  if (input.kbGateActive || input.monitorGateActive) {
    return { action: "stay" };
  }
  if (!input.lastThreadId) return { action: "stay" };
  if (input.threadIds.includes(input.lastThreadId)) {
    return { action: "restore", threadId: input.lastThreadId };
  }
  // Marker for a thread deleted elsewhere / an unknown id: drop it
  // so later visits stop re-checking a dead id.
  return { action: "forget" };
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
