"""Schema and wire-contract invariants of the grounded-assessment types.

Companion to ``test_grounded_assessment.py``, which owns the validator rules.
This module owns the *structure*: each frozen dataclass refuses a value outside
its own invariants (``Malformed*Error``, never a narrowing), every closed Literal
vocabulary stays the schema's single authority — the rot guard covers each one, not
a chosen few — and ``to_record`` / ``from_payload`` are driven by one reflected
field-name authority: every field present at every nesting level, the nested
evidence ref kept as its ``(kind, id)`` pair, the committed basis absent because it
is a host fact, and a malformed payload shape refused instead of coerced into
something plausible. A declared field a payload omits is refused **by name** at
every depth, which is the one wire rule this module states as a matrix rather than
as one test per field. Pure and hermetic: no model, no network, no time, no
storage. ``tests/host`` is not a package, so the shared builders are imported by
plain module name from ``grounded_assessment_fixtures``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from typing import Any, Final, cast, get_args

import pytest

from fin.host.citation_gate import QUOTABLE_EVIDENCE_KINDS
from fin.host.grounded_assessment import (
    RECEIPT_SHAPED_FAILURE_CLASSES,
    Completion,
    Derivation,
    EvidenceKind,
    EvidenceRef,
    FailureClass,
    GoalPart,
    GroundedAssessment,
    GroundedAssessmentError,
    HostFacts,
    MalformedAssessmentError,
    MalformedCheckError,
    MalformedClaimError,
    MalformedEvidenceRefError,
    MalformedGoalPartError,
    MalformedPayloadError,
    PartStatus,
    ProposedCheck,
    ProposedClarification,
    Prospect,
    SupportedClaim,
    validate_assessment,
)
from grounded_assessment_fixtures import (
    _assessment,
    _check,
    _claim,
    _evidence,
    _host_facts,
    _obj,
    _part,
    _rich_assessment,
    _seq,
    _support,
    _validate,
)

# ----- closed vocabularies: the schema's Literal sets are the contract -----


def test_evidence_kind_reuses_the_existing_evidence_contract() -> None:
    # A rot guard: the schema's kind vocabulary must equal the enterprise
    # evidence contract's, so no second evidence taxonomy is introduced.
    assert frozenset(get_args(EvidenceKind)) == QUOTABLE_EVIDENCE_KINDS


def test_receipt_and_judgment_failure_classes_partition_the_vocabulary() -> None:
    # The receipt-shaped set is the one authority for which failures the host must
    # have observed. It stays a proper subset of the closed vocabulary, and what it
    # leaves over is exactly the model's own semantic judgment — so no failure
    # class is both gated and ungated, and none is left undeclared.
    vocabulary = frozenset(get_args(FailureClass))
    assert RECEIPT_SHAPED_FAILURE_CLASSES < vocabulary
    assert vocabulary - RECEIPT_SHAPED_FAILURE_CLASSES == frozenset({"unsupported"})


# Each closed vocabulary with the builder that puts a member of it through the schema
# boundary that checks it, and the field that must carry that member back. Adding a
# member to a Literal makes this guard construct it, so a runtime set left behind
# cannot quietly refuse a valid member; reading the member back is what stops a
# builder that maps two members onto one value from passing as coverage.
def _build_claim(derivation: str) -> SupportedClaim:
    return _claim("C1", derivation=derivation)


def _build_part(status: str) -> GoalPart:
    return _part(status=status)


def _build_check(prospect: str) -> ProposedCheck:
    return _check(prospect=prospect)


def _build_assessment(completion: str) -> GroundedAssessment:
    return _assessment(completion=completion)


def _build_evidence_ref(kind: str) -> EvidenceRef:
    return _evidence("doc-A#unit-1", kind=cast(EvidenceKind, kind))


def _build_failing_assessment(failure: str) -> GroundedAssessment:
    return _assessment(failure=failure, completion="partial")


_CLOSED_VOCABULARIES: Final[
    tuple[tuple[str, object, Callable[[str], object], str], ...]
] = (
    ("Derivation", Derivation, _build_claim, "derivation"),
    ("PartStatus", PartStatus, _build_part, "status"),
    ("Prospect", Prospect, _build_check, "prospect"),
    ("Completion", Completion, _build_assessment, "completion"),
    ("EvidenceKind", EvidenceKind, _build_evidence_ref, "kind"),
    ("FailureClass", FailureClass, _build_failing_assessment, "failure"),
)


@pytest.mark.parametrize(
    ("name", "literal", "build", "field"),
    _CLOSED_VOCABULARIES,
    ids=[entry[0] for entry in _CLOSED_VOCABULARIES],
)
def test_every_closed_vocabulary_accepts_every_literal_member(
    name: str,
    literal: object,
    build: Callable[[str], object],
    field: str,
) -> None:
    # The refusals for values OUTSIDE these sets are the structural tests below,
    # so this guard proves the sets are not narrower than the types they spell.
    # Reading the stored member back is what makes the guard bite: a builder that
    # quietly mapped two members onto one value would still "construct" each one.
    members = frozenset(get_args(literal))  # type: ignore[arg-type]
    assert members, f"{name} declares no members to accept"
    for member in sorted(members):
        assert getattr(build(member), field) == member, (
            f"{name} refused its own member {member!r}"
        )


# ----- HostFacts: one frozen carrier for every host-owned fact -----


def test_host_facts_carrier_is_frozen() -> None:
    facts = _host_facts()
    with pytest.raises(FrozenInstanceError):
        facts.basis_committed = False  # type: ignore[misc]


# Every host-owned fact, with the whole carrier minus one: a host fact a caller can
# forget is a host fact that silently stops narrowing, so each one must be named at
# construction. The guard below first pins this baseline against the carrier's own
# fields, so a newly declared host fact cannot join without a test for omitting it.
_HOST_FACT_BASELINE: Final[dict[str, object]] = {
    "admitted_evidence": frozenset(),
    "host_capabilities": {},
    "basis_committed": True,
    "committed_goal_part_ids": frozenset(),
    "observed_receipt_classes": frozenset(),
}


@pytest.mark.parametrize("omitted", sorted(_HOST_FACT_BASELINE))
def test_host_facts_refuses_a_caller_that_omits_any_one_fact(omitted: str) -> None:
    assert sorted(_HOST_FACT_BASELINE) == sorted(
        field.name for field in fields(HostFacts)
    )
    supplied = dict(_HOST_FACT_BASELINE)
    del supplied[omitted]

    with pytest.raises(TypeError) as raised:
        HostFacts(**supplied)  # type: ignore[arg-type]

    assert omitted in str(raised.value)


def test_empty_committed_goal_part_ids_reports_the_grant_as_uncertified() -> None:
    # D3 makes the decomposition optional, so an empty committed set neither refuses
    # a completion that meets every other precondition nor certifies whole-goal
    # coverage: the only part list was the candidate's, and the result says so.
    result = _validate(_assessment())

    assert result.host_facts.committed_goal_part_ids == frozenset()
    assert result.completion == "complete"
    assert result.goal_coverage == "candidate_declared"
    assert result.accepted is True


def test_committed_goal_parts_certify_the_granted_completion() -> None:
    # The same proposal, this time narrowed against a decomposition the host
    # committed and covered: only here is whole-goal coverage the host's own finding.
    result = _validate(
        _assessment(), committed_goal_part_ids=frozenset({"P1"})
    )

    assert result.completion == "complete"
    assert result.goal_coverage == "host_certified"
    assert result.accepted is True


def test_a_completion_that_was_not_granted_reports_no_coverage() -> None:
    # Coverage answers "whose list closed the goal", so a completion the host
    # narrowed or never granted has none to report.
    result = _validate(
        _assessment(completion="partial"),
        committed_goal_part_ids=frozenset({"P1"}),
    )

    assert result.completion == "partial"
    assert result.goal_coverage == "not_granted"


def test_validation_exposes_the_host_facts_it_was_narrowed_against() -> None:
    # A narrowed result is not self-explaining: Runtime reads the carrier it was
    # produced against, and it is the very one that was passed in.
    facts = _host_facts(committed_goal_part_ids=frozenset({"P1"}))

    result = validate_assessment(_assessment(), host_facts=facts)

    assert result.host_facts == facts
    assert result.host_facts.committed_goal_part_ids == frozenset({"P1"})


def test_record_does_not_store_host_facts() -> None:
    # Complement to the committed-basis guard: the whole carrier is a validation
    # input, so no part of it may arrive as a candidate field the model sends —
    # otherwise the model could commit its own goal decomposition.
    record = _rich_assessment().to_record()

    for field_name in (
        "admitted_evidence",
        "host_capabilities",
        "basis_committed",
        "committed_goal_part_ids",
        "observed_receipt_classes",
    ):
        assert field_name not in record


# ----- wire <-> schema conversion: one field-name authority -----


def test_record_covers_every_schema_field() -> None:
    # The silent-drop guard: the stored record is produced by reflection over
    # the dataclass fields, so every field appears at every nesting level. If a
    # hand-written serializer ever omits a field, this fails.
    record = _rich_assessment().to_record()
    claim = _obj(_seq(record["claims"])[0])
    ref = _obj(_seq(claim["support"])[0])
    part = _obj(_seq(record["goal_parts"])[0])
    check = _obj(_seq(record["proposed_checks"])[0])
    clarification = _obj(record["proposed_clarification"])

    assert set(record) == {f.name for f in fields(GroundedAssessment)}
    assert set(claim) == {f.name for f in fields(SupportedClaim)}
    assert set(ref) == {f.name for f in fields(EvidenceRef)}
    assert set(part) == {f.name for f in fields(GoalPart)}
    assert set(check) == {f.name for f in fields(ProposedCheck)}
    assert set(clarification) == {f.name for f in fields(ProposedClarification)}


def test_record_stores_nested_evidence_refs_as_kind_and_id() -> None:
    # Admission is the (kind, id) pair, so the pair must survive the record: a
    # ref flattened back to a bare id would lose its kind on reload.
    record = _rich_assessment().to_record()
    claim = _obj(_seq(record["claims"])[0])
    ref = _obj(_seq(claim["support"])[0])

    assert ref == {"kind": "company", "id": "doc-A#unit-1"}
    decoded = GroundedAssessment.from_payload(record)
    assert decoded.claims[0].support == (_evidence("doc-A#unit-1"),)


def test_payload_round_trips_to_record() -> None:
    assessment = _rich_assessment()
    decoded = GroundedAssessment.from_payload(assessment.to_record())
    assert decoded == assessment


_SCHEMA_DATACLASSES: Final[tuple[object, ...]] = (
    GroundedAssessment,
    SupportedClaim,
    EvidenceRef,
    GoalPart,
    ProposedCheck,
    ProposedClarification,
)


def _declared_field_paths(
    node: object, prefix: tuple[str | int, ...] = ()
) -> list[tuple[str | int, ...]]:
    """Every key a record carries, with the path the decoder walks to reach it.

    A key whose value is a nested object is recorded both for itself and for each
    key inside it, since an absent container and an absent member are both an
    incomplete payload.
    """
    if not isinstance(node, dict):
        raise AssertionError(f"the record must be a mapping, got {type(node).__name__}")
    found: list[tuple[str | int, ...]] = []
    for key, value in node.items():
        step = (*prefix, str(key))
        found.append(step)
        if isinstance(value, dict):
            found.extend(_declared_field_paths(value, step))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    found.extend(_declared_field_paths(item, (*step, index)))
    return found


# A declared field the payload omits, at every nesting level the schema declares one:
# the tuple is the path into the record, and the last step is the field name the
# refusal must name. Generated by walking the rich record rather than spelled out, so
# no declared field can join the contract without appearing in the matrix. The
# top-level ``failure`` case is the nullable-field instance of the same rule
# (``failure: null`` is a stated outcome, an absent ``failure`` is an incomplete
# payload).
_MISSING_FIELD_PATHS: Final[tuple[tuple[str | int, ...], ...]] = tuple(
    _declared_field_paths(_rich_assessment().to_record())
)


def test_missing_field_matrix_names_every_declared_field() -> None:
    # The generator is only as wide as the record it walks: if the rich assessment
    # ever stopped carrying one of these types, its fields would vanish from the
    # matrix silently, so the matrix must name every field every type declares.
    declared = {
        field.name
        for target in _SCHEMA_DATACLASSES
        for field in fields(target)  # type: ignore[arg-type]
    }
    assert declared - {path[-1] for path in _MISSING_FIELD_PATHS} == set()


def _drop_declared_field(record: dict[str, object], path: tuple[str | int, ...]) -> str:
    """Delete the leaf field ``path`` names and return its name."""
    node: Any = record
    for step in path[:-1]:
        node = node[step]
    leaf = path[-1]
    if not isinstance(leaf, str):
        raise AssertionError(f"the last path step must name a field, got {leaf!r}")
    del node[leaf]
    return leaf


@pytest.mark.parametrize(
    "path",
    _MISSING_FIELD_PATHS,
    ids=["-".join(str(step) for step in entry) for entry in _MISSING_FIELD_PATHS],
)
def test_from_payload_refuses_a_payload_missing_any_declared_field(
    path: tuple[str | int, ...],
) -> None:
    # ``SupportedClaim.premises`` and its sibling fields have no default: a model
    # that leaves one out is not declaring an empty value, it is sending an
    # incomplete record, and the host may not invent what it omitted.
    record = _rich_assessment().to_record()
    missing = _drop_declared_field(record, path)

    with pytest.raises(MalformedPayloadError) as raised:
        GroundedAssessment.from_payload(record)

    assert missing in str(raised.value)


def test_from_payload_refuses_a_string_where_a_sequence_is_required() -> None:
    # A bare string is iterable, so an unguarded parse would silently unpack a
    # single premise id into characters and return a plausible-looking payload.
    record = _rich_assessment().to_record()
    claims = cast(list[dict[str, object]], record["claims"])
    claims[1]["premises"] = "C1"
    with pytest.raises(MalformedPayloadError):
        GroundedAssessment.from_payload(record)


def test_from_payload_refuses_a_string_where_an_object_is_required() -> None:
    record = _rich_assessment().to_record()
    record["goal_parts"] = ["P1"]
    with pytest.raises(MalformedPayloadError):
        GroundedAssessment.from_payload(record)


def test_from_payload_ignores_keys_the_schema_does_not_declare() -> None:
    # The rebuild reads exactly the reflected field set, so a model that pads its
    # answer with a key nobody asked for has still sent a complete record. The
    # extra key must not reach the object, and must not come back out of it.
    record = _rich_assessment().to_record()
    record["prose"] = "a conclusion the host did not ask for"
    claims = cast(list[dict[str, object]], record["claims"])
    claims[0]["reasoning"] = "not a declared field"

    decoded = GroundedAssessment.from_payload(record)

    assert decoded == _rich_assessment()
    assert "prose" not in decoded.to_record()


def test_from_payload_accepts_the_record_it_produced() -> None:
    # The guard must reject malformed shapes without rejecting its own output.
    record = _rich_assessment().to_record()
    assert GroundedAssessment.from_payload(record) == _rich_assessment()


# ----- structural invariants (invalid input) -----


def test_evidence_ref_rejects_unknown_kind() -> None:
    with pytest.raises(MalformedEvidenceRefError):
        EvidenceRef(kind="spreadsheet", id="x")  # type: ignore[arg-type]


def test_evidence_ref_rejects_empty_id() -> None:
    with pytest.raises(MalformedEvidenceRefError):
        EvidenceRef(kind="company", id="   ")


def test_failure_outside_the_closed_set_raises() -> None:
    with pytest.raises(MalformedAssessmentError):
        _assessment(failure="generic_error", completion="partial")


def test_empty_claim_id_raises() -> None:
    with pytest.raises(MalformedClaimError):
        _claim("")


def test_empty_subject_raises() -> None:
    with pytest.raises(MalformedClaimError):
        _claim("C1", subject="")


def test_blank_subject_raises() -> None:
    with pytest.raises(MalformedClaimError):
        _claim("C1", subject="   ")


def test_empty_applicability_raises() -> None:
    with pytest.raises(MalformedClaimError):
        _claim("C1", applicability="")


def test_blank_applicability_raises() -> None:
    with pytest.raises(MalformedClaimError):
        _claim("C1", applicability="  ")


def test_claim_with_unknown_derivation_raises() -> None:
    with pytest.raises(MalformedClaimError):
        SupportedClaim(
            claim_id="C1",
            text="t",
            subject="s",
            derivation="guess",  # type: ignore[arg-type]
            support=_support("doc-A#unit-1"),
            premises=(),
            applicability="a",
        )


def test_empty_goal_part_id_raises() -> None:
    with pytest.raises(MalformedGoalPartError):
        _part("")


def test_goal_part_with_unknown_status_raises() -> None:
    with pytest.raises(MalformedGoalPartError):
        GoalPart(
            part_id="P1",
            description="d",
            status="maybe",  # type: ignore[arg-type]
            claim_ids=(),
            gap="",
            conflict="",
        )


def test_check_with_unknown_prospect_raises() -> None:
    with pytest.raises(MalformedCheckError):
        _check(prospect="almost")


def test_assessment_with_unknown_completion_raises() -> None:
    with pytest.raises(MalformedAssessmentError):
        _assessment(completion="finished")


def test_all_typed_errors_share_one_base() -> None:
    for error_type in (
        MalformedClaimError,
        MalformedGoalPartError,
        MalformedCheckError,
        MalformedEvidenceRefError,
        MalformedAssessmentError,
    ):
        assert issubclass(error_type, GroundedAssessmentError)
