"""Run the ablation matrix: every configuration, every repeat (T22 / SC31, SC33).

One call to :func:`run_ablation` covers all 10 §B1 configurations and
repeats each at least :data:`B1_MIN_REPEATS` times, keeping **每次原始值**
alongside the median. Missing configurations are not possible by
construction (the matrix is built from the snapshot, not enumerated by
hand) and the count is reported anyway, because "缺失配置数 = 0" has to
be a number somebody can read.

Capability configurations are passed to the port's **eval seam** only
(ADR 0039): the product seam has no capability parameter, and this
module never touches it.

Arm attribution is required, not optional. A port that cannot say which
arm produced a hit cannot produce 图臂命中率, and reporting 0.0 for a
metric nobody measured would read exactly like a measured zero — so it
is a named failure instead.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from fin.eval.ablation.matrix import (
    ALL_OFF_CONFIGURATION_ID,
    ALL_ON_CONFIGURATION_ID,
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    AblationConfiguration,
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
from fin.retrieve.input_validation import InputValidationError
from fin.retrieve.mcp_adapter import RetrieveUnavailableError


#: Failures a single query may raise without aborting the whole run:
#: they are counted into 错误率 and named in the run's error ledger.
RETRIEVAL_FAILURES = (InputValidationError, RetrieveUnavailableError)


class AblationRunError(Exception):
    """Base for ablation-run failures. ``code`` is stable and assertable."""

    code = "ablation_run_error"


class AblationRepeatsBelowFloorError(AblationRunError):
    """Fewer repeats than the §B1「消融矩阵」floor were requested."""

    code = "ablation_repeats_below_floor"


class GraphArmAttributionUnavailableError(AblationRunError):
    """The port cannot attribute hits to capability arms."""

    code = "graph_arm_attribution_unavailable"


class AblationPortContractError(AblationRunError):
    """The port returned a shape the driver cannot read."""

    code = "ablation_port_contract_violation"


@dataclass(frozen=True)
class MetricSeries:
    """One metric across a configuration's repeats: raw values + median."""

    field_name: str
    raw: tuple[float, ...]
    median: float

    @property
    def repeats(self) -> int:
        return len(self.raw)


@dataclass(frozen=True)
class ConfigurationResult:
    """One configuration's full §B1 metric set, per repeat and median."""

    configuration_id: str
    kind: str
    disabled_capability: str | None
    capabilities: dict[str, bool]
    metrics: dict[str, MetricSeries]
    error_codes: tuple[str, ...]

    @property
    def repeats(self) -> int:
        return self.metrics[METRIC_FIELDS[0]].repeats

    def median(self, field_name: str) -> float:
        return self.metrics[field_name].median


@dataclass(frozen=True)
class AblationRun:
    """Everything one command invocation produced."""

    configurations: tuple[ConfigurationResult, ...]
    query_ids: tuple[str, ...]
    repeats: int
    sample_size: int
    baseline_configuration_id: str = ALL_OFF_CONFIGURATION_ID
    all_on_configuration_id: str = ALL_ON_CONFIGURATION_ID
    metadata: dict[str, Any] = field(default_factory=dict)

    def configuration(self, configuration_id: str) -> ConfigurationResult:
        for config in self.configurations:
            if config.configuration_id == configuration_id:
                return config
        raise AblationRunError(
            f"no configuration {configuration_id!r} in this run: "
            f"{[c.configuration_id for c in self.configurations]}"
        )

    @property
    def missing_configuration_count(self) -> int:
        return B1_CONFIGURATION_TOTAL - len(
            {c.configuration_id for c in self.configurations}
        )

    @property
    def configurations_missing_any_metric(self) -> int:
        return sum(
            1
            for config in self.configurations
            if any(field_name not in config.metrics for field_name in METRIC_FIELDS)
        )

    @property
    def repeats_below_floor_count(self) -> int:
        return sum(
            1 for config in self.configurations if config.repeats < B1_MIN_REPEATS
        )


def _read_hits(payload: Mapping[str, Any]) -> tuple[RetrievedHit, ...]:
    chunks = payload.get("chunks")
    arms = payload.get("arms")
    if not isinstance(chunks, (list, tuple)):
        raise AblationPortContractError(
            f"eval seam returned no `chunks` list: {sorted(payload)}"
        )
    if not isinstance(arms, (list, tuple)) or len(arms) != len(chunks):
        raise GraphArmAttributionUnavailableError(
            "eval seam returned no per-hit arm attribution matching its "
            f"{len(chunks)} chunks; 图臂命中率 cannot be produced"
        )
    return tuple(
        RetrievedHit(
            locator=chunk.source_ref,
            text=chunk.text,
            arms=frozenset(arm),
        )
        for chunk, arm in zip(chunks, arms)
    )


