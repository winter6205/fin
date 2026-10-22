// Which facts belong to the question the clarification card is rendering.
//
// The ledger holds at most one operation per thread (workspaceOperation.ts),
// so a surface that renders it without asking paints an unrelated operation
// onto whatever question is current now: a restore in flight disables the
// options, a reply for another question reads as this thread's pending
// answer, and an old refusal prints the previous question's rejection over a
// newer question. specs/question-workspace.md §"Clarification and original
// input" fixes the rules selected here — a pending reply is never an answer
// and acceptance is the projection's word only (SC-3); a refusal stays
// attached to its own question and keeps its draft (SC-4).
//
// Pure: it reads the projection facts it is handed and writes nothing,
// so it sends no request and cannot promote a local pick into a host
// outcome. Rendered at the bottom with ``renderToString`` because this
// project's vitest setup has no DOM testing library (same split as
// components/ArchiveList.test.ts).

import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import { clarificationRegion } from "./clarificationRegion";
import type { ClarificationRegionInput } from "./clarificationRegion";
import { ClarificationCardView } from "./components/ClarificationCard";
import {
  CLARIFICATION_DRAFT_RESTORED_COPY,
  OTHER_CHOICE_ID,
  dismissClarification,
  openClarification,
  selectChoice,
  setOtherText,
} from "./clarificationReply";
import type { ClarificationDraft } from "./clarificationReply";
import { ACCEPTED_NOTICE, refusalCopy } from "../../activeInput";
import type { WorkspaceOperation } from "../../workspaceOperation";
import type { PendingQuestionView } from "../../types";

/** The thread the operation under test belongs to. The region receives only
 * the OPEN thread's record, so the id is a constant here. */
const THREAD = "11111111-1111-4111-8111-111111111111";

const Q1: PendingQuestionView = {
  question_id: "q-1",
  task_id: "t-1",
  task_version: 3,
  prompt: "请选择统计口径：全年汇总，还是分季度？",
  choices: ["全年汇总", "分季度", OTHER_CHOICE_ID],
  input_version: 7,
};

/** A NEWER pending question that arrived after q-1 was refused. */
const Q2: PendingQuestionView = {
  ...Q1,
  question_id: "q-2",
  prompt: "要比较哪两个期间？",
  choices: ["2022", "2023", OTHER_CHOICE_ID],
};

/** The host's TERMINAL free-text-only clarification: Other is the only
 * choice, so the typed answer is the whole reply. */
const Q_FREE: PendingQuestionView = {
  ...Q1,
  question_id: "q-free",
  prompt: "请补充：公司/期间",
  choices: [OTHER_CHOICE_ID],
};

/** A terminal clarification carrying real options from the catalog. */
const Q_REAL_OPTIONS: PendingQuestionView = {
  ...Q1,
  question_id: "q-real",
  prompt: "请补充：公司/期间",
  choices: ["苹果公司", "2023 年度", OTHER_CHOICE_ID],
};

function draftFor(question: PendingQuestionView, text: string): ClarificationDraft {
  return setOtherText(
    selectChoice(openClarification(question), OTHER_CHOICE_ID),
    text,
  );
}

/** The open thread's CURRENT operation, in the state a case is about.
 * Everything the region reads is a field of this one record, so a case
 * cannot accidentally test two slots staying in sync. */
function operation(over: Partial<WorkspaceOperation>): WorkspaceOperation {
  return {
    threadId: THREAD,
    door: "reply",
    kind: "reply",
    attempt: 1,
    questionId: null,
    archiveId: null,
    taskId: null,
    state: "pending",
    reason: null,
    ...over,
  };
}

function replySubmission(
  state: WorkspaceOperation["state"],
  questionId: string | null,
): WorkspaceOperation {
  return operation({ state, questionId });
}

function replyRefusal(kind: string, questionId: string | null) {
  return { kind, questionId, door: "reply" as const };
}

function region(over: Partial<ClarificationRegionInput> = {}) {
  const input: ClarificationRegionInput = {
    pendingQuestion: over.pendingQuestion ?? null,
    drafts: over.drafts ?? {},
    submission: over.submission ?? null,
    refusal: over.refusal ?? null,
  };
  return clarificationRegion(input);
}

