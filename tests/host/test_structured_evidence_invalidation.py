"""EP9 scrub-before-ack and EP10 checkpoint recovery, on the in-memory store.

These cases pin the ORDERING the lane exists for: the payload is gone from
storage before the acknowledgement is sent, and a failure anywhere before
that leaves the checkpoint un-advanced so the next drain retries instead of
skipping. The live-JSONB half of the same contract (real Postgres, a second
connection reading the column the scrub wrote) is in
``test_structured_evidence_live_pg.py``; an in-memory pass is not that proof.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from fin.host.evidence.evidence_contract import (
    CONTRACT_REFUSAL_CODES,
    EVIDENCE_UNAVAILABLE,
    MALFORMED_EVIDENCE_CONTRACT,
    RETRIEVE_UNAVAILABLE,
    UNSUPPORTED_EVIDENCE_CONTRACT,
)
from fin.host.evidence.structured_evidence import (
    build_stored_record,
    structured_snapshots_of,
)
from fin.host.evidence.structured_evidence_invalidation import (
    INVALIDATION_EVENT_FIELDS,
    MAX_EVIDENCE_REF_CHARS,
    MAX_INVALIDATION_EVENTS,
    MAX_INVALIDATION_TOKEN_CHARS,
    StructuredInvalidationRefusal,
    drain_invalidation_batch,
)
from fin.host.thread_store import (
    EvidenceScrubOutcome,
    InMemoryThreadStore,
    PersistenceUnavailableError,
    RunRecord,
)

from invalidation_batch import (
    EVIDENCE_RESULT_ENVELOPE,
    INVALIDATION_SUCCESS_FIELDS,
    build_batch,
    build_envelope,
    build_event,
)

_CONSUMER_ID = "fin-default"
_SEQ = [0]

_BASE = {
    "schema": "iknow.structured_result",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "query_digest": "sha256:0f1e2d3c",
    "scope_binding": "scope-fin-tenant-7",
    "read_revision": "rev-118",
    "queried_at": "2026-10-03T00:00:00Z",
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

_ROW = {
    "id": "00000000-0000-4000-8000-000000000001",
    "canonical_name": "Northwind Logistics",
    "type": "organization",
    "display_support_id": "00000000-0000-4000-8000-000000000101",
    "display_disagreement": False,
    "evidence_ref": "evref-0001",
    "evidence_expires_at": "2027-10-03T00:00:00Z",
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


def _stored(evidence_ref: str) -> dict[str, Any]:
    """One stored record whose snapshot carries ``evidence_ref``."""
    row = {**_ROW, "evidence_ref": evidence_ref}
    payload = {**_BASE, "rows": [row], "next_cursor": None}
    snapshot = structured_snapshots_of(payload)[0]
    return build_stored_record(snapshot)


def _record(run_id: str, thread_id: str, *refs: str) -> RunRecord:
    return RunRecord(
        id=run_id,
        thread_id=thread_id,
        question="who is the counterparty",
        status="done",
        structured_evidence=[_stored(ref) for ref in refs],
    )


def _event(
    evidence_ref: str,
    *,
    seq: int | None = None,
    reason: str = "access_revoked",
    event_id: str | None = None,
    **over: Any,
) -> dict[str, Any]:
    """One ADR 0097 §2.1 event, with ``seq`` defaulting to the next
    per-consumer value so a batch is contiguous unless a case says otherwise.

    The shape is :func:`invalidation_batch.build_event`'s; only the sequence
    bookkeeping is this suite's own, because the counter has to reset between
    cases and a shared builder cannot know when.
    """
    if seq is None:
        _SEQ[0] += 1
        seq = _SEQ[0]
    return build_event(
        evidence_ref, seq=seq, reason=reason, event_id=event_id, **over
    )


def _reset_seq() -> None:
    _SEQ[0] = 0


def _batch(
    *events: dict[str, Any],
    next_checkpoint: str = "ck-2",
    gap: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return build_batch(*events, next_checkpoint=next_checkpoint, gap=gap, **extra)


def _drain(
    store: InMemoryThreadStore,
    payload: object,
    acks: list[str],
    consumer_id: str = _CONSUMER_ID,
    fetch: Callable[[str | None], object] | None = None,
) -> Any:
    return drain_invalidation_batch(
        store,
        consumer_id=consumer_id,
        fetch=fetch if fetch is not None else (lambda _cursor: payload),
        acknowledge=acks.append,
    )


@pytest.fixture(autouse=True)
def _per_test_sequence() -> None:
    """Each case starts its stream at seq 1; a new store has no watermark."""
    _reset_seq()


# ----- empty batch -----


def test_an_empty_batch_scrubs_nothing_and_still_acknowledges() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    outcome = _drain(store, _batch(), acks)

    assert outcome.events == 0
    assert outcome.scrubbed_entries == 0
    assert acks == ["ck-2"]
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


def test_an_event_naming_a_reference_fin_never_stored_is_acknowledged() -> None:
    """Spec: a reference fin never stored can be acknowledged after
    verifying no local payload exists — it must not stall the stream."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    outcome = _drain(store, _batch(_event("evref-absent")), acks)

    assert outcome.scrubbed_entries == 0
    assert acks == ["ck-2"]
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


