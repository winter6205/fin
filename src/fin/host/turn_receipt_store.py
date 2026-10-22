"""One durable operational receipt per logical turn.

``fin_run`` stays the question / answer / citation history. This module
owns the ``fin_turn_receipt`` contract the host adopted: the
(``thread_id``, ``turn_id``) key, the closed enums, the revision a
compare-and-swap advances, and the twelve ``stage_metrics`` entries.
A receipt holds no source identity and no question text — source
scope stays where it already lives.

Both stores answer the three questions a caller has — "is this the
same turn?", "did I win the write?", "is this a replay?" — through the
module-level decision helpers below, so an in-process run and a
durable one cannot drift on keying, revision or idempotency.

One place they DO drift, stated here so a reader does not take it for
an accident: the in-process store remembers every thread id it
retired, forever. The refusal it buys is real — a writer still holding
an admission against a deleted thread must not resurrect a receipt for
it, even after the same id has been handed out again — but the durable
store keeps no such tombstone, so a thread deleted and then recreated
under the same id is admitted by Postgres and refused by this process
for the rest of the process's life. The set also grows without bound
while it does so; bounding it would trade that refusal away.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Sequence, runtime_checkable

from fin.host.thread_store import (
    RunRecord,
    ThreadNotFoundError,
    ThreadStore,
    ThreadStoreError,
    ensure_thread_schema,
    guarded_connection,
    insert_run_row,
)

# ----- closed vocabularies -----


ExecutionState = Literal["running", "paused", "terminal"]
TechnicalResult = Literal["done", "stopped", "timeout", "failed"]
SemanticResult = Literal[
    "answered",
    "policy_refuse",
    "evidence_refuse",
    "clarify",
    "overview",
    "not_ready",
]
RouteSource = Literal["rule", "jev", "fallback"]
RouteIntent = Literal["retrieve", "overview", "chat", "policy_refuse"]
GroundedDecision = Literal[
    "answer",
    "document_choice",
    "clarify",
    "evidence_refuse",
    "not_ready",
    "continue",
]
DecisionParse = Literal[
    "structured",
    "fallback_merge",
    "fallback_refuse",
    "invalid",
    "unavailable",
]
NotReadyCause = Literal["index", "busy"]
NotReadyStage = Literal["index", "queue", "startup", "response"]
CancellationReason = Literal[
    "user_stop",
    "client_disconnect",
    "thread_deleted",
    "application_shutdown",
    "phase_deadline",
    "ownership_fenced",
]

#: The twelve stages a receipt accounts for, and only those. A stage
#: that was never started is ``complete: true`` at zero because its
#: absence is known; an interrupted stage is ``complete: false`` and
#: reports the lower bound that was observed, never a total.
STAGE_METRIC_KEYS: Final[tuple[str, ...]] = (
    "model",
    "runtime_reconciliation",
    "grounded_decision",
    "tool_planning",
    "other_model",
    "jev",
    "retrieve",
    "source_read",
    "database_acquire",
    "persistence",
    "cleanup",
    "disconnect_recovery",
)

STAGE_METRIC_FIELDS: Final[tuple[str, ...]] = ("calls", "elapsed_ms", "complete")

TOKEN_USAGE_KEYS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
)

#: A typed cause is never collapsed into a generic one: each Task
#: Runtime cause keeps its ``task_*`` token and each named Corpus
#: failure keeps its exact wire token.
FAILURE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "task_input_invalid",
        "task_input_unreconciled",
        "task_already_reconciled",
        "task_permission_denied",
        "task_dependency_failure",
        "task_capability_unavailable",
        "task_capability_unsupported",
        "task_insufficient_evidence",
        "task_applicability_unresolved",
        "task_intent_missing",
        "task_execution_limit",
        "task_checkpoint_failed",
        "task_history_failed",
        "task_reply_expired",
        "task_reply_unknown",
        "task_restore_unknown",
        "task_restore_not_adopted",
        "invalid_filter",
        "invalid_search_mode",
        "unsupported_filter_mode",
        "unsupported_source_scope",
        "retrieve_unavailable",
        "retrieve_protocol_error",
        "authorization_denied",
        "retrieve_degraded",
        "model_timeout",
        "jev_timeout",
        "retrieval_timeout",
        "source_read_timeout",
        "database_timeout",
        "persistence_failed",
        "checkpoint_failed",
        "graph_failed",
        "worker_failed",
        "recovery_inconsistent",
        "internal",
    }
)

#: The only (cause, stage) pairs an observed Corpus ``not_ready``
#: result may carry. ``index`` names the stage as well as the cause;
#: a generic empty result is never inferred into ``index``.
NOT_READY_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("index", "index"),
        ("busy", "queue"),
        ("busy", "startup"),
        ("busy", "response"),
    }
)

MAX_CALLS: Final[int] = 2147483647
MAX_ELAPSED_MS: Final[int] = 9223372036854775807

_KEY_MAX_OCTETS: Final[int] = 256
_RECEIPT_SCHEMA_VERSION: Final[int] = 1


def zero_stage_metrics() -> dict[str, dict[str, int | bool]]:
    """A turn that has measured nothing: twelve known absences."""
    return {
        key: {"calls": 0, "elapsed_ms": 0, "complete": True}
        for key in STAGE_METRIC_KEYS
    }


# ----- typed errors -----


class ReceiptError(ThreadStoreError):
    """Base class for turn-receipt errors."""


class ReceiptNotFoundError(ReceiptError):
    """A write addressed a (thread, turn) key the store does not hold."""


class ReceiptContractError(ReceiptError):
    """A draft violates the receipt's own shape or state invariants.

    Raised before anything is written, so a row that breaks a closed
    enum or a state invariant never reaches durable storage.
    """


class ReceiptRevisionConflict(ReceiptError):
    """The caller's expected revision is no longer the stored one.

    The stored row is left exactly as it was: a stale writer loses, it
    never clobbers the writer that moved first.
    """

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(
            f"receipt revision conflict: expected {expected}, stored {actual}"
        )
        self.expected = expected
        self.actual = actual


# ----- the record -----


@dataclass(frozen=True)
class TurnReceipt:
    """One logical turn's operational receipt, in the table's shape."""

    thread_id: str
    turn_id: str
    schema_version: int = _RECEIPT_SCHEMA_VERSION
    revision: int = 1
    execution_state: ExecutionState = "running"
    technical_result: TechnicalResult | None = None
    semantic_result: SemanticResult | None = None
    # Scoped to the inputs attached to THIS turn, so it starts at one
    # and is not the thread's absolute input count.
    accepted_input_count: int = 1
    # A projection of Task Runtime's authoritative version, never a
    # second input authority: it may already be well above one, and
    # the receipt does not assume its own prior value plus one.
    accepted_input_version: int = 0
    route_source: RouteSource | None = None
    route_intent: RouteIntent | None = None
    grounded_decision: GroundedDecision | None = None
    decision_parse: DecisionParse | None = None
    citation_verified: bool | None = None
    not_ready_cause: NotReadyCause | None = None
    not_ready_stage: NotReadyStage | None = None
    failure_kind: str | None = None
    cancellation_reason: CancellationReason | None = None
    # The turn's original start. Never rewritten by a continuation or
    # a recovery, which is why it is not part of the writable content.
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    terminal_at: datetime | None = None
    # Observed active work, excluding deliberate user wait: an
    # interruption leaves the observed lower bound and
    # ``accounting_complete`` false rather than an invented total.
    work_elapsed_ms: int = 0
    user_wait_ms: int = 0
    accounting_complete: bool = False
    stage_metrics: dict[str, dict[str, int | bool]] = field(
        default_factory=zero_stage_metrics
    )
    catalog_warning_count: int = 0
    token_usage: dict[str, int] | None = None
    # The export cursor. It moves without moving ``revision``, so an
    # acknowledgement can never look like a content change.
    exported_revision: int = 0
    # The execution segment this turn is in, as a durable counter. A
    # resume after an accepted clarification reply opens the next one.
    # Like the export cursor and unlike content, it is not rewritten by
    # a content update, so it is a cursor with its own write path rather
    # than a ``_CONTENT_FIELDS`` member: a content replay compares the
    # content set alone, and a segment bump must not read as one.
    segment_seq: int = 0


#: The columns, in the table's order. Written and read through this
#: one list so a column cannot be added to one and forgotten in the
#: other.
_COLUMNS: Final[tuple[str, ...]] = (
    "thread_id",
    "turn_id",
    "schema_version",
    "revision",
    "execution_state",
    "technical_result",
    "semantic_result",
    "accepted_input_count",
    "accepted_input_version",
    "route_source",
    "route_intent",
    "grounded_decision",
    "decision_parse",
    "citation_verified",
    "not_ready_cause",
    "not_ready_stage",
    "failure_kind",
    "cancellation_reason",
    "started_at",
    "updated_at",
    "terminal_at",
    "work_elapsed_ms",
    "user_wait_ms",
    "accounting_complete",
    "stage_metrics",
    "catalog_warning_count",
    "token_usage",
    "exported_revision",
    "segment_seq",
)

#: What a content update may rewrite. ``started_at`` is absent on
#: purpose: a continuation or a recovery keeps the turn's original
#: start, so a draft that moves it is refused rather than honoured.
_CONTENT_FIELDS: Final[tuple[str, ...]] = (
    "execution_state",
    "technical_result",
    "semantic_result",
    "accepted_input_count",
    "accepted_input_version",
    "route_source",
    "route_intent",
    "grounded_decision",
    "decision_parse",
    "citation_verified",
    "not_ready_cause",
    "not_ready_stage",
    "failure_kind",
    "cancellation_reason",
    "terminal_at",
    "work_elapsed_ms",
    "user_wait_ms",
    "accounting_complete",
    "stage_metrics",
    "catalog_warning_count",
    "token_usage",
)

_MAX_PENDING_RECEIPT_SCAN: Final[int] = 256
_EXPORT_STATEMENT_TIMEOUT: Final[str] = "SET LOCAL statement_timeout = '750ms'"
_EXPORT_LOCK_TIMEOUT: Final[str] = "SET LOCAL lock_timeout = '250ms'"


# ----- validation -----


def _require_text_key(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ReceiptContractError(f"{name} must be a string")
    if not 1 <= len(value.encode("utf-8")) <= _KEY_MAX_OCTETS:
        raise ReceiptContractError(
            f"{name} must be between 1 and {_KEY_MAX_OCTETS} octets"
        )


def _require_aware(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReceiptContractError(f"{name} must be a timezone-aware datetime")


def _require_choice(name: str, value: object, allowed: Sequence[str]) -> None:
    if value is not None and value not in allowed:
        raise ReceiptContractError(
            f"{name} must be one of {'|'.join(allowed)} or null, got {value!r}"
        )


def _require_int(name: str, value: object, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReceiptContractError(f"{name} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise ReceiptContractError(f"{name} must be between {low} and {high}")


def _validate_pending_scan(
    after: tuple[str, str] | None, limit: int
) -> None:
    _require_int("pending receipt limit", limit, 1, _MAX_PENDING_RECEIPT_SCAN)
    if after is None:
        return
    if not isinstance(after, tuple) or len(after) != 2:
        raise ReceiptContractError("pending receipt cursor must be a two-key tuple")
    _require_text_key("cursor thread_id", after[0])
    _require_text_key("cursor turn_id", after[1])


def _validate_stage_metrics(value: object) -> None:
    if not isinstance(value, dict):
        raise ReceiptContractError("stage_metrics must be an object")
    # WHY the set and not the tuple: a jsonb round-trip through the
    # durable store reorders its keys, so the KEY SET is the contract and
    # key order deliberately is not.
    if set(value) != set(STAGE_METRIC_KEYS):
        raise ReceiptContractError(
            "stage_metrics must carry exactly "
            f"{', '.join(STAGE_METRIC_KEYS)}; got {sorted(value)}"
        )
    for key in STAGE_METRIC_KEYS:
        entry = value[key]
        if not isinstance(entry, dict) or set(entry) != set(STAGE_METRIC_FIELDS):
            raise ReceiptContractError(
                f"stage_metrics[{key!r}] must carry exactly "
                f"{', '.join(STAGE_METRIC_FIELDS)}"
            )
        _require_int(f"stage_metrics[{key!r}].calls", entry["calls"], 0, MAX_CALLS)
        _require_int(
            f"stage_metrics[{key!r}].elapsed_ms", entry["elapsed_ms"], 0, MAX_ELAPSED_MS
        )
        if not isinstance(entry["complete"], bool):
            raise ReceiptContractError(
                f"stage_metrics[{key!r}].complete must be a boolean"
            )


def _validate_token_usage(value: object) -> None:
    # Absent is honest: provider usage is never estimated, so a key
    # that was not observed is missing rather than zero.
    if value is None:
        return
    if not isinstance(value, dict) or not set(value) <= set(TOKEN_USAGE_KEYS):
        raise ReceiptContractError(
            f"token_usage may only carry {', '.join(TOKEN_USAGE_KEYS)}; got {value!r}"
        )
    for key, count in value.items():
        _require_int(f"token_usage[{key!r}]", count, 0, MAX_CALLS)


def _validate_receipt(receipt: TurnReceipt) -> None:
    """Refuse a draft that could not be a row of this table."""
    _require_text_key("thread_id", receipt.thread_id)
    _require_text_key("turn_id", receipt.turn_id)
    if receipt.schema_version != _RECEIPT_SCHEMA_VERSION:
        raise ReceiptContractError(
            f"schema_version must be {_RECEIPT_SCHEMA_VERSION}"
        )
    _require_int("revision", receipt.revision, 1, MAX_ELAPSED_MS)
    _require_choice(
        "execution_state", receipt.execution_state, ("running", "paused", "terminal")
    )
    _require_choice(
        "technical_result",
        receipt.technical_result,
        ("done", "stopped", "timeout", "failed"),
    )
    _require_choice(
        "semantic_result",
        receipt.semantic_result,
        (
            "answered",
            "policy_refuse",
            "evidence_refuse",
            "clarify",
            "overview",
            "not_ready",
        ),
    )
    _require_choice(
        "route_source", receipt.route_source, ("rule", "jev", "fallback")
    )
    _require_choice(
        "route_intent",
        receipt.route_intent,
        ("retrieve", "overview", "chat", "policy_refuse"),
    )
    _require_choice(
        "grounded_decision",
        receipt.grounded_decision,
        (
            "answer",
            "document_choice",
            "clarify",
            "evidence_refuse",
            "not_ready",
            "continue",
        ),
    )
    _require_choice(
        "decision_parse",
        receipt.decision_parse,
        ("structured", "fallback_merge", "fallback_refuse", "invalid", "unavailable"),
    )
    if receipt.citation_verified is not None and not isinstance(
        receipt.citation_verified, bool
    ):
        raise ReceiptContractError("citation_verified must be a boolean or null")
    _require_choice(
        "cancellation_reason",
        receipt.cancellation_reason,
        (
            "user_stop",
            "client_disconnect",
            "thread_deleted",
            "application_shutdown",
            "phase_deadline",
            "ownership_fenced",
        ),
    )
    if receipt.failure_kind is not None and receipt.failure_kind not in FAILURE_KINDS:
        raise ReceiptContractError(
            f"failure_kind {receipt.failure_kind!r} is not an allowlisted token"
        )
    _require_int("accepted_input_count", receipt.accepted_input_count, 1, MAX_CALLS)
    _require_int(
        "accepted_input_version", receipt.accepted_input_version, 0, MAX_ELAPSED_MS
    )
    _require_aware("started_at", receipt.started_at)
    _require_aware("updated_at", receipt.updated_at)
    _require_int("work_elapsed_ms", receipt.work_elapsed_ms, 0, MAX_ELAPSED_MS)
    _require_int("user_wait_ms", receipt.user_wait_ms, 0, MAX_ELAPSED_MS)
    if not isinstance(receipt.accounting_complete, bool):
        raise ReceiptContractError("accounting_complete must be a boolean")
    _require_int(
        "catalog_warning_count", receipt.catalog_warning_count, 0, MAX_CALLS
    )
    _require_int(
        "exported_revision", receipt.exported_revision, 0, MAX_ELAPSED_MS
    )
    _require_int("segment_seq", receipt.segment_seq, 0, MAX_ELAPSED_MS)
    _validate_stage_metrics(receipt.stage_metrics)
    _validate_token_usage(receipt.token_usage)

    # Terminal is the only state with an outcome to report, and an
    # outcome without a technical result would leave a reader unable
    # to tell a finished turn from an abandoned one.
    if receipt.execution_state == "terminal":
        if receipt.technical_result is None or receipt.terminal_at is None:
            raise ReceiptContractError(
                "a terminal receipt requires technical_result and terminal_at"
            )
    elif receipt.technical_result is not None or receipt.terminal_at is not None:
        raise ReceiptContractError(
            "a nonterminal receipt requires technical_result and terminal_at to be null"
        )
    if receipt.terminal_at is not None:
        _require_aware("terminal_at", receipt.terminal_at)
    # A cause and a stage only exist together, and only as the pair an
    # observed Corpus not_ready result actually reported.
    if (receipt.not_ready_cause is None) != (receipt.not_ready_stage is None):
        raise ReceiptContractError(
            "not_ready_cause and not_ready_stage must be set together"
        )
    if receipt.not_ready_cause is not None and receipt.not_ready_stage is not None:
        if (receipt.not_ready_cause, receipt.not_ready_stage) not in NOT_READY_PAIRS:
            raise ReceiptContractError(
                f"({receipt.not_ready_cause!r}, {receipt.not_ready_stage!r}) is not an "
                "observed not_ready cause/stage pair"
            )
    if receipt.failure_kind is not None and receipt.technical_result not in (
        "failed",
        "timeout",
    ):
        raise ReceiptContractError(
            "failure_kind is present only for a failed or timed-out turn"
        )
    if receipt.exported_revision > receipt.revision:
        raise ReceiptContractError("exported_revision cannot exceed revision")


# ----- decisions both stores share -----


def _content_of(receipt: TurnReceipt) -> tuple[Any, ...]:
    return tuple(getattr(receipt, name) for name in _CONTENT_FIELDS)


def _is_replay(stored: TurnReceipt, draft: TurnReceipt) -> bool:
    """Whether ``draft`` states what ``stored`` already states.

    Replaying a lifecycle update or an accepted input version is a
    no-op: it must not advance ``revision``, and therefore must not
    look like a content change to anything watching the receipt.
    """
    return _content_of(stored) == _content_of(draft)


def _apply_change(
    stored: TurnReceipt, draft: TurnReceipt, *, expected_revision: int, now: datetime
) -> TurnReceipt:
    """Decide what one content update does. Pure; both stores call it.

    A replay is answered with the stored row unchanged. A genuine
    change advances the revision by exactly one and moves
    ``updated_at``. Anything else is a stale writer and is refused.
    """
    if draft.started_at != stored.started_at:
        # A continuation, a resume and a recovery all report under the
        # same key, so the turn's original start is not theirs to
        # rewrite; refusing beats quietly keeping one and dropping the
        # other.
        raise ReceiptContractError(
            "started_at is the turn's original start and never moves"
        )
    if _is_replay(stored, draft):
        return stored
    if expected_revision != stored.revision:
        raise ReceiptRevisionConflict(
            expected=expected_revision, actual=stored.revision
        )
    return replace(
        draft,
        revision=stored.revision + 1,
        updated_at=now,
        exported_revision=stored.exported_revision,
        segment_seq=stored.segment_seq,
    )


def _apply_accepted_input(
    stored: TurnReceipt, *, expected_revision: int, input_version: int, now: datetime
) -> TurnReceipt:
    """Record one newly accepted input against this turn.

    Deduplicated by Task Runtime's authoritative version, not by
    assuming the receipt's own prior projection is contiguous: an
    intervening accepted input can leave a version gap while the
    pending question still applies.

    The same accepted reply is the only thing that opens a new
    execution segment, so it is also the only place ``segment_seq``
    moves. A duplicate delivery returns the stored row untouched, and a
    segment bump therefore cannot be mistaken for one.
    """
    if input_version <= stored.accepted_input_version:
        return stored
    if expected_revision != stored.revision:
        raise ReceiptRevisionConflict(
            expected=expected_revision, actual=stored.revision
        )
    return replace(
        stored,
        accepted_input_count=stored.accepted_input_count + 1,
        accepted_input_version=input_version,
        segment_seq=stored.segment_seq + 1,
        revision=stored.revision + 1,
        updated_at=now,
    )


def _new_receipt(
    *, thread_id: str, turn_id: str, started_at: datetime, accepted_input_version: int
) -> TurnReceipt:
    receipt = TurnReceipt(
        thread_id=thread_id,
        turn_id=turn_id,
        revision=1,
        execution_state="running",
        accepted_input_count=1,
        accepted_input_version=accepted_input_version,
        started_at=started_at,
        updated_at=started_at,
    )
    _validate_receipt(receipt)
    return receipt


def _require_same_turn(receipt: TurnReceipt, run: RunRecord) -> None:
    """The terminal transcript row and the receipt are one turn.

    A transcript row filed under a different identity would be a
    second turn's history, and a reader would never find it next to the
    receipt that claims it.
    """
    if run.id != receipt.turn_id or run.thread_id != receipt.thread_id:
        raise ReceiptContractError(
            f"run {run.id!r} on thread {run.thread_id!r} is not the turn "
            f"{receipt.turn_id!r} on thread {receipt.thread_id!r}"
        )


def _require_terminal(draft: TurnReceipt) -> None:
    if draft.execution_state != "terminal":
        raise ReceiptContractError("a terminal commit needs a terminal receipt")


def _thread_deleted(receipt: TurnReceipt, *, now: datetime) -> TurnReceipt:
    """The pre-delete projection: this turn ended because its thread did.

    Written while the row still exists, in the same transaction that
    deletes it, so there is no window in which a fenced writer could read
    a receipt that does not say the thread was deleted. The turn's own
    semantic result is left as it stood — the thread is going away, not the
    answer it reached — while the cause, the technical result and the
    instant are the deletion's.

    Pure; both stores stamp through it so the durable row and the in-process
    twin carry the identical projection.
    """
    return replace(
        receipt,
        execution_state="terminal",
        technical_result="stopped",
        # A deletion is not a failure, so the turn cannot keep the failure
        # cause it had been carrying: a stopped turn carrying a
        # ``failure_kind`` is a combination this module's own validator
        # refuses, and leaving one behind means the retirement stamp — the
        # one row every store returns — is a row no store could have
        # written. The turn's own semantic result is untouched above, so
        # what it reached is still readable; only the cause is the
        # deletion's, and the deletion names itself as a cancellation.
        failure_kind=None,
        cancellation_reason="thread_deleted",
        terminal_at=receipt.terminal_at or now,
        revision=receipt.revision + 1,
        updated_at=now,
    )


# ----- the store protocol -----


@runtime_checkable
class ReceiptStore(Protocol):
    """One receipt per logical turn, addressed by (thread, turn).

    ``turn_id`` is the graph's ``run_id``: a continuation, a resume and
    a recovery all report under the same key, so one turn never
    accumulates two receipts.
    """

    def begin_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        started_at: datetime,
        accepted_input_version: int,
    ) -> TurnReceipt:
        """Create the receipt once. A repeat for the same key is a no-op."""
        ...

    def read_receipt(self, thread_id: str, turn_id: str) -> TurnReceipt | None:
        """The stored receipt, or ``None`` when the turn has none."""
        ...

    def list_receipts(self, thread_id: str) -> tuple[TurnReceipt, ...]:
        """Every receipt stored for ``thread_id``, ordered by ``turn_id``.

        Key-addressed reads alone cannot observe how many receipts a
        thread has: a store that wrote a second turn's receipt under its
        own id would answer every one of those reads correctly. This is
        what makes "exactly one receipt per turn" — and one turn's
        receipt per turn — a claim something can check.
        """
        ...

    def list_pending_receipts(
        self,
        *,
        after: tuple[str, str] | None = None,
        limit: int = 64,
    ) -> tuple[TurnReceipt, ...]:
        """Read a bounded, key-ordered page of unacknowledged receipts."""
        ...

    def acknowledge_export(
        self, thread_id: str, turn_id: str, revision: int
    ) -> bool:
        """Advance only the export cursor; a deleted receipt is a no-op."""
        ...

    def write_receipt(
        self, receipt: TurnReceipt, *, expected_revision: int
    ) -> TurnReceipt:
        """Compare-and-swap one content change onto the stored receipt."""
        ...

    def record_accepted_input(
        self,
        *,
        thread_id: str,
        turn_id: str,
        expected_revision: int,
        input_version: int,
    ) -> TurnReceipt:
        """Count one newly accepted clarification reply on this turn."""
        ...

    def commit_terminal(
        self, receipt: TurnReceipt, *, expected_revision: int, run: RunRecord
    ) -> TurnReceipt:
        """Commit the terminal receipt and its transcript row together."""
        ...

    def delete_thread(self, thread_id: str) -> list[TurnReceipt]:
        """Retire ``thread_id``: record why, then remove thread and all.

        The receipts are stamped with ``thread_deleted`` WHILE the rows
        still exist and are returned as they were stamped, so the cause is
        observable without leaving a row behind. The thread, its history
        and its receipts leave in ONE transaction — the durable store gets
        the cascade from the foreign key, the in-process store gets the
        same critical section.

        Raises :class:`ThreadNotFoundError` for a thread this store does
        not hold, and afterwards no write can recreate a receipt for it: a
        stale writer finds no row, and a fresh one is refused by the
        foreign key or by the in-process fence.
        """
        ...


# ----- in-memory twin -----


class InMemoryReceiptStore:
    """Receipts held beside the in-process thread store.

    The terminal commit is as atomic as the durable one: the receipt is
    put back the way it was if the transcript write does not land, so a
    reader sees both halves or neither. Locks are taken receipt-lock
    then thread-lock, always in that order, so the two stores cannot
    deadlock against each other.

    Deletion carries the fence the durable store gets from its foreign
    key. A dict has no cascade, so the thread's receipts are dropped
    inside the thread store's own deletion section and the thread id is
    remembered as deleted — an id a writer still holds an admission
    against is not a new thread, so nothing may bring a receipt back for
    it.
    """

    def __init__(self, threads: ThreadStore) -> None:
        self._threads = threads
        self._lock = threading.Lock()
        self._receipts: dict[tuple[str, str], TurnReceipt] = {}
        # WHY unbounded and never pruned: a tombstone is the only thing
        # that still refuses a writer holding an admission against a
        # thread this process retired, and dropping one would let that
        # writer resurrect a receipt under the id. The module docstring
        # names what this costs against the durable store.
        self._deleted: set[str] = set()

    def begin_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        started_at: datetime,
        accepted_input_version: int,
    ) -> TurnReceipt:
        fresh = _new_receipt(
            thread_id=thread_id,
            turn_id=turn_id,
            started_at=started_at,
            accepted_input_version=accepted_input_version,
        )
        with self._lock:
            # The durable write is refused by the foreign key, and a
            # receipt for a thread that does not exist is just as
            # unreadable here. A thread this process deleted is refused
            # even if the id has since been handed out again: the writer
            # holding that admission belongs to the thread that is gone.
            if thread_id in self._deleted:
                raise ThreadNotFoundError(f"thread {thread_id!r} was deleted")
            if self._threads.get_thread(thread_id) is None:
                raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
            existing = self._receipts.get((thread_id, turn_id))
            if existing is not None:
                return copy.deepcopy(existing)
            self._receipts[(thread_id, turn_id)] = fresh
            return copy.deepcopy(fresh)

    def read_receipt(self, thread_id: str, turn_id: str) -> TurnReceipt | None:
        with self._lock:
            stored = self._receipts.get((thread_id, turn_id))
        return copy.deepcopy(stored) if stored is not None else None

    def list_receipts(self, thread_id: str) -> tuple[TurnReceipt, ...]:
        with self._lock:
            found = [
                self._receipts[key]
                for key in sorted(self._receipts)
                if key[0] == thread_id
            ]
        return tuple(copy.deepcopy(receipt) for receipt in found)

    def list_pending_receipts(
        self,
        *,
        after: tuple[str, str] | None = None,
        limit: int = 64,
    ) -> tuple[TurnReceipt, ...]:
        _validate_pending_scan(after, limit)
        with self._lock:
            found = [
                receipt
                for key, receipt in sorted(self._receipts.items())
                if receipt.exported_revision < receipt.revision
                and (after is None or key > after)
            ][:limit]
        return tuple(copy.deepcopy(receipt) for receipt in found)

    def acknowledge_export(
        self, thread_id: str, turn_id: str, revision: int
    ) -> bool:
        _require_int("export revision", revision, 1, MAX_ELAPSED_MS)
        key = (thread_id, turn_id)
        with self._lock:
            stored = self._receipts.get(key)
            if stored is None:
                return False
            if revision > stored.revision:
                raise ReceiptContractError(
                    "export revision cannot exceed the stored receipt revision"
                )
            if revision <= stored.exported_revision:
                return False
            self._receipts[key] = replace(stored, exported_revision=revision)
            return True

    def write_receipt(
        self, receipt: TurnReceipt, *, expected_revision: int
    ) -> TurnReceipt:
        _validate_receipt(receipt)
        with self._lock:
            stored = self._require((receipt.thread_id, receipt.turn_id))
            committed = _apply_change(
                stored, receipt, expected_revision=expected_revision, now=_now()
            )
            if committed is not stored:
                self._receipts[(receipt.thread_id, receipt.turn_id)] = committed
            return copy.deepcopy(committed)

    def record_accepted_input(
        self,
        *,
        thread_id: str,
        turn_id: str,
        expected_revision: int,
        input_version: int,
    ) -> TurnReceipt:
        _require_int("input_version", input_version, 0, MAX_ELAPSED_MS)
        with self._lock:
            stored = self._require((thread_id, turn_id))
            committed = _apply_accepted_input(
                stored,
                expected_revision=expected_revision,
                input_version=input_version,
                now=_now(),
            )
            if committed is not stored:
                self._receipts[(thread_id, turn_id)] = committed
            return copy.deepcopy(committed)

    def commit_terminal(
        self, receipt: TurnReceipt, *, expected_revision: int, run: RunRecord
    ) -> TurnReceipt:
        _validate_receipt(receipt)
        _require_terminal(receipt)
        _require_same_turn(receipt, run)
        with self._lock:
            key = (receipt.thread_id, receipt.turn_id)
            stored = self._require(key)
            committed = _apply_change(
                stored, receipt, expected_revision=expected_revision, now=_now()
            )
            if committed is not stored:
                self._receipts[key] = committed
            try:
                # A second finalizer for the same turn writes the same
                # keyed transcript row, which the thread store ignores.
                self._threads.append_run(run)
            except BaseException:
                self._receipts[key] = stored
                raise
            return copy.deepcopy(committed)

    def _require(self, key: tuple[str, str]) -> TurnReceipt:
        stored = self._receipts.get(key)
        if stored is None:
            raise ReceiptNotFoundError(
                f"no receipt for turn {key[1]!r} on thread {key[0]!r}"
            )
        return stored

    def delete_thread(self, thread_id: str) -> list[TurnReceipt]:
        """Retire ``thread_id`` in the one section that holds both stores."""
        with self._lock:
            now = _now()
            # The thread's own deletion reports whether the thread was
            # here at all, so it goes FIRST: raising after dropping the
            # receipts would retire a thread this store never held and
            # leave the caller with a refusal it has already paid for.
            if not self._threads.delete_thread(thread_id):
                raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
            keys = sorted(key for key in self._receipts if key[0] == thread_id)
            retired = [_thread_deleted(self._receipts[key], now=now) for key in keys]
            for key in keys:
                del self._receipts[key]
            self._deleted.add(thread_id)
            return retired


# ----- postgres -----


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join("'{}'".format(value) for value in values)


def _stage_metrics_constraint() -> str:
    """The twelve-key shape, as one expression built from the key list.

    ``?&`` proves every key is present, ``-`` proves nothing else is,
    and the JSONPath clause proves each entry's three fields have the
    types the contract names.
    """
    keys = _sql_list(STAGE_METRIC_KEYS)
    fields = _sql_list(STAGE_METRIC_FIELDS)
    clauses = [
        f"stage_metrics ?& ARRAY[{keys}]",
        f"stage_metrics - ARRAY[{keys}] = '{{}}'::jsonb",
    ]
    for key in STAGE_METRIC_KEYS:
        clauses.append(f"(stage_metrics->'{key}') ?& ARRAY[{fields}]")
        clauses.append(
            f"(stage_metrics->'{key}') - ARRAY[{fields}] = '{{}}'::jsonb"
        )
        clauses.append(
            "NOT (stage_metrics @? '$.{key} ? (@.calls.type() != \"number\" || "
            "@.elapsed_ms.type() != \"number\" || @.complete.type() != \"boolean\")')"
            .format(key=key)
        )
    return "\n        AND ".join(clauses)


_RECEIPT_SCHEMA_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS fin_turn_receipt (
    thread_id              TEXT NOT NULL
                           CHECK (octet_length(thread_id) BETWEEN 1 AND 256),
    turn_id                TEXT NOT NULL
                           CHECK (octet_length(turn_id) BETWEEN 1 AND 256),
    schema_version         SMALLINT NOT NULL CHECK (schema_version = 1),
    revision               BIGINT NOT NULL CHECK (revision >= 1),
    execution_state        TEXT NOT NULL
                           CHECK (execution_state IN ('running','paused','terminal')),
    -- NULL while the turn is still open: a semantic result nobody
    -- established is not a failed one.
    technical_result       TEXT
                           CHECK (technical_result IN ('done','stopped','timeout','failed')),
    semantic_result        TEXT
                           CHECK (semantic_result IN ('answered','policy_refuse',
                           'evidence_refuse','clarify','overview','not_ready')),
    accepted_input_count   INTEGER NOT NULL
                           CHECK (accepted_input_count BETWEEN 1 AND 2147483647),
    accepted_input_version BIGINT NOT NULL
                           CHECK (accepted_input_version BETWEEN 0 AND 9223372036854775807),
    route_source           TEXT CHECK (route_source IN ('rule','jev','fallback')),
    route_intent           TEXT
                           CHECK (route_intent IN ('retrieve','overview','chat','policy_refuse')),
    grounded_decision      TEXT
                           CHECK (grounded_decision IN ('answer','document_choice','clarify',
                           'evidence_refuse','not_ready','continue')),
    decision_parse         TEXT
                           CHECK (decision_parse IN ('structured','fallback_merge',
                           'fallback_refuse','invalid','unavailable')),
    citation_verified      BOOLEAN,
    not_ready_cause        TEXT CHECK (not_ready_cause IN ('index','busy')),
    not_ready_stage        TEXT
                           CHECK (not_ready_stage IN ('index','queue','startup','response')),
    failure_kind           TEXT
                           CHECK (failure_kind IN ({_sql_list(sorted(FAILURE_KINDS))})),
    cancellation_reason    TEXT
                           CHECK (cancellation_reason IN ('user_stop','client_disconnect',
                           'thread_deleted','application_shutdown','phase_deadline',
                           'ownership_fenced')),
    started_at             TIMESTAMPTZ NOT NULL,
    updated_at             TIMESTAMPTZ NOT NULL,
    terminal_at            TIMESTAMPTZ,
    work_elapsed_ms        BIGINT NOT NULL CHECK (work_elapsed_ms >= 0),
    user_wait_ms           BIGINT NOT NULL CHECK (user_wait_ms >= 0),
    -- FALSE marks partial, lower-bound measurements after an
    -- interruption; a crashed turn never backfills a total.
    accounting_complete    BOOLEAN NOT NULL DEFAULT FALSE,
    stage_metrics          JSONB NOT NULL,
    catalog_warning_count  INTEGER NOT NULL
                           CHECK (catalog_warning_count BETWEEN 0 AND 2147483647),
    token_usage            JSONB,
    exported_revision      BIGINT NOT NULL DEFAULT 0
                           CHECK (exported_revision BETWEEN 0 AND revision),
    segment_seq            BIGINT NOT NULL DEFAULT 0 CHECK (segment_seq >= 0),
    PRIMARY KEY (thread_id, turn_id),
    FOREIGN KEY (thread_id) REFERENCES fin_thread(id) ON DELETE CASCADE,
    -- A cause and a stage exist together, and only as the pair an
    -- observed Corpus not_ready result actually reported. The CASE
    -- keeps the whole expression boolean: a row-constructor IN whose
    -- stage is NULL is NULL, not FALSE, and a CHECK passes on NULL.
    CONSTRAINT fin_turn_receipt_not_ready_pair CHECK (
        CASE
            WHEN not_ready_cause IS NULL THEN not_ready_stage IS NULL
            ELSE not_ready_stage IS NOT NULL
                 AND (not_ready_cause, not_ready_stage) IN
                     (('index','index'), ('busy','queue'), ('busy','startup'),
                      ('busy','response'))
        END
    ),
    -- The same rule the Python validator enforces, so the durable store
    -- refuses the pair rather than accepting a row the in-process twin
    -- could never have written.
    CONSTRAINT fin_turn_receipt_failure_result CHECK (
        failure_kind IS NULL OR technical_result IN ('failed','timeout')
    ),
    CONSTRAINT fin_turn_receipt_stage_metrics CHECK (
        {_stage_metrics_constraint()}
    ),
    -- Provider usage is never estimated: a key nobody observed is
    -- missing, and a present count is a number inside the signed
    -- 32-bit range the column itself uses.
    CONSTRAINT fin_turn_receipt_token_usage CHECK (
        token_usage IS NULL
        OR (token_usage - ARRAY[{_sql_list(TOKEN_USAGE_KEYS)}] = '{{}}'::jsonb
            AND COALESCE(token_usage @? '$.* ? (@.type() != "number" || @ < 0
                    || @ > 2147483647)', false) = false)
    )
);
"""

