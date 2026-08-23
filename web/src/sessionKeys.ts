/** Session gate: operator chose Corpus home; skip lastThread restore (T4). */
export const CORPUS_HOME_GATE_KEY = "fin.corpusHome";

export function markCorpusHomeVisit(): void {
  try {
    window.sessionStorage.setItem(CORPUS_HOME_GATE_KEY, "1");
  } catch {
    /* storage unavailable */
  }
}
