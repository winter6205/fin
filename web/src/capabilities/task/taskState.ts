// The thread surface's task / turn / run-status / gap / source render
// decision, kept as pure functions.
//
// WHY a module and not inline JSX: the web suite runs in a node
// environment with no DOM and does not collect ``.tsx`` files, so every
// rule below — which turn result to show, what the run itself reported,
// whether a gap report exists, whether any source is currently available —
// could only be pinned by a test if it lives in pure code. Same split as
// ``threadViewState.ts`` / ``choiceWindow.ts``: plain data in, plain data
// out.
//
// The load-bearing distinction (spec question-workspace SC-1/SC-2 and
// contract question-task-runtime-contract §"The two rules every module
// inherits"): task outcome is not turn outcome, and a saved source choice
// is user intent, not current authorization. Every function here reads
// the authoritative envelope (``task_runtime``) as the source of truth
// and never derives one from the other.

import type {
  PendingQuestionView,
  RunStatus,
  TaskOutcome,
  TaskRuntimeView,
  Turn,
} from "../../types";

// ----- User-facing copy (hardcoded Chinese; the repo has no i18n) -----

/** Task outcome, from Runtime only (spec §"Active work, outcomes, and
 * history": show active / complete / cancelled only when Runtime
 * reports them). */
export const TASK_OUTCOME_LABEL: Record<TaskOutcome, string> = {
  active: "进行中",
  complete: "已完成",
  cancelled: "已取消",
};

/** The turn-level result, deliberately labelled apart from the task
 * outcome: a run ending is a turn fact, not a task fact. Tone follows
 * the existing ThreadPage status line (完成 / 未完成). */
export const TURN_RESULT_LABEL: Record<TurnResultKind, string> = {
  none: "尚未开始",
  running: "运行中",
  answer: "已回答",
  partial: "部分回答",
  clarification: "待澄清",
  failure: "未完成",
  cancelled: "已取消",
};

// ----- Run status -----

/** The run's own status as the wire reports it. Kept apart from both the
 * task outcome and the turn result: ``done`` here means "this run
 * finished", never "this task is complete" — the wording below is chosen
 * so the row cannot be misread as an outcome (spec §"Active work,
 * outcomes, and history"). */
export type RunStatusKind = RunStatus | "none" | "running" | "stopped";

export const RUN_STATUS_LABEL: Record<RunStatusKind, string> = {
  none: "尚无运行",
  running: "运行中",
  done: "运行已结束",
  refuse: "运行未完成",
  step_cap: "运行达到步数上限",
  stopped: "运行已停止",
};

// ----- The turn's own status -----

/** The turn status wording, the SINGLE authority for it. The thread's
 * status line and the developer trace fallback both read this: two copies
 * of "what does this turn's status say" is how one surface starts leaking
 * the raw ``status_reason`` token while the other does not. */
const TURN_STATUS_LABEL: Record<Turn["status"], string> = {
  done: "完成",
  refuse: "未完成",
  step_cap: "步数上限（step_cap）",
};

/** The turn's status label; a turn still in flight says so instead of
 * reporting the placeholder ``done`` the wire carries while it streams. */
export function turnStatusLabel(turn: Pick<Turn, "status" | "in_flight">): string {
  if (turn.in_flight === true) return "运行中";
  return TURN_STATUS_LABEL[turn.status];
}

// ----- Submission notice -----

/** The outcome of the operator's own submission, as its owner reports it.
 * A presentation-only fact: it says what happened to what was sent and
 * claims nothing about the task. */
export type SubmissionState = "pending" | "accepted" | "rejected" | "failed";

export interface SubmissionNotice {
  state: SubmissionState;
  /** The owner's own wording, shown beside the state label; empty renders
   * the label alone rather than a blank row. */
  text: string;
}

export const SUBMISSION_STATE_LABEL: Record<SubmissionState, string> = {
  pending: "待受理",
  accepted: "已受理",
  rejected: "已拒绝",
  failed: "发送失败",
};

// ----- Turn result -----

export type TurnResultKind =
  | "none"
  | "running"
  | "answer"
  | "partial"
  | "clarification"
  | "failure"
  | "cancelled";

