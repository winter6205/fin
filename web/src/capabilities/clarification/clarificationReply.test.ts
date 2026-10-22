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

import * as clarificationReply from "./clarificationReply";
import { describe, expect, it } from "vitest";
import {
  CLARIFICATION_DRAFT_RESTORED_COPY,
  CLARIFICATION_DRAFT_STORAGE_KEY,
  CLARIFICATION_OTHER_LABEL,
  OTHER_CHOICE_ID,
  applyRefusal,
  dismissClarification,
  draftRecords,
  hasOtherChoice,
  isFreeTextOnly,
  isSubmittable,
  listedChoices,
  openClarification,
  parseDraftRecords,
  reopenClarification,
  replyOutcome,
  restoreDraftText,
  selectChoice,
  setOtherText,
  submitPayload,
} from "./clarificationReply";
import type { ClarificationDraft } from "./clarificationReply";
import type { PendingQuestionView } from "../../types";

/** A host pending question: full prompt, listed choices, and the one
 * trailing Other the host appends (PendingQuestionView.choices). */
const QUESTION: PendingQuestionView = {
  question_id: "q-1",
  task_id: "t-1",
  task_version: 3,
  prompt: "请选择统计口径：全年汇总，还是分季度？",
  choices: ["全年汇总", "分季度", OTHER_CHOICE_ID],
  input_version: 7,
};

/** A second, NEWER pending question from the same host. */
const LATER_QUESTION: PendingQuestionView = {
  ...QUESTION,
  question_id: "q-2",
  prompt: "要比较哪两个期间？",
  choices: ["2022", "2023", OTHER_CHOICE_ID],
};

/** The terminal clarification the host publishes when it has no real
 * option to offer: the reserved Other is its only choice. */
const FREE_TEXT_ONLY_QUESTION: PendingQuestionView = {
  question_id: "q-free",
  task_id: "t-1",
  task_version: 1,
  prompt: "请补充：公司/期间",
  choices: [OTHER_CHOICE_ID],
  input_version: 2,
};

/** A draft with Other selected and some free text typed. */
function otherDraft(text: string): ClarificationDraft {
  const open = selectChoice(openClarification(QUESTION), OTHER_CHOICE_ID);
  return setOtherText(open, text);
}

describe("openClarification", () => {
  it("preselects nothing and copies the prompt and choices verbatim", () => {
    const draft = openClarification(QUESTION);
    expect(draft.questionId).toBe("q-1");
    expect(draft.prompt).toBe(QUESTION.prompt);
    expect(draft.choices).toEqual(QUESTION.choices);
    // Nothing is preselected ("Do not preselect an answer").
    expect(draft.selectedChoiceId).toBeNull();
    expect(draft.otherText).toBe("");
    expect(draft.dismissed).toBe(false);
  });
});

describe("listedChoices / hasOtherChoice", () => {
  it("excludes the trailing Other from the option list", () => {
    expect(listedChoices(QUESTION.choices)).toEqual(["全年汇总", "分季度"]);
    expect(hasOtherChoice(QUESTION.choices)).toBe(true);
  });

  it("reports a duplicated Other once rather than as two options", () => {
    const duplicated = ["全年汇总", OTHER_CHOICE_ID, OTHER_CHOICE_ID];
    expect(hasOtherChoice(duplicated)).toBe(true);
    expect(listedChoices(duplicated)).toEqual(["全年汇总"]);
  });

  it("hasOtherChoice is false without the reserved token", () => {
    expect(hasOtherChoice(["全年汇总", "分季度"])).toBe(false);
    expect(hasOtherChoice([])).toBe(false);
  });

  it("isFreeTextOnly is true only when Other is the whole list", () => {
    expect(isFreeTextOnly([OTHER_CHOICE_ID])).toBe(true);
    expect(isFreeTextOnly(QUESTION.choices)).toBe(false);
    expect(isFreeTextOnly(["全年汇总"])).toBe(false);
    expect(isFreeTextOnly([])).toBe(false);
  });
});

describe("selectChoice", () => {
  it("clears the Other text when a listed choice is picked", () => {
    const next = selectChoice(otherDraft("手写的答案"), "分季度");
    expect(next.selectedChoiceId).toBe("分季度");
    // The text belonged to Other; carrying it into a listed pick would
    // misreport what the operator chose.
    expect(next.otherText).toBe("");
  });

  it("keeps the typed text when Other is re-selected", () => {
    const draft = otherDraft("手写的答案");
    const next = selectChoice(draft, OTHER_CHOICE_ID);
    expect(next.selectedChoiceId).toBe(OTHER_CHOICE_ID);
    expect(next.otherText).toBe("手写的答案");
  });

  it("returns the draft unchanged for a choice the host never offered", () => {
    const draft = otherDraft("手写的答案");
    const next = selectChoice(draft, "不存在的选项");
    expect(next).toBe(draft);
    expect(next.selectedChoiceId).toBe(OTHER_CHOICE_ID);
    expect(next.otherText).toBe("手写的答案");
  });

  it("clears dismissed when the operator answers a collapsed card", () => {
    const collapsed = dismissClarification(otherDraft("手写的答案"));
    expect(collapsed.dismissed).toBe(true);
    const answered = selectChoice(collapsed, OTHER_CHOICE_ID);
    expect(answered.dismissed).toBe(false);
    expect(answered.selectedChoiceId).toBe(OTHER_CHOICE_ID);
  });
});

