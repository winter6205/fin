import { describe, expect, it } from "vitest";
import {
  AttachmentUploadInProgressError,
  ConcurrentRunError,
  IngestMissingError,
  KnowledgeMcpFaultError,
} from "./api";
import {
  classifySendFailure,
  describeSendFailure,
  describeThreadCreateFailure,
} from "./sendFailure";

describe("classifySendFailure", () => {
  it("maps the 409 typed EXIT to concurrent_run", () => {
    expect(classifySendFailure(new ConcurrentRunError("concurrent_run"))).toBe(
      "concurrent_run",
    );
  });

  it("maps the knowledge-MCP 409 to its own kind, not concurrent_run", () => {
    const fault = new KnowledgeMcpFaultError("知识 MCP 故障");
    expect(classifySendFailure(fault)).toBe("knowledge_mcp_fault");
    expect(classifySendFailure(fault)).not.toBe("concurrent_run");
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
    const notice = describeSendFailure(
      new ConcurrentRunError("concurrent_run"),
    );
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

  it("surfaces 知识 MCP 故障 and the put-back for the knowledge-MCP 409", () => {
    const fault = describeSendFailure(
      new KnowledgeMcpFaultError("知识 MCP 故障"),
    );
    expect(fault.kind).toBe("knowledge_mcp_fault");
    expect(fault.message).toContain("知识 MCP 故障");
    expect(fault.message).toContain("放回输入框");
    // NOT the wrong concurrency claim.
    expect(fault.message).not.toContain("还有一轮在运行");
    expect(fault.message).not.toBe(
      describeSendFailure(new ConcurrentRunError("concurrent_run")).message,
    );
  });

  it("still names the fault even when the server message is empty", () => {
    const fault = describeSendFailure(new KnowledgeMcpFaultError(""));
    expect(fault.message.length).toBeGreaterThan(0);
    expect(fault.message).toContain("知识 MCP 故障");
  });

  it("still says something for an untyped failure", () => {
    const notice = describeSendFailure(new Error("HTTP 500: boom"));
    expect(notice.kind).toBe("unknown");
    expect(notice.message).toContain("HTTP 500: boom");
  });

  it("says something even when the rejection is not an Error", () => {
    expect(describeSendFailure(undefined).message.length).toBeGreaterThan(0);
  });

  it("names an in-flight upload as its own kind, not a held run", () => {
    const gate = new AttachmentUploadInProgressError();
    expect(classifySendFailure(gate)).toBe("attachment_upload_in_progress");
    expect(classifySendFailure(gate)).not.toBe("concurrent_run");
    const notice = describeSendFailure(gate);
    expect(notice.message).toContain("上传");
    expect(notice.message).not.toContain("还有一轮在运行");
    expect(notice.message).not.toContain("attachment_upload_in_progress");
  });
});

describe("describeThreadCreateFailure", () => {
  it("refuses a new conversation during an upload without the send wording", () => {
    // 新建 thread has no draft, so the create copy must not promise a
    // put-back, and must not leak the wire code.
    const notice = describeThreadCreateFailure(
      new AttachmentUploadInProgressError(),
    );
    expect(notice.kind).toBe("attachment_upload_in_progress");
    expect(notice.message).toContain("上传");
    expect(notice.message).not.toContain("放回输入框");
    expect(notice.message).not.toContain("attachment_upload_in_progress");
  });
});
