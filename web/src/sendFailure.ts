// Ordinary-person copy for a send that never produced a run.
//
// The composer clears the draft the moment the operator hits Enter,
// so a swallowed rejection looked like the question evaporated. The
// send route's typed EXITs (specs/workspace.md: 409
// ``{error: concurrent_run}`` / 409 ``{error: knowledge_mcp_fault}``
// / 503 ``{error: ingest_missing}``, all decided BEFORE any SSE
// header) map here to copy the operator can act on; anything else
// still gets a non-empty message rather than silence.

import {
  ConcurrentRunError,
  IngestMissingError,
  KnowledgeMcpFaultError,
} from "./api";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";

export type SendFailureKind =
  "concurrent_run" | "knowledge_mcp_fault" | "ingest_missing" | "unknown";

export interface SendFailureNotice {
  kind: SendFailureKind;
  message: string;
}

export function classifySendFailure(error: unknown): SendFailureKind {
  if (error instanceof KnowledgeMcpFaultError) return "knowledge_mcp_fault";
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
// 缺 / 坏 boot: the knowledge MCP cannot answer, so the question never
// left the box. Surface the shared host copy verbatim so the left
// corner and this notice name the same fault.
const KNOWLEDGE_MCP_FAULT_SEND_COPY =
  `${KNOWLEDGE_MCP_FAULT_COPY}：检索服务连接不上，问题没有发出，` +
  "已经放回输入框，等服务恢复后再试。";

function unknownCopy(error: unknown): string {
  const detail = error instanceof Error ? error.message.trim() : "";
  const tail = detail ? `（${detail}）` : "";
  return `没能发出去${tail}。你的问题已经放回输入框，可以重试。`;
}

/** Never returns empty copy — a failed send must be visible. */
export function describeSendFailure(error: unknown): SendFailureNotice {
  const kind = classifySendFailure(error);
  switch (kind) {
    case "knowledge_mcp_fault":
      return { kind, message: KNOWLEDGE_MCP_FAULT_SEND_COPY };
    case "concurrent_run":
      return { kind, message: CONCURRENT_RUN_COPY };
    case "ingest_missing":
      return { kind, message: INGEST_MISSING_COPY };
    default:
      return { kind, message: unknownCopy(error) };
  }
}

// 新建 thread has NO draft, so every create notice must stay clear of
// the send copy's 「已经放回输入框」 — that sentence would describe a
// textarea that does not exist on that door.
const KNOWLEDGE_MCP_FAULT_CREATE_COPY =
  `${KNOWLEDGE_MCP_FAULT_COPY}：检索服务连接不上，` +
  "新建不了 thread。等服务恢复后再试。";

function unknownCreateCopy(error: unknown): string {
  const detail = error instanceof Error ? error.message.trim() : "";
  const tail = detail ? `（${detail}）` : "";
  return `没能新建 thread${tail}。可以重试。`;
}

/** Copy for the 新建 thread door. Same typed faults as the send path;
 * the wording is the door's own because only the send path restores a
 * draft. */
export function describeThreadCreateFailure(error: unknown): SendFailureNotice {
  const kind = classifySendFailure(error);
  if (kind === "knowledge_mcp_fault") {
    return { kind, message: KNOWLEDGE_MCP_FAULT_CREATE_COPY };
  }
  return { kind, message: unknownCreateCopy(error) };
}
