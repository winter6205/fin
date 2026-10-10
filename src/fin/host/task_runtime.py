"""Question and task runtime — the host's task/input decision boundary (T1–T6).

CONTEXT **host agent**: fin as the LangGraph runtime that routes tracks
and calls grounded-track tools behind the **retrieve port**. This module
is pure decision logic inside that boundary: it holds no store, takes no
lock, calls no model, and performs no I/O. The LangGraph ``AgentState``
plus its configured checkpointer remain the single task authority
(specs/question-task-runtime.md decision 1) — this module produces the
value that authority stores, and reads the stored value back.

Every transition takes the committed :class:`TaskRuntimeState` and
returns a NEW one. Nothing mutates in place, so a caller that fails to
persist a result still holds the last consistent state — which is what
makes a failed checkpoint application leave the previous task state
authoritative.

Every failure raised here is a named token from one closed set
(:data:`TaskRuntimeState`'s module
:data:`~fin.host.task_runtime_types.TASK_FAILURE_KINDS`) so input,
permission, dependency, capability, evidence, applicability, intent,
execution-limit, checkpoint, and history failures stay distinguishable —
and so an operational failure can never be answered with a clarification
(specs/question-task-runtime.md, the superseding note over
``soft-track-judge-route``).

Shape SSOT is ``fin.host.task_runtime``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from fin.host.task_runtime_types import (
    ARCHIVE_LIMIT,
    CHANGE_NECESSARY_CONDITIONS,
    DISPOSITION_AWAIT_REPLY,
    DISPOSITION_CANCEL,
    DISPOSITION_CONTINUE,
    DISPOSITION_NONE,
    DISPOSITION_RESTORE,
    DISPOSITION_START,
    DISPOSITION_SWITCH,
    INPUT_HISTORY_LIMIT,
    INPUT_KINDS,
    INPUT_REPLY,
    INPUT_RESTORE,
    TASK_ACTIVE,
    TASK_CANCELLED,
    TASK_COMPLETE,
    TASK_ALREADY_RECONCILED,
    TASK_APPLICABILITY_UNRESOLVED,
    TASK_DEPENDENCY_FAILURE,
    TASK_FAILURE_KINDS,
    TASK_INPUT_INVALID,
    TASK_INPUT_UNRECONCILED,
    TASK_INTENT_MISSING,
    TASK_REPLY_EXPIRED,
    TASK_REPLY_UNKNOWN,
    TASK_RESTORE_NOT_ADOPTED,
    TASK_RESTORE_UNKNOWN,
    AcceptedInput,
    ArchivedTask,
    CancellationRecord,
    GroundingAssessment,
    HostGates,
    PendingQuestion,
    Reconciliation,
    ReconciliationProposal,
    RestoreRecheck,
    TaskRuntimeError,
    TaskRuntimeState,
    TaskState,
    default_task_id,
)

__all__ = [
    "TASK_FAILURE_KINDS",
    "TaskRuntimeError",
    "accept_input",
    "apply_assessment",
    "archived_task_summaries",
    "bounded_input_history",
    "current_task_summary",
    "decide",
    "decide_cancellation",
    "is_reconciled",
    "pending_question_payload",
    "reconcile_marker",
    "record_cancellation",
]


# ----- transitions -----


def accept_input(
    state: TaskRuntimeState,
    *,
    kind: str,
    payload: str | None,
    provenance: str,
    question_id: str | None = None,
    choice_id: str | None = None,
    archive_id: str | None = None,
    run_id: str | None = None,
) -> tuple[TaskRuntimeState, AcceptedInput]:
    """Durably accept ONE input and advance input history exactly once.

    This is the only place ``input_version`` moves. It performs no
    semantic work and commits no task change, so the original payload is
    already recoverable before anything depends on it.

    Empty and whitespace-only payloads are refused: an accepted input has
    to carry what the user actually submitted, and dropping the message
    here would advance history with no recoverable original.
    """
    if kind not in INPUT_KINDS:
        raise TaskRuntimeError(
            TASK_INPUT_INVALID,
            f"unknown input kind: {kind}",
            input_kind=kind,
        )
    if not provenance.strip():
        raise TaskRuntimeError(
            TASK_INPUT_INVALID, "an accepted input must name its provenance"
        )
    if payload is not None and not payload.strip():
        raise TaskRuntimeError(
            TASK_INPUT_INVALID, "an accepted input must carry its original payload"
        )
    accepted = AcceptedInput(
        input_version=state.input_version + 1,
        kind=kind,
        payload=payload,
        provenance=provenance,
        question_id=question_id,
        archive_id=archive_id,
        choice_id=choice_id,
        run_id=run_id,
    )
    return (
        replace(
            state,
            input_history=(*state.input_history, accepted)[-INPUT_HISTORY_LIMIT:],
            input_version=accepted.input_version,
        ),
        accepted,
    )


def decide(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
    *,
    task_id_factory: Callable[[AcceptedInput], str] | None = None,
) -> TaskRuntimeState:
    """Validate one proposal against the latest state and commit the whole
    transition — task change, archive, pending question, execution
    disposition, and reconciliation marker — together.

    Returns a NEW state; nothing is mutated in place, so a caller that
    fails to persist the result still holds the last consistent state.
    Raises :class:`TaskRuntimeError` with one of
    :data:`TASK_FAILURE_KINDS` rather than returning a partial or a
    silently-ignored decision.
    """
    _guard_reconcilable(state, accepted, proposal)
    if proposal.kind == INPUT_REPLY:
        return _decide_reply(state, accepted, proposal)
    if proposal.kind == INPUT_RESTORE:
        return _decide_restore(state, accepted, proposal)
    if proposal.taskless:
        return _decide_taskless(state, accepted, proposal)
    return _decide_message(state, accepted, proposal, task_id_factory)


def _guard_reconcilable(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
) -> None:
    """Refuse anything that would reconcile an input twice or out of
    order — the one-reconciliation-per-accepted-input rule."""
    if accepted.input_version > state.input_version:
        raise TaskRuntimeError(
            TASK_INPUT_UNRECONCILED,
            "the accepted input is newer than the recorded input history",
            input_version=accepted.input_version,
            recorded_input_version=state.input_version,
        )
    marker = state.reconciliation
    if marker is not None and marker.reconciled_input_version >= accepted.input_version:
        raise TaskRuntimeError(
            TASK_ALREADY_RECONCILED,
            "this input has already been reconciled",
            input_version=accepted.input_version,
            reconciled_input_version=marker.reconciled_input_version,
        )
    if accepted.kind != proposal.kind:
        raise TaskRuntimeError(
            TASK_INPUT_INVALID,
            "the proposal describes a different input than the accepted one",
            accepted=accepted.kind,
            proposed=proposal.kind,
        )


def _marker(
    accepted: AcceptedInput,
    task: TaskState | None,
    disposition: str,
    reason: str,
) -> Reconciliation:
    return Reconciliation(
        reconciled_input_version=accepted.input_version,
        task_id=task.task_id if task else None,
        task_version=task.task_version if task else None,
        disposition=disposition,
        reason=reason,
    )


def _decide_taskless(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
) -> TaskRuntimeState:
    """A greeting or off-topic turn: no task, no task outcome.

    An existing task is NOT disturbed — only the taskless turn is
    recorded — and no clarification is opened, because a question is
    actionable user-held information, not the absence of one.
    """
    if proposal.question is not None:
        raise TaskRuntimeError(
            TASK_INPUT_INVALID,
            "a taskless turn cannot open a clarification",
            input_version=accepted.input_version,
        )
    task = state.current_task
    return replace(
        state,
        task_outcome=None,
        reconciliation=_marker(accepted, task, DISPOSITION_NONE, proposal.reason),
    )


def _decide_message(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
    task_id_factory: Callable[[AcceptedInput], str] | None,
) -> TaskRuntimeState:
    """A plain message: start, continue, or switch.

    The task version moves ONLY when the proposal names a material
    change. A progress input that leaves goal, expected result, and
    necessary conditions alone keeps the version, and therefore keeps
    the work already applicable under it.
    """
    current = state.current_task
    archives = state.archived_tasks
    # The marker records what this input DID to the task, so a turn that
    # keeps the current task intact reports ``continue``. ``start`` is
    # only true when this input is the one that opened the task.
    disposition = (
        DISPOSITION_CONTINUE if current is not None else DISPOSITION_START
    )

    if current is not None and proposal.switch:
        archives = (*archives, _archive_of(current, accepted, proposal))[-ARCHIVE_LIMIT:]
        current = None
        disposition = DISPOSITION_SWITCH
    elif current is not None and proposal.changed:
        current = _with_material_change(current, proposal)
        disposition = DISPOSITION_CONTINUE

    if current is None:
        factory = task_id_factory or default_task_id
        current = TaskState(
            task_id=str(factory(accepted)),
            goal=proposal.goal or accepted.payload or "",
            expected_result=proposal.expected_result,
            necessary_conditions=tuple(proposal.necessary_conditions),
            task_version=1,
            created_at_input_version=accepted.input_version,
        )

    question = proposal.question
    if question is not None:
        # A question created during a task transition binds to the
        # RESULTING task, so the binding is written after the task exists.
        question = PendingQuestion(
            question_id=question.question_id,
            task_id=current.task_id,
            task_version=current.task_version,
            prompt=question.prompt,
            choices=question.choices,
            input_version=accepted.input_version,
        )
        disposition = DISPOSITION_AWAIT_REPLY
    elif disposition != DISPOSITION_SWITCH:
        # A newer unrelated input does NOT expire a pending clarification
        # (spec decision 5). Only a switch drops the question, because the
        # task it was bound to has just been archived.
        question = state.pending_question

    return replace(
        state,
        current_task=current,
        archived_tasks=archives,
        pending_question=question,
        task_outcome=None,
        reconciliation=_marker(accepted, current, disposition, proposal.reason),
    )


def _archive_of(
    current: TaskState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
) -> ArchivedTask:
    return ArchivedTask(
        archive_id=(
            proposal.archive_id
            or f"archive-{current.task_id}-v{current.task_version}"
        ),
        task=current,
        archived_at_input_version=accepted.input_version,
        run_id=proposal.run_id or accepted.run_id,
        evidence_refs=tuple(proposal.evidence_refs),
    )


def _with_material_change(
    current: TaskState,
    proposal: ReconciliationProposal,
) -> TaskState:
    """Bump the task version for a material change, keeping everything
    else. Conditions are replaced only when the proposal says they
    changed, so naming one field does not silently reset the others."""
    return replace(
        current,
        goal=proposal.goal or current.goal,
        expected_result=proposal.expected_result or current.expected_result,
        task_version=current.task_version + 1,
        necessary_conditions=(
            tuple(proposal.necessary_conditions)
            if CHANGE_NECESSARY_CONDITIONS in proposal.changed
            else current.necessary_conditions
        ),
        outcome=TASK_ACTIVE,
        outcome_reason="",
    )


def _decide_reply(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
) -> TaskRuntimeState:
    """A clarification reply: resolve the question the reply names.

    A reply is valid only for the STILL-current question. An obsolete one
    is refused as expired and changes nothing — in particular it can
    neither answer nor restore some other task. A newer unrelated input
    alone does NOT expire a question: pending-ness is checked against the
    question itself, never against recency.
    """
    question = state.pending_question
    if question is None:
        raise TaskRuntimeError(
            TASK_REPLY_UNKNOWN,
            "no clarification is pending on this thread",
            input_version=accepted.input_version,
        )
    if question.question_id != accepted.question_id:
        raise TaskRuntimeError(
            TASK_REPLY_EXPIRED,
            "the clarification this reply names is no longer pending",
            question_id=accepted.question_id,
            pending_question_id=question.question_id,
        )
    if question.task_id and state.current_task is None:
        raise TaskRuntimeError(
            TASK_REPLY_EXPIRED,
            "the task this clarification belongs to is no longer current",
            question_id=question.question_id,
        )
    return replace(
        state,
        pending_question=None,
        reconciliation=_marker(
            accepted,
            state.current_task,
            DISPOSITION_CONTINUE,
            proposal.reason or "clarification answered",
        ),
    )


def _decide_restore(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
) -> TaskRuntimeState:
    """An explicit restore: make an archived task current ONLY after the
    rechecks pass.

    An unknown archive is refused. A known archive whose rechecks fail is
    refused naming each check that failed, and the current task is left
    exactly as it was: old results stay historical until a current
    assessment supports adopting them.
    """
    archive_id = accepted.archive_id or proposal.archive_id
    if not archive_id:
        raise TaskRuntimeError(
            TASK_RESTORE_UNKNOWN,
            "a restore must name the archived task it targets",
            input_version=accepted.input_version,
        )
    match = next(
        (row for row in state.archived_tasks if row.archive_id == archive_id), None
    )
    if match is None:
        raise TaskRuntimeError(
            TASK_RESTORE_UNKNOWN,
            "no archived task carries that id",
            archive_id=archive_id,
        )
    recheck = proposal.recheck
    if recheck is None:
        raise TaskRuntimeError(
            TASK_APPLICABILITY_UNRESOLVED,
            "restore requires current conditions, authority, version, "
            "and evidence checks",
            archive_id=archive_id,
        )
    failures = recheck.failures()
    if failures:
        raise TaskRuntimeError(
            TASK_RESTORE_NOT_ADOPTED,
            "the archived task is not adoptable under current conditions",
            archive_id=archive_id,
            reasons=list(failures),
        )
    return replace(
        state,
        current_task=match.task,
        archived_tasks=tuple(row for row in state.archived_tasks if row is not match),
        pending_question=None,
        task_outcome=None,
        reconciliation=_marker(
            accepted, match.task, DISPOSITION_RESTORE, proposal.reason
        ),
    )


def decide_cancellation(
    state: TaskRuntimeState,
    accepted: AcceptedInput,
    proposal: ReconciliationProposal,
    *,
    run_control_reported: str,
    effect_undone: bool | None = None,
) -> TaskRuntimeState:
    """Reconcile a cancellation as ONE accepted input.

    Cancellation follows reconciled user intent, so it enters through
    the same door as every other input and carries the same marker: an
    accepted input that was never reconciled has no authority to cancel
    anything. Recording it here (rather than only in
    :func:`record_cancellation`) is what makes ``cancel`` an execution
    disposition a dependent boundary can act on.
    """
    _guard_reconcilable(state, accepted, proposal)
    requested = proposal.reason.strip()
    if not requested:
        raise TaskRuntimeError(
            TASK_INTENT_MISSING, "cancellation requires reconciled user intent"
        )
    if not run_control_reported.strip():
        raise TaskRuntimeError(
            TASK_DEPENDENCY_FAILURE,
            "cancellation must record what run control reported",
        )
    recorded = record_cancellation(
        state,
        requested_reason=requested,
        run_control_reported=run_control_reported,
        input_version=accepted.input_version,
        effect_undone=effect_undone,
    )
    return replace(
        recorded,
        reconciliation=_marker(
            accepted, recorded.current_task, DISPOSITION_CANCEL, requested
        ),
    )


def record_cancellation(
    state: TaskRuntimeState,
    *,
    requested_reason: str,
    run_control_reported: str,
    input_version: int | None = None,
    task_id: str | None = None,
    effect_undone: bool | None = None,
) -> TaskRuntimeState:
    """Record reconciled cancellation intent and what run control said.

    Both halves are required: an intent with no run-control report, or a
    report with no intent, is a different product fact from a completed
    cancellation. ``effect_undone`` stays ``None`` unless the caller has
    evidence, so nothing here can claim an external effect was rolled
    back by assertion.

    Callers that hold an accepted input should prefer
    :func:`decide_cancellation`, which adds the reconciliation marker
    this leaves off.
    """
    if not requested_reason.strip():
        raise TaskRuntimeError(
            TASK_INTENT_MISSING, "cancellation requires reconciled user intent"
        )
    if not run_control_reported.strip():
        raise TaskRuntimeError(
            TASK_DEPENDENCY_FAILURE,
            "cancellation must record what run control reported",
        )
    task = state.current_task
    record = CancellationRecord(
        requested_reason=requested_reason,
        run_control_reported=run_control_reported,
        at_input_version=(
            input_version if input_version is not None else state.input_version
        ),
        task_id=task_id or (task.task_id if task else None),
        effect_undone=effect_undone,
    )
    cancelled = (
        replace(task, outcome=TASK_CANCELLED, outcome_reason=requested_reason)
        if task is not None
        else None
    )
    return replace(
        state,
        cancellations=(*state.cancellations, record),
        current_task=cancelled,
        task_outcome=TASK_CANCELLED if cancelled is not None else None,
    )


def apply_assessment(
    state: TaskRuntimeState,
    assessment: GroundingAssessment,
    gates: HostGates,
) -> TaskRuntimeState:
    """Apply a peer assessment against the committed current task.

    The task completes ONLY when the assessment supports the requested
    goal, names no material unresolved gap, and every host hard gate
    passes. Host mechanics alone certify nothing: passing gates with a
    gap still leaves the task active, and full support with a failed gate
    is a system failure rather than a completion.

    This is the single place ``task_outcome`` reaches ``complete``, which
    is what keeps "the turn ended" from being read as "the task is done".
    """
    current = state.current_task
    if current is None:
        raise TaskRuntimeError(
            TASK_INTENT_MISSING,
            "there is no current task for an assessment to apply to",
        )
    if current.outcome == TASK_CANCELLED:
        raise TaskRuntimeError(
            TASK_INPUT_INVALID,
            "a cancelled task cannot be completed by a later assessment",
            task_id=current.task_id,
        )
    gate_failures = gates.failures()
    if assessment.supported and not assessment.material_gaps and not gate_failures:
        return replace(
            state,
            current_task=replace(
                current,
                outcome=TASK_COMPLETE,
                outcome_reason=assessment.turn_outcome or "grounded",
            ),
            task_outcome=TASK_COMPLETE,
        )
    reason = (
        ",".join(gate_failures)
        if gate_failures
        else ("material_gap" if assessment.material_gaps else "unsupported")
    )
    return replace(
        state,
        current_task=replace(current, outcome=TASK_ACTIVE, outcome_reason=reason),
        task_outcome=TASK_ACTIVE,
    )


# ----- read surfaces -----


def current_task_summary(state: TaskRuntimeState) -> dict[str, Any] | None:
    """A bounded current-task view. ``None`` when the thread is taskless."""
    current = state.current_task
    if current is None:
        return None
    return {
        **current.to_state_dict(),
        "pending_question": pending_question_payload(state),
        "task_outcome": state.task_outcome or current.outcome,
    }


def archived_task_summaries(state: TaskRuntimeState) -> list[dict[str, Any]]:
    """Every locatable archive, newest first, with its run/evidence refs.

    ``outcome`` / ``outcome_reason`` are the archived task's OWN fields, not
    a statement that archiving did anything to them: an archive of a
    still-active task reports ``active``, because archiving is separate
    from completing or cancelling (specs/question-workspace.md W5). A
    reader that omitted them could not show that distinction, and would
    have to infer it — which is the inference this contract forbids.
    """
    return [
        {
            "archive_id": row.archive_id,
            "task_id": row.task.task_id,
            "goal": row.task.goal,
            "task_version": row.task.task_version,
            "archived_at_input_version": row.archived_at_input_version,
            "run_id": row.run_id,
            "evidence_refs": list(row.evidence_refs),
            "outcome": row.task.outcome,
            "outcome_reason": row.task.outcome_reason,
        }
        for row in reversed(state.archived_tasks)
    ]


def bounded_input_history(
    state: TaskRuntimeState,
    *,
    limit: int = INPUT_HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    """The most recent ``limit`` accepted inputs, originals included.

    An input whose original was never recoverable reports
    ``payload is None``; nothing here reconstructs a missing original from
    a summary. A non-positive ``limit`` yields an empty list rather than
    the whole history, so the bound cannot be bypassed by asking.
    """
    if limit <= 0:
        return []
    return [row.to_state_dict() for row in state.input_history[-limit:]]


def pending_question_payload(state: TaskRuntimeState) -> dict[str, Any] | None:
    """The wire payload for the still-pending clarification, or ``None``.

    Carries the FULL prompt, every listed choice, and exactly one Other,
    plus the task it is bound to. A dismissed question reads the same as
    a pending one — dismissal is presentation-only and supplies no
    answer.
    """
    question = state.pending_question
    return question.to_state_dict() if question else None


def reconcile_marker(state: TaskRuntimeState) -> dict[str, Any] | None:
    """The persisted reconciliation marker, or ``None`` before the first
    reconciliation. Dependant work reads this to know whether the current
    input has already been decided."""
    return state.reconciliation.to_state_dict() if state.reconciliation else None


def is_reconciled(state: TaskRuntimeState, input_version: int) -> bool:
    """Whether ``input_version`` already carries a committed decision.

    The check every dependant boundary makes before starting retrieval,
    tools, or answer formation — and the reason a later model or tool
    iteration does not reconcile the same input again.
    """
    return state.reconciliation is not None and (
        state.reconciliation.reconciled_input_version >= input_version
    )
