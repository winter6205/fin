"""Validator-rule tests for the pure grounded-assessment contract.

``fin.host.grounded.grounded_assessment`` is the host-owned, **mechanical** contract that
narrows a model-produced assessment against one ``HostFacts`` carrier: the
evidence it admitted, the capabilities it reports, the committed basis, the
goal parts it committed, and the receipts it observed. This module covers
what ``validate_assessment`` keeps, withholds, or narrows — the normal path,
rules R1..R7, boundary and empty input, and the negative "never promotes
/ never resolves a conflict / never infers a derivation" properties — plus the
stable error-code exports. The schema's own structural guards
(``Malformed*Error``, the closed-vocabulary rot guards, and the reflected
``to_record`` / ``from_payload`` conversion, including the matrix of which
declared field a payload omits) live in
``test_grounded_assessment_schema.py``. The validator has no caller yet (the
graph, prompt, and state integration belongs to the Runtime round), so every test
here is a pure call through the feature boundary: no model, no network, no time,
no storage. ``tests/host`` is not a package, so imports are absolute and the
shared builders come from ``grounded_assessment_fixtures``.

Two ``from_payload`` properties are behaviour rather than shape and are therefore
specified here: the refusal a caller catches is typed, never a bare ``KeyError``,
and a field the model sent empty is a declaration the host must not read as an
omission.
"""

from __future__ import annotations

from typing import cast

import pytest

from fin.host.grounded import grounded_assessment as grounded_assessment_module
from fin.host.grounded.grounded_assessment import (
    CODE_CLAIM_MISSING_PREMISES,
    CODE_CLAIM_NO_SUPPORT_LOCATOR,
    CODE_CLAIM_UNKNOWN_PREMISE,
    CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR,
    CODE_CLAIM_USER_CONDITION_NOT_EVIDENCE,
    CODE_CLAIM_WRONG_EVIDENCE_KIND,
    CODE_COMPLETION_UNSUPPORTED,
    CODE_COMMITTED_GOAL_PART_UNCOVERED,
    CODE_FAILURE_RECEIPT_NOT_OBSERVED,
    CODE_GOAL_PARTS_EMPTY,
    CODE_GOAL_PART_MISSING_GAP,
    CODE_GOAL_PART_UNSUPPORTED_CLAIMS,
    RECEIPT_SHAPED_FAILURE_CLASSES,
    USER_CONDITION_PREFIX,
    CapabilityState,
    FailureClass,
    GroundedAssessment,
    MalformedPayloadError,
    ProposedClarification,
    SupportedClaim,
)
from grounded_assessment_fixtures import (
    HOST_CAPS,
    _assessment,
    _check,
    _claim,
    _evidence,
    _part,
    _rich_assessment,
    _validate,
)

# ----- normal behaviour -----


def test_fully_valid_assessment_is_accepted() -> None:
    assessment = _assessment(
        claims=(
            _claim("C1", support=("doc-A#unit-1",)),
            _claim(
                "C2",
                derivation="calculation",
                support=("doc-B#unit-2",),
                premises=("C1",),
            ),
        ),
        goal_parts=(_part(claim_ids=("C1", "C2")),),
        proposed_checks=(_check(),),
        proposed_clarification=ProposedClarification(
            condition="which entity?",
            changes="the approval band",
            choices=("employee", "contractor"),
        ),
        completion="complete",
    )

    result = _validate(assessment, basis_committed=True)

    assert result.accepted is True
    assert result.errors == ()
    assert [c.claim_id for c in result.supported_claims] == ["C1", "C2"]
    assert len(result.goal_parts) == 1
    assert len(result.proposed_checks) == 1
    assert result.proposed_clarification is not None
    assert result.failure is None
    assert result.completion == "complete"


def test_validation_is_pure_and_deterministic() -> None:
    assessment = _assessment(
        claims=(_claim("C1"), _claim("C2", support=("unknown-loc",))),
        completion="partial",
    )

    first = _validate(assessment, basis_committed=False)
    second = _validate(assessment, basis_committed=False)

    assert first == second
    # The model's input is not mutated (frozen dataclasses).
    assert assessment.claims[1].support[0].id == "unknown-loc"


