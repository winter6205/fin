"""The four fin-owned supplementary dimensions from T23 / SC38.

These are deliberately small, deterministic report builders. They run over
the same real citation gate and T14 contract fake used by the surrounding
tests; no retrieval-service-owned dimension is recreated here.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

from fin.corpus.extracted import Locator
from fin.corpus.push import PushRecord
from fin.host.grounded.citation_gate import (
    DoneResult,
    EvidenceItem,
    RefuseResult,
    verify_citations,
)
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_port import IDEMPOTENCY_BASELINE_PUSHES, push_records


FIN_EVAL_DIMENSIONS: tuple[str, ...] = (
    "citation_gate_pass_rate",
    "refusal_rate",
    "first_hit_position",
    "duplicate_submission_idempotency",
)
FORBIDDEN_FIN_DIMENSIONS: tuple[str, ...] = (
    "长文档召回",
    "跨文档多跳召回",
)


class SupplementError(Exception):
    """Base class for named supplementary-eval failures."""

    code = "supplement_error"


class SupplementInputError(SupplementError):
    """A supplementary dimension received mismatched or empty input."""

    code = "supplement_input_invalid"


def _as_bool_tuple(values: Iterable[bool], *, field: str) -> tuple[bool, ...]:
    resolved = tuple(values)
    if not resolved:
        raise SupplementInputError(f"{field} must contain at least one value")
    if not all(isinstance(value, bool) for value in resolved):
        raise SupplementInputError(f"{field} must contain booleans")
    return resolved


def first_hit_position_distribution(
    positions: Iterable[int],
) -> dict[str, int]:
    """Return a stable histogram; ``0`` and negatives mean no hit."""
    resolved = tuple(positions)
    if not resolved:
        raise SupplementInputError("first-hit positions must not be empty")
    if not all(isinstance(position, int) and not isinstance(position, bool) for position in resolved):
        raise SupplementInputError("first-hit positions must contain integers")
    counts: Counter[str] = Counter(
        str(position) if position > 0 else "not_found"
        for position in resolved
    )
    return dict(sorted(counts.items(), key=lambda item: (item[0] == "not_found", item[0])))


def _duplicate_submission_summary(
    written_counts: Sequence[int],
) -> dict[str, Any]:
    if not written_counts:
        raise SupplementInputError("duplicate submission counts must not be empty")
    if len(written_counts) < IDEMPOTENCY_BASELINE_PUSHES:
        raise SupplementInputError(
            "duplicate submission count is below the §B1 baseline"
        )
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in written_counts
    ):
        raise SupplementInputError(
            "duplicate submission counts must be non-negative integers"
        )
    first_write = written_counts[0]
    replay_counts = tuple(written_counts[1:])
    return {
        "submission_count": len(written_counts),
        "first_write_count": first_write,
        "replay_write_counts": list(replay_counts),
        "replay_writes_total": sum(replay_counts),
        "idempotent": all(value == 0 for value in replay_counts),
    }


def build_supplement_report(
    *,
    citation_gate_passes: Iterable[bool],
    refusal_expected: Iterable[bool],
    refusal_observed: Iterable[bool],
    first_hit_positions: Iterable[int],
    duplicate_submission: Sequence[int],
) -> dict[str, Any]:
    """Assemble all four supplementary dimensions with countable evidence."""
    citation_values = _as_bool_tuple(
        citation_gate_passes, field="citation_gate_passes"
    )
    expected = _as_bool_tuple(refusal_expected, field="refusal_expected")
    observed = _as_bool_tuple(refusal_observed, field="refusal_observed")
    if len(expected) != len(observed):
        raise SupplementInputError(
            "refusal_expected and refusal_observed must have equal length"
        )
    first_hit_values = tuple(first_hit_positions)
    refusal_expected_and_observed = sum(
        expected_value and observed_value
        for expected_value, observed_value in zip(expected, observed)
    )
    refusal_expected_but_missing = sum(
        expected_value and not observed_value
        for expected_value, observed_value in zip(expected, observed)
    )
    duplicate = _duplicate_submission_summary(duplicate_submission)
    dimension_keys = {
        "citation_gate_pass_rate": "citation_gate",
        "refusal_rate": "refusal_rate",
        "first_hit_position": "first_hit_position",
        "duplicate_submission_idempotency": "duplicate_submission",
    }
    present_keys = {
        "citation_gate",
        "refusal_rate",
        "first_hit_position",
        "duplicate_submission",
    }
    missing = [
        dimension
        for dimension, key in dimension_keys.items()
        if key not in present_keys
    ]
    return {
        "report_version": "1.0.0",
        "dimensions": list(FIN_EVAL_DIMENSIONS),
        "fin_dimensions": list(FIN_EVAL_DIMENSIONS),
        "dimension": "supplementary",
        "missing_dimension_count": len(missing),
        "missing_dimensions": missing,
        "citation_gate": {
            "sample_count": len(citation_values),
            "pass_count": sum(citation_values),
            "pass_rate": sum(citation_values) / len(citation_values),
        },
        "refusal_rate": {
            "sample_count": len(expected),
            "expected_and_refused": refusal_expected_and_observed,
            "expected_but_not_refused": refusal_expected_but_missing,
            "expected_refusal_count": sum(expected),
            "observed_refusal_count": sum(observed),
            "expected_refusal_rate": (
                refusal_expected_and_observed / sum(expected)
                if sum(expected)
                else 0.0
            ),
        },
        "first_hit_position": {
            "sample_count": len(first_hit_values),
            "distribution": first_hit_position_distribution(first_hit_values),
        },
        "duplicate_submission": duplicate,
        "retrieval_service_owned_dimensions": list(FORBIDDEN_FIN_DIMENSIONS),
        "forbidden_fin_dimension_count": sum(
            dimension in FORBIDDEN_FIN_DIMENSIONS
            for dimension in FIN_EVAL_DIMENSIONS
        ),
    }


def _sample_record() -> PushRecord:
    return PushRecord(
        block_id="t23-supplement-block",
        source_ref='{"document_id":"T23-2023Q1","unit":1,"version":"a"}',
        content_id="t23-supplement-content",
        text="A frozen passage for the duplicate-submission dimension.",
        parent_block_id="t23-supplement-parent",
        parent_text="A frozen passage for the duplicate-submission dimension.",
        metadata={
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "T23",
            "source_sha256": "a" * 64,
            "parse_rule_version": "parse-1",
            "params_table_version": "params-1",
        },
    )


def _citation_evidence() -> tuple[str, list[EvidenceItem]]:
    span = "A grounded citation span with more than thirty-two characters."
    return (
        span,
        [
            EvidenceItem(
                locator=Locator(
                    document_id="AAPL-2022Q3",
                    version="a" * 64,
                    unit=1,
                ),
                unit_text=span,
            )
        ],
    )


def citation_gate_dimension_report() -> dict[str, Any]:
    """Run the citation gate on a pass and a non-pass answer."""
    span, evidence = _citation_evidence()
    passed = isinstance(
        verify_citations(
            f"Reported: {span}",
            evidence,
            0,
            known_document_ids={"AAPL-2022Q3"},
        ),
        DoneResult,
    )
    failed = isinstance(
        verify_citations(
            "Unsupported answer without an evidence span.",
            evidence,
            0,
            known_document_ids={"AAPL-2022Q3"},
        ),
        DoneResult,
    )
    report = build_supplement_report(
        citation_gate_passes=(passed, failed),
        refusal_expected=(False, False),
        refusal_observed=(False, False),
        first_hit_positions=(1, 0),
        duplicate_submission=(1, 0, 0),
    )
    report["dimension"] = "citation-gate"
    report["citation_gate"]["source"] = "fin.host.grounded.citation_gate.verify_citations"
    return report


def refusal_rate_dimension_report() -> dict[str, Any]:
    """Measure expected refusals against the real gate's typed outcomes."""
    span, evidence = _citation_evidence()
    outcomes = (
        verify_citations(
            "See BOGUS-2023Q1 for this unsupported claim.",
            evidence,
            0,
            known_document_ids={"AAPL-2022Q3"},
        ),
        verify_citations(
            "Unsupported answer without a citation.",
            [],
            3,
            known_document_ids={"AAPL-2022Q3"},
        ),
        verify_citations(
            f"Reported: {span}",
            evidence,
            0,
            known_document_ids={"AAPL-2022Q3"},
        ),
    )
    observed = tuple(isinstance(outcome, RefuseResult) for outcome in outcomes)
    report = build_supplement_report(
        citation_gate_passes=tuple(isinstance(outcome, DoneResult) for outcome in outcomes),
        refusal_expected=(True, True, False),
        refusal_observed=observed,
        first_hit_positions=(0, 0, 1),
        duplicate_submission=(1, 0, 0),
    )
    report["dimension"] = "refusal-rate"
    report["refusal_rate"]["source"] = "fin.host.grounded.citation_gate.verify_citations"
    return report


