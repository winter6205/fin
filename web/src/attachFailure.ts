// Ordinary-person copy for a thread-attach failure (the attach door
// in the Composer). The door's own failures describe what happened
// to the ATTACHMENT — nothing about 入库 — so an attach failure can
// never read as ingest business (SC4). The one rejection that is a
// "wrong door" signal (oversize) names the 入库 process, not a fin
// page: the corpus-ingest UI lives outside the fin chrome, so the
// copy must not point at a retired entry.
//
// ADR 0089 keeps the lifecycle half: the upload door stores raw bytes
// and answers ``uploaded``; preparation starts from a graph turn and
// can fail LATER (parse / index / vision). Those states surface
// through the chip helpers below, not a door rejection — with one
// exception the composer enforces locally: a send or a new
// conversation while an upload is still in flight.

import { AttachFailedError } from "./api";
import type {
  AttachmentStatus,
  ThreadAttachment,
} from "./types";

export interface AttachFailureNotice {
  kind: string; // the wire error code, or "unknown"
  message: string;
}

/** The one sentence for the in-flight-upload gate, shared by the attach
 * door's notice and the send / new-thread refusal copy (sendFailure.ts)
 * so the two surfaces cannot name the same refusal differently. */
export const ATTACHMENT_UPLOAD_IN_PROGRESS_COPY =
  "还有附件正在上传，本轮没有发送，等上传完成后再试。";

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
      // The upload door no longer rejects non-text bytes, so this code
      // can only come from preparation. Kept mapped so an older host's
      // upload-time 400 still renders — as a stored file whose content
      // cannot be parsed here, never as "not uploaded".
      message = "这份文件已收下，但它的内容在这里解析不了，无法引用。";
      break;
    case "attachment_upload_in_progress":
      // Decided before the run: an upload is still writing its bytes.
      message = ATTACHMENT_UPLOAD_IN_PROGRESS_COPY;
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

// ----- ADR 0089: the readiness chip -----
//
// A stored attachment can sit in ``uploaded`` (raw bytes durable,
// preparation not yet requested), ``parsing`` (a graph turn asked for
// preparation and it is running), ``ready`` (referenceable), or
// ``failed`` with a NAMED reason the operator can act on. The composer
// renders exactly these labels; unknown wire values (a newer host) fall
// back to generic copy instead of breaking the row.

const STATUS_LABEL: Record<AttachmentStatus, string> = {
  // Distinct from 「可引用」 on purpose: the bytes are stored, nothing
  // is parsed or indexed yet, so the file cannot be cited.
  uploaded: "已上传，待解析",
  parsing: "解析中…",
  ready: "可引用",
  failed: "解析失败",
};

// ----- Composer copy -----
//
// Kept as exported strings rather than JSX so the composer's guidance
// is unit-testable in the node environment (no DOM in the web suite)
// and has one source the component reads.

/** The one-large-filing-per-turn guidance, offered as advice about how to
 * work, not as a bound: nothing in the product refuses a turn that
 * carries two attachments, and the server owns every byte, page and call
 * limit, so this copy claims none of its own. */
export const ATTACH_ONE_FILING_HINT =
  "一轮专注一份大文件更稳妥：先把手里这份问清楚，再传下一份。";

/** Shown while an upload POST is in flight — the reason the send is
 * held is the upload, not a running turn. */
export const ATTACH_UPLOAD_IN_FLIGHT_HINT =
  "附件正在上传，上传完成前不能发送。";

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
