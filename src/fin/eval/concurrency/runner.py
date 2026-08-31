"""Hermetic and opt-in concurrency drivers for T23.

The three drivers in this module execute the same workload shape in
different modes:

* L1 mixes retrieval reads and idempotent writes at the §B1 read/write ratio.
* L2 invokes a supplied complete run, including the caller's LLM seam.
* the idempotency scenario performs only repeated writes of the same batch.

All defaults come from :mod:`fin.eval.ablation.matrix`. A short duration or a
request cap is an explicit smoke override; the returned report says that it
did not complete the B1 duration instead of presenting it as a pass.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from fin.corpus.push import PushRecord
from fin.eval.ablation.matrix import (
    B1_CONCURRENCY_CONTRACT,
    PublishedConcurrencyContract,
    assert_concurrency_matches_published,
)


Operation = Callable[[], Any]
BlockCount = Callable[[], int]


class ConcurrencyRunError(Exception):
    """Base class for named T23 driver failures."""

    code = "concurrency_run_error"


class InvalidConcurrencyInputError(ConcurrencyRunError):
    """A driver received a non-positive duration or request cap."""

    code = "concurrency_input_invalid"


class BlockCountUnavailableError(ConcurrencyRunError):
    """The benchmark could not obtain an integer block count."""

    code = "block_count_unavailable"


class L2CredentialsMissingError(ConcurrencyRunError):
    """The opt-in real LLM lane has no explicitly configured credential."""

    code = "l2_credentials_missing"


class L2BackendSetupError(ConcurrencyRunError):
    """The opt-in real LLM graph could not be constructed."""

    code = "l2_backend_setup_failed"


class ReportWriteError(ConcurrencyRunError):
    """A scenario report could not be persisted."""

    code = "concurrency_report_unwritable"


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


def _read_block_count(block_count: BlockCount) -> int:
    try:
        value = block_count()
    except Exception as exc:
        raise BlockCountUnavailableError(
            f"block count callback failed: {_error_code(exc)}"
        ) from exc
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BlockCountUnavailableError(
            f"block count callback returned {value!r}, expected a "
            "non-negative integer"
        )
    return value


def _duration(value: float | None, target: int) -> float:
    resolved = float(target if value is None else value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise InvalidConcurrencyInputError(
            f"duration must be a finite positive number, got {value!r}"
        )
    return resolved


def _request_cap(value: int | None) -> int | None:
    if value is not None and value <= 0:
        raise InvalidConcurrencyInputError(
            f"max_requests must be positive when supplied, got {value!r}"
        )
    return value


def _contract_dict(contract: PublishedConcurrencyContract) -> dict[str, Any]:
    return {
        "l1": {
            "concurrency": contract.l1.concurrency,
            "duration_seconds": contract.l1.duration_seconds,
            "read_write_ratio": list(contract.l1.read_write_ratio),
            "min_requests": contract.l1.min_requests,
            "error_count": contract.l1.error_count,
            "max_5xx_rate": contract.l1.max_5xx_rate,
            "five_xx_operator": contract.l1.five_xx_operator,
        },
        "l2": {
            "concurrency": contract.l2.concurrency,
            "duration_seconds": contract.l2.duration_seconds,
            "error_count": contract.l2.error_count,
            "p50_operator": contract.l2.p50_operator,
            "p50_ms": contract.l2.p50_ms,
            "p95_operator": contract.l2.p95_operator,
            "p95_ms": contract.l2.p95_ms,
        },
        "idempotency": {
            "concurrency": contract.idempotency.concurrency,
            "duration_seconds": contract.idempotency.duration_seconds,
            "error_count": contract.idempotency.error_count,
        },
        "latency": {
            "p95_ms": contract.latency.p95_ms,
            "p99_ms": contract.latency.p99_ms,
            "warmup_excluded": contract.latency.warmup_excluded,
        },
        "resource_budget": contract.resource_budget,
    }


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def _duration_reached(elapsed_seconds: float, target_seconds: float) -> bool:
    return elapsed_seconds >= target_seconds


@dataclass(frozen=True)
class L1Report:
    """Measured L1 workload and its B1 acceptance facts."""

    concurrency: int
    duration_seconds: float
    measured_requests: int
    read_requests: int
    write_requests: int
    error_count: int
    error_codes: tuple[str, ...]
    five_xx_count: int
    latencies_ms: tuple[float, ...]
    warmup_requests: int
    warmup_error_count: int
    elapsed_seconds: float
    block_total_before: int
    block_total_after: int
    target_duration_seconds: int
    duration_matches_b1: bool
    duration_reached: bool
    request_floor: int
    read_write_ratio: tuple[int, int]
    latency_p95_threshold_ms: int
    latency_p99_threshold_ms: int
    report_mode: str

    @property
    def p50_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.50)

    @property
    def p95_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.95)

    @property
    def p99_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.99)

    @property
    def five_xx_rate(self) -> float:
        if self.measured_requests == 0:
            return 0.0
        return self.five_xx_count / self.measured_requests

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": "l1",
            "status": "smoke" if self.report_mode == "smoke" else "measured",
            "report_mode": self.report_mode,
            "warmup_included_in_latency": not B1_CONCURRENCY_CONTRACT.latency.warmup_excluded,
            "spec_basis": {
                "source": "specs/corpus-retrieval.md §B1",
                "concurrency": B1_CONCURRENCY_CONTRACT.l1.concurrency,
                "duration_seconds": B1_CONCURRENCY_CONTRACT.l1.duration_seconds,
                "read_write_ratio": list(
                    B1_CONCURRENCY_CONTRACT.l1.read_write_ratio
                ),
                "min_requests": B1_CONCURRENCY_CONTRACT.l1.min_requests,
                "error_count": B1_CONCURRENCY_CONTRACT.l1.error_count,
                "five_xx_rate_operator": B1_CONCURRENCY_CONTRACT.l1.five_xx_operator,
                "five_xx_rate_limit": B1_CONCURRENCY_CONTRACT.l1.max_5xx_rate,
                "latency_p95_limit_ms": B1_CONCURRENCY_CONTRACT.latency.p95_ms,
                "latency_p99_limit_ms": B1_CONCURRENCY_CONTRACT.latency.p99_ms,
                "warmup_excluded": B1_CONCURRENCY_CONTRACT.latency.warmup_excluded,
            },
            "workload": {
                "concurrency": self.concurrency,
                "duration_seconds_requested": self.duration_seconds,
                "duration_seconds_elapsed": self.elapsed_seconds,
                "duration_matches_b1": self.duration_matches_b1,
                "duration_reached": self.duration_reached,
                "read_requests": self.read_requests,
                "write_requests": self.write_requests,
                "measured_requests": self.measured_requests,
                "warmup_requests": self.warmup_requests,
                "warmup_error_count": self.warmup_error_count,
            },
            "errors": {
                "count": self.error_count,
                "codes": list(self.error_codes),
                "five_xx_count": self.five_xx_count,
                "five_xx_rate": self.five_xx_rate,
            },
            "latency_ms": {
                "p50": self.p50_ms,
                "p95": self.p95_ms,
                "p99": self.p99_ms,
                "warmup_included": False,
            },
            "blocks": {
                "before": self.block_total_before,
                "after": self.block_total_after,
                "unchanged": self.block_total_before == self.block_total_after,
            },
            "passes_b1": not validate_l1_report(self),
            "b1_contract": _contract_dict(B1_CONCURRENCY_CONTRACT),
        }


def run_l1(
    *,
    read_operation: Operation,
    write_operation: Operation,
    block_count: BlockCount,
    duration_seconds: float | None = None,
    warmup_requests: int = 0,
    max_requests: int | None = None,
) -> L1Report:
    """Run L1 with B1 concurrency and workload ratio."""
    contract = assert_concurrency_matches_published()
    if warmup_requests < 0:
        raise InvalidConcurrencyInputError(
            f"warmup_requests must be non-negative, got {warmup_requests}"
        )
    duration = _duration(duration_seconds, contract.l1.duration_seconds)
    request_cap = _request_cap(max_requests)
    before = _read_block_count(block_count)
    read_weight, write_weight = contract.l1.read_write_ratio
    cycle_length = read_weight + write_weight
    if read_weight <= 0 or write_weight <= 0:
        raise InvalidConcurrencyInputError(
            f"published read/write ratio is invalid: {contract.l1.read_write_ratio}"
        )

    def operation_for_index(index: int) -> Operation:
        return read_operation if index % cycle_length < read_weight else write_operation

    phase = _run_phase(
        operation_for_index,
        concurrency=contract.l1.concurrency,
        duration_seconds=duration,
        warmup_requests=warmup_requests,
        max_requests=request_cap,
        cycle_length=cycle_length,
    )
    after = _read_block_count(block_count)
    read_requests = sum(
        1 for index in range(phase.measured_requests) if index % cycle_length < read_weight
    )
    write_requests = phase.measured_requests - read_requests
    return L1Report(
        concurrency=contract.l1.concurrency,
        duration_seconds=duration,
        measured_requests=phase.measured_requests,
        read_requests=read_requests,
        write_requests=write_requests,
        error_count=len(phase.error_codes),
        error_codes=phase.error_codes,
        five_xx_count=phase.five_xx_count,
        latencies_ms=phase.latencies_ms,
        warmup_requests=phase.warmup_requests,
        warmup_error_count=phase.warmup_error_count,
        elapsed_seconds=phase.elapsed_seconds,
        block_total_before=before,
        block_total_after=after,
        target_duration_seconds=contract.l1.duration_seconds,
        duration_matches_b1=duration == contract.l1.duration_seconds,
        duration_reached=_duration_reached(
            phase.elapsed_seconds, duration
        ),
        request_floor=contract.l1.min_requests,
        read_write_ratio=contract.l1.read_write_ratio,
        latency_p95_threshold_ms=contract.latency.p95_ms,
        latency_p99_threshold_ms=contract.latency.p99_ms,
        report_mode="smoke"
        if duration_seconds is not None or max_requests is not None
        else "b1",
    )


def validate_l1_report(report: L1Report) -> list[str]:
    """Return B1 L1 acceptance problems; an empty list is a pass."""
    contract = B1_CONCURRENCY_CONTRACT
    problems: list[str] = []
    if report.concurrency != contract.l1.concurrency:
        problems.append("L1 concurrency differs from §B1")
    if not report.duration_matches_b1 or not report.duration_reached:
        problems.append("L1 did not complete the §B1 duration")
    if report.measured_requests < contract.l1.min_requests:
        problems.append("L1 total requests are below the §B1 floor")
    read_weight, write_weight = contract.l1.read_write_ratio
    if report.read_write_ratio != contract.l1.read_write_ratio:
        problems.append("L1 report carries a non-§B1 read/write ratio")
    if report.read_requests * write_weight != report.write_requests * read_weight:
        problems.append("L1 workload does not keep the §B1 read/write ratio")
    if report.error_count != contract.l1.error_count:
        problems.append("L1 error count is not the §B1 value")
    if report.warmup_error_count != contract.l1.error_count:
        problems.append("L1 warm-up error count is not zero")
    five_xx_rate = report.five_xx_count / max(report.measured_requests, 1)
    if not _compare(
        five_xx_rate,
        contract.l1.five_xx_operator,
        contract.l1.max_5xx_rate,
    ):
        problems.append("L1 5xx rate is not below the §B1 limit")
    if report.p95_ms > contract.latency.p95_ms:
        problems.append("L1 p95 exceeds the §B1 latency threshold")
    if report.p99_ms > contract.latency.p99_ms:
        problems.append("L1 p99 exceeds the §B1 latency threshold")
    if report.block_total_before != report.block_total_after:
        problems.append("L1 block total changed")
    return problems


@dataclass(frozen=True)
class L2LatencyContract:
    """The L2-specific p50/p95 comparison, deliberately not L1's limit."""

    p50_operator: str
    p50_ms: int
    p95_operator: str
    p95_ms: int


