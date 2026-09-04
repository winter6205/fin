// Ordinary-person copy for a corpus-ingest failure (the ingest door
// in the home corpus area). The endpoint answers every failure with a
// named ``error`` code and NO partial-success 200 (SC7/SC8); this
// module maps each named family to copy the operator can act on.
// The wire code always rides ``kind`` (rendered as
// data-failure-kind) so it stays greppable, but the sentence itself
// never prints a bare enum.

import { IngestFailedError } from "./api";

export interface IngestFailureNotice {
  kind: string; // the wire error code, or "unknown"
  message: string;
}

const FORMAT_HINT = "支持 pdf / docx / xlsx / pptx / html / md / txt。";

const UNSUPPORTED_FORMAT_COPY = `这种格式不支持入库。${FORMAT_HINT}`;
const FORMAT_UNSPECIFIED_COPY = `认不出这份文件的格式（文件名没有可识别的扩展名）。请改用带扩展名的文件名再试。${FORMAT_HINT}`;
const UPLOAD_TOO_LARGE_COPY = "这份文件超过入库大小上限，请拆分后再入库。";
const DOCUMENT_ID_MISSING_COPY =
  "无法确定这份文件的编号（document_id），也没有可用的文件名。请用带名称的文件再试。";
const INVALID_DOC_TYPE_COPY =
  "指定的文档场景（doc_type）不在参数表内。清空场景让它自动分类，或改用受支持的场景再试。";
const MISSING_FILE_COPY = "没有选中任何文件，未执行入库。";

// Parser family (400): the uploader's bytes are unusable and nothing
// was written — say that instead of implying a partial push.
const PARSE_FAMILY_COPY: Record<string, string> = {
  empty_source: "这份文件内容为空，未执行入库。",
  malformed_source: "这份文件内容损坏，无法解析，未执行入库。",
  encrypted_source: "这份文件已加密，无法解析，未执行入库。",
  format_mismatch: "文件内容与它的扩展名不符，未执行入库。",
  no_units: "这份文件没有可提取的正文，未执行入库。",
  metadata_incomplete: "这份文件缺少必要的元信息，未执行入库。",
};

// Push outage family (503): the push is idempotent, so retrying IS
// the recovery — the copy must say that instead of implying loss.
function pushOutageCopy(code: string): string {
  return `入库时检索后端暂时不可用（${code}），请稍后重试；重复入库是安全的。`;
}

// Invalidation family (503): the push already landed; only the stale
// block closure failed typed (SC8) — a retry completes the cleanup.
const INVALIDATION_COPY =
  "文件已推送，但旧版本的清理没有完成，请重试一次以完成清理；重复入库是安全的。";

function genericCopy(error: unknown): string {
  const detail = error instanceof Error ? error.message.trim() : "";
  return `入库没有完成${detail ? `（${detail}）` : ""}。请稍后重试；重复入库是安全的。`;
}

/** Never returns empty copy — a failed ingest must be visible. */
export function describeIngestFailure(error: unknown): IngestFailureNotice {
  const kind = error instanceof IngestFailedError ? error.code : "unknown";
  let message: string;
  switch (kind) {
    case "missing_file":
      message = MISSING_FILE_COPY;
      break;
    case "format_unspecified":
      message = FORMAT_UNSPECIFIED_COPY;
      break;
    case "unsupported_format":
      message = UNSUPPORTED_FORMAT_COPY;
      break;
    case "ingest_upload_too_large":
      message = UPLOAD_TOO_LARGE_COPY;
      break;
    case "document_id_missing":
      message = DOCUMENT_ID_MISSING_COPY;
      break;
    case "invalid_doc_type":
      message = INVALID_DOC_TYPE_COPY;
      break;
    default:
      if (kind in PARSE_FAMILY_COPY) {
        message = PARSE_FAMILY_COPY[kind];
      } else if (kind.startsWith("invalidation_")) {
        message = INVALIDATION_COPY;
      } else if (kind.startsWith("push_")) {
        message = pushOutageCopy(kind);
      } else {
        message = genericCopy(error);
      }
      break;
  }
  return { kind, message };
}