# ----- EP9: scrub commits before the acknowledgement -----


def test_the_payload_is_gone_from_storage_before_the_acknowledgement_is_sent() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001", "evref-0002"))
    observed: list[int] = []
    acks: list[str] = []

    def acknowledge(checkpoint: str) -> None:
        observed.append(len(store.list_runs("thread-1")[0].structured_evidence))
        acks.append(checkpoint)

    drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-0001")),
        acknowledge=acknowledge,
    )

    assert observed == [1], "the scrub must be committed before the ack is sent"
    assert acks == ["ck-2"]


def test_a_scrub_leaves_the_other_reference_and_the_rest_of_the_run_row_intact() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    original = _record("run-1", "thread-1", "evref-0001", "evref-0002")
    store.append_run(original)
    acks: list[str] = []

    _drain(store, _batch(_event("evref-0001")), acks)

    read_back = store.list_runs("thread-1")[0]
    assert [row["snapshot"]["evidence_ref"] for row in read_back.structured_evidence] == [
        "evref-0002"
    ]
    assert read_back.question == original.question
    assert read_back.answer == original.answer


def test_a_batch_scrubs_every_matching_row_across_runs() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.create_thread(id="thread-2", title="t2")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    store.append_run(_record("run-2", "thread-2", "evref-0001", "evref-0002"))
    acks: list[str] = []

    outcome = _drain(store, _batch(_event("evref-0001")), acks)

    assert outcome.scrubbed_entries == 2
    assert outcome.scrubbed_runs == 2
    assert store.list_runs("thread-1")[0].structured_evidence == []
    assert len(store.list_runs("thread-2")[0].structured_evidence) == 1


# ----- concurrent / idempotent redelivery -----


def test_redraining_the_same_event_scrubs_nothing_and_does_not_double_ack() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001"), next_checkpoint="ck-2"), acks)

    # The backend redelivers from the acknowledged cursor.
    outcome = _drain(
        store,
        _batch(_event("evref-0001"), next_checkpoint="ck-3"),
        acks,
    )

    assert outcome.scrubbed_entries == 0
    assert acks == ["ck-2", "ck-3"]
    assert store.list_runs("thread-1")[0].structured_evidence == []


def test_a_second_drain_after_the_scrub_already_converged_is_a_no_op() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001")), acks)
    assert store.list_runs("thread-1")[0].structured_evidence == []

    outcome = _drain(store, _batch(next_checkpoint="ck-3"), acks)

    assert outcome.events == 0
    assert outcome.scrubbed_entries == 0
    assert acks == ["ck-2", "ck-3"], "one acknowledgement per contiguous batch, no more"
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-3"
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).applied_checkpoint == "ck-3"


