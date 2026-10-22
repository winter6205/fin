// What the document-choice card renders — and, just as importantly, what
// it must NOT render (GE-4).
//
// GE-4 (specs/qa-chain-grounded-experience.md:96) requires each of the ≤4
// hit-backed candidates to show "title/section/summary and trailing
// rephrase". The wire's ``choices`` array does NOT carry that text — it
// carries opaque ids (``choice-1``, ``choice-2``) — so a card that printed
// the choices would satisfy the letter of "shows the candidates" while
// showing the operator nothing they can act on. These tests pin the
// rendered markup against the human-readable text.
//
// Rendered through the prop-driven ``DocumentChoiceCardView`` with
// ``renderToString`` because this project's vitest setup has no DOM testing
// library (same split as ArchiveList.test.ts / VisualClaims.test.ts).

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { DocumentChoiceCardView } from "./DocumentChoiceCard";
import { documentChoiceSurface } from "../documentChoiceSurface";
import {
  DOCUMENT_CHOICE_OTHER_LABEL,
  parsePendingQuestion,
} from "../../task/taskRuntime";
import {
  openClarification,
  selectChoice,
  setOtherText,
} from "../clarificationReply";
import type { PendingQuestionView } from "../../../types";

const ORIGIN_RUN_ID = "77777777-7777-4777-8777-777777777777";

/** A two-candidate document choice, one with a named section and one
 * without, so both section renderings are covered. */
function documentChoice(overrides: Record<string, unknown> = {}): PendingQuestionView {
  return parsePendingQuestion({
    question_id: "q-doc",
    task_id: "",
    task_version: 1,
    prompt: "要用哪一份资料对比？",
    choices: ["choice-1", "choice-2", "other"],
    input_version: 1,
    purpose: "document_choice",
    origin_run_id: ORIGIN_RUN_ID,
    choice_details: [
      {
        choice_id: "choice-1",
        label: "AAPL 10-Q FY2023Q1",
        summary: "第一季度的营收与利润概述。",
        source_ref: {
          source_kind: "corpus",
          source_id: "AAPL-10Q-2023Q1",
          version: "v1",
          section: null,
        },
      },
      {
        choice_id: "choice-2",
        label: "AAPL 10-Q FY2023Q2",
        summary: "第二季度的营收与利润概述。",
        source_ref: {
          source_kind: "attachment",
          source_id: "att-22",
          version: "v1",
          section: "Section 2",
        },
      },
    ],
    ...overrides,
  })!;
}

function render(
  question: PendingQuestionView,
  draft = openClarification(question),
  submitting = false,
): string {
  const surface = documentChoiceSurface(question, draft, submitting)!;
  return renderToString(
    createElement(DocumentChoiceCardView, {
      surface,
      submitting,
      refusalKind: null,
      onSelectChoice: () => {},
      onOtherText: () => {},
      onSubmit: () => {},
    }),
  );
}

describe("the document-choice card renders GE-4's four candidate facts", () => {
  it("shows each candidate's title, section and summary", () => {
    const html = render(documentChoice());
    // Title (the document, not the wire id).
    expect(html).toContain("AAPL 10-Q FY2023Q1");
    expect(html).toContain("AAPL 10-Q FY2023Q2");
    // Section, where the source named one.
    expect(html).toContain("Section 2");
    // Summary, for every candidate.
    expect(html).toContain("第一季度的营收与利润概述。");
    expect(html).toContain("第二季度的营收与利润概述。");
  });

  it("never shows the opaque wire ids as candidate text", () => {
    const html = render(documentChoice());
    // The ids are rendered into test ids / data attributes only; the
    // operator-visible text is the document's title. Rendering "choice-1"
    // as the row's label is the defect this pins.
    const body = html.replace(/data-testid="[^"]*"/g, "");
    expect(body).not.toContain("choice-1");
    expect(body).not.toContain("choice-2");
  });

  it("shows the COMPLETE question verbatim before any candidate", () => {
    const html = render(documentChoice());
    const promptAt = html.indexOf("要用哪一份资料对比？");
    const firstCandidateAt = html.indexOf("AAPL 10-Q FY2023Q1");
    expect(promptAt).toBeGreaterThanOrEqual(0);
    expect(firstCandidateAt).toBeGreaterThan(promptAt);
  });

  it("shows the trailing rephrase with the contract's own label", () => {
    const html = render(documentChoice());
    expect(html).toContain(DOCUMENT_CHOICE_OTHER_LABEL);
    expect(DOCUMENT_CHOICE_OTHER_LABEL).toBe("换个问法");
    // It is a re-ask, not the clarification card's 其他.
    expect(html).not.toContain("其他</button>");
  });

  it("preselects nothing (question-workspace.md: no preselected answer)", () => {
    const html = render(documentChoice());
    expect(html).not.toContain('aria-pressed="true"');
  });

  it("marks only the clicked candidate as pressed", () => {
    const question = documentChoice();
    const html = render(question, selectChoice(openClarification(question), "choice-2"));
    expect(html).toContain('aria-pressed="true"');
    // Exactly one candidate is pressed — the one the operator clicked.
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(1);
  });
});

