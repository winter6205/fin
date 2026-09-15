// What ``/t/{thread_id}`` should render. Plain data in, plain enum
// out, so the decision is unit-testable without a DOM (the web suite
// runs in a node environment — see ``productRoutes.ts`` /
// ``knowledgeMcp.ts`` for the same convention).
//
// WHY this exists at all: two different refusals reach this route with
// two different truths. An unknown id is a 404 — the thread is gone.
// The 知识 MCP gate is a 409 on a thread that still exists and merely
// cannot be opened right now (ADR 0051). Rendering the second as the
// first tells the operator their thread was deleted.

import { KnowledgeMcpFaultError } from "./api";

/** Why an ``openThread`` failed, at the granularity the route needs. */
export type ThreadEntryFault = "gated" | "missing";

/** Classify an ``openThread`` rejection. Only the 知识 MCP gate is
 * special-cased; every other failure (404, malformed body, network)
 * keeps the pre-existing not-found presentation. */
export function classifyThreadOpenFailure(error: unknown): ThreadEntryFault {
  return error instanceof KnowledgeMcpFaultError ? "gated" : "missing";
}

export type ThreadEntryView = "loading" | "gated" | "missing" | "thread";

/**
 * The render decision for the thread route.
 *
 * Order matters and is the point: ``gated`` is checked BEFORE the
 * loaded-thread check, because a refusal leaves ``activeThread`` empty
 * (and may leave a PREVIOUS thread's value in place) — either way the
 * not-found branch would claim the wrong thing.
 */
export function threadEntryView(input: {
  loading: boolean;
  fault: ThreadEntryFault | null;
  /** Whether ``activeThread`` currently holds THIS thread id. */
  threadLoaded: boolean;
}): ThreadEntryView {
  if (input.loading) return "loading";
  if (input.fault === "gated") return "gated";
  if (!input.threadLoaded) return "missing";
  return "thread";
}
