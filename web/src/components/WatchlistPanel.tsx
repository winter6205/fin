// T4 workflow (#138): watchlist CRUD + in-app notices, in the sidebar.
//
// Spec Focus 3: the operator watches an issuer (optionally narrowed to
// form types) and sees a notice when a watched filing is in the LOCAL
// catalog. Nothing is emailed or posted out — this panel IS the
// delivery surface.
//
// Parsing / copy live in ../watchlist so they are unit-tested without
// a DOM; this file is layout plus store calls.

import { useEffect, useState } from "react";
import { useWorkspaceStore } from "../store";
import {
  noticeSummary,
  parseFormsInput,
  unreadCount,
  watchSummary,
} from "../watchlist";

export default function WatchlistPanel() {
  const watchlist = useWorkspaceStore((s) => s.watchlist);
  const notices = useWorkspaceStore((s) => s.notices);
  const filingMonitorStatus = useWorkspaceStore((s) => s.filingMonitorStatus);
  const addWatch = useWorkspaceStore((s) => s.addWatch);
  const removeWatch = useWorkspaceStore((s) => s.removeWatch);
  const dismissNotice = useWorkspaceStore((s) => s.dismissNotice);
  const loadWatchlist = useWorkspaceStore((s) => s.loadWatchlist);
  const loadNotices = useWorkspaceStore((s) => s.loadNotices);

  const [issuer, setIssuer] = useState("");
  const [forms, setForms] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void loadWatchlist();
    void loadNotices();
  }, [loadWatchlist, loadNotices]);

  const unread = notices.filter((n) => !n.read);

  const submit = async () => {
    const trimmed = issuer.trim();
    if (!trimmed) {
      setError("填写公司代码（如 AAPL）");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await addWatch(trimmed, parseFormsInput(forms));
      setIssuer("");
      setForms("");
    } catch {
      setError("添加失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-label="新文件提醒"
      className="shrink-0 border-t border-black px-3 py-2 text-xs"
    >
      <h2 className="font-semibold text-gray-700">
        新文件提醒
        {unread.length > 0 ? (
          <span
            data-testid="notice-count"
            className="ml-1.5 border border-black bg-accent px-1 text-white"
          >
            {unreadCount(notices)}
          </span>
        ) : null}
      </h2>

      {filingMonitorStatus === "degraded" ? (
        <p
          data-testid="filing-monitor-degraded"
          role="alert"
          className="mt-1 text-gray-700"
        >
          文件来源暂不可用；已保存的提醒还在。
        </p>
      ) : null}

      {unread.length > 0 ? (
        <ul data-testid="notice-list" className="mt-1.5 space-y-1">
          {unread.map((notice) => (
            <li
              key={notice.id}
              className="flex items-start justify-between gap-2 border border-black bg-tool px-2 py-1"
            >
              <span className="leading-snug">{noticeSummary(notice)}</span>
              <button
                type="button"
                aria-label={`忽略 ${notice.document_id} 的通知`}
                onClick={() => {
                  void dismissNotice(notice.id);
                }}
                className="shrink-0 text-gray-500 hover:text-black"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <ul className="mt-1.5 space-y-1">
        {watchlist.map((entry) => (
          <li
            key={entry.id}
            className="flex items-center justify-between gap-2"
          >
            <span className="truncate">
              <span className="font-mono">{entry.issuer}</span>
              <span className="ml-1 text-gray-500">{watchSummary(entry)}</span>
            </span>
            <button
              type="button"
              aria-label={`取消订阅 ${entry.issuer}`}
              onClick={() => {
                void removeWatch(entry.id);
              }}
              className="shrink-0 border border-black px-1 leading-none hover:bg-black hover:text-white"
            >
              x
            </button>
          </li>
        ))}
      </ul>

      <form
        className="mt-2 space-y-1"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          value={issuer}
          onChange={(e) => setIssuer(e.target.value)}
          placeholder="公司代码（如 AAPL）"
          aria-label="要订阅的公司代码"
          className="w-full border border-black bg-card px-1.5 py-1"
        />
        <input
          value={forms}
          onChange={(e) => setForms(e.target.value)}
          placeholder="文件类型（可留空，如 10-K 8-K）"
          aria-label="要订阅的文件类型"
          className="w-full border border-black bg-card px-1.5 py-1"
        />
        {error ? (
          <p role="alert" className="text-gray-700">
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={busy}
          className="w-full border border-black bg-card px-2 py-1 font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          + 订阅这家公司
        </button>
      </form>
    </section>
  );
}
