// Workbench chrome — the two-entry sidebar nav (specs/workbench-sidebar-rail.md, T1).
//
// The sidebar renders exactly these two entries, with these exact
// labels, and the route each one points at. The decision of which
// entry is "active" for the current pathname lives here as a pure
// function so the boundary is testable without a DOM and so the
// chrome never silently re-renders an English label, a "Corpus"
// word, or a retired 对话 row that the spec has removed from this
// surface.
//
// T1: the 对话 chrome entry is retired. The home route (`/`) and a
// thread route (`/t/{id}`) are NOT chrome — they are either a
// placeholder composer / last-thread restore (/) or a research-thread
// page whose highlight lives in the 会话 list below the chrome. So
// ``activeNavKey`` returns ``null`` for both: no chrome row lights.
//
// T3: ``monitorNavMark`` decides whether the 监控 entry carries a
// degraded warning. Loading + unavailable stay clean — only a healthy
// route that reports ``retrieve_reachability: unreachable`` paints
// the warning, and it is painted in icon + label text (「监控 · 搜索
// 断开」), never a right-side pebble/dot: color is not the sole cue.
// Route-level failures show their degraded copy on /monitor itself,
// never a sidebar alarm.

import type { WorkspaceStatusView } from "./statusStrip";

export type NavKey = "kb" | "monitor";

export interface NavEntry {
  key: NavKey;
  /** The label the Sidebar renders. Spec language — never English. */
  label: string;
  /** The route this entry's NavLink points at. */
  route: string;
}

/** The chrome entries in render order. */
export const NAV_ENTRIES: readonly NavEntry[] = [
  { key: "kb", label: "知识库", route: "/kb" },
  { key: "monitor", label: "监控", route: "/monitor" },
] as const;

/**
 * Which nav entry is active for the given pathname.
 *
 *  - `/`            → null  (no chrome row lights)
 *  - `/t/{id}`      → null  (a thread page is a list item, not chrome)
 *  - `/kb`          → 知识库
 *  - `/monitor`     → 监控
 *  - anything else  → null (no chrome lit; a 404 inside the app)
 */
export function activeNavKey(pathname: string): NavKey | null {
  if (pathname === "/kb") return "kb";
  if (pathname === "/monitor") return "monitor";
  return null;
}

export interface MonitorNavMark {
  /** The full label the Sidebar renders for the degraded 监控 entry. */
  label: string;
  /** Which icon state NavIcon renders for the entry. */
  icon: "warning";
  /** Plain-text alternative the aria-label reads aloud. */
  aria: string;
}

/**
 * Decide whether the 监控 sidebar entry gets a degraded warning.
 * Reserved for a HEALTHY route reporting an unreachable retrieve
 * port — the route-level failure state (``unavailable``) paints its
 * degraded copy on the /monitor page itself, never as a sidebar
 * alarm (SC: sidebar is chrome, not diagnostics). Returns ``null``
 * when no mark should render.
 */
export function monitorNavMark(
  view: WorkspaceStatusView,
): MonitorNavMark | null {
  if (view.state !== "ready") return null;
  if (view.status.retrieve_reachability !== "unreachable") return null;
  return {
    label: "监控 · 搜索断开",
    icon: "warning",
    aria: "搜索断开 — 检索不可用",
  };
}
