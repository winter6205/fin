"""Issue invalidation requests through a port and record outcomes."""

from __future__ import annotations

from typing import Final, Sequence

from fin.corpus.invalidation import InvalidationTarget, request_key

from fin.retrieve.invalidation_port.codec import outcome_of_invalidation_response
from fin.retrieve.invalidation_port.errors import (
    InvalidationError,
    InvalidationFailedError,
    InvalidationOutcome,
    InvalidationPort,
    InvalidationProtocolError,
    InvalidationServiceUnavailableError,
    validate_targets_not_empty,
)
from fin.retrieve.invalidation_port.ledger import InvalidationLedger

# Transport-level exceptions that mean「检索服务不可用」: refused
# connections, DNS failures and timeouts are all ``OSError`` subclasses.
_TRANSPORT_UNAVAILABLE: Final[tuple[type[BaseException], ...]] = (OSError,)


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
