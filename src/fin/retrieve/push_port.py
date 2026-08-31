"""Push port: the idempotent push seam and its named failures
(plan T14, SC18 / SC20, §B4).

The port is the seam between fin's corpus side (which owns parsing,
chunking and the record shape — :mod:`fin.corpus.push`) and the
retrieval service (which stores and recalls the pushed text verbatim,
§B4「不二次切分」). This ticket ships the seam plus the contract fake
(:mod:`fin.retrieve.fake_push_port`); the real transport to the
iknow-memory service lands with a later ticket.

Idempotency contract (§B4 / SC18)
---------------------------------

The write identity is the record's idempotency key — the
``(source_ref, content identity)`` pair. Pushing the same batch again
(the §B1「幂等基准」row: :data:`IDEMPOTENCY_BASELINE_PUSHES` = 3
consecutive pushes) must leave the block total increment at 0 and the
block id set unchanged, and must return the same block ids.

Failure contract
----------------

A batch whose record misses a SC20 metadata field is refused **as a
whole** with :class:`PushMetadataIncompleteError`: the receiving side
validates every record before it writes any, so a refused batch writes
nothing and cannot half-land. Failures are typed and carry a stable
``code`` — never
``None``, never an empty success, never a silent drop. A refused batch
is not a write and therefore does not count towards the idempotency
assertion (the store's rejection counter keeps the trace).

SC21 splits the transport's two failure shapes into distinguishable
codes: :class:`PushServiceUnavailableError` (the service was never
reached) and :class:`PushProtocolError` (it answered off-contract).
Neither returns an outcome and neither is retried inside this module
(:data:`PUSH_ATTEMPTS_PER_BATCH`), so a failed push cannot be recorded
as a write and cannot be settled by a local artifact — the replay path
is :mod:`fin.retrieve.push_batching`, whose journal keeps the failure
enumerable.

An empty block batch is a third thing: **invalid input** per §B2 第 5
条, refused with :class:`PushEmptyBatchError` rather than answered with
a zero-write success, and — unlike the two capability failures above —
it does not set ``degraded`` (see :class:`PushError`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from fin.corpus.push import REQUIRED_PUSH_METADATA_FIELDS, PushRecord

# §B1「幂等基准」: same batch pushed 3 times in a row → block total
# increment = 0, block id set unchanged.
IDEMPOTENCY_BASELINE_PUSHES: Final[int] = 3

# SC44: the number of records one push call accepts. The value itself is
# open headroom (plan T15「单批上限的具体取值全部开放」); what is not open
# is that crossing it is a named overflow rather than a truncation, and
# that the batch planner (:mod:`fin.retrieve.push_batching`) reads the
# same constant so a planned run never trips it.
MAX_PUSH_BATCH_RECORDS: Final[int] = 200

# T16 retry policy: one attempt per batch, no retry inside the push
# path. SC21 forbids a *silent* retry; rather than pick a hidden retry
# count here, a failed batch surfaces as a named failure and is replayed
# deliberately through :mod:`fin.retrieve.push_batching` (whose journal
# keeps the failure enumerable). Tests assert the transport was called
# exactly this many times on a failure.
PUSH_ATTEMPTS_PER_BATCH: Final[int] = 1

# §B3 reason strings: which capability item, and whether it was never
# attempted or attempted and failed.
DEGRADED_REASON_PUSH_ATTEMPT_FAILED: Final[str] = "push:attempt_failed"

# Transport-level exceptions that mean「检索服务不可用」(SC21). Socket
# errors, refused connections, DNS failures and timeouts are all
# ``OSError`` subclasses (``TimeoutError`` included since 3.10).
_TRANSPORT_UNAVAILABLE: Final[tuple[type[BaseException], ...]] = (OSError,)

_RESPONSE_COUNT_FIELDS: Final[tuple[str, ...]] = ("written", "duplicates")


class PushError(RuntimeError):
    """Base class for the push path's named failures.

    Every subclass carries a distinct stable :attr:`code`; callers that
    must branch on the failure read ``exc.code`` instead of matching on
    message text. The push path never signals failure by returning
    ``None`` / an empty result / a sentinel number.

    :attr:`degraded` classifies the failure against §B2 第 5 条 and §B3
    (field names verbatim ``degraded`` / ``degraded_reasons``): invalid
    input from the caller is *not* a capability failure and never sets
    the flag, while a capability that was attempted and failed does.
    This ticket only *declares* the classification — reading the flag
    off a retrieval response envelope and acting on it is T20's scope.
    """

    code: str = "push_error"
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()


class PushMetadataIncompleteError(PushError):
    """A pushed record misses one or more SC20 metadata fields.

    The offending block id and the missing field names are in the
    message. The batch is refused as a whole — nothing is written.
    """

    code = "push_metadata_incomplete"


class UnknownPushedBlockError(PushError):
    """A recall asked for a block id the store never accepted.

    Raised rather than answered with ``None`` so that handing the store
    a `source_ref` (which is not block identity, §B4) fails loudly.
    """

    code = "unknown_pushed_block"


class PushBatchOverflowError(PushError):
    """One push call carries more records than the cap allows (SC44).

    Raised instead of storing a prefix and dropping the rest: an
    over-sized batch is a caller error the batch planner exists to
    prevent, so it surfaces as a named failure with nothing written.
    """

    code = "push_batch_overflow"


class PushBatchSizeInvalidError(PushError):
    """A batch size of zero or less was requested.

    A non-positive size cannot split a non-empty record sequence, so it
    would silently produce no batches — refused by name instead.
    """

    code = "push_batch_size_invalid"


class PushBatchFailedError(PushError):
    """A batch failed for a reason that is not itself a :class:`PushError`.

    The background push path (§B4) must leave a named record for every
    failure, so a transport-level exception is wrapped (``raise ... from
    exc``, original preserved as ``__cause__``) rather than escaping
    untyped or being swallowed.
    """

    code = "push_batch_failed"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_PUSH_ATTEMPT_FAILED,)


class PushServiceUnavailableError(PushError):
    """The retrieval service could not be reached (SC21).

    The transport never got an answer, so nothing about the batch is
    known to have landed: the caller must treat the batch as unwritten
    and replay it. Distinct from :class:`PushProtocolError`, where the
    service *did* answer.
    """

    code = "push_service_unavailable"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_PUSH_ATTEMPT_FAILED,)


class PushProtocolError(PushError):
    """The service answered off-contract (SC21).

    The response envelope is missing a field, carries a wrong type, or
    does not account for every pushed record. Since the outcome cannot
    be trusted, it is refused by name rather than coerced into a
    plausible-looking :class:`PushOutcome`.
    """

    code = "push_protocol_error"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_PUSH_ATTEMPT_FAILED,)


class PushEmptyBatchError(PushError):
    """A push carried no records at all (§B2 第 5 条).

    An empty block batch is **invalid input**, not a legitimate empty
    result: it is refused by name instead of answered with a zero-write
    success, and — being a caller error rather than a capability
    failure — it does not set ``degraded``.
    """

    code = "push_empty_batch"


@dataclass(frozen=True)
class PushOutcome:
    """The result of one push batch.

    ``block_ids`` is parallel to the pushed records: a record whose
    idempotency key was already stored yields the **stored** block id,
    so a replay returns the same ids as the original write. ``written``
    counts first-time writes, ``duplicates`` counts records recognized
    as an existing write.
    """

    block_ids: tuple[str, ...]
    written: int
    duplicates: int


@runtime_checkable
class PushPort(Protocol):
    """Structural protocol for push adapters.

    ``push`` takes wire records (``PushRecord.to_wire()`` shape) and
    returns ``{"block_ids": [...], "written": int, "duplicates": int}``.
    Implementations raise a :class:`PushError` subclass on failure and
    never report a refused batch as an empty success.
    """

    def push(
        self, records: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:  # pragma: no cover - protocol
        ...


def missing_metadata_fields(wire: Mapping[str, Any]) -> tuple[str, ...]:
    """The SC20 metadata fields absent or empty on one wire record.

    A present-but-empty value counts as missing: an empty format or an
    empty parse rule version is not a filterable value.
    """
    metadata = wire.get("metadata")
    if not isinstance(metadata, Mapping):
        return REQUIRED_PUSH_METADATA_FIELDS
    return tuple(
        field
        for field in REQUIRED_PUSH_METADATA_FIELDS
        if not metadata.get(field)
    )


def validate_wire_record(wire: Mapping[str, Any]) -> None:
    """Raise :class:`PushMetadataIncompleteError` when the record's SC20
    metadata is incomplete (SC20's rejection path)."""
    missing = missing_metadata_fields(wire)
    if missing:
        raise PushMetadataIncompleteError(
            f"push of block {wire.get('block_id')!r} misses metadata "
            f"fields: {', '.join(missing)}"
        )


def validate_batch_not_empty(records: Sequence[object]) -> None:
    """Raise :class:`PushEmptyBatchError` for a batch with no records.

    Single authority for §B2 第 5 条's empty-batch rule, applied on both
    sides of the seam: the caller is refused before a pointless round
    trip, and the receiving side refuses independently so a future
    transport cannot smuggle an empty batch past a client-only check.
    """
    if len(records) == 0:
        raise PushEmptyBatchError(
            "push batch is empty; an empty block batch is invalid input, "
            "not an empty result"
        )


def outcome_of_response(
    response: Any, *, record_count: int
) -> PushOutcome:
    """Read one push response envelope, or fail by name (SC21).

    Every field of the §B4 response shape is checked before it is
    trusted: the envelope must be a mapping carrying ``block_ids`` plus
    the two counters, one block id (a string) per pushed record, and
    counters that are non-negative integers accounting for exactly the
    records pushed. Anything else is a :class:`PushProtocolError` — a
    short envelope is never padded and a stray one is never truncated
    into a plausible outcome.
    """
    if not isinstance(response, Mapping):
        raise PushProtocolError(
            f"push response is {type(response).__name__}, expected a mapping"
        )
    missing = [
        field
        for field in ("block_ids", *_RESPONSE_COUNT_FIELDS)
        if field not in response
    ]
    if missing:
        raise PushProtocolError(
            f"push response misses fields: {', '.join(missing)}"
        )

    block_ids = response["block_ids"]
    if isinstance(block_ids, (str, bytes)) or not isinstance(
        block_ids, Sequence
    ):
        raise PushProtocolError(
            f"push response block_ids is {type(block_ids).__name__}, "
            "expected a sequence of block ids"
        )
    if len(block_ids) != record_count:
        raise PushProtocolError(
            f"push response carries {len(block_ids)} block ids for "
            f"{record_count} pushed records"
        )
    if not all(isinstance(block_id, str) for block_id in block_ids):
        raise PushProtocolError(
            "push response block_ids contains a non-string block id"
        )

    counts: dict[str, int] = {}
    for field in _RESPONSE_COUNT_FIELDS:
        value = response[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PushProtocolError(
                f"push response {field} is {value!r}, expected a "
                "non-negative integer"
            )
        counts[field] = value
    total = sum(counts.values())
    if total != record_count:
        raise PushProtocolError(
            f"push response accounts for {total} of {record_count} pushed "
            "records"
        )
    return PushOutcome(
        block_ids=tuple(block_ids),
        written=counts["written"],
        duplicates=counts["duplicates"],
    )


def push_records(
    port: PushPort, records: Sequence[PushRecord]
) -> PushOutcome:
    """Push ``records`` through ``port`` idempotently.

    Metadata completeness is the receiving side's judgement (SC20:
    「缺字段的推送**被拒**」), so this function does not pre-screen the
    batch for it: it hands the wire records over and lets the typed
    :class:`PushMetadataIncompleteError` propagate. A refused batch
    therefore leaves its trace where the refusal happened, and one
    validation authority (:func:`validate_wire_record`) serves both
    sides.

    The two SC21 failure shapes are separated here, because only this
    function sees both the transport and the envelope: an unreachable
    service becomes :class:`PushServiceUnavailableError` (the original
    ``OSError`` kept as ``__cause__``), an off-contract answer becomes
    :class:`PushProtocolError`. Neither is retried
    (:data:`PUSH_ATTEMPTS_PER_BATCH`) and neither returns an outcome, so
    a failed batch is never recorded as a write.
    """
    validate_batch_not_empty(records)
    try:
        response = port.push([record.to_wire() for record in records])
    except PushError:
        # already named by the receiving side (SC20 / SC44) — leave it
        raise
    except _TRANSPORT_UNAVAILABLE as exc:
        raise PushServiceUnavailableError(
            f"push to the retrieval service failed to reach it: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return outcome_of_response(response, record_count=len(records))


__all__ = [
    "DEGRADED_REASON_PUSH_ATTEMPT_FAILED",
    "IDEMPOTENCY_BASELINE_PUSHES",
    "MAX_PUSH_BATCH_RECORDS",
    "PUSH_ATTEMPTS_PER_BATCH",
    "PushBatchFailedError",
    "PushBatchOverflowError",
    "PushBatchSizeInvalidError",
    "PushEmptyBatchError",
    "PushError",
    "PushMetadataIncompleteError",
    "PushOutcome",
    "PushPort",
    "PushProtocolError",
    "PushServiceUnavailableError",
    "UnknownPushedBlockError",
    "missing_metadata_fields",
    "outcome_of_response",
    "push_records",
    "validate_batch_not_empty",
    "validate_wire_record",
]