# ----- EP10: the checkpoint is durable and gap-detecting -----


def test_the_checkpoint_is_persisted_after_a_drain() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    _drain(store, _batch(_event("evref-0001")), acks)

    checkpoint = store.read_invalidation_checkpoint(_CONSUMER_ID)
    assert checkpoint.consumer_id == _CONSUMER_ID
    assert checkpoint.acked_checkpoint == "ck-2"
    assert checkpoint.applied_checkpoint == "ck-2"


def test_an_unknown_consumer_reads_back_an_empty_checkpoint() -> None:
    store = InMemoryThreadStore()
    checkpoint = store.read_invalidation_checkpoint("fin-never-drained")
    assert checkpoint.acked_checkpoint == ""
    assert checkpoint.applied_checkpoint == ""


def test_the_drain_requests_the_persisted_checkpoint_so_a_restart_resumes_there() -> None:
    """EP10 restart: the cursor comes from storage, never from the caller."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001")), acks)

    requested: list[str | None] = []

    def fetch(cursor: str | None) -> object:
        requested.append(cursor)
        return _batch(_event("evref-0002"), next_checkpoint="ck-3")

    _drain(store, None, acks, fetch=fetch)

    assert requested == ["ck-2"], "the cursor comes from storage, never from the caller"


def test_an_upstream_gap_flag_refuses_without_scrubbing_or_acknowledging() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(_event("evref-0001"), gap=True), acks)

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == []
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


def test_a_gap_leaves_the_stored_checkpoint_untouched() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001")), acks)
    before = store.read_invalidation_checkpoint(_CONSUMER_ID)

    with pytest.raises(StructuredInvalidationRefusal):
        _drain(store, _batch(_event("evref-0002"), next_checkpoint="ck-4", gap=True), acks)

    assert store.read_invalidation_checkpoint(_CONSUMER_ID) == before
    assert acks == ["ck-2"]


def test_a_cursor_that_does_not_continue_the_requested_one_refuses() -> None:
    """The batch may echo the cursor it continued from; a different one is
    a cross-consumer or out-of-band checkpoint and must refuse."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001")), acks)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(
            store,
            _batch(_event("evref-0002"), next_checkpoint="ck-4", checkpoint="ck-foreign"),
            acks,
        )

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == ["ck-2"]


def test_a_fresh_consumer_refuses_a_batch_that_echoes_a_foreign_checkpoint() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(
            store,
            _batch(_event("evref-0001"), checkpoint="ck-someone-elses"),
            acks,
        )

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == []


# ----- invalid input -----


def test_a_non_mapping_payload_is_refused() -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, ["not", "a", "mapping"], acks)
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


@pytest.mark.parametrize(
    "payload",
    [
        # a required envelope or success key is absent
        {**EVIDENCE_RESULT_ENVELOPE, "next_checkpoint": "ck-2", "gap": False},
        {**EVIDENCE_RESULT_ENVELOPE, "events": [], "gap": False},
        {**EVIDENCE_RESULT_ENVELOPE, "events": [], "next_checkpoint": "ck-2"},
        # an unknown key refuses rather than being dropped
        {
            **EVIDENCE_RESULT_ENVELOPE,
            "events": [],
            "next_checkpoint": "ck-2",
            "gap": False,
            "source_text": "leaked",
        },
        # the envelope head is of the wrong type
        {**build_envelope(schema=7), "events": [], "next_checkpoint": "ck-2", "gap": False},
        {**build_envelope(schema=None), "events": [], "next_checkpoint": "ck-2", "gap": False},
        # the success fields are of the wrong type
        {**EVIDENCE_RESULT_ENVELOPE, "events": {}, "next_checkpoint": "ck-2", "gap": False},
        {**EVIDENCE_RESULT_ENVELOPE, "events": [], "next_checkpoint": "ck-2", "gap": "false"},
        {**EVIDENCE_RESULT_ENVELOPE, "events": [], "next_checkpoint": "", "gap": False},
        {**EVIDENCE_RESULT_ENVELOPE, "events": [], "next_checkpoint": 2, "gap": False},
    ],
)
def test_a_malformed_batch_is_refused(payload: object) -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, payload, acks)
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


