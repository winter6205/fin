// Composer: textarea; Enter sends, Shift+Enter newline; disabled while
// the active run is in flight; empty submit is a no-op (server also
// enforces 204).

import { useRef, useState } from "react";
import { describeSendFailure, type SendFailureNotice } from "../sendFailure";
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
    try {
      await sendQuestion(threadId, q, thinkingOn);
    } catch (error) {
      // The draft was cleared optimistically and the textarea held
      // the only copy of the question — put it back so the operator
      // can retry once the 409 / 503 cause clears.
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
    >
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
        placeholder="输入关于 10-Q 的问题…"
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
        </div>
        <span className="text-xs text-gray-500">
          Enter 发送 · Shift+Enter 换行
        </span>
        <button
          type="submit"
          disabled={busy || !draft.trim()}
          className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {busy ? "运行中…" : "发送"}
        </button>
      </div>
    </form>
  );
}
