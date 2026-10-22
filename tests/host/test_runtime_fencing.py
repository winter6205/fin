"""P1 T3: every synchronous checkpoint mutation is fenced in its transaction.

The live cases run against an isolated schema in the ``_test`` database and
open the real ``PostgresSaver`` connection the same way production does. A
stale worker carries its runtime identity in ``config["configurable"]``; a
takeover, cancellation or tombstone must make its ``put`` / ``put_writes``
raise before any checkpoint row commits. Maintenance callers (finalize,
compact, CLI) carry no identity and are allowed only while the thread has no
live reservation and no running run.

Ordering is explicit (a lease row update, then a claim); no case infers a
race from a sleep. Persistence claims are read back by a fresh OS process.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Iterator

import pytest

from fin.host.runtime import (
    CheckpointOwnershipLost,
    PostgresRuntimeStore,
    RuntimeCoordinator,
)
from fin.host.runtime.schema import (
    OWNERSHIP_ATTEMPT_GENERATION,
    OWNERSHIP_THREAD_CANCELLED,
    OWNERSHIP_THREAD_TOMBSTONED,
)

import runtime_pg_support

pytestmark = pytest.mark.runtime_pg

IDENTITY_INCOMPLETE = "runtime_identity_incomplete"
IDENTITY_MISSING = "runtime_identity_missing"

CHILD_THREAD_READBACK = """
import json, os
import psycopg
with psycopg.connect(os.environ["FIN_TEST_DSN"]) as conn:
    state = conn.execute(
        "SELECT tombstoned, cancellation_reason FROM fin_runtime_thread_state "
        "WHERE thread_id = %s",
        (os.environ["FIN_RUNTIME_THREAD"],),
    ).fetchone()
    checkpoints = conn.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s",
        (os.environ["FIN_RUNTIME_THREAD"],),
    ).fetchone()[0]
    writes = conn.execute(
        "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s",
        (os.environ["FIN_RUNTIME_THREAD"],),
    ).fetchone()[0]
