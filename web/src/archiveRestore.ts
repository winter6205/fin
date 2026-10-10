// question-workspace W5 — restore archived work only after Runtime
// confirms (specs/question-workspace.md §"Active work, outcomes, and
// history" final paragraph; plans/question-workspace.md task 5; SC-6).
//
// This module exists because two facts must stay apart, and neither is
// derivable from component state:
//
//   * ARCHIVING is separate from completing or cancelling: an archived
//     task keeps the outcome it had when it was archived, which may
//     legitimately still be ``active``.
//   * A RESTORE is explicit and host-authoritative: "Until Runtime
//     confirms restoration, keep showing the existing current task." The
//     operator's click moves NOTHING by itself, and neither does the send
//     coming back accepted: confirmation is one specific fact — a fresh
//     projection that reports the clicked row's task as current and has
//     dropped that archive out of the list — and ``restoreDecision`` below
//     is the only thing here that turns it into ``restored``.
//
// Restore also never promotes history: the archived answer, run and
// evidence references stay historical evidence, not current support and
// not current authorization (contract rule 1 in
// specs/question-task-runtime-contract.md — a saved source choice is
// user intent, not current authorization).

import type { ArchivedTaskSummary, TaskOutcome } from "./types";

/** One archived task as a surface renders it.
 *
 * ``runId`` and ``evidenceRefs`` are HISTORICAL references: they are what
 * a restore must revalidate, never a current authorization or a current
 * support claim (specs/question-task-runtime-contract.md §Ownership). */
export interface ArchiveRow {
  archiveId: string;
  /** The task this archive holds. Adoption is proven against this id, so a
   * rendered row must be able to name it. */
  taskId: string;
  goal: string;
  taskVersion: number;
  archivedAtInputVersion: number;
  /** Historical run reference. NOT current authorization. */
  runId: string | null;
  /** Historical evidence references. NOT current support. */
  evidenceRefs: string[];
  /** The task's outcome AS OF when it was archived. An archived task may
   * legitimately carry "active" — archiving is not completion. */
  outcomeAtArchive: TaskOutcome | null;
}

function isBlank(value: string | null | undefined): boolean {
  return !value || value.trim().length === 0;
}

/** The archived task's own outcome as of archiving. An unrecognised or
 * absent token reads ``null`` = 未记录 rather than a fabricated outcome:
 * the surface never invents a task fact it was not given. */
function readArchivedOutcome(value: unknown): TaskOutcome | null {
  return value === "active" || value === "complete" || value === "cancelled"
    ? value
    : null;
}

/**
 * Map the host's archive list into renderable rows.
 *
 * The host owns the order: ``archived_task_summaries`` reverses the
 * stored list so it is already newest-first, and this surface must not
 * re-sort (a second ordering would be a second authority). A blank goal
 * renders as "" — the surface never synthesises a description the
 * operator did not write. ``undefined`` is an older host with no
 * projection at all: that reads as TASKLESS, not broken, so it yields [].
 */
export function archiveRows(
  archives: readonly ArchivedTaskSummary[] | undefined,
): ArchiveRow[] {
  return (archives ?? []).map((archive) => ({
    archiveId: archive.archive_id,
    taskId: archive.task_id,
    goal: isBlank(archive.goal) ? "" : archive.goal,
    taskVersion: archive.task_version,
    archivedAtInputVersion: archive.archived_at_input_version,
    runId: archive.run_id,
    evidenceRefs: archive.evidence_refs,
    outcomeAtArchive: readArchivedOutcome(archive.outcome),
  }));
}

/** The whole life of a restore, and the only authority that names it.
 * ``awaiting_confirmation`` (the request is recorded and may even be
 * durably accepted) and ``restored`` (Runtime proved it) are different
 * states by contract: no send result alone reaches ``restored``.
 *
 * Every state but ``idle`` and ``restored`` is a claim about an OPEN restore
 * request: a thread nobody restored reads ``idle`` even when its projection
 * cannot be read, because an unreadable projection says nothing about a
 * restore that was never asked for. */
export type RestoreState =
  | "idle"
  | "awaiting_confirmation"
  | "restored"
  | "refused"
  | "unresolved";

/**
 * The archive the operator explicitly clicked, remembered as it was
 * offered. ``taskId`` is the task id THAT row carried: adoption is proven
 * against it and never against whatever happens to be current now, because
 * the host removes the adopted row from the listing, so after a successful
 * restore no later read can restate the id mapping.
 *
 * The record also owns the host's answer about THAT restore. The per-thread
 * operation ledger holds at most one current operation, so a verdict read
 * back from the ledger would be erased by the next door the operator opens
 * (a clarification reply, a new question) — the answer to a restore belongs
 * to the restore.
 */
