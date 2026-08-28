"""Behavioral tests for the ingest state machine and process audit trail."""

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
    IngestStepError,
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


def test_illegal_transition_raises_typed_error_and_records_audit() -> None:
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


def test_failed_step_raises_typed_error_and_records_failure_audit() -> None:
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


def test_audit_cap_raises_typed_overflow_without_partial_transition() -> None:
    store = InMemoryIngestStateStore(max_audit_entries=2)
    store.create_job("filing-1")
    store.transition("filing-1", IngestState.FETCHING)

    with pytest.raises(AuditOverflowError):
        store.transition("filing-1", IngestState.EXTRACTING)

    assert store.get_job("filing-1").state is IngestState.FETCHING
    assert len(store.list_audit()) == 2


def test_concurrent_duplicate_transition_reports_busy_and_keeps_audit() -> None:
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

    # One transition wins; the other observes the already-running step.
    assert sum(outcome is IngestState.FETCHING for outcome in outcomes) == 1
    assert sum(isinstance(outcome, IngestBusyError) for outcome in outcomes) == 1
    assert len(store.list_audit()) == 3
    rejected = store.list_audit()[-1]
    assert rejected.action == "transition_rejected"
    assert rejected.error_type == "IngestBusyError"


def test_unknown_job_is_typed_and_does_not_create_audit() -> None:
    store = InMemoryIngestStateStore()

    with pytest.raises(UnknownJobError):
        store.get_job("missing")

    assert store.list_audit() == ()
