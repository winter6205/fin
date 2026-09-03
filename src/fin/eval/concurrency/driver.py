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

from fin.corpus.push import PushRecord
from fin.eval.ablation.matrix import assert_concurrency_matches_published
from fin.eval.concurrency.errors import (
    BlockCountUnavailableError,
    InvalidConcurrencyInputError,
)
from fin.eval.concurrency.execution import (
    BlockCount,
    Operation,
    _error_code,
    _run_phase,
)
from fin.eval.concurrency.report import IdempotencyReport, L1Report, L2Report


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


def _duration_reached(elapsed_seconds: float, target_seconds: float) -> bool:
    return elapsed_seconds >= target_seconds


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
