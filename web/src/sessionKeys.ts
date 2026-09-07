/** Session gates (T1 chrome + /kb, T2 /monitor): the chrome surfaces
 * the operator picked this session must not bounce a returning
 * visit into a different one. Two independent gates: ``fin.corpusHome``
 * lights when the operator chose the legacy corpus-home nav, and
 * ``fin.kb`` lights when they visited ``/kb``. Either keeps the
 * restore logic on the surface they picked. */

export const CORPUS_HOME_GATE_KEY = "fin.corpusHome";

export function markCorpusHomeVisit(): void {
  try {
    window.sessionStorage.setItem(CORPUS_HOME_GATE_KEY, "1");
  } catch {
    /* storage unavailable */
  }
}

/** T1 (specs/workbench-chrome.md): the knowledge-base nav sets this
 * gate so a returning visit lands on /kb instead of bouncing the
 * operator back to their last /t/{id}. */
export const KB_GATE_KEY = "fin.kb";

export function markKbVisit(): void {
  try {
    window.sessionStorage.setItem(KB_GATE_KEY, "1");
  } catch {
    /* storage unavailable */
  }
}
