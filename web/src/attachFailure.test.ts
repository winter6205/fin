// corpus-serve-ingest T5: the attach door's named failures must
// surface as ordinary-person copy. The copy for size / binary-type
// rejections points at the corpus-ingest door — the two doors stay
// distinguishable (ADR 0047).

import { describe, expect, it } from "vitest";
import { AttachFailedError } from "./api";
import { describeAttachFailure } from "./attachFailure";

function attachError(code: string, status = 400): AttachFailedError {
  return new AttachFailedError(code, status);
}

describe("describeAttachFailure", () => {
  it("keeps the wire code as kind on every notice", () => {
    expect(describeAttachFailure(attachError("attachment_not_text")).kind).toBe(
      "attachment_not_text",
    );
  });

  it("gives non-empty copy for every named attach failure", () => {
    const codes = [
      "thread_not_found",
      "empty_attachment",
      "attachment_too_large",
      "attachment_not_text",
      "attachment_store_failed",
    ];
    for (const code of codes) {
      const notice = describeAttachFailure(attachError(code));
      expect(notice.kind).toBe(code);
      expect(notice.message.length).toBeGreaterThan(0);
    }
  });

  it("points binary-format rejections at the corpus-ingest door", () => {
    const notice = describeAttachFailure(attachError("attachment_not_text"));
    expect(notice.message).toContain("入库");
  });

  it("points oversize rejections at the corpus-ingest door too", () => {
    const notice = describeAttachFailure(attachError("attachment_too_large"));
    expect(notice.message).toContain("入库");
  });

  it("never borrows ingest-door vocabulary for the attach door's own code", () => {
    // The two doors must not blur: attach failures that are NOT a
    // size/type rejection say what happened to the ATTACHMENT, not
    // anything about入库 (SC4: attach failure must not look like
    // ingest business).
    const notice = describeAttachFailure(attachError("thread_not_found"));
    expect(notice.message).not.toContain("入库");
  });

  it("still says something for an untyped failure", () => {
    const notice = describeAttachFailure(new Error("HTTP 500: boom"));
    expect(notice.kind).toBe("unknown");
    expect(notice.message).toContain("HTTP 500: boom");
  });

  it("says something even when the rejection is not an Error", () => {
    expect(describeAttachFailure(undefined).message.length).toBeGreaterThan(0);
  });
});
