// Ordinary-person copy for a send that never produced a run.
//
// The draft is discarded only once the transport took the send, so a
// rejection can never read as the question evaporating. The
// send route's typed EXITs (specs/workspace.md: 409
// ``{error: concurrent_run}`` / 409 ``{error: knowledge_mcp_fault}``
// / 503 ``{error: ingest_missing}``, all decided BEFORE any SSE
// header) map here to copy the operator can act on; anything else
// still gets a non-empty message rather than silence.

import {
  AttachmentUploadInProgressError,
  ConcurrentRunError,
  IngestMissingError,
  KnowledgeMcpFaultError,
} from "./api";
import { ATTACHMENT_UPLOAD_IN_PROGRESS_COPY } from "./attachFailure";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";

export type SendFailureKind =
  | "concurrent_run"
  | "knowledge_mcp_fault"
  | "ingest_missing"
  | "attachment_upload_in_progress"
  | "unknown";

export interface SendFailureNotice {
  kind: SendFailureKind;
  message: string;
}

export function classifySendFailure(error: unknown): SendFailureKind {
  if (error instanceof KnowledgeMcpFaultError) return "knowledge_mcp_fault";
  // A held upload is neither a running turn nor an MCP fault: it is
  // this client's own pre-flight of the host's send gate.
  if (error instanceof AttachmentUploadInProgressError) {
    return "attachment_upload_in_progress";
  }
  if (error instanceof ConcurrentRunError) return "concurrent_run";
  if (error instanceof IngestMissingError) return "ingest_missing";
  return "unknown";
}

// A failed send leaves the question in the textarea (see Composer.submit):
// the draft is discarded only once the transport took the send, and every
// kind below is retryable once its cause clears.
// The 409 line names what Runtime refused for THIS send. It must not
// state a standing "wait for the run to finish" rule: an active run alone
// does not hold input (activeInput.ts is the single policy), and the
// operator keeps the question either way.
const CONCURRENT_RUN_COPY =
  "这次发送 Runtime 没有接受：这个对话上还有一轮在占用。你的问题已经放回输入框，可以直接重试。";
const INGEST_MISSING_COPY =
  "语料还没 ingest（提取树缺失），先运行 ingest 再提问。你的问题已经放回输入框。";
// 缺 / 坏 boot: the knowledge MCP cannot answer, so the question never
// left the box. Surface the shared host copy verbatim so the left
// corner and this notice name the same fault.
const KNOWLEDGE_MCP_FAULT_SEND_COPY =
  `${KNOWLEDGE_MCP_FAULT_COPY}：检索服务连接不上，问题没有发出，` +
  "已经放回输入框，等服务恢复后再试。";
// The held-upload refusal keeps the attach door's own sentence (one
// SSOT in attachFailure.ts) and adds only the put-back the send path
// owes the operator.
const UPLOAD_IN_FLIGHT_SEND_COPY =
  `${ATTACHMENT_UPLOAD_IN_PROGRESS_COPY}你的问题已经放回输入框。`;

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
    case "attachment_upload_in_progress":
      return { kind, message: UPLOAD_IN_FLIGHT_SEND_COPY };
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
// A pending upload blocks a conversation START as well as a send, and
// the refusal is neither of the two faults above.
const UPLOAD_IN_FLIGHT_CREATE_COPY =
  "还有附件正在上传，先等上传完成再新建 thread。";

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
  if (kind === "attachment_upload_in_progress") {
    return { kind, message: UPLOAD_IN_FLIGHT_CREATE_COPY };
  }
  return { kind, message: unknownCreateCopy(error) };
}
