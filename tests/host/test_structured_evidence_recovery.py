"""EP10 recovery sweep: the gate, the revalidation, and the scrub that follows.

Each case pins one property the sweep could lose without any test going red:

* a gap closes a DURABLE gate, and a fresh store instance still sees it closed
  (the restart the ticket is about);
* a withheld reference is unservable, carries no snapshot, and costs zero
  transport calls;
* revalidation asks the STORE per reference, so a revocation is observed rather
  than replayed from fin's own frozen copy;
* only the two invalidation verdicts are scrubbed — a mis-bound port must not be
  able to delete the corpus;
* the gate opens only after storage confirmed the deletion;
* nothing in the sweep advances an acknowledgement, and a backend that does not
  answer leaves recovery incomplete.

The real-storage half of this is ``test_structured_evidence_recovery_live.py``;
this module is the in-process half.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from fin.host.evidence.evidence_contract import (
    ACCESS_DENIED,
    EVIDENCE_EXPIRED,
    EVIDENCE_UNAVAILABLE,
    MALFORMED_EVIDENCE_CONTRACT,
    RETRIEVE_UNAVAILABLE,
)
from fin.host.evidence.structured_evidence import build_stored_record
from fin.host.evidence.structured_evidence_admission import admit_structured_evidence
from fin.host.evidence.structured_evidence_invalidation import (
    StructuredInvalidationRefusal,
    drain_invalidation_batch,
)
from fin.host.evidence.structured_evidence_recovery import (
    DENIED,
    UNREADABLE,
    UNRESOLVED,
    recovery_is_withheld,
    replay_retained_reference,
    revalidate_retained_references,
    sweep_recovered_evidence,
    withhold_retained_references,
)
from fin.host.evidence.structured_evidence_replay import find_replayable_record
from fin.host.thread_store import (
    EvidenceScrubOutcome,
    InMemoryThreadStore,
    RunRecord,
    ThreadStore,
)
from invalidation_batch import build_batch, build_event

_CONSUMER = "fin-p2i3-consumer"
_NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
_QUERIED_AT = "2026-10-03T00:00:00Z"
_EXPIRES_AT = "2027-10-03T00:00:00Z"
_PAST_EXPIRY = "2026-10-01T00:00:00Z"
_QUERY_DIGEST = "sha256:0f1e2d3c4b5a6978"
_SCOPE_BINDING = "scope-fin-tenant-7"
_READ_REVISION = "rev-118"

LIVE_REF = "evref-recovery-live"
REVOKED_REF = "evref-recovery-revoked"
PURGED_REF = "evref-recovery-purged"
EXPIRED_REF = "evref-recovery-expired"
FOREIGN_REF = "evref-recovery-foreign"
BROKEN_REF = "evref-recovery-unreachable"

_BASE: dict[str, Any] = {
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
        "desired_index": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "conflict_checks": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    },
}


def _record(evidence_ref: str, *, expires_at: str = _EXPIRES_AT) -> dict[str, Any]:
    """One admitted, digest-bearing stored record naming ``evidence_ref``."""

    sibling: dict[str, Any] = {
        **_BASE,
        "rows": [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "canonical_name": "Northwind Logistics",
                "type": "organization",
                "display_support_id": "00000000-0000-4000-8000-000000000101",
                "display_disagreement": False,
                "evidence_ref": evidence_ref,
                "evidence_expires_at": expires_at,
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
        ],
        "next_cursor": None,
    }
    admitted = admit_structured_evidence(sibling)
    assert admitted.admitted, admitted.refusal
    return admitted.as_dicts()[0]


def _envelope(evidence_ref: str, *, expires_at: str = _EXPIRES_AT) -> dict[str, Any]:
    return {
        "schema": "iknow.evidence_result",
        "schema_version": 1,
        "evidence_ref": evidence_ref,
        "kind": "row",
        "context": {
            "query_digest": _QUERY_DIGEST,
            "scope_binding": _SCOPE_BINDING,
            "read_revision": _READ_REVISION,
        },
        "value": {"retained": "the grant's own value column"},
        "result_count": None,
        "expires_at": expires_at,
    }


class StoreReader:
    """A read port whose verdict per reference is seeded, not asserted.

    This is the shape of the real thing: the answer comes from the store, and
    the port only carries it. ``calls`` is the record of what was actually
    asked, which is how "no transport call happened" becomes checkable.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers: dict[str, Any] = answers or {}
        self.calls: list[str] = []

    def read(self, evidence_ref: str) -> dict[str, Any]:
        self.calls.append(evidence_ref)
        answer = self.answers.get(evidence_ref, _envelope(evidence_ref))
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return dict(answer(evidence_ref))
        return dict(answer)


