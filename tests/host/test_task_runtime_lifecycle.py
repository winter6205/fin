"""Lifecycle tests for the task runtime's taskless, question, archive, and
cancellation behavior.

Covers plans/question-task-runtime.md T3 (SC-3, SC-4, SC-8), T4 (SC-5), and
T5 (SC-6): taskless dialogue, the clarification lifecycle (atomic task+question
commit, preserved prompt/choices, Other, dismissal, stale replies, question
binding), switch/archive/restore with rechecks, and truthful cancellation.

Pure decision logic only: no store, no network, no model, no I/O.
"""

from __future__ import annotations

import pytest

from fin.host.task_runtime import (
    accept_input,
    decide,
    pending_question_payload,
    record_cancellation,
)
from fin.host.task_runtime_types import (
    ARCHIVE_LIMIT,
    DISPOSITION_AWAIT_REPLY,
    DISPOSITION_CONTINUE,
    DISPOSITION_NONE,
    DISPOSITION_RESTORE,
    DISPOSITION_SWITCH,
    INPUT_MESSAGE,
    INPUT_REPLY,
    INPUT_RESTORE,
    OTHER_CHOICE_ID,
    TASK_APPLICABILITY_UNRESOLVED,
    TASK_CANCELLED,
    TASK_CAPABILITY_UNSUPPORTED,
    TASK_DEPENDENCY_FAILURE,
    TASK_INPUT_INVALID,
    TASK_INSUFFICIENT_EVIDENCE,
    TASK_INTENT_MISSING,
    TASK_PERMISSION_DENIED,
    TASK_REPLY_EXPIRED,
    TASK_REPLY_UNKNOWN,
    TASK_RESTORE_NOT_ADOPTED,
    TASK_RESTORE_UNKNOWN,
    ArchivedTask,
    PendingQuestion,
    QuestionReply,
    ReconciliationProposal,
    RestoreRecheck,
    TaskRuntimeError,
    TaskRuntimeState,
    TaskState,
)

SEND = "send-door"
REPLY_DOOR = "reply-door"
RESTORE_DOOR = "restore-door"


def _started_task() -> TaskRuntimeState:
    """A committed current task with no pending question."""
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare revenue", provenance=SEND
    )
    return decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare revenue"),
    )


def _task_with_question(
    question_id: str = "q1", choices: tuple[str, ...] = ("2022", "2023")
) -> TaskRuntimeState:
    """An initial actionable request committed with its clarification."""
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare revenue", provenance=SEND
    )
    question = PendingQuestion(
        question_id=question_id,
        task_id="",
        task_version=1,
        prompt="Which period should I compare?",
        choices=choices,
        input_version=1,
    )
    return decide(
        state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare revenue", question=question),
    )


def _switched() -> tuple[TaskRuntimeState, str]:
    """A state whose previous task is archived, plus the archive id."""
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare revenue", provenance=SEND
    )
    first = decide(
        state, accepted, ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare revenue")
    )
    state2, accepted2 = accept_input(
        first, kind=INPUT_MESSAGE, payload="now cash flow", provenance=SEND
    )
    switched = decide(
        state2,
        accepted2,
        ReconciliationProposal(
            kind=INPUT_MESSAGE,
            switch=True,
            goal="cash flow",
            archive_id="arch-1",
            run_id="run-7",
            evidence_refs=("ev-1", "ev-2"),
        ),
    )
    return switched, "arch-1"


# ----- SC-3: taskless dialogue -----


def test_taskless_turn_stays_taskless_with_no_outcome_and_none_disposition() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="good morning", provenance=SEND
    )
    next_state = decide(
        state, accepted, ReconciliationProposal(kind=INPUT_MESSAGE, taskless=True)
    )
    assert next_state.current_task is None
    assert next_state.task_outcome is None
    assert next_state.reconciliation.disposition == DISPOSITION_NONE


