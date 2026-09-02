"""Typed failures for the ingest state machine."""

from __future__ import annotations


class IngestStateError(RuntimeError):
    """Base class for state-machine failures."""


class UnknownJobError(IngestStateError, KeyError):
    """The requested ingest job does not exist."""

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(document_id)


class DuplicateJobError(IngestStateError):
    """A job already exists for the document identifier."""


class InvalidStateError(IngestStateError, ValueError):
    """A state value is not a valid ingest state."""


class IllegalTransitionError(IngestStateError):
    """The requested state transition is not allowed."""

    def __init__(
        self,
        document_id: str,
        from_state: object,
        to_state: object,
    ) -> None:
        self.document_id = document_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal ingest transition for {document_id!r}: "
            f"{from_state!s} -> {to_state!s}"
        )


class IngestBusyError(IngestStateError):
    """A concurrent caller attempted to repeat an active transition."""

    def __init__(self, document_id: str, state: object) -> None:
        self.document_id = document_id
        self.state = state
        super().__init__(
            f"ingest job {document_id!r} is busy in state {state!s}"
        )


class IngestStepError(IngestStateError):
    """A pipeline step failed and the job was marked failed."""

    def __init__(
        self,
        document_id: str,
        step: str,
        cause: BaseException,
    ) -> None:
        self.document_id = document_id
        self.step = step
        self.cause = cause
        super().__init__(
            f"ingest step {step!r} failed for {document_id!r}: "
            f"{type(cause).__name__}: {cause}"
        )


class AuditOverflowError(IngestStateError):
    """The configured audit capacity has been exhausted."""

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max_entries
        super().__init__(
            f"ingest audit capacity exhausted at {max_entries} entries"
        )


__all__ = [
    "AuditOverflowError",
    "DuplicateJobError",
    "IllegalTransitionError",
    "IngestBusyError",
    "IngestStateError",
    "IngestStepError",
    "InvalidStateError",
    "UnknownJobError",
]
