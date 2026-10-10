// Composer: textarea; Enter sends, Shift+Enter newline; held only by the
// single active-input decision (a host choice window or an in-flight
// upload), never by a live run; empty submit is a no-op (server also
// enforces 204).
//
// ADRs 0047/0089: the thread-attach door lives
// here — a file picker button AND a drop on the composer both upload
// the file to POST /threads/{id}/attachments. Attachments persist for
// the WHOLE thread (no longer one-use): each send names every
// referenceable one through ``attachmentsForSend`` — READY records plus
// the durable uploaded/parsing records a grounded run now waits on
// (T2f) — and whatever that rule leaves out is
// named on screen instead of vanishing. Each chip also carries a ``×``
// that deletes just that attachment, so "persist for the thread" never
// becomes "the operator cannot take it back". This door never touches
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
import { sendControlLabel, type ActiveInputDecision } from "../activeInput";
import { describeSendFailure, type SendFailureNotice } from "../sendFailure";
import {
  activeInputFor,
  attachmentsForSend,
  hasUploadInFlight,
  useWorkspaceStore,
} from "../store";
import type { ThreadAttachment } from "../types";

interface ComposerProps {
  threadId: string;
}

// The attach door: this file's own upload, or any upload the store is
// holding. Drag-and-drop has no disabled state, which is why `attach()`
// tests the same value rather than trusting the picker's.
function attachHeldWhile(busy: boolean, uploadInFlight: boolean) {
  return busy || uploadInFlight;
}

interface FailureNoticeProps {
  testId: string;
  dismissLabel: string;
  notice: { kind: string; message: string } | null;
  /** Names the row the refusal is about, for a per-attachment door. */
  subject?: string;
  onDismiss: () => void;
}

/** One named refusal, rendered until the operator dismisses it. Each door
 * gets its own instance so a delete refusal can never overwrite an upload
 * one, and the wire ``kind`` stays on screen as the state that was named. */
function FailureNotice({
  testId,
  dismissLabel,
  notice,
  subject,
  onDismiss,
}: FailureNoticeProps) {
  if (!notice) return null;
  return (
    <div
      role="alert"
      data-testid={testId}
      data-failure-kind={notice.kind}
      className="mb-2 flex items-start justify-between gap-3 border border-black bg-tool px-2 py-1.5 text-sm text-gray-800"
    >
      <span>{subject ? `${subject}：${notice.message}` : notice.message}</span>
      <button
        type="button"
        aria-label={dismissLabel}
        onClick={onDismiss}
        className="shrink-0 text-xs text-gray-500 hover:text-black"
      >
        ✕
      </button>
    </div>
  );
}

interface AttachmentChipProps {
  attachment: ThreadAttachment;
  door: AttachmentChipDoor;
}

/**
 * Everything one chip's controls need to reach the workspace: the thread the
 * row lives in, the store's single-attachment delete action, and where the
 * outcome lands. Named rather than inlined so the ``×``'s own wiring is one
 * unit the shipped control calls, and a refusal can never be re-labelled as
 * the upload door's.
 */
export interface AttachmentChipDoor {
  threadId: string;
  deleteAttachment: (threadId: string, attachmentId: string) => Promise<void>;
  /** The host confirmed the delete, so any earlier refusal on this door is
   * stale and stops being shown. */
  onDeleted: () => void;
  /** The host refused. The notice names the row it is about, because a
   * declined delete is a fact about THIS attachment. */
  onRefused: (notice: AttachFailureNotice & { filename: string }) => void;
  onRetry: (attachmentId: string) => void;
}

/**
 * The ``×`` on one chip: delete exactly that attachment and nothing else.
 * A refusal is reported, never swallowed — the row stays visible so a
 * cleanup that is still owed cannot read as a completed deletion.
 */
export async function deleteChipAttachment(
  door: AttachmentChipDoor,
  attachmentId: string,
  filename: string,
): Promise<void> {
  try {
    await door.deleteAttachment(door.threadId, attachmentId);
    door.onDeleted();
  } catch (error) {
    door.onRefused({ ...describeAttachFailure(error), filename });
  }
}

/** One thread attachment: its readiness label, the retry its failed state
 * offers, and the ``×`` that deletes just this attachment. Both controls
 * carry the attachment id so a click can only ever name its own row. */