export interface RestoreRequestRef {
  archiveId: string;
  taskId: string;
  /** The listing the row was picked from: an id it does not offer was never
   * an offered action. */
  offered: readonly ArchivedTaskSummary[];
  /** Durable acceptance recorded by the host for this request. Acceptance
   * is an OWNED state, never adoption. */
  accepted: boolean;
  /** The request never reached Runtime. */
  failedToSend: boolean;
  /** The host's NAMED verdict on this restore, or ``null`` while it has
   * named none. Set once by the restore door and read by nothing else, so a
   * settled refusal keeps its own sentence however many later operations
   * the thread opens. */
  refusalKind: string | null;
}

/** The thread's task projection as the last read reported it. */
export interface RestoreProjectionRef {
  /** The CURRENT task id per the host. Reported even when the last re-read
   * failed, so the existing current task keeps rendering. */
  currentTaskId: string | null;
  /** The archives the host still offers. */
  archived: readonly ArchivedTaskSummary[];
  /** ``false`` when the re-read failed on the wire or the envelope was
   * refused: these are the last READABLE facts, so they keep the current
   * task visible but can confirm nothing. */
  fresh: boolean;
}

export interface RestoreDecisionInput {
  /** The thread these facts were read from. A verdict is only renderable
   * there: the request record belongs to one thread, so the surface asks
   * before it shows what that record says. */
  threadId: string;
  request: RestoreRequestRef | null;
  projection: RestoreProjectionRef | null;
}

export interface RestoreDecision {
  threadId: string;
  state: RestoreState;
  /** The archive still owed a confirmation. */
  pendingArchiveId: string | null;
  /** The archive adopted by the correlated fresh projection — the only way
   * anything here is ever called restored. */
  restoredArchiveId: string | null;
  currentTaskId: string | null;
  notice: string | null;
  awaitingConfirmation: boolean;
  /** Whether the host has answered this restore at all. ``true`` only in
   * the window between the click and the host's first word on it, which is
   * the ONLY window in which the surface may hold the listing still: a
   * settled verdict — refused, unheard, or accepted and still waiting to be
   * proven — must not park every other row forever. */
  requestUnsettled: boolean;
  projectionUnresolved: boolean;
  historyNotice: string | null;
}

// ----- refusal copy -----

/** The named ``TASK_FAILURE_KINDS`` a restore recheck can raise, each
 * with the one sentence it deserves. An operational failure is a SYSTEM
 * OUTCOME and is never turned into a clarification — no line here asks
 * the operator a question (specs/question-task-runtime-contract.md
 * §Named failures). An unmapped token still renders non-empty generic
 * copy, so a newer host can never produce a blank notice. */
const RESTORE_REFUSAL_COPY: Readonly<Record<string, string>> = {
  // The archive id is gone or was never this thread's.
  task_restore_unknown:
    "找不到这个存档，无法恢复；它可能已经不在本对话里。当前任务保持不变。",
  // The recheck did not pass, so the restore was not adopted.
  task_restore_not_adopted:
    "这次恢复没有通过复核，没有被采纳，当前任务保持不变。",
  // RestoreRecheck.source_authority_current failed: a saved source
  // choice is user intent, not live authorization.
  task_permission_denied:
    "当前权限不允许恢复这个存档：之前保存的来源选择不等于现在的授权。当前任务保持不变。",
  // RestoreRecheck.conditions_current failed (also the conservative
  // default when no recheck was recorded at all).
  task_applicability_unresolved:
    "这个存档当时的前提条件现在无法确认，暂不恢复。当前任务保持不变。",
  // RestoreRecheck.source_identity_version_current failed.
  task_capability_unsupported:
    "这个存档引用的来源在当前已不受支持，暂不恢复。当前任务保持不变。",
  // RestoreRecheck.evidence_applicable failed.
  task_insufficient_evidence:
    "这个存档当时的证据现在不再适用，暂不恢复。当前任务保持不变。",
  task_execution_limit:
    "已达到执行上限，这次恢复没有完成。当前任务保持不变。",
};

const RESTORE_REFUSAL_GENERIC_COPY =
  "这次恢复没有完成，当前任务保持不变。可以稍后重试。";

