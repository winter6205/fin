"""Typed vocabulary and inline DDL for the durable Runtime tables.

PostgreSQL is the authority for accepted operations, per-thread FIFO order,
thread reservations, worker attempts, terminal state, event projections and
the parser inbox (ADR 0103). The types in this module are the only vocabulary
callers, the store and the recovery projection share; every failure is a typed
exception carrying a stable ``code``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Final, Literal, Mapping

# ----- typed outcome codes -------------------------------------------------

INVALID_OPERATION: Final[str] = "invalid_operation"
OPERATION_CONFLICT: Final[str] = "operation_conflict"
QUEUE_CAPACITY_EXCEEDED: Final[str] = "queue_capacity_exceeded"
RUNTIME_UNAVAILABLE: Final[str] = "runtime_unavailable"
UNKNOWN_OPERATION: Final[str] = "unknown_operation"
CHECKPOINT_OWNERSHIP_LOST: Final[str] = "checkpoint_ownership_lost"


class RuntimeStoreError(Exception):
    """Base class for every typed Runtime failure."""

    code: ClassVar[str] = RUNTIME_UNAVAILABLE


class InvalidOperation(RuntimeStoreError):
    """The caller's identity or payload is empty, missing or malformed."""

    code: ClassVar[str] = INVALID_OPERATION

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid {field}: {reason}")


class OperationConflict(RuntimeStoreError):
    """The operation id was already accepted with a different payload."""

    code: ClassVar[str] = OPERATION_CONFLICT

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"operation {operation_id!r} was already accepted with a different payload"
        )


class QueueCapacityExceeded(RuntimeStoreError):
    """The thread already holds its configured number of open operations."""

    code: ClassVar[str] = QUEUE_CAPACITY_EXCEEDED

    def __init__(self, thread_id: str, capacity: int) -> None:
        self.thread_id = thread_id
        self.capacity = capacity
        super().__init__(
            f"thread {thread_id!r} already holds {capacity} open operations"
        )


class RuntimeUnavailable(RuntimeStoreError):
    """The durable store could not be reached or a transaction failed."""

    code: ClassVar[str] = RUNTIME_UNAVAILABLE

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"{action}: the runtime store is unavailable")


class UnknownOperation(RuntimeStoreError):
    """A lookup named an operation (or inbox entry) the store never accepted."""

    code: ClassVar[str] = UNKNOWN_OPERATION

    def __init__(self, identifier: str, *, kind: str = "operation") -> None:
        self.identifier = identifier
        self.kind = kind
        super().__init__(f"unknown {kind}: {identifier!r}")


class CheckpointOwnershipLost(RuntimeStoreError):
    """A checkpoint mutation was attempted without current ownership."""

    code: ClassVar[str] = CHECKPOINT_OWNERSHIP_LOST

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        message = f"checkpoint ownership lost: {reason}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


# ----- closed vocabularies -------------------------------------------------

InputProblem = Literal[
    "missing",
    "empty",
    "malformed",
    "too_large",
    "not_an_object",
    "tombstoned",
    "terminal",
    "thread_mismatch",
]

RUN_QUEUED: Final[str] = "queued"
RUN_RUNNING: Final[str] = "running"
RUN_WAITING: Final[str] = "waiting"
RUN_COMPLETED: Final[str] = "completed"
RUN_CANCELLED: Final[str] = "cancelled"
RUN_ABANDONED: Final[str] = "abandoned"
RUN_INTERRUPTED: Final[str] = "interrupted"
TERMINAL_RUN_STATES: Final[frozenset[str]] = frozenset(
    {RUN_COMPLETED, RUN_CANCELLED, RUN_ABANDONED, RUN_INTERRUPTED}
)
RunEndOutcome = Literal["completed", "cancelled", "abandoned", "interrupted"]

OPERATION_OPEN: Final[str] = "accepted"

RESERVATION_RESERVED: Final[str] = "reserved"
RESERVATION_RELEASED: Final[str] = "released"

ATTEMPT_ACTIVE: Final[str] = "active"
ATTEMPT_RELEASED: Final[str] = "released"
ATTEMPT_EXPIRED: Final[str] = "expired"

CLAIM_NO_QUEUED_RUN: Final[str] = "no_queued_run"
CLAIM_RESERVED_BY_OTHER: Final[str] = "reserved_by_other_run"
CLAIM_LEASE_ACTIVE: Final[str] = "lease_active"
CLAIM_NOT_HEAD: Final[str] = "not_head_of_queue"
CLAIM_RUN_NOT_CLAIMABLE: Final[str] = "run_not_claimable"
CLAIM_THREAD_TOMBSTONED: Final[str] = "thread_tombstoned"
CLAIM_THREAD_CANCELLED: Final[str] = "thread_cancelled"