function AttachmentChip({ attachment, door }: AttachmentChipProps) {
  const id = attachment.attachment_id;
  return (
    <li
      data-attachment-status={attachment.status}
      className="flex items-center gap-1 border border-black bg-tool px-2 py-0.5 text-xs"
    >
      <span className="font-mono">{attachment.filename}</span>
      <span data-testid={`attachment-status-${id}`} className="text-gray-600">
        {describeAttachmentStatus(attachment)}
      </span>
      {attachment.status === "failed" ? (
        <button
          type="button"
          data-testid={`attachment-retry-${id}`}
          aria-label={`重试解析附件 ${attachment.filename}`}
          onClick={() => door.onRetry(id)}
          className="border border-black bg-card px-1 text-gray-700 hover:bg-tool"
        >
          重试
        </button>
      ) : null}
      <button
        type="button"
        data-testid={`attachment-delete-${id}`}
        aria-label={`删除附件 ${attachment.filename}`}
        title="从本对话删除这个附件"
        onClick={() => void deleteChipAttachment(door, id, attachment.filename)}
        className="shrink-0 px-1 text-gray-600 hover:text-black"
      >
        ×
      </button>
    </li>
  );
}

/**
 * The thread's chip list: one row per stored attachment, each row's controls
 * naming its own identity. Rendered as a named unit so the row wiring has one
 * source the composer and the tests both read.
 */
export function AttachmentChipList({
  attachments,
  door,
}: {
  attachments: ThreadAttachment[];
  door: AttachmentChipDoor;
}) {
  if (attachments.length === 0) return null;
  return (
    <ul data-testid="thread-attachments" className="mb-2 flex flex-wrap gap-1">
      {attachments.map((a) => (
        <AttachmentChip key={a.attachment_id} attachment={a} door={door} />
      ))}
    </ul>
  );
}

/**
 * The delete door's own refusal slot: the named state the host answered and
 * the row it is about. It never shares a slot with an upload refusal, because
 * the two ask the operator for opposite next moves.
 */
export function AttachmentDeleteNotice({
  failure,
  onDismiss,
}: {
  failure: (AttachFailureNotice & { filename: string }) | null;
  onDismiss: () => void;
}) {
  return (
    <FailureNotice
      testId="attachment-delete-failure"
      dismissLabel="关闭附件删除提示"
      notice={failure}
      subject={failure?.filename}
      onDismiss={onDismiss}
    />
  );
}

/** The lock the composer states out loud. An upload is excluded: it already
 * has its own paragraph below, so one outcome is never narrated from two
 * places. */
function HeldLockLine({ decision }: { decision: ActiveInputDecision }) {
  if (!decision.held || decision.holdReason === "upload_in_flight") {
    return null;
  }
  return (
    <p data-testid="composer-held-copy" className="mb-2 text-xs text-gray-600">
      {decision.holdCopy}
    </p>
  );
}

/** The rows the send's reference rule left out, named so a withheld
 * attachment can never read as silently dropped. */
function WithheldAttachmentLine({
  attachments,
}: {
  attachments: ThreadAttachment[];
}) {
  if (attachments.length === 0) return null;
  return (
    <p data-testid="attachment-withheld" className="mb-2 text-xs text-gray-600">
      {`本轮不会引用：${attachments
        .map((a) => `${a.filename}（${describeAttachmentStatus(a)}）`)
        .join("、")}。就绪后它会自动随后续问题带上。`}
    </p>
  );
}

/** The thinking preference's switch and the line naming what switching it
 * off costs this run. */
function ThinkingSwitch({
  on,
  onToggle,
}: {
  on: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        onClick={onToggle}
        className="flex items-center gap-2 text-xs text-gray-700 select-none"
      >
        <span
          className={`relative inline-flex h-5 w-9 shrink-0 items-center border border-black p-0.5 ${
            on ? "bg-[#2563EB]/10" : "bg-card"
          }`}
        >
          <span
            className={`block h-3.5 w-3.5 border border-black transition-transform ${
              on ? "translate-x-4 bg-[#2563EB]" : "translate-x-0 bg-page"
            }`}
          />
        </span>
        Thinking（推理）
      </button>
      {!on ? <span className="text-xs text-gray-500">本轮将跳过推理轨迹</span> : null}
    </>
  );
}

