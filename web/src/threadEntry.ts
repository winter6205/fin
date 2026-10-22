// What ``/t/{thread_id}`` should render. Plain data in, plain enum
// out, so the decision is unit-testable without a DOM (the web suite
// runs in a node environment — see ``productRoutes.ts`` /
// ``knowledgeMcp.ts`` for the same convention).
//
// WHY this exists at all: three different refusals reach this route with
// three different truths. An unknown id is a 404 — the thread is gone.
// The 知识 MCP gate is a 409 on a thread that still exists and merely
// cannot be opened right now (ADR 0051). A pending-question 503 is a
// thread that exists but whose paused choice cannot be read THIS moment;
// the operator must retry, not be told it was deleted. Rendering either
// of the latter two as not-found is a wrong claim about a live thread.

import { KnowledgeMcpFaultError, PendingQuestionUnavailableError } from "./api";

/** Why an ``openThread`` failed, at the granularity the route needs. */
export type ThreadEntryFault = "gated" | "missing" | "pending_unavailable";

/** Classify an ``openThread`` rejection. The two named refusals (the
 * 知识 MCP gate and the pending-question 503) are special-cased; every
 * other failure (404, malformed body, network) keeps the pre-existing
 * not-found presentation. */
export function classifyThreadOpenFailure(error: unknown): ThreadEntryFault {
  if (error instanceof KnowledgeMcpFaultError) return "gated";
  if (error instanceof PendingQuestionUnavailableError)
    return "pending_unavailable";
  return "missing";
}

export type ThreadEntryView =
  | "loading"
  | "gated"
  | "missing"
  | "pending_unavailable"
  | "thread";

/**
 * Whether the pane still holds THIS thread's live answer.
 *
 * T2 (plans/2026-10-20-qa-design-correction.md): the live send owns the
 * pane while it is streaming on this thread, and a thread is only LIVE
 * while the pane still holds it. BOTH facts are needed: when the
 * operator LEFT the thread while its send was in flight, the live state
 * is gone — the entry read is then REQUIRED and the route shows its
 * loader over it.
 *
 * This is the ONE rule, shared by the three places that must not
 * disagree about it: the store's entry guard (whether a read may be
 * skipped), the route's entry decision (whether the loader or the live
 * answer is painted) and the thread page's re-sync (whether a second
 * read is added). It lives here, in the module that owns the entry-view
 * vocabulary, so all three can ask it without an import cycle.
 *
 * The facts it reads are the store's ``sendingThreadId`` (set
 * synchronously when a send starts, so a route mount right after
 * ``sendQuestion(...)`` already sees it) and the id of the thread the
 * pane currently holds.
 */
export function threadEntryIsLive(input: {
  threadId: string;
  sendingThreadId: string | null;
  activeThreadId: string | null;
}): boolean {
  return (
    input.sendingThreadId === input.threadId &&
    input.activeThreadId === input.threadId
  );
}

/**
 * The render decision for the thread route.
 *
 * Order matters and is the point: the two named refusals are checked
 * BEFORE the loaded-thread check, because a refusal leaves
 * ``activeThread`` empty (and may leave a PREVIOUS thread's value in
 * place) — either way the not-found branch would claim the wrong thing.
 */
export function threadEntryView(input: {
  loading: boolean;
  fault: ThreadEntryFault | null;
  /** Whether ``activeThread`` currently holds THIS thread id. */
  threadLoaded: boolean;
}): ThreadEntryView {
  if (input.loading) return "loading";
  if (input.fault === "gated") return "gated";
  if (input.fault === "pending_unavailable") return "pending_unavailable";
  if (!input.threadLoaded) return "missing";
  return "thread";
}