# WHY the separate ALTER and not only the CREATE above: ``IF NOT EXISTS``
# leaves a table created by an earlier version exactly as it was, so a
# constraint added only to the CREATE would never reach the table an
# existing installation already has. Drop-then-add is what makes applying
# this idempotent rather than a one-shot.
_RECEIPT_FAILURE_RESULT_SQL: Final[str] = """
ALTER TABLE fin_turn_receipt
    DROP CONSTRAINT IF EXISTS fin_turn_receipt_failure_result;
ALTER TABLE fin_turn_receipt
    ADD CONSTRAINT fin_turn_receipt_failure_result
    CHECK (failure_kind IS NULL OR technical_result IN ('failed','timeout'));
"""

# WHY the same separate ALTER for the segment counter: ``IF NOT EXISTS``
# on a CREATE leaves an already-created table untouched, so a column
# added only to the CREATE would never reach an existing installation.
# ``DEFAULT 0`` is the expand half — every row that already exists
# belongs to the first segment.
_RECEIPT_SEGMENT_SEQ_SQL: Final[str] = """
ALTER TABLE fin_turn_receipt
    ADD COLUMN IF NOT EXISTS segment_seq BIGINT NOT NULL DEFAULT 0
    CHECK (segment_seq >= 0);
"""

_INSERT_SQL: Final[str] = (
    "INSERT INTO fin_turn_receipt ({columns}) VALUES ({placeholders}) "
    "ON CONFLICT (thread_id, turn_id) DO NOTHING RETURNING {columns}"
).format(
    columns=", ".join(_COLUMNS),
    placeholders=", ".join(["%s"] * len(_COLUMNS)),
)