print(json.dumps({
    "pid": os.getpid(),
    "thread_state": state,
    "checkpoints": int(checkpoints),
    "writes": int(writes),
}))
"""


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_fence")
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
    return RuntimeCoordinator(store, queue_capacity=8, lease_seconds=30.0)


def _thread() -> str:
    return f"thread-{uuid.uuid4()}"


def _claim(
    coordinator: RuntimeCoordinator,
    *,
    thread_id: str | None = None,
):
    thread = thread_id or _thread()
    coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "fence me"},
    )
    outcome = coordinator.claim(thread_id=thread, worker_id="worker-a")
    assert outcome.claimed is True, outcome.reason
    assert outcome.lease is not None
    return outcome.lease


def _config(
    lease,
    *,
    checkpoint_id: str | None = None,
    identity: bool = True,
) -> dict[str, object]:
    configurable: dict[str, object] = {
        "thread_id": lease.thread_id,
        "checkpoint_ns": "",
    }
    if identity:
        configurable.update(
            {
                "runtime_run_id": lease.run_id,
                "runtime_attempt_id": lease.attempt_id,
                "runtime_attempt_generation": lease.attempt_generation,
                "thread_generation": lease.thread_generation,
            }
        )
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _maintenance_config(thread_id: str) -> dict[str, object]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint(checkpoint_id: str) -> dict[str, object]:
    return {
        "v": 4,
        "ts": "2026-10-19T00:00:00+00:00",
        "id": checkpoint_id,
        "channel_values": {"messages": "hello"},
    }


def _metadata() -> dict[str, object]:
    return {"source": "input", "step": 1}


def _put(saver, config: dict[str, object]) -> None:
    saver.put(config, _checkpoint(str(uuid.uuid4())), _metadata(), {})


def _expire(store: PostgresRuntimeStore, attempt_id: str) -> None:
    """Expire one lease by an explicit row update, never by sleeping."""
    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "UPDATE fin_runtime_worker_attempt SET "
            "lease_expires_at = now() - interval '1 second' "
            "WHERE attempt_id = %s",
            (attempt_id,),
        )


def _counts(url: str, thread_id: str) -> tuple[int, int]:
    """Read the checkpoint row counts over a SECOND connection."""
    import psycopg

    with psycopg.connect(url) as conn:
        checkpoints = conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,)
        ).fetchone()[0]
        writes = conn.execute(
            "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
    return int(checkpoints), int(writes)


@contextmanager
def _fenced_saver(runtime_url: str, store: PostgresRuntimeStore) -> Iterator[object]:
    """Open the saver exactly as production does, then fence it."""
    from langgraph.checkpoint.postgres import PostgresSaver

    from fin.host.compile import bounded_checkpoint_conninfo
    from fin.host.runtime.fencing import fence_postgres_saver

    with PostgresSaver.from_conn_string(
        bounded_checkpoint_conninfo(runtime_url)
    ) as saver:
        saver.setup()
        yield fence_postgres_saver(saver, store)


# ----- current owner still writes -----------------------------------------


def test_current_owner_put_and_put_writes_pass(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    checkpoint_id = str(uuid.uuid4())
    with _fenced_saver(runtime_url, store) as saver:
        saver.put(_config(lease), _checkpoint(checkpoint_id), _metadata(), {})
        saver.put_writes(
            _config(lease, checkpoint_id=checkpoint_id),
            [("messages", "write")],
            "task-1",
        )
        assert saver.get_tuple(_config(lease)) is not None

    assert _counts(runtime_url, lease.thread_id) == (1, 1)


# ----- takeover -----------------------------------------------------------


def test_takeover_rejects_the_stale_attempt_without_committing(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    with _fenced_saver(runtime_url, store) as saver:
        _put(saver, _config(lease))
        before = _counts(runtime_url, lease.thread_id)
        assert before == (1, 0)

        # Explicit ordering: the old lease is expired by a row update, then
        # the takeover claim mints generation + 1.
        _expire(store, lease.attempt_id)
        takeover = coordinator.claim(
            thread_id=lease.thread_id, worker_id="worker-b", run_id=lease.run_id
        )
        assert takeover.claimed is True, takeover.reason
        assert takeover.lease is not None
        assert takeover.lease.attempt_generation == lease.attempt_generation + 1

        with pytest.raises(CheckpointOwnershipLost) as stale_put:
            _put(saver, _config(lease))
        assert stale_put.value.reason == OWNERSHIP_ATTEMPT_GENERATION
        assert _counts(runtime_url, lease.thread_id) == before

        with pytest.raises(CheckpointOwnershipLost):
            saver.put_writes(
                _config(lease, checkpoint_id=str(uuid.uuid4())),
                [("messages", "late")],
                "task-late",
            )
        assert _counts(runtime_url, lease.thread_id) == before

        # The new generation is the current owner and still writes.
        _put(saver, _config(takeover.lease))
        assert _counts(runtime_url, lease.thread_id) == (before[0] + 1, 0)


# ----- cancellation and deletion ------------------------------------------


def test_cancelled_thread_rejects_the_claimed_attempt(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    coordinator.mark_thread_cancelled(
        thread_id=lease.thread_id, reason="user_stop"
    )
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            _put(saver, _config(lease))
        assert rejected.value.reason == OWNERSHIP_THREAD_CANCELLED
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


def test_a_run_scoped_cancellation_leaves_the_thread_usable(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    cancelled = _claim(coordinator, thread_id=thread)
    coordinator.mark_thread_cancelled(
        thread_id=thread, reason="user_stop", run_id=cancelled.run_id
    )

    # The cancelled run's worker is fenced out of the thread for good.
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            _put(saver, _config(cancelled))
        assert rejected.value.reason == OWNERSHIP_THREAD_CANCELLED

    # A different run on the same thread may be accepted, claimed and write.
    next_run = f"run-{uuid.uuid4()}"
    coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "the next turn"},
        run_id=next_run,
    )
    claim = coordinator.claim(
        thread_id=thread, worker_id="worker-b", run_id=next_run
    )
    assert claim.claimed is True, claim.reason
    assert claim.lease is not None
    with _fenced_saver(runtime_url, store) as saver:
        _put(saver, _config(claim.lease))
    assert _counts(runtime_url, thread) == (1, 0)

    # Recovery reports the cancellation to the run it names, and only it.
    cancelled_snapshot = store.read_recovery_snapshot(cancelled.run_id)
    assert cancelled_snapshot is not None
    assert cancelled_snapshot.cancellation_reason == "user_stop"
    assert cancelled_snapshot.cancelled_run_id == cancelled.run_id
    next_snapshot = store.read_recovery_snapshot(next_run)
    assert next_snapshot is not None
    assert next_snapshot.cancellation_reason is None


def test_tombstoned_thread_rejects_the_claimed_attempt(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    coordinator.tombstone_thread(thread_id=lease.thread_id)
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            _put(saver, _config(lease))
        assert rejected.value.reason == OWNERSHIP_THREAD_TOMBSTONED
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


def test_partial_identity_fails_closed_before_any_write(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    config = _config(lease)
    del config["configurable"]["thread_generation"]
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            _put(saver, config)
        assert rejected.value.reason == IDENTITY_INCOMPLETE
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


# ----- maintenance callers (no runtime identity) --------------------------


def test_maintenance_write_is_allowed_without_live_ownership(
    runtime_url: str, store: PostgresRuntimeStore
) -> None:
    thread = _thread()
    with _fenced_saver(runtime_url, store) as saver:
        _put(saver, _maintenance_config(thread))
    assert _counts(runtime_url, thread) == (1, 0)


def test_maintenance_write_is_rejected_while_a_reservation_is_live(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            _put(saver, _maintenance_config(lease.thread_id))
        assert rejected.value.reason == IDENTITY_MISSING
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


# ----- the remaining mutators --------------------------------------------


def test_prune_is_rejected_while_the_thread_has_a_live_run(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            saver.prune([lease.thread_id])
        assert rejected.value.reason == IDENTITY_MISSING
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


def test_prune_is_allowed_on_a_quiet_thread(
    runtime_url: str, store: PostgresRuntimeStore
) -> None:
    thread = _thread()
    with _fenced_saver(runtime_url, store) as saver:
        _put(saver, _maintenance_config(thread))
        saver.prune([thread])
    assert _counts(runtime_url, thread) == (1, 0)


def test_copy_thread_is_rejected_when_the_target_has_a_live_run(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    source = _thread()
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            saver.copy_thread(source, lease.thread_id)
        assert rejected.value.reason == IDENTITY_MISSING
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


def test_delete_for_runs_fails_closed(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    lease = _claim(coordinator)
    with _fenced_saver(runtime_url, store) as saver:
        with pytest.raises(CheckpointOwnershipLost) as rejected:
            saver.delete_for_runs([lease.run_id])
        assert rejected.value.reason == "runtime_mutation_not_supported"
    assert _counts(runtime_url, lease.thread_id) == (0, 0)


# ----- deletion -----------------------------------------------------------


def test_delete_thread_tombstones_durably_and_blocks_late_writes(
    runtime_url: str, store: PostgresRuntimeStore
) -> None:
    thread = _thread()
    with _fenced_saver(runtime_url, store) as saver:
        _put(saver, _maintenance_config(thread))
        assert _counts(runtime_url, thread) == (1, 0)

        saver.delete_thread(thread)
        # Idempotent: the second delete neither raises nor resurrects state.
        saver.delete_thread(thread)

        child = runtime_pg_support.run_child(
            CHILD_THREAD_READBACK,
            dsn=runtime_url,
            env={"FIN_RUNTIME_THREAD": thread},
        )
        assert child["pid"] != os.getpid()
        assert child["thread_state"] is not None
        assert bool(child["thread_state"][0]) is True
        assert child["checkpoints"] == 0
        assert child["writes"] == 0

        with pytest.raises(CheckpointOwnershipLost) as rejected:
            _put(saver, _maintenance_config(thread))
        assert rejected.value.reason == OWNERSHIP_THREAD_TOMBSTONED


# ----- the adapter itself -------------------------------------------------


def test_fenced_saver_uses_a_reentrant_lock_and_no_pipeline(
    runtime_url: str, store: PostgresRuntimeStore
) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from fin.host.compile import bounded_checkpoint_conninfo
    from fin.host.runtime.fencing import FencedPostgresSaver

    with PostgresSaver.from_conn_string(
        bounded_checkpoint_conninfo(runtime_url)
    ) as base:
        saver = FencedPostgresSaver(base.conn, store)

    assert saver.supports_pipeline is False
    assert saver.store is store
    # ``conn.transaction()`` in production wraps ``_cursor``, which acquires
    # the same lock again: a plain Lock would deadlock here.
    assert saver.lock.acquire(blocking=False) is True
    try:
        assert saver.lock.acquire(blocking=False) is True
        saver.lock.release()
    finally:
        saver.lock.release()


# ----- production compile wiring ------------------------------------------


def test_compile_runtime_handle_fences_the_saver_and_exposes_runtime(
    runtime_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host import ScriptedChatModel
    from fin.host import compile as compile_module
    from fin.host.runtime.admission import RuntimeCoordinator
    from fin.host.runtime.fencing import FencedPostgresSaver

    monkeypatch.setattr(
        compile_module,
        "_build_default_model",
        lambda **_kwargs: ScriptedChatModel(script=[]),
    )

    handle = compile_module.compile_runtime_handle(
        checkpointer_url=runtime_url, env={}
    )
    try:
        compiled = handle.graph
        assert isinstance(compiled.runtime, RuntimeCoordinator)
        saver = compiled._graph.checkpointer
        assert isinstance(saver, FencedPostgresSaver)
        assert saver.store is compiled.runtime.store
        assert saver.supports_pipeline is False

        # The production boot wraps the lock in ``_RunAwareCheckpointLock``.
        # A maintenance write through the booted saver proves the nested
        # transaction re-enters it instead of deadlocking.
        thread = _thread()
        _put(saver, _maintenance_config(thread))
        assert _counts(runtime_url, thread) == (1, 0)
    finally:
        handle.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FIN_RUNTIME_QUEUE_CAPACITY", "0"),
        ("FIN_RUNTIME_QUEUE_CAPACITY", "not-a-number"),
        ("FIN_RUNTIME_LEASE_SECONDS", "0"),
        ("FIN_RUNTIME_LEASE_SECONDS", "-1"),
        ("FIN_RUNTIME_LEASE_SECONDS", "not-a-number"),
    ],
)
def test_compile_rejects_invalid_runtime_settings(
    runtime_url: str,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    from fin.host import ScriptedChatModel
    from fin.host import compile as compile_module
    from fin.host.compile import RuntimeConfigError

    monkeypatch.setattr(
        compile_module,
        "_build_default_model",
        lambda **_kwargs: ScriptedChatModel(script=[]),
    )

    with pytest.raises(RuntimeConfigError) as raised:
        compile_module.compile_runtime_handle(
            checkpointer_url=runtime_url, env={name: value}
        )
    assert raised.value.env_var == name
