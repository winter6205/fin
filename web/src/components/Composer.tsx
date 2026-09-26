// Composer: textarea; Enter sends, Shift+Enter newline; disabled while
// the active run is in flight or while an attachment upload is still
// running; empty submit is a no-op (server also enforces 204).
//
// ADRs 0047/0089: the thread-attach door lives
// here — a file picker button AND a drop on the composer both upload
// the file to POST /threads/{id}/attachments. Attachments persist for
// the WHOLE thread (no longer one-use): each send names the READY ones
// through ``attachmentsForSend``, and whatever that rule leaves out is
// named on screen instead of vanishing. This door never touches
// /api/corpus/ingest.

import { useEffect, useRef, useState } from "react";
import {
  ATTACH_ONE_FILING_HINT,
  ATTACH_UPLOAD_IN_FLIGHT_HINT,
  describeAttachFailure,
  describeAttachmentStatus,
  type AttachFailureNotice,
} from "../attachFailure";
import { AttachFailedError } from "../api";
import { describeSendFailure, type SendFailureNotice } from "../sendFailure";
import { attachmentsForSend, hasUploadInFlight, useWorkspaceStore } from "../store";

interface ComposerProps {
  threadId: string;
}

/**
 * Why the send is unavailable, and what the control says about it.
 *
 * ``busy`` alone means a RUN is live (it drives Stop and the 运行中
 * label); an upload holding the door is not a running turn and must not
 * read as one.
 */
function sendGate(busy: boolean, uploadInFlight: boolean) {
  if (uploadInFlight) return { held: true, label: "上传中…" };
  return { held: busy, label: busy ? "运行中…" : "发送" };
}

export default function Composer({ threadId }: ComposerProps) {
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
  // The upload gate is store state, not local picker state: it holds
  // for a file the question never mentions, and it is the ONE predicate
  // ``sendQuestion`` / ``createThread`` refuse with — so the control and
  // the store can never disagree about who is blocked. Its scope is
  // deliberately wider than the host's: this door holds the composer for
  // an upload on ANY thread because the composer itself is global, while
  // the host refuses only the thread its own upload is landing in.
  const uploadInFlight = useWorkspaceStore((s) =>
    hasUploadInFlight(s.pendingUploads),
  );
  const { held, label: sendLabel } = sendGate(busy, uploadInFlight);
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

  // ----- ADR 0086: the persistent attachment list -----
  // The thread's records live in the store (they outlive one send and
  // one mount); the store's readiness poll keeps ``parsing`` chips
  // moving to ready / failed while this composer is open.
  const attachFile = useWorkspaceStore((s) => s.attachFile);
  const loadAttachments = useWorkspaceStore((s) => s.loadAttachments);
  const retryAttachment = useWorkspaceStore((s) => s.retryAttachment);
  const attachments = useWorkspaceStore((s) => s.attachments);
  // Derived, never stored: the chips the reference rule leaves out are on
  // screen the same moment they stop being left out.
  const { withheld } = attachmentsForSend(attachments);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachFailure, setAttachFailure] =
    useState<AttachFailureNotice | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const attachInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void loadAttachments(threadId);
  }, [threadId, loadAttachments]);

  const attach = async (file: File) => {
    if (attachBusy || uploadInFlight) return;
    setAttachBusy(true);
    setAttachFailure(null);
    try {
      await attachFile(threadId, file);
    } catch (error) {
      setAttachFailure(describeAttachFailure(error));
    } finally {
      setAttachBusy(false);
    }
  };

  const retry = async (attachmentId: string) => {
    try {
      await retryAttachment(threadId, attachmentId);
    } catch (error) {
      setAttachFailure(describeAttachFailure(error));
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

  const submit = async () => {
    const q = draft.trim();
    if (!q) return; // Empty submit no-op.
    // Guarded here, not only by the disabled control: Enter on the
    // textarea reaches ``submit`` directly, and a send started while an
    // upload is still writing its bytes is refused by the store anyway.
    if (held) return;
    setDraft("");
    setFailure(null);
    // The store owns the one reference rule. Only a READY attachment is
    // named, so a question unrelated to an uploaded or still-parsing one
    // is never refused on its account; what was left out is rendered
    // below.
    const { ids } = attachmentsForSend(attachments);
    try {
      await sendQuestion(threadId, q, thinkingOn, ids);
    } catch (error) {
      // The draft was cleared optimistically and the textarea held
      // the only copy of the question — put it back so the operator
      // can retry once the cause clears. An attachment refusal
      // (parsing reference) is ATTACHMENT copy, not send copy.
      if (error instanceof AttachFailedError) {
        setAttachFailure(describeAttachFailure(error));
      } else {
        setFailure(describeSendFailure(error));
      }
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
      {attachments.length > 0 ? (
        <ul
          data-testid="thread-attachments"
          className="mb-2 flex flex-wrap gap-1"
        >
          {attachments.map((a) => (
            <li
              key={a.attachment_id}
              data-attachment-status={a.status}
              className="flex items-center gap-1 border border-black bg-tool px-2 py-0.5 text-xs"
            >
              <span className="font-mono">{a.filename}</span>
              <span
                data-testid={`attachment-status-${a.attachment_id}`}
                className="text-gray-600"
              >
                {describeAttachmentStatus(a)}
              </span>
              {a.status === "failed" ? (
                <button
                  type="button"
                  data-testid={`attachment-retry-${a.attachment_id}`}
                  aria-label={`重试解析附件 ${a.filename}`}
                  onClick={() => void retry(a.attachment_id)}
                  className="border border-black bg-card px-1 text-gray-700 hover:bg-tool"
                >
                  重试
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {withheld.length > 0 ? (
        <p
          data-testid="attachment-withheld"
          className="mb-2 text-xs text-gray-600"
        >
          {`本轮不会引用：${withheld
            .map((a) => `${a.filename}（${describeAttachmentStatus(a)}）`)
            .join("、")}。就绪后它会自动随后续问题带上。`}
        </p>
      ) : null}
      {uploadInFlight ? (
        <p
          data-testid="attach-upload-pending"
          className="mb-2 text-xs text-gray-600"
        >
          {ATTACH_UPLOAD_IN_FLIGHT_HINT}
        </p>
      ) : null}
      {dragOver ? (
        <p
          data-testid="attach-drop-hint"
          className="mb-2 text-xs text-gray-500"
        >
          松开以上传附件（本对话内持续可引用，不入库）
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
        disabled={held}
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
          {/* ADR 0047: the attach door's picker.
              This button is attach ONLY — corpus ingest lives in the
              home corpus area and is never reachable from the
              composer. */}
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
              title="上传附件（本对话内持续可引用，不入库）"
              onClick={() => attachInputRef.current?.click()}
              className="border border-black bg-card px-2 py-0.5 text-xs hover:bg-tool disabled:opacity-50"
            >
              {attachBusy ? "上传中…" : "附加文件…"}
            </button>
            <span className="text-xs text-gray-400">
              本对话内持续可引用，不入库
            </span>
          </span>
          {/* Advice on how to work one filing at a time — not a bound.
              Nothing caps how many attachments a turn may carry, and the
              server owns every byte / page / call limit, so this copy
              quotes none of them. */}
          <p data-testid="attach-one-filing-hint" className="text-xs text-gray-400">
            {ATTACH_ONE_FILING_HINT}
          </p>
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
          disabled={held || !draft.trim()}
          className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-accent hover:text-white disabled:opacity-50"
        >
          {sendLabel}
        </button>
      </div>
    </form>
  );
}
