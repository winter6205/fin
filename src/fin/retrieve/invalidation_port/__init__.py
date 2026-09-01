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

from fin.corpus.invalidation import InvalidationTarget
from fin.retrieve.invalidation_port.codec import outcome_of_invalidation_response
from fin.retrieve.invalidation_port.errors import (
    CODE_INVALIDATION_SETTLED,
    DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED,
    INVALIDATION_ATTEMPTS_PER_REQUEST,
    InvalidationEmptyBatchError,
    InvalidationError,
    InvalidationFailedError,
    InvalidationLedgerCorruptError,
    InvalidationLedgerUnwritableError,
    InvalidationOutcome,
    InvalidationPort,
    InvalidationProtocolError,
    InvalidationRecord,
    InvalidationServiceUnavailableError,
    InvalidationUnknownTargetError,
    validate_targets_not_empty,
)
from fin.retrieve.invalidation_port.ledger import InvalidationLedger
from fin.retrieve.invalidation_port.operation import invalidate_targets

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
    "InvalidationTarget",
    "InvalidationUnknownTargetError",
    "invalidate_targets",
    "outcome_of_invalidation_response",
    "validate_targets_not_empty",
]
