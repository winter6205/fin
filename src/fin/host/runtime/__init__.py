"""Durable Runtime: acceptance, ownership, events and the parser inbox.

PostgreSQL is the authority for accepted client operations, thread
reservations, worker attempts, terminal state, event projections and the
parser inbox (ADR 0103). This package owns the vocabulary, the store and the
safe-recovery projection; the graph compilation root and the HTTP layer wire
it in from outside.

The outbox publisher (``pending_outbox`` / ``mark_outbox_published``) and the
parser completion inbox (``record_parser_completion`` and its readers) are
deliberate seams: P1 persists their facts, while S2 and S3 own the delivery
and reconciliation callers. Until those land, only tests call them.
"""

from __future__ import annotations

from fin.host.runtime.admission import RuntimeCoordinator
from fin.host.runtime.recovery import (
    open_effect_keys,
    project_recovery_facts,
    recover_run,
)
from fin.host.runtime.schema import (
    AcceptanceRecord,
    AttemptLease,
    CheckpointOwnershipLost,
    ClaimOutcome,
    EventCommit,
    InvalidOperation,
    OperationConflict,
    OutboxRecord,
    ParserInboxRecord,
    QueueCapacityExceeded,
    RecoveryFacts,
    RenewalOutcome,
    RunEndRecord,
    RunRecoverySnapshot,
    RuntimeStoreError,
    RuntimeUnavailable,
    Snapshot,
    TerminalCommit,
    TerminalRecord,
    ThreadEventRecord,
    ThreadFenceRecord,
    UnknownOperation,
    normalize_identity,
    normalize_payload,
    payload_digest,
)
from fin.host.runtime.store import PostgresRuntimeStore, assert_ownership

__all__ = [
    "AcceptanceRecord",
    "AttemptLease",
    "CheckpointOwnershipLost",
    "ClaimOutcome",
    "EventCommit",
    "InvalidOperation",
    "OperationConflict",
    "OutboxRecord",
    "ParserInboxRecord",
    "PostgresRuntimeStore",
    "QueueCapacityExceeded",
    "RecoveryFacts",
    "RenewalOutcome",
    "RunEndRecord",
    "RunRecoverySnapshot",
    "RuntimeCoordinator",
    "RuntimeStoreError",
    "RuntimeUnavailable",
    "Snapshot",
    "TerminalCommit",
    "TerminalRecord",
    "ThreadEventRecord",
    "ThreadFenceRecord",
    "UnknownOperation",
    "assert_ownership",
    "normalize_identity",
    "normalize_payload",
    "open_effect_keys",
    "payload_digest",
    "project_recovery_facts",
    "recover_run",
]
