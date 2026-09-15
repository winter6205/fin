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

/** Sidebar-facing wrapper: returns true when the disclosure should
 * start open. ``window.sessionStorage`` itself may be inaccessible
 * (privacy mode throws on property access), so the default-unfolded
 * fallback lives here — in exactly one place. */
export function readSessionsOpen(): boolean {
  try {
    return !readSessionsFolded(window.sessionStorage);
  } catch {
    return true; /* no storage at all - default unfolded. */
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
