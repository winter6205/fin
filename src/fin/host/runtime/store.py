"""PostgreSQL store for durable Runtime acceptance, ownership and events.

Every public call opens one short-lived connection; multi-statement units run
inside ``with conn.transaction()`` so a reader never observes a half-accepted
operation, a half-committed terminal, or a projection without its sequence and
outbox row. Per-thread acceptance and claims serialize on a transaction-scoped
advisory lock, which also keeps the FIFO and event counters gapless: a rolled
back transaction rolls its allocation back with it.

``assert_ownership`` is a free function so the fencing adapter can run it on
its own cursor inside its own checkpoint transaction (plan T3).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence

from fin.host.runtime.schema import (
    ATTEMPT_ACTIVE,
    ATTEMPT_EXPIRED,
    ATTEMPT_RELEASED,
    CLAIM_LEASE_ACTIVE,
    CLAIM_NO_QUEUED_RUN,
    CLAIM_NOT_HEAD,
    CLAIM_RESERVED_BY_OTHER,
    CLAIM_RUN_NOT_CLAIMABLE,
    CLAIM_THREAD_CANCELLED,
    CLAIM_THREAD_TOMBSTONED,
    OPERATION_OPEN,
    OWNERSHIP_ATTEMPT_GENERATION,
    OWNERSHIP_ATTEMPT_MISSING,
    OWNERSHIP_ATTEMPT_NOT_ACTIVE,
    OWNERSHIP_IDENTITY_INCOMPLETE,
    OWNERSHIP_LEASE_EXPIRED,
    OWNERSHIP_RESERVATION_MISSING,
    OWNERSHIP_RESERVATION_OTHER_RUN,
    OWNERSHIP_RESERVATION_RELEASED,
    OWNERSHIP_RUN_MISSING,
    OWNERSHIP_RUN_NOT_RUNNING,
    OWNERSHIP_THREAD_CANCELLED,
    OWNERSHIP_THREAD_GENERATION,
    OWNERSHIP_THREAD_TOMBSTONED,
    PARSER_INBOX_PENDING,
    PARSER_INBOX_RECONCILED,
    RENEW_GENERATION_MISMATCH,
    RENEW_LEASE_EXPIRED,
    RENEW_NOT_ACTIVE,
    RENEW_RUN_NOT_RUNNING,
    RENEW_UNKNOWN_ATTEMPT,
    RESET_CURSOR_AHEAD,
    RESET_EVENT_GAP,
    RESET_PROJECTION_MISSING,
    RESET_SEQUENCE_INCONSISTENT,
    RESERVATION_RELEASED,
    RESERVATION_RESERVED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_WAITING,
    SCHEMA_SQL,
    TERMINAL_RUN_STATES,
    AcceptanceRecord,
    AttemptFact,
    AttemptLease,
    CheckpointOwnershipLost,
    ClaimOutcome,
    EventCommit,
    EventFact,
    InvalidOperation,
    OperationConflict,
    OutboxRecord,
    ParserInboxRecord,
    QueueCapacityExceeded,
    RenewalOutcome,
    RunEndOutcome,
    RunEndRecord,
    RunRecoverySnapshot,
    RuntimeUnavailable,
    Snapshot,
    TerminalCommit,
    TerminalRecord,
    ThreadEventRecord,
    ThreadFenceRecord,
    UnknownOperation,
    normalize_identity,
    normalize_payload,
)

#: Default bound for a caller-supplied payload held in the acceptance row.
DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576

#: Bound for the durable documents the Runtime commits (projection, evidence).
_T4_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

_OPERATION_COLUMNS = (
    "operation_id, thread_id, payload_digest, accepted_input, fifo_position, "
    "run_id, notification_intent, result, state, created_at"
)


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeUnavailable("runtime store import") from exc
    return psycopg


def _jsonb(value: object) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _json_object(field: str, value: object) -> dict[str, Any]:
    """A durable document the caller handed us, checked before any write."""
    return normalize_payload(field, value, max_bytes=_T4_MAX_PAYLOAD_BYTES)


def _cancellation_applies(
    thread_state: Sequence[Any] | None, run_id: str
) -> bool:
    """Whether the thread's cancellation covers ``run_id``.

    ``thread_state`` is ``(tombstoned, cancellation_reason, cancelled_run_id)``.
    A cancellation with no run id is thread-wide; one that names a run covers
    only that run, so the thread's next run stays usable.
    """
    if thread_state is None or thread_state[1] is None:
        return False
    cancelled_run_id = thread_state[2]
    return cancelled_run_id is None or cancelled_run_id == run_id


def _refuse_deleted_thread(conn: Any, thread_id: str) -> None:
    """A deleted thread accepts no new durable state.

    Called after the thread advisory lock, before any sequence, projection,
    event or outbox write: deletion must not be resurrected by a late run.
    """
    row = conn.execute(
        "SELECT tombstoned FROM fin_runtime_thread_state WHERE thread_id = %s",
        (thread_id,),
    ).fetchone()
    if row is not None and bool(row[0]):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_TOMBSTONED)


def _row_to_acceptance(row: Sequence[Any]) -> AcceptanceRecord:
    return AcceptanceRecord(
        operation_id=row[0],
        thread_id=row[1],
        payload_digest=row[2],
        accepted_input=row[3],
        fifo_position=int(row[4]),
        run_id=row[5],
        notification_intent=row[6],
        result=row[7],
        state=row[8],
        created_at=row[9],
    )


def _row_to_attempt(row: Sequence[Any]) -> AttemptLease:
    return AttemptLease(
        attempt_id=row[0],
        run_id=row[1],
        thread_id=row[2],
        worker_id=row[3],
        attempt_generation=int(row[4]),
        thread_generation=int(row[5]),
        lease_expires_at=row[6],
        state=row[7],
    )


def _row_to_parser_inbox(row: Sequence[Any], *, replay: bool = False) -> ParserInboxRecord:
    return ParserInboxRecord(
        inbox_id=row[0],
        job_id=row[1],
        attachment_id=row[2],
        corpus_id=row[3],
        notification=row[4],
        state=row[5],
        received_at=row[6],
        reconciled_at=row[7],
        authority_state=row[8],
        replay=replay,
    )


_PARSER_INBOX_COLUMNS = (
    "inbox_id, job_id, attachment_id, corpus_id, notification, state, "
    "received_at, reconciled_at, authority_state"
)


class PostgresRuntimeStore:
    """Durable Runtime state in PostgreSQL.

    Construction does not touch the database; :meth:`setup` creates the
    idempotent DDL. Each public call opens a fresh connection, matching the
    existing host stores' short-transaction model.
    """

    def __init__(self, connection_url: str) -> None:
        if not isinstance(connection_url, str) or not connection_url.strip():
            raise ValueError("connection_url is required")
        self._url = connection_url

    @property
    def connection_url(self) -> str:
        return self._url

    @contextmanager
    def _connection(self, action: str) -> Iterator[Any]:
        psycopg = _psycopg()
        try:
            with psycopg.connect(self._url) as conn:
                yield conn
        except psycopg.Error as exc:
            raise RuntimeUnavailable(action) from exc

    def setup(self) -> None:
        """Create every Runtime table and index; safe to call repeatedly."""
        with self._connection("runtime schema setup") as conn:
            conn.execute(SCHEMA_SQL)

    # ----- T1: durable acceptance -----------------------------------------

    def accept_operation(
        self,
        *,
        operation_id: str,
        thread_id: str,
        payload_digest: str,
        accepted_input: Mapping[str, Any],
        notification_intent: Mapping[str, Any],
        run_id: str,
        capacity: int,
        thread_generation: int = 1,
    ) -> AcceptanceRecord:
        """Commit one accepted operation, its queue position and its run.

        A duplicate is recognized before capacity is consulted, so a caller
        retrying a lost response never pays for a full queue. When ``run_id``
        already exists the operation continues that run: the run row is not
        recreated or rewritten, and a terminal run is refused.
        """
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        with self._connection("accept operation") as conn:
            with conn.transaction():
                # Operation key first, then thread key: a stable global order.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 2)",
                    (operation_id,),
                )
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)",
                    (thread_id,),
                )
                thread_state = conn.execute(
                    "SELECT tombstoned FROM fin_runtime_thread_state "
                    "WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                if thread_state is not None and bool(thread_state[0]):
                    raise InvalidOperation("thread_id", "tombstoned")
                prior_row = conn.execute(
                    f"SELECT {_OPERATION_COLUMNS} FROM fin_runtime_operation "
                    "WHERE operation_id = %s",
                    (operation_id,),
                ).fetchone()
                if prior_row is not None:
                    prior = _row_to_acceptance(prior_row)
                    if (
                        prior.payload_digest != payload_digest
                        or prior.thread_id != thread_id
                    ):
                        raise OperationConflict(operation_id)
                    return replace(prior, replay=True)
                (open_count,) = conn.execute(
                    "SELECT count(*) FROM fin_runtime_operation "
                    "WHERE thread_id = %s AND state = %s",
                    (thread_id, OPERATION_OPEN),
                ).fetchone()
                if int(open_count) >= capacity:
                    raise QueueCapacityExceeded(thread_id, capacity)
                existing_run = conn.execute(
                    "SELECT thread_id, state, attempt_generation, thread_generation "
                    "FROM fin_runtime_run WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                ).fetchone()
                if existing_run is not None:
                    # A continuation is a new client operation on an existing
                    # run: the run row, its state and its attempt generation
                    # stay untouched, and the operation takes its own slot.
                    if existing_run[0] != thread_id:
                        raise InvalidOperation("run_id", "thread_mismatch")
                    if existing_run[1] in TERMINAL_RUN_STATES:
                        raise InvalidOperation("run_id", "terminal")
                    thread_generation = int(existing_run[3])
                else:
                    conn.execute(
                        "INSERT INTO fin_runtime_run "
                        "(run_id, thread_id, operation_id, state, "
                        "attempt_generation, thread_generation) "
                        "VALUES (%s, %s, %s, %s, 0, %s)",
                        (run_id, thread_id, operation_id, RUN_QUEUED, thread_generation),
                    )
                (fifo_position,) = conn.execute(
                    "INSERT INTO fin_runtime_thread_counter "
                    "(thread_id, next_fifo, next_event_seq, updated_at) "
                    "VALUES (%s, 2, 0, now()) "
                    "ON CONFLICT (thread_id) DO UPDATE SET "
                    "next_fifo = fin_runtime_thread_counter.next_fifo + 1, "
                    "updated_at = now() "
                    "RETURNING next_fifo - 1",
                    (thread_id,),
                ).fetchone()
                (created_at,) = conn.execute(
                    "INSERT INTO fin_runtime_operation "
                    "(operation_id, thread_id, payload_digest, accepted_input, "
                    "fifo_position, run_id, notification_intent, result, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s) "
                    "RETURNING created_at",
                    (
                        operation_id,
                        thread_id,
                        payload_digest,
                        _jsonb(dict(accepted_input)),
                        int(fifo_position),
                        run_id,
                        _jsonb(dict(notification_intent)),
                        OPERATION_OPEN,
                    ),
                ).fetchone()
                return AcceptanceRecord(
                    operation_id=operation_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    fifo_position=int(fifo_position),
                    payload_digest=payload_digest,
                    accepted_input=dict(accepted_input),
                    notification_intent=dict(notification_intent),
                    result=None,
                    state=OPERATION_OPEN,
                    created_at=created_at,
                )

    def read_operation(self, operation_id: str) -> AcceptanceRecord | None:
        with self._connection("read operation") as conn:
            row = conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM fin_runtime_operation "
                "WHERE operation_id = %s",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_acceptance(row)

    # ----- T2: reservations and worker attempts ---------------------------

    def claim_run(
        self,
        *,
        thread_id: str,
        worker_id: str,
        lease_seconds: float,
        attempt_id: str,
        run_id: str | None = None,
    ) -> ClaimOutcome:
        """Claim the thread's next runnable run as a new fenced attempt.

        With ``run_id`` the named run is claimed only when it is the head of
        the thread's queue. Without it the thread's reserved run is taken
        over when its lease is gone, and otherwise the head of the queue is
        selected. A thread already reserved by a different run refuses every
        other run.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._connection("claim run") as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (thread_id,)
                )
                thread_state = conn.execute(
                    "SELECT tombstoned, cancellation_reason, cancelled_run_id "
                    "FROM fin_runtime_thread_state WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                if thread_state is not None and bool(thread_state[0]):
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_THREAD_TOMBSTONED
                    )
                reservation = conn.execute(
                    "SELECT run_id, state, thread_generation "
                    "FROM fin_runtime_thread_reservation "
                    "WHERE thread_id = %s FOR UPDATE",
                    (thread_id,),
                ).fetchone()
                target = run_id
                takeover: Sequence[Any] | None = None
                if target is None:
                    if (
                        reservation is not None
                        and reservation[1] == RESERVATION_RESERVED
                    ):
                        # The thread's own run: taken over when its worker
                        # lease is gone, refused while the lease is live.
                        target = reservation[0]
                        takeover = conn.execute(
                            "SELECT 1 FROM fin_runtime_worker_attempt "
                            "WHERE run_id = %s AND state = %s "
                            "AND lease_expires_at > now()",
                            (target, ATTEMPT_ACTIVE),
                        ).fetchone()
                    else:
                        head_row = conn.execute(
                            "SELECT r.run_id FROM fin_runtime_run r "
                            "JOIN fin_runtime_operation o ON o.run_id = r.run_id "
                            "WHERE r.thread_id = %s AND r.state = %s "
                            "ORDER BY o.fifo_position ASC LIMIT 1",
                            (thread_id, RUN_QUEUED),
                        ).fetchone()
                        if head_row is None:
                            return ClaimOutcome(
                                claimed=False, lease=None, reason=CLAIM_NO_QUEUED_RUN
                            )
                        target = head_row[0]
                # A cancellation names one run or the whole thread, so a
                # different run on the same thread may still claim.
                if thread_state is not None and _cancellation_applies(
                    thread_state, target
                ):
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_THREAD_CANCELLED
                    )
                if takeover is not None:
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_LEASE_ACTIVE
                    )
                if (
                    reservation is not None
                    and reservation[1] == RESERVATION_RESERVED
                    and reservation[0] != target
                ):
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_RESERVED_BY_OTHER
                    )
                row = conn.execute(
                    "SELECT state, attempt_generation, thread_generation "
                    "FROM fin_runtime_run WHERE run_id = %s FOR UPDATE",
                    (target,),
                ).fetchone()
                if row is None:
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_RUN_NOT_CLAIMABLE
                    )
                run_state, attempt_generation, thread_generation = (
                    row[0],
                    int(row[1]),
                    int(row[2]),
                )
                if run_state in TERMINAL_RUN_STATES:
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_RUN_NOT_CLAIMABLE
                    )
                if run_state == RUN_QUEUED:
                    head = conn.execute(
                        "SELECT r.run_id FROM fin_runtime_run r "
                        "JOIN fin_runtime_operation o ON o.run_id = r.run_id "
                        "WHERE r.thread_id = %s AND r.state = %s "
                        "ORDER BY o.fifo_position ASC LIMIT 1 FOR UPDATE OF r",
                        (thread_id, RUN_QUEUED),
                    ).fetchone()
                    if head is None or head[0] != target:
                        return ClaimOutcome(
                            claimed=False, lease=None, reason=CLAIM_NOT_HEAD
                        )
                active = conn.execute(
                    "SELECT attempt_id, attempt_generation, "
                    "lease_expires_at > now() AS live "
                    "FROM fin_runtime_worker_attempt "
                    "WHERE run_id = %s AND state = %s "
                    "ORDER BY attempt_generation DESC LIMIT 1 FOR UPDATE",
                    (target, ATTEMPT_ACTIVE),
                ).fetchone()
                if active is not None and bool(active[2]):
                    return ClaimOutcome(
                        claimed=False, lease=None, reason=CLAIM_LEASE_ACTIVE
                    )
                if active is not None:
                    conn.execute(
                        "UPDATE fin_runtime_worker_attempt "
                        "SET state = %s, updated_at = now() WHERE attempt_id = %s",
                        (ATTEMPT_EXPIRED, active[0]),
                    )
                next_generation = attempt_generation + 1
                (lease_expires_at,) = conn.execute(
                    "INSERT INTO fin_runtime_worker_attempt "
                    "(attempt_id, run_id, worker_id, attempt_generation, "
                    "lease_expires_at, state) "
                    "VALUES (%s, %s, %s, %s, "
                    "now() + (%s::double precision * interval '1 second'), %s) "
                    "RETURNING lease_expires_at",
                    (
                        attempt_id,
                        target,
                        worker_id,
                        next_generation,
                        float(lease_seconds),
                        ATTEMPT_ACTIVE,
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE fin_runtime_run SET state = %s, "
                    "attempt_generation = %s, updated_at = now() "
                    "WHERE run_id = %s",
                    (RUN_RUNNING, next_generation, target),
                )
                conn.execute(
                    "INSERT INTO fin_runtime_thread_reservation "
                    "(thread_id, run_id, thread_generation, state, updated_at) "
                    "VALUES (%s, %s, %s, %s, now()) "
                    "ON CONFLICT (thread_id) DO UPDATE SET "
                    "run_id = EXCLUDED.run_id, "
                    "thread_generation = EXCLUDED.thread_generation, "
                    "state = EXCLUDED.state, updated_at = now()",
                    (thread_id, target, thread_generation, RESERVATION_RESERVED),
                )
                return ClaimOutcome(
                    claimed=True,
                    lease=AttemptLease(
                        attempt_id=attempt_id,
                        run_id=target,
                        thread_id=thread_id,
                        worker_id=worker_id,
                        attempt_generation=next_generation,
                        thread_generation=thread_generation,
                        lease_expires_at=lease_expires_at,
                        state=ATTEMPT_ACTIVE,
                    ),
                    reason=None,
                )

    def renew_attempt(
        self, *, attempt_id: str, lease_seconds: float
    ) -> RenewalOutcome:
        """Extend one live attempt; an expired or superseded lease never revives."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._connection("renew attempt") as conn:
            with conn.transaction():
                located = conn.execute(
                    "SELECT r.thread_id FROM fin_runtime_worker_attempt a "
                    "JOIN fin_runtime_run r ON r.run_id = a.run_id "
                    "WHERE a.attempt_id = %s",
                    (attempt_id,),
                ).fetchone()
                if located is None:
                    return RenewalOutcome(
                        renewed=False, lease=None, reason=RENEW_UNKNOWN_ATTEMPT
                    )
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (located[0],)
                )
                row = conn.execute(
                    "SELECT a.run_id, a.worker_id, a.attempt_generation, "
                    "a.state, a.lease_expires_at > now(), r.thread_id, r.state, "
                    "r.attempt_generation, r.thread_generation "
                    "FROM fin_runtime_worker_attempt a "
                    "JOIN fin_runtime_run r ON r.run_id = a.run_id "
                    "WHERE a.attempt_id = %s FOR UPDATE OF a, r",
                    (attempt_id,),
                ).fetchone()
                if row is None:
                    return RenewalOutcome(
                        renewed=False, lease=None, reason=RENEW_UNKNOWN_ATTEMPT
                    )
                if row[3] != ATTEMPT_ACTIVE:
                    return RenewalOutcome(
                        renewed=False, lease=None, reason=RENEW_NOT_ACTIVE
                    )
                if not bool(row[4]):
                    return RenewalOutcome(
                        renewed=False, lease=None, reason=RENEW_LEASE_EXPIRED
                    )
                if row[6] != RUN_RUNNING:
                    return RenewalOutcome(
                        renewed=False, lease=None, reason=RENEW_RUN_NOT_RUNNING
                    )
                if int(row[7]) != int(row[2]):
                    return RenewalOutcome(
                        renewed=False, lease=None, reason=RENEW_GENERATION_MISMATCH
                    )
                (lease_expires_at,) = conn.execute(
                    "UPDATE fin_runtime_worker_attempt SET "
                    "lease_expires_at = now() + "
                    "(%s::double precision * interval '1 second'), "
                    "updated_at = now() WHERE attempt_id = %s "
                    "RETURNING lease_expires_at",
                    (float(lease_seconds), attempt_id),
                ).fetchone()
                return RenewalOutcome(
                    renewed=True,
                    lease=AttemptLease(
                        attempt_id=attempt_id,
                        run_id=row[0],
                        thread_id=row[5],
                        worker_id=row[1],
                        attempt_generation=int(row[2]),
                        thread_generation=int(row[8]),
                        lease_expires_at=lease_expires_at,
                        state=ATTEMPT_ACTIVE,
                    ),
                    reason=None,
                )

    def release_attempt(self, *, attempt_id: str) -> bool:
        """Release one worker lease and leave the run waiting.

        The thread reservation stays with the run: a waiting run still owns
        its thread, only its worker slot is freed.
        """
        with self._connection("release attempt") as conn:
            with conn.transaction():
                located = conn.execute(
                    "SELECT r.thread_id FROM fin_runtime_worker_attempt a "
                    "JOIN fin_runtime_run r ON r.run_id = a.run_id "
                    "WHERE a.attempt_id = %s",
                    (attempt_id,),
                ).fetchone()
                if located is None:
                    return False
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (located[0],)
                )
                row = conn.execute(
                    "SELECT a.run_id, a.attempt_generation, a.state, r.state, "
                    "r.attempt_generation "
                    "FROM fin_runtime_worker_attempt a "
                    "JOIN fin_runtime_run r ON r.run_id = a.run_id "
                    "WHERE a.attempt_id = %s FOR UPDATE OF a, r",
                    (attempt_id,),
                ).fetchone()
                if row is None or row[2] != ATTEMPT_ACTIVE:
                    return False
                conn.execute(
                    "UPDATE fin_runtime_worker_attempt SET state = %s, "
                    "updated_at = now() WHERE attempt_id = %s",
                    (ATTEMPT_RELEASED, attempt_id),
                )
                if row[3] == RUN_RUNNING and int(row[4]) == int(row[1]):
                    conn.execute(
                        "UPDATE fin_runtime_run SET state = %s, updated_at = now() "
                        "WHERE run_id = %s",
                        (RUN_WAITING, row[0]),
                    )
                return True

    def end_run(
        self,
        *,
        run_id: str,
        outcome: RunEndOutcome,
        attempt_id: str | None = None,
        attempt_generation: int | None = None,
    ) -> RunEndRecord:
        """End one logical run, releasing its reservation and worker leases.

        A caller that carries a worker attempt proves it still owns the run
        in this transaction: a stale attempt whose lease was taken over
        raises :class:`CheckpointOwnershipLost` instead of ending the new
        owner's run. An identity-less call is the operator/maintenance path
        (a run that was never claimed, or a boot finalizing an abandoned
        checkpoint) and keeps the pre-existing behaviour.
        """
        if (attempt_id is None) != (attempt_generation is None):
            raise CheckpointOwnershipLost(OWNERSHIP_IDENTITY_INCOMPLETE)
        # EXIT: a partial identity is a wiring bug, never maintenance mode.
        with self._connection("end run") as conn:
            with conn.transaction():
                located = conn.execute(
                    "SELECT thread_id FROM fin_runtime_run WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                if located is None:
                    raise UnknownOperation(run_id, kind="run")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (located[0],)
                )
                # Lock order: thread reservation -> run/attempt authority.
                conn.execute(
                    "SELECT 1 FROM fin_runtime_thread_reservation "
                    "WHERE thread_id = %s FOR UPDATE",
                    (located[0],),
                )
                run = conn.execute(
                    "SELECT thread_id, state FROM fin_runtime_run "
                    "WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise UnknownOperation(run_id, kind="run")
                thread_id = run[0]
                if _read_terminal_on(conn, run_id) is not None:
                    # The terminal row is the authority: a late cancel,
                    # abandon or interrupt never rewrites a finished run.
                    return RunEndRecord(
                        run_id=run_id,
                        thread_id=thread_id,
                        state=run[1],
                        reservation_released=False,
                        attempts_released=0,
                        terminal=True,
                    )
                if run[1] in TERMINAL_RUN_STATES:
                    return RunEndRecord(
                        run_id=run_id,
                        thread_id=thread_id,
                        state=run[1],
                        reservation_released=False,
                        attempts_released=0,
                        terminal=True,
                    )
                # Fence only a mutation: an already terminal run is answered
                # above without touching the new owner's attempt.
                if attempt_id is not None and attempt_generation is not None:
                    _assert_end_ownership(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        attempt_generation=attempt_generation,
                    )
                reservation_released, attempts_released = _end_run_on(
                    conn,
                    run_id=run_id,
                    thread_id=thread_id,
                    outcome=outcome,
                    result_payload={"run_id": run_id, "state": outcome},
                )
                return RunEndRecord(
                    run_id=run_id,
                    thread_id=thread_id,
                    state=outcome,
                    reservation_released=reservation_released,
                    attempts_released=int(attempts_released),
                )

    # ----- thread cancellation and deletion -------------------------------

    def mark_thread_cancelled(
        self, *, thread_id: str, reason: str, run_id: str | None = None
    ) -> ThreadFenceRecord:
        """Record thread cancellation so checkpoint writes fail closed.

        Idempotent: the reason is replaced, the tombstone flag is untouched.
        A ``run_id`` scopes the cancellation to that run: the run's hold on
        the thread is released so the next run may be claimed, and every
        other run on the thread stays writable.
        """
        thread = normalize_identity("thread_id", thread_id)
        why = normalize_identity("reason", reason)
        target = (
            normalize_identity("run_id", run_id) if run_id is not None else None
        )
        with self._connection("mark thread cancelled") as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (thread,)
                )
                reservation_released = False
                if target is not None:
                    # The cancelled run must not keep holding the thread:
                    # the next run on this thread has to be claimable.
                    reservation_released = (
                        conn.execute(
                            "UPDATE fin_runtime_thread_reservation SET state = %s, "
                            "updated_at = now() WHERE thread_id = %s AND run_id = %s "
                            "AND state = %s",
                            (
                                RESERVATION_RELEASED,
                                thread,
                                target,
                                RESERVATION_RESERVED,
                            ),
                        ).rowcount
                        > 0
                    )
                row = conn.execute(
                    "INSERT INTO fin_runtime_thread_state "
                    "(thread_id, tombstoned, cancellation_reason, cancelled_run_id) "
                    "VALUES (%s, FALSE, %s, %s) "
                    "ON CONFLICT (thread_id) DO UPDATE SET "
                    "cancellation_reason = EXCLUDED.cancellation_reason, "
                    "cancelled_run_id = EXCLUDED.cancelled_run_id, "
                    "updated_at = now() "
                    "RETURNING tombstoned, cancellation_reason, cancelled_run_id",
                    (thread, why, target),
                ).fetchone()
                return ThreadFenceRecord(
                    thread_id=thread,
                    tombstoned=bool(row[0]),
                    cancellation_reason=row[1],
                    reservation_released=reservation_released,
                    attempts_released=0,
                    cancelled_run_id=row[2],
                )

    def tombstone_thread(self, *, thread_id: str) -> ThreadFenceRecord:
        """Fence a thread before its history and receipts are removed.

        The reservation is released and the reserved run's live worker leases
        are dropped, so no new claim can start and no checkpoint write can
        pass the ownership assertion afterwards.
        """
        thread = normalize_identity("thread_id", thread_id)
        with self._connection("tombstone thread") as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (thread,)
                )
                # Lock order: thread reservation -> run/attempt authority.
                reservation = conn.execute(
                    "SELECT run_id, state FROM fin_runtime_thread_reservation "
                    "WHERE thread_id = %s FOR UPDATE",
                    (thread,),
                ).fetchone()
                reservation_released = (
                    conn.execute(
                        "UPDATE fin_runtime_thread_reservation SET state = %s, "
                        "updated_at = now() "
                        "WHERE thread_id = %s AND state = %s",
                        (RESERVATION_RELEASED, thread, RESERVATION_RESERVED),
                    ).rowcount
                    > 0
                )
                attempts_released = 0
                if reservation is not None:
                    attempts_released = conn.execute(
                        "UPDATE fin_runtime_worker_attempt SET state = %s, "
                        "updated_at = now() WHERE run_id = %s AND state = %s",
                        (ATTEMPT_RELEASED, reservation[0], ATTEMPT_ACTIVE),
                    ).rowcount
                row = conn.execute(
                    "INSERT INTO fin_runtime_thread_state (thread_id, tombstoned) "
                    "VALUES (%s, TRUE) "
                    "ON CONFLICT (thread_id) DO UPDATE SET tombstoned = TRUE, "
                    "updated_at = now() "
                    "RETURNING tombstoned, cancellation_reason, cancelled_run_id",
                    (thread,),
                ).fetchone()
                return ThreadFenceRecord(
                    thread_id=thread,
                    tombstoned=bool(row[0]),
                    cancellation_reason=row[1],
                    reservation_released=reservation_released,
                    attempts_released=int(attempts_released),
                    cancelled_run_id=row[2],
                )

    def read_recovery_snapshot(self, run_id: str) -> RunRecoverySnapshot | None:
        """Read one run's ownership facts from one consistent snapshot."""
        with self._connection("read recovery snapshot") as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                run = conn.execute(
                    "SELECT thread_id, state, attempt_generation, thread_generation "
                    "FROM fin_runtime_run WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                if run is None:
                    return None
                thread_id = run[0]
                reservation = conn.execute(
                    "SELECT state, run_id FROM fin_runtime_thread_reservation "
                    "WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                thread_state = conn.execute(
                    "SELECT tombstoned, cancellation_reason, cancelled_run_id "
                    "FROM fin_runtime_thread_state WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                terminal = conn.execute(
                    "SELECT 1 FROM fin_runtime_terminal WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                attempts = conn.execute(
                    "SELECT attempt_id, attempt_generation, state, lease_expires_at "
                    "FROM fin_runtime_worker_attempt WHERE run_id = %s "
                    "ORDER BY attempt_generation ASC",
                    (run_id,),
                ).fetchall()
                events = conn.execute(
                    "SELECT seq, run_id, kind, payload "
                    "FROM fin_runtime_thread_event WHERE run_id = %s "
                    "ORDER BY seq ASC",
                    (run_id,),
                ).fetchall()
                projection = conn.execute(
                    "SELECT revision FROM fin_runtime_thread_projection "
                    "WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                counter = conn.execute(
                    "SELECT next_event_seq FROM fin_runtime_thread_counter "
                    "WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                return RunRecoverySnapshot(
                    run_id=run_id,
                    thread_id=thread_id,
                    run_state=run[1],
                    attempt_generation=int(run[2]),
                    thread_generation=int(run[3]),
                    reservation_state=reservation[0] if reservation else None,
                    reservation_run_id=reservation[1] if reservation else None,
                    tombstoned=bool(thread_state[0]) if thread_state else False,
                    cancellation_reason=(
                        thread_state[1]
                        if thread_state is not None
                        and _cancellation_applies(thread_state, run_id)
                        else None
                    ),
                    terminal_committed=terminal is not None,
                    attempts=tuple(
                        AttemptFact(
                            attempt_id=row[0],
                            attempt_generation=int(row[1]),
                            state=row[2],
                            lease_expires_at=row[3],
                        )
                        for row in attempts
                    ),
                    events=tuple(
                        EventFact(
                            seq=int(row[0]),
                            run_id=row[1],
                            kind=row[2],
                            payload=row[3],
                        )
                        for row in events
                    ),
                    projection_revision=int(projection[0]) if projection else None,
                    high_water_seq=int(counter[0]) if counter else 0,
                    cancelled_run_id=(
                        thread_state[2] if thread_state is not None else None
                    ),
                )

    # ----- T4: terminal, projection, sequence and outbox ------------------

    def commit_event(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        kind: str,
        payload: Mapping[str, Any],
        projection: Mapping[str, Any],
        source_position: Mapping[str, Any] | None = None,
        outbox_payload: Mapping[str, Any] | None = None,
    ) -> EventCommit:
        """Commit one event, its projection revision and its outbox intent."""
        thread = normalize_identity("thread_id", thread_id)
        target = normalize_identity("run_id", run_id) if run_id is not None else None
        event_kind = normalize_identity("kind", kind)
        body = _json_object("payload", payload)
        next_projection = _json_object("projection", projection)
        position = (
            _json_object("source_position", source_position)
            if source_position is not None
            else None
        )
        outbox = (
            _json_object("outbox_payload", outbox_payload)
            if outbox_payload is not None
            else None
        )
        with self._connection("commit event") as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (thread,)
                )
                _refuse_deleted_thread(conn, thread)
                return _commit_event_on(
                    conn,
                    thread_id=thread,
                    run_id=target,
                    kind=event_kind,
                    payload=body,
                    projection=next_projection,
                    source_position=position,
                    outbox_payload=outbox,
                )

    def commit_terminal(
        self,
        *,
        run_id: str,
        technical_result: str,
        semantic_result: str | None,
        payload: Mapping[str, Any],
        evidence: Mapping[str, Any],
        projection: Mapping[str, Any],
        outbox_payload: Mapping[str, Any],
        event_payload: Mapping[str, Any] | None = None,
        source_position: Mapping[str, Any] | None = None,
        attempt_id: str | None = None,
        attempt_generation: int | None = None,
    ) -> TerminalCommit:
        """Commit the terminal result, sequence, projection and outbox together.

        A second call for the same run returns the durable prior commit. When
        ``attempt_id`` is supplied the commit is fenced against the current
        attempt generation; an unfenced call is for a process that already
        holds the run's only attempt.
        """
        normalize_identity("technical_result", technical_result)
        semantic = (
            normalize_identity("semantic_result", semantic_result)
            if semantic_result is not None
            else None
        )
        result_payload = _json_object("payload", payload)
        result_evidence = _json_object("evidence", evidence)
        next_projection = _json_object("projection", projection)
        outbox = _json_object("outbox_payload", outbox_payload)
        event_body = (
            _json_object("event_payload", event_payload)
            if event_payload is not None
            else None
        )
        position = (
            _json_object("source_position", source_position)
            if source_position is not None
            else None
        )
        with self._connection("commit terminal") as conn:
            with conn.transaction():
                located = conn.execute(
                    "SELECT thread_id FROM fin_runtime_run WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                if located is None:
                    raise UnknownOperation(run_id, kind="run")
                thread_id = located[0]
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), 1)", (thread_id,)
                )
                _refuse_deleted_thread(conn, thread_id)
                # Lock order: thread reservation -> run/attempt authority.
                conn.execute(
                    "SELECT 1 FROM fin_runtime_thread_reservation "
                    "WHERE thread_id = %s FOR UPDATE",
                    (thread_id,),
                )
                run = conn.execute(
                    "SELECT state, attempt_generation FROM fin_runtime_run "
                    "WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise UnknownOperation(run_id, kind="run")
                existing = _read_terminal_on(conn, run_id)
                if existing is not None:
                    outbox_id = conn.execute(
                        "SELECT outbox_id FROM fin_runtime_outbox "
                        "WHERE thread_id = %s AND seq = %s",
                        (existing.thread_id, existing.seq),
                    ).fetchone()
                    return TerminalCommit(
                        run_id=existing.run_id,
                        thread_id=existing.thread_id,
                        seq=existing.seq,
                        revision=existing.revision,
                        high_water_seq=existing.seq,
                        outbox_id=outbox_id[0] if outbox_id else None,
                        committed_at=existing.committed_at,
                        replayed=True,
                    )
                if run[0] in TERMINAL_RUN_STATES:
                    raise CheckpointOwnershipLost(
                        OWNERSHIP_RUN_NOT_RUNNING,
                        f"run {run_id} already ended as {run[0]}",
                    )
                if attempt_id is not None:
                    attempt = conn.execute(
                        "SELECT attempt_generation, state, "
                        "lease_expires_at > now() "
                        "FROM fin_runtime_worker_attempt WHERE attempt_id = %s "
                        "AND run_id = %s FOR UPDATE",
                        (attempt_id, run_id),
                    ).fetchone()
                    if attempt is None:
                        raise CheckpointOwnershipLost(
                            OWNERSHIP_ATTEMPT_MISSING, f"attempt {attempt_id}"
                        )
                    expected = (
                        int(attempt_generation)
                        if attempt_generation is not None
                        else int(attempt[0])
                    )
                    if int(attempt[0]) != expected:
                        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_GENERATION)
                    if attempt[1] != ATTEMPT_ACTIVE or not bool(attempt[2]):
                        raise CheckpointOwnershipLost(OWNERSHIP_LEASE_EXPIRED)
                    if int(run[1]) != expected:
                        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_GENERATION)
                seq = _allocate_event_seq(conn, thread_id)
                revision = _bump_projection(conn, thread_id, next_projection, seq)
                (committed_at,) = conn.execute(
                    "INSERT INTO fin_runtime_terminal "
                    "(run_id, thread_id, technical_result, semantic_result, "
                    "payload, evidence, seq, revision) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING committed_at",
                    (
                        run_id,
                        thread_id,
                        technical_result,
                        semantic,
                        _jsonb(result_payload),
                        _jsonb(result_evidence),
                        seq,
                        revision,
                    ),
                ).fetchone()
                _insert_event_on(
                    conn,
                    thread_id=thread_id,
                    seq=seq,
                    run_id=run_id,
                    kind="terminal",
                    payload=event_body if event_body is not None else result_payload,
                    source_position=position,
                )
                outbox_id = _insert_outbox_on(
                    conn, thread_id=thread_id, seq=seq, payload=outbox
                )
                _end_run_on(
                    conn,
                    run_id=run_id,
                    thread_id=thread_id,
                    outcome="completed",
                    result_payload={
                        "run_id": run_id,
                        "state": "completed",
                        "technical_result": technical_result,
                        "semantic_result": semantic,
                        "seq": seq,
                    },
                )
                return TerminalCommit(
                    run_id=run_id,
                    thread_id=thread_id,
                    seq=seq,
                    revision=revision,
                    high_water_seq=seq,
                    outbox_id=outbox_id,
                    committed_at=committed_at,
                    replayed=False,
                )

    def read_terminal(self, run_id: str) -> TerminalRecord | None:
        with self._connection("read terminal") as conn:
            return _read_terminal_on(conn, run_id)

    def read_snapshot(
        self, *, thread_id: str, after_seq: int | None = None
    ) -> Snapshot:
        """Read projection, revision and events from one consistent snapshot."""
        if after_seq is not None and after_seq < 0:
            raise InvalidOperation("after_seq", "malformed")
        cursor = after_seq if after_seq is not None else 0
        with self._connection("read snapshot") as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                projection = conn.execute(
                    "SELECT revision, projection, high_water_seq "
                    "FROM fin_runtime_thread_projection WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                counter = conn.execute(
                    "SELECT next_event_seq FROM fin_runtime_thread_counter "
                    "WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                bounds = conn.execute(
                    "SELECT min(seq) FROM fin_runtime_thread_event "
                    "WHERE thread_id = %s",
                    (thread_id,),
                ).fetchone()
                events = conn.execute(
                    "SELECT seq, run_id, kind, payload, source_position, created_at "
                    "FROM fin_runtime_thread_event "
                    "WHERE thread_id = %s AND seq > %s ORDER BY seq ASC",
                    (thread_id, cursor),
                ).fetchall()
        high_water = int(counter[0]) if counter else 0
        event_rows = tuple(
            ThreadEventRecord(
                seq=int(row[0]),
                run_id=row[1],
                kind=row[2],
                payload=row[3],
                source_position=row[4],
                created_at=row[5],
            )
            for row in events
        )
        reset_reason = _continuity_problem(
            cursor=cursor,
            high_water=high_water,
            projection=projection,
            bounds=bounds,
            returned=len(event_rows),
        )
        return Snapshot(
            thread_id=thread_id,
            projection=projection[1] if projection else {},
            revision=int(projection[0]) if projection else 0,
            high_water_seq=high_water,
            events=event_rows,
            reset_required=reset_reason is not None,
            reset_reason=reset_reason,
        )

    def pending_outbox(self, *, limit: int = 100) -> tuple[OutboxRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connection("read outbox") as conn:
            rows = conn.execute(
                "SELECT outbox_id, thread_id, seq, payload, created_at, "
                "published_at FROM fin_runtime_outbox "
                "WHERE published_at IS NULL ORDER BY thread_id, seq LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(
            OutboxRecord(
                outbox_id=row[0],
                thread_id=row[1],
                seq=int(row[2]),
                payload=row[3],
                created_at=row[4],
                published_at=row[5],
            )
            for row in rows
        )

    def mark_outbox_published(self, outbox_ids: Sequence[str]) -> int:
        if not outbox_ids:
            return 0
        with self._connection("publish outbox") as conn:
            with conn.transaction():
                updated = conn.execute(
                    "UPDATE fin_runtime_outbox SET published_at = now() "
                    "WHERE outbox_id = ANY(%s) AND published_at IS NULL",
                    (list(outbox_ids),),
                ).rowcount
        return int(updated)

    # ----- T5: parser inbox ----------------------------------------------

    def record_parser_completion(
        self,
        *,
        job_id: str,
        attachment_id: str,
        corpus_id: str,
        notification: Mapping[str, Any],
        inbox_id: str | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> ParserInboxRecord:
        """Durably record a completion notice as pending consumption.

        The caller acknowledges the parser only after this returns. A
        redelivery with the same ``(job_id, attachment_id)`` returns the
        durable prior row and never creates a second entry.
        """
        normalize_identity("job_id", job_id)
        normalize_identity("attachment_id", attachment_id)
        normalize_identity("corpus_id", corpus_id)
        body = normalize_payload(
            "notification", notification, max_bytes=max_payload_bytes
        )
        identifier = inbox_id or str(uuid.uuid4())
        with self._connection("record parser completion") as conn:
            with conn.transaction():
                row = conn.execute(
                    "INSERT INTO fin_runtime_parser_inbox "
                    "(inbox_id, job_id, attachment_id, corpus_id, notification, "
                    "state) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (job_id, attachment_id) DO NOTHING "
                    f"RETURNING {_PARSER_INBOX_COLUMNS}",
                    (
                        identifier,
                        job_id,
                        attachment_id,
                        corpus_id,
                        _jsonb(body),
                        PARSER_INBOX_PENDING,
                    ),
                ).fetchone()
                if row is not None:
                    return _row_to_parser_inbox(row)
                prior = conn.execute(
                    f"SELECT {_PARSER_INBOX_COLUMNS} FROM fin_runtime_parser_inbox "
                    "WHERE job_id = %s AND attachment_id = %s",
                    (job_id, attachment_id),
                ).fetchone()
                if prior is None:  # pragma: no cover - ON CONFLICT guarantees one
                    raise RuntimeUnavailable("record parser completion")
                return _row_to_parser_inbox(prior, replay=True)

    def pending_parser_completions(
        self, *, limit: int = 100
    ) -> tuple[ParserInboxRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connection("read parser inbox") as conn:
            rows = conn.execute(
                f"SELECT {_PARSER_INBOX_COLUMNS} FROM fin_runtime_parser_inbox "
                "WHERE state = %s ORDER BY received_at ASC, inbox_id ASC LIMIT %s",
                (PARSER_INBOX_PENDING, limit),
            ).fetchall()
        return tuple(_row_to_parser_inbox(row) for row in rows)

    def reconcile_parser_completion(
        self, *, inbox_id: str, authority_state: str
    ) -> ParserInboxRecord:
        """Record parser-owned truth; readiness is derived, never inferred.

        Only ``succeeded`` authority makes the entry ready for adoption, and
        only the reconcile call writes it. A late duplicate returns the row
        as already reconciled.
        """
        normalize_identity("inbox_id", inbox_id)
        normalize_identity("authority_state", authority_state)
        with self._connection("reconcile parser completion") as conn:
            with conn.transaction():
                row = conn.execute(
                    f"SELECT {_PARSER_INBOX_COLUMNS} FROM fin_runtime_parser_inbox "
                    "WHERE inbox_id = %s FOR UPDATE",
                    (inbox_id,),
                ).fetchone()
                if row is None:
                    raise UnknownOperation(inbox_id, kind="parser inbox entry")
                current = _row_to_parser_inbox(row)
                if current.state == PARSER_INBOX_RECONCILED:
                    return replace(current, replay=True)
                updated = conn.execute(
                    "UPDATE fin_runtime_parser_inbox SET state = %s, "
                    "reconciled_at = now(), authority_state = %s "
                    "WHERE inbox_id = %s AND state = %s "
                    f"RETURNING {_PARSER_INBOX_COLUMNS}",
                    (
                        PARSER_INBOX_RECONCILED,
                        authority_state,
                        inbox_id,
                        PARSER_INBOX_PENDING,
                    ),
                ).fetchone()
                if updated is None:  # pragma: no cover - locked row cannot move
                    raise RuntimeUnavailable("reconcile parser completion")
                return _row_to_parser_inbox(updated)


# ----- shared SQL units ----------------------------------------------------


def _allocate_event_seq(conn: Any, thread_id: str) -> int:
    """Allocate the next thread-wide sequence inside the caller's transaction.

    The counter row lock is held until commit, so a rollback returns the
    number too and the committed sequence never has a hole.
    """
    conn.execute(
        "INSERT INTO fin_runtime_thread_counter "
        "(thread_id, next_fifo, next_event_seq, updated_at) "
        "VALUES (%s, 1, 0, now()) ON CONFLICT (thread_id) DO NOTHING",
        (thread_id,),
    )
    row = conn.execute(
        "UPDATE fin_runtime_thread_counter SET "
        "next_event_seq = next_event_seq + 1, updated_at = now() "
        "WHERE thread_id = %s RETURNING next_event_seq",
        (thread_id,),
    ).fetchone()
    return int(row[0])


def _bump_projection(
    conn: Any, thread_id: str, projection: Mapping[str, Any], seq: int
) -> int:
    row = conn.execute(
        "INSERT INTO fin_runtime_thread_projection "
        "(thread_id, revision, projection, high_water_seq, updated_at) "
        "VALUES (%s, 1, %s, %s, now()) "
        "ON CONFLICT (thread_id) DO UPDATE SET "
        "revision = fin_runtime_thread_projection.revision + 1, "
        "projection = EXCLUDED.projection, "
        "high_water_seq = EXCLUDED.high_water_seq, updated_at = now() "
        "RETURNING revision",
        (thread_id, _jsonb(dict(projection)), seq),
    ).fetchone()
    return int(row[0])


def _insert_event_on(
    conn: Any,
    *,
    thread_id: str,
    seq: int,
    run_id: str | None,
    kind: str,
    payload: Mapping[str, Any],
    source_position: Mapping[str, Any] | None,
) -> datetime:
    (created_at,) = conn.execute(
        "INSERT INTO fin_runtime_thread_event "
        "(thread_id, seq, run_id, kind, payload, source_position) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING created_at",
        (
            thread_id,
            seq,
            run_id,
            kind,
            _jsonb(dict(payload)),
            _jsonb(dict(source_position)) if source_position is not None else None,
        ),
    ).fetchone()
    return created_at


def _insert_outbox_on(
    conn: Any, *, thread_id: str, seq: int, payload: Mapping[str, Any]
) -> str:
    outbox_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO fin_runtime_outbox (outbox_id, thread_id, seq, payload) "
        "VALUES (%s, %s, %s, %s)",
        (outbox_id, thread_id, seq, _jsonb(dict(payload))),
    )
    return outbox_id


def _commit_event_on(
    conn: Any,
    *,
    thread_id: str,
    run_id: str | None,
    kind: str,
    payload: Mapping[str, Any],
    projection: Mapping[str, Any],
    source_position: Mapping[str, Any] | None,
    outbox_payload: Mapping[str, Any] | None,
) -> EventCommit:
    seq = _allocate_event_seq(conn, thread_id)
    revision = _bump_projection(conn, thread_id, projection, seq)
    created_at = _insert_event_on(
        conn,
        thread_id=thread_id,
        seq=seq,
        run_id=run_id,
        kind=kind,
        payload=payload,
        source_position=source_position,
    )
    outbox_id = (
        _insert_outbox_on(conn, thread_id=thread_id, seq=seq, payload=outbox_payload)
        if outbox_payload is not None
        else None
    )
    return EventCommit(
        thread_id=thread_id,
        seq=seq,
        revision=revision,
        high_water_seq=seq,
        outbox_id=outbox_id,
        created_at=created_at,
    )


def _assert_end_ownership(
    conn: Any,
    *,
    run_id: str,
    attempt_id: str,
    attempt_generation: int,
) -> None:
    """Prove a caller ending a run still owns its current worker attempt.

    Runs after the run and reservation are already locked in this
    transaction, so a concurrent takeover either finished before this read
    (and the generation no longer matches) or waits behind these locks.
    """
    run = conn.execute(
        "SELECT state FROM fin_runtime_run WHERE run_id = %s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if run is None:
        raise CheckpointOwnershipLost(OWNERSHIP_RUN_MISSING)
    attempt = conn.execute(
        "SELECT run_id, attempt_generation, state, lease_expires_at > now() "
        "FROM fin_runtime_worker_attempt WHERE attempt_id = %s "
        "AND run_id = %s FOR UPDATE",
        (attempt_id, run_id),
    ).fetchone()
    if attempt is None:
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_MISSING)
    if int(attempt[1]) != int(attempt_generation):
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_GENERATION)
    if attempt[2] != ATTEMPT_ACTIVE:
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_NOT_ACTIVE)
    if not bool(attempt[3]):
        raise CheckpointOwnershipLost(OWNERSHIP_LEASE_EXPIRED)


