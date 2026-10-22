"""T14 -- EP4: the `supports` wire contract, checked without a process.

Nine cases of the continuation contract reach no PostgreSQL, no child process and
no MCP server: they drive :class:`EvidenceRouteClient` over a recorded session
and read the answers iknow's dispatch layer puts on the wire.  They live here
rather than in ``test_live_t14_support_continuation.py`` so the **default** lane
runs them.  That module's ``pytestmark`` is the opt-in boundary for a live
database and a real child, and a contract that only runs behind
``-m "mcp and sql_live"`` has no coverage in the lane every reader runs first.

Both arms of the action are pinned here: the refusals, and the success envelope
that carries a page back -- including the last page of a sequence, whose
``next_cursor`` is ``null`` and which is the only case in this module where the
parse must accept that value rather than refuse it.

The trusted-binding seam rides here for the same reason.  Which variables reach
a child is decided by :data:`TRUSTED_BINDING_ENV_VARS` and by
:meth:`EvidenceIdentityBinding.child_env`, and both are pure functions of the
binding -- no process, no database -- so pinning them here is what puts them in
the lane every reader runs first instead of behind the live markers.

The expectations come from iknow's source, not from anything a child answered,
so a drift between the contract and the implementation fails here rather than
hiding behind a live run.  The record these were read from is
``docs/validation/2026-10-06-t14-ep4-support-continuation-freeze.md`` §12.3.

The literals are this module's own.  Nothing here claims anything about a live
store, which is what lets the module stay independent of the live fixtures.
"""

from __future__ import annotations

from typing import Any

import pytest

from fin.retrieve.evidence_route import (
    EVIDENCE_ENVELOPE_VERSION,
    MAX_BINDING_TOKEN_CHARS,
    TRUSTED_BINDING_ENV_VARS,
    EvidenceGrantRefusal,
    EvidenceIdentityBinding,
    EvidenceIdentityError,
    EvidenceRouteClient,
    _is_bounded_token,
    deadline_within,
)
from fin.retrieve.mcp_session import MCPStdioTransportError


# ----- frozen expectations, read off iknow's contract -------------------------

#: The reference every case asks for. It is a grant no live lane seeds, because
#: the only thing under test is the wire, not any store's answer.
PAGED_REF = "d1000000-0000-4000-8000-000000000000"
#: A member of that grant, and the one member a selector may name.
PAGED_MEMBER = "d2000000-0000-4000-8000-000000000001"
#: A reference of a different grant: what an envelope for the wrong read carries.
OTHER_REF = "c1000000-0000-4000-8000-000000000000"

#: An identity no assertion is ever made against. The wire cases below are about
#: the request and the envelope, so the binding only has to be complete.
SCOPE_A = "t14-envelope-owner-scope"
SOURCE_A1 = "t14-envelope-source-a1"
SOURCE_A2 = "t14-envelope-source-a2"
AUTH_REV = "t14-envelope-auth-rev-1"

#: A cursor as it travels: opaque text the caller replays undecoded.
CURSOR = "ZmluLWN1cnNvcg"

#: Two consumers, because ``specs/structured-corpus-evidence.md:257-259`` makes a
#: cross-consumer checkpoint refuse and that refusal is unobservable with one.
CONSUMER_A = "t14-envelope-consumer-a"
CONSUMER_B = "t14-envelope-consumer-b"


# ----- the recorded session ----------------------------------------------------


