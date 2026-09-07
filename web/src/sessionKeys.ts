/** Session gates (T1 chrome + /kb, T2 /monitor): the chrome surfaces
 * the operator picked this session must not bounce a returning
 * visit into a different one. Two independent gates:
 *  - ``fin.kb`` lights when they visited ``/kb``.
 *  - ``fin.monitor`` lights when they visited ``/monitor``.
 * Any one set keeps the restore logic on the surface they picked. */

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

/** T2 (specs/workbench-sidebar-rail.md): the 会话 disclosure remembers
 * its folded state for this session so a folded rail stays folded
 * across route changes. Storage may be unavailable (privacy mode),
 * so every access is guarded — an unreadable/unwritable flag just
 * means the default (unfolded). Storage is injected for testability;
 * the UI passes window.sessionStorage. */
export const SESSIONS_FOLDED_KEY = "fin.sidebar.sessions.folded";

export function readSessionsFolded(storage: Storage): boolean {
  try {
    return storage.getItem(SESSIONS_FOLDED_KEY) === "1";
  } catch {
    return false; /* storage unavailable - default unfolded. */
  }
}

export function writeSessionsFolded(folded: boolean, storage: Storage): void {
  try {
    if (folded) {
      storage.setItem(SESSIONS_FOLDED_KEY, "1");
    } else {
      // Remove rather than store "0" so absence always means the
      // default unfolded state.
      storage.removeItem(SESSIONS_FOLDED_KEY);
    }
  } catch {
    /* storage unavailable - fold state just will not persist. */
  }
}
