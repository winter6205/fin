// Clarification-reply semantics, kept pure so the reply boundary and
// its association rules are testable without a DOM (same split as
// choiceWindow.ts).
//
// The task-runtime seam (specs/question-task-runtime-contract.md
// §Representation) makes Runtime the single writer of the task
// envelope: a submitted reply is a REQUEST, and only Runtime's
// acceptance decides what the workspace shows. This module holds the
// local half the spec fixes — the complete question before its
// options, the Other option, the association of a reply with the
// question it answers, and the safety rules for dismissal, retry, and
// a stale rejection (specs/question-workspace.md §"Clarification and
// original input", SC-3 and SC-4).
//
// Nothing here mutates a draft in place: each function returns a new
// draft, or the same one on a no-op, so a component can hold the prior
// value and the operator's draft is never silently rewritten.

import type { PendingQuestionView } from "../../types";

/** ``fin.host.task.task_runtime_types.OTHER_CHOICE_ID``. The id is a
 * RESERVED token, not a generated one, so a reply is recognised as
 * free text without this surface guessing which option it picked
 * (question-task-runtime-contract.md §"Closed vocabularies": Other is
 * exactly one, id ``other``, always trailing). */
export const OTHER_CHOICE_ID = "other";

export interface ClarificationDraft {
  /** The question this draft answers — its ORIGINAL association. */
  questionId: string;
  /** The complete question, verbatim from the host. */
  prompt: string;
  /** Every offered choice, host order, including the trailing Other. */
  choices: string[];
  /** null = nothing chosen. NEVER preselected. */
  selectedChoiceId: string | null;
  /** Free text that lives INSIDE the selected Other option. */
  otherText: string;
  /** Presentation-only collapse. Never an answer. */
  dismissed: boolean;
}

/** Open a draft for a pending question. Nothing is preselected: the
 * spec's "Do not preselect an answer" (question-workspace.md) means
 * the operator must make the first choice, so ``selectedChoiceId``
 * starts ``null`` and no default option can be read as an accepted
 * one. ``choices`` is copied so a caller cannot mutate the wire object
 * through the draft. */
export function openClarification(
  question: PendingQuestionView,
): ClarificationDraft {
  return {
    questionId: question.question_id,
    prompt: question.prompt,
    choices: [...question.choices],
    selectedChoiceId: null,
    otherText: "",
    dismissed: false,
  };
}

/** The listed choices for the option list — every choice except the
 * reserved Other token, which the surface renders separately as
 * ``CLARIFICATION_OTHER_LABEL``. ``other`` is reserved (no listed
 * choice can carry it), so dropping every occurrence is safe and a
 * host that sent a duplicate cannot leak a second Other into the
 * options. */
export function listedChoices(choices: readonly string[]): string[] {
  return choices.filter((choiceId) => choiceId !== OTHER_CHOICE_ID);
}

/** Whether the host's list carries the Other option. The host contract
 * guarantees exactly one trailing Other
 * (question-task-runtime-contract.md §"Closed vocabularies"), so two
 * ``other`` tokens is a host fault. This surface reports Other ONCE,
 * and ``listedChoices`` drops every ``other`` entry, so a duplicate is
 * never amplified into two Other buttons. */
export function hasOtherChoice(choices: readonly string[]): boolean {
  return choices.some((choiceId) => choiceId === OTHER_CHOICE_ID);
}

/** Whether the host offered NO listed option: the reserved Other is the
 * only choice. The host publishes exactly this shape when it has nothing
 * real to offer, and then the typed answer IS the reply — requiring a
 * click on Other first would be a step that picks nothing. */
export function isFreeTextOnly(choices: readonly string[]): boolean {
  return choices.length > 0 && listedChoices(choices).length === 0;
}

/** The Other option's label. The wire uses the reserved ``other`` id;
 * the surface shows this Chinese label beside the listed choices,
 * because Other is an option the operator may choose
 * (question-workspace.md "choose an offered option or select Other"). */
export const CLARIFICATION_OTHER_LABEL = "其他";

/** Apply an operator pick. A pick the host did not offer leaves the
 * draft untouched — this surface never invents an option.
 *
 * The free text lives INSIDE the Other option ("free text appears
 * inside the selected Other option"), so moving to a listed choice
 * clears it: carrying the typed text into a listed pick would misreport
 * what the operator chose. Re-selecting Other keeps the text, because
 * re-selecting must not destroy a draft mid-typing.
 *
 * Any pick also clears ``dismissed``: choosing from a collapsed card is
 * an explicit action, and the answer must not stay hidden behind a
 * presentation flag. */
export function selectChoice(
  draft: ClarificationDraft,
  choiceId: string,
): ClarificationDraft {
  if (!draft.choices.includes(choiceId)) return draft;
  return {
    ...draft,
    selectedChoiceId: choiceId,
    otherText: choiceId === OTHER_CHOICE_ID ? draft.otherText : "",
    dismissed: false,
  };
}

