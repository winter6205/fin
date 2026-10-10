"""Grounded assessment contract for the host agent (spec question-grounded-answer).

This module is the **pure** host-side validator and the single schema
authority for a model-produced grounded assessment. It narrows a proposal
against the facts the host already settled — the evidence it admitted this
run and the capabilities it reports — before anything is applied or
persisted.

Ownership rule. This feature module owns the assessment schema *and* its wire
conversion: :meth:`GroundedAssessment.from_payload` and
:meth:`GroundedAssessment.to_record` are driven off the dataclass field names by
reflection, so field names have exactly one authority and adding a field cannot
silently drop it from the payload parse or the stored record. Pure helpers live
here next to the schema, not in ``graph.py``.

Host integration seam (deferred, by design). The graph node, prompt block, and
state channel that would call :func:`validate_assessment` belong to Runtime,
whose shared assessment contract is not frozen yet. So this module has **no
caller yet** and holds **no authoritative task state**: the validator returns a
*proposal*; Runtime alone validates it against the current basis and host checks
and applies and persists the outcome.

No node-test seam is needed: the whole contract is exercised through the feature
boundary — call :func:`validate_assessment` /
:meth:`GroundedAssessment.from_payload` directly, as
``tests/host/test_citation_gate.py`` does for the citation verifier — with no
graph, model, or network setup.

The module performs **mechanical checks only**. It never judges semantic
support, never picks which source prevails in a conflict, and never promotes
a claim or a completion the model did not already justify. Every rule below
narrows or withholds; none strengthens. Every host-owned fact arrives as one
carrier, :class:`HostFacts` — the evidence it admitted, the capabilities it
reports, the committed basis, the goal parts it actually committed, and the
operation receipts it observed — never as a model-set candidate field: a model
cannot create a permission, a capability, a commitment, or a receipt by naming
one.
Semantic judgment stays with the model, and authoritative task state stays with
Runtime.

Rules R1..R7 mirror spec decisions D1..D8 at the granularity that is
decidable without semantics. A narrowing that changes what Runtime may
rely on records a stable string code (exposed as a module constant) so
consumers assert on the code, never on prose; a check or clarification
that simply does not qualify is dropped without a code — repeating an
exhausted direction without a new basis is not progress, not a
validation error (D5, D6).
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Final, Literal, Union, cast, get_args, get_origin, get_type_hints

# A locator id carrying this prefix names a **user-confirmed condition**,
# not admitted evidence. A user's confirmed situation may be a task
# condition but is never evidence for a policy fact (spec D2), so such an
# id in a claim's support set is a category error with its own exit.
USER_CONDITION_PREFIX: Final[str] = "user_condition:"

Derivation = Literal["source_statement", "calculation", "synthesis", "inference"]
PartStatus = Literal["supported", "unresolved"]
Prospect = Literal["reasonable", "unclear", "none"]
Completion = Literal["complete", "partial", "unresolved"]
# Who decided whole-goal coverage behind a granted ``complete``. D3 makes the goal
# decomposition optional, so a host that committed none cannot refuse a completion
# that meets every host-decidable precondition — and cannot certify coverage it
# never held either, so it says whose part list the grant rests on.
GoalCoverage = Literal["not_granted", "host_certified", "candidate_declared"]
# The closed evidence kinds a claim may cite are the ones already defined by
# the enterprise evidence contract (citation_gate.QUOTABLE_EVIDENCE_KINDS):
# a company/attachment locator and a database-row are DISTINCT kinds. A
# rot-guard test asserts this Literal equals that contract's set, so the two
# cannot drift; no second evidence vocabulary is introduced here.
EvidenceKind = Literal["company", "attachment", "database-row"]
# Successful lookup without support is kept distinct from an unsupported
# condition (spec D7): a tool that ran fine but found no support is not the
# same outcome as a permission, dependency, capability, or exhaustion failure.
FailureClass = Literal[
    "unsupported",
    "successful_empty",
    "permission_denied",
    "dependency_failed",
    "capability_unavailable",
    "evidence_exhausted",
]

# The closed sets the Literal types spell, derived FROM the types so a member
# added to a Literal is a member of the runtime check on the same edit. A
# hand-spelled copy here would silently make the validator reject input the
# annotation allows; ``test_grounded_assessment_schema.py`` guards every
# vocabulary by constructing each Literal member at the schema boundary, which
# is where such a stale copy would show up.
_DERIVATIONS: Final[frozenset[str]] = frozenset(get_args(Derivation))
_PART_STATUSES: Final[frozenset[str]] = frozenset(get_args(PartStatus))
_PROSPECTS: Final[frozenset[str]] = frozenset(get_args(Prospect))
_COMPLETIONS: Final[frozenset[str]] = frozenset(get_args(Completion))
_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(get_args(EvidenceKind))
_FAILURE_CLASSES: Final[frozenset[str]] = frozenset(get_args(FailureClass))

# The failure vocabulary splits by WHO can establish the fact, and the split is
# this module's single authority (R6 / D7):
#
# * **receipt-shaped** — the truth of the claim is an outcome of a host
#   operation, the same kind of fact an operation receipt carries: a permission
#   the host enforced, a dependency that errored, a capability the host does not
#   currently offer, a recovery the host ran out of, or a lookup that ran and
#   legitimately found nothing. The model cannot know these; only the host that
#   ran (or declined to run) the operation can.
# * **judgment-shaped** — ``unsupported``: the conclusion the available evidence
#   does not carry. That IS the semantic judgment the model owns (D1), so the
#   host never gates it against a receipt.
RECEIPT_SHAPED_FAILURE_CLASSES: Final[frozenset[FailureClass]] = frozenset(
    {
        "permission_denied",
        "dependency_failed",
        "capability_unavailable",
        "evidence_exhausted",
        "successful_empty",
    }
)

# ----- stable validation error codes (assert on these, never on prose) -----

# R1 (D1): support is specific.
CODE_CLAIM_NO_SUPPORT_LOCATOR: Final[str] = "claim_no_support_locator"
CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR: Final[str] = "claim_unknown_support_locator"
# The host admitted that id, but as a different kind of evidence than the one
# the claim cites. Distinct from an unknown id: the citation names a real
# document while claiming it is something else (admitted evidence of
# the correct kind).
CODE_CLAIM_WRONG_EVIDENCE_KIND: Final[str] = "claim_wrong_evidence_kind"
CODE_CLAIM_USER_CONDITION_NOT_EVIDENCE: Final[str] = (
    "claim_user_condition_not_evidence"
)
# R2 (D2): source and derivation stay distinct.
CODE_CLAIM_MISSING_PREMISES: Final[str] = "claim_missing_premises"
CODE_CLAIM_UNKNOWN_PREMISE: Final[str] = "claim_unknown_premise"
# R3 (D3): assess the whole goal.
CODE_GOAL_PARTS_EMPTY: Final[str] = "goal_parts_empty"
CODE_GOAL_PART_MISSING_GAP: Final[str] = "goal_part_missing_gap"
CODE_GOAL_PART_UNSUPPORTED_CLAIMS: Final[str] = "goal_part_unsupported_claims"
# A goal part the host committed for this task that no surviving ``supported``
# part covers. Distinct from ``completion_unsupported``: the candidate narrowed
# the goal itself, so the generated task list — not the committed decomposition —
# would have decided coverage (D3).
CODE_COMMITTED_GOAL_PART_UNCOVERED: Final[str] = "committed_goal_part_uncovered"
# R6 (D7): a receipt-shaped failure the host did not observe this run.
CODE_FAILURE_RECEIPT_NOT_OBSERVED: Final[str] = "failure_receipt_not_observed"
# R7 (D8): separate task completion from turn ending.
CODE_COMPLETION_UNSUPPORTED: Final[str] = "completion_unsupported"


# ----- typed errors (structural invariants only) -----


class GroundedAssessmentError(Exception):
    """Base class for grounded-assessment typed errors.

    Raised only for a **structural** invariant violation in the input
    dataclasses (an empty id, a value outside a closed set). Semantic
    problems — unsupported claims, missing premises, an overclaimed
    completion — are never raised: they are recorded as error codes on
    an :class:`AssessmentValidation` so Runtime can read the narrowed
    result instead of catching.
    """


class MalformedEvidenceRefError(GroundedAssessmentError):
    """An :class:`EvidenceRef` violates a structural invariant."""

    def __init__(self, message: str, *, ref_id: str | None = None) -> None:
        super().__init__(message)
        self.ref_id = ref_id


class MalformedPayloadError(GroundedAssessmentError):
    """A wire payload value has a shape the schema does not allow.

    Raised at the :meth:`GroundedAssessment.from_payload` boundary, where the
    payload comes from a model and a wrong shape must be refused rather than
    coerced into something plausible.
    """


class MalformedClaimError(GroundedAssessmentError):
    """A :class:`SupportedClaim` violates a structural invariant."""

    def __init__(self, message: str, *, claim_id: str | None = None) -> None:
        super().__init__(message)
        self.claim_id = claim_id


class MalformedGoalPartError(GroundedAssessmentError):
    """A :class:`GoalPart` violates a structural invariant."""

    def __init__(self, message: str, *, part_id: str | None = None) -> None:
        super().__init__(message)
        self.part_id = part_id


class MalformedCheckError(GroundedAssessmentError):
    """A :class:`ProposedCheck` violates a structural invariant."""


class MalformedAssessmentError(GroundedAssessmentError):
    """A :class:`GroundedAssessment` violates a structural invariant."""


# ----- schema (frozen dataclasses) -----


@dataclass(frozen=True)
class EvidenceRef:
    """A reference to one admitted piece of evidence, by its contract kind.

    ``kind`` is one of the existing evidence-contract kinds and ``id`` is
    the identity the host admits for that kind: a company/attachment
    locator carries a document locator id, while a ``database-row`` carries
    its own record identity — the two are kept distinct rather than folding
    a row into a locator. ``__post_init__`` rejects an empty id
    or a kind outside the closed set as a structural error; whether the id
    was actually admitted is R1's semantic check, not a construction error.
    """

    kind: EvidenceKind
    id: str

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise MalformedEvidenceRefError(
                "evidence ref id must be non-empty", ref_id=self.id
            )
        if self.kind not in _EVIDENCE_KINDS:
            raise MalformedEvidenceRefError(
                f"kind {self.kind!r} is outside {sorted(_EVIDENCE_KINDS)}",
                ref_id=self.id,
            )


@dataclass(frozen=True)
class SupportedClaim:
    """One material conclusion the model asks the host to accept.

    ``support`` is the set of evidence references the model cites as support
    **this run**; the validator keeps the claim only when every cited id is
    an admitted evidence reference of a valid kind (R1). ``premises`` names
    the claims a derived conclusion rests on; the host never infers the
    derivation from them (R2). ``applicability`` states who / what / when the
    claim covers.
    """

    claim_id: str
    text: str
    subject: str
    derivation: Derivation
    support: tuple[EvidenceRef, ...]
    premises: tuple[str, ...]
    applicability: str

    def __post_init__(self) -> None:
        if not self.claim_id or not self.claim_id.strip():
            raise MalformedClaimError(
                "claim_id must be non-empty", claim_id=self.claim_id
            )
        # D1 names subject and applicability as required support dimensions;
        # a claim that states neither cannot match evidence for the asked
        # relationship, so carrying them as always-empty fields would let a
        # validator that ignores them pass. Enforced structurally here
        # because it is decidable without semantics.
        if not self.subject or not self.subject.strip():
            raise MalformedClaimError(
                "subject must be non-empty", claim_id=self.claim_id
            )
        if not self.applicability or not self.applicability.strip():
            raise MalformedClaimError(
                "applicability must be non-empty", claim_id=self.claim_id
            )
        if self.derivation not in _DERIVATIONS:
            raise MalformedClaimError(
                f"derivation {self.derivation!r} is outside "
                f"{sorted(_DERIVATIONS)}",
                claim_id=self.claim_id,
            )


@dataclass(frozen=True)
class GoalPart:
    """One material part of the user's requested goal.

    ``gap`` names what is missing when the part is ``unresolved``. A
    non-empty ``conflict`` forces the part to ``unresolved`` and is
    preserved verbatim — the host never resolves a conflict (D3/D4).
    """

    part_id: str
    description: str
    status: PartStatus
    claim_ids: tuple[str, ...]
    gap: str
    conflict: str

    def __post_init__(self) -> None:
        if not self.part_id or not self.part_id.strip():
            raise MalformedGoalPartError(
                "part_id must be non-empty", part_id=self.part_id
            )
        if self.status not in _PART_STATUSES:
            raise MalformedGoalPartError(
                f"status {self.status!r} is outside {sorted(_PART_STATUSES)}",
                part_id=self.part_id,
            )


@dataclass(frozen=True)
class CapabilityState:
    """A host-reported fact about one capability: whether it is permitted and
    currently available. Supplied to :func:`validate_assessment` by the host;
    never a model-set candidate field."""

    permitted: bool
    available: bool


@dataclass(frozen=True)
class HostFacts:
    """Every host-owned fact one validation is narrowed against — one carrier.

    This is the single input :func:`validate_assessment` reads, so a host fact
    cannot be added to the contract and then forgotten by one caller: each caller
    supplies the whole carrier instead of inventing its own bundle. Nothing here
    is a model-asserted field; the candidate's own flags are what these narrow.

    * ``admitted_evidence`` — the ``(kind, id)`` pairs the host admitted for the
      current task and source scope (R1).
    * ``host_capabilities`` — per capability, whether the host currently permits
      it and has it available (R4).
    * ``basis_committed`` — whether the task / source-scope basis was committed
      before this assessment (R7).
    * ``committed_goal_part_ids`` — the goal-part identities the host actually
      committed for this task, as part ids. Coverage is measured over these, not
      over whatever parts the model happened to declare (R3, D3).
      **An empty set means the host committed no decomposition at all** — the
      legal state D3 calls optional, never "the goal is covered". With no
      committed list the host cannot check coverage, so a ``complete`` that meets
      every other precondition is still granted, and ``goal_coverage`` reports
      ``"candidate_declared"``: the only part list was the candidate's, which D3
      refuses as the goal's authority. A caller cannot leave this fact out; the
      field has no default, because a forgettable host fact is a silent one.
    * ``observed_receipt_classes`` — the receipt-shaped failure classes
      (:data:`RECEIPT_SHAPED_FAILURE_CLASSES`) the host observed this run. A
      receipt-shaped failure the model declares that is absent here is a fact
      about an operation the host did not report, so it is withheld (R6).
      Judgment-shaped classes — ``unsupported`` — are never listed here, because
      their truth is the model's judgment, not a receipt.
    """

    admitted_evidence: frozenset[EvidenceRef]
    host_capabilities: Mapping[str, CapabilityState]
    basis_committed: bool
    committed_goal_part_ids: frozenset[str]
    observed_receipt_classes: frozenset[FailureClass]


@dataclass(frozen=True)
class ProposedCheck:
    """A proposed next lookup the model wants to run against a named gap.

    The model names the ``gap`` and the ``capability`` it wants and its
    ``prospect``. Whether that capability is actually permitted and available
    is a host fact supplied at validation, not something the candidate can
    assert by setting a field.
    """

    gap: str
    capability: str
    prospect: Prospect

    def __post_init__(self) -> None:
        if self.prospect not in _PROSPECTS:
            raise MalformedCheckError(
                f"prospect {self.prospect!r} is outside {sorted(_PROSPECTS)}"
            )


@dataclass(frozen=True)
class ProposedClarification:
    """A proposed question for user-held information that can change the result."""

    condition: str
    changes: str
    choices: tuple[str, ...]


@dataclass(frozen=True)
class GroundedAssessment:
    """The model's proposed grounded assessment, before host validation.

    ``completion`` is the model's proposed task disposition; Runtime alone
    applies and persists it, and the committed-basis precondition comes from
    the host at validation (:func:`validate_assessment`), never from a field
    here — an absent or stale basis cannot be made to look committed by the
    model.
    """

    claims: tuple[SupportedClaim, ...]
    goal_parts: tuple[GoalPart, ...]
    proposed_checks: tuple[ProposedCheck, ...]
    proposed_clarification: ProposedClarification | None
    failure: FailureClass | None
    completion: Completion

    def __post_init__(self) -> None:
        # At most one failure value is structural in this schema (one
        # optional slot); the closed set is the vocabulary, so a value
        # outside it is rejected here rather than narrowed (R6).
        if self.failure is not None and self.failure not in _FAILURE_CLASSES:
            raise MalformedAssessmentError(
                f"failure {self.failure!r} is outside {sorted(_FAILURE_CLASSES)}"
            )
        if self.completion not in _COMPLETIONS:
            raise MalformedAssessmentError(
                f"completion {self.completion!r} is outside "
                f"{sorted(_COMPLETIONS)}"
            )

    def to_record(self) -> dict[str, object]:
        """Serialize to the stored record, field names taken from this schema.

        The record is produced by reflection over :func:`dataclasses.fields`,
        so every field is present and no separate serializer restates a field
        name it could forget.
        """
        record = _encode(self)
        assert isinstance(record, dict)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> GroundedAssessment:
        """Parse a model wire payload into the schema, driven by field names.

        Uses the same reflected field set as :meth:`to_record`, so a field
        added to the dataclass is read here too; a payload missing any field
        raises :class:`MalformedPayloadError` naming the field rather than
        silently defaulting it away.
        """
        return cast(GroundedAssessment, _decode_dataclass(cls, payload))


@dataclass(frozen=True)
class AssessmentValidation:
    """The host-validated assessment returned to Runtime.

    ``accepted`` is True when no error code was recorded. The narrowed
    fields carry only what the host accepted: ``supported_claims`` are
    the claims that survived R1/R2, ``goal_parts`` the parts that
    survived R3, ``proposed_checks`` only the checks that qualify, and
    ``completion`` is narrowed (never promoted) from the proposal.

    ``host_facts`` is the carrier this result was narrowed against, echoed so
    Runtime can see which admitted evidence, capability report, committed basis,
    committed goal decomposition, and observed receipts the outcome rests on —
    a result is not self-explaining without them. It carries no basis identity:
    a stale-basis guard needs a basis pinned by the shared contract freeze, and
    inventing an identifier here would be a second authority for one. Until that
    freeze exists, ``basis_committed`` is the only basis fact this module can
    honour.

    ``goal_coverage`` says whose part list a granted ``complete`` rests on, and is
    ``"not_granted"`` for any other completion. It is a report, not an error: an
    uncommitted decomposition is the optional state D3 allows, so it neither
    promotes a completion nor blocks one, and it never reads as certified coverage.
    """

    accepted: bool
    errors: tuple[str, ...]
    supported_claims: tuple[SupportedClaim, ...]
    goal_parts: tuple[GoalPart, ...]
    proposed_checks: tuple[ProposedCheck, ...]
    proposed_clarification: ProposedClarification | None
    failure: FailureClass | None
    completion: Completion
    goal_coverage: GoalCoverage
    host_facts: HostFacts


# ----- wire <-> schema codec (single field-name authority) -----


def _encode(value: object) -> object:
    """Recursively turn schema objects into JSON-shaped values by field name."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    return value


