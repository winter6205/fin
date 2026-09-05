"""The quantified report: absolutes, percentages, contributions (T22 / SC32–SC35).

Three rules shape this module, all from the spec:

* **SC32** — every configuration carries the full §B1 metric set and a
  percentage improvement over the「全关」baseline, as a *number*.
* **SC35** — no claim may be a bare percentage. Every improvement claim
  carries ``sample_size``, ``baseline_absolute`` and
  ``configuration_absolute`` next to it, and the report's summary states
  how many claims lack them (the acceptance value is 0).
* **SC34** — the report gives 全开 minus「仅关掉某一项」for every
  capability and every metric; ``missing_contribution_count`` is the
  number of gaps (acceptance value 0).

A percentage over a zero baseline is *undefined*, not zero and not a
failure: the all-off configuration legitimately retrieves nothing, so
"infinitely better" has no number. Those claims carry
``percent_status = "undefined_zero_baseline"`` and still give both
absolutes and the sample size, which is what SC35 actually asks for.
The count is surfaced in the summary rather than buried.

The report is built to be merged with the retrieval side's report
(§B1「指标集」判定: 两侧报告合并后指标集完整):
:func:`merge_with_retrieval_report` joins on the merge keys and refuses
to overwrite a metric either side already produced — fin never
recomputes the retrieval side's dimensions, and 长文档召回 /
跨文档多跳召回 are not fin's to compute at all (SC38).

Atomic write (T3 / ADR 0042)
----------------------------

The baseline ``report.json`` is the canonical §B1 artifact, so a half-
written document on a crash — or two simultaneous ``run`` invocations
tearing each other's writes — would silently lower the numbers the
operator reads. :func:`write_report_atomically` serializes through a
sibling lock file (``fcntl.flock``) and then writes to a temp file
before renaming onto the target, so the document on disk is always a
whole one or the previous whole one.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fin.eval.ablation.matrix import (
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    B1_QUERY_SUBSET_MAX,
    B1_QUERY_SUBSET_MIN,
)
from fin.eval.ablation.metrics import (
    FIN_OWNED_METRICS,
    LOWER_IS_BETTER,
    METRIC_DEFINITIONS,
    METRIC_FIELDS,
)
from fin.eval.ablation.runner import AblationRun


ABLATION_REPORT_VERSION = "1.0.0"
REPORT_FILENAME = "report.json"

#: Merge keys: two reports describe the same experiment only when all
#: three agree.
MERGE_KEYS: tuple[str, ...] = (
    "dataset_id",
    "query_subset_hash",
    "configuration_id",
)

#: SC38: these are the retrieval service's dimensions. fin does not
#: compute them, and the count of them in fin's metric list is 0.
DIMENSIONS_NOT_COMPUTED_BY_FIN: tuple[str, ...] = (
    "长文档召回",
    "跨文档多跳召回",
)

PERCENT_COMPUTED = "computed"
PERCENT_UNDEFINED_ZERO_BASELINE = "undefined_zero_baseline"


class AblationReportError(Exception):
    """Base for report failures. ``code`` is stable and assertable."""

    code = "ablation_report_error"


class AblationMergeKeyMismatchError(AblationReportError):
    """The two reports do not describe the same experiment."""

    code = "ablation_merge_key_mismatch"


class AblationMergeMetricConflictError(AblationReportError):
    """The other side recomputed a metric this side already produced."""

    code = "ablation_merge_metric_conflict"


def query_subset_hash(query_ids: Sequence[str]) -> str:
    """Stable hash of the eval subset, used as a merge key."""
    payload = json.dumps(list(query_ids), ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _direction(
    metric: str, delta: float, *, percent_status: str | None = None
) -> str:
    if delta == 0.0:
        return "unchanged"
    if percent_status == PERCENT_UNDEFINED_ZERO_BASELINE:
        return "unquantifiable_zero_baseline"
    better = delta < 0.0 if metric in LOWER_IS_BETTER else delta > 0.0
    return "improvement" if better else "regression"


def _percent(baseline: float, value: float) -> tuple[float | None, str]:
    """Percentage change of ``value`` against ``baseline``.

    Returns ``(percent, status)``. A zero baseline has no percentage —
    that is a property of the arithmetic, not a failure to measure, and
    the caller still receives both absolutes.
    """
    if baseline == 0.0:
        return None, PERCENT_UNDEFINED_ZERO_BASELINE
    return (value - baseline) / abs(baseline) * 100.0, PERCENT_COMPUTED


def _improvement_claims(run: AblationRun) -> list[dict[str, Any]]:
    baseline = run.configuration(run.baseline_configuration_id)
    claims: list[dict[str, Any]] = []
    for config in run.configurations:
        if config.configuration_id == baseline.configuration_id:
            continue
        for metric in METRIC_FIELDS:
            baseline_absolute = baseline.median(metric)
            configuration_absolute = config.median(metric)
            delta = configuration_absolute - baseline_absolute
            percent, status = _percent(baseline_absolute, configuration_absolute)
            claims.append(
                {
                    "claim_id": f"{config.configuration_id}::{metric}",
                    "configuration_id": config.configuration_id,
                    "baseline_configuration_id": baseline.configuration_id,
                    "metric": metric,
                    "sample_size": run.sample_size,
                    "repeats": config.repeats,
                    "baseline_absolute": baseline_absolute,
                    "configuration_absolute": configuration_absolute,
                    "delta_absolute": delta,
                    "percent_improvement": percent,
                    "percent_status": status,
                    "direction": _direction(
                        metric, delta, percent_status=status
                    ),
                }
            )
    return claims


def _capability_contributions(run: AblationRun) -> list[dict[str, Any]]:
    """全开 minus「仅关掉某一项」, per capability and per metric (SC34)."""
    all_on = run.configuration(run.all_on_configuration_id)
    contributions: list[dict[str, Any]] = []
    for config in run.configurations:
        if config.kind != "single_off" or config.disabled_capability is None:
            continue
        for metric in METRIC_FIELDS:
            all_on_absolute = all_on.median(metric)
            single_off_absolute = config.median(metric)
            delta = all_on_absolute - single_off_absolute
            percent, status = _percent(single_off_absolute, all_on_absolute)
            contributions.append(
                {
                    "capability": config.disabled_capability,
                    "metric": metric,
                    "single_off_configuration_id": config.configuration_id,
                    "sample_size": run.sample_size,
                    "repeats": config.repeats,
                    "all_on_absolute": all_on_absolute,
                    "single_off_absolute": single_off_absolute,
                    "delta_absolute": delta,
                    "percent_contribution": percent,
                    "percent_status": status,
                    "direction": _direction(
                        metric, delta, percent_status=status
                    ),
                }
            )
    return contributions


def _percent_only_claim_count(claims: Sequence[Mapping[str, Any]]) -> int:
    """Claims carrying a percentage but not both absolutes + sample size."""
    count = 0
    for claim in claims:
        if claim.get("percent_status") != PERCENT_COMPUTED:
            continue
        has_absolutes = isinstance(
            claim.get("baseline_absolute"), (int, float)
        ) and isinstance(claim.get("configuration_absolute"), (int, float))
        has_sample = isinstance(claim.get("sample_size"), int) and (
            claim.get("sample_size", 0) > 0
        )
        if not (has_absolutes and has_sample):
            count += 1
    return count


def build_report(
    run: AblationRun,
    *,
    dataset_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the JSON-serializable ablation report."""
    metadata = dict(run.metadata)
    resolved_dataset_id = dataset_id or str(metadata.get("dataset_id", "unspecified"))
    subset_hash = query_subset_hash(run.query_ids)
    claims = _improvement_claims(run)
    contributions = _capability_contributions(run)
    capability_count = sum(
        1 for config in run.configurations if config.kind == "single_off"
    )
    expected_contributions = capability_count * len(METRIC_FIELDS)

    configurations = [
        {
            "configuration_id": config.configuration_id,
            "kind": config.kind,
            "disabled_capability": config.disabled_capability,
            "capabilities": dict(config.capabilities),
            "repeats": config.repeats,
            "error_codes": list(config.error_codes),
            "metrics": {
                metric: {
                    "raw": list(config.metrics[metric].raw),
                    "median": config.metrics[metric].median,
                }
                for metric in METRIC_FIELDS
            },
        }
        for config in run.configurations
    ]

    return {
        "report_version": ABLATION_REPORT_VERSION,
        "generated_by": "fin.eval.ablation",
        "spec_basis": {
            "source": "specs/corpus-retrieval.md §B1",
            "configuration_total": B1_CONFIGURATION_TOTAL,
            "min_repeats": B1_MIN_REPEATS,
            "query_subset_band": [B1_QUERY_SUBSET_MIN, B1_QUERY_SUBSET_MAX],
            "metric_fields": list(METRIC_FIELDS),
        },
        "dataset": {
            "dataset_id": resolved_dataset_id,
            "query_subset_size": run.sample_size,
            "query_subset_hash": subset_hash,
            "query_ids": list(run.query_ids),
            "known_gaps": metadata.get("known_gaps", []),
        },
        "backend": metadata.get("backend", {}),
        "caveats": metadata.get("caveats", []),
        "merge": {
            "merge_keys": list(MERGE_KEYS),
            "dataset_id": resolved_dataset_id,
            "query_subset_hash": subset_hash,
            "metrics_produced_by_fin": list(METRIC_FIELDS),
            "metrics_owned_by_fin": list(FIN_OWNED_METRICS),
            "dimensions_not_computed_by_fin": list(DIMENSIONS_NOT_COMPUTED_BY_FIN),
            "metric_definitions": dict(METRIC_DEFINITIONS),
        },
        "baseline": {
            "configuration_id": run.baseline_configuration_id,
            "all_on_configuration_id": run.all_on_configuration_id,
        },
        "configurations": configurations,
        "improvement_claims": claims,
        "capability_contributions": contributions,
        "summary": {
            "configuration_count": len(run.configurations),
            "missing_configuration_count": run.missing_configuration_count,
            "configurations_missing_any_metric": run.configurations_missing_any_metric,
            "repeats_below_floor_count": run.repeats_below_floor_count,
            "improvement_claim_count": len(claims),
            "percent_only_claim_count": _percent_only_claim_count(claims),
            "claims_with_undefined_percent": sum(
                1
                for claim in claims
                if claim["percent_status"] == PERCENT_UNDEFINED_ZERO_BASELINE
            ),
            "contribution_count": len(contributions),
            "missing_contribution_count": expected_contributions - len(contributions),
            "fin_metrics_naming_retrieval_side_dimensions": sum(
                1
                for dimension in DIMENSIONS_NOT_COMPUTED_BY_FIN
                if dimension in METRIC_FIELDS
            ),
        },
    }


