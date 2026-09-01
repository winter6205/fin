"""Named failures, constants, and wire shapes for the invalidation path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

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
