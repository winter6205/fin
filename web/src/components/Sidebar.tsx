// Sidebar: 280px, brand + left-corner 知识 MCP line + T4 bound
// 资料集合 line + 会话 disclosure.
// Spec workbench-retrieve-product-seam retires the two nav entries
// (知识库 + the status page): the sidebar has no nav chrome (the 会话
// list below is a content list, not nav). The left-corner line reports
// the boot-scoped
// 知识 MCP three-state via ``knowledgeMcpLine`` (same snapshot as
// ``GET /api/status``, no polling); T4 adds the sibling
// ``boundCollectionLine`` under it from the SAME snapshot — the name
// only, never a 就绪 claim. T2 (specs/workbench-sidebar-rail.md)
// wraps 新建 + the thread list in a foldable 会话 group under the
// brand; the disclosure is a real <button aria-expanded> so Enter/Space
// toggle it natively; folding is remembered for the session via
// readSessionsFolded / writeSessionsFolded (storage-unavailable safe,
// default unfolded). The folded rail still shows the thread count and
// an empty list shows the visible 「尚无对话」 state rather than a blank
// rail. Active thread highlighted with the single blue accent.

import * as Dialog from "@radix-ui/react-dialog";
import { useState } from "react";
// The create refusal is the same typed fault the send path renders; one
// copy helper keeps the two surfaces from naming it differently.
import { describeThreadCreateFailure } from "../sendFailure";
import { NavLink, useNavigate } from "react-router-dom";
import { boundCollectionLine, knowledgeMcpLine } from "../knowledgeMcp";
import { readSessionsOpen, writeSessionsFolded } from "../sessionKeys";
import { useWorkspaceStore } from "../store";

export function truncateTitle(title: string, max = 50): string {
  if (title.length <= max) return title;
  return title.slice(0, max) + "…";
}

/** id of the thread-list container so the disclosure button's
 * aria-controls points at what it expands. */
const SESSIONS_LIST_ID = "sidebar-sessions-list";

/** Shared empty-state copy — the sidebar rail and `/` say the same
 * thing about the same empty 会话 list, so the strings must not
 * drift apart. (HomePage imports this.) */
export const EMPTY_SESSIONS_COPY =
  "尚无对话 — 在下方提问，开始新的 research thread。";

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
  // Boot-scoped 知识 MCP line: the same snapshot ``GET /api/status``
  // feeds. ``loaded`` distinguishes "still checking" from "route
  // never answered" (both neutral) without accusing a port.
  const status = useWorkspaceStore((s) => s.status);
  const statusLoaded = useWorkspaceStore((s) => s.statusLoaded);
  const mcpLine = knowledgeMcpLine(status, statusLoaded);
  // T4: the 资料集合 this process is bound to, from the SAME boot
  // snapshot. A sibling line, not a second state on the MCP line:
  // reachability and bind are different facts. It names the
  // collection and never claims the collection is 就绪.
  const bindLine = boundCollectionLine(status, statusLoaded);
  // Fold state starts from sessionStorage and persists on every
  // toggle, so the rail stays as the operator left it this session.
  // (Explicit over Implicit: the write happens on the event path,
  // not inside the setState updater, which StrictMode may run twice.)
  const [sessionsOpen, setSessionsOpen] = useState(() => readSessionsOpen());
  const toggleSessions = () => {
    const next = !sessionsOpen;
    writeSessionsFolded(!next, window.sessionStorage);
    setSessionsOpen(next);
  };

  return (
    <aside className="w-[280px] shrink-0 border-r border-black bg-page flex flex-col h-full">
      {/* Brand + the two boot-scoped left-corner lines (知识 MCP, then
          the bound 资料集合). The 1px rule on this block separates it
          from the 会话 disclosure below. The former nav entries left
          the product IA with the retired pages. */}
      <div className="border-b border-black">
        <div className="p-4">
          <h1 className="text-lg font-bold tracking-tight">fin</h1>
          <p className="text-xs text-gray-600">企业知识库助手</p>
          <p
            data-testid="knowledge-mcp-line"
            data-state={mcpLine.state}
            className={`mt-2 text-xs ${
              mcpLine.tone === "degraded"
                ? "text-red-700 font-medium"
                : "text-gray-500"
            }`}
          >
            {mcpLine.text}
          </p>
          {/* T4: the bound 资料集合, distinct from the line above and
              from 就绪 — the name only, no filing or chunk count. */}
          <p
            data-testid="bound-collection-line"
            data-state={bindLine.state}
            className={`mt-1 text-xs ${
              bindLine.tone === "ok" ? "text-gray-600" : "text-gray-500"
            }`}
          >
            {bindLine.text}
          </p>
        </div>
      </div>
      {/* T2: the 会话 disclosure — 新建 + thread links live inside the
          group, indented under the header. The rule above is the
          visual split between chrome and this group. The count stays
          visible when folded so threads are not lost; an empty list
          shows the dashed empty state instead of a blank rail. */}
      <div className="flex-1 overflow-y-auto p-3">
        {/* Spec Boundaries: 「h2 会话 as <button aria-expanded>」 —
            the h2 keeps 会话 in the document outline while the inner
            button stays the disclosure control. */}
        <h2 className="mb-2">
          <button
            type="button"
            aria-expanded={sessionsOpen}
            aria-controls={SESSIONS_LIST_ID}
            onClick={toggleSessions}
            className="flex w-full items-center gap-1 border border-black px-2 py-1.5 text-left text-sm font-semibold bg-card hover:bg-tool"
          >
            {/* Inline SVG chevron, same stroke language as NavIcon —
                not a text glyph, so it scales and inherits color. */}
            <svg
              aria-hidden="true"
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="square"
              className={`shrink-0 transition-transform ${
                sessionsOpen ? "" : "-rotate-90"
              }`}
            >
              <path d="M4 6l4 4 4-4" />
            </svg>
            <span className="flex-1">会话</span>
            <span className="text-xs font-normal text-gray-600">
              ({threads.length})
            </span>
          </button>
        </h2>
        {sessionsOpen ? (
          <div className="pl-3">
            <div className="mb-2">
              <NewThreadButton onCreate={() => createThread(null)} />
            </div>
            {/* The 会话 list IS navigation (thread links), so it keeps
                its nav landmark — only the retired 知识库 / 监控 entries
                left the chrome. */}
            <nav aria-label="会话">
              <ul id={SESSIONS_LIST_ID}>
                {threads.length === 0 ? (
                  <li>
                    <p className="border border-dashed border-black p-3 text-sm text-gray-500 bg-card">
                      {EMPTY_SESSIONS_COPY}
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
  // A rejected create must SAY why: the 知识 MCP gate refuses
  // thread creation on a 缺 / 坏 boot (ADR 0051), and a silent no-op
  // reads as a broken button. The same lines carry any other failure.
  const [failure, setFailure] = useState<string | null>(null);
  return (
    <div>
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setFailure(null);
          try {
            const id = await onCreate();
            if (typeof id === "string" && id) navigate(`/t/${id}`);
          } catch (error: unknown) {
            setFailure(describeThreadCreateFailure(error).message);
          } finally {
            setBusy(false);
          }
        }}
        className="w-full border border-black bg-card px-3 py-2 text-sm font-medium hover:bg-accent hover:text-white"
      >
        + 新建 thread
      </button>
      {failure ? (
        <p
          data-testid="new-thread-failure"
          className="mt-2 border border-black bg-card p-2 text-xs text-red-700"
        >
          {failure}
        </p>
      ) : null}
    </div>
  );
}
