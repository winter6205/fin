"""The §B1 metric set, computed on one configuration's run (T22 / SC32).

§B1「指标集」row is the only authority for which fields a configuration
must produce:

    P@5、R@5、Recall@20、nDCG@10、MRR、引用门通过率（由 fin 侧产出）、
    图臂命中率、错误率、延迟 p50 / p95 / p99

:data:`METRIC_FIELDS` is fin's field-name spelling of that row (the
latency label is one §B1 entry and three fields);
:func:`assert_metrics_match_published` re-reads the row out of the spec
and refuses to agree when the two sides differ.

引用门通过率 is produced **by fin, on the same run results** — the gate
(``fin.host.grounded.citation_gate.verify_citations``, unchanged by this ticket)
runs over the chunks this configuration actually retrieved. It is not a
second retrieval and not a number the retrieval service hands over.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from fin.corpus.extracted import Locator
from fin.host.grounded.citation_gate import DoneResult, EvidenceItem, verify_citations
from fin.retrieve.capabilities import published_spec_path


SPEC_METRIC_ROW_LABEL = "指标集"

#: Field names, one per §B1 label except the latency label, which is
#: three percentiles and therefore three fields.
METRIC_FIELDS: tuple[str, ...] = (
    "p_at_5",
    "r_at_5",
    "recall_at_20",
    "ndcg_at_10",
    "mrr",
    "citation_gate_pass_rate",
    "graph_arm_hit_rate",
    "error_rate",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
)

METRIC_FIELDS_BY_LABEL: dict[str, tuple[str, ...]] = {
    "P@5": ("p_at_5",),
    "R@5": ("r_at_5",),
    "Recall@20": ("recall_at_20",),
    "nDCG@10": ("ndcg_at_10",),
    "MRR": ("mrr",),
    "引用门通过率": ("citation_gate_pass_rate",),
    "图臂命中率": ("graph_arm_hit_rate",),
    "错误率": ("error_rate",),
    "延迟 p50 / p95 / p99": (
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
    ),
}

#: Metrics fin owns end to end. The gate is fin's, so its pass rate is
#: fin's to produce (§B1 判定 column).
FIN_OWNED_METRICS: tuple[str, ...] = ("citation_gate_pass_rate",)

#: Lower is better for these; an "improvement" is a decrease.
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {"error_rate", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms"}
)

#: The §B4 capability behind 图臂命中率.
GRAPH_ARM_CAPABILITY = "graph_ppr"

#: The gate's minimum extractive span (ADR 0021 default).
CITATION_MIN_SPAN_LEN = 32

_P_AT_K = 5
_R_AT_K = 5
_RECALL_AT_K = 20
_NDCG_AT_K = 10


class MetricEnumerationError(Exception):
    """Base for §B1 metric-row failures. ``code`` is stable."""

    code = "metric_enumeration_error"


class MetricRowMissingError(MetricEnumerationError):
    """The spec no longer publishes the §B1「指标集」row."""

    code = "metric_enumeration_row_missing"


class MetricEnumerationDriftError(MetricEnumerationError):
    """Local field list and the published §B1 row disagree; both are printed."""

    code = "metric_enumeration_drift"


_PARENTHETICAL = re.compile(r"（[^）]*）")


def published_metric_labels(spec_path: Path | None = None) -> tuple[str, ...]:
    """Read the §B1「指标集」labels back out of the spec, in order.

    The row's parenthetical attributions (e.g. ``（由 fin 侧产出）``) are
    prose about who produces a metric, not part of its name.
    """
    path = Path(spec_path) if spec_path is not None else published_spec_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MetricRowMissingError(
            f"cannot read the published §B1 metric row from {path}: {exc}"
        ) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == SPEC_METRIC_ROW_LABEL:
            labels = [
                _PARENTHETICAL.sub("", token).strip()
                for token in cells[1].split("、")
            ]
            return tuple(label for label in labels if label)
    raise MetricRowMissingError(
        f"{path} publishes no 「{SPEC_METRIC_ROW_LABEL}」 §B1 table row; the "
        "metric set has nothing to take its fields from"
    )


#: §B1「指标集」labels, loaded in published order rather than copied into a
# second local list. ``assert_metrics_match_published`` below still detects a
# spec edit made after this module was imported.
B1_METRIC_LABELS: tuple[str, ...] = published_metric_labels()


def assert_metrics_match_published(
    spec_path: Path | None = None,
) -> tuple[str, ...]:
    """The sync point: local labels must equal the published §B1 row."""
    published = published_metric_labels(spec_path=spec_path)
    if published != B1_METRIC_LABELS:
        raise MetricEnumerationDriftError(
            "metric enumeration drift: "
            f"local={list(B1_METRIC_LABELS)} published={list(published)}"
        )
    expected_fields = tuple(
        field for label in published for field in METRIC_FIELDS_BY_LABEL[label]
    )
    if expected_fields != METRIC_FIELDS:
        raise MetricEnumerationDriftError(
            "metric enumeration drift: fields "
            f"{list(METRIC_FIELDS)} do not expand the published labels "
            f"{list(published)}"
        )
    return published


@dataclass(frozen=True)
class RetrievedHit:
    """One retrieved passage plus the capability arms that produced it."""

    locator: Locator
    text: str
    arms: frozenset[str]


@dataclass(frozen=True)
class EvalQuery:
    """One eval question: what was asked and what should have been hit.

    ``citable_answer`` is the answer text the citation gate scores for
    this query: a >= ``CITATION_MIN_SPAN_LEN`` verbatim slice of the
    expected unit, so the gate passes exactly when this configuration
    actually retrieved the passage that grounds it.
    """

    query_id: str
    question: str
    expected_document_id: str
    expected_version: str
    expected_unit: int
    citable_answer: str


@dataclass(frozen=True)
class QueryOutcome:
    """What one configuration's one repeat of one query produced."""

    query_id: str
    hits: tuple[RetrievedHit, ...]
    latency_ms: float
    errored: bool = False
    error_code: str = ""  # non-empty iff ``errored``


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile over ``values`` (empty → 0.0)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def _is_relevant(hit: RetrievedHit, query: EvalQuery) -> bool:
    return (
        hit.locator.document_id == query.expected_document_id
        and hit.locator.unit == query.expected_unit
    )


