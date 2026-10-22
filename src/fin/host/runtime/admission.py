"""Admission door for durable client operations and worker ownership.

The coordinator validates caller input once, builds the run identity and
delegates the durable decision to the store. Validation never writes; the
store commits the operation, its FIFO position, its run and its notification
intent before :meth:`RuntimeCoordinator.accept` returns.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Callable, Mapping

from fin.host.runtime.recovery import recover_run
from fin.host.runtime.schema import (
    RUN_ABANDONED,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_INTERRUPTED,
    AcceptanceRecord,
    ClaimOutcome,
    InvalidOperation,
    ParserInboxRecord,
    RecoveryFacts,
    RenewalOutcome,
    RunEndOutcome,
    RunEndRecord,
    ThreadFenceRecord,
    UnknownOperation,
    normalize_identity,
    normalize_payload,
    payload_digest,
)
from fin.host.runtime.store import DEFAULT_MAX_PAYLOAD_BYTES, PostgresRuntimeStore

_RUN_END_OUTCOMES = frozenset(
    {RUN_COMPLETED, RUN_CANCELLED, RUN_ABANDONED, RUN_INTERRUPTED}
)


class RuntimeCoordinator:
    """The single application-facing entry to durable Runtime state.

    ``queue_capacity`` bounds the number of operations a thread may hold
    in the accepted state; ``lease_seconds`` bounds one worker attempt.
    Both are configuration, so an impossible value is a ``ValueError`` at
    construction, not a request outcome.
    """

    def __init__(
        self,
        store: PostgresRuntimeStore,
        *,
        queue_capacity: int = 8,
        lease_seconds: float = 30.0,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be at least 1")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self._store = store
        self._queue_capacity = queue_capacity
        self._lease_seconds = lease_seconds
        self._max_payload_bytes = max_payload_bytes
        self._run_id_factory = run_id_factory or (lambda: str(uuid.uuid4()))

    @property
    def store(self) -> PostgresRuntimeStore:
        return self._store

    @property
    def queue_capacity(self) -> int:
        return self._queue_capacity

    def accept(
        self,
        *,
        operation_id: str,
        thread_id: str,
        accepted_input: Mapping[str, object],
        notification_intent: Mapping[str, object] | None = None,
        run_id: str | None = None,
    ) -> AcceptanceRecord:
        """Durably accept one operation, or return its prior durable result.

        ``run_id`` lets a door bind the operation to the run identity it
        already owns (a resumed or continued run); ``None`` mints a fresh
        one from the configured factory.
        """
        operation = normalize_identity("operation_id", operation_id)
        thread = normalize_identity("thread_id", thread_id)
        body = normalize_payload(
            "accepted_input", accepted_input, max_bytes=self._max_payload_bytes
        )
        intent = (
            normalize_payload(
                "notification_intent",
                notification_intent,
                max_bytes=self._max_payload_bytes,
            )
            if notification_intent is not None
            else {}
        )
        return self._store.accept_operation(
            operation_id=operation,
            thread_id=thread,
            payload_digest=payload_digest(body),
            accepted_input=body,
            notification_intent=intent,
            run_id=(
                self._run_id_factory()
                if run_id is None
                else normalize_identity("run_id", run_id)
            ),
            capacity=self._queue_capacity,
        )

    def replay(self, *, operation_id: str) -> AcceptanceRecord:
        """Return the accepted operation; never writes and never re-runs."""
        operation = normalize_identity("operation_id", operation_id)
        record = self._store.read_operation(operation)
        if record is None:
            raise UnknownOperation(operation)
        return replace(record, replay=True)

    def claim(
        self,
        *,
        thread_id: str,
        worker_id: str,
        run_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> ClaimOutcome:
        thread = normalize_identity("thread_id", thread_id)
        worker = normalize_identity("worker_id", worker_id)
        target = normalize_identity("run_id", run_id) if run_id is not None else None
        return self._store.claim_run(
            thread_id=thread,
            worker_id=worker,
            run_id=target,
            lease_seconds=self._lease_seconds if lease_seconds is None else lease_seconds,
            attempt_id=str(uuid.uuid4()),
        )

    def wait(self, *, attempt_id: str) -> bool:
        """Release the worker lease while keeping the thread reservation."""
        return self._store.release_attempt(
            attempt_id=normalize_identity("attempt_id", attempt_id)
        )

    def renew(
        self, *, attempt_id: str, lease_seconds: float | None = None
    ) -> RenewalOutcome:
        return self._store.renew_attempt(
            attempt_id=normalize_identity("attempt_id", attempt_id),
            lease_seconds=self._lease_seconds if lease_seconds is None else lease_seconds,
        )

    def end(
        self,
        *,
        run_id: str,
        outcome: RunEndOutcome,
        attempt_id: str | None = None,
        attempt_generation: int | None = None,
    ) -> RunEndRecord:
        """End a run; a supplied attempt identity fences a stale worker out."""
        if outcome not in _RUN_END_OUTCOMES:
            raise InvalidOperation("outcome", "malformed")
        return self._store.end_run(
            run_id=normalize_identity("run_id", run_id),
            outcome=outcome,
            attempt_id=(
                normalize_identity("attempt_id", attempt_id)
                if attempt_id is not None
                else None
            ),
            attempt_generation=attempt_generation,
        )

    def recover(self, *, run_id: str) -> RecoveryFacts:
        return recover_run(
            self._store, normalize_identity("run_id", run_id)
        )

    def mark_thread_cancelled(
        self, *, thread_id: str, reason: str, run_id: str | None = None
    ) -> ThreadFenceRecord:
        """Fence checkpoint writes for one thread without deleting it.

        With ``run_id`` only that run is fenced; without it the whole thread
        is, as before.
        """
        return self._store.mark_thread_cancelled(
            thread_id=normalize_identity("thread_id", thread_id),
            reason=reason,
            run_id=normalize_identity("run_id", run_id) if run_id is not None else None,
        )

    def tombstone_thread(self, *, thread_id: str) -> ThreadFenceRecord:
        """Fence a deleted thread: release its reservation and live leases."""
        return self._store.tombstone_thread(
            thread_id=normalize_identity("thread_id", thread_id)
        )

    # ----- parser inbox ---------------------------------------------------

    def record_parser_completion(
        self,
        *,
        job_id: str,
        attachment_id: str,
        corpus_id: str,
        notification: Mapping[str, object],
    ) -> ParserInboxRecord:
        return self._store.record_parser_completion(
            job_id=job_id,
            attachment_id=attachment_id,
            corpus_id=corpus_id,
            notification=notification,
            max_payload_bytes=self._max_payload_bytes,
        )

    def pending_parser_completions(
        self, *, limit: int = 100
    ) -> tuple[ParserInboxRecord, ...]:
        return self._store.pending_parser_completions(limit=limit)

    def reconcile_parser_completion(
        self, *, inbox_id: str, authority_state: str
    ) -> ParserInboxRecord:
        return self._store.reconcile_parser_completion(
            inbox_id=inbox_id, authority_state=authority_state
        )


__all__ = ["RuntimeCoordinator"]