def l2_latency_contract() -> L2LatencyContract:
    contract = assert_concurrency_matches_published()
    return L2LatencyContract(
        p50_operator=contract.l2.p50_operator,
        p50_ms=contract.l2.p50_ms,
        p95_operator=contract.l2.p95_operator,
        p95_ms=contract.l2.p95_ms,
    )


def _compare(value: float, operator: str, limit: float) -> bool:
    if operator == "<":
        return value < limit
    if operator == "<=":
        return value <= limit
    raise ConcurrencyRunError(
        f"unsupported published comparison operator {operator!r}"
    )


@dataclass(frozen=True)
class L2Report:
    """Measured full-run workload and its independent L2 acceptance facts."""

    concurrency: int
    duration_seconds: float
    measured_requests: int
    error_count: int
    error_codes: tuple[str, ...]
    latencies_ms: tuple[float, ...]
    llm_mode: str
    duration_matches_b1: bool
    warmup_requests: int = 0
    warmup_error_count: int = 0
    elapsed_seconds: float = 0.0
    duration_reached: bool = True
    report_mode: str = "b1"

    @property
    def p50_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.50)

    @property
    def p95_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.95)

    @property
    def p99_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.99)

    def to_dict(self) -> dict[str, Any]:
        contract = B1_CONCURRENCY_CONTRACT
        return {
            "scenario": "l2",
            "status": "smoke" if self.report_mode == "smoke" else "measured",
            "report_mode": self.report_mode,
            "llm": {
                "mode": self.llm_mode,
                "real_call_attempted": self.llm_mode == "real",
                "fake_structural_path": self.llm_mode == "fake",
            },
            "spec_basis": {
                "source": "specs/corpus-retrieval.md §B1",
                "concurrency": contract.l2.concurrency,
                "duration_seconds": contract.l2.duration_seconds,
                "error_count": contract.l2.error_count,
                "p50_operator": contract.l2.p50_operator,
                "p50_limit_ms": contract.l2.p50_ms,
                "p95_operator": contract.l2.p95_operator,
                "p95_limit_ms": contract.l2.p95_ms,
                "l1_latency_threshold_applied": False,
            },
            "l1_latency_threshold_applied": False,
            "workload": {
                "concurrency": self.concurrency,
                "duration_seconds_requested": self.duration_seconds,
                "duration_seconds_elapsed": self.elapsed_seconds,
                "duration_matches_b1": self.duration_matches_b1,
                "duration_reached": self.duration_reached,
                "measured_requests": self.measured_requests,
                "warmup_requests": self.warmup_requests,
                "warmup_error_count": self.warmup_error_count,
            },
            "errors": {
                "count": self.error_count,
                "codes": list(self.error_codes),
            },
            "latency_ms": {
                "p50": self.p50_ms,
                "p95": self.p95_ms,
                "p99": self.p99_ms,
                "warmup_included": False,
            },
            "passes_b1": not validate_l2_report(self),
            "b1_contract": _contract_dict(contract),
        }


