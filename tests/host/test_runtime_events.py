"""T4: terminal, projection, sequence and outbox commit together.

One transaction allocates the thread sequence, bumps the projection revision,
writes the terminal result and the outbox intent. A constraint failure
anywhere in that transaction leaves none of it behind, and a snapshot either
proves continuity or returns ``reset_required``.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest

from fin.host.runtime import (
    CheckpointOwnershipLost,
    PostgresRuntimeStore,
    RuntimeCoordinator,
    RuntimeUnavailable,
)
from fin.host.runtime.schema import (
    OWNERSHIP_RUN_NOT_RUNNING,
    OWNERSHIP_THREAD_TOMBSTONED,
    RESET_CURSOR_AHEAD,
    RESET_EVENT_GAP,
    RESET_PROJECTION_MISSING,
    RESET_SEQUENCE_INCONSISTENT,
    RUN_COMPLETED,
    RUN_QUEUED,
)

import runtime_pg_support

CHILD_TERMINAL = """
import json, os
from fin.host.runtime import PostgresRuntimeStore
store = PostgresRuntimeStore(os.environ["FIN_TEST_DSN"])
terminal = store.read_terminal(os.environ["FIN_RUNTIME_RUN"])
snapshot = store.read_snapshot(thread_id=os.environ["FIN_RUNTIME_THREAD"])
print(json.dumps({
    "pid": os.getpid(),
    "terminal": None if terminal is None else {
        "run_id": terminal.run_id,
        "seq": terminal.seq,
        "technical_result": terminal.technical_result,
        "semantic_result": terminal.semantic_result,
        "payload": terminal.payload,
    },
    "snapshot": {
        "revision": snapshot.revision,
        "high_water_seq": snapshot.high_water_seq,
        "reset_required": snapshot.reset_required,
        "events": [event.seq for event in snapshot.events],
    },
}))
"""


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_evt")
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


def _accepted(coordinator: RuntimeCoordinator, thread: str):
    return coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "hello"},
    )


def _terminal_kwargs(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "technical_result": "done",
        "semantic_result": "answered",
        "payload": {"assistant_message": "the answer"},
        "evidence": {"citations": []},
        "projection": {"status": "done"},
        "outbox_payload": {"kind": "terminal"},
    }


@pytest.mark.runtime_pg
def test_an_event_commits_its_sequence_projection_and_optional_outbox(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)

    first = store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind="progress",
        payload={"step": 1},
        projection={"status": "running", "step": 1},
        outbox_payload={"kind": "progress"},
    )
    second = store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind="progress",
        payload={"step": 2},
        projection={"status": "running", "step": 2},
    )
    assert (first.seq, first.revision) == (1, 1)
    assert first.outbox_id is not None
    assert (second.seq, second.revision) == (2, 2)
    assert second.outbox_id is None

    pending = store.pending_outbox()
    assert [row.outbox_id for row in pending if row.thread_id == thread] == [
        first.outbox_id
    ]
    assert store.mark_outbox_published([first.outbox_id]) == 1
    assert store.mark_outbox_published([first.outbox_id]) == 0
    assert all(row.thread_id != thread for row in store.pending_outbox())


@pytest.mark.runtime_pg
def test_a_terminal_commit_is_one_atomic_projection_sequence_and_outbox(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None

    commit = store.commit_terminal(
        **_terminal_kwargs(record.run_id),  # type: ignore[arg-type]
        attempt_id=claimed.lease.attempt_id,
        attempt_generation=claimed.lease.attempt_generation,
    )
    assert commit.seq == 1 and commit.revision == 1
    assert commit.outbox_id is not None
    assert commit.replayed is False

    terminal = store.read_terminal(record.run_id)
    assert terminal is not None
    assert terminal.payload == {"assistant_message": "the answer"}
    assert terminal.seq == commit.seq

    snapshot = store.read_snapshot(thread_id=thread)
    assert snapshot.revision == 1
    assert snapshot.high_water_seq == 1
    assert [event.seq for event in snapshot.events] == [1]
    assert snapshot.events[0].kind == "terminal"
    assert snapshot.reset_required is False

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        run_state, operation_state = conn.execute(
            "SELECT r.state, o.state FROM fin_runtime_run r "
            "JOIN fin_runtime_operation o ON o.run_id = r.run_id "
            "WHERE r.run_id = %s",
            (record.run_id,),
        ).fetchone()
        reservation = conn.execute(
            "SELECT state FROM fin_runtime_thread_reservation "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert (run_state, operation_state) == (RUN_COMPLETED, RUN_COMPLETED)
    assert reservation == ("released",)

    replay = store.commit_terminal(**_terminal_kwargs(record.run_id))  # type: ignore[arg-type]
    assert replay.replayed is True
    assert (replay.seq, replay.revision) == (commit.seq, commit.revision)
    assert replay.outbox_id == commit.outbox_id

    with psycopg.connect(store.connection_url) as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM fin_runtime_terminal WHERE run_id = %s), "
            "(SELECT count(*) FROM fin_runtime_thread_event WHERE run_id = %s), "
            "(SELECT count(*) FROM fin_runtime_outbox WHERE thread_id = %s)",
            (record.run_id, record.run_id, thread),
        ).fetchone()
    assert counts == (1, 1, 1)


@pytest.mark.runtime_pg
def test_a_completed_run_replays_its_durable_result_and_cannot_be_reopened(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    operation = f"op-{uuid.uuid4()}"
    record = coordinator.accept(
        operation_id=operation,
        thread_id=thread,
        accepted_input={"message": "hello"},
    )
    commit = store.commit_terminal(**_terminal_kwargs(record.run_id))  # type: ignore[arg-type]

    replay = coordinator.replay(operation_id=operation)
    assert replay.state == RUN_COMPLETED
    assert replay.result is not None
    assert replay.result["technical_result"] == "done"
    assert replay.result["semantic_result"] == "answered"
    assert replay.result["seq"] == commit.seq

    late = coordinator.end(run_id=record.run_id, outcome="cancelled")
    assert late.state == RUN_COMPLETED
    assert late.reservation_released is False


@pytest.mark.runtime_pg
def test_end_run_never_rewrites_a_run_with_a_committed_terminal(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    store.commit_terminal(
        **_terminal_kwargs(record.run_id),  # type: ignore[arg-type]
        attempt_id=claimed.lease.attempt_id,
        attempt_generation=claimed.lease.attempt_generation,
    )

    late = coordinator.end(run_id=record.run_id, outcome="interrupted")
    assert late.terminal is True
    assert late.state == RUN_COMPLETED
    assert late.reservation_released is False
    assert late.attempts_released == 0

    again = coordinator.end(run_id=record.run_id, outcome="completed")
    assert again.terminal is True
    assert again.state == RUN_COMPLETED

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        run_state = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
        terminal = conn.execute(
            "SELECT technical_result, semantic_result FROM fin_runtime_terminal "
            "WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
        reservation = conn.execute(
            "SELECT state FROM fin_runtime_thread_reservation "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
    assert run_state == (RUN_COMPLETED,)
    assert terminal == ("done", "answered")
    assert reservation == ("released",)


@pytest.mark.runtime_pg
def test_a_failure_inside_the_terminal_transaction_leaves_no_partial_state(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind="progress",
        payload={"step": 1},
        projection={"status": "running"},
    )

    import psycopg

    # Occupy the outbox slot the next sequence allocation will use, so the
    # final INSERT of the terminal transaction violates a real constraint.
    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "INSERT INTO fin_runtime_outbox (outbox_id, thread_id, seq, payload) "
            "VALUES (%s, %s, 2, %s)",
            (f"blocker-{uuid.uuid4()}", thread, '{"blocker": true}'),
        )

    with pytest.raises(RuntimeUnavailable):
        store.commit_terminal(**_terminal_kwargs(record.run_id))  # type: ignore[arg-type]

    with psycopg.connect(store.connection_url) as conn:
        terminal = conn.execute(
            "SELECT 1 FROM fin_runtime_terminal WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
        projection = conn.execute(
            "SELECT revision, high_water_seq FROM fin_runtime_thread_projection "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
        run_state = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
        counter = conn.execute(
            "SELECT next_event_seq FROM fin_runtime_thread_counter "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
        second_event = conn.execute(
            "SELECT 1 FROM fin_runtime_thread_event "
            "WHERE thread_id = %s AND seq = 2",
            (thread,),
        ).fetchone()
        outbox_rows = conn.execute(
            "SELECT count(*) FROM fin_runtime_outbox "
            "WHERE thread_id = %s AND seq = 2",
            (thread,),
        ).fetchone()
    assert terminal is None
    assert projection == (1, 1)
    assert run_state == (RUN_QUEUED,)
    assert counter == (1,)
    assert second_event is None
    assert outbox_rows == (1,)


@pytest.mark.runtime_pg
def test_a_tombstoned_thread_rejects_an_event_and_writes_nothing(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    coordinator.tombstone_thread(thread_id=thread)

    with pytest.raises(CheckpointOwnershipLost) as refused:
        store.commit_event(
            thread_id=thread,
            run_id=record.run_id,
            kind="progress",
            payload={"step": 1},
            projection={"step": 1},
            outbox_payload={"kind": "progress"},
        )
    assert refused.value.reason == OWNERSHIP_THREAD_TOMBSTONED

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM fin_runtime_thread_event "
            "        WHERE thread_id = %s), "
            "(SELECT count(*) FROM fin_runtime_thread_projection "
            "        WHERE thread_id = %s), "
            "(SELECT count(*) FROM fin_runtime_outbox WHERE thread_id = %s)",
            (thread, thread, thread),
        ).fetchone()
    assert counts == (0, 0, 0)


@pytest.mark.runtime_pg
def test_a_tombstoned_thread_rejects_a_terminal_and_writes_nothing(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    coordinator.tombstone_thread(thread_id=thread)

    with pytest.raises(CheckpointOwnershipLost) as refused:
        store.commit_terminal(**_terminal_kwargs(record.run_id))  # type: ignore[arg-type]
    assert refused.value.reason == OWNERSHIP_THREAD_TOMBSTONED

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM fin_runtime_terminal WHERE run_id = %s), "
            "(SELECT count(*) FROM fin_runtime_thread_event WHERE thread_id = %s), "
            "(SELECT count(*) FROM fin_runtime_thread_projection "
            "        WHERE thread_id = %s), "
            "(SELECT count(*) FROM fin_runtime_outbox WHERE thread_id = %s)",
            (record.run_id, thread, thread, thread),
        ).fetchone()
        run_state = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
    assert counts == (0, 0, 0, 0)
    assert run_state == (RUN_QUEUED,)


@pytest.mark.runtime_pg
def test_a_snapshot_reads_a_consistent_revision_and_high_water(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    for step in range(1, 4):
        store.commit_event(
            thread_id=thread,
            run_id=record.run_id,
            kind="progress",
            payload={"step": step},
            projection={"step": step},
        )

    head = store.read_snapshot(thread_id=thread)
    assert head.revision == 3
    assert head.high_water_seq == 3
    assert [event.seq for event in head.events] == [1, 2, 3]
    assert head.projection == {"step": 3}
    assert head.reset_required is False

    resumed = store.read_snapshot(thread_id=thread, after_seq=1)
    assert [event.seq for event in resumed.events] == [2, 3]
    assert resumed.revision == 3
    assert resumed.reset_required is False


@pytest.mark.runtime_pg
def test_a_cursor_ahead_of_the_high_water_mark_requires_a_reset(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind="progress",
        payload={},
        projection={"step": 1},
    )
    snapshot = store.read_snapshot(thread_id=thread, after_seq=99)
    assert snapshot.reset_required is True
    assert snapshot.reset_reason == RESET_CURSOR_AHEAD


@pytest.mark.runtime_pg
def test_a_missing_committed_event_requires_a_reset(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    for step in range(1, 4):
        store.commit_event(
            thread_id=thread,
            run_id=record.run_id,
            kind="progress",
            payload={},
            projection={"step": step},
        )

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "DELETE FROM fin_runtime_thread_event WHERE thread_id = %s AND seq = 2",
            (thread,),
        )

    resumed = store.read_snapshot(thread_id=thread, after_seq=1)
    assert resumed.reset_required is True
    assert resumed.reset_reason == RESET_EVENT_GAP
    assert [event.seq for event in resumed.events] == [3]


@pytest.mark.runtime_pg
def test_a_missing_projection_requires_a_reset(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind="progress",
        payload={},
        projection={"step": 1},
    )

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "DELETE FROM fin_runtime_thread_projection WHERE thread_id = %s",
            (thread,),
        )
    snapshot = store.read_snapshot(thread_id=thread)
    assert snapshot.reset_required is True
    assert snapshot.reset_reason == RESET_PROJECTION_MISSING


@pytest.mark.runtime_pg
def test_an_inconsistent_sequence_counter_requires_a_reset(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind="progress",
        payload={},
        projection={"step": 1},
    )

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "UPDATE fin_runtime_thread_projection SET high_water_seq = 0 "
            "WHERE thread_id = %s",
            (thread,),
        )
    snapshot = store.read_snapshot(thread_id=thread)
    assert snapshot.reset_required is True
    assert snapshot.reset_reason == RESET_SEQUENCE_INCONSISTENT


@pytest.mark.runtime_pg
def test_a_terminal_commit_is_fenced_by_the_attempt_generation(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None

    with pytest.raises(CheckpointOwnershipLost):
        store.commit_terminal(
            **_terminal_kwargs(record.run_id),  # type: ignore[arg-type]
            attempt_id=claimed.lease.attempt_id,
            attempt_generation=claimed.lease.attempt_generation + 1,
        )

    coordinator.end(run_id=record.run_id, outcome="cancelled")
    with pytest.raises(CheckpointOwnershipLost) as cancelled:
        store.commit_terminal(**_terminal_kwargs(record.run_id))  # type: ignore[arg-type]
    assert cancelled.value.reason == OWNERSHIP_RUN_NOT_RUNNING


@pytest.mark.runtime_pg
def test_an_empty_thread_snapshot_is_consistent(
    store: PostgresRuntimeStore,
) -> None:
    snapshot = store.read_snapshot(thread_id=f"thread-{uuid.uuid4()}")
    assert snapshot.revision == 0
    assert snapshot.high_water_seq == 0
    assert snapshot.events == ()
    assert snapshot.reset_required is False


@pytest.mark.runtime_pg
def test_a_fresh_os_process_reads_the_terminal_and_the_cursor(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    commit = store.commit_terminal(**_terminal_kwargs(record.run_id))  # type: ignore[arg-type]

    child = runtime_pg_support.run_child(
        CHILD_TERMINAL,
        dsn=runtime_url,
        env={"FIN_RUNTIME_RUN": record.run_id, "FIN_RUNTIME_THREAD": thread},
    )
    assert child["pid"] != os.getpid()
    assert child["terminal"]["run_id"] == record.run_id
    assert child["terminal"]["seq"] == commit.seq
    assert child["terminal"]["payload"] == {"assistant_message": "the answer"}
    assert child["snapshot"] == {
        "revision": commit.revision,
        "high_water_seq": commit.high_water_seq,
        "reset_required": False,
        "events": [commit.seq],
    }


@pytest.mark.runtime_pg
def test_a_stale_attempt_replays_a_terminal_the_new_owner_committed(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    """A finished run answers a late end; only a live run needs fencing."""
    thread = f"thread-{uuid.uuid4()}"
    record = _accepted(coordinator, thread)
    first = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert first.lease is not None
    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "UPDATE fin_runtime_worker_attempt SET "
            "lease_expires_at = now() - interval '1 second' "
            "WHERE attempt_id = %s",
            (first.lease.attempt_id,),
        )
    second = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert second.lease is not None
    store.commit_terminal(
        **_terminal_kwargs(record.run_id),  # type: ignore[arg-type]
        attempt_id=second.lease.attempt_id,
        attempt_generation=second.lease.attempt_generation,
    )

    late = coordinator.end(
        run_id=record.run_id,
        outcome="abandoned",
        attempt_id=first.lease.attempt_id,
        attempt_generation=first.lease.attempt_generation,
    )
    assert late.terminal is True
    assert late.state == RUN_COMPLETED
