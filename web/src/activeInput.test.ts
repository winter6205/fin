// The one active-input decision: what holds the composer, and what the
// operator is told about a submission.
//
// Expected values come from specs/question-workspace.md §"Active work,
// outcomes, and history" and §"Input ordering and module seams": a live run
// alone never holds input, current host locks and in-flight uploads do, and
// the workspace reports submission state without inventing task,
// clarification, or cancellation facts of its own.

import { describe, expect, it } from "vitest";
import {
  AttachmentUploadInProgressError,
  ConcurrentRunError,
} from "./api";
import { ATTACHMENT_UPLOAD_IN_PROGRESS_COPY } from "./attachFailure";
import {
  ACCEPTED_NOTICE,
  decideActiveInput,
  heldRefusalFor,
  PENDING_CHOICE_HELD_COPY,
  refusalCopy,
  sendControlLabel,
  SUBMITTING_NOTICE,
  TRANSPORT_UNKNOWN_NOTICE,
  UPLOAD_IN_FLIGHT_HELD_COPY,
  type ActiveInputFacts,
} from "./activeInput";

const BASE: ActiveInputFacts = {
  pendingChoiceWindow: false,
  uploadInFlight: false,
  submission: "idle",
  transportFailed: false,
  refusalKind: null,
};

const decide = (over: Partial<ActiveInputFacts> = {}) =>
  decideActiveInput({ ...BASE, ...over });

describe("decideActiveInput — what holds the input", () => {
  it("holds while a pending choice window owns the thread", () => {
    const d = decide({ pendingChoiceWindow: true });
    expect(d.held).toBe(true);
    expect(d.holdReason).toBe("pending_choice");
    expect(d.holdCopy).toBe(PENDING_CHOICE_HELD_COPY);
  });

  it("holds while an attachment upload is in flight", () => {
    const d = decide({ uploadInFlight: true });
    expect(d.held).toBe(true);
    expect(d.holdReason).toBe("upload_in_flight");
    // One sentence for one refusal: the attach door's own copy, not a
    // second wording the two surfaces could drift apart from.
    expect(d.holdCopy).toBe(UPLOAD_IN_FLIGHT_HELD_COPY);
    expect(UPLOAD_IN_FLIGHT_HELD_COPY).toBe(ATTACHMENT_UPLOAD_IN_PROGRESS_COPY);
  });

  it("names the host lock when both holds are present", () => {
    const d = decide({ pendingChoiceWindow: true, uploadInFlight: true });
    expect(d.held).toBe(true);
    expect(d.holdReason).toBe("pending_choice");
  });

  it("leaves input available when nothing holds it", () => {
    const d = decide();
    expect(d.held).toBe(false);
    expect(d.holdReason).toBeNull();
    expect(d.holdCopy).toBeNull();
  });

  it("keeps input available for the next submission while one is pending", () => {
    const d = decide({ submission: "pending" });
    expect(d.held).toBe(false);
    expect(d.pending).toBe(true);
  });

  it("takes no run fact at all, so a live run cannot hold input", () => {
    // The hold inputs are the whole shape: a running turn is not readable
    // here, which is the SC-5 rule enforced by construction rather than by
    // a field the caller could forget to ignore.
    expect(Object.keys(BASE).sort()).toEqual([
      "pendingChoiceWindow",
      "refusalKind",
      "submission",
      "transportFailed",
      "uploadInFlight",
    ]);
  });
});

