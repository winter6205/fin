// question-workspace W2: the pending clarification card (SC-3, SC-4).
//
// The COMPLETE question is shown before every option, nothing is
// preselected, and the free-text field lives INSIDE the selected Other
// option rather than beside the list — the spec's exact requirement that
// "the full question and all options are available; Other text is inside
// the selected Other option".
//
// Dismissal here is presentation-only. It collapses the card and posts
// nothing: the question stays pending on the host and stays recoverable
// from the authoritative projection, so re-opening it finds the same
// draft with the same typed text (question-workspace.md
// §"Clarification and original input").
//
// Rendered from props by ``ClarificationCardView`` and wrapped below with
// the store's own actions, so the render decision is testable without a
// DOM — the same split as components/ArchiveList.tsx.

import {
  CLARIFICATION_DRAFT_RESTORED_COPY,
  CLARIFICATION_OTHER_LABEL,
  OTHER_CHOICE_ID,
  hasOtherChoice,
  isFreeTextOnly,
  isSubmittable,
  listedChoices,
} from "../clarificationReply";
import { refusalCopy } from "../../../activeInput";
import { useWorkspaceStore } from "../../../store";
import type { ClarificationDraft, ReplyOutcome } from "../clarificationReply";

/** What the card renders, all of it resolved by the caller. */
export interface ClarificationCardViewProps {
  draft: ClarificationDraft;
  /** True while the reply is on the wire and the host has not answered.
   * A pending reply is not an accepted answer, so the controls stay
   * visible but disabled rather than disappearing. */
  submitting: boolean;
  /** The refusal of THIS draft's question, already association-checked
   * by ``clarificationRegion``. The card prints whatever it is handed,
   * so a caller that passes an unfiltered refusal re-renders one
   * question's rejection over another's card. */
  refusalKind: string | null;
  onSelectChoice: (choiceId: string) => void;
  onOtherText: (text: string) => void;
  onDismiss: () => void;
  onReopen: () => void;
  onSubmit: () => void;
}

/** The offered picks and the free-text field they gate.
 *
 * Extracted so the card's render decision stays a short branch chain and
 * this list remains the ONE place that translates choices into buttons.
 * The field lives inside the selected Other option; when Other is the
 * only choice there is nothing to select first, so it is always shown.
 */
function ClarificationOptions({
  draft,
  submitting,
  freeTextOnly,
  otherSelected,
  onSelectChoice,
  onOtherText,
}: {
  draft: ClarificationDraft;
  submitting: boolean;
  freeTextOnly: boolean;
  otherSelected: boolean;
  onSelectChoice: (choiceId: string) => void;
  onOtherText: (text: string) => void;
}) {
  return (
    <>
      <div className="mt-2 flex flex-wrap gap-2">
        {listedChoices(draft.choices).map((choiceId) => (
          <button
            key={choiceId}
            type="button"
            data-testid={`clarification-choice-${choiceId}`}
            aria-pressed={draft.selectedChoiceId === choiceId}
            disabled={submitting}
            onClick={() => onSelectChoice(choiceId)}
            className={
              draft.selectedChoiceId === choiceId
                ? "border border-black bg-accent px-3 py-1 text-sm text-white"
                : "border border-black bg-card px-3 py-1 text-sm hover:bg-accent hover:text-white disabled:opacity-50"
            }
          >
            {choiceId}
          </button>
        ))}
        {!freeTextOnly && hasOtherChoice(draft.choices) ? (
          <button
            type="button"
            data-testid="clarification-choice-other"
            aria-pressed={otherSelected}
            disabled={submitting}
            onClick={() => onSelectChoice(OTHER_CHOICE_ID)}
            className={
              otherSelected
                ? "border border-black bg-accent px-3 py-1 text-sm text-white"
                : "border border-black bg-card px-3 py-1 text-sm hover:bg-accent hover:text-white disabled:opacity-50"
            }
          >
            {CLARIFICATION_OTHER_LABEL}
          </button>
        ) : null}
      </div>

      {/* The free-text field is INSIDE the selected Other option — it
          exists only once Other is chosen, so text can never be attached
          to a listed pick. When Other is the only offered choice there is
          nothing to pick, so the field is shown directly. */}
      {freeTextOnly || otherSelected ? (
        <textarea
          data-testid="clarification-other-text"
          aria-label={freeTextOnly ? "补充内容" : "其他内容"}
          className="mt-2 w-full border border-black bg-card px-2 py-1 text-sm"
          rows={2}
          value={draft.otherText}
          disabled={submitting}
          onChange={(event) => onOtherText(event.target.value)}
        />
      ) : null}
    </>
  );
}