def _first_relevant_rank(query: EvalQuery, hits: Sequence[RetrievedHit]) -> int:
    """1-based rank of the first relevant hit, or 0 when there is none."""
    for index, hit in enumerate(hits, start=1):
        if _is_relevant(hit, query):
            return index
    return 0


def citation_gate_passes(
    query: EvalQuery,
    hits: Sequence[RetrievedHit],
    *,
    known_document_ids: set[str],
) -> bool:
    """Run fin's citation gate over this configuration's retrieved chunks.

    The gate is unchanged (ADR 0008 / 0021): it either finds a
    >= 32-character span of ``citable_answer`` inside a retrieved
    chunk's text (``DoneResult``) or it does not. Chunks with empty text
    are not handed to the gate — an empty ``unit_text`` is a malformed
    evidence item, and inventing one would be scoring the gate on input
    this run did not retrieve.
    """
    evidence = [
        EvidenceItem(locator=hit.locator, unit_text=hit.text)
        for hit in hits
        if hit.text
    ]
    if not evidence:
        return False
    result = verify_citations(
        query.citable_answer,
        evidence,
        0,
        known_document_ids=known_document_ids,
        min_span_len=CITATION_MIN_SPAN_LEN,
    )
    return isinstance(result, DoneResult)


def evaluate_outcomes(
    queries: Sequence[EvalQuery],
    outcomes: Mapping[str, QueryOutcome],
    *,
    known_document_ids: set[str],
) -> dict[str, float]:
    """Compute the full §B1 metric set for one configuration repeat.

    Every field in :data:`METRIC_FIELDS` is produced; a query that
    errored contributes 0 to the quality metrics and 1 to the error
    count, so an unanswered query never quietly leaves the denominator.
    """
    total = len(queries)
    if total == 0:
        raise ValueError("cannot evaluate an empty query set")

    p_at_5 = 0.0
    r_at_5 = 0.0
    recall_at_20 = 0.0
    ndcg_at_10 = 0.0
    mrr = 0.0
    gate_passes = 0
    graph_arm_hits = 0
    errors = 0
    latencies: list[float] = []

    for query in queries:
        outcome = outcomes[query.query_id]
        latencies.append(outcome.latency_ms)
        if outcome.errored:
            errors += 1
            continue
        hits = outcome.hits
        relevant_in_5 = sum(1 for hit in hits[:_P_AT_K] if _is_relevant(hit, query))
        p_at_5 += relevant_in_5 / _P_AT_K
        r_at_5 += 1.0 if relevant_in_5 else 0.0
        recall_at_20 += (
            1.0
            if any(_is_relevant(hit, query) for hit in hits[:_RECALL_AT_K])
            else 0.0
        )
        rank = _first_relevant_rank(query, hits)
        if rank and rank <= _NDCG_AT_K:
            ndcg_at_10 += 1.0 / math.log2(rank + 1)
        if rank:
            mrr += 1.0 / rank
        if any(GRAPH_ARM_CAPABILITY in hit.arms for hit in hits):
            graph_arm_hits += 1
        if citation_gate_passes(
            query, hits, known_document_ids=known_document_ids
        ):
            gate_passes += 1

    return {
        "p_at_5": p_at_5 / total,
        "r_at_5": r_at_5 / total,
        "recall_at_20": recall_at_20 / total,
        "ndcg_at_10": ndcg_at_10 / total,
        "mrr": mrr / total,
        "citation_gate_pass_rate": gate_passes / total,
        "graph_arm_hit_rate": graph_arm_hits / total,
        "error_rate": errors / total,
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_p99_ms": percentile(latencies, 0.99),
    }


