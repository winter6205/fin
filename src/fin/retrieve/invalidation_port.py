"""Invalidation port: the idempotent invalidation seam, its named
failures and its queryable record (plan T18, SC24, §B4「内容失效语义」).

This module is the retrieval-facing half of T18. The corpus side
(:mod:`fin.corpus.invalidation`) decides *which* old blocks went stale
and *why*; this module issues the request, reads the answer, and keeps
the record.

Division of duties (§B4)
------------------------
The invalidation *action* — 关闭时间有效区间使其不再被任何查询时刻命中、
不物理删除、幂等可重复执行 — is retrieval-side semantics. fin guarantees
two things only: that the request is **issued** on a version change, and
that a failure is **named**. The contract fake
(:mod:`fin.retrieve.fake_push_port`) implements the retrieval-side
semantics so the SC24 assertions have something to stand on before line
B (the iknow-memory repo) is reachable.

Idempotency
-----------
A request is identified by :func:`fin.corpus.invalidation.request_key` —
the digest of the targets' T14 ``(source_ref, content identity)`` keys.
Re-issuing it must yield the same answer: the same block ids and the
same closing instants (:class:`InvalidationOutcome`). The store counts
the repeat as ``already_closed`` rather than closing anything twice.

Failure contract
----------------
Every failure is a typed :class:`InvalidationError` with a stable
``code``; none of them returns an outcome, a ``None``, a sentinel number
or an empty success, and none is retried inside this module
(:data:`INVALIDATION_ATTEMPTS_PER_REQUEST`). A whole request is refused
before a single interval is closed, so a refused invalidation cannot
leave half of itself behind. ``degraded`` / ``degraded_reasons`` (field
names verbatim §B3) classify the failure the same way the push path
does: invalid input from the caller does not set the flag, a capability
that was attempted and failed does.

The record
----------
:class:`InvalidationLedger` is the「可查询的失效记录」on the fin side:
one entry per issued request, settled or failed, queryable by request
key or `source_ref`. With a ``path`` it is an append-only JSONL log
(reloaded by replay) so the record outlives the process that made it;
without one it lives in memory. It is appended to, never rewritten — a
repeated request adds an entry instead of overwriting the trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from fin.corpus.invalidation import InvalidationTarget, request_key

# T18 retry policy, mirroring the push path's
# ``PUSH_ATTEMPTS_PER_BATCH``: one attempt per request, no retry hidden
# inside the call. A failed request stays enumerable in the ledger and is
# replayed deliberately — which is safe precisely because the request is
# idempotent.
INVALIDATION_ATTEMPTS_PER_REQUEST: Final[int] = 1

# §B3 reason string: the invalidation capability was attempted and failed.
DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED: Final[str] = (
    "invalidation:attempt_failed"
)

# The ledger's entry kind for a request the service settled.
CODE_INVALIDATION_SETTLED: Final[str] = "invalidation_settled"

_EVENT_SETTLED: Final[str] = "settled"
_EVENT_FAILED: Final[str] = "failed"

_RESPONSE_COUNT_FIELDS: Final[tuple[str, ...]] = ("closed", "already_closed")

# Transport-level exceptions that mean「检索服务不可用」: refused
# connections, DNS failures and timeouts are all ``OSError`` subclasses.
_TRANSPORT_UNAVAILABLE: Final[tuple[type[BaseException], ...]] = (OSError,)


class InvalidationError(RuntimeError):
    """Base class for the invalidation path's named failures.

    Same shape as :class:`fin.retrieve.push_port.PushError`: a distinct
    stable :attr:`code` per subclass, plus the §B3 ``degraded`` /
    ``degraded_reasons`` classification. Kept as its own family because
    SC40 lists 失效失败 as a failure path of its own, so ``except
    InvalidationError`` cannot accidentally swallow a push failure or
    vice versa.
    """

    code: str = "invalidation_error"
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()


class InvalidationEmptyBatchError(InvalidationError):
    """An invalidation request carried no targets (§B2 第 5 条).

    Invalid input from the caller, not a legitimate no-op: refused by
    name instead of answered with a zero-closure success, and — being a
    caller error — it does not set ``degraded``.
    """

    code = "invalidation_empty_batch"


class InvalidationUnknownTargetError(InvalidationError):
    """A target names an idempotency key the store never accepted.

    Refused rather than skipped, because a silently skipped target is an
    old block that stays hittable. The whole request is refused before
    anything is closed, so the store keeps no half-done state.
    """

    code = "invalidation_unknown_target"


class InvalidationServiceUnavailableError(InvalidationError):
    """The retrieval service could not be reached.

    Nothing is known to have been closed, so the caller must treat the
    request as un-issued and replay it (safe: the request is idempotent).
    Distinct from :class:`InvalidationProtocolError`, where the service
    did answer.
    """

    code = "invalidation_service_unavailable"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED,)


class InvalidationProtocolError(InvalidationError):
    """The service answered off-contract.

    The envelope is missing a field, carries a wrong type or does not
    account for exactly the targets sent. The outcome cannot be trusted,
    so it is refused by name rather than coerced into a plausible
    :class:`InvalidationOutcome`.
    """

    code = "invalidation_protocol_error"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED,)


class InvalidationFailedError(InvalidationError):
    """A request failed for a reason that is not itself an
    :class:`InvalidationError`.

    An untyped transport exception is wrapped (``raise ... from exc``,
    original kept as ``__cause__``) rather than escaping untyped or being
    swallowed.
    """

    code = "invalidation_failed"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED,)


class InvalidationLedgerUnwritableError(InvalidationError):
    """The invalidation record could not be persisted.

    Raised instead of counting the entry in memory only: a record fin
    believes it wrote but did not is exactly the silence SC24 forbids.
    The invalidation itself may well have been applied — it is
    idempotent, so replaying the request after fixing the path is safe.
    """

    code = "invalidation_ledger_unwritable"


class InvalidationLedgerCorruptError(InvalidationError):
    """A persisted invalidation record could not be read back.

    A corrupt line is not skipped: skipping it would make the queryable
    record quietly smaller than what actually happened.
    """

    code = "invalidation_ledger_corrupt"


@dataclass(frozen=True)
class InvalidationOutcome:
    """The answer to one invalidation request.

    ``block_ids`` is parallel to the targets sent. ``closed`` counts
    intervals this request closed, ``already_closed`` counts targets that
    were closed by an earlier identical request. ``closed_at`` is the
    per-block closing instant, sorted by block id: it is what makes
    idempotency assertable — a replay returns the *same* instants, not
    fresh ones.
    """

    block_ids: tuple[str, ...]
    closed: int
    already_closed: int
    closed_at: tuple[tuple[str, int], ...]


@runtime_checkable
class InvalidationPort(Protocol):
    """Structural protocol for invalidation adapters.

    ``invalidate`` takes wire targets
    (:meth:`fin.corpus.invalidation.InvalidationTarget.to_wire` shape)
    and returns ``{"block_ids": [...], "closed": int, "already_closed":
    int, "closed_at": {block_id: int}}``. Implementations raise an
    :class:`InvalidationError` subclass on failure and never report a
    refused request as an empty success.
    """

    def invalidate(
        self, targets: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class InvalidationRecord:
    """One queryable ledger entry (SC24「失效记录可查询」)."""

    request_key: str
    code: str
    settled: bool
    source_refs: tuple[str, ...]
    block_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    closed: int
    already_closed: int
    message: str = ""


def validate_targets_not_empty(targets: Sequence[object]) -> None:
    """Raise :class:`InvalidationEmptyBatchError` for a request with no
    targets.

    Single authority for §B2 第 5 条 on this path, applied on both sides
    of the seam: the caller is refused before a pointless round trip, and
    the receiving side refuses independently so a future transport cannot
    smuggle an empty request past a client-only check.
    """
    if len(targets) == 0:
        raise InvalidationEmptyBatchError(
            "invalidation request carries no targets; an empty target set "
            "is invalid input, not a no-op"
        )


def outcome_of_invalidation_response(
    response: Any, *, target_count: int, block_ids: Sequence[str] = ()
) -> InvalidationOutcome:
    """Read one invalidation response envelope, or fail by name.

    Every field is checked before it is trusted: a mapping carrying
    ``block_ids`` plus the two counters and ``closed_at``, one block id
    (a string) per target, counters that are non-negative integers
    accounting for exactly the targets sent, and a closing instant for
    every block id. Anything else is an
    :class:`InvalidationProtocolError` — a short envelope is never padded
    and a stray one is never truncated into a plausible outcome.

    ``block_ids`` (the ids sent) is only used to sharpen the message.
    """
    if not isinstance(response, Mapping):
        raise InvalidationProtocolError(
            f"invalidation response is {type(response).__name__}, "
            "expected a mapping"
        )
    missing = [
        field
        for field in ("block_ids", *_RESPONSE_COUNT_FIELDS, "closed_at")
        if field not in response
    ]
    if missing:
        raise InvalidationProtocolError(
            f"invalidation response misses fields: {', '.join(missing)}"
        )

    answered = _response_block_ids(response["block_ids"], target_count)
    counts = _response_counts(response, target_count)
    closed_at = _response_closed_at(response["closed_at"], answered)
    if block_ids and set(answered) - set(block_ids):
        raise InvalidationProtocolError(
            "invalidation response answers for block ids that were not "
            "sent: " + ", ".join(sorted(set(answered) - set(block_ids)))
        )
    return InvalidationOutcome(
        block_ids=answered,
        closed=counts["closed"],
        already_closed=counts["already_closed"],
        closed_at=closed_at,
    )


def _response_block_ids(value: Any, target_count: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidationProtocolError(
            f"invalidation response block_ids is {type(value).__name__}, "
            "expected a sequence of block ids"
        )
    if len(value) != target_count:
        raise InvalidationProtocolError(
            f"invalidation response carries {len(value)} block ids for "
            f"{target_count} targets"
        )
    if not all(isinstance(block_id, str) for block_id in value):
        raise InvalidationProtocolError(
            "invalidation response block_ids contains a non-string block id"
        )
    return tuple(value)


def _response_counts(
    response: Mapping[str, Any], target_count: int
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for field in _RESPONSE_COUNT_FIELDS:
        value = response[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InvalidationProtocolError(
                f"invalidation response {field} is {value!r}, expected a "
                "non-negative integer"
            )
        counts[field] = value
    total = sum(counts.values())
    if total != target_count:
        raise InvalidationProtocolError(
            f"invalidation response accounts for {total} of {target_count} "
            "targets"
        )
    return counts


def _response_closed_at(
    value: Any, block_ids: Sequence[str]
) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, Mapping):
        raise InvalidationProtocolError(
            f"invalidation response closed_at is {type(value).__name__}, "
            "expected a mapping of block id to closing instant"
        )
    closed_at: list[tuple[str, int]] = []
    for block_id in block_ids:
        if block_id not in value:
            raise InvalidationProtocolError(
                f"invalidation response closed_at misses block {block_id!r}"
            )
        instant = value[block_id]
        if not isinstance(instant, int) or isinstance(instant, bool):
            raise InvalidationProtocolError(
                f"invalidation response closed_at[{block_id!r}] is "
                f"{instant!r}, expected an integer instant"
            )
        closed_at.append((block_id, instant))
    return tuple(sorted(closed_at))


class InvalidationLedger:
    """The queryable record of the invalidation requests fin issued.

    Append-only. With a ``path`` every entry is also a JSONL line and the
    ledger reloads by replaying them, so the record survives the process;
    without a path it is in-memory only (the default suite's shape).
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._records: list[InvalidationRecord] = []
        if path is not None and path.exists():
            self._replay(path)

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- queries -----

    def records(self) -> tuple[InvalidationRecord, ...]:
        """Every entry, in the order it was recorded."""
        return tuple(self._records)

    def failures(self) -> tuple[InvalidationRecord, ...]:
        """Entries for requests that failed — the replay work list."""
        return tuple(record for record in self._records if not record.settled)

    def settled_requests(self) -> tuple[str, ...]:
        """Distinct request keys the service settled, in first-seen order.

        A repeated (idempotent) request does not add a key here, though
        it does add an entry to :meth:`records`.
        """
        seen: dict[str, None] = {}
        for record in self._records:
            if record.settled:
                seen.setdefault(record.request_key, None)
        return tuple(seen)

    def records_for_source_ref(
        self, source_ref: str
    ) -> tuple[InvalidationRecord, ...]:
        """Entries touching one frozen unit."""
        return tuple(
            record
            for record in self._records
            if source_ref in record.source_refs
        )

    def count(self) -> int:
        return len(self._records)

    # ----- recording -----

    def record_settled(
        self,
        key: str,
        targets: Sequence[InvalidationTarget],
        outcome: InvalidationOutcome,
    ) -> InvalidationRecord:
        """Record that ``key`` was settled by the service."""
        return self._record(
            InvalidationRecord(
                request_key=key,
                code=CODE_INVALIDATION_SETTLED,
                settled=True,
                source_refs=_distinct(t.source_ref for t in targets),
                block_ids=tuple(t.block_id for t in targets),
                reasons=_distinct(t.reason for t in targets),
                closed=outcome.closed,
                already_closed=outcome.already_closed,
            ),
            _EVENT_SETTLED,
        )

    def record_failure(
        self,
        key: str,
        targets: Sequence[InvalidationTarget],
        *,
        code: str,
        message: str,
    ) -> InvalidationRecord:
        """Record that ``key`` failed, by name."""
        return self._record(
            InvalidationRecord(
                request_key=key,
                code=code,
                settled=False,
                source_refs=_distinct(t.source_ref for t in targets),
                block_ids=tuple(t.block_id for t in targets),
                reasons=_distinct(t.reason for t in targets),
                closed=0,
                already_closed=0,
                message=message,
            ),
            _EVENT_FAILED,
        )

    # ----- persistence -----

    def _record(
        self, record: InvalidationRecord, event: str
    ) -> InvalidationRecord:
        # Persist first, count second: an entry fin failed to write must
        # not be reported as recorded.
        self._append(record, event)
        self._records.append(record)
        return record

    def _append(self, record: InvalidationRecord, event: str) -> None:
        if self._path is None:
            return
        payload = {
            "event": event,
            "request_key": record.request_key,
            "code": record.code,
            "source_refs": list(record.source_refs),
            "block_ids": list(record.block_ids),
            "reasons": list(record.reasons),
            "closed": record.closed,
            "already_closed": record.already_closed,
            "message": record.message,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError as exc:
            raise InvalidationLedgerUnwritableError(
                f"cannot append the invalidation record for request "
                f"{record.request_key!r} to {self._path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _replay(self, path: Path) -> None:
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            self._records.append(self._record_of_line(path, number, line))

    def _record_of_line(
        self, path: Path, number: int, line: str
    ) -> InvalidationRecord:
        try:
            event = json.loads(line)
            settled = str(event["event"]) == _EVENT_SETTLED
            return InvalidationRecord(
                request_key=str(event["request_key"]),
                code=str(event["code"]),
                settled=settled,
                source_refs=tuple(str(ref) for ref in event["source_refs"]),
                block_ids=tuple(str(bid) for bid in event["block_ids"]),
                reasons=tuple(str(reason) for reason in event["reasons"]),
                closed=int(event["closed"]),
                already_closed=int(event["already_closed"]),
                message=str(event.get("message", "")),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise InvalidationLedgerCorruptError(
                f"{path}:{number} is not a readable invalidation record: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


def _distinct(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(str(value), None)
    return tuple(seen)


def invalidate_targets(
    port: InvalidationPort,
    targets: Sequence[InvalidationTarget],
    *,
    ledger: InvalidationLedger,
) -> InvalidationOutcome:
    """Issue one invalidation request through ``port`` and record it.

    The request is identified by
    :func:`fin.corpus.invalidation.request_key`, so re-issuing it is the
    same request and yields the same outcome. Every failure path — an
    unreachable service, an off-contract answer, a target the store never
    accepted, an untyped transport exception — leaves a named ledger
    entry and then re-raises a typed :class:`InvalidationError`; none of
    them returns an outcome, and none is retried here
    (:data:`INVALIDATION_ATTEMPTS_PER_REQUEST`).
    """
    validate_targets_not_empty(targets)
    key = request_key(targets)
    wire = [target.to_wire() for target in targets]
    try:
        response = port.invalidate(wire)
    except InvalidationError as exc:
        # already named by the receiving side — keep the name
        ledger.record_failure(key, targets, code=exc.code, message=str(exc))
        raise
    except _TRANSPORT_UNAVAILABLE as exc:
        unreachable = InvalidationServiceUnavailableError(
            f"invalidation request {key} failed to reach the retrieval "
            f"service: {type(exc).__name__}: {exc}"
        )
        ledger.record_failure(
            key, targets, code=unreachable.code, message=str(unreachable)
        )
        raise unreachable from exc
    except Exception as exc:  # transport-level: wrap, never swallow
        wrapped = InvalidationFailedError(
            f"invalidation request {key} failed: {type(exc).__name__}: {exc}"
        )
        ledger.record_failure(
            key, targets, code=wrapped.code, message=str(wrapped)
        )
        raise wrapped from exc

    try:
        outcome = outcome_of_invalidation_response(
            response,
            target_count=len(targets),
            block_ids=[target.block_id for target in targets],
        )
    except InvalidationProtocolError as exc:
        ledger.record_failure(key, targets, code=exc.code, message=str(exc))
        raise
    ledger.record_settled(key, targets, outcome)
    return outcome


__all__ = [
    "CODE_INVALIDATION_SETTLED",
    "DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED",
    "INVALIDATION_ATTEMPTS_PER_REQUEST",
    "InvalidationEmptyBatchError",
    "InvalidationError",
    "InvalidationFailedError",
    "InvalidationLedger",
    "InvalidationLedgerCorruptError",
    "InvalidationLedgerUnwritableError",
    "InvalidationOutcome",
    "InvalidationPort",
    "InvalidationProtocolError",
    "InvalidationRecord",
    "InvalidationServiceUnavailableError",
    "InvalidationUnknownTargetError",
    "invalidate_targets",
    "outcome_of_invalidation_response",
    "validate_targets_not_empty",
]