export function ClarificationCardView({
  draft,
  submitting,
  refusalKind,
  onSelectChoice,
  onOtherText,
  onDismiss,
  onReopen,
  onSubmit,
}: ClarificationCardViewProps) {
  if (draft.dismissed) {
    return (
      <section
        data-testid="clarification-card"
        data-dismissed="true"
        className="border border-gray-300 bg-tool px-3 py-2"
      >
        <p className="text-xs text-gray-600">
          有一条待回答的澄清提问。收起只是折叠显示，没有回答任何问题。
        </p>
        <button
          type="button"
          data-testid="clarification-reopen"
          onClick={onReopen}
          className="mt-2 border border-black bg-card px-3 py-1 text-sm hover:bg-accent hover:text-white"
        >
          展开提问
        </button>
      </section>
    );
  }

  const freeTextOnly = isFreeTextOnly(draft.choices);
  const otherSelected = draft.selectedChoiceId === OTHER_CHOICE_ID;

  return (
    <section
      data-testid="clarification-card"
      className="border border-black bg-tool px-3 py-2"
    >
      {/* The COMPLETE question, verbatim from the host, before every
          option. Never truncated and never replaced by a summary. */}
      <p data-testid="clarification-prompt" className="text-sm font-semibold text-gray-800">
        {draft.prompt}
      </p>

      {refusalKind ? (
        <p data-testid="clarification-refusal" className="mt-1 text-xs text-red-700">
          {refusalCopy(refusalKind)}
        </p>
      ) : null}
      {refusalKind === "task_reply_expired" ? (
        <p data-testid="clarification-stale" className="mt-1 text-xs text-gray-600">
          {CLARIFICATION_DRAFT_RESTORED_COPY}
        </p>
      ) : null}

      <ClarificationOptions
        draft={draft}
        submitting={submitting}
        freeTextOnly={freeTextOnly}
        otherSelected={otherSelected}
        onSelectChoice={onSelectChoice}
        onOtherText={onOtherText}
      />

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          data-testid="clarification-submit"
          disabled={!isSubmittable(draft) || submitting}
          onClick={onSubmit}
          className="border border-black bg-accent px-3 py-1 text-sm text-white disabled:opacity-50"
        >
          {submitting ? "提交中…" : "提交回答"}
        </button>
        <button
          type="button"
          data-testid="clarification-dismiss"
          disabled={submitting}
          onClick={onDismiss}
          className="border border-black bg-card px-3 py-1 text-sm hover:bg-accent hover:text-white disabled:opacity-50"
        >
          收起
        </button>
      </div>
    </section>
  );
}

/** A refused reply whose question is no longer the one on screen.
 *
 * Read-only by construction. The store's reply actions resolve the thread's
 * CURRENT pending question, so giving this card live controls would answer a
 * different question than the one it refuses. What it states is the shared
 * copy only — the named refusal's own sentence and, for a stale reply, the
 * preserved-draft sentence — so the notice cannot be reworded into a claim
 * the host did not make, and the refused draft stays visible with it. */
export function RefusedClarificationNotice({
  prompt,
  refusalKind,
  outcome,
}: {
  prompt: string;
  refusalKind: string;
  outcome: ReplyOutcome;
}) {
  return (
    <section
      data-testid="clarification-refused"
      className="border border-gray-300 bg-tool px-3 py-2"
    >
      <p className="text-xs text-gray-600">{prompt}</p>
      <p className="mt-1 text-xs text-red-700">{refusalCopy(refusalKind)}</p>
      {outcome === "stale" ? (
        <p className="mt-1 text-xs text-gray-600">
          {CLARIFICATION_DRAFT_RESTORED_COPY}
        </p>
      ) : null}
    </section>
  );
}

/** The store-resolving wrapper. Its prop signature is unchanged from the
 * card that ThreadPage mounts today; ``refusalKind`` is expected to be
 * the refusal the region associated with THIS draft's question. */
export default function ClarificationCard({
  threadId,
  draft,
  submitting,
  refusalKind,
}: {
  threadId: string;
  draft: ClarificationDraft;
  submitting: boolean;
  refusalKind: string | null;
}) {
  const selectTaskChoice = useWorkspaceStore((s) => s.selectTaskChoice);
  const setTaskOtherText = useWorkspaceStore((s) => s.setTaskOtherText);
  const dismissTaskClarification = useWorkspaceStore(
    (s) => s.dismissTaskClarification,
  );
  const submitTaskReply = useWorkspaceStore((s) => s.submitTaskReply);
  const reopenTaskClarification = useWorkspaceStore(
    (s) => s.reopenTaskClarification,
  );

  return ClarificationCardView({
    draft,
    submitting,
    refusalKind,
    onSelectChoice: (choiceId) => selectTaskChoice(threadId, choiceId),
    onOtherText: (text) => setTaskOtherText(threadId, text),
    onDismiss: () => dismissTaskClarification(threadId),
    onReopen: () => reopenTaskClarification(threadId),
    onSubmit: () => void submitTaskReply(threadId),
  });
}