# ----- R1: support is specific (D1) -----


def test_r1_claim_with_admitted_evidence_survives() -> None:
    result = _validate(_assessment(claims=(_claim("C1"),)))

    assert [c.claim_id for c in result.supported_claims] == ["C1"]
    assert result.errors == ()


def test_r1_claim_with_unknown_evidence_is_dropped_with_error() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=("not-admitted",)),),
            goal_parts=(_part(claim_ids=("C1",), gap="no support"),),
            completion="partial",
        ),
        basis_committed=False,
    )

    assert result.supported_claims == ()
    assert CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR in result.errors
    assert result.accepted is False


def test_r1_claim_with_no_evidence_is_dropped_with_error() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=()),),
            goal_parts=(_part(claim_ids=("C1",), gap="no support"),),
            completion="partial",
        )
    )

    assert result.supported_claims == ()
    assert CODE_CLAIM_NO_SUPPORT_LOCATOR in result.errors


def test_r1_user_condition_reference_is_rejected_with_distinct_code() -> None:
    condition_id = f"{USER_CONDITION_PREFIX}employee_band_1500"
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=(condition_id,)),),
            goal_parts=(_part(claim_ids=("C1",), gap="needs policy"),),
            completion="partial",
        )
    )

    assert result.supported_claims == ()
    assert CODE_CLAIM_USER_CONDITION_NOT_EVIDENCE in result.errors
    # The user-condition exit is distinct from the unknown-locator exit.
    assert CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR not in result.errors


# ----- R1: evidence kinds reuse the existing evidence contract -----


def test_locator_and_database_row_are_distinct_kinds() -> None:
    locator = _evidence("doc-A#unit-1", kind="company")
    row = _evidence("ledger#row-42", kind="database-row")

    assert locator.kind != row.kind
    # A database-row is admitted by its own identity, kept distinct from a
    # locator rather than folded into the locator vocabulary.
    result = _validate(
        _assessment(claims=(_claim("C1", support=()),), goal_parts=(_part(),)),
        admitted=frozenset({row}),
    )
    claim = SupportedClaim(
        claim_id="C1",
        text="a row fact",
        subject="ledger",
        derivation="source_statement",
        support=(row,),
        premises=(),
        applicability="FY2023",
    )
    row_result = _validate(
        _assessment(claims=(claim,), goal_parts=(_part(),)),
        admitted=frozenset({row}),
    )
    assert result.supported_claims == ()
    assert [c.claim_id for c in row_result.supported_claims] == ["C1"]


def test_r1_admitted_id_with_the_wrong_kind_is_withheld_under_its_own_code() -> None:
    # The host admitted that id as a company document. Keeping the id while
    # changing its kind is a different product fact from citing an id the host
    # never admitted, so the two exits must not collapse into one another.
    row_cite = _evidence("doc-A#unit-1", kind="database-row")
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=(row_cite,)),),
            goal_parts=(_part(claim_ids=("C1",), gap="wrong kind"),),
            completion="partial",
        )
    )

    assert result.supported_claims == ()
    assert CODE_CLAIM_WRONG_EVIDENCE_KIND in result.errors
    assert CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR not in result.errors


def test_r1_admission_is_the_kind_and_id_pair_not_the_id_alone() -> None:
    locator = _evidence("ledger#row-42", kind="company")
    row = _evidence("ledger#row-42", kind="database-row")
    claims = (_claim("C1", support=(locator,)), _claim("C2", support=(row,)))

    both = _validate(
        _assessment(claims=claims, goal_parts=(_part(claim_ids=("C1", "C2")),)),
        admitted=frozenset({locator, row}),
    )
    # Admitting the id under one kind does not admit it under the other, so
    # dropping one pair withholds exactly the claim that cited it.
    one_kind_only = _validate(
        _assessment(
            claims=claims,
            goal_parts=(_part(claim_ids=("C1", "C2"), gap="row not admitted"),),
            completion="partial",
        ),
        admitted=frozenset({locator}),
    )

    assert [c.claim_id for c in both.supported_claims] == ["C1", "C2"]
    assert both.errors == ()
    assert both.completion == "complete"
    assert [c.claim_id for c in one_kind_only.supported_claims] == ["C1"]
    assert CODE_CLAIM_WRONG_EVIDENCE_KIND in one_kind_only.errors


