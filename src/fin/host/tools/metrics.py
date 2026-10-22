"""P3 T6: tool measures from canonical ledger facts and a frozen rubric.

Every rate is computed from immutable
:class:`~fin.host.tools.ledger.ToolOutcomeFact` rows (the canonical
attempt/outcome record) plus the rubric of each scored case. Spans, turn
receipts, UI rows and event counts are never inputs: they may be sampled or
missing and cannot change a numerator or denominator.

Logical-call measures group facts by ``(run_id, logical_call_id)``; one
execution attempt is ``(run_id, logical_call_id, execution_attempt)``. A
zero eligible denominator reports ``N/A`` (``value is None``), never 0 or 1.
Unknown execution outcomes stay in the denominator and in ``unknown``, never
in a success numerator. Retry counts stay visible per logical call and per
business operation so a retried call stays one logical call.

A reconciled fact is a success only when its resolution code proves the
effect happened (``proven_applied``). A ``proven_not_applied`` reconciliation
proves the effect did NOT happen: it is a known non-effect, neither a
success nor an unknown.

Every scored case names the frozen rubric it was scored against, and the
returned measures carry that rubric identity, so a report can say which
rubric produced its numbers.

Opportunity units are cases, not calls. A ``tool_name`` of ``None`` means a
"no tool" opportunity: it prohibits any call and is never an expected-tool
selection target. A case declares a tool opportunity when its rubric labels
any opportunity, so the descriptive tool-use denominator and its per-class
split stay consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Mapping, Sequence

from fin.host.tools.ledger import ToolOutcomeFact

OpportunityLabel = Literal["REQUIRED", "OPTIONAL", "PROHIBITED"]
_OPPORTUNITY_LABELS: Final[frozenset[str]] = frozenset(
    {"REQUIRED", "OPTIONAL", "PROHIBITED"}
)

#: Facts that observe the canonical validator for one model attempt.
_VALIDATION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "schema_rejected",
        "correction_offered",
        "valid_input_accepted",
        "correction_exhausted",
    }
)
#: Facts that observe an execution attempt and its final result.
_EXECUTION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "execution_started",
        "succeeded",
        "known_failed",
        "outcome_unknown",
        "reconciled",
    }
)
#: The reconciliation code that proves the effect DID happen.
_PROVEN_APPLIED: Final[str] = "proven_applied"
#: The reconciliation code that proves the effect did NOT happen.
_PROVEN_NOT_APPLIED: Final[str] = "proven_not_applied"

CallKey = tuple[str, str]
_AttemptKey = tuple[str, str, int]


@dataclass(frozen=True)
class Measure:
    """One rate plus the counts a reviewer needs to reproduce it.

    ``value`` is ``None`` and ``na`` is ``True`` when no case or call was
    eligible (``denominator == 0``). ``unknown`` counts the observations
    whose effect is still unresolved and that are already inside
    ``denominator``; a proven non-effect is neither a success nor unknown.
    """

    label: str
    numerator: int
    denominator: int
    value: float | None
    na: bool
    unknown: int = 0


@dataclass(frozen=True)
class ToolOpportunity:
    """One frozen-rubric label for a case.

    ``tool_name`` identifies the tool the label applies to; ``None`` marks a
    "no tool" opportunity (any call is a violation).
    """

    label: OpportunityLabel
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if self.label not in _OPPORTUNITY_LABELS:
            raise ValueError(f"unknown opportunity label: {self.label!r}")
        if self.tool_name is not None and not self.tool_name.strip():
            raise ValueError("tool_name must be non-empty or None")


@dataclass(frozen=True)
class ScoredCase:
    """One scored case and the ledger runs that produced its facts.

    ``rubric_id`` and ``rubric_version`` name the frozen rubric the case was
    scored against, so the measures computed from it can say which rubric
    produced their numbers.
    """

    case_id: str
    rubric_id: str
    rubric_version: str
    run_ids: tuple[str, ...]
    opportunities: tuple[ToolOpportunity, ...] = ()

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must be non-empty")
        if not self.rubric_id.strip():
            raise ValueError("rubric_id must be non-empty")
        if not self.rubric_version.strip():
            raise ValueError("rubric_version must be non-empty")
        if not self.run_ids:
            raise ValueError("a scored case requires at least one run_id")


@dataclass(frozen=True)
class RetrySummary:
    """Retries stay visible without inflating logical-call denominators.

    Keys are ``(run_id, logical_call_id)`` and ``(run_id,
    business_operation_id)``. A business-operation attempt is counted as a
    ``(logical_call_id, execution_attempt)`` pair because execution attempt
    numbers are only unique within a logical call.
    """

    logical_calls: int
    retried_logical_calls: int
    model_attempts_by_logical_call: Mapping[CallKey, int]
    execution_attempts_by_logical_call: Mapping[CallKey, int]
    execution_attempts_by_business_operation: Mapping[CallKey, int]


@dataclass(frozen=True)
class ToolUseByOpportunity:
    """The descriptive tool-use rate split by opportunity class."""

    required: Measure
    optional: Measure
    prohibited: Measure


@dataclass(frozen=True)
class ToolMeasures:
    """The S4 measures, the attempt-level supplement and retry visibility.

    ``rubric_id`` and ``rubric_version`` name the frozen rubric the scored
    cases were scored against; they are ``None`` only when no case was
    scored.
    """

    rubric_id: str | None
    rubric_version: str | None
    first_attempt_schema_valid_rate: Measure
    correction_recovery_rate: Measure
    execution_success_rate: Measure
    expected_tool_selection: Measure
    unnecessary_call_rate: Measure
    tool_use_rate: Measure
    tool_use_by_opportunity: ToolUseByOpportunity
    execution_attempt_success_rate: Measure
    retries: RetrySummary


def _measure(
    label: str, numerator: int, denominator: int, *, unknown: int = 0
) -> Measure:
    if min(numerator, denominator, unknown) < 0:
        raise ValueError(f"{label}: counts must be non-negative")
    if numerator > denominator or unknown > denominator:
        raise ValueError(f"{label}: counts exceed the denominator")
    if denominator == 0:
        return Measure(
            label=label,
            numerator=0,
            denominator=0,
            value=None,
            na=True,
            unknown=0,
        )
    return Measure(
        label=label,
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator,
        na=False,
        unknown=unknown,
    )


def _group_by_call(
    facts: Sequence[ToolOutcomeFact],
) -> dict[CallKey, list[ToolOutcomeFact]]:
    grouped: dict[CallKey, list[ToolOutcomeFact]] = {}
    for fact in facts:
        grouped.setdefault(
            (fact.run_id, fact.logical_call_id), []
        ).append(fact)
    return grouped


def _is_success(fact: ToolOutcomeFact) -> bool:
    """Whether the final observation proves the effect happened.

    A ``reconciled`` fact proves success only when its resolution code is
    ``proven_applied``: the reconciliation resolved an uncertain effect and
    the effect turned out to have happened.
    """
    if fact.kind == "succeeded":
        return True
    if fact.kind == "reconciled":
        return fact.outcome_code == _PROVEN_APPLIED
    return False


def _is_unknown(fact: ToolOutcomeFact) -> bool:
    """Whether the final observation leaves the effect unresolved.

    A ``reconciled`` fact that carries no resolution code proves nothing,
    so it stays unknown. A ``proven_not_applied`` reconciliation is NOT
    unknown: it proves the effect did not happen.
    """
    if fact.kind in ("outcome_unknown", "execution_started"):
        return True
    if fact.kind == "reconciled":
        return fact.outcome_code not in (_PROVEN_APPLIED, _PROVEN_NOT_APPLIED)
    return False


def _last(
    facts: Sequence[ToolOutcomeFact],
) -> ToolOutcomeFact:
    """The latest observation in commit order."""
    return max(facts, key=lambda fact: fact.fact_seq)


def _first_attempt_schema_valid(
    facts: Sequence[ToolOutcomeFact],
) -> Measure:
    numerator = denominator = 0
    for call_facts in _group_by_call(facts).values():
        validation = [
            fact for fact in call_facts if fact.kind in _VALIDATION_KINDS
        ]
        if not validation:
            continue
        denominator += 1
        first_attempt = min(fact.model_attempt for fact in validation)
        if any(
            fact.kind == "valid_input_accepted"
            and fact.model_attempt == first_attempt
            for fact in validation
        ):
            numerator += 1
    return _measure(
        "first-attempt schema-valid rate", numerator, denominator
    )


def _correction_recovery(facts: Sequence[ToolOutcomeFact]) -> Measure:
    numerator = denominator = 0
    for call_facts in _group_by_call(facts).values():
        corrected_at = [
            fact.model_attempt
            for fact in call_facts
            if fact.kind == "correction_offered"
        ]
        if not corrected_at:
            continue
        denominator += 1
        offered_attempt = min(corrected_at)
        if any(
            fact.kind == "valid_input_accepted"
            and fact.model_attempt > offered_attempt
            for fact in call_facts
        ):
            numerator += 1
    return _measure("correction recovery rate", numerator, denominator)


def _execution_success(facts: Sequence[ToolOutcomeFact]) -> Measure:
    numerator = denominator = unknown = 0
    for call_facts in _group_by_call(facts).values():
        if not any(
            fact.kind == "execution_started" for fact in call_facts
        ):
            continue
        denominator += 1
        execution = [
            fact for fact in call_facts if fact.kind in _EXECUTION_KINDS
        ]
        final = _last(execution)
        if _is_success(final):
            numerator += 1
        elif _is_unknown(final):
            unknown += 1
        # A proven non-effect (reconciled / proven_not_applied) is a known
        # non-success: it is neither a success nor an unknown.
    return _measure(
        "logical-call execution success rate",
        numerator,
        denominator,
        unknown=unknown,
    )


def _execution_attempt_success(
    facts: Sequence[ToolOutcomeFact],
) -> Measure:
    groups: dict[_AttemptKey, list[ToolOutcomeFact]] = {}
    for fact in facts:
        if fact.execution_attempt is None:
            continue
        groups.setdefault(
            (fact.run_id, fact.logical_call_id, fact.execution_attempt), []
        ).append(fact)

    numerator = denominator = unknown = 0
    for attempt_facts in groups.values():
        denominator += 1
        execution = [
            fact
            for fact in attempt_facts
            if fact.kind in _EXECUTION_KINDS
        ]
        if not execution:
            unknown += 1
            continue
        final = _last(execution)
        if _is_success(final):
            numerator += 1
        elif _is_unknown(final):
            unknown += 1
    return _measure(
        "execution-attempt success rate (test-model-visible)",
        numerator,
        denominator,
        unknown=unknown,
    )


def _retry_summary(facts: Sequence[ToolOutcomeFact]) -> RetrySummary:
    model_attempts: dict[CallKey, set[int]] = {}
    execution_attempts: dict[CallKey, set[int]] = {}
    business_attempts: dict[CallKey, set[tuple[str, int]]] = {}
    for fact in facts:
        call_key = (fact.run_id, fact.logical_call_id)
        model_attempts.setdefault(call_key, set()).add(fact.model_attempt)
        if fact.execution_attempt is None:
            continue
        execution_attempts.setdefault(call_key, set()).add(
            fact.execution_attempt
        )
        if fact.business_operation_id is not None:
            business_attempts.setdefault(
                (fact.run_id, fact.business_operation_id), set()
            ).add((fact.logical_call_id, fact.execution_attempt))

    calls = set(model_attempts) | set(execution_attempts)
    retried = {
        key
        for key in calls
        if len(model_attempts.get(key, ())) > 1
        or len(execution_attempts.get(key, ())) > 1
    }
    return RetrySummary(
        logical_calls=len(calls),
        retried_logical_calls=len(retried),
        model_attempts_by_logical_call={
            key: len(model_attempts[key]) for key in sorted(model_attempts)
        },
        execution_attempts_by_logical_call={
            key: len(execution_attempts[key])
            for key in sorted(execution_attempts)
        },
        execution_attempts_by_business_operation={
            key: len(business_attempts[key])
            for key in sorted(business_attempts)
        },
    )


def _facts_by_run(
    facts: Sequence[ToolOutcomeFact],
) -> dict[str, list[ToolOutcomeFact]]:
    grouped: dict[str, list[ToolOutcomeFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.run_id, []).append(fact)
    return grouped


def _case_facts(
    case: ScoredCase,
    by_run: Mapping[str, list[ToolOutcomeFact]],
) -> list[ToolOutcomeFact]:
    return [
        fact
        for run_id in case.run_ids
        for fact in by_run.get(run_id, ())
    ]


def _expected_tool_selection(
    cases: Sequence[ScoredCase],
    by_run: Mapping[str, list[ToolOutcomeFact]],
) -> Measure:
    numerator = denominator = 0
    for case in cases:
        accepted = {
            fact.tool_name
            for fact in _case_facts(case, by_run)
            if fact.kind == "valid_input_accepted"
        }
        for opportunity in case.opportunities:
            if (
                opportunity.label != "REQUIRED"
                or opportunity.tool_name is None
            ):
                continue
            denominator += 1
            if opportunity.tool_name in accepted:
                numerator += 1
    return _measure(
        "expected-tool selection", numerator, denominator
    )


def _unnecessary_call_rate(
    cases: Sequence[ScoredCase],
    by_run: Mapping[str, list[ToolOutcomeFact]],
) -> Measure:
    numerator = denominator = 0
    for case in cases:
        prohibited = [
            opportunity
            for opportunity in case.opportunities
            if opportunity.label == "PROHIBITED"
        ]
        if not prohibited:
            continue
        denominator += 1
        case_facts = _case_facts(case, by_run)
        called = {fact.tool_name for fact in case_facts}
        violated = False
        for opportunity in prohibited:
            if opportunity.tool_name is None:
                violated = violated or bool(case_facts)
            elif opportunity.tool_name in called:
                violated = True
        if violated:
            numerator += 1
    return _measure(
        "unnecessary-call rate", numerator, denominator
    )


def _tool_use(
    cases: Sequence[ScoredCase],
    by_run: Mapping[str, list[ToolOutcomeFact]],
) -> tuple[Measure, ToolUseByOpportunity]:
    numerator = denominator = 0
    split: dict[str, list[int]] = {
        label: [0, 0] for label in _OPPORTUNITY_LABELS
    }
    for case in cases:
        called = bool(_case_facts(case, by_run))
        if case.opportunities:
            denominator += 1
            numerator += int(called)
        for label in {opportunity.label for opportunity in case.opportunities}:
            split[label][1] += 1
            split[label][0] += int(called)

    def _split_measure(label: str) -> Measure:
        split_numerator, split_denominator = split[label]
        return _measure(
            f"tool-use rate ({label.lower()} opportunities)",
            split_numerator,
            split_denominator,
        )

    return (
        _measure("tool-use rate (descriptive)", numerator, denominator),
        ToolUseByOpportunity(
            required=_split_measure("REQUIRED"),
            optional=_split_measure("OPTIONAL"),
            prohibited=_split_measure("PROHIBITED"),
        ),
    )


def _rubric_identity(
    cases: Sequence[ScoredCase],
) -> tuple[str | None, str | None]:
    """The one rubric the scored cases were scored against.

    A report whose cases come from different rubrics cannot name the rubric
    its measures were computed from, so that mix is refused.
    """
    identities = {(case.rubric_id, case.rubric_version) for case in cases}
    if not identities:
        return (None, None)
    if len(identities) > 1:
        raise ValueError(
            "scored cases must share one rubric id and rubric version"
        )
    rubric_id, rubric_version = identities.pop()
    return (rubric_id, rubric_version)


def compute_tool_measures(
    facts: Sequence[ToolOutcomeFact],
    cases: Sequence[ScoredCase],
) -> ToolMeasures:
    """Derive the S4 measures from ledger facts and scored cases.

    ``facts`` are canonical ledger rows (any number of runs); ``cases`` are
    the frozen rubric. The result is deterministic for the same inputs and
    contains no span, turn-receipt, UI or event-count contribution. The
    returned measures name the rubric the cases were scored against.
    """
    by_run = _facts_by_run(facts)
    tool_use_rate, tool_use_split = _tool_use(cases, by_run)
    rubric_id, rubric_version = _rubric_identity(cases)
    return ToolMeasures(
        rubric_id=rubric_id,
        rubric_version=rubric_version,
        first_attempt_schema_valid_rate=_first_attempt_schema_valid(facts),
        correction_recovery_rate=_correction_recovery(facts),
        execution_success_rate=_execution_success(facts),
        expected_tool_selection=_expected_tool_selection(cases, by_run),
        unnecessary_call_rate=_unnecessary_call_rate(cases, by_run),
        tool_use_rate=tool_use_rate,
        tool_use_by_opportunity=tool_use_split,
        execution_attempt_success_rate=_execution_attempt_success(facts),
        retries=_retry_summary(facts),
    )


__all__ = [
    "Measure",
    "OpportunityLabel",
    "RetrySummary",
    "ScoredCase",
    "ToolMeasures",
    "ToolOpportunity",
    "ToolUseByOpportunity",
    "compute_tool_measures",
]