#: One-line definition per metric, carried in the report so the two
#: sides can be merged without either recomputing the other's numbers.
METRIC_DEFINITIONS: dict[str, str] = {
    "p_at_5": "mean over queries of (relevant hits in top 5) / 5",
    "r_at_5": "share of queries whose expected unit appears in the top 5",
    "recall_at_20": "share of queries whose expected unit appears in the top 20",
    "ndcg_at_10": "mean of 1/log2(rank+1) for the first relevant hit at rank <= 10",
    "mrr": "mean reciprocal rank of the first relevant hit",
    "citation_gate_pass_rate": (
        "share of queries whose citable answer passes "
        "fin.host.grounded.citation_gate.verify_citations over THIS run's retrieved "
        "chunks (produced by fin, ADR 0008 / 0021)"
    ),
    "graph_arm_hit_rate": (
        f"share of queries with >= 1 hit attributed to the {GRAPH_ARM_CAPABILITY} arm"
    ),
    "error_rate": "share of queries whose retrieval call failed (named error)",
    "latency_p50_ms": "nearest-rank p50 of per-query retrieval latency, milliseconds",
    "latency_p95_ms": "nearest-rank p95 of per-query retrieval latency, milliseconds",
    "latency_p99_ms": "nearest-rank p99 of per-query retrieval latency, milliseconds",
}


__all__ = [
    "B1_METRIC_LABELS",
    "CITATION_MIN_SPAN_LEN",
    "FIN_OWNED_METRICS",
    "GRAPH_ARM_CAPABILITY",
    "LOWER_IS_BETTER",
    "METRIC_DEFINITIONS",
    "METRIC_FIELDS",
    "METRIC_FIELDS_BY_LABEL",
    "SPEC_METRIC_ROW_LABEL",
    "EvalQuery",
    "MetricEnumerationDriftError",
    "MetricEnumerationError",
    "MetricRowMissingError",
    "QueryOutcome",
    "RetrievedHit",
    "assert_metrics_match_published",
    "citation_gate_passes",
    "evaluate_outcomes",
    "percentile",
    "published_metric_labels",
]
