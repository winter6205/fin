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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from fin.corpus.push import REQUIRED_PUSH_METADATA_FIELDS, PushRecord

# §B1「幂等基准」: same batch pushed 3 times in a row → block total
# increment = 0, block id set unchanged.
IDEMPOTENCY_BASELINE_PUSHES: Final[int] = 3


class PushError(RuntimeError):
    """Base class for the push path's named failures.

    Every subclass carries a distinct stable :attr:`code`; callers that
    must branch on the failure read ``exc.code`` instead of matching on
    message text. The push path never signals failure by returning
    ``None`` / an empty result / a sentinel number.
    """

    code: str = "push_error"


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


def push_records(
    port: PushPort, records: Sequence[PushRecord]
) -> PushOutcome:
    """Push ``records`` through ``port`` idempotently.

    Metadata completeness is the receiving side's judgement (SC20:
    「缺字段的推送**被拒**」), so this function does not pre-screen the
    batch: it hands the wire records over and lets the typed
    :class:`PushMetadataIncompleteError` propagate. A refused batch
    therefore leaves its trace where the refusal happened, and one
    validation authority (:func:`validate_wire_record`) serves both
    sides.
    """
    response = port.push([record.to_wire() for record in records])
    return PushOutcome(
        block_ids=tuple(response["block_ids"]),
        written=int(response["written"]),
        duplicates=int(response["duplicates"]),
    )


__all__ = [
    "IDEMPOTENCY_BASELINE_PUSHES",
    "PushError",
    "PushMetadataIncompleteError",
    "PushOutcome",
    "PushPort",
    "UnknownPushedBlockError",
    "missing_metadata_fields",
    "push_records",
    "validate_wire_record",
]