def _decode_dataclass(target: type, data: Mapping[str, object]) -> object:
    """Rebuild one schema object, reading exactly its reflected field set.

    A field the schema declares but the payload omits is refused by name, not
    guessed: the host may not invent a value the model did not send, and a
    bare ``KeyError`` would leak past the typed wire boundary while hiding
    which field was absent. Naming the missing field(s) keeps the refusal a
    ``MalformedPayloadError`` at every nesting depth, because this is the
    single rebuild point.

    Keys the schema does not declare are ignored rather than refused: a model that
    pads its answer with a field nobody asked for has still sent a complete record,
    and the host reads exactly the reflected field set, so the extra key cannot
    reach the rebuilt object, the validation result, or the record.
    """
    if not isinstance(data, Mapping):
        raise MalformedPayloadError(
            f"expected an object for {target.__name__}, got {type(data).__name__}"
        )
    missing = sorted(f.name for f in fields(target) if f.name not in data)
    if missing:
        raise MalformedPayloadError(
            f"{target.__name__} payload is missing field(s): {', '.join(missing)}"
        )
    hints = get_type_hints(target)
    kwargs = {f.name: _decode(hints[f.name], data[f.name]) for f in fields(target)}
    return target(**kwargs)


def _decode_optional(args: tuple[object, ...], value: object) -> object:
    """Decode an ``X | None`` annotation: ``None`` stays ``None``."""
    if value is None:
        return None
    return _decode(next(a for a in args if a is not types.NoneType), value)


