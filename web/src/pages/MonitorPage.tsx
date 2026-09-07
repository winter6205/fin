// /monitor: 监控 (specs/workbench-chrome.md).
//
// T1 placed the route + chrome label; T2 fills in the surface —
// watchlist CRUD + unread notices move here from the sidebar. T3
// (separate ticket) adds retrieve-down copy + a **重连** control
// once the StatusStrip footer leaves the sidebar.
//
// Visiting this page sets the ``fin.monitor`` session gate so a
// returning visit lands back on /monitor instead of bouncing the
// operator to their last /t/{id} — same role as ``markKbVisit``.

import { useEffect } from "react";
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
        <WatchlistPanel />
      </div>
    </main>
  );
}