class _RecordedSession:
    """A session that answers one result and keeps what it was asked.

    `failure` raises instead of answering, which is how the transport arm of a
    refusal is reached without a process that is not there.
    """

    def __init__(self, result: Any, failure: Exception | None = None) -> None:
        self._result = result
        self._failure = failure
        self.requests: list[dict[str, Any]] = []

    def call(self, payload: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        self.requests.append(payload)
        if self._failure is not None:
            raise self._failure
        return {"jsonrpc": "2.0", "id": payload["id"], "result": self._result}


def _client_for(session: Any) -> EvidenceRouteClient:
    """A client over a recorded session, under an identity that is never asserted."""

    binding = EvidenceIdentityBinding(
        actor_id="fin-t14-envelope",
        owner_scope_binding=SCOPE_A,
        reader_binding="fin-t14-envelope",
        source_refs=(SOURCE_A1, SOURCE_A2),
        authorization_revision=AUTH_REV,
        deadline_at=deadline_within(60.0),
    )
    return EvidenceRouteClient(session, binding=binding)


def _answering(result: Any) -> tuple[EvidenceRouteClient, _RecordedSession]:
    session = _RecordedSession(result)
    return _client_for(session), session


def _binding(**overrides: Any) -> EvidenceIdentityBinding:
    """A complete binding, with ``consumer_id`` only when a case names one."""

    fields: dict[str, Any] = {
        "actor_id": "fin-t14-envelope",
        "owner_scope_binding": SCOPE_A,
        "reader_binding": "fin-t14-envelope",
        "source_refs": (SOURCE_A1, SOURCE_A2),
        "authorization_revision": AUTH_REV,
        "deadline_at": deadline_within(60.0),
    }
    fields.update(overrides)
    return EvidenceIdentityBinding(**fields)


def _support_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "iknow.evidence_result",
        "schema_version": 1,
        "evidence_ref": PAGED_REF,
        "expires_at": "2026-10-07T00:00:00.000Z",
        "total_supports": 1,
        "supports": [{"support_id": PAGED_MEMBER}],
        "next_cursor": CURSOR,
        "selected": False,
    }
    result.update(overrides)
    return result


def test_only_a_named_selector_is_sent_and_nothing_else_is_added() -> None:
    """The private route's key check is strict, so an unnamed selector stays absent.

    A selector the caller did not name is a missing key and never a `null` one:
    the request carries exactly the four contract keys, plus whichever selector
    the caller did name.
    """

    client, session = _answering(_support_result())
    base = {"schema", "schema_version", "action", "evidence_ref"}

    client.supports(PAGED_REF)
    client.supports(PAGED_REF, cursor=None, support_id=None)
    client.supports(PAGED_REF, cursor=CURSOR)
    client.supports(PAGED_REF, support_id=PAGED_MEMBER)

    assert [set(request["params"]) for request in session.requests] == [
        base,
        base,
        base | {"cursor"},
        base | {"support_id"},
    ]
    assert session.requests[2]["params"]["cursor"] == CURSOR
    assert session.requests[3]["params"]["support_id"] == PAGED_MEMBER
    assert session.requests[0]["params"]["action"] == "supports"


def test_an_incomplete_support_envelope_is_refused_rather_than_returned() -> None:
    """A page whose cursor member is gone is a contract violation, not an ending.

    A caller cannot tell the last page from a dropped cursor, so the absent
    member is refused instead of defaulted.
    """

    dropped_cursor = _support_result()
    del dropped_cursor["next_cursor"]
    client, _ = _answering(dropped_cursor)
    with pytest.raises(EvidenceGrantRefusal) as without_cursor:
        client.supports(PAGED_REF, cursor=CURSOR)
    assert without_cursor.value.code == "malformed_evidence_contract"
    assert without_cursor.value.reason == "incomplete_result"
    assert "next_cursor" in str(without_cursor.value)

    incomplete = _support_result()
    del incomplete["total_supports"]
    client, _ = _answering(incomplete)
    with pytest.raises(EvidenceGrantRefusal) as without_total:
        client.supports(PAGED_REF)
    assert without_total.value.reason == "incomplete_result"


