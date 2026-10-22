// The single active-input decision.
//
// One pure function answers both questions the shipped surfaces used to
// answer separately: may the operator send right now, and what is the
// submission's current state worth saying. The Composer's control, its
// submit handler and the store's send guard all read this one result, so
// the two halves of one door cannot disagree (a disabled box plus a
// silently swallowed POST is the failure mode this exists to prevent).
//
// It holds no state and reads no transport: every value is a fact handed
// in by the caller. The load-bearing rule (specs/question-workspace.md
// §"Active work, outcomes, and history") is that a turn ending is not a
// task outcome, so nothing here returns a task, clarification, or
// cancellation claim — those belong to the authoritative projection and
// are read from it directly, never derived from a submission.

import {
  AttachmentUploadInProgressError,
  ConcurrentRunError,
} from "./api";
import { ATTACHMENT_UPLOAD_IN_PROGRESS_COPY } from "./attachFailure";

/**
 * The submission states specs/question-workspace.md §"Input ordering and
 * module seams" requires the workspace to keep apart: sent but unresolved
 * (`pending`), and the three authoritative endings — `accepted`, `rejected`,
 * and `failed` for a transport failure that produced no answer at all
 * (unknown). Each carries its own notice; none is inferred from local
 * intent.
 */
export type SubmissionState =
  | "idle"
  | "pending"
  | "accepted"
  | "rejected"
  | "failed";

/**
 * The only two reasons input is held. An active run is deliberately NOT a
 * member: compatible follow-up input stays available while work is running
 * (SC-5), and the host — not this client — decides whether that input is
 * queued, accepted, or interrupted.
 */
export type HoldReason = "pending_choice" | "upload_in_flight";

// ----- held-input copy: a HOST lock, not an inference -----

/** A paused choice window holds the thread on the host side: the run is
 * suspended on the checkpoint awaiting the operator's pick, and the send
 * route answers 409 `concurrent_run` while it waits. Holding here keeps a
 * doomed POST off the wire. */
export const PENDING_CHOICE_HELD_COPY =
  "有一轮正在等你的选择，这个对话现在不能发送新的输入。";

/** The attach door is mid-upload, so a send is refused before it leaves the
 * box. The sentence is the attach door's own (attachFailure.ts is its single
 * source) so the upload refusal cannot be named two different ways. */
export const UPLOAD_IN_FLIGHT_HELD_COPY = ATTACHMENT_UPLOAD_IN_PROGRESS_COPY;

const HELD_COPY: Readonly<Record<HoldReason, string>> = {
  pending_choice: PENDING_CHOICE_HELD_COPY,
  upload_in_flight: UPLOAD_IN_FLIGHT_HELD_COPY,
};

// The control's short line for the same two locks. Copy, not policy: which
// lock holds is decided above, here it is only named on the button.
const HELD_CONTROL_LABEL: Readonly<Record<HoldReason, string>> = {
  pending_choice: "等待选择…",
  upload_in_flight: "上传中…",
};

// ----- submission notices: the one production notice path -----

/** The operator's submission is on the wire and not yet resolved. */
export const SUBMITTING_NOTICE = "正在提交，等 Runtime 的答复。";

/** Runtime accepted the input. */
export const ACCEPTED_NOTICE = "Runtime 已经接受了这次输入。";

/** A transport failure produced no authoritative answer, so the result is
 * UNKNOWN: the copy claims neither a cancellation nor an answered
 * clarification (spec §"Clarification and original input"). */
export const TRANSPORT_UNKNOWN_NOTICE =
  "没能收到 Runtime 的答复，结果未知：这次输入可能还没有生效，当前任务可能保持不变。";

// The contract's named task failures (fin.host.task.task_runtime_types.
// TASK_FAILURE_KINDS) and the copy each deserves. Red line (contract
// §Named failures): an operational failure is a SYSTEM outcome and is
// never converted into a clarification — so none of these strings asks
// the operator anything (no `?` / `？` / `吗`).
const REFUSAL_COPY: Readonly<Record<string, string>> = {
  task_reply_expired: "这条澄清回复已经过期，Runtime 没有接受。",
  task_reply_unknown: "这条澄清回复没有找到对应的问题，Runtime 没有接受。",
  task_restore_unknown: "要恢复的归档任务不存在或已不可恢复，这次恢复没有执行。",
  task_restore_not_adopted: "这次恢复没有被 Runtime 采纳，当前任务保持不变。",
  task_permission_denied: "当前权限不允许这次输入，Runtime 已经拒绝。",
  task_execution_limit: "这次输入到达了运行上限，Runtime 已经停止处理。",
  task_input_invalid: "这次输入不合法，Runtime 没有接受。",
};

