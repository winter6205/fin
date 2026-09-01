"""Decode invalidation response envelopes into outcomes."""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from fin.retrieve.invalidation_port.errors import (
    InvalidationOutcome,
    InvalidationProtocolError,
)

_RESPONSE_COUNT_FIELDS: Final[tuple[str, ...]] = ("closed", "already_closed")


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