_SELECT_SQL: Final[str] = (
    f"SELECT {', '.join(_COLUMNS)} FROM fin_turn_receipt "
    "WHERE thread_id = %s AND turn_id = %s"
)

_LIST_SQL: Final[str] = (
    f"SELECT {', '.join(_COLUMNS)} FROM fin_turn_receipt "
    "WHERE thread_id = %s ORDER BY turn_id"
)

_PENDING_FIRST_SQL: Final[str] = (
    f"SELECT {', '.join(_COLUMNS)} FROM fin_turn_receipt "
    "WHERE exported_revision < revision ORDER BY thread_id, turn_id LIMIT %s"
)

_PENDING_AFTER_SQL: Final[str] = (
    f"SELECT {', '.join(_COLUMNS)} FROM fin_turn_receipt "
    "WHERE exported_revision < revision AND (thread_id, turn_id) > (%s, %s) "
    "ORDER BY thread_id, turn_id LIMIT %s"
)

_ACKNOWLEDGE_EXPORT_SELECT_SQL: Final[str] = (
    "SELECT revision, exported_revision FROM fin_turn_receipt "
    "WHERE thread_id = %s AND turn_id = %s FOR UPDATE"
)

_ACKNOWLEDGE_EXPORT_SQL: Final[str] = (
    "UPDATE fin_turn_receipt SET exported_revision = %s "
    "WHERE thread_id = %s AND turn_id = %s RETURNING exported_revision"
)