/** Write the free text. It is accepted only under the Other option: free
 * text has no meaning outside it (question-workspace.md "free text appears
 * inside the selected Other option"), so a write under a listed selection
 * is inert rather than parking text that would later be submitted against
 * a listed choice. When Other is the ONLY choice there is no pick to make
 * first, so the text is accepted with nothing selected. */
export function setOtherText(
  draft: ClarificationDraft,
  text: string,
): ClarificationDraft {
  if (
    draft.selectedChoiceId !== OTHER_CHOICE_ID &&
    !isFreeTextOnly(draft.choices)
  ) {
    return draft;
  }
  return { ...draft, otherText: text };
}

/** Dismissal is presentation-only: it "supplies no default answer",
 * does not answer or cancel the task, and leaves the question
 * recoverable from authoritative state (question-workspace.md). It
 * therefore flips only ``dismissed`` — the selection and the typed text
 * stay, so re-opening the card finds the same draft. */
export function dismissClarification(
  draft: ClarificationDraft,
): ClarificationDraft {
  return { ...draft, dismissed: true };
}

/** The exact inverse of ``dismissClarification``: re-opening a collapsed
 * card is presentation-only too, so it flips only ``dismissed`` and
 * never fills ``selectedChoiceId`` — picking the first listed choice
 * would preselect an answer the operator never made (SC-3). A no-op
 * on a card that is already open. */
export function reopenClarification(
  draft: ClarificationDraft,
): ClarificationDraft {
  if (!draft.dismissed) return draft;
  return { ...draft, dismissed: false };
}

/** A submittable reply names a choice, and an Other reply must carry
 * non-blank free text. An empty Other would be refused by the host as
 * ``task_input_invalid`` (TASK_FAILURE_KINDS), and a POST the host is
 * known to reject is worse than a disabled button. Text is judged by
 * ``trim`` here, but the payload below keeps it verbatim.
 *
 * A free-text-only question offers no option to select, so its non-blank
 * text alone is the complete answer. */
export function isSubmittable(draft: ClarificationDraft): boolean {
  if (draft.selectedChoiceId === OTHER_CHOICE_ID) {
    return draft.otherText.trim() !== "";
  }
  if (draft.selectedChoiceId === null) {
    return isFreeTextOnly(draft.choices) && draft.otherText.trim() !== "";
  }
  return true;
}

export interface ClarificationSubmission {
  questionId: string;
  choiceId: string;
  freeText: string | null;
}

/** The reply to POST. ``null`` while not submittable mirrors the
 * disabled button, so no caller can send a request the host is known to
 * refuse. The ``questionId`` always rides the payload — that
 * association is the whole safety property of SC-3/SC-4: Runtime can
 * tell whether the question still applies before it accepts. ``freeText``
 * is the verbatim Other text and ``null`` for a listed choice, matching
 * ``QuestionReply`` ("free text stays beside the reserved Other
 * token").
 *
 * This is the ONLY reply builder, which is what makes a retry identical:
 * a refused or transport-failed draft keeps its choice, its text and its
 * question above, so resending produces the same request rather than a
 * near-match. A second retry entry point could drift from this one and
 * quietly break that guarantee.
 *
 * A free-text-only question submits the reserved Other id: the host's
 * only choice is that token, and its non-blank text rides ``freeText``
 * exactly as a selected Other would. */
export function submitPayload(
  draft: ClarificationDraft,
): ClarificationSubmission | null {
  if (!isSubmittable(draft)) return null;
  const choiceId =
    draft.selectedChoiceId ??
    (isFreeTextOnly(draft.choices) ? OTHER_CHOICE_ID : null);
  if (choiceId === null) return null;
  return {
    questionId: draft.questionId,
    choiceId,
    freeText: choiceId === OTHER_CHOICE_ID ? draft.otherText : null,
  };
}

export type ReplyOutcome = "stale" | "unknown_question" | "rejected";

export interface ReplyRejection {
  kind: string;
  questionId: string | null;
}

/** Map a host refusal to what the workspace shows.
 * ``task_reply_expired`` means the reply's question is no longer
 * pending (stale); ``task_reply_unknown`` means the host never knew the
 * question. Every other ``TASK_FAILURE_KINDS`` token is a plain
 * rejection, and a blank or unrecognised kind reads ``rejected`` rather
 * than crashing — a refusal must always render something. */
export function replyOutcome(refusal: ReplyRejection): ReplyOutcome {
  switch (refusal.kind) {
    case "task_reply_expired":
      return "stale";
    case "task_reply_unknown":
      return "unknown_question";
    default:
      return "rejected";
  }
}

/** Copy after a stale rejection: the reply keeps its original question
 * and the current state governs the display (SC-4). The operator's
 * draft is preserved, so the sentence also says it is safe to
 * resubmit. */