def _refusal(code: str, reason: str) -> Any:
    from fin.retrieve.evidence_route import EvidenceGrantRefusal

    return EvidenceGrantRefusal(code, reason, "seeded store refusal")


def _store(*records: dict[str, Any], threads: int = 1) -> InMemoryThreadStore:
    """An in-memory store holding ``records`` across ``threads`` threads."""

    store = InMemoryThreadStore()
    for index in range(threads):
        thread_id = f"thread-{index}"
        store.create_thread(id=thread_id, title=f"thread {index}")
        store.append_run(
            RunRecord(
                id=f"run-{index}",
                thread_id=thread_id,
                question="Which organizations are in scope?",
                status="done",
                status_reason="",
                answer="Northwind Logistics.",
                structured_evidence=[copy.deepcopy(r) for r in records],
            )
        )
    return store


def _expired_record(evidence_ref: str) -> dict[str, Any]:
    """A stored record whose own deadline has already passed.

    Built by re-digesting a snapshot with a past ``expires_at`` rather than by
    mutating a live record: admission refuses a pre-expired row, and a record
    whose snapshot no longer hashes to its own digest would be refused at a
    different (and unrelated) check. This one is a faithful stored record that
    has simply aged out.
    """

    snapshot = _record(evidence_ref)["snapshot"]
    snapshot["expires_at"] = _PAST_EXPIRY
    return build_stored_record(snapshot)


def _seed_thread(store: ThreadStore, thread_id: str, entries: list[dict[str, Any]]) -> None:
    """Write ``entries`` into a fresh thread.

    Through ``append_run`` rather than by mutating a ``list_runs`` result: the
    store hands back deep copies, so a mutated return value would never reach
    the state the sweep reads.
    """

    store.create_thread(id=thread_id, title=thread_id)
    store.append_run(
        RunRecord(
            id=f"run-{thread_id}",
            thread_id=thread_id,
            question="Which organizations are in scope?",
            status="done",
            status_reason="",
            answer="Northwind Logistics.",
            structured_evidence=copy.deepcopy(entries),
        )
    )


def _retained(store: ThreadStore) -> list[dict[str, Any]]:
    """Every retained entry, read back through a FRESH store instance.

    A new object over the same state stands in for a restart, which is the
    boundary T33 is about and the one a fixture otherwise hides.
    """

    out: list[dict[str, Any]] = []
    for thread in store.list_threads():
        for run in store.list_runs(thread.id):
            out.extend(run.structured_evidence)
    return out


def _gap_batch() -> dict[str, Any]:
    return build_batch(next_checkpoint="ckpt-after-gap", gap=True)


def _event(seq: int, evidence_ref: str) -> dict[str, Any]:
    return build_event(evidence_ref, seq=seq, occurred_at="2026-10-03T00:00:00Z")


def _drain(
    store: ThreadStore, payload: Any, *, acknowledged: list[str] | None = None
) -> Any:
    return drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER,
        fetch=lambda cursor: payload,
        acknowledge=(acknowledged.append if acknowledged is not None else (lambda c: None)),
    )


# ----- 1. the gate is closed on a gap, and it is durable ---------------------


def test_a_gap_closes_the_recovery_gate() -> None:
    store = _store(_record(LIVE_REF))
    assert recovery_is_withheld(store, _CONSUMER) is False

    with pytest.raises(StructuredInvalidationRefusal) as caught:
        _drain(store, _gap_batch())
    assert caught.value.code == RETRIEVE_UNAVAILABLE

    # The refusal is not the whole behaviour: the retained evidence is now
    # withheld, which is the decision the sweep depends on.
    state = store.read_recovery_state(_CONSUMER)
    assert state.withheld is True
    assert "gap=True" in state.reason