# WHY a dedicated statement rather than another ``_CONTENT_FIELDS``
# column: the segment counter has its own write path, and folding it
# into the content UPDATE would let every content draft rewrite it.
_SEGMENT_SEQ_SQL: Final[str] = (
    "UPDATE fin_turn_receipt SET segment_seq = %s "
    "WHERE thread_id = %s AND turn_id = %s RETURNING segment_seq"
)

_UPDATE_SQL: Final[str] = (
    "UPDATE fin_turn_receipt SET {assignments} "
    "WHERE thread_id = %s AND turn_id = %s AND revision = %s RETURNING {columns}"
).format(
    assignments=", ".join(f"{name} = %s" for name in _CONTENT_FIELDS)
    + ", revision = revision + 1, updated_at = %s",
    columns=", ".join(_COLUMNS),
)

#: Names this store in the typed error ``guarded_connection`` raises,
#: which is shared with the thread store.
_COMPONENT: Final[str] = "the receipt store"

#: The pre-delete stamp. It runs before the DELETE in the same
#: transaction, so the receipts carry the cause while the rows still
#: exist and the cascade removes them immediately after.
#:
#: WHY it clears ``failure_kind`` the way :func:`_thread_deleted` does: the
#: two must write the same row, and a ``stopped`` turn carrying a failure
#: cause is the one combination neither the Python validator nor the
#: table's own CHECK accepts.
_STAMP_THREAD_DELETED_SQL: Final[str] = (
    "UPDATE fin_turn_receipt SET "
    "execution_state = 'terminal', technical_result = 'stopped', "
    "failure_kind = NULL, "
    "cancellation_reason = 'thread_deleted', "
    "terminal_at = COALESCE(terminal_at, %s), revision = revision + 1, "
    "updated_at = %s WHERE thread_id = %s RETURNING {columns}"
).format(columns=", ".join(_COLUMNS))



