// Contract tests for the terminal-clarification wire shape and its reply.
//
// A terminal clarification is a `task_clarification` bound to a REAL
// task id, offering real options plus exactly one trailing `other`, and
// carrying no `choice_details`. A document choice stays run-bound and
// may carry `task_id=""`. The reply keeps the original question
// association, and a free-text answer is `choice_id="other"` with
// non-blank `free_text`.
//
// These are decoder/reply-helper tests only: no DOM, no store, no
// network. They pin the wire contract the host and the surface share.

import { describe, expect, it } from "vitest";

import {
  OTHER_CHOICE_ID,
  hasOtherChoice,
  isFreeTextOnly,
  isSubmittable,
  listedChoices,
  openClarification,
  selectChoice,
  setOtherText,
  submitPayload,
} from "./capabilities/clarification/clarificationReply";
import {
  documentChoicePick,
  documentChoiceView,
  isDocumentChoice,
  parsePendingQuestion,
} from "./capabilities/task/taskRuntime";
import type { PendingQuestionView } from "./types";

/** A task-bound clarification carrying real document/company options. */
const TERMINAL_CLARIFICATION = {
  question_id: "q-terminal",
  task_id: "task-7",
  task_version: 2,
  prompt: "要比较哪一份资料：ZEUS 年报还是 ACME 年报？",
  choices: ["ZEUS 2024 年报", "ACME 2024 年报", OTHER_CHOICE_ID],
  input_version: 3,
  purpose: "task_clarification",
  origin_run_id: null,
  choice_details: [],
} as const;

/** The same question with no real candidate: free text only. */
const FREE_TEXT_CLARIFICATION = {
  ...TERMINAL_CLARIFICATION,
  question_id: "q-free-text",
  prompt: "请用一句话补充你要问的资料。",
  choices: [OTHER_CHOICE_ID],
} as const;

const DOCUMENT_CHOICE = {
  question_id: "q-doc",
  task_id: "",
  task_version: 1,
  prompt: "要用哪一份资料对比？",
  choices: ["doc-a", OTHER_CHOICE_ID],
  input_version: 1,
  purpose: "document_choice",
  origin_run_id: "run-7",
  choice_details: [
    {
      choice_id: "doc-a",
      label: "ZEUS 2024 年报",
      summary: "一份可读的 ZEUS 年报。",
      source_ref: {
        source_kind: "corpus",
        source_id: "zeus-handbook",
        version: "v1",
        section: null,
      },
    },
  ],
} as const;

function decoded(value: unknown): PendingQuestionView {
  const question = parsePendingQuestion(value);
  expect(question).not.toBeNull();
  return question as PendingQuestionView;
}

describe("terminal clarification decode", () => {
  it("accepts a real-task-id clarification with one trailing other", () => {
    const question = decoded(TERMINAL_CLARIFICATION);

    expect(question.purpose).toBe("task_clarification");
    expect(question.task_id).toBe("task-7");
    expect(question.choice_details).toEqual([]);
    // The host appends exactly one trailing Other; this surface neither
    // adds a second one nor strips the real options.
    expect(question.choices).toEqual([
      "ZEUS 2024 年报",
      "ACME 2024 年报",
      "other",
    ]);
    expect(question.choices.filter((id) => id === OTHER_CHOICE_ID)).toHaveLength(
      1,
    );
    expect(question.choices[question.choices.length - 1]).toBe(OTHER_CHOICE_ID);
    expect(hasOtherChoice(question.choices)).toBe(true);
  });

  it("rejects a clarification with an empty task id", () => {
    expect(
      parsePendingQuestion({ ...TERMINAL_CLARIFICATION, task_id: "" }),
    ).toBeNull();
    expect(
      parsePendingQuestion({ ...TERMINAL_CLARIFICATION, task_id: "   " }),
    ).toBeNull();
  });

  it("still accepts a document choice whose task id is empty", () => {
    const question = decoded(DOCUMENT_CHOICE);

    expect(isDocumentChoice(question)).toBe(true);
    expect(question.task_id).toBe("");
    expect(question.purpose).toBe("document_choice");
    // The pick is the original run association, not a task id.
    expect(documentChoicePick(question, "doc-a")).toEqual({
      question_id: "q-doc",
      choice_id: "doc-a",
      payload: null,
    });
    expect(documentChoicePick(question, "not-a-candidate")).toBeNull();
    const view = documentChoiceView(question);
    expect(view).not.toBeNull();
    expect(view?.origin_run_id).toBe("run-7");
    expect(view?.candidates[0].label).toBe("ZEUS 2024 年报");
  });

  it("does not invent options the host did not send", () => {
    const question = decoded(TERMINAL_CLARIFICATION);
    const draft = openClarification(question);

    // A pick outside the host's list is inert.
    expect(selectChoice(draft, "company")).toBe(draft);
    expect(selectChoice(draft, "period")).toBe(draft);
    expect(listedChoices(question.choices)).toEqual([
      "ZEUS 2024 年报",
      "ACME 2024 年报",
    ]);
  });
});

describe("free-text-only clarification reply", () => {
  it("accepts a question whose only offered choice is other", () => {
    const question = decoded(FREE_TEXT_CLARIFICATION);

    expect(question.choices).toEqual([OTHER_CHOICE_ID]);
    expect(listedChoices(question.choices)).toEqual([]);
    expect(hasOtherChoice(question.choices)).toBe(true);
    expect(isFreeTextOnly(question.choices)).toBe(true);
    expect(isFreeTextOnly(TERMINAL_CLARIFICATION.choices)).toBe(false);
  });

  it("makes the free text itself the answer, sending other plus the question id", () => {
    const question = decoded(FREE_TEXT_CLARIFICATION);
    const opened = openClarification(question);
    expect(opened.questionId).toBe("q-free-text");
    expect(opened.selectedChoiceId).toBeNull();
    // There is no real option to click, so blank text is the only
    // incomplete state.
    expect(isSubmittable(opened)).toBe(false);
    expect(submitPayload(opened)).toBeNull();

    const typed = setOtherText(opened, "  ZEUS 2024 年的年假  ");
    expect(typed.selectedChoiceId).toBeNull();
    expect(isSubmittable(typed)).toBe(true);
    expect(submitPayload(typed)).toEqual({
      questionId: "q-free-text",
      choiceId: OTHER_CHOICE_ID,
      freeText: "  ZEUS 2024 年的年假  ",
    });
  });

  it("keeps the same question association for a listed choice", () => {
    const question = decoded(TERMINAL_CLARIFICATION);
    const chosen = selectChoice(
      openClarification(question),
      "ZEUS 2024 年报",
    );

    expect(submitPayload(chosen)).toEqual({
      questionId: "q-terminal",
      choiceId: "ZEUS 2024 年报",
      freeText: null,
    });
  });
});