def first_hit_dimension_report() -> dict[str, Any]:
    """Persist the first-hit rank histogram from a scripted retrieval set."""
    report = build_supplement_report(
        citation_gate_passes=(True, True, True),
        refusal_expected=(False, False, False),
        refusal_observed=(False, False, False),
        first_hit_positions=(3, 1, 0, 2),
        duplicate_submission=(1, 0, 0),
    )
    report["dimension"] = "first-hit"
    return report


def duplicate_submission_dimension_report() -> dict[str, Any]:
    """Run the §B1 three-submission idempotency baseline and persist it."""
    port = FakePushPort()
    record = _sample_record()
    block_total_before = port.block_total()
    outcomes = [
        push_records(port, [record])
        for _ in range(IDEMPOTENCY_BASELINE_PUSHES)
    ]
    first_block_ids = outcomes[0].block_ids
    report = build_supplement_report(
        citation_gate_passes=(True,),
        refusal_expected=(False,),
        refusal_observed=(False,),
        first_hit_positions=(1,),
        duplicate_submission=tuple(outcome.written for outcome in outcomes),
    )
    report["dimension"] = "duplicate-submission"
    report["duplicate_submission"].update(
        {
            "block_total_before": block_total_before,
            "block_total_after": port.block_total(),
            "block_ids": sorted(port.block_ids()),
            "same_block_ids_on_replay": all(
                outcome.block_ids == first_block_ids for outcome in outcomes[1:]
            ),
            "block_total_unchanged_after_replays": (
                port.block_total() == block_total_before + outcomes[0].written
            ),
        }
    )
    return report