def _decode_tuple(element_type: object, value: object) -> object:
    """Decode a ``tuple[T, ...]`` annotation into a tuple of coerced items.

    A bare string is iterable, so an unguarded comprehension would silently
    unpack ``"j09-c0"`` into single characters and hand back a plausible-looking
    payload. The wire must name a sequence; anything else is a structural
    rejection, not a coercion.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise MalformedPayloadError(
            f"expected a sequence, got {type(value).__name__}"
        )
    return tuple(_decode(element_type, item) for item in value)


def _decode(annotation: object, value: object) -> object:
    """Coerce a wire value back to ``annotation``'s schema type."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return _decode_optional(get_args(annotation), value)
    if origin is tuple:
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        if not args:
            # Every wire annotation names its element type. Defaulting to ``str``
            # here would silently coerce a payload the host cannot type-check,
            # so an annotation that names nothing is a refusal, not a fallback.
            raise MalformedPayloadError("tuple annotation names no element type")
        return _decode_tuple(args[0], value)
    if is_dataclass(annotation):
        return _decode_dataclass(annotation, value)  # type: ignore[arg-type]
    return value


# ----- R1 / R2: claim support and derivation -----


def _claim_survives_support(
    claim: SupportedClaim,
    admitted: frozenset[EvidenceRef],
    errors: list[str],
) -> bool:
    """R1 (D1): keep the claim only when its cites are all admitted evidence.

    A cited id that is a user-confirmed condition is a category error
    and gets its own code (D2). Any unknown / absent id is a support
    claim the host cannot honor, so the claim is withheld with an error
    rather than kept on partial support (D1: narrow or withhold).

    Admission is the ``(kind, id)`` pair, not the id alone: an id the host
    admitted as a company document is not thereby admitted as a
    ``database-row``, so a claim that keeps the id while changing its kind is
    withheld under its own code. That is what makes the two evidence classes
    distinct rather than merely labelled.
    """
    if any(ref.id.startswith(USER_CONDITION_PREFIX) for ref in claim.support):
        errors.append(CODE_CLAIM_USER_CONDITION_NOT_EVIDENCE)
        return False
    if not claim.support:
        errors.append(CODE_CLAIM_NO_SUPPORT_LOCATOR)
        return False
    admitted_ids = {ref.id for ref in admitted}
    if any(ref.id not in admitted_ids for ref in claim.support):
        errors.append(CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR)
        return False
    if any(ref not in admitted for ref in claim.support):
        errors.append(CODE_CLAIM_WRONG_EVIDENCE_KIND)
        return False
    return True