def _end_run_on(
    conn: Any,
    *,
    run_id: str,
    thread_id: str,
    outcome: str,
    result_payload: Mapping[str, Any],
) -> tuple[bool, int]:
    reservation_released = (
        conn.execute(
            "UPDATE fin_runtime_thread_reservation SET state = %s, "
            "updated_at = now() "
            "WHERE thread_id = %s AND run_id = %s AND state = %s",
            (RESERVATION_RELEASED, thread_id, run_id, RESERVATION_RESERVED),
        ).rowcount
        > 0
    )
    attempts_released = conn.execute(
        "UPDATE fin_runtime_worker_attempt SET state = %s, updated_at = now() "
        "WHERE run_id = %s AND state = %s",
        (ATTEMPT_RELEASED, run_id, ATTEMPT_ACTIVE),
    ).rowcount
    conn.execute(
        "UPDATE fin_runtime_run SET state = %s, updated_at = now() WHERE run_id = %s",
        (outcome, run_id),
    )
    conn.execute(
        "UPDATE fin_runtime_operation SET state = %s, "
        "result = COALESCE(result, %s) WHERE run_id = %s",
        (outcome, _jsonb(dict(result_payload)), run_id),
    )
    return reservation_released, int(attempts_released)


def _read_terminal_on(conn: Any, run_id: str) -> TerminalRecord | None:
    row = conn.execute(
        "SELECT run_id, thread_id, technical_result, semantic_result, payload, "
        "evidence, seq, revision, committed_at FROM fin_runtime_terminal "
        "WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return TerminalRecord(
        run_id=row[0],
        thread_id=row[1],
        technical_result=row[2],
        semantic_result=row[3],
        payload=row[4],
        evidence=row[5],
        seq=int(row[6]),
        revision=int(row[7]),
        committed_at=row[8],
    )