def test_a_valid_support_envelope_is_returned_whole() -> None:
    """The success arm is pinned here, and the last page with it.

    Every other case in this module is a refusal, so without this one the *only*
    lane a reader runs first never executes the arm that actually returns a page.
    The last page is the case that matters most: it carries ``next_cursor:
    None``, and that is a value the contract must accept as the end of a
    sequence rather than refuse as a missing cursor. ``next_cursor is None`` is
    therefore a success path here, and a fin that treated a null cursor as a
    contract violation would refuse the final page of every long sequence.

    The envelope is also checked to travel whole: no member is dropped, renamed,
    or defaulted on the way to the caller, so a caller reconstructs the page from
    what this returns rather than from what it expected to receive.
    """

    page = _support_result(
        total_supports=2,
        supports=[{"support_id": PAGED_MEMBER}],
        next_cursor=None,
        selected=False,
    )
    client, _ = _answering(page)
    returned = client.supports(PAGED_REF, cursor=CURSOR)

    assert returned == page
    assert returned["next_cursor"] is None
    assert returned["evidence_ref"] == PAGED_REF
    assert returned["total_supports"] == 2
    assert returned["supports"] == [{"support_id": PAGED_MEMBER}]
    assert returned["selected"] is False
    assert returned["schema"] == "iknow.evidence_result"
    assert returned["schema_version"] == 1

    # A selected answer is a page too, and is returned the same way.
    selected = _support_result(supports=[{"support_id": PAGED_MEMBER}], selected=True)
    client, _ = _answering(selected)
    chosen = client.supports(PAGED_REF, support_id=PAGED_MEMBER)
    assert chosen == selected
    assert chosen["selected"] is True


def test_a_null_cursor_is_the_end_of_a_sequence_and_not_a_refusal() -> None:
    """``None`` is the one value a cursor may carry, and it means "no more".

    This is the parse-level twin of the last-page case above, and it is what a
    caller branches on to stop following a sequence. It has to be distinguishable
    from an absent member, which is a refusal, and from a cursor of the wrong
    type, which is also a refusal -- otherwise a caller cannot tell "the grant
    ended" from "the envelope is broken".
    """

    client, _ = _answering(_support_result(next_cursor=None))
    assert client.supports(PAGED_REF)["next_cursor"] is None

    # A dropped member is not the same claim and is refused.
    dropped = _support_result()
    del dropped["next_cursor"]
    client, _ = _answering(dropped)
    with pytest.raises(EvidenceGrantRefusal) as refused:
        client.supports(PAGED_REF)
    assert refused.value.reason == "incomplete_result"


def test_two_selectors_together_are_refused_as_mutually_exclusive() -> None:
    """Both selectors at once is refused, and neither one is honoured.

    Promoted out of ``test_live_t14_support_continuation.py`` for the same
    reason the other wire cases live here: the live lane's ``pytestmark`` is the
    opt-in boundary, so a contract that only runs there has no coverage in the
    lane every reader runs first. iknow folds a contract violation onto the
    member that already means "this request is not on the contract" -- the wire
    reason is ``unknown_field``, not the continuation's own
    ``contract_violation`` -- and fin carries both halves through unchanged
    rather than opening its own vocabulary for it.

    The refusal is also not a membership oracle: it does not disclose which
    selector would have been honoured, or whether either names anything real.
    """

    client, session = _answering(
        {
            "schema": "iknow.evidence_result",
            "schema_version": 1,
            "error": {
                "code": "malformed_evidence_contract",
                "reason": "unknown_field",
                "message": (
                    "a continuation cursor and a selected support_id are "
                    "mutually exclusive; a request may name one or the other, "
                    "never both"
                ),
                "retryable": False,
            },
        }
    )
    with pytest.raises(EvidenceGrantRefusal) as refused:
        client.supports(PAGED_REF, cursor=CURSOR, support_id=PAGED_MEMBER)

    assert refused.value.code == "malformed_evidence_contract"
    assert refused.value.reason == "unknown_field"
    assert "mutually exclusive" in str(refused.value)
    # The request itself still carries both, verbatim and unexamined: fin does not
    # second-guess a combination iknow has already answered for.
    assert session.requests[0]["params"]["cursor"] == CURSOR
    assert session.requests[0]["params"]["support_id"] == PAGED_MEMBER


def test_a_wrongly_typed_support_envelope_is_refused() -> None:
    """A page is never returned half-populated, whatever the member's shape is."""

    for overrides, field in (
        ({"total_supports": True}, "total_supports"),
        ({"supports": {"support_id": PAGED_MEMBER}}, "supports"),
        ({"next_cursor": 7}, "next_cursor"),
        ({"selected": "false"}, "selected"),
        ({"expires_at": None}, "expires_at"),
    ):
        client, _ = _answering(_support_result(**overrides))
        with pytest.raises(EvidenceGrantRefusal) as refused:
            client.supports(PAGED_REF)
        assert refused.value.code == "malformed_evidence_contract", field
        assert refused.value.reason == "malformed_result_field", field
        assert field in str(refused.value)