describe("a pending reply is never an answer (SC-3)", () => {
  it("marks only its OWN question as submitting", () => {
    const draft = draftFor(Q1, "手写的答案");
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": draft },
      submission: replySubmission("pending", "q-1"),
    });
    expect(view.card.submitting).toBe(true);
    expect(view.card.draft).toBe(draft);
    // Nothing is claimed as accepted or answered while the host is
    // silent, and the refusal slot stays empty.
    expect(view.answeredQuestionId).toBeNull();
    expect(view.card.refusalKind).toBeNull();
    expect(view.refused).toBeNull();
  });

  it("does not paint a reply on another question onto this card", () => {
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": draftFor(Q1, "手写的答案"), "q-9": draftFor(Q2, "别的") },
      submission: replySubmission("pending", "q-9"),
    });
    expect(view.card.submitting).toBe(false);
    expect(view.answeredQuestionId).toBeNull();
  });

  it("does not let a pending RESTORE hold the clarification open", () => {
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": draftFor(Q1, "手写的答案") },
      submission: operation({ door: "restore", kind: "restore" }),
    });
    expect(view.card.submitting).toBe(false);
    expect(view.answeredQuestionId).toBeNull();
  });

  it("reads a transport failure as unresolved, not as an answer", () => {
    const draft = draftFor(Q1, "手写的答案");
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": draft },
      submission: replySubmission("failed", "q-1"),
    });
    expect(view.card.submitting).toBe(false);
    expect(view.card.refusalKind).toBeNull();
    expect(view.answeredQuestionId).toBeNull();
    // The draft the operator typed survives, so the next click re-sends
    // the same reply rather than a blank one.
    expect(view.card.draft).toBe(draft);
  });
});

describe("acceptance comes only from the projection", () => {
  it("waits for the projection to clear THAT question_id", () => {
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": draftFor(Q1, "手写的答案") },
      submission: replySubmission("accepted", "q-1"),
    });
    // The host accepted the input and still reports the question
    // pending, so the workspace may not call it answered.
    expect(view.answeredQuestionId).toBeNull();
    expect(view.card.submitting).toBe(false);
  });

  it("reports the answered question once the projection clears it", () => {
    const view = region({
      pendingQuestion: null,
      drafts: {},
      submission: replySubmission("accepted", "q-1"),
    });
    expect(view.answeredQuestionId).toBe("q-1");
    expect(view.card.draft).toBeNull();
  });

  it("credits a newer question with nothing the older reply answered", () => {
    const view = region({
      pendingQuestion: Q2,
      drafts: { "q-1": draftFor(Q1, "手写的答案"), "q-2": draftFor(Q2, "新的") },
      submission: replySubmission("accepted", "q-1"),
    });
    expect(view.answeredQuestionId).toBe("q-1");
    // q-2 is a different question: the accepted reply neither answers it
    // nor moves its draft.
    expect(view.card.draft?.questionId).toBe("q-2");
    expect(view.card.submitting).toBe(false);
    expect(view.refused).toBeNull();
  });

  it("never reports an answer for a refused or pending reply", () => {
    for (const state of ["pending", "rejected", "failed"] as const) {
      expect(
        region({
          pendingQuestion: null,
          submission: replySubmission(state, "q-1"),
          refusal: state === "rejected" ? replyRefusal("task_reply_expired", "q-1") : null,
        }).answeredQuestionId,
      ).toBeNull();
    }
  });
});

