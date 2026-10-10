"""EP7 replay: the chain, its refusals, and the comparison it actually makes.

Every case here pins a boundary that could be widened by accident:

* replay reads the reference OUT of the stored snapshot, so a record cannot be
  replayed under a reference it does not carry;
* the digest is recomputed, so a mutated stored snapshot is refused before any
  transport call;
* iknow's own refusal code and reason survive the crossing, so expiry stays
  distinguishable from purge;
* the grant-time identity is compared across the boundary, so a grant that no
  longer denotes what was recorded is refused;
* nothing here can reach a retrieval path, because the port has one method.

The live boundary proof against a real iknow child and real PostgreSQL lives in
``test_structured_evidence_replay_live.py``; this module is the in-process half.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from fin.host.evidence_contract import (
    ACCESS_DENIED,
    CONTRACT_REFUSAL_CODES,
    EVIDENCE_EXPIRED,
    EVIDENCE_UNAVAILABLE,
    MALFORMED_EVIDENCE_CONTRACT,
    RETRIEVE_UNAVAILABLE,
)
from fin.host.structured_evidence import build_stored_record
from fin.host.structured_evidence_admission import admit_structured_evidence
from fin.host.structured_evidence_replay import (
    GRANT_IDENTITY_CONTEXT_TOKENS,
    EvidenceReadPort,
    ReplayedEvidence,
    find_replayable_record,
    replay_stored_evidence,
)

_QUERIED_AT = "2026-10-03T00:00:00Z"
_EXPIRES_AT = "2027-10-03T00:00:00Z"
_EVIDENCE_REF = "evref-replay-0001"
_QUERY_DIGEST = "sha256:0f1e2d3c4b5a6978"
_SCOPE_BINDING = "scope-fin-tenant-7"
_READ_REVISION = "rev-118"

_BASE = {
    "schema": "iknow.structured_result",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "query_digest": _QUERY_DIGEST,
    "scope_binding": _SCOPE_BINDING,
    "read_revision": _READ_REVISION,
    "queried_at": _QUERIED_AT,
    "maintenance": {
        "maintenance_revision": "maintenance-rev-12",
        "sources_in_scope": 1,
        "published_sources": 1,
        "desired_index": {
            "pending": 0,
            "running": 0,
            "ready": 1,
            "failed": 0,
            "stale": 0,
        },
        "conflict_checks": {
            "pending": 0,
            "running": 0,
            "ready": 1,
            "failed": 0,
            "stale": 0,
        },
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    },
}

_ROW = {
    "id": "00000000-0000-4000-8000-000000000001",
    "canonical_name": "Northwind Logistics",
    "type": "organization",
    "display_support_id": "00000000-0000-4000-8000-000000000101",
    "display_disagreement": False,
    "evidence_ref": _EVIDENCE_REF,
    "evidence_expires_at": _EXPIRES_AT,
    "provenance": [
        {
            "support_id": "00000000-0000-4000-8000-000000000201",
            "source_ref": "company-source-1",
            "source_version": "source-version-1",
            "location": {
                "document_id": "example-document",
                "version": "frozen-unit-version",
                "unit": 1,
            },
            "span": {"start": 0, "end": 42},
            "extraction_ref": "extraction-revision-1",
        }
    ],
    "provenance_complete": True,
    "provenance_next_ref": None,
}

_SIBLING: dict[str, Any] = {
    **_BASE,
    "rows": [_ROW],
    "next_cursor": None,
}


def _record() -> dict[str, Any]:
    admitted = admit_structured_evidence(_SIBLING)
    assert admitted.admitted, admitted.refusal
    return admitted.as_dicts()[0]


def _snapshot() -> dict[str, Any]:
    return dict(_record()["snapshot"])


def _envelope(**overrides: Any) -> dict[str, Any]:
    """A read envelope that agrees with the frozen snapshot on every member."""
    envelope: dict[str, Any] = {
        "schema": "iknow.evidence_result",
        "schema_version": 1,
        "evidence_ref": _EVIDENCE_REF,
        "kind": "row",
        "context": {
            "query_digest": _QUERY_DIGEST,
            "scope_binding": _SCOPE_BINDING,
            "read_revision": _READ_REVISION,
        },
        "value": {"retained": "the grant's own value column"},
        "result_count": None,
        "expires_at": _EXPIRES_AT,
    }
    envelope.update(overrides)
    return envelope


class RecordingReader:
    """A one-method read port that records the references it was asked for."""

    def __init__(self, envelope: dict[str, Any] | None = None) -> None:
        self.envelope = envelope if envelope is not None else _envelope()
        self.refs: list[str] = []

    def read(self, evidence_ref: str) -> dict[str, Any]:
        self.refs.append(evidence_ref)
        return self.envelope


class RefusingReader:
    """A read port that refuses the way iknow refuses: code plus reason."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        self.refs: list[str] = []

    def read(self, evidence_ref: str) -> dict[str, Any]:
        self.refs.append(evidence_ref)

        from fin.retrieve.evidence_route import EvidenceGrantRefusal

        raise EvidenceGrantRefusal(self.code, self.reason, "seeded refusal")