@pytest.mark.parametrize(
    "missing", sorted({*EVIDENCE_RESULT_ENVELOPE, *INVALIDATION_SUCCESS_FIELDS})
)
def test_an_envelope_missing_a_required_key_is_refused(missing: str) -> None:
    """The response token and its version are envelope keys
    (``specs/structured-corpus-evidence.md:218-219``), so a result that omits
    either one is not the envelope fin asked for."""
    store = InMemoryThreadStore()
    acks: list[str] = []
    payload = {key: value for key, value in _batch().items() if key != missing}

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, payload, acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


def test_a_spec_conforming_envelope_is_accepted() -> None:
    """The envelope IK actually renders: the response token and its version,
    the three success fields, and NO ``action``.

    ``specs/structured-corpus-evidence.md:218-219`` and ``:222`` make the token
    and the version required and list the success fields "beyond
    token/version/action" — the token is a required response key, not an
    optional annotation. This case is the reason the required set carries
    ``schema``: an envelope built exactly as the spec reads, minus the action no
    shipped renderer echoes, must drain.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    conforming = build_batch(_event("evref-0001"))
    assert "action" not in conforming
    assert frozenset(conforming) == {
        "schema",
        "schema_version",
        *INVALIDATION_SUCCESS_FIELDS,
    }

    outcome = _drain(store, conforming, acks)

    assert outcome.events == 1
    assert outcome.scrubbed_entries == 1
    assert acks == ["ck-2"]


def test_an_omitted_action_is_accepted_and_a_named_one_must_be_this_action() -> None:
    """``action`` is optional while no renderer echoes it, and checked while it
    does. Register §1.3 is where IK starts sending it, so a batch that already
    carries it must be answered rather than refused for carrying a key fin
    knows about."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    outcome = _drain(store, _batch(_event("evref-0001"), action="invalidations"), acks)

    assert outcome.events == 1
    assert acks == ["ck-2"]


@pytest.mark.parametrize(
    "action",
    ["ack", "read", "supports", "Invalidations", "", None, 1],
)
def test_a_batch_for_another_action_is_refused_as_malformed(action: object) -> None:
    """A batch for another action is a malformed RESPONSE to this request,
    not a newer version of it (ADR 0097 §2.7)."""
    store = InMemoryThreadStore()
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, {**_batch(), "action": action}, acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


@pytest.mark.parametrize(
    "token",
    ["iknow.structured_result", "iknow.evidence", "fin.evidence_result", ""],
)
def test_an_envelope_naming_another_shape_is_refused_as_unsupported(token: str) -> None:
    """A token names a SHAPE, so an unrecognized one is a contract this build
    does not implement (``_check_schema_token``'s split), not a malformed
    payload: never the nearest older parser."""
    store = InMemoryThreadStore()
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, build_batch(_event("evref-0001"), schema=token), acks)

    assert error.value.code == UNSUPPORTED_EVIDENCE_CONTRACT
    assert acks == []


@pytest.mark.parametrize("version", [0, 2, 99])
def test_an_envelope_at_an_unknown_schema_version_is_refused_as_unsupported(
    version: int,
) -> None:
    """ADR 0097 §2.1: version skew is a refusal, never a fallback. The version
    lives on the ``iknow.evidence_result`` envelope, so this is the only place
    it can be refused."""
    store = InMemoryThreadStore()
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, {**_batch(_event("evref-0001")), "schema_version": version}, acks)

    assert error.value.code == UNSUPPORTED_EVIDENCE_CONTRACT
    assert acks == []


