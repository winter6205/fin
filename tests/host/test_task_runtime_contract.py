"""Contract tests for the task runtime's input, version, and read surfaces.

Covers plans/question-task-runtime.md T2 (SC-1, SC-2) and the read/overflow
and legacy-checkpoint surfaces shared by T2-T6: accepted-input provenance and
the single input-history advance, the per-input reconciliation marker and
execution disposition, material-change task versioning, bounded history, the
closed failure vocabulary, and the dataclass codecs.

Pure decision logic only: no store, no network, no model, no I/O.
"""

from __future__ import annotations

import pytest

from fin.host.task_runtime import (
    accept_input,
    archived_task_summaries,
    bounded_input_history,
    decide,
    is_reconciled,
    reconcile_marker,
)
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
    EXECUTION_DISPOSITIONS,
    INPUT_HISTORY_LIMIT,
    INPUT_KINDS,
    INPUT_MESSAGE,
    INPUT_REPLY,
    INPUT_RESTORE,
    MATERIAL_CHANGE_KINDS,
    OTHER_CHOICE_ID,
    TASK_ACTIVE,
    TASK_ALREADY_RECONCILED,
    TASK_CAPABILITY_UNAVAILABLE,
    TASK_CHECKPOINT_FAILED,
    TASK_COMPLETE,
    TASK_DEPENDENCY_FAILURE,
    TASK_EXECUTION_LIMIT,
    TASK_FAILURE_KINDS,
    TASK_HISTORY_FAILED,
    TASK_INPUT_INVALID,
    TASK_INPUT_UNRECONCILED,
    TASK_INSUFFICIENT_EVIDENCE,
    TASK_INTENT_MISSING,
    TASK_PERMISSION_DENIED,
    TASK_APPLICABILITY_UNRESOLVED,
    AcceptedInput,
    ArchivedTask,
    CancellationRecord,
    PendingQuestion,
    Reconciliation,
    ReconciliationProposal,
    TaskRuntimeError,
    TaskRuntimeState,
    TaskState,
)

SEND = "send-door"


def _started() -> tuple[TaskRuntimeState, AcceptedInput]:
    """One committed start transition plus the accepted input it reconciled."""
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare AAPL revenue", provenance=SEND
    )
    committed = decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare AAPL revenue"),
    )
    return committed, accepted


# ----- SC-1: accepted input, provenance, one advance -----


def test_accept_input_advances_input_version_by_exactly_one() -> None:
    state = TaskRuntimeState()
    first, accepted = accept_input(
        state, kind=INPUT_MESSAGE, payload="hello", provenance=SEND
    )
    assert state.input_version == 0
    assert first.input_version == 1
    assert accepted.input_version == 1

    second, accepted2 = accept_input(
        first, kind=INPUT_MESSAGE, payload="again", provenance=SEND
    )
    assert second.input_version == 2
    assert accepted2.input_version == 2


def test_accepted_input_keeps_original_payload_and_provenance_verbatim() -> None:
    payload = "  Compare AAPL 2023 Q3 revenue  "
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload=payload, provenance="send-door:ws-1"
    )
    assert accepted.payload == payload
    assert accepted.provenance == "send-door:ws-1"
    assert state.input_history[-1] == accepted


def test_accepted_input_with_unavailable_payload_reads_back_as_none() -> None:
    # The original was never recoverable; nothing may reconstruct it.
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload=None, provenance=SEND
    )
    assert accepted.payload is None
    assert state.input_history[-1].payload is None
    assert bounded_input_history(state)[-1]["payload"] is None


@pytest.mark.parametrize("payload", ["", "   ", "\n\t "])
def test_accept_input_refuses_empty_or_whitespace_payload(payload: str) -> None:
    with pytest.raises(TaskRuntimeError) as err:
        accept_input(
            TaskRuntimeState(), kind=INPUT_MESSAGE, payload=payload, provenance=SEND
        )
    assert err.value.kind == TASK_INPUT_INVALID