def _claim_survives_derivation(
    claim: SupportedClaim, declared_claim_ids: frozenset[str], errors: list[str]
) -> bool:
    """R2 (D2): a derived claim must name its premises; derivation is never inferred.

    A premise that names no declared claim id leaves the derivation's
    support chain broken, so the claim is withheld. Whether a declared
    premise's own claim survives is that claim's separate R1/R2
    judgment — a withheld premise is recorded on the premise claim, not
    re-penalised here as unknown.
    """
    if claim.derivation == "source_statement":
        return True
    if not claim.premises:
        errors.append(CODE_CLAIM_MISSING_PREMISES)
        return False
    if any(premise not in declared_claim_ids for premise in claim.premises):
        errors.append(CODE_CLAIM_UNKNOWN_PREMISE)
        return False
    return True


# ----- R3: whole-goal coverage and conflicts -----


def _reports_limitation(part: GoalPart) -> bool:
    """Whether the part itself names what it is missing, as a conflict or a gap.

    This is the only question that can keep a non-supported part in the report:
    a part naming neither has nothing left for the host to say about it.
    """
    return bool(part.conflict.strip()) or bool(part.gap.strip())


def _narrow_supported_part(
    part: GoalPart, surviving_refs: tuple[str, ...], errors: list[str]
) -> GoalPart | None:
    """R3 for a part the candidate declared ``supported``.

    Surviving references and no conflict is the only way to keep ``supported``;
    otherwise the part is narrowed to ``unresolved`` and kept only if it names a
    conflict or a gap. Naming a conflict is not an overclaim — the part already
    reported the disagreement, so losing support on top of it records no code,
    or honest reporting would refuse the assessment it was meant to surface.
    """
    reports_conflict = bool(part.conflict.strip())
    if not surviving_refs and not reports_conflict:
        errors.append(CODE_GOAL_PART_UNSUPPORTED_CLAIMS)
        if not part.gap.strip():
            return None
    if surviving_refs and not reports_conflict:
        return replace(part, status="supported", claim_ids=surviving_refs)
    if not _reports_limitation(part):
        errors.append(CODE_GOAL_PART_MISSING_GAP)
        return None
    return replace(part, status="unresolved", claim_ids=surviving_refs)


