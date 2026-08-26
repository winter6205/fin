// Mid-flight stream error taxonomy: classify the exceptions
// ``_stream_run_frames`` may surface into a closed enum the SPA
// can render a precise label for. Mirrors
// ``fin.host.workspace_http._STREAM_FAILURE_USER_COPY`` on the
// Python side — same closed set of ``kind`` strings, same
// Chinese user-facing copy. A row whose ``status_classified`` is
// empty (or one of the closed set's keys never produced by the
// server) renders as no label rather than a misleading one.

import type { RunStatus, StreamErrorKind, Turn } from "./types";

const STREAM_ERROR_KINDS: ReadonlySet<string> = new Set([
  "auth",
  "network",
  "timeout",
  "rate_limit",
  "not_found",
  "server",
  "unknown",
]);

/**
 * Defensive parse of an SSE / GET ``status_classified`` field.
 * Returns ``undefined`` for any value outside the closed enum —
 * unknown / missing wire values must never reach the render path.
 */
export function parseStreamErrorKind(
  raw: unknown,
): StreamErrorKind | undefined {
  if (typeof raw !== "string") return undefined;
  return STREAM_ERROR_KINDS.has(raw) ? (raw as StreamErrorKind) : undefined;
}

// Chinese label per kind for the small "原因: <label>" row on a
// refuse bubble. Single source of truth so every render site
// (live SSE, GET reload, history list) reads the same string.
export const STREAM_ERROR_LABEL: Readonly<Record<StreamErrorKind, string>> = {
  auth: "鉴权失败",
  network: "网络错误",
  timeout: "调用超时",
  rate_limit: "服务限流",
  not_found: "接口/模型不存在",
  server: "服务内部错误",
  unknown: "未知错误",
};

/** Label for a kind, or empty string when the kind is missing /
 * outside the closed set. */
export function describeStreamErrorKind(kind: string | undefined): string {
  if (!kind) return "";
  if (!STREAM_ERROR_KINDS.has(kind)) return "";
  return STREAM_ERROR_LABEL[kind as StreamErrorKind];
}

/**
 * Whether the refuse row should render the small "原因: <label>"
 * line. The label is mid-flight-stream-failure ONLY: a
 * citation-gate refuse or a step_cap run must not show a
 * classification that didn't come from the stream engine.
 *
 * Pure helper extracted out of ``ThreadPage.TurnView`` so the
 * conditional is unit-testable without React rendering — the
 * SPA tests do not pull in a DOM testing library by design.
 */
export function shouldRenderStreamErrorKind(
  turn: Pick<Turn, "status" | "status_classified">,
): boolean {
  if (turn.status !== "refuse") return false;
  if (!turn.status_classified) return false;
  return STREAM_ERROR_KINDS.has(turn.status_classified);
}

/** The exact text the JSX renders for the label row. Mirrors
 * ``ThreadPage.TurnView``'s template literal so a refactor that
 * drops the prefix or the colon surfaces here. */
export function streamErrorKindRowText(
  turn: Pick<Turn, "status_classified">,
): string {
  return `原因：${describeStreamErrorKind(turn.status_classified)}`;
}

// Re-export for test ergonomics.
export type { RunStatus };
