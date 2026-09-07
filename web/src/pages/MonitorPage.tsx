// /monitor: 监控 (specs/workbench-chrome.md).
//
// T1 placed the route + chrome label; T2 fills in the surface —
// watchlist CRUD + unread notices move here from the sidebar. T3
// adds the boot-scoped status panel above the watchlist: ordinary-
// person copy for loading / unavailable / unreachable / reachable-
// fake / reachable-mcp, plus a **重连** control that re-GETs
// ``GET /api/status`` without a full document reload.
//
// Visiting this page sets the ``fin.monitor`` session gate so a
// returning visit lands back on /monitor instead of bouncing the
// operator to their last /t/{id} — same role as ``markKbVisit``.

import { useEffect } from "react";
import MonitorStatusPanel from "../components/MonitorStatusPanel";
import WatchlistPanel from "../components/WatchlistPanel";
import { markMonitorVisit } from "../sessionKeys";

export default function MonitorPage() {
  // T2 chrome: visiting /monitor this session lights the monitor
  // gate so a returning visit lands back here instead of bouncing
  // the operator to their last /t/{id}. One-shot per mount so a
  // SPA-internal navigation from /monitor back to / does not keep
  // the gate fresh for a later deliberate choice.
  useEffect(() => {
    markMonitorVisit();
  }, []);

  return (
    <main className="p-6">
      <h2 className="text-xl font-bold mb-1">监控</h2>
      <p className="text-sm text-gray-600 mb-4">
        关注本地 filing 动态；未读通知在此 dismiss。
      </p>
      <div className="max-w-md border border-black bg-card p-4">
        <MonitorStatusPanel />
        <WatchlistPanel />
      </div>
    </main>
  );
}