/** Chinese copy for a restore refusal, keyed by its named failure kind.
 * Never empty, and never phrased as a question. */
export function restoreRefusalCopy(kind: string): string {
  return RESTORE_REFUSAL_COPY[kind] ?? RESTORE_REFUSAL_GENERIC_COPY;
}

// ----- the other states' copy -----
//
// Every line below that is NOT an adoption says the current task did not
// move, and no line but ``RESTORE_ADOPTED_COPY`` may claim 已恢复: a
// recorded request, an unreadable re-read, or an unheard send is never
// presented as though Runtime had confirmed it.

/** Durable acceptance is its own visible state: the request is recorded
 * and Runtime is rechecking permission, versions, and evidence
 * applicability. */
export const RESTORE_AWAITING_COPY =
  "已记下这次恢复请求，Runtime 正在复核当前权限、版本与证据适用性；确认前当前任务保持不变。";

/** The projection could not be re-read (wire failure or refused envelope),
 * so nothing about this request can be confirmed either way. */
export const RESTORE_UNRESOLVED_COPY =
  "读不到最新的任务状态，这次恢复无法确认；当前任务保持不变。";

/** The send itself never reached Runtime, so no restore was attempted. */
export const RESTORE_UNHEARD_COPY =
  "这次恢复请求没有送达 Runtime，没有执行任何恢复；当前任务保持不变。";

/** Stated ONLY on the correlated fresh-projection proof. */
export const RESTORE_ADOPTED_COPY =
  "Runtime 已确认：这个存档已恢复为当前任务。";

// ----- history copy -----

/** Stated whenever archives exist: restored material stays historical.
 * The archived answer and its citations are evidence for the archive's
 * own time, not current support, and the run/evidence refs are not a
 * live grant — restore revalidates them through the host (contract
 * rule 1; specs/question-workspace.md SC-6). */
export const ARCHIVE_HISTORY_NOTICE =
  "存档中的回答与引用只是当时的历史证据；恢复不会把它们变成当前支持或当前授权。";

/** The only archives the surface may offer a restore action for: a
 * blank id cannot be honoured (the host would refuse it as
 * ``task_restore_unknown``), so no button is presented for it. */
export function restorableArchiveIds(
  archives: readonly ArchivedTaskSummary[] | undefined,
): string[] {
  return (archives ?? [])
    .map((archive) => archive.archive_id)
    .filter((id) => !isBlank(id));
}

/** Whether a restore may be SENT for ``archiveId`` against the listing
 * this thread actually offers. A send for an id the host never listed is
 * refused as ``task_restore_unknown`` server-side, so the surface refuses
 * it first and never shows an action it cannot see through. */
export function restoreIsListed(
  archives: readonly ArchivedTaskSummary[] | undefined,
  archiveId: string,
): boolean {
  return (
    !isBlank(archiveId) && restorableArchiveIds(archives).includes(archiveId)
  );
}

/** The refusal this surface states, or ``null`` when nothing refused. Every
 * source is a field of the restore's OWN request record: the thread's
 * current operation is never consulted, because a refusal read from there
 * would belong to whichever door opened last rather than to the restore the
 * operator actually asked for. */
function restoreRefusalNotice(request: RestoreRequestRef | null): string | null {
  if (request === null) return null;
  if (request.refusalKind !== null) {
    return restoreRefusalCopy(request.refusalKind);
  }
  if (!restoreIsListed(request.offered, request.archiveId)) {
    return restoreRefusalCopy("task_restore_unknown");
  }
  return request.failedToSend ? RESTORE_UNHEARD_COPY : null;
}

/** A projection the last read could not restate: an unreadable re-read, or
 * no read at all. Its ids stay visible as the LAST readable facts but
 * confirm nothing. Only ever asked about an OPEN request — a thread with
 * nothing to confirm has nothing to resolve, and a broken channel is stated
 * by the projection's own notice. */
function projectionIsUnreadable(projection: RestoreProjectionRef | null): boolean {
  return projection === null || !projection.fresh;
}

/** Runtime's own proof, read off a FRESH projection: the task it reports
 * as current IS the clicked row's task, and that archive is gone from the
 * archive list it was adopted out of. Acceptance, a 2xx, or the click
 * itself are not this proof. */
function adoptedArchiveId(
  request: RestoreRequestRef,
  projection: RestoreProjectionRef | null,
): string | null {
  if (projection === null || !projection.fresh) return null;
  const stillListed = projection.archived.some(
    (archive) => archive.archive_id === request.archiveId,
  );
  const reportsSameTask =
    projection.currentTaskId !== null &&
    projection.currentTaskId === request.taskId;
  return !stillListed && reportsSameTask ? request.archiveId : null;
}

