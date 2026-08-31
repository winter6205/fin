"""Ablation matrix + quantified report (T22, SC31–SC35).

One command runs all 10 §B1 configurations, repeats each at least 3
times, keeps every raw value next to its median, and writes a report
that pairs each percentage with the sample size and the baseline
absolute it came from.

Entry point: ``uv run python -m fin.eval.ablation run`` (opt-in; the
default ``uv run pytest -q`` neither runs nor collects it).
"""

from __future__ import annotations

from fin.eval.ablation.matrix import (
    ALL_OFF_CONFIGURATION_ID,
    ALL_ON_CONFIGURATION_ID,
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    B1_QUERY_SUBSET_MAX,
    B1_QUERY_SUBSET_MIN,
    AblationConfiguration,
    AblationMatrixError,
    assert_matrix_matches_published,
    build_ablation_matrix,
)
from fin.eval.ablation.metrics import (
    METRIC_FIELDS,
    EvalQuery,
    QueryOutcome,
    RetrievedHit,
    assert_metrics_match_published,
    evaluate_outcomes,
)
from fin.eval.ablation.report import (
    ABLATION_REPORT_VERSION,
    AblationReportError,
    build_report,
    merge_with_retrieval_report,
    validate_report,
)
from fin.eval.ablation.runner import (
    AblationRun,
    AblationRunError,
    ConfigurationResult,
    MetricSeries,
    run_ablation,
)

__all__ = [
    "ABLATION_REPORT_VERSION",
    "ALL_OFF_CONFIGURATION_ID",
    "ALL_ON_CONFIGURATION_ID",
    "B1_CONFIGURATION_TOTAL",
    "B1_MIN_REPEATS",
    "B1_QUERY_SUBSET_MAX",
    "B1_QUERY_SUBSET_MIN",
    "METRIC_FIELDS",
    "AblationConfiguration",
    "AblationMatrixError",
    "AblationReportError",
    "AblationRun",
    "AblationRunError",
    "ConfigurationResult",
    "EvalQuery",
    "MetricSeries",
    "QueryOutcome",
    "RetrievedHit",
    "assert_matrix_matches_published",
    "assert_metrics_match_published",
    "build_ablation_matrix",
    "build_report",
    "evaluate_outcomes",
    "merge_with_retrieval_report",
    "run_ablation",
    "validate_report",
]