def _continuity_problem(
    *,
    cursor: int,
    high_water: int,
    projection: Sequence[Any] | None,
    bounds: Sequence[Any],
    returned: int,
) -> str | None:
    """Return the reset reason when continuity cannot be proven."""
    if cursor > high_water:
        return RESET_CURSOR_AHEAD
    if projection is None:
        if high_water == 0:
            return None
        return RESET_PROJECTION_MISSING
    if int(projection[2]) != high_water:
        return RESET_SEQUENCE_INCONSISTENT
    minimum = bounds[0]
    if minimum is not None and cursor != 0 and cursor < int(minimum) - 1:
        return RESET_EVENT_GAP
    if returned != high_water - cursor:
        return RESET_EVENT_GAP
    return None


# ----- T3 seam: ownership assertion ---------------------------------------


def assert_ownership(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    attempt_id: str,
    attempt_generation: int,
    thread_generation: int,
) -> None:
    """Validate current ownership on the caller's connection and transaction.

    The locks are taken in the shared order — thread reservation, then the
    run/attempt authority — so this can run immediately before a checkpoint
    write in the same transaction. Every mismatch raises
    :class:`CheckpointOwnershipLost`; nothing is silently tolerated.
    """
    # The fence is read first, without a lock, so a cancelled or deleted
    # thread is reported as such instead of as a missing reservation. The
    # locked checks below repeat it, because the fence may land while this
    # transaction runs.
    fenced = cur.execute(
        "SELECT tombstoned, cancellation_reason, cancelled_run_id "
        "FROM fin_runtime_thread_state WHERE thread_id = %s",
        (thread_id,),
    ).fetchone()
    if fenced is not None and bool(fenced[0]):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_TOMBSTONED)
    if _cancellation_applies(fenced, run_id):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_CANCELLED)
    reservation = cur.execute(
        "SELECT run_id, thread_generation, state "
        "FROM fin_runtime_thread_reservation "
        "WHERE thread_id = %s FOR UPDATE",
        (thread_id,),
    ).fetchone()
    if reservation is None:
        raise CheckpointOwnershipLost(OWNERSHIP_RESERVATION_MISSING)
    if reservation[2] != RESERVATION_RESERVED:
        raise CheckpointOwnershipLost(OWNERSHIP_RESERVATION_RELEASED)
    if reservation[0] != run_id:
        raise CheckpointOwnershipLost(OWNERSHIP_RESERVATION_OTHER_RUN)
    if int(reservation[1]) != int(thread_generation):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_GENERATION)
    run = cur.execute(
        "SELECT thread_id, state, attempt_generation, thread_generation "
        "FROM fin_runtime_run WHERE run_id = %s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if run is None or run[0] != thread_id:
        raise CheckpointOwnershipLost(OWNERSHIP_RUN_MISSING)
    if run[1] != RUN_RUNNING:
        raise CheckpointOwnershipLost(OWNERSHIP_RUN_NOT_RUNNING)
    if int(run[2]) != int(attempt_generation):
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_GENERATION)
    if int(run[3]) != int(thread_generation):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_GENERATION)
    attempt = cur.execute(
        "SELECT run_id, attempt_generation, state, lease_expires_at > now() "
        "FROM fin_runtime_worker_attempt WHERE attempt_id = %s FOR UPDATE",
        (attempt_id,),
    ).fetchone()
    if attempt is None or attempt[0] != run_id:
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_MISSING)
    if int(attempt[1]) != int(attempt_generation):
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_GENERATION)
    if attempt[2] != ATTEMPT_ACTIVE:
        raise CheckpointOwnershipLost(OWNERSHIP_ATTEMPT_NOT_ACTIVE)
    if not bool(attempt[3]):
        raise CheckpointOwnershipLost(OWNERSHIP_LEASE_EXPIRED)
    thread_state = cur.execute(
        "SELECT tombstoned, cancellation_reason, cancelled_run_id "
        "FROM fin_runtime_thread_state WHERE thread_id = %s FOR SHARE",
        (thread_id,),
    ).fetchone()
    if thread_state is not None and bool(thread_state[0]):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_TOMBSTONED)
    if _cancellation_applies(thread_state, run_id):
        raise CheckpointOwnershipLost(OWNERSHIP_THREAD_CANCELLED)


def default_attempt_id() -> str:
    """One fresh fenced attempt identity."""
    return str(uuid.uuid4())