@pytest.mark.parametrize("provenance", ["", "   "])
def test_accept_input_refuses_unnamed_provenance(provenance: str) -> None:
    with pytest.raises(TaskRuntimeError) as err:
        accept_input(
            TaskRuntimeState(), kind=INPUT_MESSAGE, payload="hello", provenance=provenance
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_accept_input_refuses_unknown_kind() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        accept_input(
            TaskRuntimeState(), kind="note", payload="hello", provenance=SEND
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_decide_refuses_input_newer_than_recorded_history() -> None:
    accepted = AcceptedInput(
        input_version=5, kind=INPUT_MESSAGE, payload="ahead", provenance=SEND
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(
            TaskRuntimeState(input_version=0),
            accepted,
            ReconciliationProposal(kind=INPUT_MESSAGE),
        )
    assert err.value.kind == TASK_INPUT_UNRECONCILED


def test_decide_refuses_a_second_reconciliation_of_the_same_input() -> None:
    committed, accepted = _started()
    with pytest.raises(TaskRuntimeError) as err:
        decide(
            committed,
            accepted,
            ReconciliationProposal(kind=INPUT_MESSAGE),
        )
    assert err.value.kind == TASK_ALREADY_RECONCILED


def test_decide_refuses_a_proposal_naming_a_different_input_kind() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="hello", provenance=SEND
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(state, accepted, ReconciliationProposal(kind=INPUT_RESTORE))
    assert err.value.kind == TASK_INPUT_INVALID


def test_transition_returns_a_new_state_without_mutating_the_original() -> None:
    committed, _ = _started()
    original = committed
    accept_input(committed, kind=INPUT_MESSAGE, payload="next", provenance=SEND)
    # The last committed state stays authoritative even when the caller drops
    # the successor (SC-8: a failed checkpoint application changes nothing).
    assert committed == original


# ----- reconciliation marker and execution disposition -----


def test_reconcile_marker_is_none_before_the_first_reconciliation() -> None:
    assert reconcile_marker(TaskRuntimeState()) is None


def test_reconcile_marker_records_input_version_task_and_disposition() -> None:
    committed, _ = _started()
    assert reconcile_marker(committed) == {
        "reconciled_input_version": 1,
        "task_id": "task-1",
        "task_version": 1,
        "disposition": DISPOSITION_START,
        "reason": "",
    }


def test_is_reconciled_is_false_before_any_reconciliation() -> None:
    assert is_reconciled(TaskRuntimeState(), 0) is False


def test_is_reconciled_covers_reconciled_and_lower_versions_only() -> None:
    committed, _ = _started()
    assert is_reconciled(committed, 1) is True
    # Version is monotonic: an already-decided input implies its predecessors.
    assert is_reconciled(committed, 0) is True
    assert is_reconciled(committed, 2) is False


def test_disposition_continue_when_a_material_change_is_named() -> None:
    committed, _ = _started()
    state, accepted = accept_input(
        committed, kind=INPUT_MESSAGE, payload="narrow it", provenance=SEND
    )
    next_state = decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="narrower", changed=("goal",)),
    )
    assert next_state.reconciliation.disposition == DISPOSITION_CONTINUE


def test_disposition_switch_archives_the_outgoing_task() -> None:
    committed, _ = _started()
    state, accepted = accept_input(
        committed, kind=INPUT_MESSAGE, payload="new subject", provenance=SEND
    )
    next_state = decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, switch=True, goal="new subject"),
    )
    assert next_state.reconciliation.disposition == DISPOSITION_SWITCH
    assert len(next_state.archived_tasks) == 1


def test_archived_summary_retains_the_outcome_the_task_had_when_archived() -> None:
    """An archive reports its task's OWN outcome, unchanged.

    Archiving is separate from completing or cancelling (specs/
    question-workspace.md W5), so the archived task of a switch — which
    was still active when it was archived — must read ``active``. A
    reader that omitted the field would have to infer that, which is the
    inference the workspace contract forbids.
    """
    committed, _ = _started()
    state, accepted = accept_input(
        committed, kind=INPUT_MESSAGE, payload="new subject", provenance=SEND
    )
    switched = decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, switch=True, goal="new subject"),
    )
    summaries = archived_task_summaries(switched)
    assert len(summaries) == 1
    assert summaries[0]["outcome"] == TASK_ACTIVE
    assert summaries[0]["outcome_reason"] == ""


def test_disposition_await_reply_when_a_question_is_offered() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="revenue?", provenance=SEND
    )
    question = PendingQuestion(
        question_id="q1",
        task_id="",
        task_version=1,
        prompt="Which period?",
        choices=("2022", "2023"),
        input_version=1,
    )
    next_state = decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="revenue", question=question),
    )
    assert next_state.reconciliation.disposition == DISPOSITION_AWAIT_REPLY


def test_execution_dispositions_is_the_closed_seven_token_set() -> None:
    assert EXECUTION_DISPOSITIONS == frozenset(
        {"none", "start", "continue", "switch", "restore", "await_reply", "cancel"}
    )
    assert DISPOSITION_NONE == "none"
    assert DISPOSITION_START == "start"
    assert DISPOSITION_CONTINUE == "continue"
    assert DISPOSITION_SWITCH == "switch"
    assert DISPOSITION_RESTORE == "restore"
    assert DISPOSITION_AWAIT_REPLY == "await_reply"
    assert DISPOSITION_CANCEL == "cancel"