/**
 * ADR 0047: the attach door's picker. This control is attach ONLY — corpus
 * ingest lives in the home corpus area and is never reachable from the
 * composer. ``held`` is the same predicate ``attach()`` tests, so an
 * enabled picker can never meet a bailed upload.
 */
function AttachFilePicker({
  busy,
  held,
  onPick,
}: {
  busy: boolean;
  held: boolean;
  onPick: (file: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <span className="flex items-center gap-2">
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) onPick(file);
        }}
      />
      <button
        type="button"
        data-testid="attach-file"
        disabled={held}
        title="上传附件（本对话内持续可引用，不入库）"
        onClick={() => inputRef.current?.click()}
        className="border border-black bg-card px-2 py-0.5 text-xs hover:bg-tool disabled:opacity-50"
      >
        {busy ? "上传中…" : "附加文件…"}
      </button>
      <span className="text-xs text-gray-400">
        本对话内持续可引用，不入库
      </span>
    </span>
  );
}

/** Stop ends the in-flight run on this thread. Shown only while something
 * is running: the host answers 204 on an idle thread, so a stale click is
 * harmless, but a permanently visible Stop reads as if a run were always
 * live. */
function StopRunButton({
  runVisible,
  stopping,
  onStop,
}: {
  runVisible: boolean;
  stopping: boolean;
  onStop: () => void;
}) {
  if (!runVisible) return null;
  return (
    <button
      type="button"
      data-testid="stop-run"
      disabled={stopping}
      onClick={onStop}
      className="border border-black bg-card px-4 py-1.5 text-sm font-medium hover:bg-black hover:text-white disabled:opacity-50"
    >
      {stopping ? "停止中…" : "停止"}
    </button>
  );
}

/** The box after a send resolves. Handing the question over is what
 * consumes it — ``submit`` empties the box at that moment — so this only
 * decides whether a send that was refused before it started puts the text
 * back. Anything the operator typed since then is theirs, never the
 * discarded question. */
export function draftAfterSend(
  current: string,
  submitted: string,
  reachedTransport: boolean,
): string {
  if (reachedTransport) return current;
  return current === "" ? submitted : current;
}

