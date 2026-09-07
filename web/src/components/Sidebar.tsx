// Sidebar: 280px, thread list, New thread button, delete with a
// Radix confirmation dialog. Active thread highlighted with the
// single blue accent. T1 (specs/workbench-chrome.md): the chrome row
// is the three-entry nav (对话 / 知识库 / 监控); the legacy "Corpus
// 首页" label is gone and the active chrome is keyed off the
// pathname via ``navChrome.ts``. T2 (specs/workbench-chrome.md): the
// watchlist + notice form moved off the sidebar onto /monitor; this
// surface is chrome + thread list only. T3: the StatusStrip footer
// leaves the sidebar — boot-scoped status copy now lives on
// /monitor, and an unreachable retrieve paints a non-text-only
// mark on the 监控 entry instead of a footer strip.

import * as Dialog from "@radix-ui/react-dialog";
import { useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  activeNavKey,
  monitorNavMark,
  NAV_ENTRIES,
} from "../navChrome";
import { workspaceStatusView } from "../statusStrip";
import { useWorkspaceStore } from "../store";

export function truncateTitle(title: string, max = 50): string {
  if (title.length <= max) return title;
  return title.slice(0, max) + "…";
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
  // degraded copy on /monitor itself, never a sidebar alarm.
  const status = useWorkspaceStore((s) => s.status);
  const statusLoaded = useWorkspaceStore((s) => s.statusLoaded);
  const monitorMark = monitorNavMark(workspaceStatusView(status, statusLoaded));

  return (
    <aside className="w-[280px] shrink-0 border-r border-black bg-page flex flex-col h-full">
      <div className="p-4 border-b border-black">
        <h1 className="text-lg font-bold tracking-tight">fin</h1>
        <p className="text-xs text-gray-600">10-Q 研究工作区</p>
      </div>
      <div className="p-3">
        <NewThreadButton onCreate={() => createThread(null)} />
      </div>
      <nav aria-label="工作台" className="flex-1 overflow-y-auto">
        <div className="px-3 py-1 mb-1 space-y-1">
          {NAV_ENTRIES.map((entry) => (
            <NavLink
              key={entry.key}
              to={entry.route}
              end={entry.route === "/"}
              className={() => {
                // Drive active off the pure helper so /t/{id} lights
                // 对话 (react-router's isActive wouldn't, since the
                // route is /t/{id}, not /). For /kb and /monitor the
                // helper agrees with NavLink's own isActive.
                const lit = activeKey === entry.key;
                return `block border px-2 py-1.5 text-sm font-medium ${
                  lit
                    ? "border-accent bg-accent text-white"
                    : "border-transparent hover:border-black"
                }`;
              }}
            >
              {entry.key === "monitor" && monitorMark ? (
                <span className="inline-flex items-center gap-2">
                  <span>{entry.label}</span>
                  <span
                    aria-label={monitorMark.aria}
                    title={monitorMark.aria}
                    data-testid="monitor-degraded-mark"
                    className="inline-block h-2 w-2 rounded-full bg-accent text-accent"
                  >
                    {monitorMark.glyph}
                  </span>
                </span>
              ) : (
                entry.label
              )}
            </NavLink>
          ))}
        </div>
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