def run_l2(
    *,
    full_run: Operation,
    llm_mode: str,
    duration_seconds: float | None = None,
    warmup_requests: int = 0,
    max_requests: int | None = None,
) -> L2Report:
    """Run complete runs at B1 concurrency; the callable owns the LLM call."""
    contract = assert_concurrency_matches_published()
    if warmup_requests < 0:
        raise InvalidConcurrencyInputError(
            f"warmup_requests must be non-negative, got {warmup_requests}"
        )
    duration = _duration(duration_seconds, contract.l2.duration_seconds)
    request_cap = _request_cap(max_requests)
    phase = _run_phase(
        lambda _index: full_run,
        concurrency=contract.l2.concurrency,
        duration_seconds=duration,
        warmup_requests=warmup_requests,
        max_requests=request_cap,
        cycle_length=1,
    )
    return L2Report(
        concurrency=contract.l2.concurrency,
        duration_seconds=duration,
        measured_requests=phase.measured_requests,
        error_count=len(phase.error_codes),
        error_codes=phase.error_codes,
        latencies_ms=phase.latencies_ms,
        llm_mode=llm_mode,
        duration_matches_b1=duration == contract.l2.duration_seconds,
        warmup_requests=phase.warmup_requests,
        warmup_error_count=phase.warmup_error_count,
        elapsed_seconds=phase.elapsed_seconds,
        duration_reached=_duration_reached(
            phase.elapsed_seconds, duration
        ),
        report_mode="smoke"
        if duration_seconds is not None or max_requests is not None
        else "b1",
    )


