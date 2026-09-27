"""The state machine's shared vocabulary and the one rule set every store runs.

Both job stores — the in-memory one (:mod:`fin.corpus.ingest_state_memory`)
and the Postgres one (:mod:`fin.corpus.ingest_state_postgres`) — evaluate
transitions through the guards here and build every audit row through the
constructors here. A rule changed in this module changes both backends at
once; a store must not grow transition or audit semantics of its own.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final, NoReturn, Protocol, runtime_checkable

from fin.corpus.ingest_state_errors import (
    IllegalTransitionError,
    IngestBusyError,
    IngestStateError,
    InvalidStateError,
)


class IngestState(str, Enum):
    """Persistable state for one document ingest job."""

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


@runtime_checkable
class IngestStateStore(Protocol):
    """Persistence port used by the ingest step orchestrator."""

    def create_job(
        self,
        document_id: str,
        *,
        actor: str | None = None,
    ) -> IngestJob: ...

    def transition(
        self,
        document_id: str,
        to_state: IngestState | str,
        *,
        actor: str | None = None,
        expected_state: IngestState | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestState: ...

    def get_job(self, document_id: str) -> IngestJob: ...

    def list_jobs(
        self,
        *,
        states: Collection[IngestState | str] | None = None,
    ) -> tuple[IngestJob, ...]:
        """Snapshot the persisted jobs, optionally filtered by state.

        ``states=None`` is the unfiltered read. An iterable keeps only
        jobs in one of those states (an unknown name is the typed
        :class:`InvalidStateError`); an empty iterable selects nothing.
        The ordering is by ``document_id`` so a worker sweep is
        deterministic.
        """
        ...

    def supersede_job(
        self,
        document_id: str,
        *,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestJob: ...

    def fail_step(
        self,
        document_id: str,
        *,
        step: str,
        cause: BaseException,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> NoReturn: ...


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


@dataclass(frozen=True)
class TransitionRequest:
    """The per-call bundle every store carries through the shared guards.

    ``to_state`` is the state the caller asked for; ``expected_state`` and
    ``metadata`` are the compare-and-set guard and the audit payload that
    travel with it. Constructed once at the public store entry so the guard
    and the audit-row builders read exactly the same values.
    """

    to_state: IngestState | str
    actor: str | None = None
    expected_state: IngestState | str | None = None
    metadata: Mapping[str, Any] | None = None

    def resolved_actor(self) -> str:
        """The caller's actor, or this process's identity when unnamed."""
        return self.actor or _process_actor()

    def metadata_snapshot(self) -> Mapping[str, Any]:
        """A private copy of the caller's metadata for one audit row."""
        return dict(self.metadata or {})


@dataclass(frozen=True)
class StepFailure:
    """One failed pipeline step and its cause, validated as it is built.

    Carries the step name through the audit builders without growing their
    signatures; the typed failure raised afterwards is built from the same
    two values, so the audit row and the exception cannot disagree.
    """

    step: str
    cause: BaseException

    def __post_init__(self) -> None:
        validate_step(self.step)

    @property
    def error_type(self) -> str:
        return type(self.cause).__name__

    @property
    def error_message(self) -> str:
        return str(self.cause)


