import { describe, expect, it } from "vitest";
import {
  UNLABELLED_FORM,
  cardMetaLine,
  cardThreadQuestion,
  corpusSummary,
  formLabel,
} from "./corpusCards";
import type { Filing } from "./types";

function filing(overrides: Partial<Filing> = {}): Filing {
  return {
    document_id: "AAPL-2023Q1",
    company: "AAPL",
    period: "2023 Q1",
    version: "a".repeat(64),
    page_count: 31,
    form: "10-Q",
    ...overrides,
  };
}

describe("formLabel", () => {
  it("reads the form off the card payload", () => {
    expect(formLabel(filing({ form: "8-K" }))).toBe("8-K");
  });

  it("never hard-codes 10-Q for a 10-K row", () => {
    expect(formLabel(filing({ form: "10-K" }))).toBe("10-K");
  });

  it("labels a corpus row the catalog gave no form", () => {
    expect(formLabel(filing({ form: null }))).toBe(UNLABELLED_FORM);
  });

  it("treats a blank form the same as no form", () => {
    expect(formLabel(filing({ form: "   " }))).toBe(UNLABELLED_FORM);
  });

  it("labels a row from a peer that omits the key entirely", () => {
    const { form: _dropped, ...withoutForm } = filing();
    expect(formLabel(withoutForm as Filing)).toBe(UNLABELLED_FORM);
  });
});

describe("cardMetaLine", () => {
  it("puts issuer, form and period on the card", () => {
    expect(cardMetaLine(filing({ form: "10-K", period: "2023 FY" }))).toBe(
      "AAPL · 10-K · 2023 FY",
    );
  });

  it("still reads as three fields when the form is missing", () => {
    expect(cardMetaLine(filing({ form: null }))).toBe(
      `AAPL · ${UNLABELLED_FORM} · 2023 Q1`,
    );
  });
});

describe("cardThreadQuestion", () => {
  it("asks about the form the row actually is", () => {
    expect(
      cardThreadQuestion(filing({ form: "8-K", period: "2023-05-04" })),
    ).toBe("What does AAPL report in its 2023-05-04 8-K?");
  });

  it("falls back to a generic noun rather than an empty gap", () => {
    expect(cardThreadQuestion(filing({ form: null }))).toBe(
      "What does AAPL report in its 2023 Q1 filing?",
    );
  });
});

describe("corpusSummary", () => {
  it("counts the library by form so the operator sees the spread", () => {
    expect(
      corpusSummary([
        filing({ document_id: "ZEUS-2023-10K", form: "10-K" }),
        filing({ document_id: "ZEUS-2023-8K", form: "8-K" }),
        filing({ document_id: "ZEUS-2023Q1", form: "10-Q" }),
        filing({ document_id: "ZEUS-2023Q2", form: "10-Q" }),
      ]),
    ).toBe("4 份 · 10-K 1 · 10-Q 2 · 8-K 1");
  });

  it("reads as one line on a single-form corpus", () => {
    expect(corpusSummary([filing(), filing({ document_id: "AAPL-2023Q2" })])).toBe(
      "2 份 · 10-Q 2",
    );
  });

  it("says nothing beyond the count on an empty corpus", () => {
    expect(corpusSummary([])).toBe("0 份");
  });

  it("orders the forms deterministically regardless of card order", () => {
    const cards = [
      filing({ document_id: "B", form: "8-K" }),
      filing({ document_id: "A", form: "10-K" }),
    ];
    expect(corpusSummary(cards)).toBe(corpusSummary([...cards].reverse()));
  });
});
