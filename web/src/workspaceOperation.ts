// Identity and association for the research-thread workspace's user-intent
// submissions (specs/question-workspace.md §"Input ordering and module
// seams": the seam is "an authoritative current projection for display and
// a user-intent submission").
//
// WHAT THIS MODULE DECIDES, AND ONLY THAT: which operation a result belongs
// to. The task facts themselves stay Runtime-owned and arrive through the
// projection decoded by taskRuntime.ts; nothing here reads a projection
// field, derives a task state, or proves adoption.
//
// WHY A BROWSER-MINTED ATTEMPT AT ALL: the wire carries no operation id,
// request id, revision, or sequence, so a late result could otherwise only
// be matched by thread — and one thread can hold several in-flight requests
// (a repeated send, a send followed by a restore). The attempt counter names
// the operator's LATEST intent on that thread and is therefore correlation
// ONLY: it is never sent to the host and never used to decide a task fact,
// because a browser counter proves nothing about what Runtime did.
//
// WHY EVERY ENTRY VALIDATES RATHER THAN TRUSTING THE CALLER: the values come
// from store actions and host payloads that are not statically narrowed at
// this seam. An unusable input opens nothing and changes nothing — it is
// never defaulted to a plausible thread, door, or verdict.

/** The three submission doors, matching the projection's own input
 * vocabulary (``AcceptedInputRow.kind``). Kept as one class union so a
 * refusal's copy stays bound to the producer that recorded it. */
export type WorkspaceOperationClass = "message" | "reply" | "restore";

/** ``pending`` is the opening state; the three verdicts are the only states
 * a result may bring. */
export type WorkspaceOperationState =
  | "pending"
  | "accepted"
  | "rejected"
  | "failed";

export type WorkspaceOperationVerdict = "accepted" | "rejected" | "failed";

/** One operation exactly as the workspace displays it.
 *
 * ``threadId`` and ``attempt`` are the association; ``questionId`` /
 * ``archiveId`` / ``taskId`` are the identity the operator acted on,
 * captured at request time and carried for display so a notice can name
 * what it refers to. */
export interface WorkspaceOperation {
  threadId: string;
  /** Which producer opened it: the composer, a clarification card, or an
   * archive row. */
  door: WorkspaceOperationClass;
  /** Which accepted-input class was submitted. Equal to ``door`` for the
   * three shipped doors and kept separate because the projection names the
   * class while the surface names the door. */
  kind: WorkspaceOperationClass;
  /** Monotonic per-thread display-only correlation key. Never sent to the
   * host, never a task fact. */
  attempt: number;
  questionId: string | null;
  archiveId: string | null;
  taskId: string | null;
  state: WorkspaceOperationState;
  reason: string | null;
}

/** Per-thread operation state.
 *
 * ``attempts`` is the minted counter and only ever moves forward, so a late
 * result can never match the operation that replaced it. ``operations``
 * holds at most the CURRENT operation per thread; a thread with no entry has
 * nothing outstanding, which is a real state and not an error. */
export interface WorkspaceOperationLedger {
  attempts: Record<string, number | undefined>;
  operations: Record<string, WorkspaceOperation | undefined>;
}

export const EMPTY_WORKSPACE_OPERATIONS: WorkspaceOperationLedger = {
  attempts: {},
  operations: {},
};

/** A request as the caller describes it. ``door`` and ``kind`` are ``string``
 * on purpose so this seam validates them instead of assuming the caller
 * narrowed them. */
export interface OpenWorkspaceOperationInput {
  threadId: string;
  door: string;
  kind: string;
  questionId?: string | null;
  archiveId?: string | null;
  taskId?: string | null;
}

/** The host's answer to one opened attempt. */
export interface WorkspaceOperationResult {
  threadId: string;
  /** The stamp read from the operation this request was opened for. */
  attempt: number;
  /** ``string`` rather than the verdict union: an unrecognised verdict reads
   * as no verdict at all rather than as a guess. */
  state: string;
  reason?: string | null;
  questionId?: string | null;
  archiveId?: string | null;
  taskId?: string | null;
}

export interface OpenedWorkspaceOperation {
  ledger: WorkspaceOperationLedger;
  /** ``null`` when the input was unusable, so the caller can skip the write
   * rather than paint an operation with no thread. */
  operation: WorkspaceOperation | null;
}

const OPERATION_CLASSES: readonly string[] = ["message", "reply", "restore"];
const VERDICTS: readonly string[] = ["accepted", "rejected", "failed"];

/** A blank or whitespace-only id is the absent thing in disguise: storing it
 * would let two empty ids match each other. */