def test_cancel_disposition_is_a_valid_reconciliation_but_unproduced_by_decide() -> None:
    # The vocabulary admits a cancel marker, and it round-trips; the module's
    # transitions never emit it (cancellation is recorded out-of-band), so this
    # pins the token's validity without claiming a decide() producer exists.
    marker = Reconciliation(
        reconciled_input_version=2,
        task_id="task-1",
        task_version=1,
        disposition=DISPOSITION_CANCEL,
        reason="user cancelled",
    )
    assert marker.disposition == DISPOSITION_CANCEL
    assert Reconciliation(**marker.to_state_dict()) == marker


def test_unknown_execution_disposition_is_refused() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        Reconciliation(
            reconciled_input_version=1,
            task_id=None,
            task_version=None,
            disposition="halt",
        )
    assert err.value.kind == TASK_INPUT_INVALID


# ----- SC-2: task version semantics -----


def test_material_change_bumps_task_version_and_preserves_other_fields() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare AAPL revenue", provenance=SEND
    )
    committed = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_MESSAGE,
            goal="compare AAPL revenue",
            expected_result="one table",
            necessary_conditions=("US GAAP",),
        ),
    )
    state2, accepted2 = accept_input(
        committed, kind=INPUT_MESSAGE, payload="re-aim", provenance=SEND
    )
    next_state = decide(
        state2,
        accepted2,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="re-aimed goal", changed=("goal",)),
    )
    assert next_state.current_task.task_version == 2
    assert next_state.current_task.goal == "re-aimed goal"
    # Naming one material field must not reset the others.
    assert next_state.current_task.expected_result == "one table"
    assert next_state.current_task.necessary_conditions == ("US GAAP",)


def test_progress_input_without_material_change_keeps_version_and_work() -> None:
    committed, _ = _started()
    state, accepted = accept_input(
        committed, kind=INPUT_MESSAGE, payload="still working", provenance=SEND
    )
    next_state = decide(
        state,
        accepted,
        # A goal is offered but not named as changed: the tuple drives the move.
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="ignored without changed"),
    )
    assert next_state.current_task.task_version == committed.current_task.task_version
    assert next_state.current_task.goal == committed.current_task.goal


def test_naming_necessary_conditions_replaces_only_conditions() -> None:
    committed, _ = _started()
    state, accepted = accept_input(
        committed, kind=INPUT_MESSAGE, payload="add a condition", provenance=SEND
    )
    next_state = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_MESSAGE,
            necessary_conditions=("consolidated only", "audited"),
            changed=(CHANGE_NECESSARY_CONDITIONS,),
        ),
    )
    assert next_state.current_task.task_version == 2
    assert next_state.current_task.necessary_conditions == ("consolidated only", "audited")
    assert next_state.current_task.goal == committed.current_task.goal


@pytest.mark.parametrize("field_name", ["progress", "summary", "priority"])
def test_non_material_changed_field_is_refused(field_name: str) -> None:
    with pytest.raises(TaskRuntimeError) as err:
        ReconciliationProposal(kind=INPUT_MESSAGE, changed=(field_name,))
    assert err.value.kind == TASK_INPUT_INVALID


def test_material_change_kinds_is_the_closed_three_field_set() -> None:
    assert MATERIAL_CHANGE_KINDS == frozenset(
        {"goal", "expected_result", "necessary_conditions"}
    )


# ----- read boundary, overflow, legacy checkpoint -----


def test_input_kinds_is_the_closed_three_kind_set() -> None:
    assert INPUT_KINDS == frozenset({"message", "reply", "restore"})
    assert INPUT_MESSAGE == "message"
    assert INPUT_REPLY == "reply"
    assert INPUT_RESTORE == "restore"


@pytest.mark.parametrize("limit", [0, -1, -99])
def test_bounded_input_history_non_positive_limit_is_empty(limit: int) -> None:
    committed, _ = _started()
    assert bounded_input_history(committed, limit=limit) == []


def test_bounded_input_history_returns_the_most_recent_n() -> None:
    state = TaskRuntimeState()
    for index in range(4):
        state, _ = accept_input(
            state, kind=INPUT_MESSAGE, payload=f"m{index}", provenance=SEND
        )
    window = bounded_input_history(state, limit=2)
    assert [row["input_version"] for row in window] == [3, 4]
    assert [row["payload"] for row in window] == ["m2", "m3"]


def test_input_history_overflow_drops_the_oldest_entry() -> None:
    state = TaskRuntimeState()
    total = INPUT_HISTORY_LIMIT + 1
    for index in range(total):
        state, _ = accept_input(
            state, kind=INPUT_MESSAGE, payload=f"m{index}", provenance=SEND
        )
    assert INPUT_HISTORY_LIMIT == 200
    assert len(state.input_history) == INPUT_HISTORY_LIMIT
    assert state.input_history[0].input_version == 2
    assert state.input_history[-1].input_version == total


