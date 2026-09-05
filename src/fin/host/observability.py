"""Small, dependency-free run observability helpers for T9.

The collector is intentionally injectable and process-local.  It records
only typed terminal statuses and latency aggregates; questions, answers,
URLs, exception text, and credentials never enter the snapshot.  A host
surface may expose :meth:`ObservabilityCollector.snapshot` as its minimal
observability payload without coupling this module to HTTP or the graph.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal


ObservationStatus = Literal["done", "refuse", "step_cap"]
OBSERVATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"done", "refuse", "step_cap"}
)
MAX_OBSERVATION_SAMPLES: Final[int] = 10_000


class ObservabilityError(Exception):
    """Base class for typed observability failures."""


class ObservabilityConfigurationError(ObservabilityError, ValueError):
    """Collector configuration is outside the bounded contract."""


class InvalidObservationStatusError(ObservabilityError):
    """A status outside the observable terminal-status vocabulary."""


class InvalidLatencyError(ObservabilityError):
    """A latency value is non-numeric, non-finite, or negative."""


class ObservationClockError(ObservabilityError):
    """The injected clock failed or returned an invalid value."""


@dataclass(frozen=True)
class Observation:
    """One bounded sample retained for local diagnostics."""

    status: ObservationStatus
    latency_ms: float


class ObservabilityCollector:
    """Thread-safe terminal counts and latency aggregates.

    ``clock`` is an injectable monotonic clock returning seconds.  The
    collector retains at most ``max_samples`` individual samples while
    aggregate counts remain available for the lifetime of the collector.
    This keeps diagnostics bounded without silently losing the status
    count exposed in the snapshot.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.perf_counter,
        max_samples: int = 1024,
    ) -> None:
        if (
            isinstance(max_samples, bool)
            or not isinstance(max_samples, int)
            or max_samples <= 0
            or max_samples > MAX_OBSERVATION_SAMPLES
        ):
            raise ObservabilityConfigurationError(
                f"max_samples must be an integer in [1, "
                f"{MAX_OBSERVATION_SAMPLES}]"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._clock = clock
        self._samples: deque[Observation] = deque(maxlen=max_samples)
        self._counts: dict[str, int] = {}
        self._latency_count = 0
        self._latency_total = 0.0
        self._retry_count = 0
        self._retry_exhausted_count = 0
        self._lock = threading.Lock()

    @property
    def sample_count(self) -> int:
        """Return the number of retained samples, not the lifetime count."""
        with self._lock:
            return len(self._samples)

    def record(self, status: str, *, latency_ms: float) -> None:
        """Record one terminal run without retaining sensitive run data."""
        _validate_status(status)
        latency = _validate_latency(latency_ms)
        sample = Observation(status=status, latency_ms=latency)
        with self._lock:
            self._counts[status] = self._counts.get(status, 0) + 1
            self._latency_count += 1
            self._latency_total += latency
            self._samples.append(sample)

    def measure(self, status: str) -> "_ObservationContext":
        """Return a context manager that records successful block latency."""
        _validate_status(status)
        return _ObservationContext(self, status)

    def record_rewrite_retry(self, *, exhausted: bool) -> None:
        """Count one performed empty-hit rewrite retry (T9).

        ``exhausted`` marks a retry whose follow-up retrieve was STILL
        an empty hit. These counters are deliberately separate from
        :meth:`snapshot`: the terminal-status vocabulary (``done`` /
        ``refuse`` / ``step_cap``) and the retry counters answer
        different questions, and the pinned ``snapshot()`` shape must
        not drift. Read them back with :meth:`retry_snapshot`.
        """
        if not isinstance(exhausted, bool):
            raise ObservabilityConfigurationError(
                "exhausted must be a boolean"
            )
        with self._lock:
            self._retry_count += 1
            if exhausted:
                self._retry_exhausted_count += 1

    def retry_snapshot(self) -> dict[str, int]:
        """Return the retry counters as a JSON-safe dict."""
        with self._lock:
            return {
                "count": self._retry_count,
                "exhausted": self._retry_exhausted_count,
            }

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-safe payload suitable for a health/status surface."""
        with self._lock:
            count = self._latency_count
            total = self._latency_total
            counts = dict(self._counts)
        average = total / count if count else 0.0
        return {
            "counts": counts,
            "latency_ms": {
                "count": count,
                "total": total,
                "average": average,
            },
        }

    def _clock_seconds(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise ObservationClockError(
                "the injected observability clock failed"
            ) from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ObservationClockError(
                "the injected observability clock must return a number"
            )
        if not math.isfinite(float(value)):
            raise ObservationClockError(
                "the injected observability clock returned a non-finite value"
            )
        return float(value)


class _ObservationContext:
    def __init__(self, collector: ObservabilityCollector, status: str) -> None:
        self._collector = collector
        self._status = status
        self._started: float | None = None

    def __enter__(self) -> "_ObservationContext":
        self._started = self._collector._clock_seconds()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> bool:
        if exc_type is not None:
            return False
        assert self._started is not None
        elapsed_ms = (
            self._collector._clock_seconds() - self._started
        ) * 1000.0
        self._collector.record(self._status, latency_ms=elapsed_ms)
        return False


# ----- process default (T9) -----


_default_collector: ObservabilityCollector | None = None
_default_collector_lock = threading.Lock()


def default_collector() -> ObservabilityCollector:
    """Return the per-process default collector, built once under a lock.

    Both the HTTP surface (``fin.host.http.get_observability_collector``)
    and the graph's retrieve tool (empty-hit rewrite retry counters)
    record on / read from this ONE instance. The singleton lives here —
    below ``graph`` and ``http`` — so the retrieve tool can reach it
    without a circular import (``http`` imports ``graph``). Callers that
    need isolation (tests) build their own :class:`ObservabilityCollector`
    instead.
    """
    global _default_collector
    with _default_collector_lock:
        if _default_collector is None:
            _default_collector = ObservabilityCollector()
        return _default_collector


def _validate_status(status: str) -> None:
    if not isinstance(status, str) or status not in OBSERVATION_STATUSES:
        raise InvalidObservationStatusError(
            f"status must be one of {sorted(OBSERVATION_STATUSES)}"
        )


def _validate_latency(latency_ms: float) -> float:
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
        raise InvalidLatencyError("latency_ms must be a number")
    value = float(latency_ms)
    if not math.isfinite(value) or value < 0:
        raise InvalidLatencyError(
            "latency_ms must be finite and greater than or equal to zero"
        )
    return value


__all__ = [
    "InvalidLatencyError",
    "InvalidObservationStatusError",
    "MAX_OBSERVATION_SAMPLES",
    "OBSERVATION_STATUSES",
    "Observation",
    "ObservationClockError",
    "ObservationStatus",
    "ObservabilityConfigurationError",
    "ObservabilityCollector",
    "ObservabilityError",
    "default_collector",
]
