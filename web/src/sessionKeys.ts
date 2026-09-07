/** Session gates (T1 chrome + /kb, T2 /monitor): the chrome surfaces
 * the operator picked this session must not bounce a returning
 * visit into a different one. Three independent gates:
 *  - ``fin.corpusHome`` lights when the operator chose the legacy
 *    corpus-home nav (kept for the deprecated label).
 *  - ``fin.kb`` lights when they visited ``/kb``.
 *  - ``fin.monitor`` lights when they visited ``/monitor``.
 * Any one set keeps the restore logic on the surface they picked. */

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

/** T2 (specs/workbench-chrome.md): the monitor nav sets this gate
 * so a returning visit lands on /monitor instead of bouncing the
 * operator back to their last /t/{id}. Same role as ``fin.kb`` —
 * the operator picked a chrome surface and the restore must respect
 * it. */
export const MONITOR_GATE_KEY = "fin.monitor";

export function markMonitorVisit(): void {
  try {
    window.sessionStorage.setItem(MONITOR_GATE_KEY, "1");
  } catch {
    /* storage unavailable */
  }
}