def test_a_support_envelope_for_another_grant_is_refused() -> None:
    """The envelope is the only thing linking the answer to the request."""

    client, _ = _answering(_support_result(evidence_ref=OTHER_REF))
    with pytest.raises(EvidenceGrantRefusal) as refused:
        client.supports(PAGED_REF)
    assert refused.value.reason == "evidence_ref_mismatch"


def test_every_supports_refusal_reaches_the_caller_with_both_halves() -> None:
    """Fin adds no member to either vocabulary: both halves travel unchanged.

    These are every `(code, reason)` pair the wire can carry on this action, read
    off iknow's source rather than off one observed run.  A denial reason is a
    member of the closed `EVIDENCE_DENIAL_REASON` set and passes through with the
    failure token `decideGrantRead` mapped it onto; a contract violation is folded
    onto `unknown_field`, and one whose failure token is the size one onto
    `envelope_too_large`.  `unknown_reference` and `scope_unknown` are reachable
    here because the SUPPORTS branch reaches `refusalForDecision` *before* the
    continuation does.  Neither fold is re-done on this side, because a second
    mapping would be a second answer to a question the transport already answered.

    Two rows carry no iknow fact and name the fault's class only: the unresolved
    binding keeps whatever scope code the identity seam chose (`backend_unavailable`
    is the class it falls back to), and an unclassifiable fault out of the
    transaction is reported the same way. `supports_continuation_not_read` is a
    member of the closed set that this path can no longer emit at all.

    Fin's own contract refusals are a separate, closed list -- `result_not_an_object`,
    `unknown_envelope`, `incomplete_result`, `malformed_result_field`,
    `evidence_ref_mismatch` and the transport's `private_route_unreachable` -- and
    each is pinned by a case in this module.
    """

    cases = (
        # the request never reached the grant
        ("malformed_evidence_contract", "unknown_field"),
        # the grant itself is withheld
        ("evidence_unavailable", "unknown_reference"),
        ("evidence_expired", "expired"),
        ("evidence_unavailable", "scope_unknown"),
        ("access_denied", "authorization_revoked"),
        ("evidence_unavailable", "purged"),
        ("evidence_unavailable", "obligations_stopped"),
        ("unsupported_source_binding", "dependency_unresolved"),
        # the grant is withheld from this caller
        ("access_denied", "context_not_minted"),
        ("access_denied", "scope_not_authorized"),
        ("access_denied", "source_outside_authorized_subset"),
        # the membership itself is refused
        ("malformed_evidence_contract", "count_grant_has_no_supports"),
        ("evidence_too_large", "envelope_too_large"),
        # the transport could not name a fact
        ("backend_unavailable", "trusted_binding_unresolved"),
        ("backend_unavailable", "backend_unavailable"),
    )
    for code, reason in cases:
        client, _ = _answering(
            {
                "schema": "iknow.evidence_result",
                "schema_version": 1,
                "error": {
                    "code": code,
                    "reason": reason,
                    "message": "the continuation refused this page",
                    "retryable": False,
                },
            }
        )
        with pytest.raises(EvidenceGrantRefusal) as refused:
            client.supports(PAGED_REF)
        assert refused.value.code == code, reason
        assert refused.value.reason == reason


# ----- the eighth allowlisted variable: the consumer identity ---------------