describe("setOtherText", () => {
  it("is inert unless Other is the selected choice", () => {
    const open = openClarification(QUESTION);
    expect(setOtherText(open, "随便写")).toBe(open);

    const listed = selectChoice(open, "分季度");
    expect(setOtherText(listed, "随便写")).toBe(listed);
    expect(setOtherText(listed, "随便写").otherText).toBe("");
  });

  it("writes the text once Other is selected", () => {
    const onOther = selectChoice(openClarification(QUESTION), OTHER_CHOICE_ID);
    expect(setOtherText(onOther, "手写的答案").otherText).toBe("手写的答案");
  });
});

describe("dismissClarification", () => {
  it("flips only dismissed, keeping the selection and the typed text", () => {
    const draft = otherDraft("手写的答案");
    const dismissed = dismissClarification(draft);
    expect(dismissed.dismissed).toBe(true);
    expect(dismissed.questionId).toBe(draft.questionId);
    expect(dismissed.selectedChoiceId).toBe(OTHER_CHOICE_ID);
    expect(dismissed.otherText).toBe("手写的答案");
    // Dismissal supplies no default answer: the fresh draft stays empty.
    expect(dismissClarification(openClarification(QUESTION)).selectedChoiceId).toBeNull();
  });
});

describe("reopenClarification", () => {
  it("preselects nothing on a dismissed draft that never had a selection", () => {
    const collapsed = dismissClarification(openClarification(QUESTION));
    const reopened = reopenClarification(collapsed);
    expect(reopened.dismissed).toBe(false);
    // Re-opening supplies no default answer (SC-3): the first listed
    // choice must not become the selection just because the card was
    // collapsed.
    expect(reopened.selectedChoiceId).toBeNull();
    expect(reopened.otherText).toBe("");
  });

  it("keeps an existing selection through the reopen", () => {
    const collapsed = dismissClarification(
      selectChoice(openClarification(QUESTION), "分季度"),
    );
    const reopened = reopenClarification(collapsed);
    expect(reopened.dismissed).toBe(false);
    expect(reopened.selectedChoiceId).toBe("分季度");
  });

  it("keeps the typed Other text through the reopen", () => {
    const reopened = reopenClarification(
      dismissClarification(otherDraft("手写的答案")),
    );
    expect(reopened.selectedChoiceId).toBe(OTHER_CHOICE_ID);
    expect(reopened.otherText).toBe("手写的答案");
  });

  it("is idempotent when the card is not dismissed", () => {
    const draft = otherDraft("手写的答案");
    expect(reopenClarification(draft)).toBe(draft);
  });
});

describe("isSubmittable", () => {
  it("is false with nothing selected", () => {
    expect(isSubmittable(openClarification(QUESTION))).toBe(false);
  });

  it("is false for Other with blank text", () => {
    expect(isSubmittable(otherDraft("   "))).toBe(false);
    expect(isSubmittable(otherDraft(""))).toBe(false);
  });

  it("is true for Other with text and for a listed choice", () => {
    expect(isSubmittable(otherDraft("手写的答案"))).toBe(true);
    expect(isSubmittable(selectChoice(openClarification(QUESTION), "分季度"))).toBe(true);
  });

  it("is true for a listed choice even with stray text present", () => {
    const stray: ClarificationDraft = {
      ...selectChoice(openClarification(QUESTION), "分季度"),
      otherText: "残留文字",
    };
    expect(isSubmittable(stray)).toBe(true);
  });
});

describe("submitPayload", () => {
  it("is null while not submittable", () => {
    expect(submitPayload(openClarification(QUESTION))).toBeNull();
    expect(submitPayload(otherDraft("   "))).toBeNull();
  });

  it("carries the question id and null free text for a listed choice", () => {
    const payload = submitPayload(
      selectChoice(openClarification(QUESTION), "全年汇总"),
    );
    expect(payload).toEqual({
      questionId: "q-1",
      choiceId: "全年汇总",
      freeText: null,
    });
  });

  it("carries the question id and the verbatim Other text", () => {
    const payload = submitPayload(otherDraft(" 手写的答案 "));
    expect(payload).toEqual({
      questionId: "q-1",
      choiceId: OTHER_CHOICE_ID,
      freeText: " 手写的答案 ",
    });
  });
});

