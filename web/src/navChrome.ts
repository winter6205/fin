// Workbench chrome — the three-entry sidebar nav (specs/workbench-chrome.md).
//
// The sidebar renders exactly these three entries, with these exact
// labels, and the route each one points at. The decision of which
// entry is "active" for the current pathname lives here as a pure
// function so the boundary is testable without a DOM and so the
// chrome never silently re-renders an English label or a "Corpus"
// word that the spec has retired from this surface.
//
// `/t/{id}` counts as 对话: opening a thread should still light the
// 对话 entry, not show three inactive labels.
//
// T3: ``monitorNavMark`` decides whether the 监控 entry carries a
// non-text-only degraded glyph. Loading + unavailable stay clean —
// only a healthy route that reports ``retrieve_reachability:
// unreachable`` paints the mark. Route-level failures show their
// degraded copy on /monitor itself, never a sidebar alarm.

import type { WorkspaceStatusView } from "./statusStrip";

export type NavKey = "chat" | "kb" | "monitor";

export interface NavEntry {
  key: NavKey;
  /** The label the Sidebar renders. Spec language — never English. */
  label: string;
  /** The route this entry's NavLink points at. */
  route: string;
}

/** The chrome entries in render order. */
export const NAV_ENTRIES: readonly NavEntry[] = [
  { key: "chat", label: "对话", route: "/" },
  { key: "kb", label: "知识库", route: "/kb" },
  { key: "monitor", label: "监控", route: "/monitor" },
] as const;

/**
 * Which nav entry is active for the given pathname.
 *
 *  - `/`            → 对话
 *  - `/t/{id}`      → 对话  (a thread page is still a 对话)
 *  - `/kb`          → 知识库
 *  - `/monitor`     → 监控
 *  - anything else  → null (no chrome lit; a 404 inside the app)
 */
export function activeNavKey(pathname: string): NavKey | null {
  if (pathname === "/") return "chat";
  if (pathname.startsWith("/t/")) return "chat";
  if (pathname === "/kb") return "kb";
  if (pathname === "/monitor") return "monitor";
  return null;
}

export interface MonitorNavMark {
  /** A non-text glyph the Sidebar paints next to the 监控 label. */
  glyph: string;
  /** Plain-text alternative the aria-label reads aloud. */
  aria: string;
}

/**
 * Decide whether the 监控 sidebar entry gets a non-text-only
 * degraded mark. The mark is reserved for a HEALTHY route reporting
 * an unreachable retrieve port — the route-level failure state
 * (``unavailable``) paints its degraded copy on the /monitor page
 * itself, never as a sidebar alarm (SC: sidebar is chrome, not
 * diagnostics). Returns ``null`` when no mark should render.
 */
export function monitorNavMark(
  view: WorkspaceStatusView,
): MonitorNavMark | null {
  if (view.state !== "ready") return null;
  if (view.status.retrieve_reachability !== "unreachable") return null;
  return {
    glyph: "●",
    aria: "检索不可用",
  };
}