def test_r1_a_database_row_pair_is_usable_evidence_once_admitted() -> None:
    row = _evidence("ledger#row-42", kind="database-row")

    result = _validate(
        _assessment(claims=(_claim("C1", support=(row,)),)),
        admitted=frozenset({row}),
    )

    assert [c.claim_id for c in result.supported_claims] == ["C1"]
    assert result.errors == ()


# ----- R2: source and derivation stay distinct (D2) -----


def test_r2_derived_claim_without_premise_is_dropped() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1", derivation="inference", premises=()),),
            goal_parts=(_part(claim_ids=("C1",), gap="no premises"),),
            completion="partial",
        )
    )

    assert result.supported_claims == ()
    assert CODE_CLAIM_MISSING_PREMISES in result.errors


def test_r2_source_statement_with_premises_is_allowed_and_kept() -> None:
    result = _validate(
        _assessment(
            claims=(
                _claim("C1", derivation="source_statement", premises=("C0",)),
            ),
        )
    )

    assert [c.claim_id for c in result.supported_claims] == ["C1"]


def test_r2_derivation_is_never_inferred_by_the_host() -> None:
    source = _validate(
        _assessment(
            claims=(
                _claim("C1", derivation="source_statement"),
            ),
        )
    )
    # The calculated branch declares its premise (R2) so the only property
    # under test is that the host keeps the label the model gave.
    calculated = _validate(
        _assessment(
            claims=(
                _claim("P0"),
                _claim(
                    "C1",
                    derivation="calculation",
                    support=("doc-A#unit-1",),
                    premises=("P0",),
                ),
            ),
            goal_parts=(_part(claim_ids=("P0", "C1")),),
        )
    )

    assert source.supported_claims[0].derivation == "source_statement"
    assert calculated.supported_claims[1].derivation == "calculation"


def test_r2_derived_claim_with_unknown_premise_is_dropped() -> None:
    result = _validate(
        _assessment(
            claims=(
                _claim(
                    "C1",
                    derivation="calculation",
                    support=("doc-A#unit-1",),
                    premises=("ghost",),
                ),
            ),
            goal_parts=(_part(claim_ids=("C1",), gap="no premises"),),
            completion="partial",
        )
    )

    assert result.supported_claims == ()
    assert CODE_CLAIM_UNKNOWN_PREMISE in result.errors


def test_r2_derived_claim_with_declared_premise_survives() -> None:
    result = _validate(
        _assessment(
            claims=(
                _claim("P0", support=("doc-A#unit-1",)),
                _claim(
                    "C1",
                    derivation="calculation",
                    support=("doc-B#unit-2",),
                    premises=("P0",),
                ),
            ),
            goal_parts=(_part(claim_ids=("C1",)),),
        )
    )

    assert [c.claim_id for c in result.supported_claims] == ["P0", "C1"]
    assert result.errors == ()


def test_r2_premise_resolves_against_the_declared_set() -> None:
    """A premise counts as resolved when it names a DECLARED claim id.

    Resolution is against the declared claim-id set, not the surviving
    set: whether the premise claim itself survives is that claim's own
    R1/R2 judgment, recorded on it separately. The dependent claim is
    not double-penalised with an unknown-premise error.
    """
    result = _validate(
        _assessment(
            claims=(
                _claim("P0", support=("not-admitted",)),
                _claim(
                    "C1",
                    derivation="calculation",
                    support=("doc-A#unit-1",),
                    premises=("P0",),
                ),
            ),
            goal_parts=(_part(claim_ids=("C1",), gap="no support"),),
            completion="partial",
        )
    )

    # P0 is withheld on its own support failure...
    assert "P0" not in [c.claim_id for c in result.supported_claims]
    assert CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR in result.errors
    # ...but its id is declared, so C1's premise resolves.
    assert "C1" in [c.claim_id for c in result.supported_claims]
    assert CODE_CLAIM_UNKNOWN_PREMISE not in result.errors


# ----- R3: assess the whole goal (D3) -----