describe("free-text-only clarification (Other is the only choice)", () => {
  it("opens with nothing selected and accepts text without an Other click", () => {
    const opened = openClarification(FREE_TEXT_ONLY_QUESTION);
    expect(opened.selectedChoiceId).toBeNull();
    expect(opened.choices).toEqual([OTHER_CHOICE_ID]);

    const typed = setOtherText(opened, " ZEUS，2024 年第三季度 ");
    expect(typed).not.toBe(opened);
    // The typed answer IS the reply; no selection is fabricated on the way.
    expect(typed.selectedChoiceId).toBeNull();
    expect(isSubmittable(typed)).toBe(true);
    expect(submitPayload(typed)).toEqual({
      questionId: "q-free",
      choiceId: OTHER_CHOICE_ID,
      freeText: " ZEUS，2024 年第三季度 ",
    });
  });

  it("stays unsubmittable while the text is blank", () => {
    const opened = openClarification(FREE_TEXT_ONLY_QUESTION);
    expect(isSubmittable(opened)).toBe(false);
    expect(submitPayload(opened)).toBeNull();
    expect(isSubmittable(setOtherText(opened, "   "))).toBe(false);
    expect(submitPayload(setOtherText(opened, "   "))).toBeNull();
  });

  it("recovers its text under the same question after a reload", () => {
    const draft = setOtherText(
      openClarification(FREE_TEXT_ONLY_QUESTION),
      "ZEUS，2024 年第三季度",
    );
    const records = draftRecords({ "q-free": draft });
    expect(records["q-free"]).toEqual({
      selectedChoiceId: null,
      otherText: "ZEUS，2024 年第三季度",
    });

    const restored = restoreDraftText(FREE_TEXT_ONLY_QUESTION, records["q-free"]);
    expect(restored.questionId).toBe("q-free");
    expect(restored.selectedChoiceId).toBeNull();
    expect(submitPayload(restored)).toEqual(submitPayload(draft));
  });
});

describe("applyRefusal (SC-4)", () => {
  it("keeps the association and draft on task_reply_expired, clearing dismissed", () => {
    const draft = dismissClarification(otherDraft("手写的答案"));
    const result = applyRefusal(draft, {
      kind: "task_reply_expired",
      questionId: "q-1",
    });
    expect(result.outcome).toBe("stale");
    // The old reply is never rebased onto a newer question.
    expect(result.rebased).toBe(false);
    expect(result.draft.questionId).toBe("q-1");
    expect(result.draft.selectedChoiceId).toBe(OTHER_CHOICE_ID);
    expect(result.draft.otherText).toBe("手写的答案");
    // The failed reply must be visible again, not collapsed.
    expect(result.draft.dismissed).toBe(false);
  });

  it("keeps the draft's own question even when the refusal names another", () => {
    const draft = otherDraft("手写的答案");
    const result = applyRefusal(draft, {
      kind: "task_reply_expired",
      questionId: "q-2",
    });
    expect(result.rebased).toBe(false);
    expect(result.draft.questionId).toBe("q-1");
  });

  it("maps an unknown refusal to rejected and still clears dismissed", () => {
    const draft = dismissClarification(otherDraft("手写的答案"));
    const result = applyRefusal(draft, {
      kind: "task_input_invalid",
      questionId: null,
    });
    expect(result.outcome).toBe("rejected");
    expect(result.draft.dismissed).toBe(false);
    expect(result.draft.otherText).toBe("手写的答案");
  });
});

describe("retry — one payload source, so a resend is identical", () => {
  it("has no second payload builder to drift", () => {
    // The retry guarantee is "the same object submitPayload produces",
    // which holds only while there is exactly one builder. A separate
    // retry entry point could diverge from the first send.
    expect("retryPayload" in clarificationReply).toBe(false);
  });

  it("re-derives a byte-identical payload after a refusal", () => {
    const draft = otherDraft("手写的答案");
    const first = submitPayload(draft);
    const after = applyRefusal(draft, {
      kind: "task_reply_expired",
      questionId: "q-1",
    }).draft;
    expect(submitPayload(after)).toEqual(first);
    expect(submitPayload(after)).toEqual({
      questionId: "q-1",
      choiceId: OTHER_CHOICE_ID,
      freeText: "手写的答案",
    });
  });

  it("re-derives a byte-identical payload when the draft is untouched", () => {
    const draft = selectChoice(openClarification(QUESTION), "分季度");
    expect(submitPayload(draft)).toEqual(submitPayload({ ...draft }));
  });

  it("produces no payload for a draft the host would refuse", () => {
    expect(submitPayload(otherDraft("   "))).toBeNull();
    expect(submitPayload(openClarification(QUESTION))).toBeNull();
  });
});

