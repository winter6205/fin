// T3 (ADR 0044): thread-detail token occupancy.
//
// ``GET /api/threads/{id}`` now carries an ``occupancy`` object
// derived from the SAME host figure the auto-compact gate computes
// (``OccupancyCompactor.occupancy`` over the checkpointed messages
// vs ``SESSION_TOKEN_BUDGET``). The wording and the neutral-state
// decision live here so they are unit-testable without a DOM;
// rendering is a 1-line use in ThreadPage.tsx. This NEVER touches
// ``statusStrip.ts`` — ADR 0033 keeps ``GET /api/status`` free of
// any occupancy / context-window figure.

import type { ThreadDetail } from "./types";

export interface Occupancy {
  estimated_tokens: number;
  budget_tokens: number;
  ratio: number;
}

/**
 * Defensive read of the thread-detail occupancy field.
 *
 * Returns ``null`` for a thread with no checkpoint (never sent
 * on) or a malformed payload — the UI renders no indicator rather
 * than a fabricated 0%.
 */
export function parseThreadOccupancy(detail: ThreadDetail): Occupancy | null {
  const raw: unknown = detail.occupancy;
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const tokens = obj.estimated_tokens;
  const budget = obj.budget_tokens;
  const ratio = obj.ratio;
  if (
    typeof tokens !== "number" ||
    !Number.isFinite(tokens) ||
    tokens < 0 ||
    typeof budget !== "number" ||
    !Number.isFinite(budget) ||
    budget <= 0 ||
    typeof ratio !== "number" ||
    !Number.isFinite(ratio) ||
    ratio < 0
  ) {
    return null;
  }
  return { estimated_tokens: tokens, budget_tokens: budget, ratio: ratio };
}

/** Percentage text, e.g. ``12%`` — floored so the indicator never
 * shows 100% before the host actually clamps the ratio there. */
export function occupancyPercent(occupancy: Occupancy): string {
  return `${Math.floor(occupancy.ratio * 100)}%`;
}
