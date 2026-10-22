"""T2: one reserved run per thread, distinct fenced attempts, safe recovery.

Barrier-controlled claims and explicit lease expiry (a row update, never a
sleep) order the races. The recovery cases insert real effect events, because
lease state alone must never decide whether an external effect is safe to
replay.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Iterator

import pytest

from fin.host.runtime import (
    CheckpointOwnershipLost,
    InvalidOperation,
    PostgresRuntimeStore,
    RuntimeCoordinator,
    assert_ownership,
)
from fin.host.runtime.schema import (
    ATTEMPT_ACTIVE,
    ATTEMPT_EXPIRED,
    ATTEMPT_RELEASED,
    CLAIM_LEASE_ACTIVE,
    CLAIM_NOT_HEAD,
    CLAIM_RESERVED_BY_OTHER,
    CLAIM_THREAD_CANCELLED,
    CLAIM_THREAD_TOMBSTONED,
    EFFECT_COMPLETED_EVENT,
    EFFECT_STARTED_EVENT,
    RECOVERY_INTERRUPTED,
    RECOVERY_PROVEN_SAFE,
    RECOVERY_RECONCILIATION_REQUIRED,
    RECOVERY_TERMINAL_COMMITTED,
    RECOVERY_UNCERTAIN_EFFECT,
    RENEW_LEASE_EXPIRED,
    RENEW_NOT_ACTIVE,
    RENEW_UNKNOWN_ATTEMPT,
    OWNERSHIP_ATTEMPT_MISSING,
    OWNERSHIP_ATTEMPT_GENERATION,
    OWNERSHIP_ATTEMPT_NOT_ACTIVE,
    OWNERSHIP_IDENTITY_INCOMPLETE,
    OWNERSHIP_LEASE_EXPIRED,
    OWNERSHIP_THREAD_CANCELLED,
    OWNERSHIP_THREAD_GENERATION,
    OWNERSHIP_THREAD_TOMBSTONED,
    RESERVATION_RESERVED,
    RUN_RUNNING,
    RUN_WAITING,
    AttemptFact,
    EventFact,
    RunRecoverySnapshot,
)
from fin.host.runtime.recovery import project_recovery_facts

import runtime_pg_support

CHILD_THREAD_STATE = """
import json, os
import psycopg
with psycopg.connect(os.environ["FIN_TEST_DSN"]) as conn:
    state = conn.execute(
        "SELECT tombstoned, cancellation_reason FROM fin_runtime_thread_state "
        "WHERE thread_id = %s",
        (os.environ["FIN_RUNTIME_THREAD"],),
    ).fetchone()
    reservation = conn.execute(
        "SELECT state FROM fin_runtime_thread_reservation WHERE thread_id = %s",
        (os.environ["FIN_RUNTIME_THREAD"],),
    ).fetchone()
print(json.dumps({
    "pid": os.getpid(),
    "thread_state": state,
    "reservation": reservation,
}))
"""

CHILD_RUN_STATE = """
import json, os
import psycopg
with psycopg.connect(os.environ["FIN_TEST_DSN"]) as conn:
    run = conn.execute(
        "SELECT state, attempt_generation FROM fin_runtime_run WHERE run_id = %s",
        (os.environ["FIN_RUNTIME_RUN"],),
    ).fetchone()
    reservation = conn.execute(
        "SELECT run_id, state FROM fin_runtime_thread_reservation "
        "WHERE thread_id = %s",
        (os.environ["FIN_RUNTIME_THREAD"],),
    ).fetchone()
    attempt = conn.execute(
        "SELECT state FROM fin_runtime_worker_attempt WHERE attempt_id = %s",
        (os.environ["FIN_RUNTIME_ATTEMPT"],),
    ).fetchone()
