// Sidebar: 280px, two-entry chrome (知识库 / 监控) + 会话 list.
// T1 (specs/workbench-sidebar-rail.md) retires the 对话 chrome row;
// research threads live in the 会话 list under a 1px black rule.
// T2 will wrap the thread list in a foldable 会话 disclosure; for T1
// the rule separates chrome (brand + two nav entries) from the
// thread list area, and a placeholder paragraph marks where the
// disclosure header will land so the rule's job is visible. Icons
// are 16px stroke SVG using currentColor so they follow the
// active/inactive text color — no emoji, no new icon package.
// Active thread highlighted with the single blue accent. T3: the
// StatusStrip footer left the sidebar; an unreachable retrieve
// paints a non-text-only mark on the 监控 entry instead.

import * as Dialog from "@radix-ui/react-dialog";
import { useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  activeNavKey,
  monitorNavMark,
  NAV_ENTRIES,
  type NavKey,
} from "../navChrome";
import { workspaceStatusView } from "../statusStrip";
import { useWorkspaceStore } from "../store";

export function truncateTitle(title: string, max = 50): string {
  if (title.length <= max) return title;
  return title.slice(0, max) + "…";
}

/** 16px stroke SVG glyph for a chrome entry. currentColor so the
 * icon follows the active/inactive text color (no per-state color
 * drift). Inline + hand-drawn: no icon-package dependency added. */
function NavIcon({ name }: { name: NavKey }): ReactNode {
  if (name === "kb") {
    // Open book — two pages, spine on the left.
    return (
      <svg
        aria-hidden="true"
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="square"
        strokeLinejoin="miter"
      >
        <path d="M2 3h5v10H2zM9 3h5v10H9z" />
        <path d="M8 3v10" />
      </svg>
    );
  }
  // monitor: pulse / radar sweep — circle with a corner wedge, the
  // icon the workbench has used for 监控 elsewhere.
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="square"
      strokeLinejoin="miter"
    >
      <circle cx="8" cy="8" r="5" />
      <path d="M8 3 a5 5 0 0 1 4.33 2.5" />
      <path d="M12.33 5.5 l1.67 -1 l-1 1.67" />
    </svg>
  );
}

function DeleteThreadDialog({
  threadId,
  threadTitle,
}: {
  threadId: string;
  threadTitle: string;
}) {
  const removeThread = useWorkspaceStore((s) => s.removeThread);
  const navigate = useNavigate();

  return (
    <Dialog.Root>
      <Dialog.Trigger asChild>
        <button
          aria-label={`删除 ${truncateTitle(threadTitle)}`}
          className="border border-black px-1 text-xs leading-none hover:bg-black hover:text-white"
        >
          x
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-[min(90vw,24rem)] -translate-x-1/2 -translate-y-1/2 border border-black bg-card p-5">
          <Dialog.Title className="text-base font-semibold mb-2">
            删除此 thread？
          </Dialog.Title>
          <Dialog.Description className="text-sm text-gray-600 mb-4">
            「{truncateTitle(threadTitle)}」及其所有 turn
            将被移除，此操作不可撤销。
          </Dialog.Description>
          <div className="flex justify-end gap-2">
            <Dialog.Close asChild>
              <button className="border border-black px-3 py-1 text-sm hover:bg-tool">
                取消
              </button>
            </Dialog.Close>
            <Dialog.Close asChild>
              <button
                onClick={async () => {
                  await removeThread(threadId);
                  navigate("/");
                }}
                className="border border-black bg-accent px-3 py-1 text-sm text-white hover:opacity-90"
              >
                删除
              </button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export default function Sidebar() {
  const threads = useWorkspaceStore((s) => s.threads);
  const createThread = useWorkspaceStore((s) => s.createThread);
  const location = useLocation();
  const activeKey = activeNavKey(location.pathname);
  // T3: a healthy /api/status that answers ``unreachable`` paints a
  // non-text-only glyph next to the 监控 label. Route-level failures
  // (``unavailable``) intentionally do NOT mark — those get their
  // degraded copy on /monitor itself, never a sidebar alarm. T1
  // leaves the presentation alone — T3 swaps the pebble for label
  // copy + icon.
  const status = useWorkspaceStore((s) => s.status);
  const statusLoaded = useWorkspaceStore((s) => s.statusLoaded);
  const monitorMark = monitorNavMark(workspaceStatusView(status, statusLoaded));

  return (
    <aside className="w-[280px] shrink-0 border-r border-black bg-page flex flex-col h-full">
      {/* T1: chrome = brand + two nav entries. The 1px rule on this
          block separates chrome from the 会话 list area below; T2
          will lift the thread list into a foldable 会话 disclosure. */}
      <div className="border-b border-black">
        <div className="p-4">
          <h1 className="text-lg font-bold tracking-tight">fin</h1>
          <p className="text-xs text-gray-600">企业知识库助手</p>
        </div>
        <nav aria-label="工作台" className="px-3 pb-3">
          <ul className="space-y-1">
            {NAV_ENTRIES.map((entry) => (
              <li key={entry.key}>
                <NavLink
                  to={entry.route}
                  className={() => {
                    // No chrome entry points at ``/`` now, so no
                    // need for ``end`` here — NavLink's isActive
                    // agrees with the helper for both /kb and
                    // /monitor. / and /t/{id} both light null.
                    const lit = activeKey === entry.key;
                    return `flex items-center gap-2 border px-2 py-1.5 text-sm font-medium ${
                      lit
                        ? "border-accent bg-accent text-white"
                        : "border-transparent hover:border-black"
                    }`;
                  }}
                >
                  <NavIcon name={entry.key} />
                  <span className="flex-1">{entry.label}</span>
                  {entry.key === "monitor" && monitorMark ? (
                    <span
                      aria-label={monitorMark.aria}
                      title={monitorMark.aria}
                      data-testid="monitor-degraded-mark"
                      className="inline-block h-2 w-2 rounded-full bg-accent text-accent"
                    >
                      {monitorMark.glyph}
                    </span>
                  ) : null}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </div>
      {/* T2 placeholder: the 会话 disclosure header lands here, so
          the rule above is the visual split between chrome and the
          会话 surface (sessions + 新建 + thread list). For T1 the
          thread list + 新建 stay directly below the rule. */}
      <div className="p-3">
        <NewThreadButton onCreate={() => createThread(null)} />
      </div>
      <nav aria-label="会话" className="flex-1 overflow-y-auto">
        <ul>
          {threads.map((t) => (
            <li key={t.id} className="group flex items-center px-3 py-1">
              <NavLink
                to={`/t/${t.id}`}
                className={({ isActive }) =>
                  `flex-1 truncate border px-2 py-1.5 text-sm ${
                    isActive
                      ? "border-accent bg-accent text-white"
                      : "border-transparent hover:border-black"
                  }`
                }
                title={t.title}
              >
                {truncateTitle(t.title)}
              </NavLink>
              <span className="ml-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100">
                <DeleteThreadDialog threadId={t.id} threadTitle={t.title} />
              </span>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}

function NewThreadButton({
  onCreate,
}: {
  onCreate: () => Promise<string | void>;
}) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  return (
    <button
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          const id = await onCreate();
          if (typeof id === "string" && id) navigate(`/t/${id}`);
        } finally {
          setBusy(false);
        }
      }}
      className="w-full border border-black bg-card px-3 py-2 text-sm font-medium hover:bg-accent hover:text-white"
    >
      + 新建 thread
    </button>
  );
}
