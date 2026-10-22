// The document-choice render surface (GE-4).
//
// A ``document_choice`` is NOT an ordinary task clarification, even though
// both are pending questions on the same projection and both are answered
// through the same reply door. Two wire facts force a different surface:
//
// 1. GE-4 (specs/qa-chain-grounded-experience.md:96) requires each
//    candidate to be rendered as "title/section/summary". A task
//    clarification's ``choices`` ARE their labels, so
//    ``ClarificationCard`` prints them verbatim. A document choice's
//    ``choices`` are opaque ``choice_id``s (``choice-1``, ``choice-2``)
//    and the human-readable text lives in ``choice_details``. Rendering
//    the ids would show the operator "choice-1" instead of the document
//    they are being asked to pick, so this surface reads the details.
// 2. The trailing Other is the RE-PHRASE action, not an "other answer":
//    the frozen contract label is 换个问法 ("ask it another way"), not the
//    clarification card's 其他. Picking it submits free text and names no
//    candidate, which is exactly the trailing-``other`` half of GE-4.
//
// The candidate authority is ``task_runtime.pending_question`` and
// nowhere else. The SSE ``document_choice`` frame carries NO
// ``choice_details`` — it is a reference naming the question and the run
// it interrupted — so this surface reads the projection the store re-reads
// on that frame, which is also why a reload paints the identical
// question (GE-4's "browser reload … preserves the original run").
//
// Pure and total, like every other render-decision module in this
// directory (clarificationReply.ts / clarificationRegion.ts): it takes the
// decoded question plus the operator's draft and returns the resolved
// view, or ``null`` when there is nothing of this kind to render. It
// never writes, never sends, and never infers a host fact — the answer
// still belongs to the reply door.

import {
  documentChoiceView,
  isDocumentChoice,
} from "../task/taskRuntime";
import { OTHER_CHOICE_ID, isSubmittable } from "./clarificationReply";
import type { ClarificationDraft } from "./clarificationReply";
import type { PendingQuestionView } from "../../types";

/** One candidate row as GE-4 words it: the document's title, the section
 * the admitted span came from (absent when the whole source was
 * admitted), and the one-line summary. ``choice_id`` is never shown —
 * it rides the pick. */
export interface DocumentChoiceRow {
  choice_id: string;
  title: string;
  section: string | null;
  summary: string;
  /** True while the operator has this candidate selected. Nothing is
   * preselected (question-workspace.md "Do not preselect an answer"), so
   * this is false until an actual click. */
  selected: boolean;
}

/** The rephrase action: the trailing ``other``, rendered with the
 * contract's own label. */
export interface DocumentChoiceRephrase {
  /** The reserved wire id — what a submitted pick-free reply carries as
   * the Other selection with the free text beside it. */
  choice_id: string;
  label: string;
  selected: boolean;
  /** The typed re-phrase. Present ONLY while rephrase is selected, so
   * text can never be attached to a candidate pick (the same rule the
   * clarification card follows for its Other box). */
  text: string | null;
}

/** The whole card: the question verbatim, its hit-backed candidates, and
 * the trailing rephrase. */
export interface DocumentChoiceSurface {
  question_id: string;
  /** The run this choice interrupted. GE-4's "preserves the original
   * run/receipt": the question names the run it belongs to even when the
   * host published it with an empty ``task_id``. */
  origin_run_id: string;
  prompt: string;
  rows: DocumentChoiceRow[];
  rephrase: DocumentChoiceRephrase;
  /** True while the reply is on the wire. */
  submitting: boolean;
  /** Whether the submit control is enabled: a draft that names a choice,
   * and non-blank text when the rephrase is the selection. */
  submittable: boolean;
}

/** Resolve the document-choice card, or ``null`` when this question is
 * not a document choice.
 *
 * ``null`` is the load-bearing answer for an ordinary task clarification:
 * the caller then renders ``ClarificationCard`` instead, so an
 * unrecognized or legacy question can never acquire document-choice
 * control state it does not have (GE-4's "other threads receive no
 * control state"). */
export function documentChoiceSurface(
  question: PendingQuestionView | null,
  draft: ClarificationDraft | null,
  submitting: boolean,
): DocumentChoiceSurface | null {
  const view = documentChoiceView(question);
  if (view === null || !isDocumentChoice(question)) return null;
  const selectedId = draft?.selectedChoiceId ?? null;
  const rows: DocumentChoiceRow[] = view.candidates.map((candidate) => ({
    choice_id: candidate.choice_id,
    title: candidate.label,
    section: candidate.section,
    summary: candidate.summary,
    selected: selectedId === candidate.choice_id,
  }));
  const rephraseSelected = selectedId === OTHER_CHOICE_ID;
  return {
    question_id: view.question_id,
    origin_run_id: view.origin_run_id,
    prompt: view.prompt,
    rows,
    rephrase: {
      choice_id: OTHER_CHOICE_ID,
      label: view.otherLabel,
      selected: rephraseSelected,
      text: rephraseSelected ? (draft?.otherText ?? "") : null,
    },
    submitting,
    // The same submittability rule as the clarification card, so a click
    // that the host would refuse (``task_input_invalid`` on empty
    // rephrase text) cannot be sent from either surface.
    submittable: draft !== null && isSubmittable(draft),
  };
}
