// Ordinary-person copy for a thread-attach failure (the attach door
// in the Composer). The door's own failures describe what happened
// to the ATTACHMENT — nothing about入库 — so an attach failure can
// never read as ingest business (SC4). The two rejections that are
// actually "wrong door" signals (oversize, non-text bytes) point the
// operator at the corpus-ingest door by name.

import { AttachFailedError } from "./api";

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
        "这份文件超过附件大小上限，没有上传。要长期入库请改用知识库的入库入口。";
      break;
    case "attachment_not_text":
      message =
        "附件只支持文本文件；PDF / Word / Excel 等格式请改用知识库的入库入口。";
      break;
    case "attachment_store_failed":
      message = "附件保存失败，请重试。";
      break;
    default:
      message = genericCopy(error);
      break;
  }
  return { kind, message };
}