// Why a turn is "cancelled": ``RunRecord.status`` is a closed enum
// (done / refuse / step_cap) with no stop value, so the host lands an
// operator stop on ``refuse`` with ``status_reason="stopped"`` as the
// discriminator (workspace_http.STOPPED_STATUS_REASON). The token is
// matched rather than compared so a future synonym still reads as an
// operator action instead of a failure.
//
// ONLY ``status_reason`` is read. ``status_classified`` is the stream-error
// taxonomy and cannot itself carry a stop, so scanning it would let a
// transport error that happens to spell a stop synonym read as a deliberate
// operator action — a fact run control never reported.
const STOP_REASON_TOKENS = ["stopped", "cancel"] as const;

function turnReportsStop(turn: Turn): boolean {
  const reason = (turn.status_reason ?? "").toLowerCase();
  return STOP_REASON_TOKENS.some((token) => reason.includes(token));
}

/**
 * The turn-level result to show for a thread.
 *
 * TURN outcome is never TASK outcome (contract §"The two rules"): a run
 * ending with an answer while the task stays ``active`` is the normal
 * case, so this function returns a value from the turn vocabulary only —
 * it can never say the task is complete, and callers must not read it
 * that way.
 */
export function turnResult(input: {
  turns: readonly Turn[];
  inFlight: boolean;
  /** The task envelope's own pending clarification, when one is pending. */
  pendingQuestion: PendingQuestionView | null;
}): TurnResultKind {
  // A pending clarification outranks everything: the thread is waiting
  // on the operator, not working, so it is not "running" even while a
  // stream is open, and it overrides a finished earlier turn.
  if (input.pendingQuestion !== null) return "clarification";
  // Work is active. This is a TURN result and must never imply the task
  // is done — see the module header.
  if (input.inFlight) return "running";

  const last = input.turns[input.turns.length - 1];
  if (last === undefined) return "none";

  // The run's answer prose. ``assistant_message`` is deliberately NOT
  // used here: by contract it is the ordinary-person COPY carried on
  // refuse / step_cap and is "" on done (see types.ts Turn), so keying
  // the answer branch on it would make a real completed answer
  // unreachable and would read a refuse-with-copy as a success.
  if (
    last.status === "done" &&
    last.semantic_outcome === "insufficient_evidence"
  ) {
    return "partial";
  }
  if (last.status === "done" && last.answer.length > 0) return "answer";

  // Read a stop before the failure branch: a stop lands on a refuse row,
  // which failure would otherwise claim.
  if (turnReportsStop(last)) return "cancelled";

  if (last.status === "step_cap") return "partial";

  // No answer prose on a terminal row: the run ended without giving the
  // operator an answer (the contract's done / refuse + status_classified
  // case). A done row with no answer and no stream error lands here too
  // rather than on an ambiguous "none", which means "no turns".
  return "failure";
}

/**
 * The run's own status, as a fact SEPARATE from the turn result.
 *
 * Both rows read the same latest turn and must not be collapsed into one:
 * ``turnResult`` classifies what the operator got (answer / partial /
 * clarification / failure / cancelled) while this says what the run
 * reported (open, ended, refused, capped, stopped). A stop is read here
 * from the same run-control field ``turnResult`` uses, so the two rows
 * can never disagree about whether an operator stopped the run.
 */
export function runStatus(input: {
  turns: readonly Turn[];
  inFlight: boolean;
}): RunStatusKind {
  if (input.inFlight) return "running";
  const last = input.turns[input.turns.length - 1];
  if (last === undefined) return "none";
  // Before the wire status: a stop lands on a ``refuse`` row, and without
  // this order an operator's deliberate stop would read as a refusal.
  if (turnReportsStop(last)) return "stopped";
  return last.status;
}

// ----- Material gaps -----

/** Whether Runtime reported material gaps, kept distinct from whether
 * there ARE gaps: an absent report is unknown, never "no gaps". */
export type MaterialGaps =
  | { reported: true; gaps: string[] }
  | { reported: false; gaps: [] };

/**
 * The material unresolved gaps Runtime reported for the current task.
 *
 * The frozen ``TaskRuntimeView`` carries NO gaps field: Runtime does not
 * persist ``GroundingAssessment.material_gaps`` on the wire, and the
 * Grounded Answer peer has not landed. So an absent envelope and a
 * present envelope without a gap report BOTH read as not-reported —
 * never as "no gaps". That is the whole point of the return shape: the
 * surface can say "Runtime 尚未报告" instead of implying the task has no
 * unresolved gaps. Contract §Ownership: "Silence from a peer module is
 * never a licence to complete or adopt anything." An older host that
 * projects no ``task_runtime`` at all reads the same way.
 */
