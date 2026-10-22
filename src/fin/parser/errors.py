"""Typed error taxonomy of the parser bounded context.

Every parser failure is raised as one of these classes. Each class owns
a stable machine-readable ``code`` so the HTTP surface can answer with
``{"error": "<code>"}`` without re-deriving a token from a message, and
so callers branch on the exception TYPE rather than on a bare string.

Failure is never signalled by returning ``None``, ``-1`` or ``""``: a
call that cannot finish raises. The code belongs to the class, so it
cannot drift per call site.
"""

from __future__ import annotations

from typing import Any, ClassVar


class ParserError(RuntimeError):
    """Base class of every parser failure.

    ``message`` is the human-readable explanation; ``details`` carries
    structured context for logs and internal handling only (it never
    reaches an API body, which carries the code alone).
    """

    code: ClassVar[str] = "parser_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details)


class ParserInvalidRequestError(ParserError):
    """The request identity or input is empty, missing, or malformed."""

    code: ClassVar[str] = "invalid_request"


class ParserJobConflictError(ParserError):
    """An idempotency key was reused with a different input."""

    code: ClassVar[str] = "job_conflict"


class ParserJobNotFoundError(ParserError):
    """No job exists for the requested job id."""

    code: ClassVar[str] = "job_not_found"


class ParserJobCapacityError(ParserError):
    """The open-job limit would be exceeded by accepting this work."""

    code: ClassVar[str] = "job_capacity"


class ParserJobCancelledError(ParserError):
    """The job was cancelled, so it may not proceed or commit."""

    code: ClassVar[str] = "job_cancelled"


class ParserQueueFullError(ParserError):
    """The pending-queue cap would be exceeded by accepting this work.

    The refusal carries the cap and the pending count it saw, so the
    caller can retry with guidance instead of guessing.
    """

    code: ClassVar[str] = "parser_queue_full"


class ParserQueueWaitTimeoutError(ParserError):
    """The job waited longer than the queue-wait deadline.

    The job is not claimable any more and becomes ``timed_out``.
    """

    code: ClassVar[str] = "parser_queue_wait_timeout"


class ParserExecutionTimeoutError(ParserError):
    """The attempt ran longer than the execution deadline.

    The result is refused and the job becomes ``timed_out``.
    """

    code: ClassVar[str] = "parser_execution_timeout"


class ParserJobTombstonedError(ParserError):
    """The job was deleted; its tombstone blocks any replay."""

    code: ClassVar[str] = "job_tombstoned"


class ParserStaleFenceError(ParserError):
    """The attempt's fencing token is no longer the effective one."""

    code: ClassVar[str] = "stale_fence"


class ParserPersistenceError(ParserError):
    """The parser-owned store could not be read or written."""

    code: ClassVar[str] = "persistence_unavailable"


class ParserEnvelopeError(ParserError):
    """A parse result does not satisfy the envelope contract."""

    code: ClassVar[str] = "envelope_invalid"


class ParserNotificationError(ParserError):
    """A notification could not be published, received, or reconciled."""

    code: ClassVar[str] = "notification_failed"


__all__ = [
    "ParserEnvelopeError",
    "ParserError",
    "ParserExecutionTimeoutError",
    "ParserInvalidRequestError",
    "ParserJobCancelledError",
    "ParserJobCapacityError",
    "ParserJobConflictError",
    "ParserJobNotFoundError",
    "ParserJobTombstonedError",
    "ParserNotificationError",
    "ParserPersistenceError",
    "ParserQueueFullError",
    "ParserQueueWaitTimeoutError",
    "ParserStaleFenceError",
]