def test_the_allowlist_is_eight_members_and_the_eighth_is_the_consumer() -> None:
    """The consumer is its own variable, not one of the seven identity strings.

    The spec makes invalidations a per-consumer stream and makes a
    cross-consumer checkpoint refuse (`:252`, `:254`, `:257-259`), so the
    consumer has to travel as a distinct member. Folding it into ``actor_id`` (a
    principal) or ``owner_scope_binding`` (a scope) would let two consumers pass
    each other's checkpoints, which is why the count grew instead.
    """

    assert TRUSTED_BINDING_ENV_VARS["consumer_id"] == "IKNOW_EVIDENCE_CONSUMER_ID"
    assert len(TRUSTED_BINDING_ENV_VARS) == 8
    # Distinct from every other member: no existing name may double as the
    # consumer, or the two would travel through one slot.
    assert len(set(TRUSTED_BINDING_ENV_VARS.values())) == 8


def test_a_supplied_consumer_reaches_the_child_and_an_absent_one_does_not() -> None:
    """The child sees the consumer it was given, and no empty stand-in.

    An absent consumer is an ABSENT variable. Forwarding it as an empty string
    would make "this host named no consumer" indistinguishable from "this host
    named an empty one" on the wire, and the backend records the consumer
    against each grant, so the two must not collapse.
    """

    name = TRUSTED_BINDING_ENV_VARS["consumer_id"]

    assert name not in _binding().child_env()
    assert name not in _binding(consumer_id="").child_env()
    assert _binding(consumer_id=CONSUMER_A).child_env()[name] == CONSUMER_A
    assert _binding(consumer_id=CONSUMER_B).child_env()[name] == CONSUMER_B

    # The seven unconditional members are untouched by any of this, and a binding
    # that supplies a consumer adds exactly one key: nothing else widened. The
    # deadline is pinned across both so the comparison is about the consumer.
    assert len(_binding().child_env()) == 7
    assert len(_binding(consumer_id=CONSUMER_A).child_env()) == 8
    pinned = deadline_within(60.0)
    with_consumer = _binding(deadline_at=pinned, consumer_id=CONSUMER_A).child_env()
    without_consumer = _binding(deadline_at=pinned).child_env()
    assert {
        key: value for key, value in with_consumer.items() if key != name
    } == without_consumer


def test_two_consumers_differ_in_the_consumer_slot_and_nowhere_else() -> None:
    """The variable is what makes N >= 2 reachable on the transport at all.

    ``specs/structured-corpus-evidence.md:259`` requires cross-consumer
    checkpoints to refuse. Two hosts sharing one ``actor_id`` and one scope would
    put the same value in every other slot, so if the consumer rode one of those
    the backend could not tell the streams apart and the refusal would be
    unreachable. Pinning the difference to exactly the one new key is the
    property that keeps it reachable.
    """

    name = TRUSTED_BINDING_ENV_VARS["consumer_id"]
    shared = {
        "actor_id": "fin-t14-envelope",
        "owner_scope_binding": SCOPE_A,
        "reader_binding": "fin-t14-envelope",
        "source_refs": (SOURCE_A1, SOURCE_A2),
        "authorization_revision": AUTH_REV,
        # One deadline for both, so the comparison below isolates the consumer
        # rather than catching a per-binding instant.
        "deadline_at": deadline_within(60.0),
    }
    env_a = _binding(**shared, consumer_id=CONSUMER_A).child_env()
    env_b = _binding(**shared, consumer_id=CONSUMER_B).child_env()

    assert {key for key in env_a if env_a[key] != env_b[key]} == {name}


def test_a_supplied_but_invalid_consumer_id_is_refused_before_any_process() -> None:
    """A named consumer is judged the moment it is supplied.

    Each case below is one the drain on the other side would refuse as a
    malformed token, so forwarding it would mint a child that looks bound and
    streams as nobody. The refusal lands here, at construction, where an unbound
    host already fails by name.
    """

    invalid = (
        (7, "a non-string is not an identity label"),
        (None, "an explicit None is not an absence"),
        (True, "a bool is not an identity label"),
        (["fin"], "a list is not an identity label"),
        ("   ", "blank-but-not-empty is not a token"),
        (" fin-consumer", "leading whitespace"),
        ("fin-consumer ", "trailing whitespace"),
        ("c" * (MAX_BINDING_TOKEN_CHARS + 1), "over the length bound"),
    )
    for value, why in invalid:
        with pytest.raises(EvidenceIdentityError) as refused:
            _binding(consumer_id=value)
        assert "consumer_id" in str(refused.value), why

    # An INTERIOR newline is accepted, deliberately, and this is the seam's
    # one documented gap rather than an oversight: the bound is the same
    # four-rule token the drain applies, and that rule checks only the ends. Tightening
    # it here alone would make the transport refuse a value the drain accepts,
    # so the two would disagree about the same string.
    assert _binding(consumer_id="fin\nconsumer").child_env()