def validate_supplement_report(report: dict[str, Any]) -> list[str]:
    """Return SC38 supplementary-report problems."""
    problems: list[str] = []
    if report.get("missing_dimension_count") != 0:
        problems.append("supplementary report has missing dimensions")
    if report.get("forbidden_fin_dimension_count") != 0:
        problems.append("fin dimension list contains retrieval-owned dimensions")
    dimensions = report.get("fin_dimensions")
    if not isinstance(dimensions, list):
        problems.append("supplementary report has no fin dimension list")
    else:
        problems.extend(validate_fin_dimensions(dimensions))
    refusal = report.get("refusal_rate")
    if not isinstance(refusal, dict):
        problems.append("supplementary report has no refusal-rate result")
    elif refusal.get("expected_but_not_refused") != 0:
        problems.append("an expected refusal was not observed")
    duplicate = report.get("duplicate_submission")
    if not isinstance(duplicate, dict):
        problems.append("supplementary report has no duplicate-submission result")
    elif not duplicate.get("idempotent"):
        problems.append("duplicate submission produced a second write")
    return problems


def validate_fin_dimensions(dimensions: Iterable[str]) -> list[str]:
    """Return violations of SC38's fin-side dimension ownership rule."""
    resolved = tuple(dimensions)
    problems: list[str] = []
    for forbidden in FORBIDDEN_FIN_DIMENSIONS:
        count = resolved.count(forbidden)
        if count:
            problems.append(
                f"fin dimension {forbidden!r} appears {count} time(s)"
            )
    expected = set(FIN_EVAL_DIMENSIONS)
    missing = sorted(expected - set(resolved))
    if missing:
        problems.append(f"fin dimension catalog misses: {missing}")
    return problems


__all__ = [
    "FIN_EVAL_DIMENSIONS",
    "FORBIDDEN_FIN_DIMENSIONS",
    "SupplementError",
    "SupplementInputError",
    "build_supplement_report",
    "citation_gate_dimension_report",
    "duplicate_submission_dimension_report",
    "first_hit_dimension_report",
    "first_hit_position_distribution",
    "refusal_rate_dimension_report",
    "validate_supplement_report",
    "validate_fin_dimensions",
]