def _validate_goal_part(
    part: GoalPart,
    surviving_claim_ids: frozenset[str],
    errors: list[str],
) -> GoalPart | None:
    """R3 (D3): validate one goal part. Returns it narrowed, or ``None`` dropped.

    References are reduced to the claims that survived R1/R2, then
    ``_narrow_supported_part`` decides a declared-supported part; a part already
    declared ``unresolved`` is kept exactly when it names its own limitation.
    """
    surviving_refs = tuple(
        cid for cid in part.claim_ids if cid in surviving_claim_ids
    )
    if part.status == "supported":
        return _narrow_supported_part(part, surviving_refs, errors)
    if not _reports_limitation(part):
        errors.append(CODE_GOAL_PART_MISSING_GAP)
        return None
    return replace(part, status="unresolved", claim_ids=surviving_refs)


# ----- R4 / R5: useful continuation and actionable clarification -----


def _check_qualifies(
    check: ProposedCheck, host_capabilities: Mapping[str, CapabilityState]
) -> bool:
    """R4 (D5): a check qualifies only for a named gap, a host-permitted and
    host-available capability, and a reasonable prospect of useful evidence.

    Permission and availability come from ``host_capabilities``, never from
    the candidate — naming a capability does not grant it. A
    capability the host does not report as permitted and available is dropped.
    """
    state = host_capabilities.get(check.capability)
    return (
        bool(check.gap.strip())
        and state is not None
        and state.permitted
        and state.available
        and check.prospect == "reasonable"
    )


