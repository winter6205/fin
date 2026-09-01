"""B1 acceptance validation for T23 concurrency scenario reports."""

from __future__ import annotations

from fin.eval.ablation.matrix import B1_CONCURRENCY_CONTRACT
from fin.eval.concurrency.errors import ConcurrencyRunError
from fin.eval.concurrency.report import IdempotencyReport, L1Report, L2Report


def _compare(value: float, operator: str, limit: float) -> bool:
    if operator == "<":
        return value < limit
    if operator == "<=":
        return value <= limit
    raise ConcurrencyRunError(
        f"unsupported published comparison operator {operator!r}"
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