export function materialGaps(view: TaskRuntimeView | undefined): MaterialGaps {
  // No field to read today; the parameter marks the seam the report will
  // arrive on, so a caller and its test already stand in the right shape.
  void view;
  return { reported: false, gaps: [] };
}

// ----- Current source scope -----

/** Why no source evaluation could be shown. ``evaluation_unavailable``:
 * the user has a saved restriction but the host projected no fresh
 * evaluation of it. ``not_evaluated``: there is nothing to evaluate —
 * no saved restriction and no host evaluation, so the surface simply has
 * no current authority to show (kept distinct so a reader is not told a
 * failed evaluation occurred when none was attempted). */
export type SourceEvaluationReason = "evaluation_unavailable" | "not_evaluated";

export type SourceEvaluation =
  | { available: false; reason: SourceEvaluationReason }
  | {
      available: true;
      /** The user's saved restriction, surfaced as INTENT only. It is
       * never merged into ``effectiveSourceIds`` (contract rule 1). */
      restrictionIntent: string;
      effectiveSourceIds: string[];
      capabilities: string[];
      /** Intent ids naming sources the fresh evaluation did not grant —
       * reported so the surface can say "not currently authorized". */
      staleIntentIds: string[];
    };

// The saved restriction is an opaque host string naming one or more
// sources. Split on commas / whitespace into ids, de-duplicated, order
// preserved. This is a presentation-side reading of a string the host
// owns; it grants nothing.
function parseRestrictionIds(restriction: string | null | undefined): string[] {
  if (typeof restriction !== "string") return [];
  const ids: string[] = [];
  for (const raw of restriction.split(/[,\s]+/)) {
    const id = raw.trim();
    if (id.length > 0 && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/**
 * The current source restriction and the host's freshly evaluated
 * effective scope, kept apart (spec SC-2).
 *
 * A saved restriction ALONE is user intent and can never make the
 * evaluation ``available`` nor appear as a grant. No evaluation means no
 * source is offered as currently available — SC-2's required behaviour
 * and the CURRENT real state, since Source Scope exposes no HTTP
 * projection yet. An empty capability list on a present evaluation is
 * still ``available: true``: an honest "evaluated, nothing is feasible
 * right now" is not the same as "unavailable".
 */
export function sourceEvaluation(input: {
  /** The user's SAVED restriction, if the host projects one. */
  savedRestriction: string | null | undefined;
  /** The host's FRESH evaluation, if it projects one. */
  effectiveScope: {
    sourceIds: readonly string[];
    capabilities: readonly string[];
  } | null | undefined;
}): SourceEvaluation {
  const scope = input.effectiveScope ?? null;
  const intentIds = parseRestrictionIds(input.savedRestriction);

  if (scope === null) {
    // Offer no source as available. With a saved restriction the honest
    // reason is that its evaluation is missing; with nothing saved there
    // is simply nothing evaluated.
    return {
      available: false,
      reason:
        intentIds.length > 0 ? "evaluation_unavailable" : "not_evaluated",
    };
  }

  const authorized = new Set(scope.sourceIds);
  return {
    available: true,
    restrictionIntent: (input.savedRestriction ?? "").trim(),
    effectiveSourceIds: [...scope.sourceIds],
    capabilities: [...scope.capabilities],
    // Intent ids outside the fresh grant are stale intent, NOT authority:
    // they are never added to ``effectiveSourceIds``.
    staleIntentIds: intentIds.filter((id) => !authorized.has(id)),
  };
}

// ----- The composed task state -----

export interface TaskStateView {
  /** True only when the envelope carries a current task. */
  hasTask: boolean;
  /** "Only when Runtime reports them"; "" on a taskless thread. Never
   * synthesized from a turn's question text. */
  goal: string;
  expectedResult: string;
  /** From the envelope alone; null when taskless. */
  taskOutcome: TaskOutcome | null;
  taskOutcomeReason: string;
  turnResult: TurnResultKind;
  /** Its own fact: what the run reported, never a task outcome. */
  runStatus: RunStatusKind;
  /** True when the authoritative projection could not be read, so an
   * absent task is UNKNOWN rather than "no task yet". */
  projectionUnreadable: boolean;
  gaps: MaterialGaps;
  sources: SourceEvaluation;
  /** The submission's own outcome, when its owner has one to show. */
  submission?: SubmissionNotice | null;
  /** A taskless thread with nothing pending renders no task panel. */
  showTaskPanel: boolean;
}

/**
 * What the thread surface renders for the current task, its sources, and
 * its answer state.
 *
 * STRUCTURAL GUARD (spec SC-1): ``taskOutcome`` is read ONLY from the
 * authoritative envelope. It is never derived from ``turnResult``, a
 * citation, or a run ending — "Citations and run completion never cause
 * the workspace to infer task completion". There is intentionally no
 * ``inferTaskComplete``-style helper here and none may be added: wanting
 * "the task is done because the run ended" is exactly the inference the
 * spec forbids.
 */
/** The task-identity slice of the view: presence + the envelope's own
 * fields verbatim. Null when taskless — the outcome field is scoped to
 * the current task; a thread with none has no outcome to show. */
function taskIdentity(
  runtime: TaskRuntimeView | undefined,
): Pick<TaskStateView, "hasTask" | "goal" | "expectedResult" | "taskOutcomeReason"> {
  const currentTask = runtime?.current_task ?? null;
  return {
    hasTask: currentTask !== null,
    goal: currentTask?.goal ?? "",
    expectedResult: currentTask?.expected_result ?? "",
    taskOutcomeReason: currentTask?.outcome_reason ?? "",
  };
}

/** The task outcome, read ONLY from the authoritative envelope (SC-1
 * structural guard: never derived from ``turnResult``, a citation, or a
 * run ending). Fall back to the task record's own outcome only when the
 * envelope level omits one — the contract carries both and they agree by
 * construction. */
function outcomeFor(
  runtime: TaskRuntimeView | undefined,
): TaskOutcome | null {
  const currentTask = runtime?.current_task ?? null;
  if (currentTask === null) return null;
  return runtime?.task_outcome ?? currentTask.outcome;
}

/** Panel visibility: a task, a pending clarification, a reported gap, an
 * unreadable projection, or a submission notice each opens it; a bare
 * taskless thread renders none (an empty panel would imply a task exists
 * and is simply unrecorded). */
function showTaskPanelFor(input: {
  hasTask: boolean;
  pendingQuestion: PendingQuestionView | null;
  gaps: MaterialGaps;
  projectionUnreadable: boolean;
  submission: SubmissionNotice | null;
}): boolean {
  return (
    input.hasTask ||
    input.pendingQuestion !== null ||
    input.gaps.reported ||
    // An unreadable projection is a fact the operator has to be told
    // directly; a closed panel would leave silence reading as "no task".
    input.projectionUnreadable ||
    // And so is the outcome of something the operator just sent. It opens
    // the notice row only — it is not evidence that a task exists.
    input.submission !== null
  );
}

export function taskState(input: {
  taskRuntime: TaskRuntimeView | undefined;
  turns: readonly Turn[];
  inFlight: boolean;
  savedRestriction?: string | null;
  effectiveScope?: {
    sourceIds: readonly string[];
    capabilities: readonly string[];
  } | null;
  /** True when the caller's projection could not be read (a malformed or
   * refused ``current_task`). Optional so existing callers are unchanged. */
  projectionUnreadable?: boolean;
  submission?: SubmissionNotice | null;
}): TaskStateView {
  const runtime = input.taskRuntime;
  const identity = taskIdentity(runtime);
  const pendingQuestion = runtime?.pending_question ?? null;
  const gaps = materialGaps(runtime);
  const projectionUnreadable = input.projectionUnreadable === true;
  const submission = input.submission ?? null;

  return {
    ...identity,
    // See outcomeFor — envelope-authoritative, never inferred.
    taskOutcome: outcomeFor(runtime),
    turnResult: turnResult({
      turns: input.turns,
      inFlight: input.inFlight,
      pendingQuestion,
    }),
    runStatus: runStatus({ turns: input.turns, inFlight: input.inFlight }),
    projectionUnreadable,
    gaps,
    sources: sourceEvaluation({
      savedRestriction: input.savedRestriction ?? null,
      effectiveScope: input.effectiveScope ?? null,
    }),
    submission,
    showTaskPanel: showTaskPanelFor({
      hasTask: identity.hasTask,
      pendingQuestion,
      gaps,
      projectionUnreadable,
      submission,
    }),
  };
}