function restoreStateOf(
  input: RestoreDecisionInput,
  refusalNotice: string | null,
): RestoreState {
  // First, and before anything about the projection: ``unresolved`` and
  // ``awaiting_confirmation`` are claims about a restore that was asked
  // for. A thread nobody restored has nothing to confirm, and its unreadable
  // projection is already stated by the projection-refusal notice.
  if (input.request === null) return "idle";
  if (refusalNotice !== null) return "refused";
  if (projectionIsUnreadable(input.projection)) return "unresolved";
  if (adoptedArchiveId(input.request, input.projection)) return "restored";
  return "awaiting_confirmation";
}

/** Whether the host has answered this restore yet. A named verdict, a
 * durable acceptance, or a send that never reached Runtime is an answer;
 * the refusal the surface states (its own listing check included) is one
 * too. Only the window with no answer at all holds the listing still. */
function restoreRequestIsUnsettled(
  request: RestoreRequestRef | null,
  refusalNotice: string | null,
): boolean {
  if (request === null || refusalNotice !== null) return false;
  return !request.accepted && !request.failedToSend && request.refusalKind === null;
}

function restoreNoticeFor(state: RestoreState, refusalNotice: string | null): string | null {
  switch (state) {
    case "refused":
      return refusalNotice;
    case "unresolved":
      return RESTORE_UNRESOLVED_COPY;
    case "restored":
      return RESTORE_ADOPTED_COPY;
    case "awaiting_confirmation":
      return RESTORE_AWAITING_COPY;
    default:
      return null;
  }
}

/** The archive the request named, snapshotted at click time. ``null`` when
 * nobody asked for a restore, so an idle thread can never carry an id that
 * would later be read as a pending or adopted archive. */
function requestedArchiveId(request: RestoreRequestRef | null): string | null {
  return request?.archiveId ?? null;
}

/** Whether any listing is on screen at all: the projection's own archives or
 * the snapshot the clicked row was picked from — after adoption the row is
 * gone from one but still restated by the other, and history stays history
 * either way. */
function hasArchives(
  projection: RestoreProjectionRef | null,
  request: RestoreRequestRef | null,
): boolean {
  return (
    (projection?.archived.length ?? 0) > 0 ||
    (request?.offered.length ?? 0) > 0
  );
}

/**
 * THE single authority for what the archive surface may claim about a
 * restore, for one thread, from one set of host facts.
 *
 * Every branch reports ``currentTaskId`` from the host's latest readable
 * projection, so the existing current task stays visible while a check is
 * owed, refused, or unresolved. ``restoredArchiveId`` is set by exactly one
 * input shape — the correlated fresh projection — which is why a durable
 * acceptance can never be mistaken for adoption, and why one archive's
 * proof cannot mark a second one restored. A refusal is the host's own word
 * about the request it answers, recorded on that request, so it outranks any
 * confirmation the projection still happens to show and outlives every later
 * operation on the thread.
 */
export function restoreDecision(input: RestoreDecisionInput): RestoreDecision {
  const refusalNotice = restoreRefusalNotice(input.request);
  const state = restoreStateOf(input, refusalNotice);
  const archiveId = requestedArchiveId(input.request);
  return {
    threadId: input.threadId,
    state,
    pendingArchiveId: state === "awaiting_confirmation" ? archiveId : null,
    restoredArchiveId: state === "restored" ? archiveId : null,
    currentTaskId: input.projection?.currentTaskId ?? null,
    notice: restoreNoticeFor(state, refusalNotice),
    awaitingConfirmation: state === "awaiting_confirmation",
    requestUnsettled: restoreRequestIsUnsettled(input.request, refusalNotice),
    projectionUnresolved: state === "unresolved",
    historyNotice: hasArchives(input.projection, input.request)
      ? ARCHIVE_HISTORY_NOTICE
      : null,
  };
}

/** Chinese label for an archived task's outcome. ``null`` renders as an
 * explicit 未记录 rather than an empty cell or a defaulted outcome. */
export function archiveOutcomeLabel(outcome: TaskOutcome | null): string {
  switch (outcome) {
    case "active":
      return "进行中";
    case "complete":
      return "已完成";
    case "cancelled":
      return "已取消";
    default:
      return "未记录";
  }
}