describe("the re-phrase box appears only for a re-phrase", () => {
  it("hides the text box until the rephrase is selected", () => {
    const question = documentChoice();
    expect(render(question)).not.toContain("document-choice-rephrase-text");
  });

  it("shows the typed re-phrase once the rephrase is selected", () => {
    const question = documentChoice();
    let draft = openClarification(question);
    draft = selectChoice(draft, "other");
    draft = setOtherText(draft, "换个角度，用现金流量表");
    const html = render(question, draft);
    expect(html).toContain("document-choice-rephrase-text");
    expect(html).toContain("换个角度，用现金流量表");
  });

  it("never attaches re-phrase text to a candidate pick", () => {
    // Moving from the rephrase to a candidate clears the text, so the
    // submitted reply can never carry free text beside a choice_id.
    const question = documentChoice();
    let draft = openClarification(question);
    draft = selectChoice(draft, "other");
    draft = setOtherText(draft, "换个角度");
    draft = selectChoice(draft, "choice-1");
    expect(draft.otherText).toBe("");
    expect(render(question, draft)).not.toContain("换个角度");
  });
});

describe("the submit control is disabled exactly when the host would refuse", () => {
  // The DISABLED ATTRIBUTE, not the ``disabled:opacity-50`` utility class
  // the enabled button also carries — matching on the whole tag would
  // satisfy both states.
  const submitTag = (html: string): string =>
    html.slice(
      html.indexOf("<button"),
      html.indexOf(">", html.indexOf('data-testid="document-choice-submit"')),
    );

  it("is disabled with nothing selected", () => {
    expect(submitTag(render(documentChoice()))).toMatch(/\sdisabled(\s|=|>)/);
  });

  it("is disabled for a rephrase with blank text", () => {
    // The host refuses an empty Other as ``task_input_invalid``, so the
    // button must not offer a request the host is known to reject.
    const question = documentChoice();
    const draft = selectChoice(openClarification(question), "other");
    expect(submitTag(render(question, draft))).toMatch(/\sdisabled(\s|=|>)/);
  });

  it("is enabled for a picked candidate", () => {
    const question = documentChoice();
    const html = render(question, selectChoice(openClarification(question), "choice-1"));
    expect(submitTag(html)).not.toMatch(/\sdisabled(\s|=|>)/);
  });
});

describe("the surface is document-choice only", () => {
  it("returns null for an ordinary task clarification", () => {
    // An ordinary clarification must keep rendering through
    // ClarificationCard; it may never acquire document-choice control
    // state (GE-4: "other threads receive no control state").
    const clarification = parsePendingQuestion({
      question_id: "q-1",
      task_id: "task-1",
      task_version: 1,
      prompt: "要比较哪个口径？",
      choices: ["合并", "母公司", "other"],
      input_version: 1,
    })!;
    expect(documentChoiceSurface(clarification, null, false)).toBeNull();
  });

  it("returns null for no question at all", () => {
    expect(documentChoiceSurface(null, null, false)).toBeNull();
  });
});
