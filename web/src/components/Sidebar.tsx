// Sidebar: 280px, two-entry chrome (知识库 / 监控) + 会话 disclosure.
// T2 (specs/workbench-sidebar-rail.md) wraps 新建 + the thread list in
// a foldable 会话 group under the chrome's 1px black rule. The
// disclosure is a real <button aria-expanded> so Enter/Space toggle
// it natively; folding is remembered for the session via
// readSessionsFolded/writeSessionsFolded (storage-unavailable safe,
// default unfolded). The folded rail still shows the thread count so
// threads never feel lost, and an empty list shows the visible
// 「尚无对话」 empty state rather than a blank rail. Icons are 16px
// stroke SVG using currentColor — no emoji, no new icon package.
// Active thread highlighted with the single blue accent. T3: the
// StatusStrip footer left the sidebar; an unreachable retrieve paints
// the warning as 「监控 · 搜索断开」 + a warning icon variant — no
// right-side pebble, so color is never the sole cue.

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
import { readSessionsFolded, writeSessionsFolded } from "../sessionKeys";
import { useWorkspaceStore } from "../store";

export function truncateTitle(title: string, max = 50): string {
  if (title.length <= max) return title;
  return title.slice(0, max) + "…";
}

/** id of the thread-list container so the disclosure button's
 * aria-controls points at what it expands. */
const SESSIONS_LIST_ID = "sidebar-sessions-list";

/** 16px stroke SVG glyph for a chrome entry. currentColor so the
 * icon follows the active/inactive text color (no per-state color
 * drift). Inline + hand-drawn: no icon-package dependency added.
 * ``warning`` swaps the monitor pulse for an attention triangle —
 * T3: the degraded 监控 state is named in icon + label text. */
function NavIcon({
  name,
  warning = false,
}: {
  name: NavKey;
  warning?: boolean;
}): ReactNode {
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
  if (name === "monitor" && warning) {
    // Warning variant: attention triangle with an exclamation mark —
    // a distinct silhouette from the pulse circle, not just a color.
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
        <path d="M8 2 L14.5 13.5 H1.5 Z" />
        <path d="M8 6.5v3" />
        <path d="M8 11.5h0.01" />
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
  // T3: a healthy /api/status that answers ``unreachable`` paints the
  // warning in icon + label text on the 监控 entry (「监控 · 搜索断
  // 开」 + warning icon) — no right-side pebble. Route-level failures
  // (``unavailable``) intentionally do NOT mark — those get their
  // degraded copy on /monitor itself, never a sidebar alarm.
  const status = useWorkspaceStore((s) => s.status);
  const statusLoaded = useWorkspaceStore((s) => s.statusLoaded);
  const monitorMark = monitorNavMark(workspaceStatusView(status, statusLoaded));
  // Fold state starts from sessionStorage and persists on every
  // toggle, so the rail stays as the operator left it this session.
  const [sessionsOpen, setSessionsOpen] = useState(() => {
    try {
      return !readSessionsFolded(window.sessionStorage);
    } catch {
      return true; /* no storage at all - default unfolded. */
    }
  });
  const toggleSessions = () => {
    setSessionsOpen((open) => {
      writeSessionsFolded(!open, window.sessionStorage);
      return !open;
    });
  };

  return (
    <aside className="w-[280px] shrink-0 border-r border-black bg-page flex flex-col h-full">
      {/* T1: chrome = brand + two nav entries. The 1px rule on this
          block separates chrome from the 会话 disclosure below. */}
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
                  <NavIcon
                    name={entry.key}
                    warning={entry.key === "monitor" && monitorMark !== null}
                  />
                  <span
                    className="flex-1"
                    aria-label={
                      entry.key === "monitor" && monitorMark
                        ? monitorMark.aria
                        : undefined
                    }
                    title={
                      entry.key === "monitor" && monitorMark
                        ? monitorMark.aria
                        : undefined
                    }
                  >
                    {entry.key === "monitor" && monitorMark
                      ? monitorMark.label
                      : entry.label}
                  </span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </div>
      {/* T2: the 会话 disclosure — 新建 + thread links live inside the
          group, indented under the header. The rule above is the
          visual split between chrome and this group. The count stays
          visible when folded so threads are not lost; an empty list
          shows the dashed empty state instead of a blank rail. */}
      <div className="flex-1 overflow-y-auto p-3">
        <button
          type="button"
          aria-expanded={sessionsOpen}
          aria-controls={SESSIONS_LIST_ID}
          onClick={toggleSessions}
          className="mb-2 flex w-full items-center gap-1 border border-black px-2 py-1.5 text-left text-sm font-semibold bg-card hover:bg-tool"
        >
          <span aria-hidden="true" className="w-3 text-xs">
            {sessionsOpen ? "▾" : "▸"}
          </span>
          <span className="flex-1">会话</span>
          <span className="text-xs font-normal text-gray-600">
            ({threads.length})
          </span>
        </button>
        {sessionsOpen ? (
          <div className="pl-3">
            <div className="mb-2">
              <NewThreadButton onCreate={() => createThread(null)} />
            </div>
            <nav aria-label="会话">
              <ul id={SESSIONS_LIST_ID}>
                {threads.length === 0 ? (
                  <li>
                    <p className="border border-dashed border-black p-3 text-sm text-gray-500 bg-card">
                      尚无对话 — 在下方提问，或选择一份语料（侧栏 知识库）。
                    </p>
                  </li>
                ) : (
                  threads.map((t) => (
                    <li key={t.id} className="group flex items-center py-1">
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
                        <DeleteThreadDialog
                          threadId={t.id}
                          threadTitle={t.title}
                        />
                      </span>
                    </li>
                  ))
                )}
              </ul>
            </nav>
          </div>
        ) : null}
      </div>
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