def test_the_gate_survives_a_restart() -> None:
    """A fresh store instance over the same state still reads it closed.

    This is the property a process-local flag would fail, and it is the case
    T33 exists for: restarting after a gap must not re-expose what the gate was
    withholding.
    """

    store = _store(_record(LIVE_REF))
    with pytest.raises(StructuredInvalidationRefusal):
        _drain(store, _gap_batch())

    # A different object, no shared state dict — the analogue of a new process
    # reading the durable row.
    restarted = InMemoryThreadStore()
    restarted._invalidation_checkpoints = store._invalidation_checkpoints  # noqa: SLF001
    restarted._recovery_states = store._recovery_states  # noqa: SLF001
    restarted._threads = store._threads  # noqa: SLF001
    assert recovery_is_withheld(restarted, _CONSUMER) is True


def test_a_gap_never_advances_the_acknowledgement() -> None:
    """The stale position stays put: a gap is a reason to stop, not to move."""

    store = _store(_record(LIVE_REF))
    acknowledged: list[str] = []
    with pytest.raises(StructuredInvalidationRefusal):
        _drain(
            store,
            build_batch(_event(1, LIVE_REF), next_checkpoint="ckpt-1", gap=True),
            acknowledged=acknowledged,
        )
    assert acknowledged == []
    checkpoint = store.read_invalidation_checkpoint(_CONSUMER)
    assert checkpoint.acked_checkpoint == ""
    assert checkpoint.applied_checkpoint == ""
    assert checkpoint.last_seq == 0


def test_a_malformed_batch_does_not_close_the_gate() -> None:
    """Only a continuity failure opens recovery.

    A contract violation is a different failure: the batch never described a
    position at all, so nothing was missed and there is nothing to recover. A
    gate that opened on one would withhold the whole corpus because a producer
    sent a bad key.
    """

    store = _store(_record(LIVE_REF))
    with pytest.raises(StructuredInvalidationRefusal) as caught:
        # ``gap`` is missing, so the envelope does not validate.
        _drain(
            store,
            {
                key: value
                for key, value in build_batch(next_checkpoint="ckpt-1").items()
                if key != "gap"
            },
        )
    assert caught.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert recovery_is_withheld(store, _CONSUMER) is False


def test_a_successful_drain_does_not_clear_a_closed_gate() -> None:
    """Scrubbing and acknowledging a real batch must not open the gate.

    Only the sweep releases it. A drain that could clear the gate would re-expose
    whatever the gap was withholding the moment the stream happened to resume,
    which is the exposure T33 forbids.
    """

    store = _store(_record(LIVE_REF))
    with pytest.raises(StructuredInvalidationRefusal):
        _drain(store, _gap_batch())
    assert recovery_is_withheld(store, _CONSUMER) is True

    outcome = _drain(
        store,
        build_batch(_event(1, LIVE_REF), next_checkpoint="ckpt-1"),
    )
    assert outcome.events == 1
    assert store.read_invalidation_checkpoint(_CONSUMER).acked_checkpoint == "ckpt-1"
    assert recovery_is_withheld(store, _CONSUMER) is True


# ----- 2. a withheld reference is unservable --------------------------------


