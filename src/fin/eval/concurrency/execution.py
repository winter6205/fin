"""Shared thread-pool execution primitives for T23 concurrency drivers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fin.eval.concurrency.errors import ConcurrencyRunError

Operation = Callable[[], Any]
BlockCount = Callable[[], int]


@dataclass
class _WorkerStats:
    measured_requests: int
    latencies_ms: list[float]
    error_codes: list[str]
    five_xx_count: int = 0
    written_count: int = 0
    duplicate_count: int = 0
    warmup_errors: int = 0


@dataclass(frozen=True)
class _PhaseStats:
    measured_requests: int
    warmup_requests: int
    latencies_ms: tuple[float, ...]
    error_codes: tuple[str, ...]
    five_xx_count: int
    written_count: int
    duplicate_count: int
    warmup_error_count: int
    elapsed_seconds: float


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__


def _status_code(response: Any) -> int | None:
    if isinstance(response, Mapping):
        value = response.get("status_code")
    else:
        value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _response_counts(response: Any) -> tuple[int, int]:
    if isinstance(response, Mapping):
        written = response.get("written", 0)
        duplicates = response.get("duplicates", 0)
    else:
        written = getattr(response, "written", 0)
        duplicates = getattr(response, "duplicates", 0)
    return (
        written if isinstance(written, int) and not isinstance(written, bool) else 0,
        duplicates
        if isinstance(duplicates, int) and not isinstance(duplicates, bool)
        else 0,
    )


def _record_operation(
    operation: Operation,
    stats: _WorkerStats,
    *,
    measured: bool,
) -> None:
    started = time.perf_counter()
    if measured:
        stats.measured_requests += 1
    try:
        response = operation()
    except Exception as exc:
        status_code = _status_code(exc)
        if measured:
            stats.error_codes.append(_error_code(exc))
            stats.latencies_ms.append(
                (time.perf_counter() - started) * 1000.0
            )
            if status_code is not None and 500 <= status_code <= 599:
                stats.five_xx_count += 1
        else:
            stats.warmup_errors += 1
        return

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    status_code = _status_code(response)
    if status_code is not None and 500 <= status_code <= 599:
        if measured:
            stats.five_xx_count += 1
            stats.error_codes.append(f"http_{status_code}")
        else:
            stats.warmup_errors += 1
    if not measured:
        return
    stats.latencies_ms.append(elapsed_ms)
    written, duplicates = _response_counts(response)
    stats.written_count += written
    stats.duplicate_count += duplicates


def _claim(
    state: dict[str, Any],
    *,
    deadline: float,
    cycle_length: int,
    max_requests: int | None,
) -> int | None:
    """Claim one measured operation, stopping only at a full workload cycle."""
    with state["lock"]:
        index = int(state["next_index"])
        if max_requests is not None and index >= max_requests:
            return None
        if time.perf_counter() >= deadline and index % cycle_length == 0:
            return None
        state["next_index"] = index + 1
        return index


def _run_phase(
    operation_for_index: Callable[[int], Operation],
    *,
    concurrency: int,
    duration_seconds: float,
    warmup_requests: int,
    max_requests: int | None,
    cycle_length: int,
) -> _PhaseStats:
    """Run warm-up and measured work with bounded per-worker state."""
    state: dict[str, Any] = {
        "lock": threading.Lock(),
        "next_index": 0,
        "warmup_index": 0,
        "measurement_started": None,
        "deadline": None,
    }
    workers = [
        _WorkerStats(measured_requests=0, latencies_ms=[], error_codes=[])
        for _ in range(concurrency)
    ]
    barrier = threading.Barrier(concurrency)

    def worker(worker_stats: _WorkerStats) -> None:
        while True:
            with state["lock"]:
                index = int(state["warmup_index"])
                if index >= warmup_requests:
                    break
                state["warmup_index"] = index + 1
            _record_operation(
                operation_for_index(index % cycle_length),
                worker_stats,
                measured=False,
            )

        barrier.wait()
        with state["lock"]:
            if state["measurement_started"] is None:
                started = time.perf_counter()
                state["measurement_started"] = started
                state["deadline"] = started + duration_seconds
            deadline = float(state["deadline"])
        while True:
            index = _claim(
                state,
                deadline=deadline,
                cycle_length=cycle_length,
                max_requests=max_requests,
            )
            if index is None:
                return
            _record_operation(
                operation_for_index(index % cycle_length),
                worker_stats,
                measured=True,
            )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, workers))
    measurement_started = state["measurement_started"]
    if not isinstance(measurement_started, float):
        raise ConcurrencyRunError(
            "worker pool completed without entering the measured phase"
        )
    elapsed = time.perf_counter() - measurement_started

    latencies = tuple(
        latency
        for worker_stats in workers
        for latency in worker_stats.latencies_ms
    )
    error_codes = tuple(
        code for worker_stats in workers for code in worker_stats.error_codes
    )
    return _PhaseStats(
        measured_requests=sum(worker.measured_requests for worker in workers),
        warmup_requests=warmup_requests,
        latencies_ms=latencies,
        error_codes=error_codes,
        five_xx_count=sum(worker.five_xx_count for worker in workers),
        written_count=sum(worker.written_count for worker in workers),
        duplicate_count=sum(worker.duplicate_count for worker in workers),
        warmup_error_count=sum(worker.warmup_errors for worker in workers),
        elapsed_seconds=elapsed,
    )