def receipt_payload(receipt: TurnReceipt) -> dict[str, Any]:
    """One receipt as its own table's columns, JSON-ready.

    Built from ``_COLUMNS`` rather than a second hand-written list, so the
    developer view cannot publish a column the table dropped or invent one
    it never had. Instants become ISO-8601 strings because the consumer is
    a JSON reader; everything else is the stored value, unchanged.
    """
    payload: dict[str, Any] = {}
    for name in _COLUMNS:
        value: Any = getattr(receipt, name)
        payload[name] = value.isoformat() if isinstance(value, datetime) else value
    return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_cell(value: object) -> Any:
    """A JSONB column reads back as a string or as a decoded object."""
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return copy.deepcopy(value)


def _row_to_receipt(row: Sequence[Any]) -> TurnReceipt:
    cells = dict(zip(_COLUMNS, row))
    return TurnReceipt(
        thread_id=cells["thread_id"],
        turn_id=cells["turn_id"],
        schema_version=cells["schema_version"],
        revision=cells["revision"],
        execution_state=cells["execution_state"],
        technical_result=cells["technical_result"],
        semantic_result=cells["semantic_result"],
        accepted_input_count=cells["accepted_input_count"],
        accepted_input_version=cells["accepted_input_version"],
        route_source=cells["route_source"],
        route_intent=cells["route_intent"],
        grounded_decision=cells["grounded_decision"],
        decision_parse=cells["decision_parse"],
        citation_verified=cells["citation_verified"],
        not_ready_cause=cells["not_ready_cause"],
        not_ready_stage=cells["not_ready_stage"],
        failure_kind=cells["failure_kind"],
        cancellation_reason=cells["cancellation_reason"],
        started_at=cells["started_at"],
        updated_at=cells["updated_at"],
        terminal_at=cells["terminal_at"],
        work_elapsed_ms=cells["work_elapsed_ms"],
        user_wait_ms=cells["user_wait_ms"],
        accounting_complete=cells["accounting_complete"],
        stage_metrics=_json_cell(cells["stage_metrics"]),
        catalog_warning_count=cells["catalog_warning_count"],
        token_usage=_json_cell(cells["token_usage"]),
        exported_revision=cells["exported_revision"],
        segment_seq=cells["segment_seq"],
    )