def test_r3_requires_at_least_one_goal_part() -> None:
    result = _validate(_assessment(goal_parts=(), completion="partial"))

    assert CODE_GOAL_PARTS_EMPTY in result.errors
    assert result.goal_parts == ()


def test_r3_supported_part_needs_a_surviving_claim() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=("unknown-loc",)),),
            goal_parts=(_part(status="supported", claim_ids=("C1",), gap=""),),
            completion="partial",
        )
    )

    assert CODE_GOAL_PART_UNSUPPORTED_CLAIMS in result.errors
    assert result.goal_parts == ()


def test_r3_part_whose_support_fell_away_keeps_the_gap_it_named() -> None:
    # A part declared supported whose claims did not survive is not supported, but
    # the gap it names is the material limitation D4 asks to be reported: dropping
    # the part would erase it and leave the goal looking shorter than the assessment
    # found it. One code is recorded for one defect, not two.
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=("unknown-loc",)),),
            goal_parts=(
                _part(
                    status="supported",
                    claim_ids=("C1",),
                    gap="contractor scope not evidenced",
                ),
            ),
            completion="partial",
        )
    )

    assert CODE_GOAL_PART_UNSUPPORTED_CLAIMS in result.errors
    # One code for the one defect: the part was kept, so no gap was missing.
    assert CODE_GOAL_PART_MISSING_GAP not in result.errors
    assert len(result.goal_parts) == 1
    part = result.goal_parts[0]
    assert part.status == "unresolved"
    assert part.gap == "contractor scope not evidenced"
    assert part.claim_ids == ()
    assert result.accepted is False


def test_r3_unresolved_part_requires_a_gap() -> None:
    result = _validate(
        _assessment(
            claims=(),
            goal_parts=(
                _part(status="unresolved", claim_ids=(), gap=""),
            ),
            completion="unresolved",
        )
    )

    assert CODE_GOAL_PART_MISSING_GAP in result.errors
    assert result.goal_parts == ()


def test_r3_conflict_forces_unresolved_and_preserves_the_conflict() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1"),),
            goal_parts=(
                _part(
                    status="supported",
                    claim_ids=("C1",),
                    gap="two policies conflict",
                    conflict="FY2022 rule vs FY2023 rule",
                ),
            ),
            completion="partial",
        )
    )

    assert len(result.goal_parts) == 1
    part = result.goal_parts[0]
    assert part.status == "unresolved"
    assert part.conflict == "FY2022 rule vs FY2023 rule"
    # A conflict is never resolved by the host.
    assert result.completion != "complete"


def test_r3_a_self_reported_conflict_is_narrowed_without_a_refusal_code() -> None:
    # The host narrows, it does not punish. A part that names a conflict has
    # already reported the disagreement, so losing its support on top of that is
    # forced ``unresolved`` and records no error code — otherwise honest reporting
    # of a conflict would refuse the assessment it was meant to surface.
    result = _validate(
        _assessment(
            claims=(_claim("C1", support=("unknown-loc",)),),
            goal_parts=(
                _part(
                    status="supported",
                    claim_ids=("C1",),
                    conflict="FY2022 rule vs FY2023 rule",
                ),
            ),
            completion="partial",
        )
    )

    assert CODE_GOAL_PART_UNSUPPORTED_CLAIMS not in result.errors
    assert len(result.goal_parts) == 1
    assert result.goal_parts[0].status == "unresolved"
    assert result.goal_parts[0].conflict == "FY2022 rule vs FY2023 rule"
    assert result.accepted is False  # the claim itself was refused, not the part


def test_r3_unresolved_part_keeps_its_surviving_claims() -> None:
    # D3 / D7: a part left unresolved is still partial progress, and the only
    # record of WHICH established findings it rests on is its claim references.
    # The part's ``status`` already says it is not established.
    result = _validate(
        _assessment(
            claims=(
                _claim("C1"),
                _claim("C2", support=("not-admitted",)),
            ),
            goal_parts=(
                _part(
                    "P1",
                    status="unresolved",
                    claim_ids=("C1", "C2"),
                    gap="the cap's applicability is unestablished",
                ),
            ),
            completion="partial",
        )
    )

    assert [c.claim_id for c in result.supported_claims] == ["C1"]
    part = result.goal_parts[0]
    assert part.status == "unresolved"
    assert part.claim_ids == ("C1",)