RENEW_UNKNOWN_ATTEMPT: Final[str] = "unknown_attempt"
RENEW_NOT_ACTIVE: Final[str] = "attempt_not_active"
RENEW_LEASE_EXPIRED: Final[str] = "lease_expired"
RENEW_RUN_NOT_RUNNING: Final[str] = "run_not_running"
RENEW_GENERATION_MISMATCH: Final[str] = "generation_mismatch"

OWNERSHIP_RESERVATION_MISSING: Final[str] = "reservation_missing"
OWNERSHIP_RESERVATION_RELEASED: Final[str] = "reservation_released"
OWNERSHIP_RESERVATION_OTHER_RUN: Final[str] = "reservation_held_by_other_run"
OWNERSHIP_RUN_MISSING: Final[str] = "run_missing"
OWNERSHIP_RUN_NOT_RUNNING: Final[str] = "run_not_running"
OWNERSHIP_ATTEMPT_MISSING: Final[str] = "attempt_missing"
OWNERSHIP_ATTEMPT_NOT_ACTIVE: Final[str] = "attempt_not_active"
OWNERSHIP_ATTEMPT_GENERATION: Final[str] = "attempt_generation_mismatch"
OWNERSHIP_THREAD_GENERATION: Final[str] = "thread_generation_mismatch"
OWNERSHIP_LEASE_EXPIRED: Final[str] = "lease_expired"
OWNERSHIP_THREAD_TOMBSTONED: Final[str] = "thread_tombstoned"
OWNERSHIP_THREAD_CANCELLED: Final[str] = "thread_cancelled"
#: A caller supplied only half of the attempt identity.
OWNERSHIP_IDENTITY_INCOMPLETE: Final[str] = "runtime_identity_incomplete"

RecoveryVerdict = Literal["proven_safe", "reconciliation_required", "interrupted"]
RECOVERY_PROVEN_SAFE: Final[RecoveryVerdict] = "proven_safe"
RECOVERY_RECONCILIATION_REQUIRED: Final[RecoveryVerdict] = "reconciliation_required"
RECOVERY_INTERRUPTED: Final[RecoveryVerdict] = "interrupted"

RECOVERY_TERMINAL_COMMITTED: Final[str] = "terminal_committed"
RECOVERY_NO_EFFECT_STARTED: Final[str] = "no_effect_started"
RECOVERY_LEASE_EXPIRED_WITHOUT_EFFECT: Final[str] = "lease_expired_without_effect"
RECOVERY_LEASE_ACTIVE: Final[str] = "lease_active"
RECOVERY_UNCERTAIN_EFFECT: Final[str] = "uncertain_effect"
RECOVERY_THREAD_TOMBSTONED: Final[str] = "thread_tombstoned"
RECOVERY_THREAD_CANCELLED: Final[str] = "thread_cancelled"
RECOVERY_RUN_CANCELLED: Final[str] = "run_cancelled"
RECOVERY_RUN_ABANDONED: Final[str] = "run_abandoned"
RECOVERY_RUN_INTERRUPTED: Final[str] = "run_interrupted"
RECOVERY_RUN_STATE_UNKNOWN: Final[str] = "run_state_unknown"

EFFECT_STARTED_EVENT: Final[str] = "effect_started"
EFFECT_COMPLETED_EVENT: Final[str] = "effect_completed"

RESET_CURSOR_AHEAD: Final[str] = "cursor_ahead_of_high_water"
RESET_EVENT_GAP: Final[str] = "event_gap"
RESET_PROJECTION_MISSING: Final[str] = "projection_missing"
RESET_SEQUENCE_INCONSISTENT: Final[str] = "sequence_inconsistent"
RESET_CURSOR_INVALID: Final[str] = "cursor_invalid"

PARSER_INBOX_PENDING: Final[str] = "pending"
PARSER_INBOX_RECONCILED: Final[str] = "reconciled"
PARSER_AUTHORITY_SUCCEEDED: Final[str] = "succeeded"

_IDENTITY_MAX = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# ----- input normalization -------------------------------------------------


def normalize_identity(field: str, value: object) -> str:
    """Return ``value`` when it is a plausible durable identifier.

    Empty, missing, over-long, control-bearing and non-text identities are
    refused rather than coerced, so an empty key can never become a shared
    durable bucket.
    """
    if value is None:
        raise InvalidOperation(field, "missing")
    if not isinstance(value, str):
        raise InvalidOperation(field, "malformed")
    if not value.strip():
        raise InvalidOperation(field, "empty")
    if value != value.strip() or _CONTROL_CHARS.search(value) is not None:
        raise InvalidOperation(field, "malformed")
    if len(value) > _IDENTITY_MAX:
        raise InvalidOperation(field, "malformed")
    return value


