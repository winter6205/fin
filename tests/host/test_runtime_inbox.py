"""T5: parser completion is a pending inbox entry, never readiness.

The parser service owns job state and results. This suite proves that fin
records a completion notice durably as pending work before anything
acknowledges it, survives a redelivery and a process restart, and only
becomes ready for adoption after the parser authority is reconciled.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest

from fin.host.runtime import (
    PostgresRuntimeStore,
    RuntimeCoordinator,
    UnknownOperation,
)
from fin.host.runtime.schema import (
    PARSER_AUTHORITY_SUCCEEDED,
    PARSER_INBOX_PENDING,
    PARSER_INBOX_RECONCILED,
)

import runtime_pg_support

CHILD_PENDING = """
import json, os
from fin.host.runtime import PostgresRuntimeStore
store = PostgresRuntimeStore(os.environ["FIN_TEST_DSN"])
pending = store.pending_parser_completions(limit=50)
print(json.dumps({
    "pid": os.getpid(),
    "pending": [
        {
            "inbox_id": record.inbox_id,
            "job_id": record.job_id,
            "state": record.state,
            "ready": record.ready_for_adoption,
        }
        for record in pending
    ],
}))
"""


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_inbox")
    try:
        yield runtime_pg_support.scoped_url(dsn, schema)
    finally:
        runtime_pg_support.drop_schema(dsn, schema)


@pytest.fixture(scope="module")
def store(runtime_url: str) -> PostgresRuntimeStore:
    runtime_store = PostgresRuntimeStore(runtime_url)
    runtime_store.setup()
    return runtime_store


@pytest.fixture()
def coordinator(store: PostgresRuntimeStore) -> RuntimeCoordinator:
    return RuntimeCoordinator(store)


def _notification() -> dict[str, str]:
    return {"event": "parser.completed", "job_id": f"job-{uuid.uuid4()}"}


@pytest.mark.runtime_pg
def test_a_completion_is_recorded_as_pending_before_anything_acks(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    job = f"job-{uuid.uuid4()}"
    record = coordinator.record_parser_completion(
        job_id=job,
        attachment_id=f"att-{uuid.uuid4()}",
        corpus_id=f"corpus-{uuid.uuid4()}",
        notification={"event": "parser.completed"},
    )
    assert record.state == PARSER_INBOX_PENDING
    assert record.reconciled_at is None
    assert record.authority_state is None
    assert record.ready_for_adoption is False

    pending = coordinator.pending_parser_completions()
    assert record.inbox_id in {entry.inbox_id for entry in pending}


@pytest.mark.runtime_pg
def test_a_redelivery_is_safe_and_keeps_the_first_notice(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    job = f"job-{uuid.uuid4()}"
    attachment = f"att-{uuid.uuid4()}"
    corpus = f"corpus-{uuid.uuid4()}"
    first = coordinator.record_parser_completion(
        job_id=job,
        attachment_id=attachment,
        corpus_id=corpus,
        notification={"event": "parser.completed", "attempt": "1"},
    )
    second = coordinator.record_parser_completion(
        job_id=job,
        attachment_id=attachment,
        corpus_id=corpus,
        notification={"event": "parser.completed", "attempt": "2"},
    )
    assert second.replay is True
    assert second.inbox_id == first.inbox_id
    assert second.notification == {"event": "parser.completed", "attempt": "1"}

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM fin_runtime_parser_inbox "
            "WHERE job_id = %s AND attachment_id = %s",
            (job, attachment),
        ).fetchone()
    assert rows == (1,)


@pytest.mark.runtime_pg
def test_a_notification_alone_never_marks_the_entry_ready(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    record = coordinator.record_parser_completion(
        job_id=f"job-{uuid.uuid4()}",
        attachment_id=f"att-{uuid.uuid4()}",
        corpus_id=f"corpus-{uuid.uuid4()}",
        notification={"event": "parser.completed"},
    )
    assert record.ready_for_adoption is False

    failed = coordinator.reconcile_parser_completion(
        inbox_id=record.inbox_id, authority_state="failed"
    )
    assert failed.state == PARSER_INBOX_RECONCILED
    assert failed.authority_state == "failed"
    assert failed.ready_for_adoption is False

    other = coordinator.record_parser_completion(
        job_id=f"job-{uuid.uuid4()}",
        attachment_id=f"att-{uuid.uuid4()}",
        corpus_id=f"corpus-{uuid.uuid4()}",
        notification={"event": "parser.completed"},
    )
    succeeded = coordinator.reconcile_parser_completion(
        inbox_id=other.inbox_id, authority_state=PARSER_AUTHORITY_SUCCEEDED
    )
    assert succeeded.ready_for_adoption is True


@pytest.mark.runtime_pg
def test_reconciling_twice_keeps_the_first_authority_state(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    record = coordinator.record_parser_completion(
        job_id=f"job-{uuid.uuid4()}",
        attachment_id=f"att-{uuid.uuid4()}",
        corpus_id=f"corpus-{uuid.uuid4()}",
        notification={"event": "parser.completed"},
    )
    first = coordinator.reconcile_parser_completion(
        inbox_id=record.inbox_id, authority_state=PARSER_AUTHORITY_SUCCEEDED
    )
    again = coordinator.reconcile_parser_completion(
        inbox_id=record.inbox_id, authority_state="failed"
    )
    assert first.authority_state == PARSER_AUTHORITY_SUCCEEDED
    assert again.replay is True
    assert again.authority_state == PARSER_AUTHORITY_SUCCEEDED


@pytest.mark.runtime_pg
def test_a_reconciled_entry_leaves_the_pending_queue(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    record = coordinator.record_parser_completion(
        job_id=f"job-{uuid.uuid4()}",
        attachment_id=f"att-{uuid.uuid4()}",
        corpus_id=f"corpus-{uuid.uuid4()}",
        notification={"event": "parser.completed"},
    )
    coordinator.reconcile_parser_completion(
        inbox_id=record.inbox_id, authority_state=PARSER_AUTHORITY_SUCCEEDED
    )
    pending = coordinator.pending_parser_completions()
    assert record.inbox_id not in {entry.inbox_id for entry in pending}


@pytest.mark.runtime_pg
def test_an_unknown_inbox_entry_is_typed(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    with pytest.raises(UnknownOperation) as error:
        coordinator.reconcile_parser_completion(
            inbox_id=f"inbox-{uuid.uuid4()}", authority_state="succeeded"
        )
    assert error.value.kind == "parser inbox entry"


@pytest.mark.runtime_pg
def test_a_fresh_os_process_still_sees_the_pending_entry(
    runtime_url: str, coordinator: RuntimeCoordinator
) -> None:
    record = coordinator.record_parser_completion(
        job_id=f"job-{uuid.uuid4()}",
        attachment_id=f"att-{uuid.uuid4()}",
        corpus_id=f"corpus-{uuid.uuid4()}",
        notification={"event": "parser.completed"},
    )
    child = runtime_pg_support.run_child(CHILD_PENDING, dsn=runtime_url)
    assert child["pid"] != os.getpid()
    entries = {entry["inbox_id"]: entry for entry in child["pending"]}
    assert record.inbox_id in entries
    assert entries[record.inbox_id]["state"] == PARSER_INBOX_PENDING
    assert entries[record.inbox_id]["ready"] is False
