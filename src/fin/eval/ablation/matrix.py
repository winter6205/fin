"""The ablation matrix: 10 configurations, straight off spec §B1 (T22 / SC31).

§B1「消融矩阵」row is the only authority for how many configurations
there are, how they are composed, how many questions the scripted subset
holds, and how many times each configuration repeats. The constants
below are fin's local copy; :func:`assert_matrix_matches_published`
re-reads the row out of ``specs/corpus-retrieval.md`` and refuses to
agree when the two sides differ, so "不得在本地另立一套" is checkable
rather than promised.

The 8 keys that get switched off come from the T19 capability snapshot
(``data/retrieval/capability_keys.json``); this module never restates
them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from fin.retrieve.capabilities import (
    CAPABILITY_COUNT,
    load_capability_keys,
    published_spec_path,
)


SPEC_ABLATION_ROW_LABEL = "消融矩阵"

ALL_ON_CONFIGURATION_ID = "all_on"
ALL_OFF_CONFIGURATION_ID = "all_off"
SINGLE_OFF_ID_PREFIX = "off_"


class AblationMatrixError(Exception):
    """Base for ablation-matrix failures. ``code`` is stable."""

    code = "ablation_matrix_error"


class AblationSpecRowMissingError(AblationMatrixError):
    """The spec no longer publishes the §B1 row the matrix is taken from."""

    code = "ablation_spec_row_missing"


class AblationMatrixDriftError(AblationMatrixError):
    """Local constants and the published §B1 row disagree; both are printed."""

    code = "ablation_matrix_drift"


class AblationMatrixIncompleteError(AblationMatrixError):
    """The built matrix does not hold the §B1 configuration count."""

    code = "ablation_matrix_incomplete"


class QuerySubsetOutOfBandError(AblationMatrixError):
    """The eval subset is outside the §B1 20–40 question band."""

    code = "ablation_query_subset_out_of_band"


class ConcurrencySpecRowMissingError(AblationMatrixError):
    """A required §B1 concurrency row or field is missing."""

    code = "concurrency_spec_row_missing"


class ConcurrencySpecDriftError(AblationMatrixError):
    """The imported concurrency contract differs from §B1."""

    code = "concurrency_spec_drift"


@dataclass(frozen=True)
class PublishedAblationNumbers:
    """The numbers as the spec publishes them."""

    total: int
    all_on: int
    single_off: int
    all_off: int
    subset_min: int
    subset_max: int
    min_repeats: int


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


@dataclass(frozen=True)
class AblationConfiguration:
    """One matrix cell: an id, its kind, and all 8 capability decisions."""

    configuration_id: str
    kind: str  # "all_on" | "single_off" | "all_off"
    disabled_capability: str | None
    capabilities: dict[str, bool]


_ROW_PATTERNS: dict[str, re.Pattern[str]] = {
    "total": re.compile(r"(\d+)\s*个配置"),
    "all_on": re.compile(r"全开\s*(\d+)"),
    "single_off": re.compile(r"逐项关闭\s*(\d+)"),
    "all_off": re.compile(r"全关\s*(\d+)"),
    "repeats": re.compile(r"每配置重复\s*≥\s*(\d+)\s*次"),
}
_SUBSET_PATTERN = re.compile(r"(\d+)\s*[–—-]\s*(\d+)\s*题")
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


def _spec_row(label: str, spec_path: Path | None = None) -> str:
    """Return the second cell of the §B1 table row named ``label``."""
    path = Path(spec_path) if spec_path is not None else published_spec_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AblationSpecRowMissingError(
            f"cannot read the published §B1 row from {path}: {exc}"
        ) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == label:
            return "|".join(cells[1:])
    raise AblationSpecRowMissingError(
        f"{path} publishes no 「{label}」 §B1 table row; the ablation matrix "
        "has nothing to take its numbers from"
    )


def published_ablation_numbers(
    spec_path: Path | None = None,
) -> PublishedAblationNumbers:
    """Read the §B1「消融矩阵」numbers back out of the spec."""
    row = _spec_row(SPEC_ABLATION_ROW_LABEL, spec_path)
    values: dict[str, int] = {}
    for name, pattern in _ROW_PATTERNS.items():
        match = pattern.search(row)
        if match is None:
            raise AblationSpecRowMissingError(
                f"§B1「{SPEC_ABLATION_ROW_LABEL}」row publishes no {name}: {row!r}"
            )
        values[name] = int(match.group(1))
    subset = _SUBSET_PATTERN.search(row)
    if subset is None:
        raise AblationSpecRowMissingError(
            f"§B1「{SPEC_ABLATION_ROW_LABEL}」row publishes no question band: {row!r}"
        )
    return PublishedAblationNumbers(
        total=values["total"],
        all_on=values["all_on"],
        single_off=values["single_off"],
        all_off=values["all_off"],
        subset_min=int(subset.group(1)),
        subset_max=int(subset.group(2)),
        min_repeats=values["repeats"],
    )


# ---- §B1「消融矩阵」行（逐字口径，不另立一套）----
#
# Keep the public constants for callers that need the published values, but
# initialize them from the spec row rather than maintaining a second literal
# copy in code. ``assert_matrix_matches_published`` below still detects a
# spec edit made after this module was imported.
_PUBLISHED_B1 = published_ablation_numbers()
B1_CONFIGURATION_TOTAL = _PUBLISHED_B1.total
B1_ALL_ON_CONFIGURATIONS = _PUBLISHED_B1.all_on
B1_SINGLE_OFF_CONFIGURATIONS = _PUBLISHED_B1.single_off
B1_ALL_OFF_CONFIGURATIONS = _PUBLISHED_B1.all_off
B1_QUERY_SUBSET_MIN = _PUBLISHED_B1.subset_min
B1_QUERY_SUBSET_MAX = _PUBLISHED_B1.subset_max
B1_MIN_REPEATS = _PUBLISHED_B1.min_repeats
B1_CONCURRENCY_CONTRACT = published_concurrency_contract()


def assert_matrix_matches_published(
    spec_path: Path | None = None,
) -> PublishedAblationNumbers:
    """The sync point: local constants must equal the published §B1 row.

    Returns the published numbers, or raises
    :class:`AblationMatrixDriftError` printing both sides — "they differ"
    alone would not say which one moved.
    """
    published = published_ablation_numbers(spec_path=spec_path)
    local = PublishedAblationNumbers(
        total=B1_CONFIGURATION_TOTAL,
        all_on=B1_ALL_ON_CONFIGURATIONS,
        single_off=B1_SINGLE_OFF_CONFIGURATIONS,
        all_off=B1_ALL_OFF_CONFIGURATIONS,
        subset_min=B1_QUERY_SUBSET_MIN,
        subset_max=B1_QUERY_SUBSET_MAX,
        min_repeats=B1_MIN_REPEATS,
    )
    if published != local:
        raise AblationMatrixDriftError(
            "ablation matrix drift: "
            f"local={local} published={published}"
        )
    if published.single_off != CAPABILITY_COUNT:
        raise AblationMatrixDriftError(
            "ablation matrix drift: §B1 publishes "
            f"{published.single_off} single-off configurations but the "
            f"capability snapshot holds {CAPABILITY_COUNT} keys"
        )
    return published


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


def build_ablation_matrix(
    keys: Sequence[str] | None = None,
) -> tuple[AblationConfiguration, ...]:
    """Build the 10 configurations: all-on 1 + single-off 8 + all-off 1.

    Every configuration decides all 8 keys explicitly — the eval seam
    (:meth:`CapabilityAwareFakePort.retrieve_for_eval`) rejects a partial
    one, so a forgotten key fails loudly instead of silently defaulting.
    """
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    configurations: list[AblationConfiguration] = [
        AblationConfiguration(
            configuration_id=ALL_ON_CONFIGURATION_ID,
            kind="all_on",
            disabled_capability=None,
            capabilities={key: True for key in resolved},
        )
    ]
    for disabled in resolved:
        configurations.append(
            AblationConfiguration(
                configuration_id=f"{SINGLE_OFF_ID_PREFIX}{disabled}",
                kind="single_off",
                disabled_capability=disabled,
                capabilities={key: key != disabled for key in resolved},
            )
        )
    configurations.append(
        AblationConfiguration(
            configuration_id=ALL_OFF_CONFIGURATION_ID,
            kind="all_off",
            disabled_capability=None,
            capabilities={key: False for key in resolved},
        )
    )
    if len(configurations) != B1_CONFIGURATION_TOTAL:
        raise AblationMatrixIncompleteError(
            f"ablation matrix holds {len(configurations)} configurations, "
            f"§B1 requires {B1_CONFIGURATION_TOTAL}"
        )
    return tuple(configurations)


def validate_query_subset_size(size: int) -> int:
    """Check the eval subset against the §B1 20–40 question band."""
    if not B1_QUERY_SUBSET_MIN <= size <= B1_QUERY_SUBSET_MAX:
        raise QuerySubsetOutOfBandError(
            f"ablation query subset holds {size} questions, §B1 requires "
            f"{B1_QUERY_SUBSET_MIN}–{B1_QUERY_SUBSET_MAX}"
        )
    return size


__all__ = [
    "ALL_OFF_CONFIGURATION_ID",
    "ALL_ON_CONFIGURATION_ID",
    "B1_ALL_OFF_CONFIGURATIONS",
    "B1_ALL_ON_CONFIGURATIONS",
    "B1_CONCURRENCY_CONTRACT",
    "B1_CONFIGURATION_TOTAL",
    "B1_MIN_REPEATS",
    "B1_QUERY_SUBSET_MAX",
    "B1_QUERY_SUBSET_MIN",
    "B1_SINGLE_OFF_CONFIGURATIONS",
    "SINGLE_OFF_ID_PREFIX",
    "SPEC_ABLATION_ROW_LABEL",
    "AblationConfiguration",
    "AblationMatrixDriftError",
    "AblationMatrixError",
    "AblationMatrixIncompleteError",
    "ConcurrencySpecDriftError",
    "ConcurrencySpecRowMissingError",
    "AblationSpecRowMissingError",
    "PublishedConcurrencyContract",
    "PublishedIdempotencyConcurrency",
    "PublishedL1Concurrency",
    "PublishedL2Concurrency",
    "PublishedLatencyThreshold",
    "PublishedAblationNumbers",
    "QuerySubsetOutOfBandError",
    "assert_concurrency_matches_published",
    "assert_matrix_matches_published",
    "build_ablation_matrix",
    "published_concurrency_contract",
    "published_ablation_numbers",
    "validate_query_subset_size",
]
