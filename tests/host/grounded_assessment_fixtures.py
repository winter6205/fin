"""Shared builders for the grounded-assessment test modules.

This module is a helper, NOT a test module: it holds only the constants and the
builders the validator-rule suite and the schema-invariant suite share, and it
contains no test function and no assertion about product behaviour. The two
suites are split by concern but drive the same schema shapes, so the builders
need exactly one authority; a second copy in each test module is how they drift
apart when the schema gains a field. That includes the host-side input: the
carrier a validation is narrowed against is built here once (``_host_facts``),
so a new host fact is added in one place.

``tests/host`` is not a package, so this is imported by plain module name
(``from grounded_assessment_fixtures import ...``), the same way ``jev_fake``
and ``grounded_assessment_cases`` are.
"""

from __future__ import annotations

from fin.host.grounded_assessment import (
    AssessmentValidation,
    CapabilityState,
    EvidenceKind,
    EvidenceRef,
    FailureClass,
    GoalPart,
    GroundedAssessment,
    HostFacts,
    ProposedCheck,
    ProposedClarification,
    SupportedClaim,
    validate_assessment,
)

ADMITTED = frozenset(
    {
        EvidenceRef(kind="company", id="doc-A#unit-1"),
        EvidenceRef(kind="company", id="doc-B#unit-2"),
    }
)
# Permission and availability are HOST facts, never model-set candidate
# fields: the host reports, per capability, whether it is permitted and
# currently available.
HOST_CAPS = {
    "retrieve": CapabilityState(permitted=True, available=True),
    "read_source": CapabilityState(permitted=True, available=True),
}


def _evidence(eid: str, kind: EvidenceKind = "company") -> EvidenceRef:
    return EvidenceRef(kind=kind, id=eid)


def _support(*items: str | EvidenceRef) -> tuple[EvidenceRef, ...]:
    # Admission is the (kind, id) pair, so a test that is not about the kind
    # axis may name a bare locator id and still read naturally.
    return tuple(
        item if isinstance(item, EvidenceRef) else _evidence(item)
        for item in items
    )


def _claim(
    claim_id: str = "C1",
    *,
    derivation: str = "source_statement",
    support: tuple[str | EvidenceRef, ...] = ("doc-A#unit-1",),
    premises: tuple[str, ...] = (),
    subject: str = "revenue policy",
    applicability: str = "FY2023, employees",
) -> SupportedClaim:
    return SupportedClaim(
        claim_id=claim_id,
        text="a conclusion",
        subject=subject,
        derivation=derivation,  # type: ignore[arg-type]
        support=_support(*support),
        premises=premises,
        applicability=applicability,
    )


def _part(
    part_id: str = "P1",
    *,
    status: str = "supported",
    claim_ids: tuple[str, ...] = ("C1",),
    gap: str = "",
    conflict: str = "",
) -> GoalPart:
    return GoalPart(
        part_id=part_id,
        description="the requested part",
        status=status,  # type: ignore[arg-type]
        claim_ids=claim_ids,
        gap=gap,
        conflict=conflict,
    )


def _check(
    gap: str = "missing Q3 detail",
    *,
    capability: str = "retrieve",
    prospect: str = "reasonable",
) -> ProposedCheck:
    return ProposedCheck(
        gap=gap,
        capability=capability,
        prospect=prospect,  # type: ignore[arg-type]
    )


def _assessment(
    *,
    claims: tuple[SupportedClaim, ...] = (_claim(),),
    goal_parts: tuple[GoalPart, ...] = (_part(),),
    proposed_checks: tuple[ProposedCheck, ...] = (),
    proposed_clarification: ProposedClarification | None = None,
    failure: str | None = None,
    completion: str = "complete",
) -> GroundedAssessment:
    return GroundedAssessment(
        claims=claims,
        goal_parts=goal_parts,
        proposed_checks=proposed_checks,
        proposed_clarification=proposed_clarification,
        failure=failure,  # type: ignore[arg-type]
        completion=completion,  # type: ignore[arg-type]
    )


def _host_facts(
    *,
    admitted: frozenset[EvidenceRef] = ADMITTED,
    host_capabilities: dict[str, CapabilityState] | None = None,
    basis_committed: bool = True,
    committed_goal_part_ids: frozenset[str] = frozenset(),
    observed_receipt_classes: frozenset[FailureClass] = frozenset(),
) -> HostFacts:
    """One host-facts carrier, with the permissive baseline the rule suite wants.

    The two committed-fact sets default to empty: the carrier's documented
    "the host committed no decomposition / observed no receipt" state, so a test
    that is not about whole-goal coverage or about receipts reads the pre-existing
    behaviour, and a test that IS about one names the set explicitly.
    """
    return HostFacts(
        admitted_evidence=admitted,
        host_capabilities=(
            HOST_CAPS if host_capabilities is None else host_capabilities
        ),
        basis_committed=basis_committed,
        committed_goal_part_ids=committed_goal_part_ids,
        observed_receipt_classes=observed_receipt_classes,
    )


def _validate(
    assessment: GroundedAssessment,
    *,
    admitted: frozenset[EvidenceRef] = ADMITTED,
    host_capabilities: dict[str, CapabilityState] | None = None,
    basis_committed: bool = True,
    committed_goal_part_ids: frozenset[str] = frozenset(),
    observed_receipt_classes: frozenset[FailureClass] = frozenset(),
) -> AssessmentValidation:
    return validate_assessment(
        assessment,
        host_facts=_host_facts(
            admitted=admitted,
            host_capabilities=host_capabilities,
            basis_committed=basis_committed,
            committed_goal_part_ids=committed_goal_part_ids,
            observed_receipt_classes=observed_receipt_classes,
        ),
    )


def _rich_assessment() -> GroundedAssessment:
    return _assessment(
        claims=(
            _claim("C1", support=("doc-A#unit-1",)),
            _claim(
                "C2",
                derivation="calculation",
                support=("doc-B#unit-2",),
                premises=("C1",),
            ),
        ),
        goal_parts=(
            _part(claim_ids=("C1",)),
            _part("P2", status="unresolved", claim_ids=(), gap="needs Q3"),
        ),
        proposed_checks=(_check(),),
        proposed_clarification=ProposedClarification(
            condition="which entity?", changes="the band", choices=("e", "c")
        ),
        failure="unsupported",
        completion="partial",
    )


def _obj(value: object) -> dict[str, object]:
    """Narrow one stored record node to its mapping shape."""
    if not isinstance(value, dict):
        raise AssertionError(f"expected a mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _seq(value: object) -> list[object]:
    """Narrow one stored record node to its sequence shape."""
    if not isinstance(value, list):
        raise AssertionError(f"expected a sequence, got {type(value).__name__}")
    return list(value)
