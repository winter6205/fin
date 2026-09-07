// T3 (specs/workbench-chrome.md): the 监控 page owns the boot-scoped
// status copy once the Sidebar footer StatusStrip is gone. SC7 keeps
// the panel never-blank across the five view states (loading /
// unavailable / unreachable / reachable-fake / reachable-mcp); the
// **重连** button re-GETs ``GET /api/status`` through the store action
// without a full document reload, with an in-flight guard so a
// double-click does not look like success twice (no new MCP handshake,
// no Python changes — just the existing ``/api/status`` route).

import { useWorkspaceStore } from "../store";
import {
  describeMonitorStatus,
  workspaceStatusView,
} from "../statusStrip";

export default function MonitorStatusPanel() {
  const status = useWorkspaceStore((s) => s.status);
  const statusLoaded = useWorkspaceStore((s) => s.statusLoaded);
  const loadingStatus = useWorkspaceStore((s) => s.loadingStatus);
  const loadStatus = useWorkspaceStore((s) => s.loadStatus);

  const copy = describeMonitorStatus(workspaceStatusView(status, statusLoaded));
  const degraded = copy.tone === "degraded";

  return (
    <section
      aria-label="工作区状态"
      data-testid="monitor-status-panel"
      className={`mb-4 border border-black p-4 text-sm ${
        degraded ? "bg-tool" : "bg-card"
      }`}
    >
      <p
        data-testid="monitor-status-headline"
        className={`font-semibold ${
          degraded
            ? "border border-black bg-black px-1.5 py-0.5 text-white"
            : "text-gray-700"
        }`}
      >
        {copy.headline}
      </p>
      <p
        data-testid="monitor-status-detail"
        className="mt-1 leading-snug text-gray-700"
      >
        {copy.detail}
      </p>
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          data-testid="monitor-reconnect"
          disabled={loadingStatus}
          onClick={() => {
            void loadStatus();
          }}
          className="border border-black bg-card px-3 py-1 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {loadingStatus ? "重连中…" : "重连"}
        </button>
      </div>
    </section>
  );
}
