"""Outcome tests for the task runtime's task-vs-turn completion boundary.

Covers plans/question-task-runtime.md T6 (SC-7): a task reaches ``complete``
only when a grounded assessment supports the requested goal, names no material
gap, and every host hard gate passes; partial work and system failure leave the
task active; a turn ending does not complete a task.

Pure decision logic only: no store, no network, no model, no I/O.
"""

from __future__ import annotations

import pytest

from fin.host.task.task_runtime import accept_input, apply_assessment, decide, record_cancellation
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    TASK_ACTIVE,
    TASK_CAPABILITY_UNSUPPORTED,
    TASK_CHECKPOINT_FAILED,
    TASK_COMPLETE,
    TASK_DEPENDENCY_FAILURE,
    TASK_INPUT_INVALID,
    TASK_INTENT_MISSING,
    TASK_PERMISSION_DENIED,
    GroundingAssessment,
    HostGates,
    ReconciliationProposal,
    TaskRuntimeError,
    TaskRuntimeState,
)

SEND = "send-door"


def _current_task_state() -> TaskRuntimeState:
    """A committed, still-active current task with no assessment applied."""
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare revenue", provenance=SEND
    )
    return decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare revenue"),
    )


def test_supported_coverage_with_passing_gates_completes_the_task() -> None:
    state = _current_task_state()
    result = apply_assessment(state, GroundingAssessment(supported=True), HostGates())
    assert result.current_task.outcome == TASK_COMPLETE
    assert result.task_outcome == TASK_COMPLETE
    assert result.current_task.outcome_reason == "grounded"


def test_completion_carries_the_assessment_turn_outcome_as_the_reason() -> None:
    state = _current_task_state()
    result = apply_assessment(
        state, GroundingAssessment(supported=True, turn_outcome="answered"), HostGates()
    )
    assert result.current_task.outcome == TASK_COMPLETE
    assert result.current_task.outcome_reason == "answered"


def test_material_gap_keeps_the_task_active() -> None:
    state = _current_task_state()
    result = apply_assessment(
        state,
        GroundingAssessment(supported=True, material_gaps=("2023 segment missing",)),
        HostGates(),
    )
    assert result.current_task.outcome == TASK_ACTIVE
    assert result.task_outcome == TASK_ACTIVE
    assert result.current_task.outcome_reason == "material_gap"


def test_unsupported_assessment_keeps_the_task_active() -> None:
    state = _current_task_state()
    result = apply_assessment(state, GroundingAssessment(supported=False), HostGates())
    assert result.current_task.outcome == TASK_ACTIVE
    assert result.current_task.outcome_reason == "unsupported"


@pytest.mark.parametrize(
    ("gate_kwarg", "expected_kind"),
    [
        ({"permission": False}, TASK_PERMISSION_DENIED),
        ({"source_identity_version": False}, TASK_CAPABILITY_UNSUPPORTED),
        ({"receipts": False}, TASK_DEPENDENCY_FAILURE),
        ({"persistence": False}, TASK_CHECKPOINT_FAILED),
    ],
)
def test_failing_host_gate_keeps_the_task_active_naming_the_gate(
    gate_kwarg: dict[str, bool], expected_kind: str
) -> None:
    state = _current_task_state()
    result = apply_assessment(
        state, GroundingAssessment(supported=True), HostGates(**gate_kwarg)
    )
    assert result.current_task.outcome == TASK_ACTIVE
    assert result.task_outcome == TASK_ACTIVE
    assert result.current_task.outcome_reason == expected_kind


def test_multiple_failed_gates_are_named_in_a_stable_order() -> None:
    state = _current_task_state()
    result = apply_assessment(
        state,
        GroundingAssessment(supported=True),
        HostGates(permission=False, receipts=False),
    )
    assert result.current_task.outcome_reason == "task_permission_denied,task_dependency_failure"


def test_assessment_without_a_current_task_is_intent_missing() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        apply_assessment(TaskRuntimeState(), GroundingAssessment(supported=True), HostGates())
    assert err.value.kind == TASK_INTENT_MISSING


def test_assessment_on_a_cancelled_task_is_refused() -> None:
    cancelled = record_cancellation(
        _current_task_state(),
        requested_reason="user changed their mind",
        run_control_reported="graph halted",
    )
    with pytest.raises(TaskRuntimeError) as err:
        apply_assessment(cancelled, GroundingAssessment(supported=True), HostGates())
    assert err.value.kind == TASK_INPUT_INVALID


def test_a_decided_turn_without_an_assessment_does_not_complete_the_task() -> None:
    state = _current_task_state()
    assert state.current_task.outcome == TASK_ACTIVE
    assert state.task_outcome is None