def test_the_consumer_bound_is_the_same_bound_the_drain_applies() -> None:
    """The transport's bound and the drain's are one number, not two.

    ``consumer_id`` is forwarded here and stored there, so a length accepted on
    one side and refused on the other would be a binding that launches a child
    and then cannot drain. The drain now imports this module's bound and its
    four-rule predicate rather than restating them, so the numbers cannot
    drift; this case pins that the alias is still wired, and it pins the four
    RULES, which the number alone never did.
    """

    from fin.host.evidence.structured_evidence_invalidation import (
        MAX_INVALIDATION_TOKEN_CHARS,
        _is_bounded_token as drain_bound_rule,
    )

    assert MAX_BINDING_TOKEN_CHARS == MAX_INVALIDATION_TOKEN_CHARS
    assert drain_bound_rule is _is_bounded_token

    # The four rules themselves, on the boundary and just inside it: a string,
    # non-empty, no longer than the bound, and no surrounding whitespace. Two
    # constants can be equal while one predicate accepts a value the other
    # refuses, and an equality assertion alone cannot see that.
    accepted = [
        "fin-default",
        "c",
        "fin consumer",  # an interior space is accepted: the rule checks the ends
        "fin\nconsumer",
        "c" * MAX_BINDING_TOKEN_CHARS,
    ]
    refused = [
        (7, "not a string"),
        (True, "a bool is not a label"),
        ("", "empty is not a label"),
        ("   ", "blank is not a label"),
        (" fin", "leading whitespace"),
        ("fin ", "trailing whitespace"),
        ("c" * (MAX_BINDING_TOKEN_CHARS + 1), "one past the bound"),
    ]
    for value in accepted:
        assert _is_bounded_token(value), value
    for value, why in refused:
        assert not _is_bounded_token(value), why
        assert not drain_bound_rule(value), why

    # And the boundary end to end: exactly at the bound is accepted, one past it
    # is not — on the transport that forges the label.
    assert _binding(consumer_id="c" * MAX_BINDING_TOKEN_CHARS)
    with pytest.raises(EvidenceIdentityError):
        _binding(consumer_id="c" * (MAX_BINDING_TOKEN_CHARS + 1))


def test_the_envelope_version_is_the_single_authority_the_drain_derives() -> None:
    """The drain's version and this envelope's version are one number, not two.

    ``invalidations`` is an ACTION of this envelope (ADR 0097 §2.1), so its
    version is this envelope's version; a second literal in the drain is a
    number that answers one reader and refuses the other. The drain derives its
    name from :data:`~fin.host.evidence.evidence_contract.EVIDENCE_SCHEMA_VERSION`, and
    this case is what stops that derivation from becoming a stale copy.
    """

    from fin.host.evidence.evidence_contract import EVIDENCE_SCHEMA_VERSION
    from fin.host.evidence.structured_evidence_invalidation import (
        INVALIDATION_EVENT_SCHEMA_VERSION,
    )

    assert INVALIDATION_EVENT_SCHEMA_VERSION == EVIDENCE_SCHEMA_VERSION
    assert INVALIDATION_EVENT_SCHEMA_VERSION == EVIDENCE_ENVELOPE_VERSION


def test_a_transport_error_is_a_refusal_and_not_an_empty_success() -> None:
    """A dead private route is a typed refusal, never a page with no supports."""

    session = _RecordedSession({}, failure=MCPStdioTransportError("no answer"))
    with pytest.raises(EvidenceGrantRefusal) as refused:
        _client_for(session).supports(PAGED_REF)
    assert refused.value.code == "backend_unavailable"
    assert refused.value.reason == "private_route_unreachable"
