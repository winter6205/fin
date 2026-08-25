// Ordinary-person copy for a send that never produced a run.
//
// The composer clears the draft the moment the operator hits Enter,
// so a swallowed rejection looked like the question evaporated. The
// send route's typed EXITs (specs/workspace.md: 409
// ``{error: concurrent_run}`` / 503 ``{error: ingest_missing}``,
// both decided BEFORE any SSE header) map here to copy the operator
// can act on; anything else still gets a non-empty message rather
// than silence.

import { ConcurrentRunError, IngestMissingError } from "./api";

export type SendFailureKind = "concurrent_run" | "ingest_missing" | "unknown";

export interface SendFailureNotice {
  kind: SendFailureKind;
  message: string;
}

export function classifySendFailure(error: unknown): SendFailureKind {
  if (error instanceof ConcurrentRunError) return "concurrent_run";
  if (error instanceof IngestMissingError) return "ingest_missing";
  return "unknown";
}

// The draft is always put back on failure (see Composer.submit): the
// operator's textarea held the only copy of the question, and every
// kind below is retryable once its cause clears.
const CONCURRENT_RUN_COPY =
  "这个对话还有一轮在运行，等它结束后再发送。你的问题已经放回输入框。";
const INGEST_MISSING_COPY =
  "语料还没 ingest（提取树缺失），先运行 ingest 再提问。你的问题已经放回输入框。";

function unknownCopy(error: unknown): string {
  const detail = error instanceof Error ? error.message.trim() : "";
  const tail = detail ? `（${detail}）` : "";
  return `没能发出去${tail}。你的问题已经放回输入框，可以重试。`;
}

/** Never returns empty copy — a failed send must be visible. */
export function describeSendFailure(error: unknown): SendFailureNotice {
  const kind = classifySendFailure(error);
  switch (kind) {
    case "concurrent_run":
      return { kind, message: CONCURRENT_RUN_COPY };
    case "ingest_missing":
      return { kind, message: INGEST_MISSING_COPY };
    default:
      return { kind, message: unknownCopy(error) };
  }
}