def test_r3_conflict_part_forced_unresolved_keeps_its_surviving_claim() -> None:
    result = _validate(
        _assessment(
            claims=(
                _claim("C1"),
                _claim("C2", support=("not-admitted",)),
            ),
            goal_parts=(
                _part(
                    "P1",
                    status="supported",
                    claim_ids=("C1", "C2"),
                    gap="which source prevails?",
                    conflict="FY2022 rule says 3%, FY2023 rule says 5%",
                ),
            ),
            completion="partial",
        )
    )

    part = result.goal_parts[0]
    assert part.status == "unresolved"
    assert part.claim_ids == ("C1",)
    # The conflict text is preserved verbatim, not rewritten or summarised.
    assert part.conflict == "FY2022 rule says 3%, FY2023 rule says 5%"


def test_r3_unresolved_part_citing_only_withheld_claims_keeps_no_refs() -> None:
    # Negative control: the unresolved path FILTERS to surviving ids, it does
    # not echo whatever the part declared. Here nothing the part cited survived.
    result = _validate(
        _assessment(
            claims=(
                _claim("C1", support=("not-admitted",)),
                _claim("C2", support=()),
            ),
            goal_parts=(
                _part(
                    "P1",
                    status="unresolved",
                    claim_ids=("C1", "C2"),
                    gap="no cited support survived",
                ),
            ),
            completion="partial",
        )
    )

    assert result.supported_claims == ()
    assert result.goal_parts[0].claim_ids == ()


def test_r3_unresolved_part_without_a_gap_is_still_dropped() -> None:
    # Keeping a surviving reference does not rescue a part that names no gap.
    result = _validate(
        _assessment(
            claims=(_claim("C1"),),
            goal_parts=(
                _part("P1", status="unresolved", claim_ids=("C1",), gap=""),
            ),
            completion="partial",
        )
    )

    assert CODE_GOAL_PART_MISSING_GAP in result.errors
    assert result.goal_parts == ()


def test_r3_committed_goal_parts_decide_whole_goal_coverage() -> None:
    # D3: coverage is measured over the parts the HOST committed, not the list the
    # candidate declared — a generated task list is not the goal's authority.
    # ``under`` declares one of a goal committed as two; ``whole`` declares both.
    host_goal = frozenset({"P1", "P2"})
    parts = (_part(claim_ids=("C1",)), _part("P2", claim_ids=("C1",)))

    under = _validate(
        _assessment(goal_parts=parts[:1]), committed_goal_part_ids=host_goal
    )
    whole = _validate(
        _assessment(goal_parts=parts), committed_goal_part_ids=host_goal
    )

    assert under.completion != "complete"
    assert CODE_COMMITTED_GOAL_PART_UNCOVERED in under.errors
    assert CODE_COMPLETION_UNSUPPORTED in under.errors
    # Narrowing only: a committed part no part declares is never added.
    assert [p.part_id for p in under.goal_parts] == ["P1"]
    assert whole.completion == "complete"
    assert whole.errors == ()


def test_r3_conflict_only_part_survives_and_a_blank_conflict_does_not() -> None:
    # D4: a conflict the host may not resolve must reach Runtime even when no gap
    # is named; whitespace is not a conflict, so that part is still dropped.
    conflict = "FY2022 rule says 3%, FY2023 rule says 5%"
    kept = _validate(
        _assessment(goal_parts=(_part(conflict=conflict),), completion="partial")
    )
    blank = _part(status="unresolved", claim_ids=(), conflict="  ")
    dropped = _validate(_assessment(goal_parts=(blank,), completion="partial"))

    assert kept.errors == ()
    assert kept.goal_parts[0].status == "unresolved"
    assert kept.goal_parts[0].conflict == conflict
    assert dropped.errors == (CODE_GOAL_PART_MISSING_GAP,)
    assert dropped.goal_parts == ()


# ----- R4: continue only when useful and permitted (D5) -----