def validate_report(report: Mapping[str, Any]) -> list[str]:
    """Return the report's acceptance problems (empty list = clean).

    Every check maps to one T22 acceptance line, so a caller can gate on
    the list being empty instead of eyeballing the JSON.
    """
    problems: list[str] = []
    configurations = report.get("configurations")
    if not isinstance(configurations, list):
        return ["report has no `configurations` list"]
    if len(configurations) != B1_CONFIGURATION_TOTAL:
        problems.append(
            f"configuration count {len(configurations)} != §B1 "
            f"{B1_CONFIGURATION_TOTAL}"
        )
    for config in configurations:
        cid = config.get("configuration_id", "<unnamed>")
        metrics = config.get("metrics")
        if not isinstance(metrics, dict):
            problems.append(f"{cid}: no metrics block")
            continue
        for metric in METRIC_FIELDS:
            series = metrics.get(metric)
            if not isinstance(series, dict):
                problems.append(f"{cid}: metric {metric} missing")
                continue
            raw = series.get("raw")
            if not isinstance(raw, list) or len(raw) < B1_MIN_REPEATS:
                problems.append(
                    f"{cid}: metric {metric} has {len(raw) if isinstance(raw, list) else 0} "
                    f"raw values, §B1 requires >= {B1_MIN_REPEATS}"
                )
            if not isinstance(series.get("median"), (int, float)):
                problems.append(f"{cid}: metric {metric} has no median")

    claims = report.get("improvement_claims")
    if not isinstance(claims, list) or not claims:
        problems.append("report carries no improvement claim")
        claims = []
    for claim in claims:
        cid = claim.get("claim_id", "<unnamed>")
        for key in ("baseline_absolute", "configuration_absolute"):
            if not isinstance(claim.get(key), (int, float)):
                problems.append(f"{cid}: percentage claim without {key}")
        if not isinstance(claim.get("sample_size"), int) or claim.get(
            "sample_size", 0
        ) <= 0:
            problems.append(f"{cid}: percentage claim without a sample size")
        if claim.get("percent_status") not in (
            PERCENT_COMPUTED,
            PERCENT_UNDEFINED_ZERO_BASELINE,
        ):
            problems.append(f"{cid}: unknown percent_status")

    contributions = report.get("capability_contributions")
    if not isinstance(contributions, list):
        problems.append("report carries no capability contributions")
        contributions = []
    single_off = [c for c in configurations if c.get("kind") == "single_off"]
    expected = len(single_off) * len(METRIC_FIELDS)
    if len(contributions) != expected:
        problems.append(
            f"capability contributions {len(contributions)} != expected {expected}"
        )

    summary = report.get("summary", {})
    for key in (
        "missing_configuration_count",
        "configurations_missing_any_metric",
        "repeats_below_floor_count",
        "percent_only_claim_count",
        "missing_contribution_count",
        "fin_metrics_naming_retrieval_side_dimensions",
    ):
        value = summary.get(key)
        if value != 0:
            problems.append(f"summary.{key} = {value!r}, acceptance value is 0")
    return problems


