"""Behavioral tests for the ingest state machine and audit trail."""

from __future__ import annotations

import concurrent.futures
from datetime import timezone
from threading import Barrier

import pytest

from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.ingest_state_errors import (
    AuditOverflowError,
    IllegalTransitionError,
    IngestBusyError,
    IngestStateError,
    IngestStepError,
    InvalidStateError,
    UnknownJobError,
)


def test_empty_store_has_no_jobs_or_audit_rows() -> None:
    store = InMemoryIngestStateStore()

    assert store.list_jobs() == ()
    assert store.list_audit() == ()


def test_happy_path_reaches_ready_and_records_process_audit() -> None:
    store = InMemoryIngestStateStore()
    store.create_job("filing-1", actor="pytest")

    store.transition("filing-1", IngestState.FETCHING, actor="pytest")
    store.transition("filing-1", IngestState.EXTRACTING, actor="pytest")
    store.transition("filing-1", IngestState.OCR, actor="pytest")
    store.transition("filing-1", IngestState.CATALOGING, actor="pytest")
    store.transition("filing-1", IngestState.READY, actor="pytest")

    job = store.get_job("filing-1")
    assert job.state is IngestState.READY
    assert [row.action for row in store.list_audit()] == [
        "job_created",
        "transition",
        "transition",
        "transition",
        "transition",
        "transition",
    ]
    assert all(row.actor == "pytest" for row in store.list_audit())
    assert all(row.occurred_at.tzinfo is timezone.utc for row in store.list_audit())


def test_illegal_transition_is_typed_and_audited() -> None:
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")

    with pytest.raises(IllegalTransitionError) as exc_info:
        store.transition("filing-1", IngestState.READY)

    assert exc_info.value.document_id == "filing-1"
    assert store.get_job("filing-1").state is IngestState.PENDING
    rejected = store.list_audit()[-1]
    assert rejected.action == "transition_rejected"
    assert rejected.from_state is IngestState.PENDING
    assert rejected.to_state is IngestState.READY
    assert rejected.error_type == "IllegalTransitionError"


def test_failed_step_is_typed_and_audited() -> None:
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")
    store.transition("filing-1", IngestState.FETCHING)
    cause = ValueError("fixture source unavailable")

    with pytest.raises(IngestStepError) as exc_info:
        store.fail_step("filing-1", step="fetch", cause=cause)

    assert exc_info.value.document_id == "filing-1"
    assert exc_info.value.step == "fetch"
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert store.get_job("filing-1").state is IngestState.FAILED
    failed = store.list_audit()[-1]
    assert failed.action == "step_failed"
    assert failed.from_state is IngestState.FETCHING
    assert failed.to_state is IngestState.FAILED
    assert failed.step == "fetch"
    assert failed.error_type == "ValueError"
    assert failed.error_message == "fixture source unavailable"


def test_audit_cap_does_not_partially_apply_transition() -> None:
    store = InMemoryIngestStateStore(max_audit_entries=2)
    store.create_job("filing-1")
    store.transition("filing-1", IngestState.FETCHING)

    with pytest.raises(AuditOverflowError):
        store.transition("filing-1", IngestState.EXTRACTING)

    assert store.get_job("filing-1").state is IngestState.FETCHING
    assert len(store.list_audit()) == 2


def test_concurrent_duplicate_transition_reports_busy() -> None:
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")
    barrier = Barrier(2)

    def move_to_fetching() -> IngestState:
        barrier.wait()
        return store.transition("filing-1", IngestState.FETCHING)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(move_to_fetching) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except IngestBusyError as error:
                outcomes.append(error)

    assert sum(outcome is IngestState.FETCHING for outcome in outcomes) == 1
    assert sum(isinstance(outcome, IngestBusyError) for outcome in outcomes) == 1
    assert len(store.list_audit()) == 3
    assert store.list_audit()[-1].action == "transition_rejected"


def test_unknown_job_is_typed_and_does_not_create_audit() -> None:
    store = InMemoryIngestStateStore()

    with pytest.raises(UnknownJobError):
        store.get_job("missing")

    assert store.list_audit() == ()


def test_failed_job_can_retry_from_fetching() -> None:
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")
    store.transition("filing-1", IngestState.FETCHING)
    with pytest.raises(IngestStepError):
        store.fail_step("filing-1", step="fetch", cause=ValueError("down"))

    store.transition("filing-1", IngestState.FETCHING)
    assert store.get_job("filing-1").state is IngestState.FETCHING


@pytest.mark.parametrize(
    "blank", ["", "   ", "\t"], ids=["empty", "space", "tab"]
)
def test_blank_document_id_is_refused_by_every_public_entry(
    blank: str,
) -> None:
    """One identity rule for both stores: a non-blank document id.

    The memory store used to validate only ``create_job`` and let a blank
    id fall through to ``UnknownJobError`` elsewhere; the durable store
    refused it by name. Both stores now raise ``ValueError`` from every
    entry that reads a document id, so a caller never has to branch on
    which backend answered.
    """
    store = InMemoryIngestStateStore()

    entries = [
        lambda: store.create_job(blank),
        lambda: store.get_job(blank),
        lambda: store.supersede_job(blank),
        lambda: store.fail_step(blank, step="fetch", cause=RuntimeError("x")),
        lambda: store.list_audit(blank),
    ]
    for entry in entries:
        with pytest.raises(ValueError, match="document_id"):
            entry()


def test_blank_step_is_refused_before_the_job_is_read() -> None:
    """A missing step name is a caller bug, not a missing job."""
    store = InMemoryIngestStateStore()

    with pytest.raises(ValueError, match="step"):
        store.fail_step("filing-1", step="  ", cause=RuntimeError("x"))

    assert store.list_audit() == ()


def test_unknown_state_is_refused_before_any_job_lookup() -> None:
    """Neither store locks or reads a job for a state that cannot exist."""
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")

    with pytest.raises(InvalidStateError):
        store.transition("filing-1", "not-a-state")

    assert store.get_job("filing-1").state is IngestState.PENDING
    assert [row.action for row in store.list_audit("filing-1")] == [
        "job_created"
    ]


def test_busy_beats_illegality_for_a_stale_expected_state() -> None:
    """The shared guard order, identical in both stores.

    A transition whose expected state has moved on is a concurrency loss
    (busy), even when the requested target also happens to be illegal from
    the state the job really is in; an operator reading the audit sees the
    race, not a bogus rule violation.
    """
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")
    store.transition("filing-1", IngestState.FETCHING)

    with pytest.raises(IngestBusyError):
        store.transition(
            "filing-1",
            IngestState.READY,
            expected_state=IngestState.PENDING,
        )

    assert store.get_job("filing-1").state is IngestState.FETCHING
    rejected = store.list_audit()[-1]
    assert rejected.action == "transition_rejected"
    assert rejected.error_type == "IngestBusyError"


def test_rejected_fail_step_is_audited_as_a_refused_transition() -> None:
    """A refused failure shares the one rejection audit shape (READY job)."""
    store = InMemoryIngestStateStore()
    store.create_job("filing-1")
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        store.transition("filing-1", state)

    with pytest.raises(IngestStateError) as exc_info:
        store.fail_step("filing-1", step="push", cause=RuntimeError("late"))

    assert type(exc_info.value) is IllegalTransitionError
    rejected = store.list_audit()[-1]
    assert rejected.action == "transition_rejected"
    assert rejected.from_state is IngestState.READY
    assert rejected.to_state is IngestState.FAILED
    assert rejected.error_type == "IllegalTransitionError"
    assert store.get_job("filing-1").state is IngestState.READY