@pytest.mark.parametrize(
    "version",
    ["1", None, True, 1.5, float("nan"), float("inf"), [1], {"version": 1}],
)
def test_an_envelope_version_with_no_integer_value_is_refused_as_malformed(
    version: object,
) -> None:
    """The house split (``_check_schema_version``): a version is validated as
    a finite integer BY VALUE, so a string, a boolean and a fractional number
    are malformed payloads — ``bool`` in particular, which subclasses ``int``
    and would otherwise pass as version 1."""
    store = InMemoryThreadStore()
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, {**_batch(_event("evref-0001")), "schema_version": version}, acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


def test_an_envelope_version_written_as_a_whole_float_is_the_same_version() -> None:
    """``json.loads`` decodes the JSON text ``1.0`` to a Python float while a
    JavaScript decoder yields the integer 1, so the contract validates the
    version as a VALUE: both must be version 1, or one JSON request would get
    two verdicts at two boundaries."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    outcome = _drain(store, {**_batch(_event("evref-0001")), "schema_version": 1.0}, acks)

    assert outcome.events == 1
    assert outcome.scrubbed_entries == 1
    assert acks == ["ck-2"]


def test_an_event_carrying_a_schema_version_is_refused_as_malformed() -> None:
    """The regression this change exists to fix: ADR 0097 §2.1 withdrew the
    per-event version, so an event built to the amended contract — five fields,
    the version on the envelope — must parse, and one that re-adds the key must
    be refused rather than silently accepted as a sixth field."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    withdrawn = _event("evref-0001", schema_version=1)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(withdrawn), acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert "schema_version" in error.value.reason
    assert acks == []
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


def test_an_event_carrying_the_consumer_id_is_refused_as_malformed() -> None:
    """``consumer_id`` is the sixth STORED column: it identifies the stream,
    not the event, so it is stored and never sent (ADR 0097 §2.1)."""
    store = InMemoryThreadStore()
    acks: list[str] = []

    stream = _event("evref-0001", consumer_id=_CONSUMER_ID)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(stream), acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert "consumer_id" in error.value.reason
    assert acks == []


def test_the_event_carries_exactly_the_five_fields_adr_0097_names() -> None:
    """ADR 0097 §2.1's table, closed: five fields, and neither ``schema_version``
    (withdrawn by the amendment) nor ``consumer_id`` (stored, never sent)."""
    store = InMemoryThreadStore()
    acks: list[str] = []

    outcome = _drain(store, _batch(_event("evref-0001")), acks)

    # WHY the PARSER's set and not a literal here: a literal would re-pin the
    # builder this suite hands the drain and stay green while the parser changed
    # its mind, so the case could not fail for any production change. Reading the
    # production constant makes the two sides one contract — re-add
    # ``schema_version`` to ``INVALIDATION_EVENT_FIELDS`` and this fails.
    assert frozenset(_event("evref-0001")) == INVALIDATION_EVENT_FIELDS
    # And the parser's set is itself pinned to what the ADR names, so neither
    # side can drift alone.
    assert INVALIDATION_EVENT_FIELDS == frozenset(
        {"event_id", "seq", "evidence_ref", "reason", "occurred_at"}
    )
    assert outcome.events == 1, "an event built to the amended contract must parse"
    assert acks == ["ck-2"]


@pytest.mark.parametrize(
    "over",
    [
        {"source_text": "leaked"},
        {"reason": "expired"},
        {"reason": ""},
        {"evidence_ref": ""},
        {"evidence_ref": "x" * (MAX_EVIDENCE_REF_CHARS + 1)},
        {"event_id": ""},
        {"event_id": "not-a-uuid"},
        {"event_id": "00000000-0000-1000-8000-000000000001"},
        {"event_id": 1},
        {"seq": 0},
        {"seq": -1},
        {"seq": "1"},
        {"seq": True},
        {"occurred_at": "2026-10-07T00:00:00.000Z"},
        {"occurred_at": "2026-10-07T00:00:00+00:00"},
    ],
)
def test_a_malformed_event_field_is_refused_and_nothing_is_acknowledged(
    over: dict[str, Any],
) -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    event = _event("evref-0001")
    event.update(over)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(event), acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