def test_taskless_turn_cannot_open_a_clarification() -> None:
    question = PendingQuestion(
        question_id="q1",
        task_id="",
        task_version=1,
        prompt="Which?",
        choices=("a",),
        input_version=1,
    )
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="hello", provenance=SEND
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(
            state,
            accepted,
            ReconciliationProposal(kind=INPUT_MESSAGE, taskless=True, question=question),
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_taskless_turn_on_an_existing_task_leaves_it_intact() -> None:
    started = _task_with_question()
    state, accepted = accept_input(
        started, kind=INPUT_MESSAGE, payload="thanks!", provenance=SEND
    )
    next_state = decide(
        state, accepted, ReconciliationProposal(kind=INPUT_MESSAGE, taskless=True)
    )
    assert next_state.current_task == started.current_task
    assert next_state.pending_question == started.pending_question


# ----- SC-3 / SC-4 / SC-8: clarification lifecycle -----


def test_initial_actionable_request_commits_task_and_question_together() -> None:
    state = _task_with_question()
    assert state.current_task is not None
    assert state.pending_question is not None
    # The question binds to the RESULTING task, not a null or previous basis.
    assert state.pending_question.task_id == state.current_task.task_id
    assert state.pending_question.task_version == state.current_task.task_version
    assert state.reconciliation.disposition == DISPOSITION_AWAIT_REPLY


def test_pending_question_payload_preserves_full_prompt_and_one_trailing_other() -> None:
    state = _task_with_question(choices=("2022", "2023"))
    payload = pending_question_payload(state)
    assert payload["prompt"] == "Which period should I compare?"
    assert payload["choices"] == ["2022", "2023", "other"]
    assert payload["choices"].count("other") == 1
    assert payload["task_id"] == state.current_task.task_id


def test_question_round_trip_does_not_promote_other_into_the_listed_choices() -> None:
    state = _task_with_question(choices=("2022", "2023"))
    restored = TaskRuntimeState.from_state_dict(state.to_state_dict())
    assert restored.pending_question.choices == ("2022", "2023")
    assert restored.pending_question.offered_choices == ("2022", "2023", "other")


def test_question_without_a_prompt_is_refused() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        PendingQuestion(
            question_id="q1", task_id="t", task_version=1, prompt="   ", choices=("a",), input_version=1
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_question_without_choices_is_refused() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        PendingQuestion(
            question_id="q1", task_id="t", task_version=1, prompt="Which?", choices=(), input_version=1
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_valid_reply_clears_the_question_and_advances_input_once() -> None:
    queried = _task_with_question()
    state, accepted = accept_input(
        queried,
        kind=INPUT_REPLY,
        payload="2022",
        provenance=REPLY_DOOR,
        question_id="q1",
        choice_id="2022",
    )
    replied = decide(state, accepted, ReconciliationProposal(kind=INPUT_REPLY))
    assert replied.pending_question is None
    assert replied.input_version == queried.input_version + 1
    assert accepted.choice_id == "2022"
    assert replied.reconciliation.disposition == DISPOSITION_CONTINUE


def test_other_reply_answers_with_its_free_text_verbatim() -> None:
    reply = QuestionReply(
        question_id="q1", choice_id=OTHER_CHOICE_ID, free_text="Compare FY2023 instead"
    )
    assert reply.is_other is True
    assert reply.answer_text() == "Compare FY2023 instead"


def test_listed_choice_reply_answers_with_its_choice_id() -> None:
    reply = QuestionReply(question_id="q1", choice_id="2022")
    assert reply.is_other is False
    assert reply.answer_text() == "2022"


def test_reply_with_a_blank_choice_id_is_refused() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        QuestionReply(question_id="q1", choice_id="   ")
    assert err.value.kind == TASK_INPUT_INVALID


def test_dismissal_is_presentation_only_and_leaves_the_question_pending() -> None:
    state = _task_with_question()
    first = pending_question_payload(state)
    second = pending_question_payload(state)
    assert first == second
    assert state.pending_question is not None
    # Reading the payload is not a state transition.
    assert state == _task_with_question()


def test_new_unrelated_message_does_not_expire_the_pending_question() -> None:
    queried = _task_with_question()
    state, accepted = accept_input(
        queried, kind=INPUT_MESSAGE, payload="also add MSFT", provenance=SEND
    )
    after = decide(state, accepted, ReconciliationProposal(kind=INPUT_MESSAGE))
    assert after.pending_question is not None

    reply_state, reply_accepted = accept_input(
        after,
        kind=INPUT_REPLY,
        payload="2022",
        provenance=REPLY_DOOR,
        question_id="q1",
        choice_id="2022",
    )
    replied = decide(reply_state, reply_accepted, ReconciliationProposal(kind=INPUT_REPLY))
    assert replied.pending_question is None


def test_reply_to_an_obsolete_question_expires_and_changes_nothing() -> None:
    queried = _task_with_question()
    state, accepted = accept_input(
        queried,
        kind=INPUT_REPLY,
        payload="2022",
        provenance=REPLY_DOOR,
        question_id="some-other-question",
        choice_id="2022",
    )
    snapshot = state
    with pytest.raises(TaskRuntimeError) as err:
        decide(state, accepted, ReconciliationProposal(kind=INPUT_REPLY))
    assert err.value.kind == TASK_REPLY_EXPIRED
    assert state == snapshot


def test_reply_with_no_pending_question_is_unknown() -> None:
    started = _started_task()
    state, accepted = accept_input(
        started,
        kind=INPUT_REPLY,
        payload="2022",
        provenance=REPLY_DOOR,
        question_id="q1",
        choice_id="2022",
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(state, accepted, ReconciliationProposal(kind=INPUT_REPLY))
    assert err.value.kind == TASK_REPLY_UNKNOWN


# ----- SC-5: archive and restore -----


def test_switch_archives_the_outgoing_task_and_leaves_one_current() -> None:
    switched, archive_id = _switched()
    assert len(switched.archived_tasks) == 1
    assert switched.archived_tasks[0].archive_id == archive_id
    assert switched.archived_tasks[0].task.goal == "compare revenue"
    assert switched.current_task.goal == "cash flow"
    assert switched.reconciliation.disposition == DISPOSITION_SWITCH


def test_archive_carries_run_and_evidence_refs() -> None:
    switched, _ = _switched()
    row = switched.archived_tasks[0]
    assert row.run_id == "run-7"
    assert row.evidence_refs == ("ev-1", "ev-2")


def test_archive_falls_back_to_the_switch_input_run_id() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_MESSAGE, payload="compare revenue", provenance=SEND
    )
    first = decide(
        state, accepted, ReconciliationProposal(kind=INPUT_MESSAGE, goal="compare revenue")
    )
    # The archive references the run of the input that triggered the switch,
    # so that input's run_id is the fallback when the proposal names none.
    state2, accepted2 = accept_input(
        first, kind=INPUT_MESSAGE, payload="switch", provenance=SEND, run_id="run-from-input"
    )
    switched = decide(
        state2, accepted2, ReconciliationProposal(kind=INPUT_MESSAGE, switch=True, goal="cash flow")
    )
    assert switched.archived_tasks[0].run_id == "run-from-input"


def test_archive_overflow_drops_the_oldest() -> None:
    archives = tuple(
        ArchivedTask(
            archive_id=f"a{index}",
            task=TaskState(task_id=f"t{index}", goal="g", expected_result=""),
            archived_at_input_version=index + 1,
        )
        for index in range(ARCHIVE_LIMIT)
    )
    state = TaskRuntimeState(
        current_task=TaskState(task_id="cur", goal="current", expected_result=""),
        archived_tasks=archives,
        input_version=100,
    )
    state, accepted = accept_input(
        state, kind=INPUT_MESSAGE, payload="switch again", provenance=SEND
    )
    switched = decide(
        state, accepted, ReconciliationProposal(kind=INPUT_MESSAGE, switch=True, goal="newest")
    )
    assert len(switched.archived_tasks) == ARCHIVE_LIMIT
    assert switched.archived_tasks[0].archive_id == "a1"
    assert switched.archived_tasks[-1].archive_id == "archive-cur-v1"


def test_restore_without_an_archive_id_is_unknown() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(), kind=INPUT_RESTORE, payload="restore it", provenance=RESTORE_DOOR
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(state, accepted, ReconciliationProposal(kind=INPUT_RESTORE))
    assert err.value.kind == TASK_RESTORE_UNKNOWN


def test_restore_of_an_unknown_archive_is_unknown() -> None:
    state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_RESTORE,
        payload="restore it",
        provenance=RESTORE_DOOR,
        archive_id="not-there",
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(
            state, accepted, ReconciliationProposal(kind=INPUT_RESTORE, archive_id="not-there")
        )
    assert err.value.kind == TASK_RESTORE_UNKNOWN


def test_restore_without_rechecks_is_applicability_unresolved() -> None:
    switched, archive_id = _switched()
    state, accepted = accept_input(
        switched,
        kind=INPUT_RESTORE,
        payload="restore it",
        provenance=RESTORE_DOOR,
        archive_id=archive_id,
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(state, accepted, ReconciliationProposal(kind=INPUT_RESTORE, archive_id=archive_id))
    assert err.value.kind == TASK_APPLICABILITY_UNRESOLVED


@pytest.mark.parametrize(
    ("recheck", "expected_reason"),
    [
        (RestoreRecheck(False, True, True, True), TASK_APPLICABILITY_UNRESOLVED),
        (RestoreRecheck(True, False, True, True), TASK_PERMISSION_DENIED),
        (RestoreRecheck(True, True, False, True), TASK_CAPABILITY_UNSUPPORTED),
        (RestoreRecheck(True, True, True, False), TASK_INSUFFICIENT_EVIDENCE),
    ],
)
def test_failing_restore_recheck_names_the_failed_check(
    recheck: RestoreRecheck, expected_reason: str
) -> None:
    switched, archive_id = _switched()
    state, accepted = accept_input(
        switched,
        kind=INPUT_RESTORE,
        payload="restore it",
        provenance=RESTORE_DOOR,
        archive_id=archive_id,
    )
    with pytest.raises(TaskRuntimeError) as err:
        decide(
            state,
            accepted,
            ReconciliationProposal(kind=INPUT_RESTORE, archive_id=archive_id, recheck=recheck),
        )
    assert err.value.kind == TASK_RESTORE_NOT_ADOPTED
    assert err.value.detail["reasons"] == [expected_reason]


def test_failed_restore_leaves_the_current_task_unchanged() -> None:
    switched, archive_id = _switched()
    state, accepted = accept_input(
        switched,
        kind=INPUT_RESTORE,
        payload="restore it",
        provenance=RESTORE_DOOR,
        archive_id=archive_id,
    )
    snapshot = state
    with pytest.raises(TaskRuntimeError):
        decide(
            state,
            accepted,
            ReconciliationProposal(
                kind=INPUT_RESTORE,
                archive_id=archive_id,
                recheck=RestoreRecheck(False, True, True, True),
            ),
        )
    assert state == snapshot
    assert state.current_task.goal == "cash flow"


def test_successful_restore_makes_the_archived_task_current_and_removes_it() -> None:
    switched, archive_id = _switched()
    archived_task = switched.archived_tasks[0].task
    state, accepted = accept_input(
        switched,
        kind=INPUT_RESTORE,
        payload="restore it",
        provenance=RESTORE_DOOR,
        archive_id=archive_id,
    )
    restored = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_RESTORE,
            archive_id=archive_id,
            recheck=RestoreRecheck(True, True, True, True),
        ),
    )
    assert restored.current_task == archived_task
    assert all(row.archive_id != archive_id for row in restored.archived_tasks)
    assert restored.reconciliation.disposition == DISPOSITION_RESTORE


# ----- SC-6 / SC-8: truthful cancellation -----


def test_cancellation_records_both_halves_without_claiming_an_undone_effect() -> None:
    started = _started_task()
    cancelled = record_cancellation(
        started,
        requested_reason="user changed their mind",
        run_control_reported="graph halted",
    )
    assert len(cancelled.cancellations) == 1
    row = cancelled.cancellations[0]
    assert row.requested_reason == "user changed their mind"
    assert row.run_control_reported == "graph halted"
    assert row.effect_undone is None
    assert row.at_input_version == started.input_version
    assert row.task_id == started.current_task.task_id
    assert cancelled.current_task.outcome == TASK_CANCELLED
    assert cancelled.task_outcome == TASK_CANCELLED


def test_cancellation_without_user_intent_is_refused() -> None:
    started = _started_task()
    with pytest.raises(TaskRuntimeError) as err:
        record_cancellation(
            started, requested_reason="   ", run_control_reported="graph halted"
        )
    assert err.value.kind == TASK_INTENT_MISSING


def test_cancellation_without_a_run_control_report_is_refused() -> None:
    started = _started_task()
    with pytest.raises(TaskRuntimeError) as err:
        record_cancellation(started, requested_reason="user asked", run_control_reported="")
    assert err.value.kind == TASK_DEPENDENCY_FAILURE


def test_cancellation_on_a_taskless_thread_has_no_task_outcome() -> None:
    cancelled = record_cancellation(
        TaskRuntimeState(), requested_reason="user asked", run_control_reported="nothing running"
    )
    assert cancelled.current_task is None
    assert cancelled.task_outcome is None
    assert len(cancelled.cancellations) == 1


def test_cancellation_records_an_undone_effect_only_when_evidence_is_supplied() -> None:
    started = _started_task()
    cancelled = record_cancellation(
        started,
        requested_reason="user asked",
        run_control_reported="graph halted",
        effect_undone=True,
    )
    assert cancelled.cancellations[0].effect_undone is True
