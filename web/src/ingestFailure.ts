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
// The identity is derived server-side and the client has no field that
// supplies one, so the only recovery is another submission.
const DOCUMENT_ID_MISSING_COPY = "服务器没有给出这份文件的编号，请重试。";
// The identity is derived from the trusted corpus scope and the content
// digest, so a caller-chosen id cannot be honored — the fix is to send the
// upload WITHOUT an id.
const DOCUMENT_ID_NOT_ALLOWED_COPY =
  "这份文件的编号由内容自动确定，不接受手动指定编号。请去掉编号后重新提交。";
// Explicit upload metadata is validated, not edited: a wildcard or an
// overlong value is refused before anything is written, so the operator
// fixes the field rather than retrying.
const INVALID_UPLOAD_METADATA_COPY =
  "填写的标题、公司或期间不符合要求（不支持 * ? 通配符，且长度需在 1–256 字节内）。请修改后重新提交。";
const INVALID_DOC_TYPE_COPY =
  "指定的文档场景（doc_type）不在参数表内。清空场景让它自动分类，或改用受支持的场景再试。";
const MISSING_FILE_COPY = "没有选中任何文件，未执行入库。";
// 409: the in-flight job for this document_id is untouched and this
// submission started nothing, so the operator waits rather than retries
// immediately (a retry would collide the same way).
const CONCURRENT_RUN_COPY =
  "同一份文件已有一轮入库在进行中，这次提交没有开始。请等它结束后再上传。";
// The status door's typed miss: the id is gone (a job store that did not
// survive a restart), so the state below it is unknown rather than
// still-queued — the only way forward is a new submission.
const UNKNOWN_JOB_COPY =
  "查不到这一轮入库的记录，因此无法确认它的状态。请重新提交这份文件。";

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

// Fixed-message wire codes, one table so adding a named code never grows
// the resolver's branching. Prefix families (push_ / invalidation_) and
// the parse family stay resolved in :func:`describeIngestFailure`.
const STATIC_COPY: Record<string, string> = {
  missing_file: MISSING_FILE_COPY,
  format_unspecified: FORMAT_UNSPECIFIED_COPY,
  unsupported_format: UNSUPPORTED_FORMAT_COPY,
  ingest_upload_too_large: UPLOAD_TOO_LARGE_COPY,
  document_id_missing: DOCUMENT_ID_MISSING_COPY,
  document_id_not_allowed: DOCUMENT_ID_NOT_ALLOWED_COPY,
  invalid_upload_metadata: INVALID_UPLOAD_METADATA_COPY,
  concurrent_run: CONCURRENT_RUN_COPY,
  unknown_job: UNKNOWN_JOB_COPY,
  invalid_doc_type: INVALID_DOC_TYPE_COPY,
};

function genericCopy(error: unknown): string {
  const detail = error instanceof Error ? error.message.trim() : "";
  return `入库没有完成${detail ? `（${detail}）` : ""}。请稍后重试；重复入库是安全的。`;
}

/** Never returns empty copy — a failed ingest must be visible. */
export function describeIngestFailure(error: unknown): IngestFailureNotice {
  const kind = error instanceof IngestFailedError ? error.code : "unknown";
  const message =
    STATIC_COPY[kind] ??
    PARSE_FAMILY_COPY[kind] ??
    (kind.startsWith("invalidation_")
      ? INVALIDATION_COPY
      : kind.startsWith("push_")
        ? pushOutageCopy(kind)
        : genericCopy(error));
  return { kind, message };
}
