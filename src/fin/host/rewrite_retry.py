"""Injectable empty-hit rewrite retry for the research tool loop (T9).

This helper retries only the retrieve contract's explicit empty-hit shape
``{"chunks": []}``.  Typed retrieve errors and memory recall results are
returned unchanged.  The result contains no citation or evidence field:
memory recall therefore cannot accidentally become citation evidence.

The helper does not choose tools or build a retrieve pipeline.  Callers
inject both the retrieve operation and the query rewriter, keeping the
existing agent loop in charge of orchestration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final


MAX_REWRITE_RETRIES: Final[int] = 3

Retrieve = Callable[[str], Mapping[str, object]]
Rewrite = Callable[[str], str]


class RewriteRetryError(Exception):
    """Base class for typed retry failures."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RewriteConfigurationError(RewriteRetryError):
    """Retry configuration or input is outside the helper contract."""


class MalformedRetrieveResultError(RewriteRetryError):
    """The injected retrieve callable returned an invalid result shape."""


class RetrieveCallError(RewriteRetryError):
    """The injected retrieve callable raised an exception."""


@dataclass(frozen=True)
class RetryAttempt:
    """A query sent to the injected retrieve callable."""

    query: str
    empty_hit: bool


@dataclass(frozen=True)
class RewriteRetryResult:
    """Final retrieve result plus auditable, non-sensitive retry metadata."""

    result: Mapping[str, object]
    attempts: tuple[RetryAttempt, ...]

    @property
    def retry_count(self) -> int:
        return max(0, len(self.attempts) - 1)

    @property
    def exhausted(self) -> bool:
        return bool(self.attempts and self.attempts[-1].empty_hit)


def retrieve_with_rewrite_retry(
    query: str,
    retrieve: Retrieve,
    rewrite: Rewrite,
    *,
    max_retries: int = 1,
) -> RewriteRetryResult:
    """Retrieve ``query`` and rewrite only after explicit empty hits.

    ``max_retries`` counts additional retrieve calls after the original
    query and is bounded so a bad rewriter cannot create an unbounded loop.
    An empty or whitespace-only original query is sent once and never
    rewritten, preserving the retrieve port's empty-query miss contract.
    """
    _validate_inputs(query, retrieve, rewrite, max_retries)

    current_query = query
    attempts: list[RetryAttempt] = []
    result = _retrieve(current_query, retrieve)
    empty_hit = _is_empty_hit(result)
    attempts.append(RetryAttempt(current_query, empty_hit))

    while empty_hit and len(attempts) - 1 < max_retries:
        if not query.strip():
            break
        current_query = _rewrite(current_query, rewrite)
        result = _retrieve(current_query, retrieve)
        empty_hit = _is_empty_hit(result)
        attempts.append(RetryAttempt(current_query, empty_hit))

    return RewriteRetryResult(result=result, attempts=tuple(attempts))


# Short name for callers that already have the tool-loop context.
retry_empty_hit = retrieve_with_rewrite_retry


def _validate_inputs(
    query: str,
    retrieve: Retrieve,
    rewrite: Rewrite,
    max_retries: int,
) -> None:
    if not isinstance(query, str):
        raise RewriteConfigurationError("query must be a string")
    if not callable(retrieve) or not callable(rewrite):
        raise RewriteConfigurationError("retrieve and rewrite must be callable")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
        or max_retries > MAX_REWRITE_RETRIES
    ):
        raise RewriteConfigurationError(
            f"max_retries must be an integer in [0, {MAX_REWRITE_RETRIES}]"
        )


def _retrieve(query: str, retrieve: Retrieve) -> Mapping[str, object]:
    try:
        result = retrieve(query)
    except Exception as exc:
        raise RetrieveCallError("retrieve_failed") from exc
    if not isinstance(result, Mapping):
        raise MalformedRetrieveResultError("retrieve_result_not_mapping")
    if "chunks" in result and not isinstance(result["chunks"], (list, tuple)):
        raise MalformedRetrieveResultError("retrieve_chunks_not_sequence")
    return result


def _is_empty_hit(result: Mapping[str, object]) -> bool:
    """Recognise retrieve misses, never ``memories`` recall payloads."""
    if "error" in result:
        return False
    return "chunks" in result and not result["chunks"]


def _rewrite(query: str, rewrite: Rewrite) -> str:
    try:
        rewritten = rewrite(query)
    except Exception as exc:
        raise RewriteRetryError("rewrite_failed") from exc
    if not isinstance(rewritten, str) or not rewritten.strip():
        raise RewriteRetryError("rewrite_empty")
    if rewritten.strip() == query.strip():
        raise RewriteRetryError("rewrite_unchanged")
    return rewritten


__all__ = [
    "MAX_REWRITE_RETRIES",
    "MalformedRetrieveResultError",
    "RetrieveCallError",
    "RetryAttempt",
    "RewriteConfigurationError",
    "RewriteRetryError",
    "RewriteRetryResult",
    "retrieve_with_rewrite_retry",
    "retry_empty_hit",
]