def _qualify_clarification(
    clarification: ProposedClarification | None,
) -> ProposedClarification | None:
    """R5 (D6): a clarification is kept only when it names both the
    condition it resolves and the result it changes."""
    if clarification is None:
        return None
    if not clarification.condition.strip() or not clarification.changes.strip():
        return None
    return clarification


# ----- R6: a receipt-shaped failure needs the host's receipt (D7) -----


def _narrow_failure(
    declared: FailureClass | None,
    *,
    observed: frozenset[FailureClass],
    errors: list[str],
) -> tuple[FailureClass | None, bool]:
    """R6 (D7): keep a receipt-shaped failure only when the host observed it.

    Returns the narrowed failure and whether an unobserved receipt had to be
    withheld. The caller holds the completion gate on that second answer: a
    receipt the host did not observe is not an established condition, so it must
    not read as "no failure" and open the gate either. A receipt-shaped failure
    is recorded, never silently dropped.

    A judgment-shaped class (``unsupported``) passes through untouched: its truth
    is the model's semantic judgment of the evidence, which no host receipt can
    confirm or refute.
    """
    if declared is None or declared not in RECEIPT_SHAPED_FAILURE_CLASSES:
        return declared, False
    if declared in observed:
        return declared, False
    errors.append(CODE_FAILURE_RECEIPT_NOT_OBSERVED)
    return None, True


