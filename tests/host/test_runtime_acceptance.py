"""T1: durable acceptance, replay, conflicts and per-thread FIFO.

The in-process cases pin input classes that must never reach the store. The
live cases run against an isolated schema in the ``_test`` database and prove
that the operation, its position, its run and its notification intent are
durable before acceptance returns; a fresh OS process then reads the same
facts, and concurrent duplicates converge on one row.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Iterator

import pytest

from fin.host.runtime import (
    InvalidOperation,
    OperationConflict,
    PostgresRuntimeStore,
    QueueCapacityExceeded,
    RuntimeCoordinator,
    RuntimeUnavailable,
    payload_digest,
)
from fin.host.runtime.schema import (
    OPERATION_OPEN,
    RUN_COMPLETED,
    RUN_QUEUED,
    RUN_WAITING,
    RUNTIME_UNAVAILABLE,
)

import runtime_pg_support

#: A store URL that never connects; validation happens before any DB call.
UNREACHABLE = "postgresql://placeholder@127.0.0.1:1/placeholder_test"

CHILD_DIGEST = """
import json, os
from fin.host.runtime import payload_digest
print(json.dumps({"digest": payload_digest({"b": [1, 2], "a": "x"})}))
"""

CHILD_READ = """
import json, os
from fin.host.runtime import PostgresRuntimeStore
store = PostgresRuntimeStore(os.environ["FIN_TEST_DSN"])
record = store.read_operation(os.environ["FIN_RUNTIME_OPERATION"])
print(json.dumps({
    "pid": os.getpid(),
    "operation": None if record is None else {
        "operation_id": record.operation_id,
        "thread_id": record.thread_id,
        "run_id": record.run_id,
        "fifo_position": record.fifo_position,
        "state": record.state,
    },
}))
"""

CHILD_ACCEPT = """
import json, os
from fin.host.runtime import PostgresRuntimeStore, RuntimeCoordinator
store = PostgresRuntimeStore(os.environ["FIN_TEST_DSN"])
coordinator = RuntimeCoordinator(store, queue_capacity=8)
record = coordinator.accept(
    operation_id=os.environ["FIN_RUNTIME_OPERATION"],
    thread_id=os.environ["FIN_RUNTIME_THREAD"],
    accepted_input={"message": "hello"},
)
print(json.dumps({
    "pid": os.getpid(),
    "operation_id": record.operation_id,
    "thread_id": record.thread_id,
    "run_id": record.run_id,
    "fifo_position": record.fifo_position,
    "replay": record.replay,
    "state": record.state,
}))
"""


def _coordinator(capacity: int = 8) -> RuntimeCoordinator:
    return RuntimeCoordinator(
        PostgresRuntimeStore(UNREACHABLE), queue_capacity=capacity
    )


def _ids() -> tuple[str, str]:
    return f"op-{uuid.uuid4()}", f"thread-{uuid.uuid4()}"


# ----- hermetic input classes ---------------------------------------------


@pytest.mark.parametrize(
    ("operation_id", "reason"),
    [
        (None, "missing"),
        ("", "empty"),
        ("   ", "empty"),
        (123, "malformed"),
        (b"bytes", "malformed"),
        ("has\x00control", "malformed"),
        (" padded", "malformed"),
        ("trailing ", "malformed"),
        ("a" * 201, "malformed"),
    ],
)
def test_operation_identity_classes_are_typed(operation_id: object, reason: str) -> None:
    with pytest.raises(InvalidOperation) as error:
        _coordinator().accept(
            operation_id=operation_id,  # type: ignore[arg-type]
            thread_id="thread-1",
            accepted_input={"message": "hi"},
        )
    assert error.value.code == "invalid_operation"
    assert error.value.field == "operation_id"
    assert error.value.reason == reason


@pytest.mark.parametrize(
    ("thread_id", "reason"),
    [
        (None, "missing"),
        ("", "empty"),
        ("\t", "empty"),
        (3.5, "malformed"),
        ("a" * 201, "malformed"),
    ],
)
def test_thread_identity_classes_are_typed(thread_id: object, reason: str) -> None:
    with pytest.raises(InvalidOperation) as error:
        _coordinator().accept(
            operation_id="op-1",
            thread_id=thread_id,  # type: ignore[arg-type]
            accepted_input={"message": "hi"},
        )
    assert error.value.field == "thread_id"
    assert error.value.reason == reason


@pytest.mark.parametrize(
    ("accepted_input", "reason"),
    [
        (None, "missing"),
        ([], "not_an_object"),
        ("text", "not_an_object"),
        (7, "not_an_object"),
        ({"value": float("nan")}, "malformed"),
        ({"value": object()}, "malformed"),
        ({1: "non-string key"}, "malformed"),
    ],
)
def test_payload_classes_are_typed(accepted_input: object, reason: str) -> None:
    with pytest.raises(InvalidOperation) as error:
        _coordinator().accept(
            operation_id="op-1",
            thread_id="thread-1",
            accepted_input=accepted_input,  # type: ignore[arg-type]
        )
    assert error.value.field == "accepted_input"
    assert error.value.reason == reason


def test_an_oversized_payload_is_refused_before_any_write() -> None:
    coordinator = RuntimeCoordinator(
        PostgresRuntimeStore(UNREACHABLE), max_payload_bytes=8
    )
    with pytest.raises(InvalidOperation) as error:
        coordinator.accept(
            operation_id="op-1",
            thread_id="thread-1",
            accepted_input={"message": "too long"},
        )
    assert error.value.reason == "too_large"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"queue_capacity": 0},
        {"lease_seconds": 0},
        {"max_payload_bytes": 0},
    ],
)
def test_impossible_configuration_is_a_construction_error(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        RuntimeCoordinator(PostgresRuntimeStore(UNREACHABLE), **kwargs)


def test_payload_digest_is_order_independent_and_cross_process() -> None:
    first = payload_digest({"b": [1, 2], "a": "x"})
    second = payload_digest({"a": "x", "b": [1, 2]})
    assert first == second
    child = runtime_pg_support.run_child(
        CHILD_DIGEST, dsn="postgresql://unused@127.0.0.1:1/unused_test"
    )
    assert child["digest"] == first


# ----- live: commit before acceptance returns -----------------------------


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_acc")
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


@pytest.mark.runtime_pg
def test_accept_commits_operation_position_run_and_intent(
    runtime_url: str, coordinator: RuntimeCoordinator
) -> None:
    operation, thread = _ids()
    record = coordinator.accept(
        operation_id=operation,
        thread_id=thread,
        accepted_input={"message": "hello"},
        notification_intent={"wake": "run"},
    )

    fresh = PostgresRuntimeStore(runtime_url).read_operation(operation)
    assert fresh is not None
    assert fresh.run_id == record.run_id
    assert fresh.thread_id == thread
    assert fresh.fifo_position == 1
    assert fresh.accepted_input == {"message": "hello"}
    assert fresh.notification_intent == {"wake": "run"}
    assert fresh.state == OPERATION_OPEN

    import psycopg

    with psycopg.connect(runtime_url) as conn:
        run_state = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
    assert run_state == (RUN_QUEUED,)


@pytest.mark.runtime_pg
def test_same_key_and_payload_returns_the_durable_prior_result(
    coordinator: RuntimeCoordinator,
) -> None:
    operation, thread = _ids()
    first = coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "one"}
    )
    second = coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "one"}
    )
    replay = coordinator.replay(operation_id=operation)

    assert first.replay is False
    assert second.replay is True
    assert replay.replay is True
    assert second.run_id == first.run_id
    assert second.fifo_position == first.fifo_position
    assert replay.run_id == first.run_id
    assert replay.accepted_input == {"message": "one"}

    import psycopg

    with psycopg.connect(coordinator.store.connection_url) as conn:
        runs, operations = conn.execute(
            "SELECT (SELECT count(*) FROM fin_runtime_run WHERE thread_id = %s), "
            "(SELECT count(*) FROM fin_runtime_operation WHERE thread_id = %s)",
            (thread, thread),
        ).fetchone()
    assert (runs, operations) == (1, 1)


@pytest.mark.runtime_pg
def test_same_key_with_a_different_payload_conflicts(
    coordinator: RuntimeCoordinator,
) -> None:
    operation, thread = _ids()
    first = coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "one"}
    )
    with pytest.raises(OperationConflict) as error:
        coordinator.accept(
            operation_id=operation,
            thread_id=thread,
            accepted_input={"message": "two"},
        )
    assert error.value.operation_id == operation
    assert coordinator.replay(operation_id=operation).run_id == first.run_id

    import psycopg

    with psycopg.connect(coordinator.store.connection_url) as conn:
        operations = conn.execute(
            "SELECT count(*) FROM fin_runtime_operation WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert operations == (1,)


@pytest.mark.runtime_pg
def test_a_duplicate_is_recognized_before_capacity_rejection(
    store: PostgresRuntimeStore,
) -> None:
    coordinator = RuntimeCoordinator(store, queue_capacity=1)
    operation, thread = _ids()
    first = coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "one"}
    )
    again = coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "one"}
    )
    assert again.replay is True and again.run_id == first.run_id

    other, _ = _ids()
    with pytest.raises(QueueCapacityExceeded):
        coordinator.accept(
            operation_id=other, thread_id=thread, accepted_input={"message": "two"}
        )
    assert store.read_operation(other) is None


@pytest.mark.runtime_pg
def test_queue_capacity_boundary_is_exact(store: PostgresRuntimeStore) -> None:
    coordinator = RuntimeCoordinator(store, queue_capacity=2)
    thread = f"thread-{uuid.uuid4()}"
    accepted = [
        coordinator.accept(
            operation_id=f"op-{uuid.uuid4()}",
            thread_id=thread,
            accepted_input={"message": index},
        )
        for index in range(2)
    ]
    with pytest.raises(QueueCapacityExceeded) as error:
        coordinator.accept(
            operation_id=f"op-{uuid.uuid4()}",
            thread_id=thread,
            accepted_input={"message": 3},
        )
    assert error.value.capacity == 2
    assert [record.fifo_position for record in accepted] == [1, 2]

    coordinator.end(run_id=accepted[0].run_id, outcome="cancelled")
    later = coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": 4},
    )
    assert later.fifo_position == 3


def _commit_terminal(store: PostgresRuntimeStore, run_id: str, *, attempt=None):
    return store.commit_terminal(
        run_id=run_id,
        technical_result="done",
        semantic_result="answered",
        payload={"assistant_message": "answer"},
        evidence={"citations": []},
        projection={"status": "done"},
        outbox_payload={"kind": "terminal"},
        attempt_id=attempt.attempt_id if attempt is not None else None,
        attempt_generation=(
            attempt.attempt_generation if attempt is not None else None
        ),
    )


@pytest.mark.runtime_pg
def test_a_continuation_operation_shares_the_waiting_run(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    first = coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "start"},
    )
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    coordinator.wait(attempt_id=claimed.lease.attempt_id)

    continuation_id = f"op-{uuid.uuid4()}"
    continuation = coordinator.accept(
        operation_id=continuation_id,
        thread_id=thread,
        accepted_input={"message": "reply"},
        run_id=first.run_id,
    )
    assert continuation.run_id == first.run_id
    assert continuation.fifo_position == first.fifo_position + 1
    assert continuation.state == OPERATION_OPEN

    replay = coordinator.accept(
        operation_id=continuation_id,
        thread_id=thread,
        accepted_input={"message": "reply"},
        run_id=first.run_id,
    )
    assert replay.replay is True
    assert replay.run_id == first.run_id
    assert replay.fifo_position == continuation.fifo_position

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        run = conn.execute(
            "SELECT state, attempt_generation FROM fin_runtime_run "
            "WHERE run_id = %s",
            (first.run_id,),
        ).fetchone()
        operations = conn.execute(
            "SELECT count(*) FROM fin_runtime_operation WHERE run_id = %s",
            (first.run_id,),
        ).fetchone()
        runs = conn.execute(
            "SELECT count(*) FROM fin_runtime_run WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert run == (RUN_WAITING, 1)
    assert operations == (2,)
    assert runs == (1,)


@pytest.mark.runtime_pg
def test_a_continuation_of_a_completed_run_is_refused(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    operation = f"op-{uuid.uuid4()}"
    record = coordinator.accept(
        operation_id=operation,
        thread_id=thread,
        accepted_input={"message": "start"},
    )
    _commit_terminal(store, record.run_id)

    late = f"op-{uuid.uuid4()}"
    with pytest.raises(InvalidOperation) as error:
        coordinator.accept(
            operation_id=late,
            thread_id=thread,
            accepted_input={"message": "late"},
            run_id=record.run_id,
        )
    assert error.value.field == "run_id"
    assert error.value.reason == "terminal"
    assert store.read_operation(late) is None

    replay = coordinator.replay(operation_id=operation)
    assert replay.state == RUN_COMPLETED


@pytest.mark.runtime_pg
def test_a_run_id_from_another_thread_is_refused(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "start"},
    )
    with pytest.raises(InvalidOperation) as error:
        coordinator.accept(
            operation_id=f"op-{uuid.uuid4()}",
            thread_id=f"thread-{uuid.uuid4()}",
            accepted_input={"message": "elsewhere"},
            run_id=record.run_id,
        )
    assert error.value.field == "run_id"
    assert error.value.reason == "thread_mismatch"


@pytest.mark.runtime_pg
def test_two_operations_for_one_run_end_together(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    first = coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "start"},
    )
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    coordinator.wait(attempt_id=claimed.lease.attempt_id)
    second = coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "reply"},
        run_id=first.run_id,
    )

    resumed = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert resumed.claimed is True
    assert resumed.lease is not None
    assert resumed.lease.run_id == first.run_id
    _commit_terminal(store, first.run_id, attempt=resumed.lease)

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        rows = conn.execute(
            "SELECT operation_id, state, result FROM fin_runtime_operation "
            "WHERE run_id = %s ORDER BY fifo_position ASC",
            (first.run_id,),
        ).fetchall()
        run_state = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (first.run_id,),
        ).fetchone()
    assert [row[0] for row in rows] == [first.operation_id, second.operation_id]
    assert {row[1] for row in rows} == {RUN_COMPLETED}
    assert rows[0][2] is not None
    assert rows[0][2] == rows[1][2]
    assert run_state == (RUN_COMPLETED,)


@pytest.mark.runtime_pg
def test_acceptance_fifo_is_independent_of_event_sequence(
    store: PostgresRuntimeStore,
) -> None:
    coordinator = RuntimeCoordinator(store)
    thread = f"thread-{uuid.uuid4()}"
    records = [
        coordinator.accept(
            operation_id=f"op-{uuid.uuid4()}",
            thread_id=thread,
            accepted_input={"message": index},
        )
        for index in range(3)
    ]
    assert [record.fifo_position for record in records] == [1, 2, 3]

    event = store.commit_event(
        thread_id=thread,
        run_id=records[0].run_id,
        kind="accepted",
        payload={"message": "queued"},
        projection={"status": "queued"},
        outbox_payload={"kind": "wake"},
    )
    assert event.seq == 1

    later = coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": 9},
    )
    assert later.fifo_position == 4

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        counters = conn.execute(
            "SELECT next_fifo, next_event_seq FROM fin_runtime_thread_counter "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert counters == (5, 1)


@pytest.mark.runtime_pg
def test_concurrent_duplicate_intents_converge_on_one_run(
    coordinator: RuntimeCoordinator,
) -> None:
    operation, thread = _ids()
    workers = 8
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    results: list[object] = []

    def submit() -> None:
        barrier.wait()
        try:
            record = coordinator.accept(
                operation_id=operation,
                thread_id=thread,
                accepted_input={"message": "same"},
            )
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            with lock:
                results.append(exc)
            return
        with lock:
            results.append(record)

    threads = [threading.Thread(target=submit) for _ in range(workers)]
    for thread_handle in threads:
        thread_handle.start()
    for thread_handle in threads:
        thread_handle.join(timeout=60.0)

    assert len(results) == workers
    assert all(not isinstance(item, Exception) for item in results)
    records = [item for item in results if not isinstance(item, Exception)]
    assert sum(1 for record in records if not record.replay) == 1
    assert len({record.run_id for record in records}) == 1
    assert len({record.fifo_position for record in records}) == 1

    import psycopg

    with psycopg.connect(coordinator.store.connection_url) as conn:
        runs = conn.execute(
            "SELECT count(*) FROM fin_runtime_run WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert runs == (1,)


@pytest.mark.runtime_pg
def test_concurrent_conflicting_payloads_have_one_winner(
    coordinator: RuntimeCoordinator,
) -> None:
    operation, thread = _ids()
    workers = 8
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    payloads = [{"message": "A"}, {"message": "B"}]
    winners: list[dict[str, object]] = []
    conflicts: list[OperationConflict] = []

    def submit(index: int) -> None:
        payload = payloads[index % 2]
        barrier.wait()
        try:
            record = coordinator.accept(
                operation_id=operation, thread_id=thread, accepted_input=payload
            )
        except OperationConflict as exc:
            with lock:
                conflicts.append(exc)
            return
        with lock:
            winners.append({"payload": payload, "record": record})

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(workers)]
    for thread_handle in threads:
        thread_handle.start()
    for thread_handle in threads:
        thread_handle.join(timeout=60.0)

    assert winners
    winning_payload = winners[0]["payload"]
    assert all(winner["payload"] == winning_payload for winner in winners)
    assert len({winner["record"].run_id for winner in winners}) == 1  # type: ignore[attr-defined]
    assert len(winners) + len(conflicts) == workers
    assert len(conflicts) == workers // 2

    import psycopg

    with psycopg.connect(coordinator.store.connection_url) as conn:
        runs = conn.execute(
            "SELECT count(*) FROM fin_runtime_run WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert runs == (1,)


@pytest.mark.runtime_pg
def test_the_same_key_in_another_thread_conflicts(
    coordinator: RuntimeCoordinator,
) -> None:
    operation, thread = _ids()
    coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "one"}
    )
    with pytest.raises(OperationConflict):
        coordinator.accept(
            operation_id=operation,
            thread_id=f"thread-{uuid.uuid4()}",
            accepted_input={"message": "one"},
        )


@pytest.mark.runtime_pg
def test_a_replay_keeps_the_first_notification_intent(
    coordinator: RuntimeCoordinator,
) -> None:
    operation, thread = _ids()
    first = coordinator.accept(
        operation_id=operation,
        thread_id=thread,
        accepted_input={"message": "one"},
        notification_intent={"wake": "first"},
    )
    again = coordinator.accept(
        operation_id=operation,
        thread_id=thread,
        accepted_input={"message": "one"},
        notification_intent={"wake": "second"},
    )
    assert again.replay is True
    assert again.notification_intent == {"wake": "first"}
    assert again.run_id == first.run_id


@pytest.mark.runtime_pg
def test_a_database_failure_is_typed_and_never_accepted(
    runtime_url: str,
) -> None:
    from psycopg.conninfo import make_conninfo

    absent = make_conninfo(
        runtime_url, dbname="fin_runtime_absent_test", connect_timeout=2
    )
    broken = PostgresRuntimeStore(absent)
    with pytest.raises(RuntimeUnavailable) as error:
        broken.setup()
    assert error.value.code == RUNTIME_UNAVAILABLE


@pytest.mark.runtime_pg
def test_a_fresh_os_process_reads_the_same_operation_and_position(
    runtime_url: str, coordinator: RuntimeCoordinator
) -> None:
    operation, thread = _ids()
    record = coordinator.accept(
        operation_id=operation,
        thread_id=thread,
        accepted_input={"message": "hello"},
        notification_intent={"wake": "run"},
    )
    child = runtime_pg_support.run_child(
        CHILD_READ,
        dsn=runtime_url,
        env={"FIN_RUNTIME_OPERATION": operation},
    )
    assert child["pid"] != os.getpid()
    assert child["operation"] == {
        "operation_id": operation,
        "thread_id": thread,
        "run_id": record.run_id,
        "fifo_position": record.fifo_position,
        "state": OPERATION_OPEN,
    }


@pytest.mark.runtime_pg
def test_a_lost_response_replays_across_os_processes(
    runtime_url: str, coordinator: RuntimeCoordinator
) -> None:
    operation, thread = _ids()
    record = coordinator.accept(
        operation_id=operation, thread_id=thread, accepted_input={"message": "hello"}
    )
    child = runtime_pg_support.run_child(
        CHILD_ACCEPT,
        dsn=runtime_url,
        env={
            "FIN_RUNTIME_OPERATION": operation,
            "FIN_RUNTIME_THREAD": thread,
        },
    )
    assert child["pid"] != os.getpid()
    assert child["replay"] is True
    assert child["run_id"] == record.run_id
    assert child["fifo_position"] == record.fifo_position
    assert child["state"] == OPERATION_OPEN