print(json.dumps({
    "pid": os.getpid(),
    "run": run,
    "reservation": reservation,
    "attempt": attempt,
}))
"""


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_res")
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
    return RuntimeCoordinator(store, lease_seconds=30.0)


def _thread() -> str:
    return f"thread-{uuid.uuid4()}"


def _accept(coordinator: RuntimeCoordinator, thread: str, count: int = 1):
    return [
        coordinator.accept(
            operation_id=f"op-{uuid.uuid4()}",
            thread_id=thread,
            accepted_input={"message": index},
        )
        for index in range(count)
    ]


def _expire(store: PostgresRuntimeStore, attempt_id: str) -> None:
    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        conn.execute(
            "UPDATE fin_runtime_worker_attempt SET "
            "lease_expires_at = now() - interval '1 second' "
            "WHERE attempt_id = %s",
            (attempt_id,),
        )


def _assert_current(
    store: PostgresRuntimeStore,
    *,
    thread_id: str,
    run_id: str,
    attempt_id: str,
    attempt_generation: int,
    thread_generation: int,
) -> None:
    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        with conn.transaction():
            assert_ownership(
                conn.cursor(),
                thread_id=thread_id,
                run_id=run_id,
                attempt_id=attempt_id,
                attempt_generation=attempt_generation,
                thread_generation=thread_generation,
            )


# ----- hermetic recovery classification -----------------------------------


def _snapshot(**overrides: object) -> RunRecoverySnapshot:
    values: dict[str, object] = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "run_state": RUN_RUNNING,
        "attempt_generation": 1,
        "thread_generation": 1,
        "reservation_state": RESERVATION_RESERVED,
        "reservation_run_id": "run-1",
        "tombstoned": False,
        "cancellation_reason": None,
        "terminal_committed": False,
        "attempts": (
            AttemptFact(
                attempt_id="attempt-1",
                attempt_generation=1,
                state=ATTEMPT_RELEASED,
                lease_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ),
        "events": (),
        "projection_revision": 1,
        "high_water_seq": 1,
    }
    values.update(overrides)
    return RunRecoverySnapshot(**values)  # type: ignore[arg-type]


def test_lease_expiry_alone_proves_safe_when_no_effect_started() -> None:
    facts = project_recovery_facts(
        _snapshot(), now=datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    assert facts.verdict == RECOVERY_PROVEN_SAFE
    assert facts.resumable is True
    assert facts.reason == "lease_expired_without_effect"


def test_an_unpaired_effect_requires_reconciliation() -> None:
    events = (
        EventFact(
            seq=1,
            run_id="run-1",
            kind=EFFECT_STARTED_EVENT,
            payload={"effect_key": "provider-1"},
        ),
    )
    facts = project_recovery_facts(
        _snapshot(events=events), now=datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    assert facts.verdict == RECOVERY_RECONCILIATION_REQUIRED
    assert facts.resumable is False
    assert facts.open_effect_keys == ("provider-1",)
    assert facts.reason == RECOVERY_UNCERTAIN_EFFECT


def test_a_paired_effect_is_closed_by_its_own_key() -> None:
    events = (
        EventFact(
            seq=1,
            run_id="run-1",
            kind=EFFECT_STARTED_EVENT,
            payload={"effect_key": "provider-1"},
        ),
        EventFact(
            seq=2,
            run_id="run-1",
            kind=EFFECT_COMPLETED_EVENT,
            payload={"effect_key": "provider-1"},
        ),
    )
    facts = project_recovery_facts(
        _snapshot(events=events), now=datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    assert facts.verdict == RECOVERY_PROVEN_SAFE
    assert facts.resumable is True


def test_a_live_lease_is_not_resumable_even_when_safe() -> None:
    attempt = AttemptFact(
        attempt_id="attempt-1",
        attempt_generation=1,
        state=ATTEMPT_ACTIVE,
        lease_expires_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    facts = project_recovery_facts(
        _snapshot(attempts=(attempt,)), now=datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    assert facts.verdict == RECOVERY_PROVEN_SAFE
    assert facts.resumable is False


def test_a_cancelled_thread_is_interrupted() -> None:
    facts = project_recovery_facts(_snapshot(cancellation_reason="user_stop"))
    assert facts.verdict == RECOVERY_INTERRUPTED
    assert facts.resumable is False


# ----- live: reservations and attempts ------------------------------------


@pytest.mark.runtime_pg
def test_a_waiting_run_keeps_its_reservation_and_releases_its_lease(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.claimed is True and claimed.lease is not None
    attempt = claimed.lease.attempt_id

    assert coordinator.wait(attempt_id=attempt) is True

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        run_state = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
        reservation = conn.execute(
            "SELECT run_id, state FROM fin_runtime_thread_reservation "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
        attempt_state = conn.execute(
            "SELECT state FROM fin_runtime_worker_attempt WHERE attempt_id = %s",
            (attempt,),
        ).fetchone()
    assert run_state == (RUN_WAITING,)
    assert reservation == (record.run_id, RESERVATION_RESERVED)
    assert attempt_state == (ATTEMPT_RELEASED,)


@pytest.mark.runtime_pg
def test_an_ordinary_queued_run_cannot_use_a_reserved_thread(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    first, second = _accept(coordinator, thread, count=2)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.claimed is True and claimed.lease is not None
    coordinator.wait(attempt_id=claimed.lease.attempt_id)

    blocked = coordinator.claim(
        thread_id=thread, worker_id="worker-2", run_id=second.run_id
    )
    assert blocked.claimed is False
    assert blocked.reason == CLAIM_RESERVED_BY_OTHER

    # An implicit claim asks for the thread's runnable work: that is the
    # reserved run itself, never the ordinary run queued behind it.
    implicit = coordinator.claim(thread_id=thread, worker_id="worker-3")
    assert implicit.claimed is True
    assert implicit.lease is not None
    assert implicit.lease.run_id == first.run_id
    coordinator.wait(attempt_id=implicit.lease.attempt_id)

    coordinator.end(run_id=first.run_id, outcome="cancelled")
    released = coordinator.claim(
        thread_id=thread, worker_id="worker-2", run_id=second.run_id
    )
    assert released.claimed is True
    assert released.lease is not None
    assert released.lease.run_id == second.run_id


@pytest.mark.runtime_pg
def test_only_the_head_of_the_thread_queue_is_claimable(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    first, second = _accept(coordinator, thread, count=2)
    blocked = coordinator.claim(
        thread_id=thread, worker_id="worker-1", run_id=second.run_id
    )
    assert blocked.claimed is False
    assert blocked.reason == CLAIM_NOT_HEAD

    head = coordinator.claim(
        thread_id=thread, worker_id="worker-1", run_id=first.run_id
    )
    assert head.claimed is True


@pytest.mark.runtime_pg
def test_a_live_lease_refuses_a_second_claim(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    _accept(coordinator, thread)
    first = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert first.claimed is True

    second = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert second.claimed is False
    assert second.reason == CLAIM_LEASE_ACTIVE

    assert first.lease is not None
    renewed = coordinator.renew(attempt_id=first.lease.attempt_id)
    assert renewed.renewed is True


@pytest.mark.runtime_pg
def test_takeover_keeps_the_run_and_increments_the_attempt_generation(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    first = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert first.lease is not None
    _expire(store, first.lease.attempt_id)

    second = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert second.claimed is True
    assert second.lease is not None
    assert second.lease.run_id == record.run_id
    assert second.lease.attempt_generation == first.lease.attempt_generation + 1

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        old_state = conn.execute(
            "SELECT state FROM fin_runtime_worker_attempt WHERE attempt_id = %s",
            (first.lease.attempt_id,),
        ).fetchone()
    assert old_state == (ATTEMPT_EXPIRED,)
    assert (
        coordinator.renew(attempt_id=first.lease.attempt_id).reason
        == RENEW_NOT_ACTIVE
    )


@pytest.mark.runtime_pg
def test_a_released_lease_is_taken_over_after_waiting(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    first = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert first.lease is not None
    coordinator.wait(attempt_id=first.lease.attempt_id)

    second = coordinator.claim(
        thread_id=thread, worker_id="worker-2", run_id=record.run_id
    )
    assert second.claimed is True
    assert second.lease is not None
    assert second.lease.attempt_generation == 2


@pytest.mark.runtime_pg
def test_renewal_after_expiry_never_revives_the_lease(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    _expire(store, claimed.lease.attempt_id)

    outcome = coordinator.renew(attempt_id=claimed.lease.attempt_id)
    assert outcome.renewed is False
    assert outcome.reason == RENEW_LEASE_EXPIRED
    assert coordinator.renew(attempt_id="absent-attempt").reason == RENEW_UNKNOWN_ATTEMPT


@pytest.mark.runtime_pg
def test_different_threads_overlap(coordinator: RuntimeCoordinator) -> None:
    threads = [_thread(), _thread()]
    for thread in threads:
        _accept(coordinator, thread)
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def claim(thread: str) -> None:
        barrier.wait()
        outcome = coordinator.claim(thread_id=thread, worker_id=f"w-{thread}")
        with lock:
            results.append(outcome)

    handles = [threading.Thread(target=claim, args=(thread,)) for thread in threads]
    for handle in handles:
        handle.start()
    for handle in handles:
        handle.join(timeout=60.0)

    assert all(getattr(result, "claimed", False) for result in results)
    run_ids = {result.lease.run_id for result in results if result.lease}  # type: ignore[attr-defined]
    assert len(run_ids) == 2


@pytest.mark.runtime_pg
def test_barrier_controlled_claims_have_one_winner(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    workers = 6
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    outcomes: list[object] = []

    def claim(index: int) -> None:
        barrier.wait()
        outcome = coordinator.claim(thread_id=thread, worker_id=f"worker-{index}")
        with lock:
            outcomes.append(outcome)

    handles = [threading.Thread(target=claim, args=(index,)) for index in range(workers)]
    for handle in handles:
        handle.start()
    for handle in handles:
        handle.join(timeout=60.0)

    winners = [outcome for outcome in outcomes if getattr(outcome, "claimed", False)]
    assert len(winners) == 1
    assert winners[0].lease.run_id == record.run_id  # type: ignore[attr-defined]

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        attempts = conn.execute(
            "SELECT count(*) FROM fin_runtime_worker_attempt WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
    assert attempts == (1,)


@pytest.mark.runtime_pg
def test_recovery_facts_come_from_durable_effect_events(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    _expire(store, claimed.lease.attempt_id)

    safe = coordinator.recover(run_id=record.run_id)
    assert safe.verdict == RECOVERY_PROVEN_SAFE
    assert safe.resumable is True

    store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind=EFFECT_STARTED_EVENT,
        payload={"effect_key": "provider-1"},
        projection={"status": "calling"},
    )
    uncertain = coordinator.recover(run_id=record.run_id)
    assert uncertain.verdict == RECOVERY_RECONCILIATION_REQUIRED
    assert uncertain.resumable is False
    assert uncertain.open_effect_keys == ("provider-1",)

    store.commit_event(
        thread_id=thread,
        run_id=record.run_id,
        kind=EFFECT_COMPLETED_EVENT,
        payload={"effect_key": "provider-1"},
        projection={"status": "called"},
    )
    recovered = coordinator.recover(run_id=record.run_id)
    assert recovered.verdict == RECOVERY_PROVEN_SAFE
    assert recovered.resumable is True


@pytest.mark.runtime_pg
def test_a_committed_terminal_is_not_resumable(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    coordinator.claim(thread_id=thread, worker_id="worker-1")
    store.commit_terminal(
        run_id=record.run_id,
        technical_result="done",
        semantic_result="answered",
        payload={"assistant_message": "answer"},
        evidence={"citations": []},
        projection={"status": "done"},
        outbox_payload={"kind": "terminal"},
    )
    facts = coordinator.recover(run_id=record.run_id)
    assert facts.verdict == RECOVERY_PROVEN_SAFE
    assert facts.resumable is False
    assert facts.reason == RECOVERY_TERMINAL_COMMITTED


@pytest.mark.runtime_pg
def test_a_cancelled_run_is_interrupted(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    coordinator.end(run_id=record.run_id, outcome="cancelled")
    facts = coordinator.recover(run_id=record.run_id)
    assert facts.verdict == RECOVERY_INTERRUPTED
    assert facts.resumable is False


@pytest.mark.runtime_pg
def test_assert_ownership_accepts_the_current_owner_and_rejects_stale_ones(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    lease = claimed.lease

    _assert_current(
        store,
        thread_id=thread,
        run_id=record.run_id,
        attempt_id=lease.attempt_id,
        attempt_generation=lease.attempt_generation,
        thread_generation=lease.thread_generation,
    )

    with pytest.raises(CheckpointOwnershipLost) as wrong_thread:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id=lease.attempt_id,
            attempt_generation=lease.attempt_generation,
            thread_generation=lease.thread_generation + 1,
        )
    assert wrong_thread.value.reason == OWNERSHIP_THREAD_GENERATION

    with pytest.raises(CheckpointOwnershipLost) as absent:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id="absent-attempt",
            attempt_generation=lease.attempt_generation,
            thread_generation=lease.thread_generation,
        )
    assert absent.value.reason == OWNERSHIP_ATTEMPT_MISSING

    _expire(store, lease.attempt_id)
    with pytest.raises(CheckpointOwnershipLost) as expired:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id=lease.attempt_id,
            attempt_generation=lease.attempt_generation,
            thread_generation=lease.thread_generation,
        )
    assert expired.value.reason == OWNERSHIP_LEASE_EXPIRED


@pytest.mark.runtime_pg
def test_assert_ownership_rejects_takeover_generation_and_deleted_threads(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    first = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert first.lease is not None
    _expire(store, first.lease.attempt_id)
    second = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert second.lease is not None

    with pytest.raises(CheckpointOwnershipLost) as stale:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id=first.lease.attempt_id,
            attempt_generation=first.lease.attempt_generation,
            thread_generation=first.lease.thread_generation,
        )
    assert stale.value.reason == OWNERSHIP_ATTEMPT_GENERATION

    coordinator.tombstone_thread(thread_id=thread)
    with pytest.raises(CheckpointOwnershipLost) as tombstoned:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id=second.lease.attempt_id,
            attempt_generation=second.lease.attempt_generation,
            thread_generation=second.lease.thread_generation,
        )
    assert tombstoned.value.reason == OWNERSHIP_THREAD_TOMBSTONED


@pytest.mark.runtime_pg
def test_marking_a_thread_cancelled_fences_the_live_attempt(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    lease = claimed.lease

    _assert_current(
        store,
        thread_id=thread,
        run_id=record.run_id,
        attempt_id=lease.attempt_id,
        attempt_generation=lease.attempt_generation,
        thread_generation=lease.thread_generation,
    )

    fence = coordinator.mark_thread_cancelled(thread_id=thread, reason="user_stop")
    assert fence.tombstoned is False
    assert fence.cancellation_reason == "user_stop"

    with pytest.raises(CheckpointOwnershipLost) as cancelled:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id=lease.attempt_id,
            attempt_generation=lease.attempt_generation,
            thread_generation=lease.thread_generation,
        )
    assert cancelled.value.reason == OWNERSHIP_THREAD_CANCELLED

    refused = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert refused.claimed is False
    assert refused.reason == CLAIM_THREAD_CANCELLED


@pytest.mark.runtime_pg
def test_tombstoning_a_thread_releases_the_reservation_and_fences_writers(
    runtime_url: str, store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    lease = claimed.lease

    fence = coordinator.tombstone_thread(thread_id=thread)
    assert fence.tombstoned is True
    assert fence.reservation_released is True
    assert fence.attempts_released == 1

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        reservation = conn.execute(
            "SELECT state FROM fin_runtime_thread_reservation "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
        attempt_state = conn.execute(
            "SELECT state FROM fin_runtime_worker_attempt WHERE attempt_id = %s",
            (lease.attempt_id,),
        ).fetchone()
    assert reservation == ("released",)
    assert attempt_state == (ATTEMPT_RELEASED,)

    with pytest.raises(CheckpointOwnershipLost) as tombstoned:
        _assert_current(
            store,
            thread_id=thread,
            run_id=record.run_id,
            attempt_id=lease.attempt_id,
            attempt_generation=lease.attempt_generation,
            thread_generation=lease.thread_generation,
        )
    assert tombstoned.value.reason == OWNERSHIP_THREAD_TOMBSTONED

    refused = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert refused.claimed is False
    assert refused.reason == CLAIM_THREAD_TOMBSTONED

    with pytest.raises(InvalidOperation) as late_accept:
        coordinator.accept(
            operation_id=f"op-{uuid.uuid4()}",
            thread_id=thread,
            accepted_input={"message": "late"},
        )
    assert late_accept.value.reason == "tombstoned"

    again = coordinator.tombstone_thread(thread_id=thread)
    assert again.tombstoned is True
    assert again.reservation_released is False
    assert again.attempts_released == 0

    child = runtime_pg_support.run_child(
        CHILD_THREAD_STATE,
        dsn=runtime_url,
        env={"FIN_RUNTIME_THREAD": thread},
    )
    assert child["pid"] != os.getpid()
    assert child["thread_state"] == [True, None]
    assert child["reservation"] == ["released"]


@pytest.mark.runtime_pg
def test_a_fresh_os_process_reads_the_reservation_and_lease(
    runtime_url: str, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None
    coordinator.wait(attempt_id=claimed.lease.attempt_id)

    child = runtime_pg_support.run_child(
        CHILD_RUN_STATE,
        dsn=runtime_url,
        env={
            "FIN_RUNTIME_RUN": record.run_id,
            "FIN_RUNTIME_THREAD": thread,
            "FIN_RUNTIME_ATTEMPT": claimed.lease.attempt_id,
        },
    )
    assert child["pid"] != os.getpid()
    assert child["run"] == [RUN_WAITING, claimed.lease.attempt_generation]
    assert child["reservation"] == [record.run_id, RESERVATION_RESERVED]
    assert child["attempt"] == [ATTEMPT_RELEASED]


@pytest.mark.runtime_pg
def test_a_stale_worker_cannot_end_the_run_it_lost(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    first = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert first.lease is not None
    _expire(store, first.lease.attempt_id)
    second = coordinator.claim(thread_id=thread, worker_id="worker-2")
    assert second.lease is not None

    with pytest.raises(CheckpointOwnershipLost) as stale_attempt:
        coordinator.end(
            run_id=record.run_id,
            outcome="abandoned",
            attempt_id=first.lease.attempt_id,
            attempt_generation=first.lease.attempt_generation,
        )
    assert stale_attempt.value.reason == OWNERSHIP_ATTEMPT_NOT_ACTIVE

    with pytest.raises(CheckpointOwnershipLost) as stale_generation:
        coordinator.end(
            run_id=record.run_id,
            outcome="abandoned",
            attempt_id=second.lease.attempt_id,
            attempt_generation=first.lease.attempt_generation,
        )
    assert stale_generation.value.reason == OWNERSHIP_ATTEMPT_GENERATION

    import psycopg

    with psycopg.connect(store.connection_url) as conn:
        run = conn.execute(
            "SELECT state FROM fin_runtime_run WHERE run_id = %s",
            (record.run_id,),
        ).fetchone()
        reservation = conn.execute(
            "SELECT run_id, state FROM fin_runtime_thread_reservation "
            "WHERE thread_id = %s",
            (thread,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT state FROM fin_runtime_worker_attempt WHERE attempt_id = %s",
            (second.lease.attempt_id,),
        ).fetchone()
    assert run == (RUN_RUNNING,)
    assert reservation == (record.run_id, RESERVATION_RESERVED)
    assert attempt == (ATTEMPT_ACTIVE,)

    ended = coordinator.end(
        run_id=record.run_id,
        outcome="abandoned",
        attempt_id=second.lease.attempt_id,
        attempt_generation=second.lease.attempt_generation,
    )
    assert ended.state == "abandoned"
    assert ended.reservation_released is True
    assert ended.attempts_released == 1


@pytest.mark.runtime_pg
def test_an_end_without_attempt_identity_is_the_maintenance_path(
    store: PostgresRuntimeStore, coordinator: RuntimeCoordinator
) -> None:
    thread = _thread()
    (record,) = _accept(coordinator, thread)
    claimed = coordinator.claim(thread_id=thread, worker_id="worker-1")
    assert claimed.lease is not None

    with pytest.raises(CheckpointOwnershipLost) as partial:
        coordinator.end(
            run_id=record.run_id,
            outcome="abandoned",
            attempt_id=claimed.lease.attempt_id,
        )
    assert partial.value.reason == OWNERSHIP_IDENTITY_INCOMPLETE

    ended = coordinator.end(run_id=record.run_id, outcome="abandoned")
    assert ended.state == "abandoned"
    assert ended.reservation_released is True
