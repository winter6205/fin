// Composer: textarea; Enter sends, Shift+Enter newline; disabled while
// the active run is in flight; empty submit is a no-op (server also
// enforces 204).

import { useRef, useState } from "react";
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
    (s) => s.activeThread?.id === threadId && s.activeThread.in_flight === true
  );
  const resumingThis = useWorkspaceStore(
    (s) => s.resumingThreadId === threadId
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
    await sendQuestion(threadId, q, thinkingOn);
  };

  return (
    <form
      className="border-t border-black p-3 bg-page"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
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
        placeholder="Ask about the frozen 10-Qs…"
        disabled={busy}
        rows={3}
        className="w-full resize-none border border-black bg-card p-2 text-sm disabled:opacity-50"
      />
      <div className="mt-2 flex items-center justify-between">
        <label className="flex items-center gap-1.5 text-xs text-gray-500 select-none">
          <input
            type="checkbox"
            checked={thinkingOn}
            onChange={toggleThinking}
            className="accent-[#2563EB]"
          />
          Thinking
        </label>
        <span className="text-xs text-gray-500">
          Enter to send · Shift+Enter for newline
        </span>
        <button
          type="submit"
          disabled={busy || !draft.trim()}
          className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {busy ? "Running…" : "Send"}
        </button>
      </div>
    </form>
  );
}