def normalize_payload(field: str, value: object, *, max_bytes: int) -> dict[str, Any]:
    """Return a plain JSON object, refusing anything the digest could not bind.

    The round trip removes caller-owned containers (and tuples) so the digest
    is computed over the exact bytes PostgreSQL will hold. NaN/Infinity and
    non-text object keys are refused because they have no stable JSON form.
    """
    if value is None:
        raise InvalidOperation(field, "missing")
    if not isinstance(value, Mapping):
        raise InvalidOperation(field, "not_an_object")
    if not _only_text_keys(value):
        raise InvalidOperation(field, "malformed")
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOperation(field, "malformed") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise InvalidOperation(field, "too_large")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise InvalidOperation(field, "not_an_object")
    return decoded


def _only_text_keys(value: object) -> bool:
    """True when every object key in the value is text."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return False
            if not _only_text_keys(item):
                return False
        return True
    if isinstance(value, (list, tuple)):
        return all(_only_text_keys(item) for item in value)
    return True


def canonical_json(value: object) -> str:
    """Deterministic JSON text: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def payload_digest(payload: Mapping[str, Any]) -> str:
    """Stable digest of the accepted input only."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ----- records -------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceRecord:
    """One durable accepted operation, including its FIFO position."""

    operation_id: str
    thread_id: str
    run_id: str
    fifo_position: int
    payload_digest: str
    accepted_input: Mapping[str, Any]
    notification_intent: Mapping[str, Any]
    result: Mapping[str, Any] | None
    state: str
    created_at: datetime
    replay: bool = False


@dataclass(frozen=True)
class AttemptLease:
    """One worker attempt, fenced by ``(run_id, attempt_generation)``."""

    attempt_id: str
    run_id: str
    thread_id: str
    worker_id: str
    attempt_generation: int
    thread_generation: int
    lease_expires_at: datetime
    state: str


@dataclass(frozen=True)
class ClaimOutcome:
    claimed: bool
    lease: AttemptLease | None
    reason: str | None


@dataclass(frozen=True)
class RenewalOutcome:
    renewed: bool
    lease: AttemptLease | None
    reason: str | None


@dataclass(frozen=True)
class RunEndRecord:
    run_id: str
    thread_id: str
    state: str
    reservation_released: bool
    attempts_released: int
    #: True when the run already had a committed terminal and nothing changed.
    terminal: bool = False


@dataclass(frozen=True)
class ThreadFenceRecord:
    """The durable fence a thread cancellation or deletion left behind."""

    thread_id: str
    tombstoned: bool
    cancellation_reason: str | None
    reservation_released: bool
    attempts_released: int
    #: The run a cancellation names; ``None`` makes it thread-wide.
    cancelled_run_id: str | None = None


@dataclass(frozen=True)
class AttemptFact:
    attempt_id: str
    attempt_generation: int
    state: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class EventFact:
    seq: int
    run_id: str | None
    kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class RunRecoverySnapshot:
    """Durable facts about one run, read in one consistent snapshot."""

    run_id: str
    thread_id: str
    run_state: str
    attempt_generation: int
    thread_generation: int
    reservation_state: str | None
    reservation_run_id: str | None
    tombstoned: bool
    cancellation_reason: str | None
    terminal_committed: bool
    attempts: tuple[AttemptFact, ...]
    events: tuple[EventFact, ...]
    projection_revision: int | None
    high_water_seq: int
    #: The run the thread's cancellation names; ``None`` makes it thread-wide.
    cancelled_run_id: str | None = None


@dataclass(frozen=True)
class RecoveryFacts:
    """The shared safe-recovery seam P7 consumes."""

    run_id: str
    thread_id: str
    verdict: RecoveryVerdict
    reason: str
    resumable: bool
    run_state: str
    attempt_generation: int
    open_effect_keys: tuple[str, ...]
    projection_revision: int | None
    high_water_seq: int


@dataclass(frozen=True)
class ThreadEventRecord:
    seq: int
    run_id: str | None
    kind: str
    payload: Mapping[str, Any]
    source_position: Mapping[str, Any] | None
    created_at: datetime


@dataclass(frozen=True)
class EventCommit:
    thread_id: str
    seq: int
    revision: int
    high_water_seq: int
    outbox_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class TerminalCommit:
    run_id: str
    thread_id: str
    seq: int
    revision: int
    high_water_seq: int
    outbox_id: str | None
    committed_at: datetime
    replayed: bool


@dataclass(frozen=True)
class Snapshot:
    thread_id: str
    projection: Mapping[str, Any]
    revision: int
    high_water_seq: int
    events: tuple[ThreadEventRecord, ...]
    reset_required: bool
    reset_reason: str | None


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: str
    thread_id: str
    seq: int
    payload: Mapping[str, Any]
    created_at: datetime
    published_at: datetime | None


@dataclass(frozen=True)
class TerminalRecord:
    run_id: str
    thread_id: str
    technical_result: str
    semantic_result: str | None
    payload: Mapping[str, Any]
    evidence: Mapping[str, Any]
    seq: int
    revision: int
    committed_at: datetime


@dataclass(frozen=True)
class ParserInboxRecord:
    inbox_id: str
    job_id: str
    attachment_id: str
    corpus_id: str
    notification: Mapping[str, Any]
    state: str
    received_at: datetime
    reconciled_at: datetime | None
    authority_state: str | None
    replay: bool = False

    @property
    def ready_for_adoption(self) -> bool:
        """A notification alone is never ready; only reconciled truth is."""
        return (
            self.state == PARSER_INBOX_RECONCILED
            and self.authority_state == PARSER_AUTHORITY_SUCCEEDED
        )


# ----- DDL -----------------------------------------------------------------

SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS fin_runtime_thread_counter (
    thread_id TEXT PRIMARY KEY,
    next_fifo BIGINT NOT NULL DEFAULT 1,
    next_event_seq BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_runtime_run (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_generation BIGINT NOT NULL DEFAULT 0,
    thread_generation BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_runtime_operation (
    operation_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    accepted_input JSONB NOT NULL,
    fifo_position BIGINT NOT NULL,
    run_id TEXT NOT NULL REFERENCES fin_runtime_run (run_id),
    notification_intent JSONB NOT NULL,
    result JSONB,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, fifo_position)
);

-- A continuation is a new client operation on an existing run, so one run
-- may carry several operations. Older schemas had run_id UNIQUE; the
-- constraint is dropped for them as well.
ALTER TABLE fin_runtime_operation
    DROP CONSTRAINT IF EXISTS fin_runtime_operation_run_id_key;

CREATE INDEX IF NOT EXISTS fin_runtime_operation_run_idx
    ON fin_runtime_operation (run_id);

CREATE TABLE IF NOT EXISTS fin_runtime_thread_reservation (
    thread_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_generation BIGINT NOT NULL,
    state TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_runtime_worker_attempt (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    attempt_generation BIGINT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, attempt_generation)
);

CREATE TABLE IF NOT EXISTS fin_runtime_terminal (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    technical_result TEXT NOT NULL,
    semantic_result TEXT,
    payload JSONB NOT NULL,
    evidence JSONB NOT NULL,
    seq BIGINT NOT NULL,
    revision BIGINT NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_runtime_thread_projection (
    thread_id TEXT PRIMARY KEY,
    revision BIGINT NOT NULL,
    projection JSONB NOT NULL,
    high_water_seq BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_runtime_thread_event (
    thread_id TEXT NOT NULL,
    seq BIGINT NOT NULL,
    run_id TEXT,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    source_position JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, seq)
);

CREATE TABLE IF NOT EXISTS fin_runtime_outbox (
    outbox_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    seq BIGINT NOT NULL,
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, seq)
);

CREATE TABLE IF NOT EXISTS fin_runtime_thread_state (
    thread_id TEXT PRIMARY KEY,
    tombstoned BOOLEAN NOT NULL DEFAULT FALSE,
    cancellation_reason TEXT,
    cancelled_run_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A cancellation that names one run leaves the thread usable for the next
-- run; older schemas carry no such column.
ALTER TABLE fin_runtime_thread_state
    ADD COLUMN IF NOT EXISTS cancelled_run_id TEXT;

CREATE TABLE IF NOT EXISTS fin_runtime_parser_inbox (
    inbox_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    notification JSONB NOT NULL,
    state TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reconciled_at TIMESTAMPTZ,
    authority_state TEXT,
    UNIQUE (job_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS fin_runtime_run_thread_idx
    ON fin_runtime_run (thread_id, state);
CREATE INDEX IF NOT EXISTS fin_runtime_attempt_run_idx
    ON fin_runtime_worker_attempt (run_id, attempt_generation DESC);
CREATE INDEX IF NOT EXISTS fin_runtime_outbox_pending_idx
    ON fin_runtime_outbox (created_at) WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS fin_runtime_parser_inbox_pending_idx
    ON fin_runtime_parser_inbox (received_at) WHERE state = 'pending';
"""