describe("a refusal stays on its own question (SC-4)", () => {
  const refusedQ1 = draftFor(Q1, "手写的答案");

  it("shows the refusal on the question that was refused", () => {
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": refusedQ1 },
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_expired", "q-1"),
    });
    expect(view.card.refusalKind).toBe("task_reply_expired");
    // It is the card's own refusal, so it is not also a detached one.
    expect(view.refused).toBeNull();
  });

  it("suppresses an old refusal when a newer question is rendering", () => {
    const view = region({
      pendingQuestion: Q2,
      drafts: { "q-1": refusedQ1, "q-2": draftFor(Q2, "新的") },
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_expired", "q-1"),
    });
    expect(view.card.refusalKind).toBeNull();
    // The newer question renders ITS OWN draft, never the refused one.
    expect(view.card.draft?.questionId).toBe("q-2");
  });

  it("keeps the refused draft reachable with its original association", () => {
    const view = region({
      pendingQuestion: Q2,
      drafts: { "q-1": refusedQ1, "q-2": draftFor(Q2, "新的") },
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_expired", "q-1"),
    });
    expect(view.refused).not.toBeNull();
    expect(view.refused?.draft).toBe(refusedQ1);
    expect(view.refused?.draft.questionId).toBe("q-1");
    expect(view.refused?.draft.otherText).toBe("手写的答案");
    expect(view.refused?.refusalKind).toBe("task_reply_expired");
    expect(view.refused?.outcome).toBe("stale");
  });

  it("keeps it reachable when the projection reports no question at all", () => {
    const view = region({
      pendingQuestion: null,
      drafts: { "q-1": refusedQ1 },
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_unknown", "q-1"),
    });
    expect(view.card.draft).toBeNull();
    expect(view.refused?.draft.questionId).toBe("q-1");
    expect(view.refused?.outcome).toBe("unknown_question");
  });

  it("never claims a refused question was answered", () => {
    const view = region({
      pendingQuestion: Q2,
      drafts: { "q-1": refusedQ1 },
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_expired", "q-1"),
    });
    expect(view.answeredQuestionId).toBeNull();
  });

  it("renders no refusal copy for another door's refusal", () => {
    // A failure kind is only meaningful against its own door's copy: the
    // refusal rides the operation that recorded it, so a message- or
    // restore-door kind here would print clarification copy for a
    // different operation.
    for (const door of ["message", "restore"] as const) {
      const view = region({
        pendingQuestion: Q1,
        drafts: { "q-1": refusedQ1 },
        submission: replySubmission("rejected", "q-1"),
        refusal: { kind: "task_input_invalid", questionId: null, door },
      });
      expect(view.card.refusalKind).toBeNull();
      expect(view.refused).toBeNull();
    }
  });

  it("falls back to the submission's own association, not the host's", () => {
    // The refused reply is identified by what was SENT (the operation's own
    // ``questionId``); a refusal that names some other question cannot move
    // the notice onto it.
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": refusedQ1 },
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_expired", "q-7"),
    });
    expect(view.card.refusalKind).toBe("task_reply_expired");
    expect(view.card.draft?.questionId).toBe("q-1");
  });

  it("has nothing detached to show once the draft is gone", () => {
    const view = region({
      pendingQuestion: Q2,
      drafts: {},
      submission: replySubmission("rejected", "q-1"),
      refusal: replyRefusal("task_reply_expired", "q-1"),
    });
    expect(view.refused).toBeNull();
    expect(view.card.refusalKind).toBeNull();
  });
});

describe("dismissal is presentation-only", () => {
  it("leaves the question pending, unanswered, and recoverable", () => {
    const open = draftFor(Q1, "手写的答案");
    const view = region({
      pendingQuestion: Q1,
      drafts: { "q-1": dismissClarification(open) },
    });
    expect(view.card.draft?.dismissed).toBe(true);
    expect(view.card.draft?.otherText).toBe("手写的答案");
    expect(view.card.submitting).toBe(false);
    expect(view.card.refusalKind).toBeNull();
    expect(view.answeredQuestionId).toBeNull();
    expect(view.refused).toBeNull();
  });

  it("reads an empty region when there is nothing to render", () => {
    const view = region();
    expect(view.card.draft).toBeNull();
    expect(view.card.submitting).toBe(false);
    expect(view.card.refusalKind).toBeNull();
    expect(view.refused).toBeNull();
    expect(view.answeredQuestionId).toBeNull();
  });
});

// ----- render: the copy appears only for its own question -----

function htmlFor(view: ReturnType<typeof region>): string {
  if (view.card.draft === null) return "";
  return renderToString(
    ClarificationCardView({
      draft: view.card.draft,
      submitting: view.card.submitting,
      refusalKind: view.card.refusalKind,
      onSelectChoice: () => undefined,
      onOtherText: () => undefined,
      onDismiss: () => undefined,
      onReopen: () => undefined,
      onSubmit: () => undefined,
    }),
  );
}

function refusedHtmlFor(view: ReturnType<typeof region>): string {
  if (view.refused === null) return "";
  return renderToString(
    ClarificationCardView({
      draft: view.refused.draft,
      submitting: false,
      refusalKind: view.refused.refusalKind,
      onSelectChoice: () => undefined,
      onOtherText: () => undefined,
      onDismiss: () => undefined,
      onReopen: () => undefined,
      onSubmit: () => undefined,
    }),
  );
}

const STALE_COPY = refusalCopy("task_reply_expired");

/** The opening tag of the element carrying a test id, so an assertion
 * about ``disabled`` does not depend on React's attribute order. */
function tag(html: string, testId: string): string {
  const match = new RegExp(`<[a-z]+[^>]*${testId}[^>]*>`).exec(html);
  if (match === null) {
    throw new Error(`no element with data-testid="${testId}"`);
  }
  return match[0];
}