function hasContent(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isKnownClass(value: string): value is WorkspaceOperationClass {
  return OPERATION_CLASSES.includes(value);
}

function isKnownVerdict(value: string): value is WorkspaceOperationVerdict {
  return VERDICTS.includes(value);
}

function readAssociation(value: string | null | undefined): string | null {
  return hasContent(value) ? value : null;
}

/** Attempts are minted from 1, so ``0`` / a non-integer / a negative value
 * names no attempt the workspace ever opened. */
function isMintedAttempt(value: number): boolean {
  return Number.isInteger(value) && value > 0;
}

/** ``rejected`` and ``failed`` are the refusal class: both say the operator's
 * intent did not take effect, and neither is a confirmation waiting to be
 * overwritten. */
function isRefusal(state: WorkspaceOperationState): boolean {
  return state === "rejected" || state === "failed";
}

/** Open one attempt for a thread and door.
 *
 * The attempt is minted here, never supplied by the caller, so a caller
 * cannot reuse a stale number. Returns the SAME ledger object when the input
 * names no thread or no known class. */
export function beginWorkspaceOperation(
  ledger: WorkspaceOperationLedger,
  input: OpenWorkspaceOperationInput,
): OpenedWorkspaceOperation {
  if (!hasContent(input.threadId)) return { ledger, operation: null };
  if (!isKnownClass(input.door) || !isKnownClass(input.kind)) {
    return { ledger, operation: null };
  }
  const threadId = input.threadId;
  const attempt = (ledger.attempts[threadId] ?? 0) + 1;
  const operation: WorkspaceOperation = {
    threadId,
    door: input.door,
    kind: input.kind,
    attempt,
    questionId: readAssociation(input.questionId),
    archiveId: readAssociation(input.archiveId),
    taskId: readAssociation(input.taskId),
    state: "pending",
    reason: null,
  };
  return {
    ledger: {
      attempts: { ...ledger.attempts, [threadId]: attempt },
      operations: { ...ledger.operations, [threadId]: operation },
    },
    operation,
  };
}

/** Apply one host verdict to the operation that opened it.
 *
 * The record changes ONLY when the result names this thread's current
 * attempt; every other case returns the given ledger unchanged, so a late
 * answer for a switched-away thread or a superseded attempt cannot repaint
 * the operation the operator is now looking at.
 *
 * Precedence inside one attempt: a recorded refusal is final, so an accepted
 * verdict arriving after it cannot turn a refusal into a confirmation; an
 * accepted verdict is still open when the refusal arrives, so a refusal
 * arriving after it wins. Either way the refusal for the current attempt
 * outranks a confirmation.
 *
 * The request-time association is filled once and never rewritten: a reply
 * keeps the question it was sent for even if the answer names another one. */
export function applyWorkspaceOperationResult(
  ledger: WorkspaceOperationLedger,
  result: WorkspaceOperationResult,
): WorkspaceOperationLedger {
  if (!hasContent(result.threadId)) return ledger;
  if (!isMintedAttempt(result.attempt)) return ledger;
  if (!isKnownVerdict(result.state)) return ledger;

  const current = ledger.operations[result.threadId];
  if (current === undefined) return ledger;
  if (current.attempt !== result.attempt) return ledger;
  if (isRefusal(current.state)) return ledger;

  const operation: WorkspaceOperation = {
    ...current,
    state: result.state,
    reason: readAssociation(result.reason),
    questionId: current.questionId ?? readAssociation(result.questionId),
    archiveId: current.archiveId ?? readAssociation(result.archiveId),
    taskId: current.taskId ?? readAssociation(result.taskId),
  };
  return {
    ...ledger,
    operations: { ...ledger.operations, [current.threadId]: operation },
  };
}

/** The operation currently open for one thread, or ``null`` when that thread
 * has nothing outstanding. A blank thread id reads as nothing outstanding. */
export function currentWorkspaceOperation(
  ledger: WorkspaceOperationLedger,
  threadId: string,
): WorkspaceOperation | null {
  if (!hasContent(threadId)) return null;
  return ledger.operations[threadId] ?? null;
}

/** Whether an operation may be rendered for the thread the operator is on.
 *
 * This is the guard a single shared slot cannot provide on its own: a record
 * carries the thread it belongs to, so a surface asks before it renders and
 * a result for thread A never appears as a notice on thread B. */
export function isWorkspaceOperationDisplayable(
  operation: WorkspaceOperation | null | undefined,
  activeThreadId: string,
): operation is WorkspaceOperation {
  if (operation === null || operation === undefined) return false;
  if (!hasContent(activeThreadId)) return false;
  return operation.threadId === activeThreadId;
}