def _cell_values(receipt: TurnReceipt) -> list[Any]:
    """The row's values in ``_COLUMNS`` order, JSONB serialized."""
    values: list[Any] = []
    for name in _COLUMNS:
        value: Any = getattr(receipt, name)
        if name in ("stage_metrics", "token_usage") and value is not None:
            value = json.dumps(value)
        values.append(value)
    return values


class PostgresReceiptStore:
    """Receipts against the same ``FIN_CHECKPOINT_URL`` as the thread store.

    The table is created on construction, after the host-owned thread
    tables it points at. The terminal commit wraps the receipt write and
    the transcript write in one transaction, so a reader never reads one
    half of a finished turn without the other.
    """

    def __init__(
        self, connection_url: str, *, fin_home: Path | None = None
    ) -> None:
        self._url = connection_url
        ensure_thread_schema(connection_url, fin_home=fin_home)
        with guarded_connection(
            connection_url, "create receipt table", component=_COMPONENT
        ) as conn:
            conn.execute(_RECEIPT_SCHEMA_SQL)
            conn.execute(_RECEIPT_FAILURE_RESULT_SQL)
            conn.execute(_RECEIPT_SEGMENT_SEQ_SQL)
            conn.commit()

    def begin_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        started_at: datetime,
        accepted_input_version: int,
    ) -> TurnReceipt:
        fresh = _new_receipt(
            thread_id=thread_id,
            turn_id=turn_id,
            started_at=started_at,
            accepted_input_version=accepted_input_version,
        )
        import psycopg

        try:
            with guarded_connection(
            self._url, "begin turn", component=_COMPONENT
        ) as conn:
                with conn.transaction():
                    row = conn.execute(
                        _INSERT_SQL, _cell_values(fresh)
                    ).fetchone()
                    if row is None:
                        # The key already holds a receipt: this turn was
                        # begun before, so the existing one is the truth
                        # and its revision does not move.
                        row = conn.execute(
                            _SELECT_SQL, (thread_id, turn_id)
                        ).fetchone()
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ThreadNotFoundError(f"unknown thread {thread_id!r}") from exc
        if row is None:
            raise ReceiptNotFoundError(
                f"no receipt for turn {turn_id!r} on thread {thread_id!r}"
            )
        return _row_to_receipt(row)

    def read_receipt(self, thread_id: str, turn_id: str) -> TurnReceipt | None:
        with guarded_connection(
            self._url, "read receipt", component=_COMPONENT
        ) as conn:
            row = conn.execute(_SELECT_SQL, (thread_id, turn_id)).fetchone()
        return _row_to_receipt(row) if row is not None else None

    def list_receipts(self, thread_id: str) -> tuple[TurnReceipt, ...]:
        with guarded_connection(
            self._url, "list receipts", component=_COMPONENT
        ) as conn:
            rows = conn.execute(_LIST_SQL, (thread_id,)).fetchall()
        return tuple(_row_to_receipt(row) for row in rows)

    def list_pending_receipts(
        self,
        *,
        after: tuple[str, str] | None = None,
        limit: int = 64,
    ) -> tuple[TurnReceipt, ...]:
        _validate_pending_scan(after, limit)
        with guarded_connection(
            self._url, "scan pending receipt exports", component=_COMPONENT
        ) as conn:
            with conn.transaction():
                conn.execute(_EXPORT_STATEMENT_TIMEOUT)
                if after is None:
                    rows = conn.execute(_PENDING_FIRST_SQL, (limit,)).fetchall()
                else:
                    rows = conn.execute(
                        _PENDING_AFTER_SQL, (*after, limit)
                    ).fetchall()
        return tuple(_row_to_receipt(row) for row in rows)

    def acknowledge_export(
        self, thread_id: str, turn_id: str, revision: int
    ) -> bool:
        _require_int("export revision", revision, 1, MAX_ELAPSED_MS)
        with guarded_connection(
            self._url, "acknowledge receipt export", component=_COMPONENT
        ) as conn:
            with conn.transaction():
                conn.execute(_EXPORT_STATEMENT_TIMEOUT)
                conn.execute(_EXPORT_LOCK_TIMEOUT)
                row = conn.execute(
                    _ACKNOWLEDGE_EXPORT_SELECT_SQL, (thread_id, turn_id)
                ).fetchone()
                if row is None:
                    return False
                current_revision, exported_revision = int(row[0]), int(row[1])
                if revision > current_revision:
                    raise ReceiptContractError(
                        "export revision cannot exceed the stored receipt revision"
                    )
                if revision <= exported_revision:
                    return False
                conn.execute(
                    _ACKNOWLEDGE_EXPORT_SQL,
                    (revision, thread_id, turn_id),
                )
        return True

    def write_receipt(
        self, receipt: TurnReceipt, *, expected_revision: int
    ) -> TurnReceipt:
        _validate_receipt(receipt)
        with guarded_connection(
            self._url, "write receipt", component=_COMPONENT
        ) as conn:
            with conn.transaction():
                stored = self._select_locked(conn, receipt.thread_id, receipt.turn_id)
                committed = _apply_change(
                    stored, receipt, expected_revision=expected_revision, now=_now()
                )
                if committed is not stored:
                    self._update(conn, committed, expected_revision=expected_revision)
        return committed

    def record_accepted_input(
        self,
        *,
        thread_id: str,
        turn_id: str,
        expected_revision: int,
        input_version: int,
    ) -> TurnReceipt:
        _require_int("input_version", input_version, 0, MAX_ELAPSED_MS)
        with guarded_connection(
            self._url, "record accepted input", component=_COMPONENT
        ) as conn:
            with conn.transaction():
                stored = self._select_locked(conn, thread_id, turn_id)
                committed = _apply_accepted_input(
                    stored,
                    expected_revision=expected_revision,
                    input_version=input_version,
                    now=_now(),
                )
                if committed is not stored:
                    self._update(conn, committed, expected_revision=expected_revision)
                    conn.execute(
                        _SEGMENT_SEQ_SQL,
                        (committed.segment_seq, thread_id, turn_id),
                    )
        return committed

    def commit_terminal(
        self, receipt: TurnReceipt, *, expected_revision: int, run: RunRecord
    ) -> TurnReceipt:
        _validate_receipt(receipt)
        _require_terminal(receipt)
        _require_same_turn(receipt, run)
        with guarded_connection(
            self._url, "commit terminal receipt", component=_COMPONENT
        ) as conn:
            with conn.transaction():
                stored = self._select_locked(conn, receipt.thread_id, receipt.turn_id)
                committed = _apply_change(
                    stored, receipt, expected_revision=expected_revision, now=_now()
                )
                if committed is not stored:
                    self._update(conn, committed, expected_revision=expected_revision)
                # The transcript row is keyed by the same run identity
                # and is written on every attempt, so a repeat
                # finalizer is as harmless as a repeat receipt.
                insert_run_row(conn, run)
        return committed

    def delete_thread(self, thread_id: str) -> list[TurnReceipt]:
        """Retire ``thread_id`` and return its receipts as they were stamped.

        One transaction covers the whole thread: the stamp, then the DELETE
        whose foreign-key cascade takes the transcript rows and the receipts
        with it. Nothing survives the commit, and the returned rows are the
        cause that was written on the way out.
        """
        now = _now()
        with guarded_connection(
            self._url, "delete thread", component=_COMPONENT
        ) as conn:
            with conn.transaction():
                rows = conn.execute(
                    _STAMP_THREAD_DELETED_SQL, (now, now, thread_id)
                ).fetchall()
                deleted = conn.execute(
                    "DELETE FROM fin_thread WHERE id = %s RETURNING id",
                    (thread_id,),
                ).fetchone()
        if deleted is None:
            raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
        return [_row_to_receipt(row) for row in rows]

    def _select_locked(self, conn: Any, thread_id: str, turn_id: str) -> TurnReceipt:
        """The stored row, pinned for the rest of this transaction.

        ``FOR UPDATE`` is what makes the compare-and-swap decide
        against the row a concurrent writer committed, not against a
        snapshot it read before that writer arrived.
        """
        row = conn.execute(
            _SELECT_SQL + " FOR UPDATE", (thread_id, turn_id)
        ).fetchone()
        if row is None:
            raise ReceiptNotFoundError(
                f"no receipt for turn {turn_id!r} on thread {thread_id!r}"
            )
        return _row_to_receipt(row)

    def _update(
        self, conn: Any, receipt: TurnReceipt, *, expected_revision: int
    ) -> None:
        values = [
            json.dumps(getattr(receipt, name))
            if name in ("stage_metrics", "token_usage")
            and getattr(receipt, name) is not None
            else getattr(receipt, name)
            for name in _CONTENT_FIELDS
        ]
        row = conn.execute(
            _UPDATE_SQL,
            (
                *values,
                receipt.updated_at,
                receipt.thread_id,
                receipt.turn_id,
                expected_revision,
            ),
        ).fetchone()
        if row is None:
            # Unreachable while the row is pinned FOR UPDATE, so this
            # names a state the transaction cannot explain. It raises
            # rather than returns, because a silent no-write must never
            # be handed back as if its revision had been committed.
            raise ReceiptError(
                f"the receipt update for turn {receipt.turn_id!r} matched no row"
            )