def validate_l2_report(report: L2Report) -> list[str]:
    """Return L2 problems using only its own B1 p50/p95 judgement."""
    contract = B1_CONCURRENCY_CONTRACT
    problems: list[str] = []
    if report.concurrency != contract.l2.concurrency:
        problems.append("L2 concurrency differs from §B1")
    if not report.duration_matches_b1 or not report.duration_reached:
        problems.append("L2 did not complete the §B1 duration")
    if report.error_count != contract.l2.error_count:
        problems.append("L2 error count is not the §B1 value")
    if report.warmup_error_count != contract.l2.error_count:
        problems.append("L2 warm-up error count is not zero")
    if not report.latencies_ms:
        problems.append("L2 recorded no measured latency")
    else:
        if not _compare(
            report.p50_ms, contract.l2.p50_operator, contract.l2.p50_ms
        ):
            problems.append("L2 p50 fails its §B1 criterion")
        if not _compare(
            report.p95_ms, contract.l2.p95_operator, contract.l2.p95_ms
        ):
            problems.append("L2 p95 fails its §B1 criterion")
    return problems


@dataclass(frozen=True)
class IdempotencyReport:
    """Measured all-write workload and block-total invariance."""

    concurrency: int
    duration_seconds: float
    measured_requests: int
    error_count: int
    error_codes: tuple[str, ...]
    warmup_requests: int
    warmup_error_count: int
    elapsed_seconds: float
    block_total_before: int
    block_total_after: int
    successful_writes: int
    duplicate_writes: int
    target_duration_seconds: int
    duration_matches_b1: bool
    duration_reached: bool
    report_mode: str

    def to_dict(self) -> dict[str, Any]:
        contract = B1_CONCURRENCY_CONTRACT
        return {
            "scenario": "idempotency",
            "status": "smoke" if self.report_mode == "smoke" else "measured",
            "report_mode": self.report_mode,
            "all_writes": True,
            "spec_basis": {
                "source": "specs/corpus-retrieval.md §B1",
                "concurrency": contract.idempotency.concurrency,
                "duration_seconds": contract.idempotency.duration_seconds,
                "error_count": contract.idempotency.error_count,
                "all_writes": True,
            },
            "workload": {
                "concurrency": self.concurrency,
                "duration_seconds_requested": self.duration_seconds,
                "duration_seconds_elapsed": self.elapsed_seconds,
                "duration_matches_b1": self.duration_matches_b1,
                "duration_reached": self.duration_reached,
                "measured_requests": self.measured_requests,
                "warmup_requests": self.warmup_requests,
                "warmup_error_count": self.warmup_error_count,
                "successful_writes": self.successful_writes,
                "duplicate_writes": self.duplicate_writes,
            },
            "errors": {
                "count": self.error_count,
                "codes": list(self.error_codes),
            },
            "blocks": {
                "before": self.block_total_before,
                "after": self.block_total_after,
                "unchanged": self.block_total_before == self.block_total_after,
            },
            "passes_b1": not validate_idempotency_report(self),
            "b1_contract": _contract_dict(contract),
        }


