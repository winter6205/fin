// Choice-window presentation + wire parsing, kept pure so the
// payload boundary and the button gating are testable without a DOM
// (same split as comparison.ts).
//
// The window arrives on the ``choice_window`` SSE frame when the host
// suspends a run for an operator pick (search-mode-host T2, ADR 0062
// HITL). Rules shaping this module:
//
// 1. The wire is ``unknown``. ``parseChoiceWindow`` is the boundary —
//    a payload without a kind and a non-empty option list is skipped
//    exactly like a malformed frame, so a future schema renders
//    nothing instead of a half-drawn window.
// 2. The SPA never invents options or reorders them: the host payload
//    is the whole contract, and the pick just echoes an option id.

import type { ChoiceOption, ChoiceWindow, ThreadDetail, Turn } from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseOption(value: unknown): ChoiceOption | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== "string" || value.id.length === 0) return null;
  if (typeof value.label !== "string" || value.label.length === 0) return null;
  const option: ChoiceOption = { id: value.id, label: value.label };
  if (typeof value.opened === "boolean") option.opened = value.opened;
  return option;
}

/**
 * Narrow one wire payload to a renderable ``ChoiceWindow``, else null.
 * Minimal contract: string ``kind`` + at least one option with
 * ``{id,label}``. ``title`` / ``prompt`` / ``opened`` /
 * ``rejected_picks`` are additive; unknown option entries drop out
 * rather than failing the window.
 */
export function parseChoiceWindow(value: unknown): ChoiceWindow | null {
  if (!isRecord(value)) return null;
  if (typeof value.kind !== "string" || value.kind.length === 0) return null;
  if (!Array.isArray(value.options)) return null;
  const options: ChoiceOption[] = [];
  for (const entry of value.options) {
    const option = parseOption(entry);
    if (option !== null) options.push(option);
  }
  if (options.length === 0) return null;
  const window: ChoiceWindow = { kind: value.kind, options };
  if (typeof value.title === "string") window.title = value.title;
  if (typeof value.prompt === "string") window.prompt = value.prompt;
  if (
    Array.isArray(value.rejected_picks) &&
    value.rejected_picks.every((p) => typeof p === "string") &&
    value.rejected_picks.length > 0
  ) {
    window.rejected_picks = value.rejected_picks as string[];
  }
  return window;
}

/** opened:false = visible but disabled (档未开放). Absent = open, so
 * an older peer that never sends the flag does not render dead
 * buttons. */
export function choiceOptionDisabled(option: ChoiceOption): boolean {
  return option.opened === false;
}

/** The window is pending while the turn still carries it: the pick
 * (or a terminal frame) clears the field, so presence alone is the
 * suspend signal. */
export function turnHasPendingChoice(turn: Turn): boolean {
  return turn.choice_window !== undefined && turn.choice_window !== null;
}

export function hasPendingChoiceWindow(turns: readonly Turn[]): boolean {
  return turns.some(turnHasPendingChoice);
}

/** The ``ThreadDetail``-level sibling of ``hasPendingChoiceWindow``: a
 * reloaded paused turn is NOT in ``turns[]`` — the host carries it on
 * ``detail.pending_turn`` (see ``ThreadDetail.pending_turn``), which the
 * per-turn predicate cannot see. Present = a card can render, so the
 * CrashResume banner and the empty-thread copy must both yield. */
export function threadHasPendingChoice(
  detail: ThreadDetail | null | undefined,
): boolean {
  const pending = detail?.pending_turn;
  return pending !== undefined && pending !== null && pending.choice_window != null;
}

/** Copy for the re-select hint on a re-issued window. Empty list /
 * absent field renders nothing. */
export function choiceRejectedText(window: ChoiceWindow): string {
  const picks = window.rejected_picks ?? [];
  if (picks.length === 0) return "";
  return `无效选择（${picks.join("、")}），请重新选择。`;
}