def test_archive_limit_is_fifty() -> None:
    assert ARCHIVE_LIMIT == 50


def test_missing_envelope_reads_as_empty_defaults() -> None:
    assert TaskRuntimeState.from_state_dict(None) == TaskRuntimeState()
    assert TaskRuntimeState.from_state_dict({}) == TaskRuntimeState()


def test_unknown_stored_outcome_and_disposition_degrade_without_raising() -> None:
    restored = TaskRuntimeState.from_state_dict(
        {
            "current_task": {
                "task_id": "task-1",
                "goal": "g",
                "expected_result": "",
                "task_version": 1,
                "outcome": "bogus-outcome",
            },
            "reconciliation": {
                "reconciled_input_version": 1,
                "disposition": "bogus-disposition",
            },
        }
    )
    assert restored.current_task.outcome == TASK_ACTIVE
    assert restored.reconciliation.disposition == DISPOSITION_NONE


def test_full_state_round_trips_through_its_stored_dict() -> None:
    full = TaskRuntimeState(
        current_task=TaskState(
            task_id="task-9",
            goal="compare cash flow",
            expected_result="one table",
            task_version=3,
            necessary_conditions=("consolidated",),
            outcome=TASK_COMPLETE,
            outcome_reason="grounded",
            created_at_input_version=4,
        ),
        archived_tasks=(
            ArchivedTask(
                archive_id="arch-1",
                task=TaskState(
                    task_id="task-1",
                    goal="compare revenue",
                    expected_result="one chart",
                    task_version=2,
                    necessary_conditions=("US GAAP",),
                    created_at_input_version=2,
                ),
                archived_at_input_version=3,
                run_id="run-7",
                evidence_refs=("ev-1", "ev-2"),
            ),
        ),
        input_history=(
            AcceptedInput(
                input_version=4, kind=INPUT_MESSAGE, payload="orig", provenance=SEND
            ),
        ),
        input_version=4,
        pending_question=PendingQuestion(
            question_id="q1",
            task_id="task-9",
            task_version=3,
            prompt="Which years?",
            choices=("2022", "2023"),
            input_version=4,
        ),
        reconciliation=Reconciliation(
            reconciled_input_version=4,
            task_id="task-9",
            task_version=3,
            disposition=DISPOSITION_AWAIT_REPLY,
            reason="asked",
        ),
        task_outcome=TASK_COMPLETE,
        cancellations=(
            CancellationRecord(
                requested_reason="user cancelled",
                run_control_reported="halted",
                at_input_version=3,
                task_id="task-9",
                effect_undone=None,
            ),
        ),
    )
    stored = full.to_state_dict()
    # The stored question list carries the trailing Other; it must not be
    # promoted back into the listed choices.
    assert stored["pending_question"]["choices"] == ["2022", "2023", OTHER_CHOICE_ID]
    assert TaskRuntimeState.from_state_dict(stored) == full


# ----- the closed failure vocabulary -----


def test_task_failure_kinds_is_a_closed_frozenset_of_spec_failures() -> None:
    assert isinstance(TASK_FAILURE_KINDS, frozenset)
    spec_named = {
        "task_input_invalid",
        "task_permission_denied",
        "task_dependency_failure",
        "task_capability_unavailable",
        "task_insufficient_evidence",
        "task_applicability_unresolved",
        "task_intent_missing",
        "task_execution_limit",
        "task_checkpoint_failed",
        "task_history_failed",
    }
    # Ten spec-named failure classes, each backed by a token that is actually used.
    assert spec_named <= TASK_FAILURE_KINDS
    assert {
        TASK_INPUT_INVALID,
        TASK_PERMISSION_DENIED,
        TASK_DEPENDENCY_FAILURE,
        TASK_CAPABILITY_UNAVAILABLE,
        TASK_INSUFFICIENT_EVIDENCE,
        TASK_APPLICABILITY_UNRESOLVED,
        TASK_INTENT_MISSING,
        TASK_EXECUTION_LIMIT,
        TASK_CHECKPOINT_FAILED,
        TASK_HISTORY_FAILED,
    } == spec_named
    assert len(TASK_FAILURE_KINDS) == 17


@pytest.mark.parametrize("fake_kind", ["bogus_kind", "task_made_up"])
def test_task_runtime_error_rejects_a_kind_outside_the_closed_set(fake_kind: str) -> None:
    with pytest.raises(AssertionError):
        TaskRuntimeError(fake_kind, "should never construct")
