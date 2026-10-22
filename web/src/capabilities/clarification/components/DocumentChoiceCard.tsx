// The document-choice card (specs/qa-chain-grounded-experience.md §GE-4).
//
// GE-4 (specs/qa-chain-grounded-experience.md:96) — "≤4 hit-backed
// candidates have title/section/summary and trailing rephrase". This is
// the only place that promise is rendered, and it is deliberately NOT
// ClarificationCard: that card prints ``draft.choices`` verbatim, which is
// right for a task clarification (its choices ARE the labels) and wrong
// here (a document choice's choices are opaque ``choice-1`` ids while the
// readable text lives in ``choice_details``).
//
// Rendered from props by ``DocumentChoiceCardView`` and wrapped below with
// the store's own actions, so the render decision is testable without a DOM
// — the same split as components/ClarificationCard.tsx.

import { useWorkspaceStore } from "../../../store";
import { refusalCopy } from "../../../activeInput";
import type { DocumentChoiceSurface } from "../documentChoiceSurface";

/** What the card renders, all of it resolved by the caller. */
export interface DocumentChoiceCardViewProps {
  surface: DocumentChoiceSurface;
  /** True while the reply is on the wire. A pending reply is not an
   * accepted answer, so the controls stay visible but disabled. */
  submitting: boolean;
  /** The refusal of THIS question's reply, already association-checked by
   * ``clarificationRegion``. A document pick is refused through the same
   * door as a clarification reply, so it shows the same named copy. */
  refusalKind: string | null;
  onSelectChoice: (choiceId: string) => void;
  onOtherText: (text: string) => void;
  onSubmit: () => void;
}

export function DocumentChoiceCardView({
  surface,
  submitting,
  refusalKind,
  onSelectChoice,
  onOtherText,
  onSubmit,
}: DocumentChoiceCardViewProps) {
  return (
    <section
      data-testid="document-choice-card"
      className="border border-black bg-tool px-3 py-2"
    >
      {/* The COMPLETE question, verbatim from the host, before every
          candidate — the same rule the clarification card follows. */}
      <p data-testid="document-choice-prompt" className="text-sm font-semibold text-gray-800">
        {surface.prompt}
      </p>

      {refusalKind ? (
        <p data-testid="document-choice-refusal" className="mt-1 text-xs text-red-700">
          {refusalCopy(refusalKind)}
        </p>
      ) : null}

      <div className="mt-2 flex flex-col gap-2">
        {surface.rows.map((row) => (
          <button
            key={row.choice_id}
            type="button"
            data-testid={`document-choice-row-${row.choice_id}`}
            aria-pressed={row.selected}
            disabled={submitting}
            onClick={() => onSelectChoice(row.choice_id)}
            className={
              row.selected
                ? "border border-black bg-accent px-3 py-1 text-left text-sm text-white"
                : "border border-black bg-card px-3 py-1 text-left text-sm hover:bg-accent hover:text-white disabled:opacity-50"
            }
          >
            {/* The document's title — never the wire's ``choice_id``. */}
            <span data-testid={`document-choice-title-${row.choice_id}`} className="font-semibold">
              {row.title}
            </span>
            {/* The section the admitted span came from, when the source
                named one; omitted rather than shown empty. */}
            {row.section ? (
              <span data-testid={`document-choice-section-${row.choice_id}`} className="ml-2 text-xs opacity-80">
                {row.section}
              </span>
            ) : null}
            <span data-testid={`document-choice-summary-${row.choice_id}`} className="mt-1 block text-xs opacity-90">
              {row.summary}
            </span>
          </button>
        ))}

        {/* The trailing re-phrase (the wire's reserved ``other``), with
            the contract's own label 换个问法 — a re-ask, not an ordinary
            其他. */}
        <button
          type="button"
          data-testid="document-choice-rephrase"
          aria-pressed={surface.rephrase.selected}
          disabled={submitting}
          onClick={() => onSelectChoice(surface.rephrase.choice_id)}
          className={
            surface.rephrase.selected
              ? "border border-black bg-accent px-3 py-1 text-left text-sm text-white"
              : "border border-black bg-card px-3 py-1 text-left text-sm hover:bg-accent hover:text-white disabled:opacity-50"
          }
        >
          {surface.rephrase.label}
        </button>
      </div>

      {/* The re-phrase box exists ONLY once the rephrase is selected, so
          text can never be attached to a candidate pick. */}
      {surface.rephrase.text !== null ? (
        <textarea
          data-testid="document-choice-rephrase-text"
          aria-label="换个问法"
          className="mt-2 w-full border border-black bg-card px-2 py-1 text-sm"
          rows={2}
          value={surface.rephrase.text}
          disabled={submitting}
          onChange={(event) => onOtherText(event.target.value)}
        />
      ) : null}

      <button
        type="button"
        data-testid="document-choice-submit"
        disabled={!surface.submittable || submitting}
        onClick={onSubmit}
        className="mt-2 border border-black bg-accent px-3 py-1 text-sm text-white disabled:opacity-50"
      >
        {submitting ? "提交中…" : "提交选择"}
      </button>
    </section>
  );
}

/** The store-resolving wrapper. ThreadPage mounts it wherever a decoded
 * ``document_choice`` is pending; the actions are the SAME reply-door ones
 * the clarification card uses, because both question kinds are answered by
 * the same POST body (``{question_id, choice_id}`` + optional
 * ``free_text``) at ``_precheck_document_reply``. */
export default function DocumentChoiceCard({
  threadId,
  surface,
  submitting,
  refusalKind,
}: {
  threadId: string;
  surface: DocumentChoiceSurface;
  submitting: boolean;
  refusalKind: string | null;
}) {
  const selectTaskChoice = useWorkspaceStore((s) => s.selectTaskChoice);
  const setTaskOtherText = useWorkspaceStore((s) => s.setTaskOtherText);
  const submitTaskReply = useWorkspaceStore((s) => s.submitTaskReply);

  return DocumentChoiceCardView({
    surface,
    submitting,
    refusalKind,
    onSelectChoice: (choiceId) => selectTaskChoice(threadId, choiceId),
    onOtherText: (text) => setTaskOtherText(threadId, text),
    onSubmit: () => void submitTaskReply(threadId),
  });
}