def test_r4_qualifying_check_is_kept() -> None:
    result = _validate(_assessment(proposed_checks=(_check(),)))

    assert len(result.proposed_checks) == 1


def test_r4_non_qualifying_checks_are_dropped_silently() -> None:
    # Permission and availability come from the HOST map, never the candidate:
    # the model cannot grant itself a capability by naming one.
    host = {
        "retrieve": CapabilityState(permitted=True, available=True),
        "secret": CapabilityState(permitted=False, available=True),
        "offline": CapabilityState(permitted=True, available=False),
    }
    assessment = _assessment(
        proposed_checks=(
            _check(gap="  "),  # empty gap
            _check(capability="unadvertised"),  # not reported by the host
            _check(capability="secret"),  # host reports not permitted
            _check(capability="offline"),  # host reports not available
            _check(capability="retrieve", prospect="unclear"),  # no prospect
            _check(capability="retrieve", prospect="none"),  # no prospect at all
        ),
    )

    result = _validate(assessment, host_capabilities=host)

    assert result.proposed_checks == ()
    # Silent drop is NOT an error (spec: repeating an exhausted direction
    # without a new basis is not progress, not a validation failure).
    assert result.errors == ()
    assert result.accepted is True


def test_r4_model_cannot_invent_a_permission() -> None:
    # A capability the host does not report at all is dropped even when the
    # candidate wants it with a reasonable prospect.
    result = _validate(
        _assessment(proposed_checks=(_check(capability="write_source"),)),
        host_capabilities=HOST_CAPS,
    )
    assert result.proposed_checks == ()


# ----- R5: clarify only actionable conditions (D6) -----


def test_r5_actionable_clarification_is_kept() -> None:
    result = _validate(
        _assessment(
            proposed_clarification=ProposedClarification(
                condition="which entity?",
                changes="the approval band",
                choices=("employee", "contractor"),
            )
        )
    )

    assert result.proposed_clarification is not None
    assert result.proposed_clarification.condition == "which entity?"


@pytest.mark.parametrize(
    "clarification",
    [
        ProposedClarification(condition="", changes="x", choices=("a",)),
        ProposedClarification(condition="x", changes="", choices=("a",)),
        ProposedClarification(condition="  ", changes="x", choices=("a",)),
    ],
)


def test_r5_non_actionable_clarification_is_dropped_silently(
    clarification: ProposedClarification,
) -> None:
    result = _validate(_assessment(proposed_clarification=clarification))

    assert result.proposed_clarification is None
    assert result.errors == ()
    assert result.accepted is True


# ----- R6: failure classes stay distinct (D7) -----


@pytest.mark.parametrize("receipt", sorted(RECEIPT_SHAPED_FAILURE_CLASSES))
def test_r6_unobserved_receipt_failure_is_withheld(receipt: FailureClass) -> None:
    # A receipt-shaped failure states a host operation outcome, so naming one
    # cannot establish it: withheld, coded, and no completion gate opens either.
    result = _validate(_assessment(failure=receipt))

    assert result.failure is None
    assert CODE_FAILURE_RECEIPT_NOT_OBSERVED in result.errors
    assert CODE_COMPLETION_UNSUPPORTED in result.errors
    assert result.completion != "complete"


@pytest.mark.parametrize("receipt", sorted(RECEIPT_SHAPED_FAILURE_CLASSES))
def test_r6_observed_receipt_failure_survives_unchanged(receipt: FailureClass) -> None:
    # The host that ran the operation reports the receipt: echoed, not narrowed.
    result = _validate(
        _assessment(completion="partial", failure=receipt),
        observed_receipt_classes=frozenset({receipt}),
    )

    assert result.failure == receipt
    assert CODE_FAILURE_RECEIPT_NOT_OBSERVED not in result.errors
    assert result.accepted is True


def test_r6_judgment_shaped_failure_is_never_receipt_gated() -> None:
    # ``unsupported`` is the model's own semantic judgment (D1): no receipt can
    # exist for it, so an empty observed set leaves it untouched.
    result = _validate(_assessment(completion="partial", failure="unsupported"))

    assert result.failure == "unsupported"
    assert result.accepted is True