def run_idempotency(
    *,
    write_operation: Operation,
    block_count: BlockCount,
    duration_seconds: float | None = None,
    warmup_requests: int = 0,
    max_requests: int | None = None,
) -> IdempotencyReport:
    """Run repeated all-write calls at the §B1 idempotency concurrency."""
    contract = assert_concurrency_matches_published()
    if warmup_requests < 0:
        raise InvalidConcurrencyInputError(
            f"warmup_requests must be non-negative, got {warmup_requests}"
        )
    duration = _duration(
        duration_seconds, contract.idempotency.duration_seconds
    )
    request_cap = _request_cap(max_requests)
    before = _read_block_count(block_count)
    phase = _run_phase(
        lambda _index: write_operation,
        concurrency=contract.idempotency.concurrency,
        duration_seconds=duration,
        warmup_requests=warmup_requests,
        max_requests=request_cap,
        cycle_length=1,
    )
    after = _read_block_count(block_count)
    return IdempotencyReport(
        concurrency=contract.idempotency.concurrency,
        duration_seconds=duration,
        measured_requests=phase.measured_requests,
        error_count=len(phase.error_codes),
        error_codes=phase.error_codes,
        warmup_requests=phase.warmup_requests,
        warmup_error_count=phase.warmup_error_count,
        elapsed_seconds=phase.elapsed_seconds,
        block_total_before=before,
        block_total_after=after,
        successful_writes=phase.written_count,
        duplicate_writes=phase.duplicate_count,
        target_duration_seconds=contract.idempotency.duration_seconds,
        duration_matches_b1=duration == contract.idempotency.duration_seconds,
        duration_reached=_duration_reached(
            phase.elapsed_seconds, duration
        ),
        report_mode="smoke"
        if duration_seconds is not None or max_requests is not None
        else "b1",
    )


