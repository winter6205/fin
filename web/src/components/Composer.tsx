// Composer: textarea; Enter sends, Shift+Enter newline; disabled while
// the active run is in flight; empty submit is a no-op (server also
// enforces 204).
//
// corpus-serve-ingest T5 (ADR 0047): the thread-attach door lives
// here — a file picker button AND a drop on the composer both upload
// the file to POST /threads/{id}/attachments and reference the minted
// id on the NEXT send. This door never touches /api/corpus/ingest;
// corpus ingest lives in the home corpus area (IngestCorpusControl).

import { useRef, useState } from "react";
import {
  describeAttachFailure,
  type AttachFailureNotice,
} from "../attachFailure";
import { describeSendFailure, type SendFailureNotice } from "../sendFailure";
import type { ThreadAttachment } from "../types";
import { useWorkspaceStore } from "../store";

interface ComposerProps {
  threadId: string;
  /** External prefill (empty-corpus card click). */
  prefill?: string | null;
  onPrefillConsumed?: () => void;
}

export default function Composer({
  threadId,
  prefill,
  onPrefillConsumed,
}: ComposerProps) {
  const sendQuestion = useWorkspaceStore((s) => s.sendQuestion);
  const sending = useWorkspaceStore((s) => s.sendingQuestion);
  // T4 (spec line 38): disable the composer while the thread's
  // per-run lock is held (live GET signal) or while a resume is
  // streaming. ``in_flight`` covers both the pre-first-turn window
  // (thread-level) and the latest-turn window (turn-level).
  const threadInFlight = useWorkspaceStore(
    (s) => s.activeThread?.id === threadId && s.activeThread.in_flight === true,
  );
  const resumingThis = useWorkspaceStore(
    (s) => s.resumingThreadId === threadId,
  );
  const busy = sending || threadInFlight || resumingThis;
  // T4 workflow (the tracked change): Stop ends the in-flight run on this thread.
  // Shown only while something is running — the host answers 204 on an
  // idle thread, so a stale click is harmless, but a permanently
  // visible Stop button reads as if a run were always live.
  const stopRun = useWorkspaceStore((s) => s.stopRun);
  const stopping = useWorkspaceStore((s) => s.stoppingThreadId === threadId);
  // T3 (#34): the operator's GLOBAL thinking preference. Defaults
  // ON (spec line 28: default is on / `adaptive`); persisted in
  // localStorage so a reload keeps the choice. This is a thinking
  // on/off preference, NOT a step-by-step instruction to the model.
  const [thinkingOn, setThinkingOn] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem("fin.thinking") !== "off";
    } catch {
      return true;
    }
  });
  const [draft, setDraft] = useState("");
  // Last send that never produced a run (409 / 503 / transport).
  // Rendered above the textarea until the next submit clears it.
  const [failure, setFailure] = useState<SendFailureNotice | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // ----- T5 (ADR 0047): the thread-attach door -----
  // Files attached here are turn-scoped: uploaded to the attach
  // route, referenced once by the next send, never pushed. Pending
  // state is Composer-local (they are this-turn UI, not workspace
  // state); on a rejected send they are KEPT — a 409/503 send does
  // not consume them server-side, so the retry can still carry them.
  const attachFile = useWorkspaceStore((s) => s.attachFile);
  const [pending, setPending] = useState<ThreadAttachment[]>([]);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachFailure, setAttachFailure] =
    useState<AttachFailureNotice | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const attachInputRef = useRef<HTMLInputElement>(null);

  const attach = async (file: File) => {
    if (attachBusy) return;
    setAttachBusy(true);
    setAttachFailure(null);
    try {
      const attachment = await attachFile(threadId, file);
      setPending((current) => [...current, attachment]);
    } catch (error) {
      setAttachFailure(describeAttachFailure(error));
    } finally {
      setAttachBusy(false);
    }
  };

  const toggleThinking = () => {
    setThinkingOn((v) => {
      const next = !v;
      try {
        window.localStorage.setItem("fin.thinking", next ? "on" : "off");
      } catch {
        /* storage unavailable - preference is session-only. */
      }
      return next;
    });
  };

  // Apply external prefill once (corpus card click -> composer).
  const appliedPrefillRef = useRef<string | null>(null);
  if (prefill && prefill !== appliedPrefillRef.current) {
    appliedPrefillRef.current = prefill;
    setDraft(prefill);
    onPrefillConsumed?.();
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  const submit = async () => {
    const q = draft.trim();
    if (!q) return; // Empty submit no-op.
    setDraft("");
    setFailure(null);
    // T5: reference the attach door's ids on THIS send (consumed
    // server-side, single use).
    const attachmentIds = pending.map((a) => a.attachment_id);
    try {
      await sendQuestion(threadId, q, thinkingOn, attachmentIds);
      setPending([]); // The send consumed them (single use).
    } catch (error) {
      // The draft was cleared optimistically and the textarea held
      // the only copy of the question — put it back so the operator
      // can retry once the 409 / 503 cause clears. Pending
      // attachments stay: a rejected send did not consume them.
      setFailure(describeSendFailure(error));
      setDraft((current) => (current === "" ? q : current));
      textareaRef.current?.focus();
    }
  };

  return (
    <form
      className="border-t border-black p-3 bg-page"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const file = e.dataTransfer.files?.[0];
        if (file) void attach(file);
      }}
    >
      {attachFailure ? (
        <div
          role="alert"
          data-testid="attach-failure"
          data-failure-kind={attachFailure.kind}
          className="mb-2 flex items-start justify-between gap-3 border border-black bg-tool px-2 py-1.5 text-sm text-gray-800"
        >
          <span>{attachFailure.message}</span>
          <button
            type="button"
            aria-label="关闭附件提示"
            onClick={() => setAttachFailure(null)}
            className="shrink-0 text-xs text-gray-500 hover:text-black"
          >
            ✕
          </button>
        </div>
      ) : null}
      {failure ? (
        <div
          role="alert"
          data-testid="send-failure"
          data-failure-kind={failure.kind}
          className="mb-2 flex items-start justify-between gap-3 border border-black bg-tool px-2 py-1.5 text-sm text-gray-800"
        >
          <span>{failure.message}</span>
          <button
            type="button"
            aria-label="关闭提示"
            onClick={() => setFailure(null)}
            className="shrink-0 text-xs text-gray-500 hover:text-black"
          >
            ✕
          </button>
        </div>
      ) : null}
      {pending.length > 0 ? (
        <ul
          data-testid="pending-attachments"
          className="mb-2 flex flex-wrap gap-1"
        >
          {pending.map((a) => (
            <li
              key={a.attachment_id}
              className="flex items-center gap-1 border border-black bg-tool px-2 py-0.5 text-xs"
            >
              <span className="font-mono">{a.filename}</span>
              <button
                type="button"
                aria-label={`移除附件 ${a.filename}`}
                onClick={() =>
                  setPending((current) =>
                    current.filter((p) => p.attachment_id !== a.attachment_id),
                  )
                }
                className="text-gray-500 hover:text-black"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {dragOver ? (
        <p
          data-testid="attach-drop-hint"
          className="mb-2 text-xs text-gray-500"
        >
          松开以上传为本轮附件（仅本轮可读，不入库）
        </p>
      ) : null}
      <textarea
        ref={textareaRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void submit();
          }
        }}
        placeholder="输入问题…"
        disabled={busy}
        rows={3}
        className="w-full resize-none border border-black bg-card p-2 text-sm disabled:opacity-50"
      />
      <div className="mt-2 flex items-end justify-between gap-3">
        <div className="flex flex-col gap-1">
          <button
            type="button"
            role="switch"
            aria-checked={thinkingOn}
            onClick={toggleThinking}
            className="flex items-center gap-2 text-xs text-gray-700 select-none"
          >
            <span
              className={`relative inline-flex h-5 w-9 shrink-0 items-center border border-black p-0.5 ${
                thinkingOn ? "bg-[#2563EB]/10" : "bg-card"
              }`}
            >
              <span
                className={`block h-3.5 w-3.5 border border-black transition-transform ${
                  thinkingOn
                    ? "translate-x-4 bg-[#2563EB]"
                    : "translate-x-0 bg-page"
                }`}
              />
            </span>
            Thinking（推理）
          </button>
          {!thinkingOn ? (
            <span className="text-xs text-gray-500">本轮将跳过推理轨迹</span>
          ) : null}
          {/* T5 (ADR 0047): the thread-attach door's picker. This
              button is attach ONLY — corpus ingest lives in the home
              corpus area and is never reachable from the composer. */}
          <span className="flex items-center gap-2">
            <input
              ref={attachInputRef}
              type="file"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) void attach(file);
              }}
            />
            <button
              type="button"
              data-testid="attach-file"
              disabled={attachBusy}
              title="附加文本文件（仅本轮可读，不入库）"
              onClick={() => attachInputRef.current?.click()}
              className="border border-black bg-card px-2 py-0.5 text-xs hover:bg-tool disabled:opacity-50"
            >
              {attachBusy ? "上传中…" : "附加文件…"}
            </button>
            <span className="text-xs text-gray-400">仅本轮可读，不入库</span>
          </span>
        </div>
        <span className="text-xs text-gray-500">
          Enter 发送 · Shift+Enter 换行
        </span>
        {busy ? (
          <button
            type="button"
            data-testid="stop-run"
            disabled={stopping}
            onClick={() => {
              void stopRun(threadId);
            }}
            className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-black hover:text-white disabled:opacity-50"
          >
            {stopping ? "停止中…" : "停止"}
          </button>
        ) : null}
        <button
          type="submit"
          data-testid="composer-send"
          disabled={busy || !draft.trim()}
          className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {busy ? "运行中…" : "发送"}
        </button>
      </div>
    </form>
  );
}