describe("ClarificationCard renders the region's association", () => {
  const staleView = region({
    pendingQuestion: Q2,
    drafts: { "q-1": draftFor(Q1, "手写的答案"), "q-2": draftFor(Q2, "新的") },
    submission: replySubmission("rejected", "q-1"),
    refusal: replyRefusal("task_reply_expired", "q-1"),
  });

  it("prints the refusal on the question it belongs to", () => {
    const html = refusedHtmlFor(staleView);
    expect(html).toContain(STALE_COPY);
    expect(html).toContain(CLARIFICATION_DRAFT_RESTORED_COPY);
    expect(html).toContain(Q1.prompt);
  });

  it("prints none of it on the newer question's card", () => {
    const html = htmlFor(staleView);
    expect(html).not.toContain(STALE_COPY);
    expect(html).not.toContain(CLARIFICATION_DRAFT_RESTORED_COPY);
    expect(html).toContain(Q2.prompt);
  });

  it("keeps the options visible but disabled while a reply is pending", () => {
    const html = htmlFor(
      region({
        pendingQuestion: Q1,
        drafts: { "q-1": draftFor(Q1, "手写的答案") },
        submission: replySubmission("pending", "q-1"),
      }),
    );
    expect(tag(html, "clarification-choice-全年汇总")).toContain("disabled");
    expect(tag(html, "clarification-submit")).toContain("disabled");
    expect(html).toContain("提交中…");
    // The controls are still THERE — a reply on the wire hides nothing.
    expect(html).toContain('data-testid="clarification-dismiss"');
    // A reply on the wire is not yet an accepted input.
    expect(html).not.toContain(ACCEPTED_NOTICE);
  });

  it("preselects nothing and shows the whole question before the options", () => {
    const html = htmlFor(
      region({ pendingQuestion: Q1, drafts: { "q-1": openClarification(Q1) } }),
    );
    expect(html).toContain('data-testid="clarification-prompt"');
    expect(html.indexOf("clarification-prompt")).toBeLessThan(
      html.indexOf("clarification-choice-全年汇总"),
    );
    expect(html).not.toContain('aria-pressed="true"');
    // Other's free text exists only inside a selected Other option.
    expect(html).not.toContain('data-testid="clarification-other-text"');
  });

  it("shows the free text only once Other is the selection", () => {
    const html = htmlFor(
      region({ pendingQuestion: Q1, drafts: { "q-1": draftFor(Q1, "手写的答案") } }),
    );
    expect(html).toContain('data-testid="clarification-other-text"');
    expect(html).toContain("手写的答案");
  });

  it("renders the host's real options verbatim", () => {
    const html = htmlFor(
      region({
        pendingQuestion: Q_REAL_OPTIONS,
        drafts: { "q-real": openClarification(Q_REAL_OPTIONS) },
      }),
    );
    expect(html).toContain('data-testid="clarification-choice-苹果公司"');
    expect(html).toContain(">苹果公司<");
    expect(html).toContain('data-testid="clarification-choice-2023 年度"');
    expect(html).toContain(">2023 年度<");
    // The reserved Other token stays a client action, never an option label.
    expect(html).toContain('data-testid="clarification-choice-other"');
    expect(html).not.toContain(">other<");
  });

  it("shows the free-text field immediately when Other is the only choice", () => {
    const html = htmlFor(
      region({
        pendingQuestion: Q_FREE,
        drafts: { "q-free": openClarification(Q_FREE) },
      }),
    );
    // No Other click is required: the field is the only control, and a
    // blank answer keeps the submit disabled.
    expect(html).toContain('data-testid="clarification-other-text"');
    expect(html).not.toContain('data-testid="clarification-choice-other"');
    expect(tag(html, "clarification-submit")).toContain('disabled=""');
  });

  it("enables submit for a free-text-only answer once its text is non-blank", () => {
    const typed = setOtherText(
      openClarification(Q_FREE),
      "ZEUS，2024 年第三季度",
    );
    const html = htmlFor(
      region({ pendingQuestion: Q_FREE, drafts: { "q-free": typed } }),
    );
    expect(html).toContain("ZEUS，2024 年第三季度");
    expect(tag(html, "clarification-submit")).not.toContain('disabled=""');
  });

  it("collapses a dismissed card without offering an answer control", () => {
    const html = htmlFor(
      region({
        pendingQuestion: Q1,
        drafts: { "q-1": dismissClarification(openClarification(Q1)) },
      }),
    );
    expect(html).toContain('data-dismissed="true"');
    expect(html).toContain('data-testid="clarification-reopen"');
    expect(html).not.toContain('data-testid="clarification-submit"');
  });
});