# ----- the chain ------------------------------------------------------------


def test_replay_reads_the_reference_out_of_the_stored_snapshot() -> None:
    reader = RecordingReader()
    outcome = replay_stored_evidence(_record(), reader=reader)
    assert outcome.replayed is True, outcome.refusal
    # The only reference asked for is the one the record froze. Nothing derived
    # it from a subject, a query or a caller argument.
    assert reader.refs == [_EVIDENCE_REF]


def test_replay_returns_fin_s_frozen_copy_unchanged() -> None:
    outcome = replay_stored_evidence(_record(), reader=RecordingReader())
    assert outcome.replayed is True
    assert isinstance(outcome.evidence, ReplayedEvidence)
    assert outcome.evidence.snapshot == _snapshot()
    # The envelope is kept beside it, never merged into what replay serves.
    assert outcome.evidence.envelope["value"] == {
        "retained": "the grant's own value column"
    }


def test_the_read_port_has_exactly_one_method_so_replay_cannot_query() -> None:
    """One method, asserted on the PROTOCOL rather than on a test double.

    Checking a double for a missing attribute proves nothing: the double is the
    test's own object. The claim is about the type replay accepts, so it is
    asserted against that type's own members.
    """

    members = {
        name
        for name in vars(EvidenceReadPort)
        if not name.startswith("_") and name != "read"
    }
    assert members == set(), (
        f"the replay read port gained members beyond read(): {sorted(members)}"
    )
    assert "read" in vars(EvidenceReadPort)
    # And the real client satisfies it structurally, which is how the host keeps
    # handing over the private client without importing it here.
    from fin.retrieve.evidence_route import EvidenceRouteClient

    assert isinstance(EvidenceRouteClient, type)
    assert issubclass(EvidenceRouteClient, EvidenceReadPort)
    assert hasattr(EvidenceRouteClient, "read")
    assert not hasattr(EvidenceRouteClient, "query")


def test_the_module_imports_no_retrieve_query_port() -> None:
    """The structural claim, asserted rather than asserted-in-a-comment.

    Replay's namespace holds no retrieve adapter, no session and no retrieve
    port, so there is no retrieval capability in scope for it to call. Reading
    the module's own source is what makes this a fact about the import graph
    rather than about the names that happen to be bound today.
    """

    import inspect

    import fin.host.structured_evidence_replay as replay_module

    source = inspect.getsource(replay_module)
    imported: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            imported.add(stripped)
    for line in imported:
        assert "mcp_adapter" not in line, f"replay imported a retrieve adapter: {line}"
        assert "mcp_session" not in line, f"replay imported a session: {line}"
        assert "fin.retrieve.port" not in line, f"replay imported a retrieve port: {line}"
    # The EvidenceRouteClient appears only inside the except arm that normalizes
    # iknow's refusal, and only as a local import.
    for name in dir(replay_module):
        assert name not in {"MCPRetrieveAdapter", "MCPStdioSession", "RetrievePort"}