export default function Composer({ threadId }: ComposerProps) {
  const sendQuestion = useWorkspaceStore((s) => s.sendQuestion);
  const sending = useWorkspaceStore((s) => s.sendingQuestion);
  // A live run is REPORTED here and never a hold: compatible follow-up
  // input stays available while work is visibly active, and Runtime decides
  // what happens to it. ``busy`` therefore feeds only the button's line and
  // the Stop control — it cannot disable the box.
  const threadInFlight = useWorkspaceStore(
    (s) => s.activeThread?.id === threadId && s.activeThread.in_flight === true,
  );
  const resumingThis = useWorkspaceStore(
    (s) => s.resumingThreadId === threadId,
  );
  const busy = sending || threadInFlight || resumingThis;
  // Both halves of the send door — this control and the store's own guard —
  // read ONE decision over the store's state, so an enabled box can never
  // meet a POST the store swallows and a held box never fires a doomed one.
  // The upload gate is store state, not local picker state: it holds for a
  // file the question never mentions, and its scope is deliberately wider
  // than the host's because the composer itself is global while the host
  // refuses only the thread its own upload is landing in.
  const activeThread = useWorkspaceStore((s) => s.activeThread);
  const pendingUploads = useWorkspaceStore((s) => s.pendingUploads);
  const workspaceOperations = useWorkspaceStore((s) => s.workspaceOperations);
  const decision = activeInputFor(
    { activeThread, pendingUploads, workspaceOperations },
    threadId,
  );
  const uploadInFlight = hasUploadInFlight(pendingUploads);
  // Stop ends the in-flight run on this thread.
  const stopRun = useWorkspaceStore((s) => s.stopRun);
  const stopping = useWorkspaceStore((s) => s.stoppingThreadId === threadId);
  // T3: the operator's GLOBAL thinking preference. Defaults
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

  const { held } = decision;
  const sendLabel = sendControlLabel({
    held,
    holdReason: decision.holdReason,
    runInFlight: busy,
  });

  // ----- ADR 0086: the persistent attachment list -----
  // The thread's records live in the store (they outlive one send and
  // one mount); the store's readiness poll keeps the lifecycle chip
  // moving (uploaded → parsing → ready / failed, including the T2f
  // window where a run is suspended waiting on it) while this
  // composer is open.
  const attachFile = useWorkspaceStore((s) => s.attachFile);
  const loadAttachments = useWorkspaceStore((s) => s.loadAttachments);
  const retryAttachment = useWorkspaceStore((s) => s.retryAttachment);
  const deleteAttachment = useWorkspaceStore((s) => s.deleteAttachment);
  const attachments = useWorkspaceStore((s) => s.attachments);
  // Derived, never stored: the chips the reference rule leaves out are on
  // screen the same moment they stop being left out.
  const { withheld } = attachmentsForSend(attachments);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachFailure, setAttachFailure] =
    useState<AttachFailureNotice | null>(null);
  // Its own slot, not ``attachFailure``: an upload refusal and a delete
  // refusal ask for opposite next moves, so the two must never overwrite
  // each other on screen.
  const [deleteFailure, setDeleteFailure] =
    useState<(AttachFailureNotice & { filename: string }) | null>(null);
  const [dragOver, setDragOver] = useState(false);
  // One predicate for both doors, so the picker can never be enabled while
  // `attach()` would bail.
  const attachHeld = attachHeldWhile(attachBusy, uploadInFlight);

  useEffect(() => {
    void loadAttachments(threadId);
  }, [threadId, loadAttachments]);

  const attach = async (file: File) => {
    if (attachHeld) return;
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

  // The one door every chip's ``×`` calls: this thread, the store's
  // single-attachment delete, and the delete slot's own notice state.
  const chipDoor: AttachmentChipDoor = {
    threadId,
    deleteAttachment,
    onDeleted: () => setDeleteFailure(null),
    onRefused: setDeleteFailure,
    onRetry: (attachmentId) => void retry(attachmentId),
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
    const submitted = draft;
    const q = submitted.trim();
    if (!q) return; // Empty submit no-op.
    // Guarded here, not only by the disabled control: Enter on the
    // textarea reaches ``submit`` directly, and a send started while an
    // upload is still writing its bytes is refused by the store anyway.
    if (held) return;
    setFailure(null);
    // The store owns the one reference rule. Every durable record the
    // host accepts rides the send: READY ones are citable now, and an
    // uploaded/parsing one makes the grounded run suspend until
    // preparation completes (T2f) — so the question is never refused
    // on their account and nothing has to be resent. What stays out
    // (a failed record) is rendered below.
    const { ids } = attachmentsForSend(attachments);
    // Handing the question over empties the box here rather than on the
    // answer: a grounded run streams for a long time and the operator's
    // only copy of the question must not sit in the box the whole time.
    // A send the store refused before it started puts it back below.
    setDraft("");
    try {
      const sent = await sendQuestion(threadId, q, thinkingOn, ids);
      setDraft((current) => draftAfterSend(current, submitted, sent !== null));
    } catch (error) {
      // The textarea still holds the question unless the operator emptied
      // it while the request was in flight — put it back either way so the
      // retry is not a retype. An attachment refusal (parsing reference) is
      // ATTACHMENT copy, not send copy.
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
      <FailureNotice
        testId="attach-failure"
        dismissLabel="关闭附件提示"
        notice={attachFailure}
        onDismiss={() => setAttachFailure(null)}
      />
      <FailureNotice
        testId="send-failure"
        dismissLabel="关闭提示"
        notice={failure}
        onDismiss={() => setFailure(null)}
      />
      <HeldLockLine decision={decision} />
      <AttachmentDeleteNotice
        failure={deleteFailure}
        onDismiss={() => setDeleteFailure(null)}
      />
      <AttachmentChipList attachments={attachments} door={chipDoor} />
      <WithheldAttachmentLine attachments={withheld} />
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
          <ThinkingSwitch on={thinkingOn} onToggle={toggleThinking} />
          <AttachFilePicker
            busy={attachBusy}
            held={attachHeld}
            onPick={(file) => void attach(file)}
          />
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
        <StopRunButton
          runVisible={busy}
          stopping={stopping}
          onStop={() => void stopRun(threadId)}
        />
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
