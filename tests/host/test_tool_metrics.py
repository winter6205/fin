"""P3 T6 — tool measures derived from canonical ledger facts.

Hermetic cases over crafted fact streams: every numerator and denominator is
hand-computable, a zero eligible denominator is ``N/A`` (never 0 or 1),
unknown execution outcomes stay separate from success, retries stay visible
as attempts without inflating logical-call denominators, and the rubric
measures keep REQUIRED / OPTIONAL / PROHIBITED opportunities apart. No
span, receipt, UI row or event count is an input to the aggregation.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from fin.host.tools.ledger import ToolOutcomeFact
from fin.host.tools.metrics import (
    OpportunityLabel,
    ScoredCase,
    ToolOpportunity,
    compute_tool_measures,
)

CREATED_AT = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _fact(
    *,
    seq: int,
    logical_call_id: str,
    kind: str,
    run_id: str = "run-1",
    tool_name: str = "retrieve",
    model_attempt: int = 1,
    execution_attempt: int | None = None,
    business_operation_id: str | None = None,
    outcome_code: str | None = None,
) -> ToolOutcomeFact:
    return ToolOutcomeFact(
        fact_seq=seq,
        fact_id=f"{run_id}:{logical_call_id}:{kind}:{execution_attempt}:{seq}",
        run_id=run_id,
        thread_id="thread-1",
        logical_call_id=logical_call_id,
        tool_name=tool_name,
        model_attempt=model_attempt,
        execution_attempt=execution_attempt,
        provider_tool_call_id=None,
        runtime_attempt_id="attempt-1",
        runtime_attempt_generation=1,
        thread_generation=1,
        business_operation_id=business_operation_id,
        kind=kind,
        outcome_code=outcome_code,
        field_paths=(),
        created_at=CREATED_AT,
    )


def _opportunity(
    label: OpportunityLabel, tool_name: str | None
) -> ToolOpportunity:
    return ToolOpportunity(label=label, tool_name=tool_name)


def _case(
    case_id: str,
    *,
    run_ids: tuple[str, ...] = ("run-1",),
    opportunities: tuple[ToolOpportunity, ...] = (),
    rubric_id: str = "s4-tool-use",
    rubric_version: str = "1.0.0",
) -> ScoredCase:
    return ScoredCase(
        case_id=case_id,
        rubric_id=rubric_id,
        rubric_version=rubric_version,
        run_ids=run_ids,
        opportunities=opportunities,
    )


# ----- first-attempt schema validity ----------------------------------------


def test_first_attempt_schema_valid_rate_uses_the_first_model_attempt() -> None:
    facts = [
        # call-a: first attempt rejected, corrected on attempt 2.
        _fact(seq=1, logical_call_id="call-a", kind="schema_rejected"),
        _fact(seq=2, logical_call_id="call-a", kind="correction_offered"),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=2,
        ),
        _fact(
            seq=4,
            logical_call_id="call-a",
            kind="execution_started",
            model_attempt=2,
            execution_attempt=2,
        ),
        _fact(
            seq=5,
            logical_call_id="call-a",
            kind="succeeded",
            model_attempt=2,
            execution_attempt=2,
        ),
        # call-b: first attempt accepted.
        _fact(
            seq=6,
            logical_call_id="call-b",
            kind="valid_input_accepted",
            tool_name="get_xbrl_fact",
        ),
        _fact(
            seq=7,
            logical_call_id="call-b",
            kind="execution_started",
            tool_name="get_xbrl_fact",
            execution_attempt=1,
        ),
        _fact(
            seq=8,
            logical_call_id="call-b",
            kind="succeeded",
            tool_name="get_xbrl_fact",
            execution_attempt=1,
        ),
        # call-c: exhausted on the first attempt, never accepted.
        _fact(seq=9, logical_call_id="call-c", kind="schema_rejected"),
        _fact(seq=10, logical_call_id="call-c", kind="correction_exhausted"),
    ]

    measure = compute_tool_measures(facts, []).first_attempt_schema_valid_rate

    assert (measure.numerator, measure.denominator) == (1, 3)
    assert measure.value == 1 / 3
    assert measure.na is False
    assert measure.unknown == 0


def test_first_attempt_denominator_counts_logical_calls_not_attempts() -> None:
    facts = [
        _fact(seq=1, logical_call_id="call-a", kind="schema_rejected"),
        _fact(seq=2, logical_call_id="call-a", kind="correction_offered"),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=2,
        ),
    ]

    measure = compute_tool_measures(facts, []).first_attempt_schema_valid_rate

    assert (measure.numerator, measure.denominator) == (0, 1)


# ----- correction recovery --------------------------------------------------


def test_correction_recovery_rate_requires_later_valid_input() -> None:
    facts = [
        # call-a: corrected on a later attempt.
        _fact(seq=1, logical_call_id="call-a", kind="schema_rejected"),
        _fact(seq=2, logical_call_id="call-a", kind="correction_offered"),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=2,
        ),
        # call-b: correction offered, second attempt also invalid.
        _fact(seq=4, logical_call_id="call-b", kind="schema_rejected"),
        _fact(seq=5, logical_call_id="call-b", kind="correction_offered"),
        _fact(
            seq=6,
            logical_call_id="call-b",
            kind="schema_rejected",
            model_attempt=2,
        ),
        _fact(
            seq=7,
            logical_call_id="call-b",
            kind="correction_exhausted",
            model_attempt=2,
        ),
        # call-c: valid on the first attempt, not eligible for correction.
        _fact(seq=8, logical_call_id="call-c", kind="valid_input_accepted"),
    ]

    measure = compute_tool_measures(facts, []).correction_recovery_rate

    assert (measure.numerator, measure.denominator) == (1, 2)
    assert measure.value == 0.5
    assert measure.na is False


def test_valid_input_before_the_correction_is_not_recovery() -> None:
    facts = [
        _fact(
            seq=1,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=1,
        ),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="correction_offered",
            model_attempt=2,
        ),
    ]

    measure = compute_tool_measures(facts, []).correction_recovery_rate

    assert (measure.numerator, measure.denominator) == (0, 1)


# ----- execution success and unknown outcomes -------------------------------


def test_execution_success_reports_unknown_separately() -> None:
    facts = [
        # call-a: succeeded.
        _fact(seq=1, logical_call_id="call-a", kind="valid_input_accepted"),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="succeeded",
            execution_attempt=1,
        ),
        # call-b: known failure.
        _fact(
            seq=4,
            logical_call_id="call-b",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=5,
            logical_call_id="call-b",
            kind="known_failed",
            execution_attempt=1,
        ),
        # call-c: unknown effect.
        _fact(
            seq=6,
            logical_call_id="call-c",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=7,
            logical_call_id="call-c",
            kind="outcome_unknown",
            execution_attempt=1,
        ),
        # call-d: reconciled, and the reconciliation proves the effect
        # happened.
        _fact(
            seq=8,
            logical_call_id="call-d",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=9,
            logical_call_id="call-d",
            kind="reconciled",
            execution_attempt=1,
            outcome_code="proven_applied",
        ),
    ]

    measure = compute_tool_measures(facts, []).execution_success_rate

    assert (measure.numerator, measure.denominator) == (2, 4)
    assert measure.value == 0.5
    # The unknown call is in the denominator, never in the numerator.
    assert measure.unknown == 1


def test_a_reconciliation_is_a_success_only_when_the_effect_happened() -> None:
    facts = [
        # call-a: reconciled, the effect DID happen.
        _fact(
            seq=1,
            logical_call_id="call-a",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="reconciled",
            execution_attempt=1,
            outcome_code="proven_applied",
        ),
        # call-b: reconciled, the effect did NOT happen.
        _fact(
            seq=3,
            logical_call_id="call-b",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=4,
            logical_call_id="call-b",
            kind="reconciled",
            execution_attempt=1,
            outcome_code="proven_not_applied",
        ),
    ]

    measures = compute_tool_measures(facts, [])

    # Only the proven effect counts as a success. The proven non-effect is
    # a known non-effect: neither a success nor an unknown.
    assert (
        measures.execution_success_rate.numerator,
        measures.execution_success_rate.denominator,
    ) == (1, 2)
    assert measures.execution_success_rate.unknown == 0
    # The attempt-level supplement follows the same rule.
    assert (
        measures.execution_attempt_success_rate.numerator,
        measures.execution_attempt_success_rate.denominator,
    ) == (1, 2)
    assert measures.execution_attempt_success_rate.unknown == 0


def test_a_reconciliation_without_a_resolution_code_stays_unknown() -> None:
    facts = [
        _fact(
            seq=1,
            logical_call_id="call-a",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="reconciled",
            execution_attempt=1,
        ),
    ]

    measure = compute_tool_measures(facts, []).execution_success_rate

    assert (measure.numerator, measure.denominator) == (0, 1)
    assert measure.unknown == 1


def test_an_unfinished_execution_is_unknown_not_success() -> None:
    facts = [
        _fact(
            seq=1,
            logical_call_id="call-a",
            kind="execution_started",
            execution_attempt=1,
        ),
    ]

    measure = compute_tool_measures(facts, []).execution_success_rate

    assert (measure.numerator, measure.denominator) == (0, 1)
    assert measure.unknown == 1


# ----- N/A on zero eligible denominators ------------------------------------


def test_zero_eligible_denominators_report_na() -> None:
    measures = compute_tool_measures([], [])

    for measure in (
        measures.first_attempt_schema_valid_rate,
        measures.correction_recovery_rate,
        measures.execution_success_rate,
        measures.expected_tool_selection,
        measures.unnecessary_call_rate,
        measures.tool_use_rate,
        measures.tool_use_by_opportunity.required,
        measures.tool_use_by_opportunity.optional,
        measures.tool_use_by_opportunity.prohibited,
        measures.execution_attempt_success_rate,
    ):
        assert measure.denominator == 0, measure.label
        assert measure.value is None, measure.label
        assert measure.na is True, measure.label


# ----- retry visibility -----------------------------------------------------


def test_a_retried_logical_call_is_one_call_with_several_attempts() -> None:
    facts = [
        # Model attempt 1: invalid, corrected.
        _fact(seq=1, logical_call_id="call-a", kind="schema_rejected"),
        _fact(seq=2, logical_call_id="call-a", kind="correction_offered"),
        # Model attempt 2: valid, execution attempt fails.
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=2,
        ),
        _fact(
            seq=4,
            logical_call_id="call-a",
            kind="execution_started",
            model_attempt=2,
            execution_attempt=2,
            business_operation_id="op-1",
        ),
        _fact(
            seq=5,
            logical_call_id="call-a",
            kind="known_failed",
            model_attempt=2,
            execution_attempt=2,
            business_operation_id="op-1",
        ),
        # Model attempt 3: valid, execution attempt succeeds.
        _fact(
            seq=6,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=3,
        ),
        _fact(
            seq=7,
            logical_call_id="call-a",
            kind="execution_started",
            model_attempt=3,
            execution_attempt=3,
            business_operation_id="op-1",
        ),
        _fact(
            seq=8,
            logical_call_id="call-a",
            kind="succeeded",
            model_attempt=3,
            execution_attempt=3,
            business_operation_id="op-1",
        ),
    ]

    measures = compute_tool_measures(facts, [])
    key = ("run-1", "call-a")

    assert measures.first_attempt_schema_valid_rate.denominator == 1
    assert measures.correction_recovery_rate.denominator == 1
    assert measures.correction_recovery_rate.numerator == 1
    assert measures.execution_success_rate.denominator == 1
    assert measures.execution_success_rate.numerator == 1

    assert measures.retries.logical_calls == 1
    assert measures.retries.retried_logical_calls == 1
    assert measures.retries.model_attempts_by_logical_call == {key: 3}
    assert measures.retries.execution_attempts_by_logical_call == {key: 2}
    assert measures.retries.execution_attempts_by_business_operation == {
        ("run-1", "op-1"): 2
    }


def test_logical_call_identity_includes_the_run() -> None:
    facts = [
        _fact(
            seq=1,
            run_id="run-1",
            logical_call_id="call-a",
            kind="valid_input_accepted",
        ),
        _fact(
            seq=1,
            run_id="run-2",
            logical_call_id="call-a",
            kind="valid_input_accepted",
        ),
    ]

    measures = compute_tool_measures(facts, [])

    assert measures.first_attempt_schema_valid_rate.denominator == 2
    assert measures.retries.logical_calls == 2


# ----- rubric measures ------------------------------------------------------


def test_opportunities_are_separated_and_measured_in_cases() -> None:
    facts = [
        _fact(
            seq=1,
            run_id="run-1",
            logical_call_id="call-a",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
        _fact(
            seq=2,
            run_id="run-2",
            logical_call_id="call-b",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
        _fact(
            seq=3,
            run_id="run-3",
            logical_call_id="call-c",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
    ]
    cases = [
        _case(
            "case-1",
            run_ids=("run-1",),
            opportunities=(
                _opportunity("REQUIRED", "retrieve"),
                _opportunity("OPTIONAL", "compare_facts"),
            ),
        ),
        _case(
            "case-2",
            run_ids=("run-2",),
            opportunities=(_opportunity("REQUIRED", "get_xbrl_fact"),),
        ),
        _case(
            "case-3",
            run_ids=("run-3",),
            opportunities=(_opportunity("PROHIBITED", "retrieve"),),
        ),
        _case(
            "case-4",
            run_ids=("run-4",),
            opportunities=(_opportunity("PROHIBITED", None),),
        ),
    ]

    measures = compute_tool_measures(facts, cases)

    # Only named REQUIRED opportunities can select an expected tool.
    assert (measures.expected_tool_selection.numerator, measures.expected_tool_selection.denominator) == (1, 2)
    # A prohibited tool that was called, plus a no-tool case that called none.
    assert (measures.unnecessary_call_rate.numerator, measures.unnecessary_call_rate.denominator) == (1, 2)
    # Descriptive: three of four rubric cases emitted a call.
    assert (measures.tool_use_rate.numerator, measures.tool_use_rate.denominator) == (3, 4)
    assert measures.tool_use_by_opportunity.required.value == 1.0
    assert measures.tool_use_by_opportunity.optional.value == 1.0
    assert measures.tool_use_by_opportunity.prohibited.value == 0.5


def test_tool_use_is_descriptive_not_a_quality_score() -> None:
    facts = [
        _fact(
            seq=1,
            run_id="run-1",
            logical_call_id="call-a",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
        _fact(
            seq=2,
            run_id="run-2",
            logical_call_id="call-b",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
    ]
    cases = [
        _case(
            "required",
            run_ids=("run-1",),
            opportunities=(_opportunity("REQUIRED", "retrieve"),),
        ),
        _case(
            "prohibited",
            run_ids=("run-2",),
            opportunities=(_opportunity("PROHIBITED", "retrieve"),),
        ),
    ]

    measures = compute_tool_measures(facts, cases)

    # Both cases emit one call, so the descriptive rate is identical ...
    assert measures.tool_use_rate.value == 1.0
    # ... while only the rubric measures distinguish good from bad.
    assert measures.expected_tool_selection.value == 1.0
    assert measures.unnecessary_call_rate.value == 1.0
    assert not hasattr(measures.tool_use_rate, "higher_is_better")


def test_required_opportunity_without_a_tool_is_not_a_selection_target() -> None:
    cases = [
        _case(
            "case-1",
            opportunities=(_opportunity("REQUIRED", None),),
        )
    ]

    measures = compute_tool_measures([], cases)

    assert measures.expected_tool_selection.na is True
    assert measures.tool_use_rate.denominator == 1


# ----- execution-attempt success rate ---------------------------------------


def test_execution_attempt_success_rate_supplements_logical_success() -> None:
    facts = [
        # One logical call, two execution attempts: the first fails, the
        # second succeeds.
        _fact(seq=1, logical_call_id="call-a", kind="valid_input_accepted"),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="execution_started",
            model_attempt=1,
            execution_attempt=1,
        ),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="known_failed",
            model_attempt=1,
            execution_attempt=1,
        ),
        _fact(
            seq=4,
            logical_call_id="call-a",
            kind="valid_input_accepted",
            model_attempt=2,
        ),
        _fact(
            seq=5,
            logical_call_id="call-a",
            kind="execution_started",
            model_attempt=2,
            execution_attempt=2,
        ),
        _fact(
            seq=6,
            logical_call_id="call-a",
            kind="succeeded",
            model_attempt=2,
            execution_attempt=2,
        ),
    ]

    measures = compute_tool_measures(facts, [])

    assert measures.execution_success_rate.value == 1.0
    assert (
        measures.execution_attempt_success_rate.numerator,
        measures.execution_attempt_success_rate.denominator,
    ) == (1, 2)
    assert measures.execution_attempt_success_rate.value == 0.5
    assert measures.execution_attempt_success_rate.label != (
        measures.execution_success_rate.label
    )


def test_execution_attempt_unknown_stays_separate() -> None:
    facts = [
        _fact(
            seq=1,
            logical_call_id="call-a",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="outcome_unknown",
            execution_attempt=1,
        ),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="execution_started",
            model_attempt=2,
            execution_attempt=2,
        ),
        _fact(
            seq=4,
            logical_call_id="call-a",
            kind="succeeded",
            model_attempt=2,
            execution_attempt=2,
        ),
    ]

    measure = compute_tool_measures(facts, []).execution_attempt_success_rate

    assert (measure.numerator, measure.denominator) == (1, 2)
    assert measure.unknown == 1


# ----- no span / receipt / event input --------------------------------------


def test_aggregation_inputs_are_facts_and_cases_only() -> None:
    signature = inspect.signature(compute_tool_measures)
    assert list(signature.parameters) == ["facts", "cases"]

    facts = [
        _fact(seq=1, logical_call_id="call-a", kind="valid_input_accepted"),
        _fact(
            seq=2,
            logical_call_id="call-a",
            kind="execution_started",
            execution_attempt=1,
        ),
        _fact(
            seq=3,
            logical_call_id="call-a",
            kind="succeeded",
            execution_attempt=1,
        ),
    ]

    first = compute_tool_measures(facts, [])
    second = compute_tool_measures(facts, [])

    assert first == second
    assert first.execution_success_rate.numerator == 1


# ----- rubric input contracts ----------------------------------------------


def test_opportunity_labels_and_names_are_validated() -> None:
    with pytest.raises(ValueError):
        ToolOpportunity(label="MAYBE", tool_name="retrieve")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolOpportunity(label="REQUIRED", tool_name="  ")


def test_a_scored_case_requires_an_id_and_at_least_one_run() -> None:
    with pytest.raises(ValueError):
        ScoredCase(
            case_id="",
            rubric_id="s4-tool-use",
            rubric_version="1.0.0",
            run_ids=("run-1",),
        )
    with pytest.raises(ValueError):
        ScoredCase(
            case_id="case-1",
            rubric_id="s4-tool-use",
            rubric_version="1.0.0",
            run_ids=(),
        )


def test_a_scored_case_names_the_rubric_it_was_scored_against() -> None:
    with pytest.raises(ValueError):
        ScoredCase(
            case_id="case-1",
            rubric_id="  ",
            rubric_version="1.0.0",
            run_ids=("run-1",),
        )
    with pytest.raises(ValueError):
        ScoredCase(
            case_id="case-1",
            rubric_id="s4-tool-use",
            rubric_version="",
            run_ids=("run-1",),
        )


def test_the_rubric_measures_reproduce_from_a_named_rubric() -> None:
    """The three rubric-based measures, from a rubric that names itself."""
    rubric = {
        "rubric_id": "s4-tool-use",
        "rubric_version": "1.2.0",
        "cases": [
            {
                "case_id": "case-required",
                "run_ids": ["run-1"],
                "opportunities": [{"label": "REQUIRED", "tool_name": "retrieve"}],
            },
            {
                "case_id": "case-other-tool",
                "run_ids": ["run-2"],
                "opportunities": [
                    {"label": "REQUIRED", "tool_name": "get_xbrl_fact"}
                ],
            },
            {
                "case_id": "case-prohibited",
                "run_ids": ["run-3"],
                "opportunities": [{"label": "PROHIBITED", "tool_name": "retrieve"}],
            },
            {
                "case_id": "case-no-tool",
                "run_ids": ["run-4"],
                "opportunities": [{"label": "PROHIBITED", "tool_name": None}],
            },
        ],
    }
    facts = [
        _fact(
            seq=1,
            run_id="run-1",
            logical_call_id="call-a",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
        _fact(
            seq=2,
            run_id="run-2",
            logical_call_id="call-b",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
        _fact(
            seq=3,
            run_id="run-3",
            logical_call_id="call-c",
            kind="valid_input_accepted",
            tool_name="retrieve",
        ),
    ]
    cases = [
        ScoredCase(
            case_id=case["case_id"],
            rubric_id=rubric["rubric_id"],
            rubric_version=rubric["rubric_version"],
            run_ids=tuple(case["run_ids"]),
            opportunities=tuple(
                ToolOpportunity(
                    label=opportunity["label"],  # type: ignore[arg-type]
                    tool_name=opportunity["tool_name"],
                )
                for opportunity in case["opportunities"]
            ),
        )
        for case in rubric["cases"]
    ]

    measures = compute_tool_measures(facts, cases)

    # The report names the rubric its numbers came from.
    assert measures.rubric_id == "s4-tool-use"
    assert measures.rubric_version == "1.2.0"
    # Expected-tool selection: only the named REQUIRED opportunities count.
    assert (
        measures.expected_tool_selection.numerator,
        measures.expected_tool_selection.denominator,
    ) == (1, 2)
    # Unnecessary calls: the prohibited tool was called, the no-tool case
    # was not.
    assert (
        measures.unnecessary_call_rate.numerator,
        measures.unnecessary_call_rate.denominator,
    ) == (1, 2)
    # Descriptive tool use: three of four rubric cases emitted a call.
    assert (
        measures.tool_use_rate.numerator,
        measures.tool_use_rate.denominator,
    ) == (3, 4)


def test_cases_from_different_rubrics_cannot_share_one_report() -> None:
    cases = [
        _case("case-1", rubric_id="s4-tool-use", rubric_version="1.0.0"),
        _case("case-2", rubric_id="s4-tool-use", rubric_version="1.1.0"),
    ]

    with pytest.raises(ValueError):
        compute_tool_measures([], cases)


def test_measures_without_a_scored_case_name_no_rubric() -> None:
    measures = compute_tool_measures([], [])

    assert measures.rubric_id is None
    assert measures.rubric_version is None


def test_a_case_without_opportunities_is_in_no_rubric_denominator() -> None:
    measures = compute_tool_measures([], [_case("case-1")])

    assert measures.tool_use_rate.na is True
    assert measures.expected_tool_selection.na is True
    assert measures.unnecessary_call_rate.na is True
