"""T23 scenario report models and on-disk serialization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from fin.eval.ablation.matrix import (
    B1_CONCURRENCY_CONTRACT,
    PublishedConcurrencyContract,
    assert_concurrency_matches_published,
)
from fin.eval.concurrency.errors import ConcurrencyRunError


class ReportWriteError(ConcurrencyRunError):
    """A scenario report could not be persisted."""

    code = "concurrency_report_unwritable"


def _percentile(values: tuple[float, ...] | list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


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
        from fin.eval.concurrency.validation import validate_l1_report

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
        from fin.eval.concurrency.validation import validate_l2_report

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
        from fin.eval.concurrency.validation import validate_idempotency_report

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
