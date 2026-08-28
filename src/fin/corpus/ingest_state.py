"""Ingest state machine and process-level audit trail.

The ingest pipeline owns the following state graph::

    PENDING -> FETCHING -> EXTRACTING -> OCR -> CATALOGING -> READY
                   |            |          |
                   +------------+----------+----> FAILED

``EXTRACTING`` may also transition directly to ``CATALOGING`` when OCR
is not required. Any non-terminal state may transition to ``FAILED``.
A failed job may be retried by transitioning it to ``FETCHING``. ``READY``
is terminal.

``InMemoryIngestStateStore`` is deliberately an interface-shaped local
implementation. Later wiring can replace it with a Postgres-backed store
without changing the ingest caller: create a job, call ``transition`` for
successful steps, and call ``fail_step`` from the step exception boundary.
Each accepted or rejected operation produces an audit row containing the
process-level actor, action, and UTC timestamp. There is no authentication
or user identity assumption in this local implementation.
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, NoReturn

from fin.corpus.ingest_state_errors import (
    AuditOverflowError,
    DuplicateJobError,
    IllegalTransitionError,
    IngestBusyError,
    IngestStepError,
    InvalidStateError,
    UnknownJobError,
)


class IngestState(str, Enum):
    """Persistable states for one document ingest job."""

    PENDING = "pending"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    OCR = "ocr"
    CATALOGING = "cataloging"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestJob:
    """Current state and last failure details for one document."""

    document_id: str
    state: IngestState
    created_at: datetime
    updated_at: datetime
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class IngestAuditRow:
    """One process-level state-machine audit record."""

    document_id: str
    action: str
    actor: str
    occurred_at: datetime
    from_state: IngestState | None = None
    to_state: IngestState | None = None
    step: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


_ALLOWED_TRANSITIONS: dict[IngestState, frozenset[IngestState]] = {
    IngestState.PENDING: frozenset({IngestState.FETCHING, IngestState.FAILED}),
    IngestState.FETCHING: frozenset(
        {IngestState.EXTRACTING, IngestState.FAILED}
    ),
    IngestState.EXTRACTING: frozenset(
        {IngestState.OCR, IngestState.CATALOGING, IngestState.FAILED}
    ),
    IngestState.OCR: frozenset({IngestState.CATALOGING, IngestState.FAILED}),
    IngestState.CATALOGING: frozenset({IngestState.READY, IngestState.FAILED}),
    IngestState.FAILED: frozenset({IngestState.FETCHING}),
    IngestState.READY: frozenset(),
}


def _process_actor() -> str:
    """Return a stable-enough local actor identity without auth."""
    username = os.environ.get("USER") or "unknown-user"
    return f"{username}@{socket.gethostname()} pid={os.getpid()}"


def _coerce_state(value: IngestState | str) -> IngestState:
    if isinstance(value, IngestState):
        return value
    try:
        return IngestState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidStateError(f"unknown ingest state: {value!r}") from exc


class InMemoryIngestStateStore:
    """Thread-safe in-memory state and audit store for local ingest.

    The lock makes the read/validate/write transition atomic. In a race,
    exactly one caller can move a job into a state; a duplicate attempt is
    reported as ``IngestBusyError`` and remains auditable.
    """

    def __init__(self, *, max_audit_entries: int | None = None) -> None:
        if max_audit_entries is not None and max_audit_entries < 0:
            raise ValueError("max_audit_entries must be non-negative")
        self._jobs: dict[str, IngestJob] = {}
        self._audit: list[IngestAuditRow] = []
        self._max_audit_entries = max_audit_entries
        self._lock = threading.RLock()

    def create_job(self, document_id: str, *, actor: str | None = None) -> IngestJob:
        """Create a pending job and audit its creation."""
        self._validate_document_id(document_id)
        with self._lock:
            if document_id in self._jobs:
                raise DuplicateJobError(
                    f"ingest job already exists for {document_id!r}"
                )
            now = self._now()
            self._append_audit(
                IngestAuditRow(
                    document_id=document_id,
                    action="job_created",
                    actor=actor or _process_actor(),
                    occurred_at=now,
                    to_state=IngestState.PENDING,
                )
            )
            job = IngestJob(
                document_id=document_id,
                state=IngestState.PENDING,
                created_at=now,
                updated_at=now,
            )
            self._jobs[document_id] = job
            return job

    def get_job(self, document_id: str) -> IngestJob:
        """Return a job or raise a typed missing-job error."""
        with self._lock:
            return self._require_job(document_id)

    def list_jobs(self) -> tuple[IngestJob, ...]:
        """Return a deterministic snapshot of all jobs."""
        with self._lock:
            return tuple(
                self._jobs[document_id]
                for document_id in sorted(self._jobs)
            )

    def list_audit(self, document_id: str | None = None) -> tuple[IngestAuditRow, ...]:
        """Return a deterministic snapshot of all or one job's audit rows."""
        with self._lock:
            if document_id is None:
                return tuple(self._audit)
            return tuple(
                row for row in self._audit if row.document_id == document_id
            )

    def transition(
        self,
        document_id: str,
        to_state: IngestState | str,
        *,
        actor: str | None = None,
        expected_state: IngestState | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestState:
        """Atomically transition a job and return its new state.

        ``expected_state`` is an optional optimistic-concurrency guard.
        A mismatch is typed as busy, and the attempted operation still
        receives a rejection audit row.
        """
        target = _coerce_state(to_state)
        expected = (
            _coerce_state(expected_state)
            if expected_state is not None
            else None
        )
        with self._lock:
            job = self._require_job(document_id)
            if expected is not None and job.state is not expected:
                self._reject_busy(
                    job,
                    target,
                    actor=actor,
                    metadata=metadata,
                )
            if target is job.state:
                self._reject_busy(
                    job,
                    target,
                    actor=actor,
                    metadata=metadata,
                )
            if target not in _ALLOWED_TRANSITIONS[job.state]:
                self._reject_illegal(
                    job,
                    target,
                    actor=actor,
                    metadata=metadata,
                )

            now = self._now()
            self._append_audit(
                IngestAuditRow(
                    document_id=document_id,
                    action="transition",
                    actor=actor or _process_actor(),
                    occurred_at=now,
                    from_state=job.state,
                    to_state=target,
                    metadata=dict(metadata or {}),
                )
            )
            self._jobs[document_id] = IngestJob(
                document_id=job.document_id,
                state=target,
                created_at=job.created_at,
                updated_at=now,
            )
            return target

    def fail_step(
        self,
        document_id: str,
        *,
        step: str,
        cause: BaseException,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> NoReturn:
        """Mark a non-ready job failed, audit it, and raise a typed error."""
        if not step.strip():
            raise ValueError("step must not be empty")
        with self._lock:
            job = self._require_job(document_id)
            if IngestState.FAILED not in _ALLOWED_TRANSITIONS[job.state]:
                self._reject_illegal(
                    job,
                    IngestState.FAILED,
                    actor=actor,
                    metadata=metadata,
                )
            now = self._now()
            self._append_audit(
                IngestAuditRow(
                    document_id=document_id,
                    action="step_failed",
                    actor=actor or _process_actor(),
                    occurred_at=now,
                    from_state=job.state,
                    to_state=IngestState.FAILED,
                    step=step,
                    error_type=type(cause).__name__,
                    error_message=str(cause),
                    metadata=dict(metadata or {}),
                )
            )
            self._jobs[document_id] = IngestJob(
                document_id=job.document_id,
                state=IngestState.FAILED,
                created_at=job.created_at,
                updated_at=now,
                error_type=type(cause).__name__,
                error_message=str(cause),
            )
        raise IngestStepError(document_id, step, cause) from cause

    def _require_job(self, document_id: str) -> IngestJob:
        try:
            return self._jobs[document_id]
        except KeyError as exc:
            raise UnknownJobError(document_id) from exc

    @staticmethod
    def _validate_document_id(document_id: str) -> None:
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id must not be empty")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _append_audit(self, row: IngestAuditRow) -> None:
        if (
            self._max_audit_entries is not None
            and len(self._audit) >= self._max_audit_entries
        ):
            raise AuditOverflowError(self._max_audit_entries)
        self._audit.append(row)

    def _reject_busy(
        self,
        job: IngestJob,
        target: IngestState,
        *,
        actor: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> NoReturn:
        error = IngestBusyError(job.document_id, job.state)
        self._append_rejection(
            job,
            target,
            error,
            actor=actor,
            metadata=metadata,
        )
        raise error

    def _reject_illegal(
        self,
        job: IngestJob,
        target: IngestState,
        *,
        actor: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> NoReturn:
        error = IllegalTransitionError(job.document_id, job.state, target)
        self._append_rejection(
            job,
            target,
            error,
            actor=actor,
            metadata=metadata,
        )
        raise error

    def _append_rejection(
        self,
        job: IngestJob,
        target: IngestState,
        error: IngestStateErrorLike,
        *,
        actor: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        self._append_audit(
            IngestAuditRow(
                document_id=job.document_id,
                action="transition_rejected",
                actor=actor or _process_actor(),
                occurred_at=self._now(),
                from_state=job.state,
                to_state=target,
                error_type=type(error).__name__,
                error_message=str(error),
                metadata=dict(metadata or {}),
            )
        )


IngestStateErrorLike = IllegalTransitionError | IngestBusyError


__all__ = [
    "InMemoryIngestStateStore",
    "IngestAuditRow",
    "IngestJob",
    "IngestState",
]
