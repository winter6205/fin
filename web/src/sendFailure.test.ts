import { describe, expect, it } from "vitest";
import { ConcurrentRunError, IngestMissingError } from "./api";
import { classifySendFailure, describeSendFailure } from "./sendFailure";

describe("classifySendFailure", () => {
  it("maps the 409 typed EXIT to concurrent_run", () => {
    expect(classifySendFailure(new ConcurrentRunError("concurrent_run"))).toBe(
      "concurrent_run",
    );
  });

  it("maps the 503 typed EXIT to ingest_missing", () => {
    expect(classifySendFailure(new IngestMissingError("ingest_missing"))).toBe(
      "ingest_missing",
    );
  });

  it("maps anything else to unknown", () => {
    expect(classifySendFailure(new Error("HTTP 500: boom"))).toBe("unknown");
    expect(classifySendFailure("not an error")).toBe("unknown");
  });
});

describe("describeSendFailure", () => {
  it("gives 409 non-empty ordinary-person copy, not the raw enum", () => {
    const notice = describeSendFailure(new ConcurrentRunError("concurrent_run"));
    expect(notice.kind).toBe("concurrent_run");
    expect(notice.message.length).toBeGreaterThan(0);
    expect(notice.message).not.toContain("concurrent_run");
  });

  it("gives 503 copy distinct from the 409 copy", () => {
    const concurrent = describeSendFailure(new ConcurrentRunError(""));
    const ingest = describeSendFailure(new IngestMissingError(""));
    expect(ingest.kind).toBe("ingest_missing");
    expect(ingest.message.length).toBeGreaterThan(0);
    expect(ingest.message).not.toBe(concurrent.message);
  });

  it("still says something for an untyped failure", () => {
    const notice = describeSendFailure(new Error("HTTP 500: boom"));
    expect(notice.kind).toBe("unknown");
    expect(notice.message).toContain("HTTP 500: boom");
  });

  it("says something even when the rejection is not an Error", () => {
    expect(describeSendFailure(undefined).message.length).toBeGreaterThan(0);
  });
});
