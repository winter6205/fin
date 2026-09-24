// Ordinary-person copy for a thread-attach failure (the attach door
// in the Composer). The door's own failures describe what happened
// to the ATTACHMENT — nothing about 入库 — so an attach failure can
// never read as ingest business (SC4). The two rejections that are
// actually "wrong door" signals (oversize, non-text bytes) name the
// 入库 process, not a fin page: the corpus-ingest UI lives outside
// the fin chrome, so the copy must not point at a retired entry.
//
// ADR 0086 adds the lifecycle half: the door can
// now fail LATER (async parse / index), and a send may be refused
// because a referenced attachment is still parsing. Those states
// surface through the chip helpers below, not a door rejection.

import { AttachFailedError } from "./api";
import type {
  AttachmentStatus,
  ThreadAttachment,
} from "./types";

export interface AttachFailureNotice {
  kind: string; // the wire error code, or "unknown"
  message: string;
}

function genericCopy(error: unknown): string {
  const detail = error instanceof Error ? error.message.trim() : "";
  return `附件没有上传${detail ? `（${detail}）` : ""}。请重试。`;
}

/** Never returns empty copy — a failed attach must be visible. */
export function describeAttachFailure(error: unknown): AttachFailureNotice {
  const kind = error instanceof AttachFailedError ? error.code : "unknown";
  let message: string;
  switch (kind) {
    case "thread_not_found":
      message = "这个对话不存在或已被删除，附件没有上传。";
      break;
    case "empty_attachment":
      message = "这份文件是空的，没有上传。";
      break;
    case "attachment_too_large":
      message =
        "这份文件超过附件大小上限，没有上传。大文件请在入库流程里处理。";
      break;
    case "attachment_not_text":
      message =
        "附件只支持文本文件。PDF / Word / Excel 等请在入库流程里处理，再在对话里提问。";
      break;
    case "attachment_store_failed":
      message = "附件保存失败，请重试。";
      break;
    case "attachment_not_found":
      // The id is gone (thread deleted / rebuilt): only the retry /
      // read door can hit this, never a re-upload of the same file.
      message = "这个附件已经不在此对话中，无法继续引用。";
      break;
    case "attachment_not_ready":
      // A send refused because it referenced a still-parsing
      // attachment (ADR 0086): the question is retryable once the
      // chip turns ready.
      message = "引用的附件还在解析中，本轮没有发问，等它就绪后再试。";
      break;
    default:
      message = genericCopy(error);
      break;
  }
  return { kind, message };
}

// ----- ADR 0086: the readiness chip -----
//
// A stored attachment can sit in ``parsing`` (async preparation
// running), ``ready`` (referenceable), or ``failed`` with a NAMED
// reason the operator can act on. The composer renders exactly these
// labels; unknown wire values (a newer host) fall back to generic
// copy instead of breaking the row.

const STATUS_LABEL: Record<AttachmentStatus, string> = {
  parsing: "解析中…",
  ready: "可引用",
  failed: "解析失败",
};

/** Copy for ``failure_reason`` on a failed record. Never empty: an
 * unrecognised reason still reads as a failure with retry offered
 * rather than a blank chip. */
export function describeAttachmentFailureReason(
  reason: string | null | undefined,
): string {
  switch (reason) {
    case "attachment_empty_text":
      return "没有解析出文本内容";
    case "source_unavailable":
      return "解析或索引服务不可用";
    default:
      return "原因未知";
  }
}

/** One-line status text for the Composer's attachment chip. */
export function describeAttachmentStatus(
  attachment: ThreadAttachment,
): string {
  if (attachment.status === "failed") {
    const reason = describeAttachmentFailureReason(attachment.failure_reason);
    return `${STATUS_LABEL.failed}（${reason}）`;
  }
  return STATUS_LABEL[attachment.status];
}