describe("replyOutcome", () => {
  it("names the two reply failures and rejects everything else", () => {
    expect(replyOutcome({ kind: "task_reply_expired", questionId: null })).toBe("stale");
    expect(replyOutcome({ kind: "task_reply_unknown", questionId: null })).toBe(
      "unknown_question",
    );
    expect(replyOutcome({ kind: "task_input_invalid", questionId: null })).toBe("rejected");
    // A blank or unfamiliar kind reads rejected rather than crashing.
    expect(replyOutcome({ kind: "", questionId: null })).toBe("rejected");
    expect(replyOutcome({ kind: "something_unheard_of", questionId: null })).toBe(
      "rejected",
    );
  });
});

describe("surface copy", () => {
  it("exposes the Other label and the post-refusal sentence", () => {
    expect(CLARIFICATION_OTHER_LABEL).toBe("其他");
    expect(CLARIFICATION_DRAFT_RESTORED_COPY.length).toBeGreaterThan(0);
  });
});

describe("draft durability — what a reload may recover", () => {
  it("names the one storage key the drafts live under", () => {
    expect(CLARIFICATION_DRAFT_STORAGE_KEY).toBe("fin.clarificationDrafts");
  });

  it("records only the answer and its question, never the host's text", () => {
    const records = draftRecords({ "q-1": otherDraft("手写的答案") });
    // The question, its prompt and its choices are authoritative from
    // the projection on reload; persisting them here would create a
    // second, staler copy of a host fact.
    expect(records).toEqual({
      "q-1": { selectedChoiceId: OTHER_CHOICE_ID, otherText: "手写的答案" },
    });
  });

  it("round-trips a draft through the persisted shape", () => {
    const draft = otherDraft("手写的答案");
    const stored = JSON.stringify(draftRecords({ "q-1": draft }));
    const parsed = parseDraftRecords(JSON.parse(stored));
    const restored = restoreDraftText(QUESTION, parsed["q-1"] ?? null);
    expect(restored.questionId).toBe("q-1");
    expect(restored.selectedChoiceId).toBe(OTHER_CHOICE_ID);
    expect(restored.otherText).toBe("手写的答案");
    expect(submitPayload(restored)).toEqual(submitPayload(draft));
  });

  it("restores nothing when there is no record for the question", () => {
    expect(restoreDraftText(QUESTION, null)).toEqual(openClarification(QUESTION));
    expect(restoreDraftText(QUESTION, undefined)).toEqual(openClarification(QUESTION));
  });

  it("drops a selection the host no longer offers instead of inventing one", () => {
    const stale = { selectedChoiceId: "分季度", otherText: "" };
    const restored = restoreDraftText(LATER_QUESTION, stale);
    expect(restored.questionId).toBe("q-2");
    expect(restored.selectedChoiceId).toBeNull();
    expect(restored.otherText).toBe("");
  });

  it("keeps free text only under the reserved Other option", () => {
    const listed = restoreDraftText(QUESTION, {
      selectedChoiceId: "分季度",
      otherText: "残留文字",
    });
    expect(listed.otherText).toBe("");

    const onOther = restoreDraftText(QUESTION, {
      selectedChoiceId: OTHER_CHOICE_ID,
      otherText: "手写的答案",
    });
    expect(onOther.otherText).toBe("手写的答案");
    expect(isSubmittable(onOther)).toBe(true);
  });

  it("never restores a collapse, so a pending question stays visible", () => {
    const restored = restoreDraftText(QUESTION, {
      selectedChoiceId: OTHER_CHOICE_ID,
      otherText: "手写的答案",
    });
    expect(restored.dismissed).toBe(false);
  });

  it("drops unreadable persisted entries rather than guessing", () => {
    expect(parseDraftRecords(null)).toEqual({});
    expect(parseDraftRecords("text")).toEqual({});
    expect(parseDraftRecords([])).toEqual({});
    expect(parseDraftRecords({ "q-1": null })).toEqual({});
    expect(parseDraftRecords({ "q-1": { otherText: 7 } })).toEqual({});
    expect(
      parseDraftRecords({ "q-1": { selectedChoiceId: 3, otherText: "x" } }),
    ).toEqual({});
    expect(
      parseDraftRecords({ "q-1": { selectedChoiceId: null, otherText: "" } }),
    ).toEqual({ "q-1": { selectedChoiceId: null, otherText: "" } });
    expect(
      parseDraftRecords({
        "q-1": { selectedChoiceId: OTHER_CHOICE_ID, otherText: "手写的答案" },
        "q-2": { selectedChoiceId: "2022", otherText: "" },
      }),
    ).toEqual({
      "q-1": { selectedChoiceId: OTHER_CHOICE_ID, otherText: "手写的答案" },
      "q-2": { selectedChoiceId: "2022", otherText: "" },
    });
  });
});
