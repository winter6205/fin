"""``fin.host.source_scope`` — the resolved selection and its enforcement.

Cases follow the behavior classes in ``.claude/rules/test.md`` against the
pure resolver and the gate, with expected values derived from
``specs/question-source-scope.md`` and ADR 0088 (not from the implementation):

* normal: default resolves to current ready attachments; a restriction narrows;
* failure: a resolver that raises contributes nothing and is ``dependency_failed``;
* boundary: blank thread; authority gone; version mismatch; not-ready;
* insufficient permission: unknown id; no host-bound thread;
* empty/invalid: empty restriction tuple; blank ids;
* inheritance and the decision-4 regression: a saved restriction is not a grant.

``ScopeGate.DENIAL_CODES`` is exercised for the closed set: every outcome is
reachable and the denial codes are pairwise distinct.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from fin.host.source_scope import (
    ATTACHMENT_SCOPED_OPERATIONS,
    COMPANY_CAPABILITY,
    KNOWN_LIMIT_CODES,
    SCOPE_ATTACHMENT_NOT_PERMITTED,
    SCOPE_ATTACHMENT_NOT_READY,
    SCOPE_ATTACHMENT_UNKNOWN,
    SCOPE_ATTACHMENT_VERSION_STALE,
    SCOPE_COMPANY_NOT_PERMITTED,
    SCOPE_DEPENDENCY_FAILED,
    SCOPE_OPERATION_UNSUPPORTED,
    SCOPE_PERMITTED,
    SCOPE_SELECTION_EMPTY,
    SCOPE_SOURCE_REF_UNREADABLE,
    SCOPE_THREAD_UNBOUND,
    AttachmentAuthority,
    AuthorityFacts,
    EffectiveScope,
    KnownLimit,
    ScopeDecision,
    ScopeGate,
    ScopeProposal,
    ScopeReport,
    ScopeRestriction,
    propose_scope_change,
    resolve_scope,
    scope_denied_error,
)


def _facts(
    *,
    thread_id: str = "thread-1",
    authorities: dict[str, int] | None = None,
    ready_ids: set[str] | None = None,
    listed: tuple[str, ...] | None = None,
    capable: bool = True,
    resolve_raises: frozenset[str] = frozenset(),
    ready_raises: frozenset[str] = frozenset(),
    capable_raises: bool = False,
    list_raises: bool = False,
) -> AuthorityFacts:
    """A host fact set over an in-memory authority table.

    ``authorities`` maps an id to its current parsed-content version (an absent
    id has no authority). ``ready_ids`` defaults to every authorized id, so a
    case that is not about readiness sees only the eligibility question.
    ``list_raises`` models an enumeration whose authority could not be
    consulted at all (a backend fault), distinct from an answered-but-empty
    enumeration.
    """
    auth = {} if authorities is None else dict(authorities)
    ready = set(auth) if ready_ids is None else ready_ids
    enumerated = tuple(auth) if listed is None else listed

    def resolve(attachment_id: str) -> AttachmentAuthority | None:
        if attachment_id in resolve_raises:
            raise RuntimeError("resolver fault")
        version = auth.get(attachment_id)
        return None if version is None else AttachmentAuthority(attachment_id, version)

    def is_ready(attachment_id: str, version: int) -> bool:
        if attachment_id in ready_raises:
            raise RuntimeError("readiness fault")
        return attachment_id in ready and auth.get(attachment_id) == version

    def is_capable(_operation: str) -> bool:
        if capable_raises:
            raise RuntimeError("capability fault")
        return capable

    def list_ids() -> tuple[str, ...]:
        if list_raises:
            raise RuntimeError("enumeration backend is down")
        return enumerated

    return AuthorityFacts(
        thread_id=thread_id,
        resolve_attachment=resolve,
        attachment_ready=is_ready,
        operation_capable=is_capable,
        list_attachments=list_ids,
    )


def _attachment_ref(attachment_id: str, version: object = 1) -> dict[str, object]:
    return {
        "kind": "attachment",
        "attachment_id": attachment_id,
        "version": version,
        "unit": 1,
    }


# --- normal behavior -------------------------------------------------------


def test_default_resolves_to_current_ready_attachments() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    scope = resolve_scope(None, facts)

    assert scope.attachment_ids == ("a", "b")
    assert scope.basis == "default"


def test_default_includes_an_authority_backed_attachment_even_when_not_ready() -> None:
    # Readiness is NOT a scope filter (spec decision 1: eligibility, readiness
    # and permission are separate facts). A not-ready-but-authorized id stays
    # in the eligibility set.
    facts = _facts(authorities={"a": 1, "b": 2}, ready_ids={"a"})

    scope = resolve_scope(None, facts)

    assert scope.attachment_ids == ("a", "b")


def test_explicit_restriction_narrows_to_named_authorized_ids() -> None:
    facts = _facts(authorities={"a": 1, "b": 2, "c": 3})

    scope = resolve_scope(ScopeRestriction(("c", "a"), "explicit"), facts)

    assert scope.attachment_ids == ("c", "a")
    assert scope.basis == "explicit"


# --- failure behavior ------------------------------------------------------


def test_resolver_that_raises_contributes_nothing() -> None:
    facts = _facts(
        authorities={"a": 1, "b": 2}, resolve_raises=frozenset({"b"})
    )

    restricted = resolve_scope(ScopeRestriction(("a", "b"), "explicit"), facts)
    default = resolve_scope(None, facts)

    assert restricted.attachment_ids == ("a",)
    assert default.attachment_ids == ("a",)


def test_gate_reports_a_raising_resolver_as_dependency_failed() -> None:
    scope = resolve_scope(None, _facts(authorities={"a": 1}))
    raising = _facts(authorities={"a": 1}, resolve_raises=frozenset({"a"}))
    gate = ScopeGate(effective=scope, facts=raising)

    decision = gate.check("read_source", source_ref=_attachment_ref("a"))

    assert decision.outcome == "dependency_failed"
    assert decision.reason == SCOPE_DEPENDENCY_FAILED


def test_gate_reports_a_raising_readiness_check_as_dependency_failed() -> None:
    scope = resolve_scope(None, _facts(authorities={"a": 1}))
    raising = _facts(authorities={"a": 1}, ready_raises=frozenset({"a"}))
    gate = ScopeGate(effective=scope, facts=raising)

    decision = gate.check("read_source", source_ref=_attachment_ref("a"))

    assert decision.outcome == "dependency_failed"


def test_gate_reports_a_raising_capability_check_as_dependency_failed() -> None:
    facts = _facts(authorities={}, capable_raises=True)
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    decision = gate.check("search_corpus")

    assert decision.outcome == "dependency_failed"


# --- boundary conditions ---------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_thread_yields_no_attachments_never_allow_all(blank: str) -> None:
    facts = _facts(thread_id=blank, authorities={"a": 1})

    scope = resolve_scope(ScopeRestriction(("a",), "explicit"), facts)

    assert scope.attachment_ids == ()
    assert scope.basis == "thread_unbound"
    assert scope.company_allowed is True


def test_restriction_naming_an_id_authority_no_longer_stands_behind_is_excluded() -> None:
    facts = _facts(authorities={"a": 1})

    scope = resolve_scope(ScopeRestriction(("a", "ghost"), "explicit"), facts)

    assert scope.attachment_ids == ("a",)


def test_gate_denies_a_version_mismatch_against_trusted_authority() -> None:
    facts = _facts(authorities={"a": 2})
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("a", version=1))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_ATTACHMENT_VERSION_STALE


def test_gate_reports_not_ready_as_unavailable_not_denied() -> None:
    # An authorized but not-ready attachment is IN scope; the gate reads
    # readiness live and answers ``unavailable``, which is neither a denial
    # (the id is permitted) nor a grant (the search cannot answer yet).
    facts = _facts(authorities={"a": 2}, ready_ids=set())
    scope = resolve_scope(None, facts)
    assert scope.attachment_ids == ("a",)

    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("a", version=2))

    assert decision.outcome == "unavailable"
    assert decision.reason == SCOPE_ATTACHMENT_NOT_READY


def test_readiness_never_turns_a_denial_into_a_grant() -> None:
    # Membership is checked before readiness: a not-ready id that current
    # authority does NOT stand behind is still denied, never made available.
    facts = _facts(authorities={"a": 1}, ready_ids=set())
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("zzz"))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_ATTACHMENT_NOT_PERMITTED


def test_not_ready_code_is_reachable_and_distinct_from_not_permitted() -> None:
    facts = _facts(authorities={"a": 1}, ready_ids=set())
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    unavailable = gate.check("read_source", source_ref=_attachment_ref("a"))
    denied = gate.check("read_source", source_ref=_attachment_ref("zzz"))

    assert unavailable.reason == SCOPE_ATTACHMENT_NOT_READY
    assert denied.reason == SCOPE_ATTACHMENT_NOT_PERMITTED
    assert unavailable.reason != denied.reason
    assert SCOPE_ATTACHMENT_NOT_READY in ScopeGate.DENIAL_CODES
    assert SCOPE_ATTACHMENT_NOT_PERMITTED in ScopeGate.DENIAL_CODES


# --- insufficient permission ----------------------------------------------


def test_gate_denies_an_attachment_id_outside_the_scope() -> None:
    facts = _facts(authorities={"a": 1})
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("zzz"))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_ATTACHMENT_NOT_PERMITTED


def test_gate_denies_when_authority_is_missing() -> None:
    scope = resolve_scope(None, _facts(authorities={"a": 1}))
    gone = _facts(authorities={})
    gate = ScopeGate(effective=scope, facts=gone)

    decision = gate.check("read_source", source_ref=_attachment_ref("a"))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_ATTACHMENT_UNKNOWN


def test_gate_denies_with_no_host_bound_thread() -> None:
    facts = _facts(thread_id="   ", authorities={"a": 1})
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("a"))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_THREAD_UNBOUND


def test_gate_denies_a_non_mapping_source_ref() -> None:
    facts = _facts(authorities={})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    decision = gate.check("read_source", source_ref="not-a-locator")  # type: ignore[arg-type]

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_SOURCE_REF_UNREADABLE


# --- empty / invalid input -------------------------------------------------


def test_empty_restriction_tuple_yields_an_empty_selection() -> None:
    facts = _facts(authorities={"a": 1})

    scope = resolve_scope(ScopeRestriction((), "explicit"), facts)

    assert scope.attachment_ids == ()


def test_blank_ids_in_a_restriction_contribute_nothing() -> None:
    facts = _facts(authorities={"a": 1})

    scope = resolve_scope(
        ScopeRestriction(("a", "", "   ", "a"), "explicit"), facts
    )

    assert scope.attachment_ids == ("a",)


def test_restriction_with_only_blank_ids_resolves_to_nothing() -> None:
    facts = _facts(authorities={"a": 1})

    scope = resolve_scope(ScopeRestriction(("", "   "), "explicit"), facts)

    assert scope.attachment_ids == ()


def test_gate_denies_an_unreadable_attachment_version() -> None:
    facts = _facts(authorities={"a": 1})
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check(
        "read_source", source_ref=_attachment_ref("a", version="not-a-version")
    )

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_SOURCE_REF_UNREADABLE


def test_gate_denies_a_blank_attachment_id() -> None:
    facts = _facts(authorities={"a": 1})
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("   "))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_SOURCE_REF_UNREADABLE


# --- inheritance and the decision-4 regression ----------------------------


def test_inherited_restriction_resolves_identically_to_explicit() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    explicit = resolve_scope(ScopeRestriction(("a",), "explicit"), facts)
    inherited = resolve_scope(ScopeRestriction(("a",), "inherited"), facts)

    assert explicit.attachment_ids == inherited.attachment_ids == ("a",)
    assert explicit.basis == "explicit"
    assert inherited.basis == "inherited"


def test_inherited_restriction_never_widens_to_a_new_attachment() -> None:
    # Only ``b`` has authority now; the inherited intent named ``a``, and an
    # absent ``a`` must resolve to fewer attachments, never to ``b``.
    facts = _facts(authorities={"b": 2})

    scope = resolve_scope(ScopeRestriction(("a",), "inherited"), facts)

    assert scope.attachment_ids == ()


def test_saved_restriction_is_not_a_grant_when_authority_is_absent() -> None:
    saved = resolve_scope(
        ScopeRestriction(("a",), "inherited"), _facts(authorities={"a": 1})
    )
    assert saved.attachment_ids == ("a",)

    gone = _facts(authorities={})
    gate = ScopeGate(effective=saved, facts=gone)

    decision = gate.check("read_source", source_ref=_attachment_ref("a"))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_ATTACHMENT_UNKNOWN


# --- company branch, capability, and the closed outcome set ---------------


def test_company_ref_is_permitted_subject_to_capability() -> None:
    facts = _facts(authorities={})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    permitted = gate.check(
        "read_source",
        source_ref={"document_id": "doc", "version": "v1", "unit": 1},
    )
    assert permitted.outcome == "permitted"

    incapable = _facts(authorities={}, capable=False)
    blocked = ScopeGate(
        effective=resolve_scope(None, incapable), facts=incapable
    ).check("read_source", source_ref={"document_id": "doc", "version": "v1", "unit": 1})
    assert blocked.outcome == "unsupported"
    assert blocked.reason == SCOPE_OPERATION_UNSUPPORTED


def test_broad_request_is_permitted() -> None:
    facts = _facts(authorities={"a": 1})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    assert gate.check("search_corpus").outcome == "permitted"


def test_attachment_only_broad_request_without_a_selection_is_empty() -> None:
    facts = _facts(authorities={})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    decision = gate.check("search_attachments")

    assert decision.outcome == "empty"
    assert decision.reason == SCOPE_SELECTION_EMPTY


def test_attachment_operation_is_unsupported_when_incapable() -> None:
    facts = _facts(authorities={"a": 1}, capable=False)
    scope = resolve_scope(None, facts)
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("a"))

    assert decision.outcome == "unsupported"


def test_company_allowed_is_true_unless_a_restriction_excludes_it() -> None:
    cases = [
        resolve_scope(None, _facts(authorities={"a": 1})),
        resolve_scope(ScopeRestriction((), "explicit"), _facts(authorities={"a": 1})),
        resolve_scope(
            ScopeRestriction(("a",), "inherited"), _facts(authorities={"a": 1})
        ),
        resolve_scope(None, _facts(thread_id="  ", authorities={"a": 1})),
    ]
    excluded = resolve_scope(
        ScopeRestriction(("a",), "explicit", company_excluded=True),
        _facts(authorities={"a": 1}),
    )

    assert all(scope.company_allowed is True for scope in cases)
    assert excluded.company_allowed is False


def test_gate_never_permits_an_attachment_outside_the_scope() -> None:
    facts = _facts(authorities={"a": 1, "b": 2}, listed=("a",))
    scope = resolve_scope(None, facts)  # default: only a is authorized
    gate = ScopeGate(effective=scope, facts=facts)

    decision = gate.check("read_source", source_ref=_attachment_ref("b"))

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_ATTACHMENT_NOT_PERMITTED


def test_every_outcome_is_reachable_and_denial_codes_are_distinct() -> None:
    in_scope = _facts(authorities={"a": 1})

    permitted = ScopeGate(
        effective=resolve_scope(None, in_scope), facts=in_scope
    ).check("read_source", source_ref=_attachment_ref("a"))
    denied = ScopeGate(
        effective=resolve_scope(None, in_scope), facts=in_scope
    ).check("read_source", source_ref=_attachment_ref("zzz"))

    incapable = _facts(authorities={"a": 1}, capable=False)
    unsupported = ScopeGate(
        effective=resolve_scope(None, incapable), facts=incapable
    ).check("read_source", source_ref=_attachment_ref("a"))

    not_ready = _facts(authorities={"a": 1}, ready_ids=set())
    unavailable = ScopeGate(
        effective=resolve_scope(None, not_ready), facts=not_ready
    ).check("read_source", source_ref=_attachment_ref("a"))

    raising = _facts(authorities={"a": 1}, resolve_raises=frozenset({"a"}))
    dependency_failed = ScopeGate(
        effective=resolve_scope(None, in_scope), facts=raising
    ).check("read_source", source_ref=_attachment_ref("a"))

    empty_facts = _facts(authorities={})
    empty = ScopeGate(
        effective=resolve_scope(None, empty_facts), facts=empty_facts
    ).check("search_attachments")

    outcomes = {
        permitted.outcome,
        denied.outcome,
        unsupported.outcome,
        unavailable.outcome,
        dependency_failed.outcome,
        empty.outcome,
    }
    assert outcomes == {
        "permitted",
        "denied",
        "unsupported",
        "unavailable",
        "dependency_failed",
        "empty",
    }

    reasons = [
        denied.reason,
        unsupported.reason,
        unavailable.reason,
        dependency_failed.reason,
    ]
    assert len(set(reasons)) == len(reasons)
    assert all(reason in ScopeGate.DENIAL_CODES for reason in reasons)


def test_two_distinct_causes_yield_two_distinct_codes() -> None:
    # Behavioral, not a set self-check: drive two DIFFERENT causes through the
    # same gate and assert the two answers differ. A not-ready (authorized but
    # unusable) id and an out-of-scope id are different facts and must never be
    # collapsed onto one code (SC-4).
    facts = _facts(authorities={"a": 1}, ready_ids=set())
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    unavailable = gate.check("read_source", source_ref=_attachment_ref("a"))
    denied = gate.check("read_source", source_ref=_attachment_ref("ghost"))

    assert unavailable.outcome == "unavailable"
    assert denied.outcome == "denied"
    assert unavailable.reason != denied.reason
    assert unavailable.reason == SCOPE_ATTACHMENT_NOT_READY
    assert denied.reason == SCOPE_ATTACHMENT_NOT_PERMITTED


# --- resolution fault is not an empty selection (SC-4) ---------------------


def test_a_faulting_enumeration_records_a_resolution_fault() -> None:
    # A list_attachments fault and an answered-but-empty enumeration both
    # yield no ids; the fault is recorded so they stay distinguishable.
    faulted = resolve_scope(None, _facts(authorities={}, list_raises=True))
    answered_empty = resolve_scope(None, _facts(authorities={}))

    assert faulted.attachment_ids == ()
    assert faulted.resolution_fault is not None
    assert answered_empty.attachment_ids == ()
    assert answered_empty.resolution_fault is None


def test_a_broad_attachment_request_is_dependency_failed_not_empty_when_faulted() -> None:
    facts = _facts(authorities={}, list_raises=True)
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    decision = gate.check("search_attachments")

    assert decision.outcome == "dependency_failed"
    assert decision.reason == SCOPE_DEPENDENCY_FAILED


def test_a_broad_attachment_request_over_an_answered_empty_is_still_empty() -> None:
    facts = _facts(authorities={})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    decision = gate.check("search_attachments")

    assert decision.outcome == "empty"
    assert decision.reason == SCOPE_SELECTION_EMPTY


# --- a requested scope change is a proposal, never authority (decision 9) --


def test_a_narrowing_request_yields_a_proposal_with_its_author() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    proposal = propose_scope_change(
        ScopeRestriction(("a",), "explicit"), facts, requested_by="model"
    )

    assert proposal is not None
    assert proposal.attachment_ids == ("a",)
    assert proposal.origin == "explicit"
    assert proposal.requested_by == "model"


def test_a_request_equal_to_the_current_selection_yields_no_proposal() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    # No narrowing, and a narrowing naming exactly the current selection, both
    # change nothing and must not mint a proposal.
    assert propose_scope_change(
        ScopeRestriction(None, "explicit"), facts, requested_by="model"
    ) is None
    assert propose_scope_change(
        ScopeRestriction(("a", "b"), "explicit"), facts, requested_by="model"
    ) is None


def test_a_proposal_alone_is_not_authority() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    proposal = propose_scope_change(
        ScopeRestriction(("a",), "explicit"), facts, requested_by="model"
    )
    assert isinstance(proposal, ScopeProposal)

    # The proposal grants nothing: the current effective selection is
    # unchanged, and a gate built on it still refuses an id the proposal names
    # unless authority already selected it.
    current = resolve_scope(None, facts)
    assert current.attachment_ids == ("a", "b")
    gate = ScopeGate(effective=current, facts=facts)
    assert gate.check(
        "read_source", source_ref=_attachment_ref("ghost")
    ).outcome == "denied"


# --- the company branch reads the decision, and can be refused -------------


def test_the_company_branch_consults_company_allowed() -> None:
    # Driven on a hand-built scope so the boundary's READ is pinned
    # independently of how the exclusion got resolved.
    facts = _facts(authorities={})
    narrowed = EffectiveScope(
        attachment_ids=(), company_allowed=False, basis="explicit"
    )
    gate = ScopeGate(effective=narrowed, facts=facts)

    decision = gate.check("read_source", source_ref={"document_id": "doc", "unit": 1})

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_COMPANY_NOT_PERMITTED


def test_scope_denied_error_names_the_code_only_for_a_refusal() -> None:
    denial = ScopeDecision("denied", SCOPE_ATTACHMENT_NOT_PERMITTED)

    assert scope_denied_error(denial) == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}

    with pytest.raises(ValueError):
        scope_denied_error(ScopeDecision("permitted", SCOPE_PERMITTED))
    with pytest.raises(ValueError):
        scope_denied_error(ScopeDecision("empty", SCOPE_SELECTION_EMPTY))


# --- the read-only report: capabilities and known limits (plan T2) --------
#
# Expected values come from ``specs/question-source-scope.md`` (decision 8
# "report known access limits directly", decision 4 "a saved restriction is
# not a grant", SC-5) — the report is derived by DRIVING the gate, never by
# reading the report's own fields back out.


def _report(gate: ScopeGate) -> ScopeReport:
    return gate.report()


def test_report_agrees_with_the_gate_operation_by_operation() -> None:
    """The anti-divergence guard: the report can never out-claim the gate.

    The expected permitted/refused truth is derived by calling ``check`` on a
    fixed universe the report does not own — not read off the report itself —
    and compared with what the report listed.
    """
    facts = _facts(authorities={"a": 1, "b": 2})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)
    report = _report(gate)

    universe = set(ATTACHMENT_SCOPED_OPERATIONS) | {COMPANY_CAPABILITY}
    for operation in universe:
        permitted = gate.check(operation).outcome == "permitted"
        assert (operation in report.feasible_capabilities) == permitted


def test_report_never_lists_an_operation_the_gate_would_not_permit() -> None:
    # An answered-but-empty selection: the attachment branch has nothing to
    # search, so ``search_attachments`` is the truthful empty, NOT a grant —
    # while the never-narrowed company branch stays feasible.
    empty = _facts(authorities={})
    empty_gate = ScopeGate(effective=resolve_scope(None, empty), facts=empty)
    report = _report(empty_gate)

    assert empty_gate.check("search_attachments").outcome == "empty"
    assert "search_attachments" not in report.feasible_capabilities
    assert COMPANY_CAPABILITY in report.feasible_capabilities
    for operation in report.feasible_capabilities:
        assert empty_gate.check(operation).outcome == "permitted"


def test_report_feasible_set_agrees_with_the_gate_under_incapability() -> None:
    facts = _facts(authorities={"a": 1}, capable=False)
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)
    report = _report(gate)

    universe = set(ATTACHMENT_SCOPED_OPERATIONS) | {COMPANY_CAPABILITY}
    for operation in universe:
        assert gate.check(operation).outcome == "unsupported"
        assert operation not in report.feasible_capabilities


def test_a_restriction_excluding_an_attachment_yields_a_known_limit_naming_it() -> None:
    # The restriction named two ids; current authority stands behind only one.
    # The excluded id is the saved intent that cannot be honored (decision 4)
    # and must surface directly rather than read as a current selection.
    facts = _facts(authorities={"att-a": 1})
    scope = resolve_scope(
        ScopeRestriction(("att-a", "att-gone"), "explicit"), facts
    )
    assert scope.attachment_ids == ("att-a",)
    assert scope.excluded_ids == ("att-gone",)

    gate = ScopeGate(effective=scope, facts=facts)
    report = _report(gate)

    excluded = [limit for limit in report.known_limits if "att-gone" in limit.detail]
    assert len(excluded) == 1
    assert excluded[0].code == SCOPE_ATTACHMENT_UNKNOWN
    assert excluded[0].code in KNOWN_LIMIT_CODES
    # It is a limit, never a grant: the id is not in the effective selection,
    # and the gate refuses a read of it.
    assert "att-gone" not in report.effective.attachment_ids
    assert gate.check(
        "read_source", source_ref=_attachment_ref("att-gone")
    ).outcome == "denied"


def test_an_unbound_thread_yields_a_known_limit_never_an_empty_report() -> None:
    facts = _facts(thread_id="   ", authorities={"att-a": 1})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)
    report = _report(gate)

    # Not an empty report that would read as full access.
    assert report.known_limits != ()
    assert [limit.code for limit in report.known_limits] == [SCOPE_THREAD_UNBOUND]
    assert SCOPE_THREAD_UNBOUND in KNOWN_LIMIT_CODES


def test_a_clean_default_scope_yields_no_limits() -> None:
    # Bound thread, every attachment authorized and ready, no restriction: no
    # limit applies, so the empty list is the truthful "nothing is withheld".
    facts = _facts(authorities={"a": 1, "b": 2})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)
    report = _report(gate)

    assert report.known_limits == ()
    assert report.feasible_capabilities  # access is real, not empty


def test_not_ready_is_reported_as_a_limit_and_not_as_a_permission_grant() -> None:
    facts = _facts(authorities={"a": 1}, ready_ids=set())
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)
    report = _report(gate)

    not_ready = [
        limit for limit in report.known_limits if limit.code == SCOPE_ATTACHMENT_NOT_READY
    ]
    assert len(not_ready) == 1
    assert "a" in not_ready[0].detail
    assert SCOPE_ATTACHMENT_NOT_READY in KNOWN_LIMIT_CODES
    # Not a grant: reading the id is unavailable, never permitted.
    decision = gate.check("read_source", source_ref=_attachment_ref("a"))
    assert decision.outcome == "unavailable"
    assert decision.reason == SCOPE_ATTACHMENT_NOT_READY


def test_a_faulting_enumeration_yields_a_dependency_limit() -> None:
    facts = _facts(authorities={}, list_raises=True)
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)
    report = _report(gate)

    assert [limit.code for limit in report.known_limits] == [SCOPE_DEPENDENCY_FAILED]
    assert SCOPE_DEPENDENCY_FAILED in KNOWN_LIMIT_CODES


def test_every_emitted_limit_code_is_in_the_closed_set_and_none_is_a_grant() -> None:
    # Drive every limit-producing shape and assert the codes stay inside the
    # declared closed set, and that no permission/empty code is ever a limit.
    cases: list[ScopeGate] = []
    unbound = _facts(thread_id="", authorities={"a": 1})
    cases.append(ScopeGate(effective=resolve_scope(None, unbound), facts=unbound))
    faulted = _facts(authorities={}, list_raises=True)
    cases.append(ScopeGate(effective=resolve_scope(None, faulted), facts=faulted))
    not_ready = _facts(authorities={"a": 1}, ready_ids=set())
    cases.append(ScopeGate(effective=resolve_scope(None, not_ready), facts=not_ready))
    restricted = _facts(authorities={"a": 1})
    cases.append(
        ScopeGate(
            effective=resolve_scope(
                ScopeRestriction(("a", "gone"), "inherited"), restricted
            ),
            facts=restricted,
        )
    )

    emitted = [
        limit for gate in cases for limit in gate.report().known_limits
    ]
    assert emitted  # the shapes above really produce limits
    assert all(limit.code in KNOWN_LIMIT_CODES for limit in emitted)
    assert all(isinstance(limit, KnownLimit) for limit in emitted)
    assert SCOPE_PERMITTED not in KNOWN_LIMIT_CODES
    assert SCOPE_SELECTION_EMPTY not in KNOWN_LIMIT_CODES


def test_report_is_pure_and_repeatable() -> None:
    facts = _facts(authorities={"a": 1, "b": 2}, ready_ids={"a"})
    effective = resolve_scope(None, facts)
    gate = ScopeGate(effective=effective, facts=facts)

    first = gate.report()
    second = gate.report()

    # Repeated calls return equal reports and do not mutate the gate's scope.
    assert first == second
    assert first.effective == effective
    assert second.effective == effective
    # And a fresh resolve of the same facts is byte-for-byte the same scope.
    assert effective == resolve_scope(None, facts)


# --- an explicit restriction can exclude the company branch ----------------
#
# Expected values come from specs/question-source-scope.md decision 3 — an
# operator-declared exclusive restriction removes the company branch — read
# together with ADR 0088:10's naming rule, which stays untouched.


def _exclusive(attachment_ids: tuple[str, ...] | None) -> ScopeRestriction:
    return ScopeRestriction(attachment_ids, "explicit", company_excluded=True)


def test_an_exclusive_restriction_excludes_the_company_branch() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    scope = resolve_scope(_exclusive(("a",)), facts)

    assert scope.attachment_ids == ("a",)
    assert scope.company_allowed is False
    assert scope.basis == "explicit"


def test_a_narrowing_restriction_leaves_the_company_branch_eligible() -> None:
    # ADR 0088:10 no-regression at the module level: narrowing the attachment
    # branch is not an exclusive selection.
    facts = _facts(authorities={"a": 1, "b": 2})

    scope = resolve_scope(ScopeRestriction(("a",), "explicit"), facts)

    assert scope.company_allowed is True


def test_an_inherited_exclusive_restriction_still_excludes_the_corpus() -> None:
    # Decision 3: inheritance reuses the INTENT. An exclusive restriction
    # carried forward excludes the corpus exactly as the request that made it.
    facts = _facts(authorities={"a": 1})
    explicit = resolve_scope(_exclusive(("a",)), facts)
    inherited = resolve_scope(
        ScopeRestriction(("a",), "inherited", company_excluded=True), facts
    )

    # Same selection, same branch ruling; only the recorded origin differs.
    assert inherited.attachment_ids == explicit.attachment_ids
    assert inherited.company_allowed is explicit.company_allowed is False
    assert (explicit.basis, inherited.basis) == ("explicit", "inherited")


def test_an_exclusive_restriction_excludes_the_corpus_without_a_bound_thread() -> None:
    # Decision 4: no thread to consult is never a reason to fall back to the
    # broader selection the operator excluded.
    facts = _facts(thread_id="  ", authorities={"a": 1})

    scope = resolve_scope(_exclusive(("a",)), facts)

    assert scope.company_allowed is False
    assert scope.basis == "thread_unbound"


def test_a_default_scope_and_a_naming_restriction_both_allow_the_corpus() -> None:
    facts = _facts(authorities={"a": 1})

    assert resolve_scope(None, facts).company_allowed is True
    assert resolve_scope(
        ScopeRestriction(None, "inherited"), facts
    ).company_allowed is True


def test_gate_denies_a_company_locator_when_the_branch_is_excluded() -> None:
    facts = _facts(authorities={"a": 1})
    gate = ScopeGate(effective=resolve_scope(_exclusive(("a",)), facts), facts=facts)

    decision = gate.check(
        "read_source", source_ref={"document_id": "doc", "version": "v", "unit": 1}
    )

    assert decision.outcome == "denied"
    assert decision.reason == SCOPE_COMPANY_NOT_PERMITTED


def test_gate_denies_a_broad_company_operation_when_the_branch_is_excluded() -> None:
    facts = _facts(authorities={"a": 1})
    gate = ScopeGate(effective=resolve_scope(_exclusive(("a",)), facts), facts=facts)

    denied = gate.check("search_corpus")
    # The attachment branch of the same scope is untouched by the exclusion.
    attachment = gate.check("search_attachments")

    assert (denied.outcome, denied.reason) == (
        "denied",
        SCOPE_COMPANY_NOT_PERMITTED,
    )
    assert attachment.outcome == "permitted"


def test_an_excluded_branch_is_denied_before_any_capability_question() -> None:
    # Two causes, two codes: an excluded branch is a denial the operator can
    # act on, and must never be re-labeled by an adapter's capability answer.
    facts = _facts(authorities={"a": 1}, capable_raises=True)
    gate = ScopeGate(effective=resolve_scope(_exclusive(("a",)), facts), facts=facts)

    decision = gate.check("get_xbrl_fact")

    assert decision.reason == SCOPE_COMPANY_NOT_PERMITTED


def test_a_restriction_that_narrows_nothing_is_not_in_force() -> None:
    # The predicate a boundary uses to decide whether it must consult an
    # authority at all: only a narrowing or an exclusion is in force.
    assert ScopeRestriction(None, "explicit").in_force is False
    assert ScopeRestriction((), "explicit").in_force is True
    assert ScopeRestriction(("a",), "explicit").in_force is True
    assert ScopeRestriction(None, "inherited", company_excluded=True).in_force is True


# --- capability is a derived fact, never an unconditional grant ------------


def test_an_operation_with_no_expressing_adapter_is_unsupported() -> None:
    # Fail-closed: an operation the wired adapters cannot express is refused
    # rather than silently allowed — the shape the previous constant had.
    facts = _facts(authorities={"a": 1})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    decision = gate.check("search_every_source")

    assert decision.outcome == "unsupported"
    assert decision.reason == SCOPE_OPERATION_UNSUPPORTED


@pytest.mark.parametrize(
    "operation",
    [
        "search_attachments",
        "search_corpus",
        "list_files",
        "read_source",
        "read_visual_detail",
        "admit_evidence",
        "get_xbrl_fact",
        "compare_facts",
        "redline",
        "extract_tables",
        "three_year_statement",
        COMPANY_CAPABILITY,
    ],
)
def test_every_wired_operation_is_declared_expressible(operation: str) -> None:
    facts = _facts(authorities={"a": 1})
    gate = ScopeGate(effective=resolve_scope(None, facts), facts=facts)

    assert gate.check(operation).outcome == "permitted"


# --- an unconsultable authority is never the successful empty --------------


def test_a_naming_restriction_over_an_unconsultable_authority_faults() -> None:
    # The named id could not be asked about at all. That is a dependency
    # failure, not the "authority answered and nothing matched" empty.
    facts = _facts(authorities={"a": 1}, resolve_raises=frozenset({"a"}))

    scope = resolve_scope(ScopeRestriction(("a",), "explicit"), facts)

    assert scope.attachment_ids == ()
    assert scope.resolution_fault == "RuntimeError"
    # Nothing was refuted, so nothing may be reported as refuted.
    assert scope.excluded_ids == ()

    gate = ScopeGate(effective=scope, facts=facts)
    assert gate.check("search_attachments").reason == SCOPE_DEPENDENCY_FAILED


def test_a_partially_faulted_restriction_keeps_the_ids_authority_answered() -> None:
    facts = _facts(authorities={"a": 1, "b": 2}, resolve_raises=frozenset({"b"}))

    scope = resolve_scope(ScopeRestriction(("a", "b"), "explicit"), facts)

    assert scope.attachment_ids == ("a",)
    assert scope.excluded_ids == ()
    assert scope.resolution_fault == "RuntimeError"


# --- the proposal carries the whole intent, not just the ids (decision 9) --


def test_a_proposal_carries_the_exclusion_so_runtime_can_persist_it() -> None:
    facts = _facts(authorities={"a": 1, "b": 2})

    proposal = propose_scope_change(
        _exclusive(("a",)), facts, requested_by="operator"
    )

    assert proposal is not None
    assert proposal.attachment_ids == ("a",)
    assert proposal.company_excluded is True
    # A proposal grants nothing on its own.
    assert resolve_scope(None, facts).company_allowed is True


# --- the report names the exclusion as a known limit (decision 8) ----------


def test_report_names_the_excluded_corpus_as_a_known_limit() -> None:
    facts = _facts(authorities={"a": 1})
    gate = ScopeGate(effective=resolve_scope(_exclusive(("a",)), facts), facts=facts)
    report = _report(gate)

    assert SCOPE_COMPANY_NOT_PERMITTED in KNOWN_LIMIT_CODES
    assert COMPANY_CAPABILITY not in report.feasible_capabilities
    assert "search_attachments" in report.feasible_capabilities
    limits = [limit for limit in report.known_limits]
    assert [limit.code for limit in limits] == [SCOPE_COMPANY_NOT_PERMITTED]
    assert limits[0].remedy is not None
    # The limit is a fact about access, never a grant: the gate still refuses.
    assert gate.check("search_corpus").outcome == "denied"
