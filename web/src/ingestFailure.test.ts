// corpus-serve-ingest T5: the ingest door's named failures must
// surface as ordinary-person copy — never a silent skip (SC7/SC8),
// never a bare English enum. Mirrors sendFailure.test.ts.

import { describe, expect, it } from "vitest";
import { IngestFailedError } from "./api";
import { describeIngestFailure } from "./ingestFailure";

function ingestError(code: string, status = 422): IngestFailedError {
  return new IngestFailedError(code, status);
}

describe("describeIngestFailure", () => {
  it("keeps the wire code as kind on every notice", () => {
    expect(describeIngestFailure(ingestError("unsupported_format")).kind).toBe(
      "unsupported_format",
    );
    expect(
      describeIngestFailure(ingestError("ingest_upload_too_large")).kind,
    ).toBe("ingest_upload_too_large");
  });

  it("gives non-empty copy for every named ingest failure", () => {
    const codes = [
      "missing_file",
      "format_unspecified",
      "unsupported_format",
      "ingest_upload_too_large",
      "document_id_missing",
      "invalid_doc_type",
      "concurrent_run",
      "unknown_job",
    ];
    for (const code of codes) {
      const notice = describeIngestFailure(ingestError(code));
      expect(notice.kind).toBe(code);
      expect(notice.message.length).toBeGreaterThan(0);
    }
  });

  it("never prints the bare enum for a named code", () => {
    for (const code of [
      "document_id_missing",
      "invalid_doc_type",
      "concurrent_run",
      "unknown_job",
    ]) {
      expect(describeIngestFailure(ingestError(code)).message).not.toContain(
        code,
      );
    }
  });

  // Identity now comes from the form's two required fields and the
  // filename never participates, so renaming the file is not the fix.
  it("points at company and period on document_id_missing", () => {
    const notice = describeIngestFailure(ingestError("document_id_missing"));
    expect(notice.message).toContain("公司");
    expect(notice.message).toContain("期间");
  });

  it("says this submission started nothing on concurrent_run (409)", () => {
    const notice = describeIngestFailure(ingestError("concurrent_run", 409));
    expect(notice.message).toContain("没有开始");
  });

  it("says the job state is unverifiable on unknown_job (404)", () => {
    const notice = describeIngestFailure(ingestError("unknown_job", 404));
    expect(notice.message).toContain("查不到");
  });

  it("names the supported formats on unsupported_format (415)", () => {
    const notice = describeIngestFailure(ingestError("unsupported_format", 415));
    expect(notice.message).toContain("pdf");
    expect(notice.message).toContain("md");
  });

  it("tells the operator what to fix on format_unspecified", () => {
    const notice = describeIngestFailure(ingestError("format_unspecified"));
    expect(notice.message).toContain("扩展名");
  });

  it("maps the parser family (400) to unusable-bytes copy", () => {
    const codes = [
      "empty_source",
      "malformed_source",
      "encrypted_source",
      "format_mismatch",
      "no_units",
      "metadata_incomplete",
    ];
    for (const code of codes) {
      const notice = describeIngestFailure(ingestError(code, 400));
      expect(notice.message.length).toBeGreaterThan(0);
    }
  });

  it("makes retry the recovery on push outage (503) copy", () => {
    const codes = [
      "push_service_unavailable",
      "push_protocol_error",
      "push_batch_failed",
      "push_journal_unwritable",
      "push_journal_unreadable",
      "push_journal_corrupt",
    ];
    for (const code of codes) {
      const notice = describeIngestFailure(ingestError(code, 503));
      expect(notice.message).toContain("重试");
    }
  });

  it("says the push landed but cleanup is pending on invalidation (503) codes", () => {
    const notice = describeIngestFailure(
      ingestError("invalidation_service_unavailable", 503),
    );
    expect(notice.message).toContain("重试");
    expect(notice.message).not.toBe(
      describeIngestFailure(ingestError("push_service_unavailable", 503))
        .message,
    );
  });

  it("still says something for an untyped failure", () => {
    const notice = describeIngestFailure(new Error("HTTP 500: boom"));
    expect(notice.kind).toBe("unknown");
    expect(notice.message).toContain("HTTP 500: boom");
  });

  it("says something even when the rejection is not an Error", () => {
    expect(describeIngestFailure(undefined).message.length).toBeGreaterThan(0);
  });
});