# ----- R7: completion -----


def _complete_is_grounded(
    *,
    basis_committed: bool,
    failure: FailureClass | None,
    every_goal_part_supported: bool,
    uncovered_committed_parts: frozenset[str],
    receipt_withheld: bool,
) -> bool:
    """Every host-decidable precondition a ``complete`` proposal must meet."""
    return (
        basis_committed
        and every_goal_part_supported
        and failure is None
        and not uncovered_committed_parts
        and not receipt_withheld
    )


def _resolve_completion(
    declared: Completion,
    *,
    basis_committed: bool,
    failure: FailureClass | None,
    has_surviving_claims: bool,
    every_goal_part_supported: bool,
    uncovered_committed_parts: frozenset[str],
    receipt_withheld: bool,
    errors: list[str],
) -> Completion:
    """R7 (D8): accept ``complete`` only on the grounded preconditions.

    A ``complete`` proposal that fails them is an overclaim: an error is
    recorded (never a silent narrowing) and the completion is narrowed.
    An honest ``partial`` / ``unresolved`` proposal is never promoted —
    it only narrows to ``unresolved`` when nothing survives.

    When a failed ``complete`` still has surviving supported findings,
    the narrowed value is ``partial``: whether a part is unresolved or
    the basis / failure precondition is what failed, the task is not
    grounded-complete.
    """
    if declared == "complete":
        if _complete_is_grounded(
            basis_committed=basis_committed,
            failure=failure,
            every_goal_part_supported=every_goal_part_supported,
            uncovered_committed_parts=uncovered_committed_parts,
            receipt_withheld=receipt_withheld,
        ):
            return "complete"
        if uncovered_committed_parts:
            errors.append(CODE_COMMITTED_GOAL_PART_UNCOVERED)
        errors.append(CODE_COMPLETION_UNSUPPORTED)
        if not has_surviving_claims:
            return "unresolved"
        return "partial"
    if not has_surviving_claims:
        return "unresolved"
    # An honest partial / unresolved is never promoted; keep it as declared.
    return declared


def _goal_coverage(
    completion: Completion, *, host_committed_parts: bool
) -> GoalCoverage:
    """Whose part list a granted ``complete`` rests on.

    Meeting the preconditions is not the same as being host-certified. D3 makes
    the decomposition optional, so the host has no basis to *refuse* a complete
    that meets every host-decidable precondition when it committed no parts —
    and no authority to *certify* whole-goal coverage either, since the only part
    list was the candidate's own. Reporting the difference is what keeps an empty
    committed set from reading as "the goal is covered"; deciding it belongs to
    the host that holds the current basis (D3, R7).
    """
    if completion != "complete":
        return "not_granted"
    return "host_certified" if host_committed_parts else "candidate_declared"


# ----- the validator -----


def _surviving_claims(
    assessment: GroundedAssessment,
    admitted: frozenset[EvidenceRef],
    errors: list[str],
) -> list[SupportedClaim]:
    """R1 / R2: keep only the claims whose support and derivation hold."""
    declared_claim_ids = frozenset(claim.claim_id for claim in assessment.claims)
    supported_claims: list[SupportedClaim] = []
    for claim in assessment.claims:
        if not _claim_survives_support(claim, admitted, errors):
            continue
        if not _claim_survives_derivation(claim, declared_claim_ids, errors):
            continue
        supported_claims.append(claim)
    return supported_claims


def _validated_parts(
    assessment: GroundedAssessment,
    surviving_claim_ids: frozenset[str],
    errors: list[str],
) -> tuple[list[GoalPart], list[PartStatus | None]]:
    """R3: validate every declared part and track each part's effective status.

    The effective status of EVERY declared part is tracked (``None`` for a
    dropped part) so completion can require coverage of the whole goal, not
    only of the parts that happened to survive.
    """
    if not assessment.goal_parts:
        errors.append(CODE_GOAL_PARTS_EMPTY)
    goal_parts: list[GoalPart] = []
    effective_statuses: list[PartStatus | None] = []
    for part in assessment.goal_parts:
        validated = _validate_goal_part(part, surviving_claim_ids, errors)
        effective_statuses.append(None if validated is None else validated.status)
        if validated is not None:
            goal_parts.append(validated)
    return goal_parts, effective_statuses


def _uncovered_committed_parts(
    committed: frozenset[str], supported_part_ids: frozenset[str]
) -> frozenset[str]:
    """R3 (D3): committed goal parts no surviving ``supported`` part covers.

    An empty committed set means the host committed no decomposition, so nothing
    beyond the candidate's own declared parts can be checked; the resulting empty
    hole must never be read as "the goal is covered". The answer only ever
    subtracts from what ``complete`` may be granted: a committed id no part
    declares is reported as uncovered, never added to the result.
    """
    return committed - supported_part_ids