def validate_idempotency_report(report: IdempotencyReport) -> list[str]:
    """Return all-write B1 acceptance problems."""
    contract = B1_CONCURRENCY_CONTRACT
    problems: list[str] = []
    if report.concurrency != contract.idempotency.concurrency:
        problems.append("idempotency concurrency differs from §B1")
    if not report.duration_matches_b1 or not report.duration_reached:
        problems.append("idempotency run did not complete the §B1 duration")
    if report.error_count != contract.idempotency.error_count:
        problems.append("idempotency error count is not zero")
    if report.warmup_error_count != contract.idempotency.error_count:
        problems.append("idempotency warm-up error count is not zero")
    if report.block_total_before != report.block_total_after:
        problems.append("idempotency block total changed")
    return problems


def build_contract_fake_push_records() -> tuple[PushRecord, ...]:
    """Return one real ``PushRecord`` for the opt-in contract-fake lanes."""
    return (
        PushRecord(
            block_id="t23-contract-block",
            source_ref=(
                '{"document_id":"T23-2023Q1","unit":1,'
                '"version":"a"}'
            ),
            content_id="t23-contract-content",
            text="A frozen T23 passage used by the idempotency benchmark.",
            parent_block_id="t23-contract-parent",
            parent_text="A frozen T23 passage used by the idempotency benchmark.",
            metadata={
                "fmt": "txt",
                "doc_type": "narrative",
                "section": "T23",
                "source_sha256": "a" * 64,
                "parse_rule_version": "parse-1",
                "params_table_version": "params-1",
            },
        ),
    )


__all__ = [
    "BlockCountUnavailableError",
    "ConcurrencyRunError",
    "IdempotencyReport",
    "InvalidConcurrencyInputError",
    "L1Report",
    "L2BackendSetupError",
    "L2CredentialsMissingError",
    "L2LatencyContract",
    "L2Report",
    "ReportWriteError",
    "build_contract_fake_push_records",
    "l2_latency_contract",
    "run_idempotency",
    "run_l1",
    "run_l2",
    "validate_idempotency_report",
    "validate_l1_report",
    "validate_l2_report",
]
