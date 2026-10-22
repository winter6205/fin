/**
 * The developer-mode switch (specs/demo-seed.md item 6, ADR 0100 decision
 * 6): run status, Runtime receipts and source evaluation are internal
 * accounting and stay out of the default user view.
 *
 * WHY storage and not a build flag: this app's display switches already
 * reach the browser the same way (``fin.thinking`` and
 * ``fin.sidebar.sessions.folded`` read a guarded ``window.localStorage`` /
 * ``sessionStorage`` key — see ``Composer.tsx`` / ``sessionKeys.ts``), so
 * the developer switch follows that mechanism rather than introducing a
 * parallel one. Storage is injected for testability; the UI passes
 * ``window.localStorage``.
 *
 * WHY absence means OFF: every read is guarded, and the fallback is the
 * DEFAULT view. An unreadable or throwing storage (privacy mode) must
 * never be the reason an internal receipt surface reaches a user, so a
 * switch that cannot be read renders the product a user is meant to see.
 *
 * Set it for a developer-mode run from the browser console:
 * ``localStorage.setItem("fin.developer-mode", "1")`` (or remove the key
 * to return to the default view).
 */
export const DEVELOPER_MODE_KEY = "fin.developer-mode";

/** Reads the flag off an injected Storage. Only the exact ``"1"`` is on:
 * a sloppy truthy read would let ``"0"`` or ``"false"`` switch the receipt
 * surface on, which is the one failure this default exists to prevent. */
export function readDeveloperMode(storage: Storage): boolean {
  try {
    return storage.getItem(DEVELOPER_MODE_KEY) === "1";
  } catch {
    return false; /* storage unavailable - default view. */
  }
}

/** The default-view wrapper: the single ``window.localStorage`` access,
 * kept here so an absent or inaccessible ``window`` falls back to the
 * default view in exactly one place. */
export function readDeveloperModeEnabled(): boolean {
  try {
    return readDeveloperMode(window.localStorage);
  } catch {
    return false; /* no storage at all - default view. */
  }
}

/** Writes the flag. Off REMOVES the key rather than storing ``"0"``, so
 * absence always reads as the default. */
export function writeDeveloperMode(enabled: boolean, storage: Storage): void {
  try {
    if (enabled) {
      storage.setItem(DEVELOPER_MODE_KEY, "1");
    } else {
      storage.removeItem(DEVELOPER_MODE_KEY);
    }
  } catch {
    /* storage unavailable - the switch just will not persist. */
  }
}
