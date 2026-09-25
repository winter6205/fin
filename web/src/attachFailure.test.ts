// corpus-serve-ingest T5: the attach door's named failures must
// surface as ordinary-person copy. The copy for size / binary-type
// rejections points at the corpus-ingest door — the two doors stay
// distinguishable (ADR 0047).
//
// ADR 0086 adds the lifecycle half: the chip
// labels (parsing / ready / failed + named reason) and the send
// refusal ``attachment_not_ready`` all render through this module,
// and NONE of the async-state copy may borrow 入库 vocabulary —
// the failure is the attachment's own.

import { describe, expect, it } from "vitest";
import { AttachFailedError } from "./api";
import {
  describeAttachFailure,
  describeAttachmentFailureReason,
  describeAttachmentStatus,
} from "./attachFailure";
import type { ThreadAttachment } from "./types";

function attachError(code: string, status = 400): AttachFailedError {
  return new AttachFailedError(code, status);
}

function attachment(
  overrides: Partial<ThreadAttachment> = {},
): ThreadAttachment {
  return {
    attachment_id: "att-1",
    filename: "notes.md",
    size_bytes: 6,
    scope: "thread_attach",
    status: "ready",
    version: 1,
    unit_count: 2,
    searchable: true,
    ...overrides,
  };
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
      "attachment_not_found",
      "attachment_not_ready",
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

  it("names the 入库 process without pointing at a retired fin page", () => {
    // /kb / KnowledgeBasePage left the product surface, so the copy
    // must not tell the operator to open the 知识库 page by name.
    for (const code of ["attachment_too_large", "attachment_not_text"]) {
      const notice = describeAttachFailure(attachError(code));
      expect(notice.message).toContain("入库");
      expect(notice.message).not.toContain("知识库");
    }
  });

  it("never borrows ingest-door vocabulary for the attach door's own code", () => {
    // The two doors must not blur: attach failures that are NOT a
    // size/type rejection say what happened to the ATTACHMENT, not
    // anything about入库 (SC4: attach failure must not look like
    // ingest business).
    const notice = describeAttachFailure(attachError("thread_not_found"));
    expect(notice.message).not.toContain("入库");
  });

  it("keeps the lifecycle refusals clear of 入库 vocabulary", () => {
    // attachment_not_ready / attachment_not_found are async-state
    // facts about the ATTACHMENT, never ingest business (SC4).
    for (const code of ["attachment_not_ready", "attachment_not_found"]) {
      const notice = describeAttachFailure(attachError(code));
      expect(notice.message).not.toContain("入库");
    }
  });

  it("says the parsing refusal is retryable once the attachment is ready", () => {
    const notice = describeAttachFailure(attachError("attachment_not_ready"));
    expect(notice.message).toContain("解析中");
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

describe("describeAttachmentStatus (readiness chip)", () => {
  it("labels each lifecycle state distinctly and non-empty", () => {
    const labels = [
      describeAttachmentStatus(attachment({ status: "parsing" })),
      describeAttachmentStatus(attachment({ status: "ready" })),
      describeAttachmentStatus(
        attachment({ status: "failed", failure_reason: "source_unavailable" }),
      ),
    ];
    for (const label of labels) expect(label.trim().length).toBeGreaterThan(0);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("names the failure reason on a failed record", () => {
    const label = describeAttachmentStatus(
      attachment({ status: "failed", failure_reason: "attachment_empty_text" }),
    );
    expect(label).toContain("解析失败");
    expect(label).toContain(describeAttachmentFailureReason("attachment_empty_text"));
  });

  it("never renders an empty chip for an absent or unknown reason", () => {
    // The wire may gain reasons this client has not seen: the label
    // still has to read as a failure.
    for (const reason of [null, undefined, "vision_call_failed"]) {
      const label = describeAttachmentStatus(
        attachment({ status: "failed", failure_reason: reason }),
      );
      expect(label).toContain("解析失败");
      expect(label.length).toBeGreaterThan("解析失败".length);
    }
  });
});

describe("describeAttachmentFailureReason", () => {
  it("gives ordinary-person copy for each named reason", () => {
    expect(
      describeAttachmentFailureReason("attachment_empty_text"),
    ).not.toBe(describeAttachmentFailureReason("source_unavailable"));
    for (const reason of ["attachment_empty_text", "source_unavailable"]) {
      expect(describeAttachmentFailureReason(reason).length).toBeGreaterThan(0);
    }
  });

  it("falls back to non-empty copy for null or unrecognised reasons", () => {
    for (const reason of [null, undefined, "some_future_code"]) {
      expect(describeAttachmentFailureReason(reason).length).toBeGreaterThan(
        0,
      );
    }
  });
});