def test_r6_successful_empty_stays_distinct_from_unsupported() -> None:
    # Spec D7: a lookup that succeeded but found no support is its own class,
    # distinct from an unsupported condition; neither collapses into the other.
    # An empty lookup IS a host operation outcome, so that receipt is observed.
    empty = _validate(
        _assessment(completion="partial", failure="successful_empty"),
        observed_receipt_classes=frozenset({"successful_empty"}),
    )
    unsupported = _validate(_assessment(completion="partial", failure="unsupported"))

    assert empty.failure == "successful_empty"
    assert unsupported.failure == "unsupported"
    assert empty.failure != unsupported.failure


# ----- R7: separate task completion from turn ending (D8) -----


def test_r7_complete_accepted_when_preconditions_met() -> None:
    result = _validate(_assessment(completion="complete", failure=None))

    assert result.completion == "complete"
    assert result.errors == ()


def test_r7_complete_without_committed_basis_is_error_and_narrowed() -> None:
    # The committed basis is a HOST fact: even a fully supported whole goal
    # cannot become complete while the host reports the basis uncommitted —
    # an absent basis cannot be made to look committed by the model.
    result = _validate(
        _assessment(
            claims=(_claim("C1"),),
            goal_parts=(_part(claim_ids=("C1",)),),
            completion="complete",
            failure=None,
        ),
        basis_committed=False,
    )

    assert CODE_COMPLETION_UNSUPPORTED in result.errors
    assert result.completion == "partial"
    assert result.accepted is False


def test_r7_complete_with_an_unresolved_part_is_error_and_narrowed() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1"),),
            goal_parts=(
                _part(claim_ids=("C1",)),
                _part("P2", status="unresolved", claim_ids=(), gap="missing"),
            ),
            completion="complete",
        )
    )

    assert CODE_COMPLETION_UNSUPPORTED in result.errors
    assert result.completion == "partial"


def test_r7_complete_with_a_failure_is_error_and_narrowed() -> None:
    result = _validate(
        _assessment(
            completion="complete",
            failure="dependency_failed",
        )
    )

    assert CODE_COMPLETION_UNSUPPORTED in result.errors
    assert result.completion == "partial"


def test_r7_unresolved_part_that_keeps_claims_never_makes_completion_complete() -> None:
    # An unresolved part now carries its surviving references; status is what
    # completion reads, so a part citing established findings must not be able
    # to lift the task to ``complete`` while that part is unresolved.
    result = _validate(
        _assessment(
            claims=(
                _claim("C1"),
                _claim("C2", support=("not-admitted",)),
            ),
            goal_parts=(
                _part("P1", status="supported", claim_ids=("C1",)),
                _part(
                    "P2",
                    status="unresolved",
                    claim_ids=("C1",),
                    gap="the operative rule for contractors is unestablished",
                ),
            ),
            completion="complete",
        )
    )

    assert result.goal_parts[1].status == "unresolved"
    assert result.goal_parts[1].claim_ids == ("C1",)
    assert CODE_COMPLETION_UNSUPPORTED in result.errors
    assert result.completion != "complete"
    assert result.completion == "partial"


def test_r7_honest_partial_narrows_to_unresolved_when_nothing_survives() -> None:
    result = _validate(
        _assessment(
            claims=(),
            goal_parts=(_part(status="unresolved", claim_ids=(), gap="needs"),),
            completion="partial",
        ),
        basis_committed=False,
    )

    assert result.completion == "unresolved"
    assert result.errors == ()
    assert result.accepted is True


def test_r7_honest_partial_with_surviving_claims_is_not_promoted() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1"),),
            goal_parts=(
                _part(status="unresolved", claim_ids=(), gap="needs"),
            ),
            completion="partial",
        ),
        basis_committed=False,
    )

    assert result.completion == "partial"


def test_r7_empty_assessment_has_no_goal_parts_and_narrows() -> None:
    result = _validate(
        _assessment(claims=(), goal_parts=(), completion="complete"),
        basis_committed=False,
    )

    assert CODE_GOAL_PARTS_EMPTY in result.errors
    assert CODE_COMPLETION_UNSUPPORTED in result.errors
    assert result.completion == "unresolved"


# ----- negative: the host only narrows, never promotes or resolves -----


