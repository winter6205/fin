// Clarification-region selection: which operation facts belong to the
// question the card is rendering, kept pure so the association rules are
// testable without a DOM (same split as clarificationReply.ts).
//
// The ledger keeps at most the CURRENT operation of each thread
// (workspaceOperation.ts), while the card renders the thread's current
// pending question. Reading those two fields directly therefore paints an
// unrelated operation onto the question in front of the operator: a restore
// in flight disables the options, a reply for another question reads as this
// card's pending answer, and a refusal of an older question prints its
// rejection over a newer one (SC-4's exact prohibition).
//
// The rules selected here are the spec's (specs/question-workspace.md
// §"Clarification and original input"):
//
// 1. A reply on the wire is a REQUEST. It is never an accepted or
//    answered clarification, so ``answeredQuestionId`` is filled only
//    from the authoritative projection.
// 2. Acceptance is the projection's word, and about ONE question: the
//    question must be gone from it, otherwise the host still asks.
// 3. A refusal belongs to the question that was SENT (the submission's
//    own association), and only to that question. The refused draft
//    stays reachable under its original id and is never handed to a
//    newer question's card.
//
// Nothing here writes, sends, or infers a host fact: dismissal and
// collapse live in the draft, and this module cannot turn a local pick
// into a claim about Runtime.

import { replyOutcome } from "./clarificationReply";
import type { ClarificationDraft, ReplyOutcome } from "./clarificationReply";
import type { WorkspaceOperation, WorkspaceOperationClass } from "./workspaceOperation";
import type { PendingQuestionView } from "./types";

/** The last refusal the thread's current operation carries, with the door
 * that recorded it. A failure kind is only meaningful against its own
 * door's copy, so the door travels with the kind rather than being guessed
 * at. */
export interface ClarificationRefusalFact {
  kind: string;
  questionId: string | null;
  door: WorkspaceOperationClass;
}

export interface ClarificationRegionInput {
  /** The question the authoritative projection reports as pending. */
  pendingQuestion: PendingQuestionView | null;
  /** Every draft the operator has open, keyed by its own question id. */
  drafts: Record<string, ClarificationDraft>;
  /** The open thread's current operation, or null when it has none. */
  submission: WorkspaceOperation | null;
  refusal: ClarificationRefusalFact | null;
}

/** What the pending clarification card may render. */
export interface ClarificationCardFacts {
  /** The draft belonging to the CURRENT question, or null when there is
   * no question or no draft was opened for it yet. */
  draft: ClarificationDraft | null;
  /** True only while a reply for THIS question is on the wire. */
  submitting: boolean;
  /** The refusal kind belonging to THIS question, or null. */
  refusalKind: string | null;
}

/** A refused reply whose question is no longer the one on screen. Kept
 * so the surface can still show it with its own draft and its own copy. */
export interface RefusedClarification {
  draft: ClarificationDraft;
  refusalKind: string;
  outcome: ReplyOutcome;
}

export interface ClarificationRegion {
  card: ClarificationCardFacts;
  refused: RefusedClarification | null;
  /** The question the projection proved answered, or null. A pending,
   * refused, or transport-unknown reply never fills it. */
  answeredQuestionId: string | null;
}

/** The question a REPLY-door submission in this state was made against,
 * or null. The ``kind`` check is what keeps a pending restore off the
 * card, and the id is what keeps another question's reply off it: question
 * ids are the host's, so an id match is the association. */
function replyQuestionFor(
  submission: WorkspaceOperation | null,
  state: WorkspaceOperation["state"],
): string | null {
  if (submission === null) return null;
  if (submission.kind !== "reply") return null;
  if (submission.state !== state) return null;
  return submission.questionId;
}

function replyDoorRefusalKind(
  refusal: ClarificationRefusalFact | null,
): string | null {
  if (refusal === null) return null;
  return refusal.door === "reply" ? refusal.kind : null;
}

/** The detached form of a refusal: the refused question's own draft and the
 * outcome its id still reads as. */
function refusedClarification(
  questionId: string,
  drafts: Record<string, ClarificationDraft>,
  refusalKind: string,
): RefusedClarification | null {
  const draft = drafts[questionId];
  // A refusal with no draft to attach to is a notice about nothing: the
  // operator's answer is gone, so there is no association to preserve.
  if (draft === undefined || draft.questionId !== questionId) return null;
  return {
    draft,
    refusalKind,
    outcome: replyOutcome({ kind: refusalKind, questionId }),
  };
}

/** Resolve the clarification region from the projection facts it is
 * handed. Pure and total: every missing or mismatched input reads as
 * "nothing to claim", never as an answer. The refusal has exactly two
 * homes — the card of the question it was made against, or a detached
 * refused reply — so those two dispositions are decided apart. */
export function clarificationRegion(
  input: ClarificationRegionInput,
): ClarificationRegion {
  const current = input.pendingQuestion?.question_id ?? null;
  const refusalKind = replyDoorRefusalKind(input.refusal);
  return {
    card: cardFactsFor(input, current, refusalKind),
    refused: detachedRefusalFor(input, current, refusalKind),
    answeredQuestionId: answeredQuestionFor(input, current),
  };
}

/** What the card on screen may claim: its own draft, whether a reply for
 * THIS question is on the wire, and only the refusal belonging to it. */
function cardFactsFor(
  input: ClarificationRegionInput,
  current: string | null,
  refusalKind: string | null,
): ClarificationCardFacts {
  const pendingReply = replyQuestionFor(input.submission, "pending");
  const rejectedReply = replyQuestionFor(input.submission, "rejected");
  const onThisCard =
    refusalKind !== null && rejectedReply !== null && rejectedReply === current;
  return {
    draft: current === null ? null : (input.drafts[current] ?? null),
    submitting: pendingReply !== null && pendingReply === current,
    refusalKind: onThisCard ? refusalKind : null,
  };
}

/** The refusal that belongs to a question OTHER than the one on screen, or
 * null when there is none to detach. A refusal for the current question is
 * the card's own and must not also appear detached. */
function detachedRefusalFor(
  input: ClarificationRegionInput,
  current: string | null,
  refusalKind: string | null,
): RefusedClarification | null {
  const rejectedReply = replyQuestionFor(input.submission, "rejected");
  if (
    refusalKind === null ||
    rejectedReply === null ||
    rejectedReply === current
  ) {
    return null;
  }
  return refusedClarification(rejectedReply, input.drafts, refusalKind);
}

/** Acceptance is the projection's word about ONE question: the reply the
 * host accepted, and only once that question is no longer pending. */
function answeredQuestionFor(
  input: ClarificationRegionInput,
  current: string | null,
): string | null {
  const acceptedReply = replyQuestionFor(input.submission, "accepted");
  return acceptedReply !== null && acceptedReply !== current
    ? acceptedReply
    : null;
}