def test_a_withheld_reference_is_unavailable_with_no_snapshot() -> None:
    store = _store(_record(LIVE_REF))
    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader()

    outcome = replay_retained_reference(
        store, _record(LIVE_REF), consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    # No snapshot on the refusal, so a withheld citation cannot be rendered as
    # a successful empty one.
    assert outcome.evidence is None
    # And the gate is honoured BEFORE the transport, so a withheld consumer
    # spends no read to learn it is withheld.
    assert reader.calls == []


def test_the_gate_is_read_for_the_consumer_that_asked() -> None:
    """Both directions, because either alone is satisfied by a constant.

    A gate that ignored its ``consumer_id`` and read some fixed consumer's state
    would still let consumer B through, so asserting only that is no evidence at
    all. The withheld side is the one that distinguishes them.
    """

    store = _store(_record(LIVE_REF))
    withhold_retained_references(store, _CONSUMER, "gap")
    other = "fin-p2i3-other-consumer"
    reader = StoreReader()

    withheld = replay_retained_reference(
        store, _record(LIVE_REF), consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert withheld.replayed is False, "the gate was not read for the consumer that asked"
    assert withheld.refusal is not None
    assert withheld.refusal.code == EVIDENCE_UNAVAILABLE

    # A different consumer is not mid-recovery, so the same record serves.
    outcome = replay_retained_reference(
        store, _record(LIVE_REF), consumer_id=other, reader=reader, now=_NOW
    )
    assert outcome.replayed is True, outcome.refusal
    assert reader.calls == [LIVE_REF]


# ----- 3. revalidation against the current authorized binding --------------


def test_revalidation_asks_the_store_about_every_retained_reference() -> None:
    store = _store(_record(LIVE_REF), _record(REVOKED_REF), threads=2)
    reader = StoreReader()

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert sorted(reader.calls) == sorted([LIVE_REF, REVOKED_REF])
    assert sorted(v.evidence_ref for v in report.verdicts) == sorted(
        [LIVE_REF, REVOKED_REF]
    )


def test_a_revoked_grant_is_denied_not_replayed_from_fin_s_own_copy() -> None:
    """The verdict comes from the store's live authorization, not the snapshot.

    The stored record is byte-identical to the live one — same digest, same
    grant-time tokens, same deadline. The ONLY difference is that the scope's
    authorization revision has moved, which fin's frozen copy cannot observe. So
    this case is the one that distinguishes the ticket's "current" from the
    weaker grant-time comparison.
    """

    store = _store(_record(REVOKED_REF))
    reader = StoreReader(
        {REVOKED_REF: _refusal(ACCESS_DENIED, "authorization_revoked")}
    )

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert report.denied == (REVOKED_REF,)
    assert report.verified == ()
    assert report.verdicts[0].code == ACCESS_DENIED
    assert report.verdicts[0].reason == "authorization_revoked"


def test_a_purged_grant_is_denied() -> None:
    store = _store(_record(PURGED_REF))
    reader = StoreReader({PURGED_REF: _refusal(EVIDENCE_UNAVAILABLE, "purged")})

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert report.denied == (PURGED_REF,)


def test_a_mis_bound_port_cannot_delete_the_corpus() -> None:
    """``scope_not_authorized`` is about WHO IS ASKING, so it is not scrubbed.

    It arrives as ``access_denied``, the same code as a revocation. Scrubbing on
    the code would let a sweep handed the wrong binding delete every payload it
    touched, which is data loss wearing recovery's clothes. The reference stays
    retained and stays unservable.
    """

    store = _store(_record(FOREIGN_REF))
    reader = StoreReader({FOREIGN_REF: _refusal(ACCESS_DENIED, "scope_not_authorized")})

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert report.denied == ()
    assert [v.state for v in report.verdicts] == [UNREADABLE]

    withhold_retained_references(store, _CONSUMER, "gap")
    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is True
    assert outcome.denied == ()
    # Retained, and therefore still findable — it was not deleted.
    assert find_replayable_record(_retained(store), FOREIGN_REF) is not None
    # But it is still not servable: the store refuses it on every read.
    replay = replay_retained_reference(
        store,
        find_replayable_record(_retained(store), FOREIGN_REF),
        consumer_id=_CONSUMER,
        reader=reader,
        now=_NOW,
    )
    assert replay.replayed is False
    assert replay.evidence is None


def test_an_expired_reference_is_retained_but_not_servable() -> None:
    """Expiry is not an invalidation, so T34's retention is left alone."""

    store = _store(_expired_record(EXPIRED_REF))
    reader = StoreReader()

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert [v.state for v in report.verdicts] == [UNREADABLE]
    assert report.denied == ()
    assert find_replayable_record(_retained(store), EXPIRED_REF) is not None


def test_an_unanswering_route_leaves_the_reference_unresolved() -> None:
    store = _store(_record(BROKEN_REF))
    reader = StoreReader({BROKEN_REF: _refusal(RETRIEVE_UNAVAILABLE, "backend down")})

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert [v.state for v in report.verdicts] == [UNRESOLVED]
    assert report.unresolved == (BROKEN_REF,)


def test_one_reference_is_asked_once_even_when_several_runs_retain_it() -> None:
    store = _store(_record(LIVE_REF), _record(LIVE_REF), threads=2)
    reader = StoreReader()

    report = revalidate_retained_references(store, reader=reader, now=_NOW)
    assert reader.calls == [LIVE_REF]
    assert len(report.verdicts) == 1


def test_an_entry_naming_no_reference_is_counted_not_swept() -> None:
    store = _store(_record(LIVE_REF))
    _seed_thread(
        store,
        "thread-unaddressable",
        [{"snapshot": {}, "digest": "sha256:x", "selector": {}}],
    )

    report = revalidate_retained_references(store, reader=StoreReader(), now=_NOW)
    assert report.unaddressable == 1
    # It cannot be found by any reference, so it can never be served either.
    assert find_replayable_record(_retained(store), LIVE_REF) is not None


# ----- 4. scrub the denied ones, then expose --------------------------------


def test_a_sweep_scrubs_the_denied_and_releases_the_gate() -> None:
    store = _store(_record(LIVE_REF), _record(REVOKED_REF), _record(PURGED_REF))
    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader(
        {
            REVOKED_REF: _refusal(ACCESS_DENIED, "authorization_revoked"),
            PURGED_REF: _refusal(EVIDENCE_UNAVAILABLE, "purged"),
        }
    )

    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is True
    assert sorted(outcome.denied) == sorted([REVOKED_REF, PURGED_REF])
    assert outcome.scrubbed_entries == 2
    assert outcome.scrubbed_runs == 1
    assert outcome.examined == 3

    # The denied payloads are gone, so the records naming them cannot be found.
    retained = _retained(store)
    assert [record["snapshot"]["evidence_ref"] for record in retained] == [LIVE_REF]
    assert find_replayable_record(retained, REVOKED_REF) is None
    assert find_replayable_record(retained, PURGED_REF) is None

    # The surviving reference is servable again, and only now.
    assert recovery_is_withheld(store, _CONSUMER) is False
    replay = replay_retained_reference(
        store,
        find_replayable_record(retained, LIVE_REF),
        consumer_id=_CONSUMER,
        reader=reader,
        now=_NOW,
    )
    assert replay.replayed is True, replay.refusal
    assert replay.evidence is not None
    assert replay.evidence.snapshot["evidence_ref"] == LIVE_REF


class UnconfirmingStore(InMemoryThreadStore):
    """A store whose scrub cannot be backed, as a real one might not be.

    A subclass rather than a wrapper, so the sweep's enumeration, the durable
    gate and the checkpoints are the REAL ones; only the confirmation read is
    falsified. The delete still happens, which is what makes this the dangerous
    case: the payloads are gone but storage reports it cannot prove it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.scrub_calls: list[tuple[str, ...]] = []

    def scrub_retained_evidence_payloads(
        self, evidence_refs: Any
    ) -> EvidenceScrubOutcome:
        refs = list(evidence_refs)
        self.scrub_calls.append(tuple(refs))
        super().scrub_retained_evidence_payloads(refs)
        return EvidenceScrubOutcome(
            scrubbed_entries=len(refs), scrubbed_runs=1, remaining_entries=1
        )


def test_the_gate_stays_shut_when_storage_cannot_confirm_the_scrub() -> None:
    """A scrub fin cannot back must not be followed by an open gate."""

    store = UnconfirmingStore()
    _seed_thread(
        store, "thread-unconfirmed", [_record(LIVE_REF), _record(REVOKED_REF)]
    )
    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader(
        {REVOKED_REF: _refusal(ACCESS_DENIED, "authorization_revoked")}
    )

    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    assert "still holds 1 payload" in outcome.refusal.reason
    # The scrub was attempted for exactly the denied set, and nothing else.
    assert store.scrub_calls == [(REVOKED_REF,)]
    assert recovery_is_withheld(store, _CONSUMER) is True


def test_an_unanswering_backend_leaves_recovery_incomplete() -> None:
    """The gate is the ticket's answer to a backend that is not there."""

    store = _store(_record(LIVE_REF), _record(BROKEN_REF))
    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader({BROKEN_REF: _refusal(RETRIEVE_UNAVAILABLE, "backend down")})

    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == RETRIEVE_UNAVAILABLE
    # A decided denial is still scrubbed even while another reference is
    # unresolved: deleting a payload the store has revoked is safe in any world.
    assert outcome.denied == ()
    assert recovery_is_withheld(store, _CONSUMER) is True
    assert replay_retained_reference(
        store, _record(LIVE_REF), consumer_id=_CONSUMER, reader=reader, now=_NOW
    ).replayed is False


def test_a_sweep_with_no_recovery_in_progress_refuses_and_scrubs_nothing() -> None:
    """No gap, no sweep: a sweep scrubs, so it may not be run casually."""

    store = _store(_record(REVOKED_REF))
    reader = StoreReader(
        {REVOKED_REF: _refusal(ACCESS_DENIED, "authorization_revoked")}
    )

    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is False
    assert outcome.refusal is not None
    # Not even the read was attempted.
    assert reader.calls == []
    assert find_replayable_record(_retained(store), REVOKED_REF) is not None


def test_a_sweep_never_advances_the_acknowledgement() -> None:
    """The stream position stays owed to the drain; the sweep has no such power.

    This is the whole of "stale checkpoints and unavailable backends never
    advance acknowledgement" for the sweep: it writes no watermark column and
    calls no acknowledgement, so there is no statement here that can move one.
    """

    store = _store(_record(LIVE_REF), _record(REVOKED_REF))
    acknowledged: list[str] = []
    _drain(
        store,
        build_batch(_event(1, LIVE_REF), next_checkpoint="ckpt-1"),
        acknowledged=acknowledged,
    )
    before = store.read_invalidation_checkpoint(_CONSUMER)
    assert before.acked_checkpoint == "ckpt-1"
    assert before.last_seq == 1

    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader(
        {REVOKED_REF: _refusal(ACCESS_DENIED, "authorization_revoked")}
    )
    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is True

    after = store.read_invalidation_checkpoint(_CONSUMER)
    assert after.acked_checkpoint == before.acked_checkpoint
    assert after.applied_checkpoint == before.applied_checkpoint
    assert after.last_seq == before.last_seq
    # The earlier acknowledgement was never re-sent either.
    assert acknowledged == ["ckpt-1"]


def test_the_sweep_covers_every_thread_not_just_the_one_in_hand() -> None:
    """Withholding is global, so the sweep that releases it must be global too.

    A sweep that only examined one thread would leave the others' denied
    payloads in place and then open the gate over them.
    """

    store = _store(_record(REVOKED_REF), threads=1)
    # A second thread, seeded directly, standing in for a run written earlier.
    store.create_thread(id="thread-elsewhere", title="elsewhere")
    store.append_run(
        RunRecord(
            id="run-elsewhere",
            thread_id="thread-elsewhere",
            question="q",
            status="done",
            status_reason="",
            answer="a",
            structured_evidence=[_record(PURGED_REF)],
        )
    )
    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader(
        {
            REVOKED_REF: _refusal(ACCESS_DENIED, "authorization_revoked"),
            PURGED_REF: _refusal(EVIDENCE_UNAVAILABLE, "purged"),
        }
    )

    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is True
    assert sorted(outcome.denied) == sorted([REVOKED_REF, PURGED_REF])
    assert outcome.scrubbed_runs == 2
    assert _retained(store) == []


def test_an_empty_corpus_completes_recovery_without_a_scrub() -> None:
    """Nothing retained, nothing to decide: recovery completes honestly."""

    store = _store()
    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader()

    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is True
    assert outcome.examined == 0
    assert outcome.scrubbed_entries == 0
    assert reader.calls == []


def test_the_sweep_refuses_an_empty_consumer_id() -> None:
    """The consumer id is validated, so a sweep cannot run unowned."""

    store = _store(_record(LIVE_REF))
    outcome = sweep_recovered_evidence(
        store, consumer_id="  ", reader=StoreReader(), now=_NOW
    )
    assert outcome.released is False
    assert outcome.refusal is not None
    assert "consumer_id" in outcome.refusal.reason


def test_a_retained_record_that_fails_its_own_digest_is_not_served() -> None:
    """A mutated stored snapshot is refused before any transport call.

    The sweep must not be a way around the digest comparison: a record that no
    longer hashes to its own digest is unreadable, and no sweep verdict can make
    it servable.
    """

    store = _store()
    tampered = copy.deepcopy(_record(LIVE_REF))
    tampered["snapshot"]["canonical_name"] = "Tampered Corp"
    _seed_thread(store, "thread-tampered", [tampered])

    withhold_retained_references(store, _CONSUMER, "gap")
    reader = StoreReader()
    outcome = sweep_recovered_evidence(
        store, consumer_id=_CONSUMER, reader=reader, now=_NOW
    )
    assert outcome.released is True
    # Refused at fin's own digest check, so the route was never asked.
    assert reader.calls == []
    assert find_replayable_record(_retained(store), LIVE_REF) is not None
    replay = replay_retained_reference(
        store,
        tampered,
        consumer_id=_CONSUMER,
        reader=reader,
        now=_NOW,
    )
    assert replay.replayed is False
    assert replay.evidence is None


def test_the_recovery_state_carries_its_reason_and_clears_it_on_release() -> None:
    """The reason is recorded for operators and dropped when the gate opens."""

    store = _store(_record(LIVE_REF))
    withhold_retained_references(store, _CONSUMER, "the stream skipped a window")
    state = store.read_recovery_state(_CONSUMER)
    assert state.withheld is True
    assert state.reason == "the stream skipped a window"

    store.set_recovery_released(_CONSUMER)
    released = store.read_recovery_state(_CONSUMER)
    assert released.withheld is False
    assert released.reason == ""


def test_a_never_drained_consumer_is_not_withheld() -> None:
    """"Never seen a gap" is the normal state and must not read as mid-recovery."""

    store = _store(_record(LIVE_REF))
    state = store.read_recovery_state("fin-p2i3-never-drained")
    assert state.withheld is False
    assert state.reason == ""


# ----- 5. the serialized shape is what the durable store must read back -----


def test_the_gate_travels_as_a_checkpoint_row_a_second_store_can_read() -> None:
    """The durable columns are named here, so a schema drift fails this test.

    The in-memory store proves behaviour and cannot see SQL. These assertions
    read the Postgres source instead, because a rename that left the in-memory
    path green would reach the drain on a real database and fail there — in
    production, on the gap path, which is the worst place to find out.
    """

    from fin.host import thread_store as store_module

    schema = store_module._SCHEMA_SQL  # noqa: SLF001
    assert "recovery_withheld BOOLEAN NOT NULL DEFAULT FALSE" in schema
    assert "recovery_reason TEXT NOT NULL DEFAULT ''" in schema
    # Both live on the watermark row, so the invariant is checkable in one place.
    assert schema.count("ALTER TABLE fin_evidence_invalidation_checkpoint") == 2


def test_no_checkpoint_write_mentions_the_recovery_columns() -> None:
    """Scrubbing or acknowledging must not silently reopen a closed gate.

    Checked on the source of both watermark writers: neither statement may name
    a recovery column, so ``ON CONFLICT DO UPDATE`` leaves the gate exactly as
    it found it. A drain that could clear the gate would re-expose whatever the
    gap was withholding the moment the stream happened to resume.
    """

    import inspect

    from fin.host import thread_store as store_module

    writers = inspect.getsource(
        store_module.PostgresThreadStore._upsert_checkpoint  # noqa: SLF001
    ) + inspect.getsource(
        store_module.PostgresThreadStore.advance_invalidation_acknowledgement
    )
    assert writers, "the two watermark writers were not found"
    assert "recovery_withheld" not in writers
    assert "recovery_reason" not in writers