_ALLOWED_TRANSITIONS: dict[IngestState, frozenset[IngestState]] = {
    IngestState.PENDING: frozenset(
        {IngestState.FETCHING, IngestState.FAILED}
    ),
    IngestState.FETCHING: frozenset(
        {IngestState.EXTRACTING, IngestState.FAILED}
    ),
    IngestState.EXTRACTING: frozenset(
        {IngestState.OCR, IngestState.CATALOGING, IngestState.FAILED}
    ),
    IngestState.OCR: frozenset(
        {IngestState.CATALOGING, IngestState.FAILED}
    ),
    IngestState.CATALOGING: frozenset({IngestState.READY, IngestState.FAILED}),
    IngestState.FAILED: frozenset({IngestState.FETCHING}),
    IngestState.READY: frozenset(),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _process_actor() -> str:
    """Return a local process identity without assuming authentication."""
    username = os.environ.get("USER") or os.environ.get("USERNAME")
    return f"{username or 'unknown-user'}@{socket.gethostname()} pid={os.getpid()}"


def coerce_state(value: IngestState | str) -> IngestState:
    if isinstance(value, IngestState):
        return value
    try:
        return IngestState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidStateError(f"unknown ingest state: {value!r}") from exc


def coerce_state_filter(
    states: Collection[IngestState | str] | None,
) -> frozenset[IngestState] | None:
    """Resolve a ``list_jobs`` state filter once, before any store reads.

    ``None`` is the unfiltered read; an unknown state name is refused
    with the same typed error both stores share, and an empty iterable
    selects nothing.
    """
    if states is None:
        return None
    return frozenset(coerce_state(state) for state in states)


def coerce_request(request: TransitionRequest) -> TransitionRequest:
    """Resolve the request's state strings once, before any job is read.

    An unknown state name is refused here, so neither store looks up (or
    locks) a job on its way to a state that cannot exist.
    """
    return replace(
        request,
        to_state=coerce_state(request.to_state),
        expected_state=(
            coerce_state(request.expected_state)
            if request.expected_state is not None
            else None
        ),
    )


def validate_document_id(document_id: str) -> None:
    """The one identity rule every job store shares: a non-blank string."""
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must not be empty")


class AllDocuments:
    """Marker type of the unfiltered ``list_audit`` read.

    Its only value, :data:`ALL_DOCUMENTS`, is the default argument both
    stores give ``list_audit``. Omitting the argument is the unfiltered
    read; passing ``None`` explicitly is a caller bug and is refused by
    :func:`validate_document_id` like a blank id, so no store can mistake
    one for the other.
    """

    def __repr__(self) -> str:
        return "ALL_DOCUMENTS"


ALL_DOCUMENTS: Final[AllDocuments] = AllDocuments()


def validate_step(step: str) -> None:
    """The one step rule every job store shares: a non-blank string."""
    if not isinstance(step, str) or not step.strip():
        raise ValueError("step must not be empty")


def transition_guard(
    job: IngestJob, request: TransitionRequest
) -> IngestStateError | None:
    """The shared transition rules, in the order both stores must apply them.

    Busy first (a stale expected state or a no-op repeat of the current
    state), then legality. Returns the typed error a caller must reject with
    and audit, or ``None`` when the transition may be written.
    """
    target = request.to_state
    expected = request.expected_state
    if (expected is not None and job.state is not expected) or (
        target is job.state
    ):
        return IngestBusyError(job.document_id, job.state)
    if target not in _ALLOWED_TRANSITIONS[job.state]:
        return IllegalTransitionError(job.document_id, job.state, target)
    return None


def failure_guard(job: IngestJob) -> IngestStateError | None:
    """Whether a step failure may move this job to ``FAILED``."""
    if IngestState.FAILED not in _ALLOWED_TRANSITIONS[job.state]:
        return IllegalTransitionError(
            job.document_id, job.state, IngestState.FAILED
        )  # EXIT: a terminal generation records no new failure
    return None


def replacement_guard(job: IngestJob) -> IngestStateError | None:
    """Whether a settled replacement may supersede this job's generation.

    Only ``READY`` ends a version, so any other state is a live pipeline
    that a replacement must not cut short.
    """
    if job.state is not IngestState.READY:
        return IngestBusyError(job.document_id, job.state)
    return None


def job_created_audit(
    document_id: str, request: TransitionRequest, now: datetime
) -> IngestAuditRow:
    return IngestAuditRow(
        document_id=document_id,
        action="job_created",
        actor=request.resolved_actor(),
        occurred_at=now,
        to_state=IngestState.PENDING,
    )


def transition_audit(
    job: IngestJob, request: TransitionRequest, now: datetime
) -> IngestAuditRow:
    return IngestAuditRow(
        document_id=job.document_id,
        action="transition",
        actor=request.resolved_actor(),
        occurred_at=now,
        from_state=job.state,
        to_state=request.to_state,
        metadata=request.metadata_snapshot(),
    )


def rejection_audit(
    job: IngestJob,
    error: IngestStateError,
    request: TransitionRequest,
    now: datetime,
) -> IngestAuditRow:
    """One refused operation, auditable for every store and guard alike."""
    return IngestAuditRow(
        document_id=job.document_id,
        action="transition_rejected",
        actor=request.resolved_actor(),
        occurred_at=now,
        from_state=job.state,
        to_state=request.to_state,
        error_type=type(error).__name__,
        error_message=str(error),
        metadata=request.metadata_snapshot(),
    )


def step_failed_audit(
    job: IngestJob,
    failure: StepFailure,
    request: TransitionRequest,
    now: datetime,
) -> IngestAuditRow:
    return IngestAuditRow(
        document_id=job.document_id,
        action="step_failed",
        actor=request.resolved_actor(),
        occurred_at=now,
        from_state=job.state,
        to_state=IngestState.FAILED,
        step=failure.step,
        error_type=failure.error_type,
        error_message=failure.error_message,
        metadata=request.metadata_snapshot(),
    )


def job_superseded_audit(
    document_id: str, request: TransitionRequest, now: datetime
) -> IngestAuditRow:
    return IngestAuditRow(
        document_id=document_id,
        action="job_superseded",
        actor=request.resolved_actor(),
        occurred_at=now,
        from_state=IngestState.READY,
        to_state=IngestState.PENDING,
        metadata=request.metadata_snapshot(),
    )


__all__ = [
    "ALL_DOCUMENTS",
    "AllDocuments",
    "IngestAuditRow",
    "IngestJob",
    "IngestState",
    "IngestStateStore",
    "StepFailure",
    "TransitionRequest",
    "coerce_state",
    "coerce_state_filter",
    "coerce_request",
    "failure_guard",
    "job_created_audit",
    "job_superseded_audit",
    "rejection_audit",
    "replacement_guard",
    "step_failed_audit",
    "transition_audit",
    "transition_guard",
    "utc_now",
    "validate_document_id",
    "validate_step",
]