const GENERIC_REFUSAL = "这次输入没有被 Runtime 接受。";

/** Chinese refusal copy for a named task-failure token. An unmapped token
 * still gets a non-empty generic line (same posture as attachFailure.ts),
 * so a newer host can never produce a blank notice. */
export function refusalCopy(kind: string): string {
  return REFUSAL_COPY[kind] ?? GENERIC_REFUSAL;
}

export interface ActiveInputFacts {
  /** The host holds this thread on a choice window awaiting the operator.
   * Projection-derived: the in-memory turn table AND a reloaded
   * ``pending_turn``, not a client guess. */
  pendingChoiceWindow: boolean;
  /** A composer upload POST is in flight, so no send can reference it yet. */
  uploadInFlight: boolean;
  /** The submission state of the operation the caller is reporting. */
  submission: SubmissionState;
  /** A transport/network failure: no authoritative answer arrived at all. */
  transportFailed: boolean;
  /** The host's named task refusal, when the submission was refused. */
  refusalKind: string | null;
}

export interface ActiveInputDecision {
  /** Whether a send is refused right now, by a host lock or an upload. */
  held: boolean;
  /** Which lock holds it, or null. */
  holdReason: HoldReason | null;
  /** The full sentence naming that lock, or null. */
  holdCopy: string | null;
  submission: SubmissionState;
  /** True only while the submission is still unresolved. */
  pending: boolean;
  /** What the operator is told about this submission, or null when idle. */
  notice: string | null;
}

/** Which lock wins when several hold. The choice window is the more
 * specific reason and the one the operator can act on. */
function holdReasonOf(facts: ActiveInputFacts): HoldReason | null {
  if (facts.pendingChoiceWindow) return "pending_choice";
  if (facts.uploadInFlight) return "upload_in_flight";
  return null;
}

/** An authoritative answer outranks the transport flag because there the
 * host actually spoke; anything else the transport broke reads as
 * unknown. */
function submissionOf(facts: ActiveInputFacts): SubmissionState {
  const spoke = facts.submission === "accepted" || facts.submission === "rejected";
  if (facts.submission === "failed" || (facts.transportFailed && !spoke)) {
    return "failed";
  }
  return facts.submission;
}

function noticeOf(
  submission: SubmissionState,
  refusalKind: string | null,
): string | null {
  switch (submission) {
    case "pending":
      return SUBMITTING_NOTICE;
    case "accepted":
      return ACCEPTED_NOTICE;
    case "rejected":
      return refusalCopy(refusalKind ?? "");
    case "failed":
      return TRANSPORT_UNKNOWN_NOTICE;
    default:
      return null;
  }
}

/** The one active-input decision. Callers hand in facts and render or
 * refuse from the result; nothing here reads the store, the projection, or
 * the transport, and nothing here claims a task outcome. */
export function decideActiveInput(facts: ActiveInputFacts): ActiveInputDecision {
  const holdReason = holdReasonOf(facts);
  const submission = submissionOf(facts);
  return {
    held: holdReason !== null,
    holdReason,
    holdCopy: holdReason === null ? null : HELD_COPY[holdReason],
    submission,
    pending: submission === "pending",
    notice: noticeOf(submission, facts.refusalKind),
  };
}

/** The send button's line: the lock's name while held, otherwise whether
 * a run is visible. A run reads here as status only — it never holds. */
export function sendControlLabel(facts: {
  held: boolean;
  holdReason: HoldReason | null;
  runInFlight: boolean;
}): string {
  if (facts.held && facts.holdReason !== null) {
    return HELD_CONTROL_LABEL[facts.holdReason];
  }
  return facts.runInFlight ? "运行中…" : "发送";
}

/** The typed refusal matching a hold, so the store's send guard and the
 * composer's control refuse the same way and the notice path classifies
 * one shared exit. */
export function heldRefusalFor(reason: HoldReason): Error {
  return reason === "upload_in_flight"
    ? new AttachmentUploadInProgressError()
    : new ConcurrentRunError("concurrent_run");
}