describe("decideActiveInput — submission notices", () => {
  it("says the submission is still unresolved while pending", () => {
    expect(decide({ submission: "pending" }).notice).toBe(SUBMITTING_NOTICE);
  });

  it("says Runtime accepted it when the host said so", () => {
    expect(decide({ submission: "accepted" }).notice).toBe(ACCEPTED_NOTICE);
  });

  it("names the refusal when the host refused with a known code", () => {
    const d = decide({
      submission: "rejected",
      refusalKind: "task_reply_expired",
    });
    expect(d.notice).toBe(refusalCopy("task_reply_expired"));
    expect(d.notice).not.toContain("task_reply_expired");
  });

  it("still speaks when the refusal carries no code", () => {
    const d = decide({ submission: "rejected" });
    expect(d.notice).toBe(refusalCopy(""));
    expect((d.notice ?? "").trim().length).toBeGreaterThan(0);
  });

  it("reports an unanswered transport as unknown, not as an outcome", () => {
    const d = decide({ submission: "idle", transportFailed: true });
    expect(d.submission).toBe("failed");
    expect(d.notice).toBe(TRANSPORT_UNKNOWN_NOTICE);
    expect(d.notice).not.toContain("已取消");
    expect(d.notice).not.toContain("已回答");
  });

  it("keeps an authoritative answer over the transport flag", () => {
    expect(
      decide({ submission: "accepted", transportFailed: true }).submission,
    ).toBe("accepted");
    expect(
      decide({
        submission: "rejected",
        refusalKind: "task_input_invalid",
        transportFailed: true,
      }).submission,
    ).toBe("rejected");
    expect(decide({ submission: "failed" }).submission).toBe("failed");
  });

  it("says nothing about a submission that was never made", () => {
    expect(decide().notice).toBeNull();
    expect(decide().pending).toBe(false);
  });
});

describe("decideActiveInput — no optimism", () => {
  it("claims no task outcome, answered clarification, or cancellation", () => {
    // Those are projection facts: the surfaces that render them read the
    // host's own values, so the decision must not carry a local guess that
    // a later caller could mistake for authority.
    expect(Object.keys(decide({ submission: "accepted" })).sort()).toEqual([
      "held",
      "holdCopy",
      "holdReason",
      "notice",
      "pending",
      "submission",
    ]);
  });
});

describe("sendControlLabel", () => {
  it("names the hold while the input is held", () => {
    expect(
      sendControlLabel({ held: true, holdReason: "pending_choice", runInFlight: true }),
    ).toBe("等待选择…");
    expect(
      sendControlLabel({ held: true, holdReason: "upload_in_flight", runInFlight: false }),
    ).toBe("上传中…");
  });

  it("keeps a live run legible without disabling the send", () => {
    expect(
      sendControlLabel({ held: false, holdReason: null, runInFlight: true }),
    ).toBe("运行中…");
    expect(
      sendControlLabel({ held: false, holdReason: null, runInFlight: false }),
    ).toBe("发送");
  });
});

describe("heldRefusalFor", () => {
  it("refuses a held send with the typed exit the send route already answers", () => {
    // The control and the store refuse with the same pair, so a refusal
    // reaching the notice path is classified the same way from either side.
    expect(heldRefusalFor("pending_choice")).toBeInstanceOf(ConcurrentRunError);
    expect(heldRefusalFor("upload_in_flight")).toBeInstanceOf(
      AttachmentUploadInProgressError,
    );
  });
});

describe("refusalCopy", () => {
  const NAMED_TOKENS = [
    "task_reply_expired",
    "task_reply_unknown",
    "task_restore_unknown",
    "task_restore_not_adopted",
    "task_permission_denied",
    "task_execution_limit",
    "task_input_invalid",
  ];

  it("maps each named token to a distinct non-empty line, never the raw token", () => {
    const seen = new Set<string>();
    for (const token of NAMED_TOKENS) {
      const copy = refusalCopy(token);
      expect(copy.trim().length, token).toBeGreaterThan(0);
      expect(copy, token).not.toBe(token);
      expect(seen.has(copy), `duplicate copy for ${token}`).toBe(false);
      seen.add(copy);
    }
    expect(seen.size).toBe(NAMED_TOKENS.length);
  });

  it("gives an unmapped token a non-empty generic line, never blank", () => {
    const copy = refusalCopy("task_teleporter_broken");
    expect(copy.trim().length).toBeGreaterThan(0);
    expect(copy).not.toContain("task_teleporter_broken");
  });

  it("never turns an operational failure into a clarification question", () => {
    // Contract §Named failures red line: a system outcome is not a
    // question. Mechanical check across every named token and a generic
    // fallback.
    for (const token of [...NAMED_TOKENS, "task_something_unknown"]) {
      const copy = refusalCopy(token);
      expect(copy, token).not.toContain("?");
      expect(copy, token).not.toContain("？");
      expect(copy, token).not.toContain("吗");
    }
  });
});