def merge_with_retrieval_report(
    fin_report: Mapping[str, Any],
    retrieval_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge fin's report with the retrieval side's over the merge keys.

    Neither side's metrics are recomputed: a metric present on both
    sides is a named conflict, because two numbers for one 口径 is
    exactly the ambiguity the merge is supposed to prevent.
    """
    fin_merge = fin_report.get("merge", {})
    for key in ("dataset_id", "query_subset_hash"):
        if fin_merge.get(key) != retrieval_report.get(key):
            raise AblationMergeKeyMismatchError(
                f"merge key {key} differs: fin={fin_merge.get(key)!r} "
                f"retrieval={retrieval_report.get(key)!r}"
            )
    other_by_id = {
        config.get("configuration_id"): config
        for config in retrieval_report.get("configurations", [])
    }
    merged_configurations: list[dict[str, Any]] = []
    for config in fin_report.get("configurations", []):
        configuration_id = config.get("configuration_id")
        other = other_by_id.get(configuration_id)
        if other is None:
            raise AblationMergeKeyMismatchError(
                f"retrieval-side report has no configuration {configuration_id!r}"
            )
        merged_metrics = dict(config.get("metrics", {}))
        for metric, value in (other.get("metrics") or {}).items():
            if metric in merged_metrics:
                raise AblationMergeMetricConflictError(
                    f"{configuration_id}: retrieval side recomputed {metric!r}, "
                    "which fin already produced on this run"
                )
            merged_metrics[metric] = value
        merged = dict(config)
        merged["metrics"] = merged_metrics
        merged_configurations.append(merged)

    merged_report = dict(fin_report)
    merged_report["configurations"] = merged_configurations
    merged_report["merged_with"] = {
        "produced_by": retrieval_report.get("produced_by", "retrieval-service"),
        "dataset_id": retrieval_report.get("dataset_id"),
        "query_subset_hash": retrieval_report.get("query_subset_hash"),
    }
    return merged_report


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _default_report_serializer(payload: Mapping[str, Any]) -> str:
    """Canonical JSON shape the operator's tooling reads back."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _lock_path_for(target: Path) -> Path:
    """Sibling lock file path used to serialize write access."""
    return target.with_name(target.name + ".lock")


@contextlib.contextmanager
def _report_lock(target: Path):
    """Hold an exclusive ``fcntl.flock`` on the sibling lock file.

    Shared with ``fin.corpus.catalog._table_lock`` so the same
    ``flock`` discipline is used wherever two processes may write to
    the same JSON artifact.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for(target)
    with open(lock_path, "a+", encoding="utf-8") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


def write_report_atomically(
    target: Path,
    payload: Mapping[str, Any] | Callable[[], str],
) -> Path:
    """Write ``payload`` to ``target`` under a flock, atomically.

    The contract is: a reader of ``target`` always sees either the
    previous whole document or a freshly-written whole document —
    never a half-written one. Two writers serialize through the
    sibling lock file (T3 / ADR 0042). The temp file is renamed over
    ``target`` so a crash mid-write leaves the previous document
    intact.

    ``payload`` may be a mapping (serialized via the canonical
    formatter) or a zero-argument callable that returns the encoded
    string. The callable form lets callers defer expensive work until
    the lock is held, and lets tests inject serializers that raise
    mid-write.
    """
    encoded: str | None = None
    if isinstance(payload, Mapping):
        encoded = _default_report_serializer(payload)
    elif callable(payload):
        # ``callable`` covers lambdas / functions; the Mapping branch
        # above already handled dicts.
        encoded = payload()
    else:
        raise TypeError(
            "write_report_atomically payload must be a mapping or a "
            f"callable, got {type(payload).__name__}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(target.name + ".tmp")
    with _report_lock(target):
        with open(tmp_path, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(encoded)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target)
    return target


__all__ = [
    "ABLATION_REPORT_VERSION",
    "DIMENSIONS_NOT_COMPUTED_BY_FIN",
    "MERGE_KEYS",
    "PERCENT_COMPUTED",
    "PERCENT_UNDEFINED_ZERO_BASELINE",
    "REPORT_FILENAME",
    "AblationMergeKeyMismatchError",
    "AblationMergeMetricConflictError",
    "AblationReportError",
    "build_report",
    "merge_with_retrieval_report",
    "query_subset_hash",
    "validate_report",
    "write_report_atomically",
]