# ----- the digest comparison ------------------------------------------------


def test_a_record_whose_snapshot_no_longer_hashes_to_its_own_digest_is_refused() -> None:
    record = _record()
    record["snapshot"]["canonical_name"] = "Tampered Corp"
    reader = RecordingReader()
    outcome = replay_stored_evidence(record, reader=reader)
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT
    # Refused BEFORE any transport call: a corrupt local copy must not spend a read.
    assert reader.refs == []


def test_a_selector_that_does_not_match_its_snapshot_is_refused() -> None:
    record = _record()
    record["selector"]["result_id"] = "00000000-0000-4000-8000-0000000000ff"
    reader = RecordingReader()
    outcome = replay_stored_evidence(record, reader=reader)
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT
    assert reader.refs == []


def test_a_record_naming_no_reference_is_refused_by_shape() -> None:
    outcome = replay_stored_evidence({"snapshot": {}, "digest": "x"}, reader=RecordingReader())
    assert outcome.replayed is False
    assert outcome.evidence_ref is None
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


# ----- the refusals iknow owns ----------------------------------------------


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("access_denied", "authorization_revoked"),
        ("access_denied", "scope_not_authorized"),
        ("access_denied", "source_outside_authorized_subset"),
        (EVIDENCE_EXPIRED, "expired"),
        (EVIDENCE_UNAVAILABLE, "purged"),
        (EVIDENCE_UNAVAILABLE, "obligations_stopped"),
        ("unsupported_source_binding", "dependency_unresolved"),
        ("malformed_evidence_contract", "unknown_reference"),
    ],
)
def test_iknows_refusal_code_and_reason_both_survive(code: str, reason: str) -> None:
    outcome = replay_stored_evidence(_record(), reader=RefusingReader(code, reason))
    assert outcome.replayed is False
    assert outcome.evidence is None
    assert outcome.refusal is not None
    assert outcome.refusal.code == code
    # The reason is what keeps an expiry distinguishable from a purge to a caller
    # that only sees a shared code.
    assert outcome.refusal.reason == reason


def test_expiry_and_purge_stay_distinguishable_under_one_shared_code() -> None:
    expired = replay_stored_evidence(
        _record(), reader=RefusingReader(EVIDENCE_EXPIRED, "expired")
    )
    purged = replay_stored_evidence(
        _record(), reader=RefusingReader(EVIDENCE_UNAVAILABLE, "purged")
    )
    assert expired.refusal is not None and purged.refusal is not None
    assert expired.refusal.reason != purged.refusal.reason
    assert expired.refusal.code in CONTRACT_REFUSAL_CODES
    assert purged.refusal.code in CONTRACT_REFUSAL_CODES


