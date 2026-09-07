// Copy for the 知识库 (/kb) cards.
//
// Spec Focus 2: the kb page lists form / period so the operator can
// see what is in the library. The catalog stopped being a 10-Q-only
// slice, so no string here may assume one — a 10-K row that reads
// "10-Q" on the card is worse than no label at all. Rendering lives in
// components/CorpusCard.tsx and pages/KnowledgeBasePage.tsx; the
// wording lives here so it is unit-testable without a DOM.

import type { Filing } from "./types";

/** Shown instead of a blank slot when the catalog row carries no form.
 * ``GET /api/corpus`` always sends the key, so this is the corpus
 * saying "unlabelled", not the SPA failing to read the payload. */
export const UNLABELLED_FORM = "未标注表单";

function formOf(filing: Filing): string {
  return typeof filing.form === "string" ? filing.form.trim() : "";
}

export function formLabel(filing: Filing): string {
  return formOf(filing) || UNLABELLED_FORM;
}

/** The card's second line: issuer, form, period. */
export function cardMetaLine(filing: Filing): string {
  return [filing.company, formLabel(filing), filing.period].join(" · ");
}

/** Title for the thread a card click opens. The composer prefill is
 * never auto-sent (spec), so this is a question the operator reads
 * before deciding to ask it. */
export function cardThreadQuestion(filing: Filing): string {
  const form = formOf(filing) || "filing";
  return `What does ${filing.company} report in its ${filing.period} ${form}?`;
}

/** "12 份 · 10-Q 12" — the library's size and its form spread. Forms
 * are ordered by label so two renders of the same corpus read the
 * same regardless of card order. */
export function corpusSummary(filings: Filing[]): string {
  const counts = new Map<string, number>();
  for (const filing of filings) {
    const label = formLabel(filing);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const parts = [...counts.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([label, count]) => `${label} ${count}`);
  return [`${filings.length} 份`, ...parts].join(" · ");
}