export const CLARIFICATION_DRAFT_RESTORED_COPY =
  "这条回复仍然属于它原来的问题；界面显示以当前状态为准。你的选择和草稿已保留，可以重新提交。";

export interface DraftAfterRefusal {
  draft: ClarificationDraft;
  outcome: ReplyOutcome;
  /** True when the host's refusal named a DIFFERENT question than this draft's. */
  rebased: boolean;
}

/** Reconcile a host refusal with the local draft. Three rules, each a
 * spec sentence (question-workspace.md §"Clarification and original
 * input", SC-4):
 *
 * 1. The draft's ``questionId`` never changes. A stale reply "does not
 *    mutate, reactivate, or answer another task" and is "never silently
 *    rebase[d]... onto a new question", so the association stays put.
 *    ``rebased`` is therefore always false here, and is returned
 *    explicitly so no caller has to infer it.
 * 2. The selected choice and ``otherText`` survive verbatim: the spec
 *    requires preserving "the user's draft".
 * 3. ``dismissed`` is cleared so the operator SEES the failed reply
 *    again. A refusal is not a collapse. */
export function applyRefusal(
  draft: ClarificationDraft,
  refusal: ReplyRejection,
): DraftAfterRefusal {
  return {
    draft: { ...draft, dismissed: false },
    outcome: replyOutcome(refusal),
    rebased: false,
  };
}

// ----- draft durability -----

/** ``localStorage`` key the operator's clarification drafts live under.
 * Named here so the writer (the store's persist wiring) and a future
 * reader cannot drift apart on a string. */
export const CLARIFICATION_DRAFT_STORAGE_KEY = "fin.clarificationDrafts";

/** The recoverable half of a draft: the pick and the typed text. The
 * prompt, the choices and their order are AUTHORITATIVE host facts, so
 * storing them here would keep a second, staler copy of them. The
 * record's key in ``ClarificationDraftRecords`` is the question it
 * belongs to — that is the whole association. */
export interface ClarificationDraftRecord {
  selectedChoiceId: string | null;
  otherText: string;
}

export type ClarificationDraftRecords = Record<string, ClarificationDraftRecord>;

/** The slice to persist, keyed by ``questionId`` — the same key the
 * store's draft map uses, so a reload cannot shift an answer onto a
 * different question. */
export function draftRecords(
  drafts: Record<string, ClarificationDraft>,
): ClarificationDraftRecords {
  const records: ClarificationDraftRecords = {};
  for (const [questionId, draft] of Object.entries(drafts)) {
    records[questionId] = {
      selectedChoiceId: draft.selectedChoiceId,
      otherText: draft.otherText,
    };
  }
  return records;
}

function isDraftRecordEntry(value: unknown): value is ClarificationDraftRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const entry = value as Record<string, unknown>;
  const choice = entry.selectedChoiceId;
  return (
    (choice === null || typeof choice === "string") &&
    typeof entry.otherText === "string"
  );
}

/** Read a persisted draft map back. Anything unreadable drops out
 * silently: a corrupted or foreign entry must not become an answer the
 * operator never picked, and it must never take the reload down with it. */
export function parseDraftRecords(value: unknown): ClarificationDraftRecords {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return {};
  }
  const records: ClarificationDraftRecords = {};
  for (const [questionId, entry] of Object.entries(value)) {
    if (!isDraftRecordEntry(entry)) continue;
    records[questionId] = {
      selectedChoiceId: entry.selectedChoiceId,
      otherText: entry.otherText,
    };
  }
  return records;
}

/** Rebuild the draft for a question the projection just reported.
 *
 * The question is the input, never the record, so a recovered pick can
 * only land on the question it was looked up under — the association
 * survives a reload without any rebinding logic here. A pick the host no
 * longer offers is dropped rather than sent: the host would refuse it as
 * ``task_input_invalid``, and a restored answer the operator cannot see
 * is worse than a blank card.
 *
 * ``dismissed`` is deliberately never restored: a reload that came back
 * with the pending question folded away would hide the one thing the
 * operator has to answer. */
export function restoreDraftText(
  question: PendingQuestionView,
  record: ClarificationDraftRecord | null | undefined,
): ClarificationDraft {
  const fresh = openClarification(question);
  if (record === undefined || record === null) return fresh;
  if (record.selectedChoiceId === null) {
    // A free-text-only draft has no selection to restore — the text is the
    // whole answer — so it is recovered under the same question id.
    if (!isFreeTextOnly(question.choices)) return fresh;
    return setOtherText(fresh, record.otherText);
  }
  if (!question.choices.includes(record.selectedChoiceId)) return fresh;
  const chosen = selectChoice(fresh, record.selectedChoiceId);
  if (record.selectedChoiceId !== OTHER_CHOICE_ID) return chosen;
  return setOtherText(chosen, record.otherText);
}