def test_a_code_outside_the_closed_set_never_reaches_the_caller() -> None:
    outcome = replay_stored_evidence(
        _record(), reader=RefusingReader("backend_unavailable", "transport_down")
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == RETRIEVE_UNAVAILABLE
    assert outcome.refusal.code in CONTRACT_REFUSAL_CODES


def test_a_transport_failure_is_a_refusal_not_a_successful_replay() -> None:
    class Exploding:
        def read(self, evidence_ref: str) -> dict[str, Any]:
            raise TimeoutError("child did not answer")

    outcome = replay_stored_evidence(_record(), reader=Exploding())
    assert outcome.replayed is False
    assert outcome.evidence is None
    assert outcome.refusal is not None
    assert outcome.refusal.code == RETRIEVE_UNAVAILABLE


def test_a_local_snapshot_whose_own_deadline_passed_is_refused() -> None:
    record = _record()
    now = datetime.fromisoformat(_EXPIRES_AT.replace("Z", "+00:00")) + timedelta(seconds=1)
    outcome = replay_stored_evidence(record, reader=RecordingReader(), now=now)
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_EXPIRED


def test_a_read_does_not_renew_the_frozen_deadline() -> None:
    """Two replays across the boundary: the second still refuses.

    A read returns the grant's own ``expires_at`` unchanged, so the local clock
    is the only thing that can move a replay from served to unavailable.
    """

    before = datetime.fromisoformat(_EXPIRES_AT.replace("Z", "+00:00")) - timedelta(days=1)
    reader = RecordingReader()
    first = replay_stored_evidence(_record(), reader=reader, now=before)
    after = datetime.fromisoformat(_EXPIRES_AT.replace("Z", "+00:00")) + timedelta(days=1)
    second = replay_stored_evidence(_record(), reader=reader, now=after)
    assert first.replayed is True
    assert second.replayed is False
    assert second.refusal is not None
    assert second.refusal.code == EVIDENCE_EXPIRED
    # The envelope carried the same expired deadline both times; nothing renewed it.
    assert reader.envelope["expires_at"] == _EXPIRES_AT


# ----- the identity comparison across the boundary -------------------------


def test_a_moved_grant_deadline_is_refused() -> None:
    moved = _envelope(expires_at="2028-01-01T00:00:00Z")
    outcome = replay_stored_evidence(_record(), reader=RecordingReader(moved))
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    assert "deadline" in outcome.refusal.reason


def test_the_deadline_is_compared_as_an_instant_not_as_text() -> None:
    """The spelling the real lane sends must not refuse an honest replay.

    fin's snapshot carries the spec's whole-second ``Z`` form; the grant's
    ``timestamptz`` column reaches the read through ``Date.toISOString()`` and
    arrives with milliseconds. Same instant, different text. Comparing the text
    would refuse every honest replay, which is a check that proves nothing.
    """

    rendered = _envelope(expires_at=_EXPIRES_AT.replace("Z", ".000Z"))
    assert rendered["expires_at"] != _EXPIRES_AT  # the texts genuinely differ
    outcome = replay_stored_evidence(_record(), reader=RecordingReader(rendered))
    assert outcome.replayed is True, outcome.refusal


def test_a_deadline_moved_by_a_second_is_still_refused() -> None:
    """Instant comparison must not become lax: a real move still fails."""

    moved = _envelope(
        expires_at=(
            datetime.fromisoformat(_EXPIRES_AT.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    outcome = replay_stored_evidence(_record(), reader=RecordingReader(moved))
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE


def test_a_deadline_that_is_not_an_instant_is_refused() -> None:
    outcome = replay_stored_evidence(
        _record(), reader=RecordingReader(_envelope(expires_at="soon"))
    )
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


@pytest.mark.parametrize("token", GRANT_IDENTITY_CONTEXT_TOKENS)
def test_each_grant_time_identity_token_is_compared(token: str) -> None:
    drifted = _envelope()
    drifted["context"] = {**drifted["context"], token: "drifted-token"}
    outcome = replay_stored_evidence(_record(), reader=RecordingReader(drifted))
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    # The reason names which token drifted, and names no payload.
    assert token in outcome.refusal.reason
    assert "value" not in outcome.refusal.reason


def test_the_identity_comparison_would_not_pass_over_a_pure_recompute() -> None:
    """WHY the payload is NOT compared, stated as a failing case.

    A digest-to-digest comparison is only evidence if the two digests were
    pinned independently. The read envelope carries no digest at all and its
    context tokens are the only cross-store facts it returns, so those tokens
    are what replay compares — and each one can be moved on one side alone, which
    the parametrized case above proves. This case pins WHY there is nothing else
    to compare: a payload comparison would have to invent a digest the lane does
    not carry.
    """

    snapshot = _snapshot()
    envelope = _envelope()
    # The frozen snapshot carries no digest field of its own; the record keeps it
    # beside the snapshot (spec §"Frozen snapshot"), and the lane returns none.
    assert "digest" not in snapshot
    assert "digest" not in envelope
    # The only cross-store members the lane returns are these three and the
    # deadline, so these are the whole of what can be compared.
    assert set(envelope["context"]) == set(GRANT_IDENTITY_CONTEXT_TOKENS)


def test_an_envelope_answering_for_another_reference_is_refused() -> None:
    outcome = replay_stored_evidence(
        _record(), reader=RecordingReader(_envelope(evidence_ref="evref-other"))
    )
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_an_envelope_with_no_context_is_refused() -> None:
    outcome = replay_stored_evidence(
        _record(), reader=RecordingReader(_envelope(context="not-an-object"))
    )
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_non_object_envelope_is_refused() -> None:
    class Wrong:
        def read(self, evidence_ref: str) -> Any:
            return ["not", "an", "object"]

    outcome = replay_stored_evidence(_record(), reader=Wrong())
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


# ----- the history entry point ---------------------------------------------


def test_the_history_entry_is_found_by_the_reference_it_names() -> None:
    records = [_record()]
    found = find_replayable_record(records, _EVIDENCE_REF)
    assert found is not None
    assert found["snapshot"]["evidence_ref"] == _EVIDENCE_REF


def test_an_unknown_reference_finds_no_entry() -> None:
    assert find_replayable_record([_record()], "evref-absent") is None


def test_two_records_naming_one_reference_are_not_resolved_by_first_match() -> None:
    first = _record()
    second = _record()
    second["snapshot"] = copy.deepcopy(second["snapshot"])
    # Both name the same reference; picking either would be a substitution.
    assert first["snapshot"]["evidence_ref"] == second["snapshot"]["evidence_ref"]
    assert find_replayable_record([first, second], _EVIDENCE_REF) is None


def test_a_corrupt_record_is_still_findable_so_the_refusal_can_name_it() -> None:
    """A record that fails projection must still be LOCATED.

    Reading the selector through the projector would make the row an
    invalidation most needs to scrub unreachable, and here it would leave a
    replay unable to say which reference it refused.
    """

    corrupt = _record()
    corrupt["snapshot"]["canonical_name"] = "Tampered Corp"
    found = find_replayable_record([corrupt], _EVIDENCE_REF)
    assert found is not None
    outcome = replay_stored_evidence(found, reader=RecordingReader())
    assert outcome.replayed is False
    # The refusal still names the reference that was asked about.
    assert outcome.evidence_ref == _EVIDENCE_REF


# ----- the closed sets ------------------------------------------------------


def test_every_code_this_module_can_emit_is_in_the_closed_set() -> None:
    for code in (
        EVIDENCE_EXPIRED,
        EVIDENCE_UNAVAILABLE,
        MALFORMED_EVIDENCE_CONTRACT,
        RETRIEVE_UNAVAILABLE,
    ):
        assert code in CONTRACT_REFUSAL_CODES


def test_the_replay_refusal_vocabulary_is_not_widened() -> None:
    """The chain's refusals must be exactly the four it names.

    A fifth code here would be a new member of a published vocabulary, which is
    the one thing this module is not allowed to do.
    """

    seen: set[str] = set()

    def note(outcome: Any) -> None:
        if outcome.refusal is not None:
            seen.add(outcome.refusal.code)

    note(replay_stored_evidence({"snapshot": {}}, reader=RecordingReader()))
    tampered = _record()
    tampered["snapshot"]["canonical_name"] = "Tampered Corp"
    note(replay_stored_evidence(tampered, reader=RecordingReader()))
    note(replay_stored_evidence(_record(), reader=RecordingReader(_envelope(context=None))))
    note(
        replay_stored_evidence(
            _record(), reader=RecordingReader(_envelope(expires_at="2028-01-01T00:00:00Z"))
        )
    )
    note(replay_stored_evidence(_record(), reader=RefusingReader("access_denied", "revoked")))
    late = datetime.fromisoformat(_EXPIRES_AT.replace("Z", "+00:00")) + timedelta(days=1)
    note(replay_stored_evidence(_record(), reader=RecordingReader(), now=late))
    # Exactly the codes the chain names: three of fin's own, plus the
    # authorization code iknow chose, passed through unchanged.
    assert seen == {
        MALFORMED_EVIDENCE_CONTRACT,
        EVIDENCE_UNAVAILABLE,
        EVIDENCE_EXPIRED,
        ACCESS_DENIED,
    }