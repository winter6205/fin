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