def default_receipt_store(threads: ThreadStore) -> ReceiptStore:
    """The receipt store that matches ``threads``' durability.

    An in-process receipt beside a durable thread store (or the other
    way round) would split one turn across two histories, so the
    thread store decides and is passed in rather than re-selected.
    """
    from fin.host.thread_store import PostgresThreadStore

    if isinstance(threads, PostgresThreadStore):
        return PostgresReceiptStore(threads.connection_url)
    return InMemoryReceiptStore(threads)


__all__ = [
    "CancellationReason",
    "DecisionParse",
    "ExecutionState",
    "FAILURE_KINDS",
    "GroundedDecision",
    "InMemoryReceiptStore",
    "NOT_READY_PAIRS",
    "NotReadyCause",
    "NotReadyStage",
    "PostgresReceiptStore",
    "ReceiptContractError",
    "ReceiptError",
    "ReceiptNotFoundError",
    "ReceiptRevisionConflict",
    "ReceiptStore",
    "RouteIntent",
    "RouteSource",
    "STAGE_METRIC_FIELDS",
    "STAGE_METRIC_KEYS",
    "SemanticResult",
    "TOKEN_USAGE_KEYS",
    "TechnicalResult",
    "TurnReceipt",
    "default_receipt_store",
    "receipt_payload",
    "zero_stage_metrics",
]
