"""The process-local ingest job store: same port, no durability promise.

Transition legality, busy detection and audit-row shape all come from
:mod:`fin.corpus.ingest_state_model` so this store and the durable
Postgres store cannot diverge on the rules; only the storage mechanics
(live dicts under a lock) live here.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Mapping
from typing import Any, NoReturn

from fin.corpus.ingest_state_errors import (
    AuditOverflowError,
    DuplicateJobError,
    IngestStateError,
    IngestStepError,
    UnknownJobError,
)
from fin.corpus.ingest_state_model import (
    ALL_DOCUMENTS,
    AllDocuments,
    IngestAuditRow,
    IngestJob,
    IngestState,
    StepFailure,
    TransitionRequest,
    coerce_request,
    coerce_state_filter,
    event_audit,
    failure_guard,
    job_created_audit,
    job_superseded_audit,
    rejection_audit,
    replacement_guard,
    step_failed_audit,
    transition_audit,
    transition_guard,
    utc_now,
    validate_document_id,
)


class InMemoryIngestStateStore:
    """Thread-safe local state and audit store.

    The read/validate/write transition is atomic. A concurrent duplicate
    transition is rejected as a typed busy error and remains auditable.
    """

    def __init__(self, *, max_audit_entries: int | None = None) -> None:
        if max_audit_entries is not None and max_audit_entries < 0:
            raise ValueError("max_audit_entries must be non-negative")
        self._jobs: dict[str, IngestJob] = {}
        self._audit: list[IngestAuditRow] = []
        self._max_audit_entries = max_audit_entries
        self._lock = threading.RLock()

    def create_job(
        self,
        document_id: str,
        *,
        actor: str | None = None,
    ) -> IngestJob:
        """Create a pending job and audit its creation."""
        validate_document_id(document_id)
        request = TransitionRequest(
            to_state=IngestState.PENDING, actor=actor
        )
        with self._lock:
            if document_id in self._jobs:
                raise DuplicateJobError(
                    f"ingest job already exists for {document_id!r}"
                )
            now = utc_now()
            self._append_audit(job_created_audit(document_id, request, now))
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
        validate_document_id(document_id)
        with self._lock:
            return self._require_job(document_id)

    def list_jobs(
        self,
        *,
        states: Collection[IngestState | str] | None = None,
    ) -> tuple[IngestJob, ...]:
        """Return a deterministic snapshot, optionally state-filtered."""
        wanted = coerce_state_filter(states)
        with self._lock:
            return tuple(
                self._jobs[document_id]
                for document_id in sorted(self._jobs)
                if wanted is None or self._jobs[document_id].state in wanted
            )

    def list_audit(
        self,
        document_id: str | None | AllDocuments = ALL_DOCUMENTS,
    ) -> tuple[IngestAuditRow, ...]:
        """Return all audit rows or only rows for one document.

        The unfiltered read is ``list_audit()``; an explicit ``None`` is
        refused by the shared identity rule, like a blank id.
        """
        if document_id is not ALL_DOCUMENTS:
            validate_document_id(document_id)
        with self._lock:
            if document_id is ALL_DOCUMENTS:
                return tuple(self._audit)
            return tuple(
                row for row in self._audit if row.document_id == document_id
            )

    def append_audit(
        self,
        document_id: str,
        action: str,
        metadata: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> IngestAuditRow:
        """Append one state-machine-external event row; return it.

        The job's state is deliberately NOT touched: an event records a
        publication fact, and moving or failing a job from an audit
        write would make the audit trail a second state machine.
        """
        validate_document_id(document_id)
        if not isinstance(action, str) or not action.strip():
            raise ValueError("action must not be empty")
        row = event_audit(
            document_id, action, metadata, actor, utc_now()
        )
        with self._lock:
            # The job must exist: an event about nothing is a bug, and
            # appending it would let a warning poll read a document no
            # job ever accepted.
            self._require_job(document_id)
            self._append_audit(row)
        return row

    def transition(
        self,
        document_id: str,
        to_state: IngestState | str,
        *,
        actor: str | None = None,
        expected_state: IngestState | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestState:
        """Atomically transition a job and return its new state."""
        request = coerce_request(
            TransitionRequest(
                to_state=to_state,
                actor=actor,
                expected_state=expected_state,
                metadata=metadata,
            )
        )
        target = request.to_state
        assert isinstance(target, IngestState)  # narrowed by coerce_request
        with self._lock:
            job = self._require_job(document_id)
            error = transition_guard(job, request)
            if error is not None:
                self._reject(job, error, request)
            now = utc_now()
            self._append_audit(transition_audit(job, request, now))
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
        """Mark a non-terminal job failed, audit it, and raise typed."""
        failure = StepFailure(step=step, cause=cause)
        validate_document_id(document_id)
        request = TransitionRequest(
            to_state=IngestState.FAILED, actor=actor, metadata=metadata
        )
        with self._lock:
            job = self._require_job(document_id)
            error = failure_guard(job)
            if error is not None:
                self._reject(job, error, request)
            now = utc_now()
            self._append_audit(
                step_failed_audit(job, failure, request, now)
            )
            self._jobs[document_id] = IngestJob(
                document_id=job.document_id,
                state=IngestState.FAILED,
                created_at=job.created_at,
                updated_at=now,
                error_type=failure.error_type,
                error_message=failure.error_message,
            )
        raise IngestStepError(document_id, step, cause) from cause

    def supersede_job(
        self,
        document_id: str,
        *,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestJob:
        """Open the next ``pending`` generation of a ready document's job.

        ``READY`` is terminal for the version it completed, so a settled
        source replacement is not a transition of that generation: the row
        moves back to ``PENDING`` with fresh timestamps while the superseded
        generation stays in the audit trail.
        """
        validate_document_id(document_id)
        request = TransitionRequest(
            to_state=IngestState.PENDING, actor=actor, metadata=metadata
        )
        with self._lock:
            job = self._require_job(document_id)
            error = replacement_guard(job)
            if error is not None:
                self._reject(job, error, request)
            now = utc_now()
            self._append_audit(job_superseded_audit(document_id, request, now))
            job = IngestJob(
                document_id=job.document_id,
                state=IngestState.PENDING,
                created_at=now,
                updated_at=now,
            )
            self._jobs[document_id] = job
            return job

    def _require_job(self, document_id: str) -> IngestJob:
        try:
            return self._jobs[document_id]
        except KeyError as exc:
            raise UnknownJobError(document_id) from exc

    def _append_audit(self, row: IngestAuditRow) -> None:
        if (
            self._max_audit_entries is not None
            and len(self._audit) >= self._max_audit_entries
        ):
            raise AuditOverflowError(self._max_audit_entries)
        self._audit.append(row)

    def _reject(
        self,
        job: IngestJob,
        error: IngestStateError,
        request: TransitionRequest,
    ) -> NoReturn:
        """Audit a refused operation in memory, then raise it."""
        self._append_audit(rejection_audit(job, error, request, utc_now()))
        raise error