def test_never_promotes_an_unsupported_claim() -> None:
    assessment = _assessment(
        claims=(
            _claim("C1", support=("unknown-loc",)),
            _claim("C2", support=(f"{USER_CONDITION_PREFIX}band",)),
        ),
        goal_parts=(_part(claim_ids=("C1", "C2"), gap="none survive"),),
        completion="partial",
    )

    result = _validate(assessment, basis_committed=False)

    assert result.supported_claims == ()
    assert result.errors
    assert result.accepted is False


def test_never_resolves_a_conflict() -> None:
    result = _validate(
        _assessment(
            claims=(_claim("C1"),),
            goal_parts=(
                _part(
                    status="supported",
                    claim_ids=("C1",),
                    gap="which source prevails?",
                    conflict="source X says 3, source Y says 5",
                ),
            ),
            completion="complete",
        )
    )

    assert result.goal_parts[0].status == "unresolved"
    assert "source X says 3" in result.goal_parts[0].conflict
    assert result.completion != "complete"


# ----- stable error codes: one assertable exit per rule -----


def _exported_codes() -> dict[str, str]:
    """Every ``CODE_*`` constant the module exports, keyed by attribute name."""
    names = [
        name for name in grounded_assessment_module.__all__ if name.startswith("CODE_")
    ]
    return {name: getattr(grounded_assessment_module, name) for name in names}


def test_wrong_evidence_kind_code_is_exported_and_unique() -> None:
    codes = _exported_codes()
    assert "CODE_CLAIM_WRONG_EVIDENCE_KIND" in codes
    assert codes["CODE_CLAIM_WRONG_EVIDENCE_KIND"] == CODE_CLAIM_WRONG_EVIDENCE_KIND
    # The stable wire value is what consumers record, so it is pinned here.
    assert CODE_CLAIM_WRONG_EVIDENCE_KIND == "claim_wrong_evidence_kind"
    # Every rule's exit is importable and carries its own wire value: an exit no
    # consumer can import is not an exit, and one that reuses another rule's
    # value would make two rules read as one.
    assert set(codes) == {
        name for name in vars(grounded_assessment_module) if name.startswith("CODE_")
    }
    for name, wire in (
        ("CODE_COMMITTED_GOAL_PART_UNCOVERED", "committed_goal_part_uncovered"),
        ("CODE_FAILURE_RECEIPT_NOT_OBSERVED", "failure_receipt_not_observed"),
    ):
        assert name in codes
        assert codes[name] == getattr(grounded_assessment_module, name) == wire
    values = list(codes.values())
    assert len(values) == len(set(values))


# ----- wire payload: the refusal type, and an empty value as a declaration -----
#
# WHICH declared field is missing at which nesting level, refused by name, is the
# wire contract and lives as one parametrized matrix in
# ``test_grounded_assessment_schema.py``. What is specified here is the behaviour
# around that refusal: the error type Runtime catches at this boundary, and that an
# empty sequence the model did send is a declaration, not an omission.


def test_from_payload_missing_field_is_typed_not_key_error() -> None:
    # Runtime catches ``GroundedAssessmentError`` at this boundary; a bare
    # ``KeyError`` escaping instead would be an un-typed crash with no shared
    # base to catch, so the escape itself — not just the raised class — is
    # what this refuses.
    assert not issubclass(MalformedPayloadError, KeyError)
    record = _rich_assessment().to_record()
    claims = cast(list[dict[str, object]], record["claims"])
    del claims[0]["subject"]

    with pytest.raises(MalformedPayloadError):
        try:
            GroundedAssessment.from_payload(record)
        except KeyError as leaked:
            pytest.fail(f"a bare KeyError escaped from_payload: {leaked!r}")


def test_from_payload_accepts_an_explicitly_empty_premises_field() -> None:
    # Positive control for the refusal above: an empty sequence that the model
    # did send is a valid declaration, so the guard refuses omission only.
    record = _rich_assessment().to_record()
    claims = cast(list[dict[str, object]], record["claims"])
    claims[1]["premises"] = []

    decoded = GroundedAssessment.from_payload(record)

    assert decoded.claims[1].premises == ()
