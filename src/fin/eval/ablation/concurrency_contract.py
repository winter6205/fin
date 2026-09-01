"""§B1 T23 concurrency numbers: read from the spec, not a second local copy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fin.eval.ablation.b1_spec_row import _spec_row
from fin.eval.ablation.matrix import AblationMatrixError


class ConcurrencySpecRowMissingError(AblationMatrixError):
    """A required §B1 concurrency row or field is missing."""

    code = "concurrency_spec_row_missing"


class ConcurrencySpecDriftError(AblationMatrixError):
    """The imported concurrency contract differs from §B1."""

    code = "concurrency_spec_drift"


@dataclass(frozen=True)
class PublishedL1Concurrency:
    """§B1「并发 L1」numbers and its error-rate judgement."""

    concurrency: int
    duration_seconds: int
    read_write_ratio: tuple[int, int]
    min_requests: int
    error_count: int
    max_5xx_rate: float
    five_xx_operator: str


@dataclass(frozen=True)
class PublishedL2Concurrency:
    """§B1「并发 L2」numbers and its own latency judgement."""

    concurrency: int
    duration_seconds: int
    error_count: int
    p50_operator: str
    p50_ms: int
    p95_operator: str
    p95_ms: int


@dataclass(frozen=True)
class PublishedIdempotencyConcurrency:
    """§B1「并发幂等场景（全写）」numbers."""

    concurrency: int
    duration_seconds: int
    error_count: int


@dataclass(frozen=True)
class PublishedLatencyThreshold:
    """§B1「延迟阈值」numbers and warm-up accounting rule."""

    p95_ms: int
    p99_ms: int
    warmup_excluded: bool


@dataclass(frozen=True)
class PublishedConcurrencyContract:
    """All T23 concurrency numbers read from §B1 in one object."""

    l1: PublishedL1Concurrency
    l2: PublishedL2Concurrency
    idempotency: PublishedIdempotencyConcurrency
    latency: PublishedLatencyThreshold
    resource_budget: str


_CONCURRENCY_PATTERN = re.compile(r"并发\s*(\d+)")
_DURATION_PATTERN = re.compile(r"持续\s*(\d+)\s*秒")
_RATIO_PATTERN = re.compile(r"读写比\s*(\d+)\s*:\s*(\d+)")
_MIN_REQUESTS_PATTERN = re.compile(r"总请求数\s*[≥>=]+\s*(\d+)")
_ERROR_COUNT_PATTERN = re.compile(r"错误数\s*=\s*(\d+)")
_FIVE_XX_PATTERN = re.compile(r"5xx\s*([<≤])\s*(\d+(?:\.\d+)?)\s*%")


def _required_match(
    pattern: re.Pattern[str],
    row: str,
    *,
    field: str,
    label: str,
) -> re.Match[str]:
    match = pattern.search(row)
    if match is None:
        raise ConcurrencySpecRowMissingError(
            f"§B1「{label}」row publishes no {field}: {row!r}"
        )
    return match


def _operator(symbol: str) -> str:
    return "<=" if symbol == "≤" else symbol


def _latency_match(
    row: str,
    *,
    percentile_name: str,
    unit: str,
    label: str,
) -> re.Match[str]:
    pattern = re.compile(
        rf"{re.escape(percentile_name)}\s*([<≤])\s*(\d+)\s*{unit}"
    )
    return _required_match(
        pattern, row, field=percentile_name, label=label
    )


def published_concurrency_contract(
    spec_path: Path | None = None,
) -> PublishedConcurrencyContract:
    """Read every T23 number from its §B1 row, without another local source."""
    l1_label = "并发 L1（检索 / 读取路径，不含 LLM）"
    l2_label = "并发 L2（完整 run，含 LLM）"
    idempotency_label = "并发幂等场景（全写）"
    latency_label = "延迟阈值"

    l1_row = _spec_row(l1_label, spec_path=spec_path)
    l1_error = _required_match(
        _ERROR_COUNT_PATTERN, l1_row, field="错误数", label=l1_label
    )
    five_xx = _required_match(
        _FIVE_XX_PATTERN, l1_row, field="5xx", label=l1_label
    )
    l1_ratio = _required_match(
        _RATIO_PATTERN, l1_row, field="读写比", label=l1_label
    )
    l1 = PublishedL1Concurrency(
        concurrency=int(
            _required_match(
                _CONCURRENCY_PATTERN,
                l1_row,
                field="并发数",
                label=l1_label,
            ).group(1)
        ),
        duration_seconds=int(
            _required_match(
                _DURATION_PATTERN,
                l1_row,
                field="持续时长",
                label=l1_label,
            ).group(1)
        ),
        read_write_ratio=(int(l1_ratio.group(1)), int(l1_ratio.group(2))),
        min_requests=int(
            _required_match(
                _MIN_REQUESTS_PATTERN,
                l1_row,
                field="总请求数下限",
                label=l1_label,
            ).group(1)
        ),
        error_count=int(l1_error.group(1)),
        max_5xx_rate=float(five_xx.group(2)) / 100.0,
        five_xx_operator=_operator(five_xx.group(1)),
    )

    l2_row = _spec_row(l2_label, spec_path=spec_path)
    l2_p50 = _latency_match(
        l2_row, percentile_name="p50", unit="s", label=l2_label
    )
    l2_p95 = _latency_match(
        l2_row, percentile_name="p95", unit="s", label=l2_label
    )
    l2 = PublishedL2Concurrency(
        concurrency=int(
            _required_match(
                _CONCURRENCY_PATTERN,
                l2_row,
                field="并发数",
                label=l2_label,
            ).group(1)
        ),
        duration_seconds=int(
            _required_match(
                _DURATION_PATTERN,
                l2_row,
                field="持续时长",
                label=l2_label,
            ).group(1)
        ),
        error_count=int(
            _required_match(
                _ERROR_COUNT_PATTERN,
                l2_row,
                field="错误数",
                label=l2_label,
            ).group(1)
        ),
        p50_operator=_operator(l2_p50.group(1)),
        p50_ms=int(l2_p50.group(2)) * 1000,
        p95_operator=_operator(l2_p95.group(1)),
        p95_ms=int(l2_p95.group(2)) * 1000,
    )

    idempotency_row = _spec_row(idempotency_label, spec_path=spec_path)
    idempotency = PublishedIdempotencyConcurrency(
        concurrency=int(
            _required_match(
                _CONCURRENCY_PATTERN,
                idempotency_row,
                field="并发数",
                label=idempotency_label,
            ).group(1)
        ),
        duration_seconds=int(
            _required_match(
                _DURATION_PATTERN,
                idempotency_row,
                field="持续时长",
                label=idempotency_label,
            ).group(1)
        ),
        error_count=int(
            _required_match(
                _ERROR_COUNT_PATTERN,
                idempotency_row,
                field="错误数",
                label=idempotency_label,
            ).group(1)
        ),
    )

    latency_row = _spec_row(latency_label, spec_path=spec_path)
    latency_p95 = _latency_match(
        latency_row, percentile_name="p95", unit="ms", label=latency_label
    )
    latency_p99 = _latency_match(
        latency_row, percentile_name="p99", unit="ms", label=latency_label
    )
    return PublishedConcurrencyContract(
        l1=l1,
        l2=l2,
        idempotency=idempotency,
        latency=PublishedLatencyThreshold(
            p95_ms=int(latency_p95.group(2)),
            p99_ms=int(latency_p99.group(2)),
            warmup_excluded="预热请求不计入" in latency_row,
        ),
        resource_budget=_spec_row("评测资源预算", spec_path=spec_path),
    )


B1_CONCURRENCY_CONTRACT = published_concurrency_contract()


def assert_concurrency_matches_published(
    spec_path: Path | None = None,
) -> PublishedConcurrencyContract:
    """Refuse a run when its imported T23 contract drifted from §B1."""
    published = published_concurrency_contract(spec_path=spec_path)
    if published != B1_CONCURRENCY_CONTRACT:
        raise ConcurrencySpecDriftError(
            "T23 concurrency contract drift: "
            f"local={B1_CONCURRENCY_CONTRACT} published={published}"
        )
    return published


__all__ = [
    "B1_CONCURRENCY_CONTRACT",
    "ConcurrencySpecDriftError",
    "ConcurrencySpecRowMissingError",
    "PublishedConcurrencyContract",
    "PublishedIdempotencyConcurrency",
    "PublishedL1Concurrency",
    "PublishedL2Concurrency",
    "PublishedLatencyThreshold",
    "assert_concurrency_matches_published",
    "published_concurrency_contract",
]