def _run_one_query(
    port: Any,
    query: EvalQuery,
    capabilities: Mapping[str, bool],
    limit: int,
) -> QueryOutcome:
    started = time.perf_counter()
    try:
        payload = port.retrieve_for_eval_with_arms(
            query.question, capabilities=dict(capabilities), limit=limit
        )
    except RETRIEVAL_FAILURES as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        code = getattr(exc, "code", "")
        if not code:
            raise AblationPortContractError(
                f"retrieval failure without a stable code: {type(exc).__name__}"
            ) from exc
        return QueryOutcome(
            query_id=query.query_id,
            hits=(),
            latency_ms=elapsed_ms,
            errored=True,
            error_code=code,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return QueryOutcome(
        query_id=query.query_id,
        hits=_read_hits(payload),
        latency_ms=elapsed_ms,
    )


def _run_one_configuration(
    port: Any,
    configuration: AblationConfiguration,
    queries: Sequence[EvalQuery],
    *,
    repeats: int,
    limit: int,
    known_document_ids: set[str],
) -> ConfigurationResult:
    per_repeat: list[dict[str, float]] = []
    error_codes: list[str] = []
    for _ in range(repeats):
        outcomes: dict[str, QueryOutcome] = {}
        for query in queries:
            outcome = _run_one_query(
                port, query, configuration.capabilities, limit
            )
            if outcome.errored:
                error_codes.append(outcome.error_code)
            outcomes[query.query_id] = outcome
        per_repeat.append(
            evaluate_outcomes(
                queries, outcomes, known_document_ids=known_document_ids
            )
        )
    metrics = {
        field_name: MetricSeries(
            field_name=field_name,
            raw=tuple(float(values[field_name]) for values in per_repeat),
            median=float(
                statistics.median([values[field_name] for values in per_repeat])
            ),
        )
        for field_name in METRIC_FIELDS
    }
    return ConfigurationResult(
        configuration_id=configuration.configuration_id,
        kind=configuration.kind,
        disabled_capability=configuration.disabled_capability,
        capabilities=dict(configuration.capabilities),
        metrics=metrics,
        error_codes=tuple(error_codes),
    )


def run_ablation(
    port: Any,
    queries: Sequence[EvalQuery],
    *,
    repeats: int = B1_MIN_REPEATS,
    limit: int = 20,
    known_document_ids: set[str],
    metadata: Mapping[str, Any] | None = None,
) -> AblationRun:
    """Run every §B1 configuration ``repeats`` times over ``queries``.

    Parameters
    ----------
    port
        An eval-seam port exposing
        ``retrieve_for_eval_with_arms(query, *, capabilities, limit)``.
        The product seam is never called from here.
    repeats
        Repeats per configuration. Below the §B1 floor is a named
        failure — a run whose numbers cannot be believed should not be
        written at all.
    known_document_ids
        Passed straight to the citation gate; a locator claim outside
        this set refuses (ADR 0008), exactly as in a product run.
    """
    # Validate both B1 enumerations at the command's execution boundary.
    # Keeping these checks here prevents a run from silently using stale
    # local copies when either the spec's matrix or metric row changes.
    assert_matrix_matches_published()
    assert_metrics_match_published()
    if repeats < B1_MIN_REPEATS:
        raise AblationRepeatsBelowFloorError(
            f"§B1「消融矩阵」requires >= {B1_MIN_REPEATS} repeats per "
            f"configuration, got {repeats}"
        )
    if not queries:
        raise AblationRunError("cannot run an ablation over an empty query set")
    if not hasattr(port, "retrieve_for_eval_with_arms"):
        raise GraphArmAttributionUnavailableError(
            f"{type(port).__name__} exposes no `retrieve_for_eval_with_arms` "
            "eval seam; 图臂命中率 cannot be produced from its results"
        )

    results = tuple(
        _run_one_configuration(
            port,
            configuration,
            queries,
            repeats=repeats,
            limit=limit,
            known_document_ids=known_document_ids,
        )
        for configuration in build_ablation_matrix()
    )
    return AblationRun(
        configurations=results,
        query_ids=tuple(query.query_id for query in queries),
        repeats=repeats,
        sample_size=len(queries),
        metadata=dict(metadata or {}),
    )


__all__ = [
    "RETRIEVAL_FAILURES",
    "AblationPortContractError",
    "AblationRepeatsBelowFloorError",
    "AblationRun",
    "AblationRunError",
    "ConfigurationResult",
    "GraphArmAttributionUnavailableError",
    "MetricSeries",
    "run_ablation",
]