@pytest.mark.parametrize("event", ["not-a-mapping", None, 7, ["event_id"]])
def test_a_non_mapping_event_is_refused(event: object) -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(event), acks)
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


@pytest.mark.parametrize(
    "missing",
    sorted({"event_id", "seq", "evidence_ref", "reason", "occurred_at"}),
)
def test_an_event_missing_a_required_field_is_refused(missing: str) -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    event = {key: value for key, value in _event("evref-0001").items() if key != missing}

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(event), acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


def test_a_non_contiguous_batch_refuses_before_acknowledging() -> None:
    """P2-D1 §2.3: a batch is acknowledged only when it is contiguous."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(
            store,
            _batch(_event("evref-0001", seq=1), _event("evref-0002", seq=3)),
            acks,
        )

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == []
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


def test_a_batch_starting_past_the_applied_watermark_refuses() -> None:
    """P2-D1 §2.7: fin's own check catches a skipped window even when the
    backend reported no gap."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001", seq=1)), acks)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(_event("evref-0002", seq=7), next_checkpoint="ck-3"), acks)

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == ["ck-2"]
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).applied_checkpoint == "ck-2"


def test_a_redelivered_seq_at_or_below_the_watermark_is_accepted() -> None:
    """The crash case: the backend replays events fin already applied."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001", seq=1)), acks)
    store.advance_invalidation_acknowledgement(_CONSUMER_ID, "ck-0")

    outcome = _drain(store, _batch(_event("evref-0001", seq=1), next_checkpoint="ck-2"), acks)

    assert outcome.events == 1
    assert outcome.scrubbed_entries == 0
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).last_seq == 1


def test_a_stale_redelivery_below_the_stored_watermark_refuses() -> None:
    """A batch ending before the stored ``last_seq`` is a stale redelivery.

    ``applied_checkpoint`` is opaque, so neither store can order it — the
    watermark can only be defended on ``last_seq``, which the drain does.
    Accepting the batch would move ``applied_checkpoint`` backwards and lose
    the record that those payloads are already scrubbed.
    """
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    _drain(store, _batch(_event("evref-0001", seq=1), _event("evref-0002", seq=2)), acks)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(
            store,
            _batch(_event("evref-0001", seq=1), next_checkpoint="ck-3"),
            acks,
        )

    assert error.value.code == RETRIEVE_UNAVAILABLE
    checkpoint = store.read_invalidation_checkpoint(_CONSUMER_ID)
    assert checkpoint.applied_checkpoint == "ck-2", "the applied watermark must not regress"
    assert checkpoint.last_seq == 2
    assert acks == ["ck-2"]


def test_an_empty_consumer_is_refused() -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(), acks, consumer_id="   ")
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


# ----- overflow -----


def test_a_batch_over_its_event_bound_is_refused() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []
    events = [_event(f"evref-{index:04d}") for index in range(MAX_INVALIDATION_EVENTS + 1)]

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(*events), acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1


def test_an_over_long_evidence_ref_is_refused() -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    over = "x" * (MAX_EVIDENCE_REF_CHARS + 1)

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(_event(over)), acks)

    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert acks == []


def test_a_consumer_over_the_token_bound_is_refused() -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(), acks, consumer_id="c" * (MAX_INVALIDATION_TOKEN_CHARS + 1))
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_batch_exactly_at_the_event_bound_is_accepted() -> None:
    store = InMemoryThreadStore()
    acks: list[str] = []
    events = [_event(f"evref-{index:04d}") for index in range(MAX_INVALIDATION_EVENTS)]

    outcome = _drain(store, _batch(*events), acks)

    assert outcome.events == MAX_INVALIDATION_EVENTS
    assert acks == ["ck-2"]


# ----- exception: a failed scrub must not advance anything -----


class _ScrubFailsOnce(InMemoryThreadStore):
    """One mid-batch storage failure, then a clean store."""

    def __init__(self) -> None:
        super().__init__()
        self.scrub_calls = 0

    def scrub_structured_evidence_payloads(
        self,
        *,
        consumer_id: str,
        evidence_refs: list[str],
        next_checkpoint: str,
        last_seq: int,
    ) -> EvidenceScrubOutcome:
        self.scrub_calls += 1
        if self.scrub_calls == 1:
            raise PersistenceUnavailableError("scrub: the thread store could not be reached")
        return super().scrub_structured_evidence_payloads(
            consumer_id=consumer_id,
            evidence_refs=evidence_refs,
            next_checkpoint=next_checkpoint,
            last_seq=last_seq,
        )


def test_a_failing_scrub_leaves_the_checkpoint_un_advanced_and_does_not_acknowledge() -> None:
    store = _ScrubFailsOnce()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(_event("evref-0001", seq=1)), acks)

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == []
    checkpoint = store.read_invalidation_checkpoint(_CONSUMER_ID)
    assert checkpoint.acked_checkpoint == ""
    assert checkpoint.applied_checkpoint == ""
    assert len(store.list_runs("thread-1")[0].structured_evidence) == 1

    # The retry drains from the SAME cursor and converges.
    outcome = _drain(store, _batch(_event("evref-0001", seq=1)), acks)
    assert outcome.scrubbed_entries == 1
    assert acks == ["ck-2"]
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-2"


def test_a_crash_between_the_scrub_and_the_ack_redelivers_and_converges() -> None:
    """The scrub committed but the acknowledgement never landed: the stored
    cursor has not moved, so the backend redelivers and the retry converges
    without a second scrub and without a lost event."""
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))

    def crash(checkpoint: str) -> None:
        raise RuntimeError("transport died after the scrub committed")

    with pytest.raises(RuntimeError):
        drain_invalidation_batch(
            store,
            consumer_id=_CONSUMER_ID,
            fetch=lambda _cursor: _batch(_event("evref-0001", seq=1)),
            acknowledge=crash,
        )

    assert store.list_runs("thread-1")[0].structured_evidence == []
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == ""
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).applied_checkpoint == "ck-2"

    acks: list[str] = []
    outcome = _drain(store, _batch(_event("evref-0001", seq=1), checkpoint=None), acks)

    assert outcome.scrubbed_entries == 0, "the payload was already gone"
    assert acks == ["ck-2"]
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-2"


# ----- storage must confirm the scrub before the acknowledgement -----


class _ScrubNotConfirmed(InMemoryThreadStore):
    def scrub_structured_evidence_payloads(
        self,
        *,
        consumer_id: str,
        evidence_refs: list[str],
        next_checkpoint: str,
        last_seq: int,
    ) -> EvidenceScrubOutcome:
        return EvidenceScrubOutcome(scrubbed_entries=0, scrubbed_runs=0, remaining_entries=1)


def test_a_scrub_storage_cannot_confirm_refuses_before_any_acknowledgement() -> None:
    store = _ScrubNotConfirmed()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record("run-1", "thread-1", "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        _drain(store, _batch(_event("evref-0001")), acks)

    assert error.value.code == EVIDENCE_UNAVAILABLE
    assert acks == []
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == ""


# ----- vocabulary -----


def test_every_drain_refusal_code_is_in_the_contract_registry() -> None:
    assert {
        MALFORMED_EVIDENCE_CONTRACT,
        UNSUPPORTED_EVIDENCE_CONTRACT,
        RETRIEVE_UNAVAILABLE,
        EVIDENCE_UNAVAILABLE,
    } <= CONTRACT_REFUSAL_CODES