def validate_assessment(
    assessment: GroundedAssessment,
    *,
    host_facts: HostFacts,
) -> AssessmentValidation:
    """Narrow a model proposal against the host's settled facts.

    ``host_facts`` is the one carrier of the host-owned inputs: the evidence the
    host admitted, the capabilities it reports, whether the basis was committed,
    the goal parts it committed for this task, and the receipts it observed. All
    of them are read here, never model-asserted candidate fields.

    Returns an :class:`AssessmentValidation` — a *proposal*, whose narrowed
    fields carry only what the host accepted. The function never raises for a
    semantic problem (those become error codes), never mutates its input (the
    schema is frozen), and holds no task state: Runtime alone validates against
    the current basis and applies and persists the outcome.
    """
    admitted = host_facts.admitted_evidence
    errors: list[str] = []

    # R1 / R2: claims.
    supported_claims = _surviving_claims(assessment, admitted, errors)
    surviving_claim_ids = frozenset(c.claim_id for c in supported_claims)

    # R3: goal parts.
    goal_parts, effective_statuses = _validated_parts(
        assessment, surviving_claim_ids, errors
    )
    # R3 (D3): whole-goal coverage is measured over the parts the HOST committed
    # for this task, not over the list the candidate happened to declare.
    supported_part_ids = frozenset(
        part.part_id for part in goal_parts if part.status == "supported"
    )
    uncovered_committed_parts = _uncovered_committed_parts(
        host_facts.committed_goal_part_ids, supported_part_ids
    )

    # R4: only the checks the host can actually run; the rest drop silently.
    proposed_checks = tuple(
        check
        for check in assessment.proposed_checks
        if _check_qualifies(check, host_facts.host_capabilities)
    )

    # R5: only an actionable clarification survives (D6).
    clarification = _qualify_clarification(assessment.proposed_clarification)

    # R6 (D7): a receipt-shaped failure needs a host receipt; a judgment stands.
    failure, receipt_withheld = _narrow_failure(
        assessment.failure,
        observed=host_facts.observed_receipt_classes,
        errors=errors,
    )

    # R7: completion, narrowed (never promoted); the basis is the host's fact.
    completion = _resolve_completion(
        assessment.completion,
        basis_committed=host_facts.basis_committed,
        failure=failure,
        has_surviving_claims=bool(supported_claims),
        every_goal_part_supported=bool(effective_statuses)
        and all(status == "supported" for status in effective_statuses),
        uncovered_committed_parts=uncovered_committed_parts,
        receipt_withheld=receipt_withheld,
        errors=errors,
    )

    return AssessmentValidation(
        accepted=not errors,
        errors=tuple(errors),
        supported_claims=tuple(supported_claims),
        goal_parts=tuple(goal_parts),
        proposed_checks=proposed_checks,
        proposed_clarification=clarification,
        failure=failure,
        completion=completion,
        goal_coverage=_goal_coverage(
            completion, host_committed_parts=bool(host_facts.committed_goal_part_ids)
        ),
        host_facts=host_facts,
    )


__all__ = [
    "CODE_CLAIM_MISSING_PREMISES",
    "CODE_CLAIM_NO_SUPPORT_LOCATOR",
    "CODE_CLAIM_UNKNOWN_PREMISE",
    "CODE_CLAIM_UNKNOWN_SUPPORT_LOCATOR",
    "CODE_CLAIM_USER_CONDITION_NOT_EVIDENCE",
    "CODE_CLAIM_WRONG_EVIDENCE_KIND",
    "CODE_COMPLETION_UNSUPPORTED",
    "CODE_COMMITTED_GOAL_PART_UNCOVERED",
    "CODE_FAILURE_RECEIPT_NOT_OBSERVED",
    "CODE_GOAL_PARTS_EMPTY",
    "CODE_GOAL_PART_MISSING_GAP",
    "CODE_GOAL_PART_UNSUPPORTED_CLAIMS",
    "RECEIPT_SHAPED_FAILURE_CLASSES",
    "USER_CONDITION_PREFIX",
    "AssessmentValidation",
    "CapabilityState",
    "Completion",
    "Derivation",
    "EvidenceKind",
    "EvidenceRef",
    "FailureClass",
    "GoalCoverage",
    "GoalPart",
    "GroundedAssessment",
    "GroundedAssessmentError",
    "HostFacts",
    "MalformedAssessmentError",
    "MalformedCheckError",
    "MalformedClaimError",
    "MalformedEvidenceRefError",
    "MalformedGoalPartError",
    "MalformedPayloadError",
    "PartStatus",
    "ProposedCheck",
    "ProposedClarification",
    "Prospect",
    "SupportedClaim",
    "validate_assessment",
]
