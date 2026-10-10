"""EP9/EP10 against real Postgres: the lane crosses the live-JSONB boundary.

An in-memory pass is not this proof. ``InMemoryThreadStore`` does not run the
durable write guard, holds its rows in Python objects, and cannot show that a
committed ``UPDATE`` on ``fin_run.structured_evidence`` is visible to a second
connection before the acknowledgement goes out — which is exactly the ordering
T32/T33 are about. These cases read the column over a SEPARATE connection and
re-open the store to stand in for a restart.

Each case runs in its own schema (the ``invalidation_live_pg_url`` fixture), so
a live run never touches another suite's tables.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import pytest

from fin.host.attachment_prep_worker import DrainCadence
from fin.host.evidence_contract import (
    EVIDENCE_UNAVAILABLE,
    RETRIEVE_UNAVAILABLE,
)
from fin.host.structured_evidence import build_stored_record, structured_snapshots_of
from fin.host.structured_evidence_drain_worker import (
    StructuredInvalidationDrainWorker,
)
from fin.host.structured_evidence_invalidation import (
    StructuredInvalidationRefusal,
    drain_invalidation_batch,
)
from fin.host.thread_store import PostgresThreadStore, RunRecord
from invalidation_batch import build_batch, build_event

# The fake clock and the recording transport are the scheduler test's own
# helpers; one definition, two suites (same sibling-import pattern
# ``tests/corpus/test_ingest_worker_durable.py`` uses for its restart harness).
from test_structured_invalidation_drain_worker import (  # noqa: E402
    _FakeClock,
    _RecordingTransport,
)

_CONSUMER_ID = "fin-live-pg"

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


@pytest.fixture
def invalidation_live_pg_url() -> Any:
    """A schema-scoped live URL, skipped when no live Postgres is configured."""
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip("FIN_CHECKPOINT_URL is not set; live Postgres drain tests are untested")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"fin_invalidation_test_{uuid.uuid4().hex}"
    scoped_url = make_conninfo(url, options=f"-c search_path={schema}")
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(scoped_url) as conn:
            assert conn.execute("SELECT current_schema()").fetchone() == (schema,)
        yield scoped_url
    finally:
        with psycopg.connect(url) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def _stored(evidence_ref: str) -> dict[str, Any]:
    row = {**_ROW, "evidence_ref": evidence_ref}
    snapshot = structured_snapshots_of({**_BASE, "rows": [row], "next_cursor": None})[0]
    return build_stored_record(snapshot)


def _record(run_id: str, thread_id: str, *refs: str) -> RunRecord:
    return RunRecord(
        id=run_id,
        thread_id=thread_id,
        question="who is the counterparty",
        status="done",
        structured_evidence=[_stored(ref) for ref in refs],
    )


def _event(evidence_ref: str, seq: int) -> dict[str, Any]:
    return build_event(evidence_ref, seq=seq)


def _batch(*events: dict[str, Any], next_checkpoint: str, gap: bool = False) -> dict[str, Any]:
    return build_batch(*events, next_checkpoint=next_checkpoint, gap=gap)


def _stored_refs(url: str, run_id: str) -> list[str]:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT elem FROM fin_run AS r, "
            "LATERAL jsonb_array_elements(r.structured_evidence) AS elem "
            "WHERE r.id = %s",
            (run_id,),
        ).fetchall()
    return [row[0]["snapshot"]["evidence_ref"] for row in rows]


def test_live_pg_invalidation_scrub_is_committed_before_the_acknowledgement(
    invalidation_live_pg_url: str,
) -> None:
    """The ordering T32 is about, read over a SECOND connection."""
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id, "evref-0001"))
    assert _stored_refs(invalidation_live_pg_url, run_id) == ["evref-0001"]

    observed: list[list[str]] = []
    acks: list[str] = []

    def acknowledge(checkpoint: str) -> None:
        observed.append(_stored_refs(invalidation_live_pg_url, run_id))
        acks.append(checkpoint)

    outcome = drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
        acknowledge=acknowledge,
    )

    assert observed == [[]], "the JSONB column must be scrubbed before the ack is sent"
    assert acks == ["ck-2"]
    assert outcome.scrubbed_entries == 1
    assert outcome.scrubbed_runs == 1
    assert _stored_refs(invalidation_live_pg_url, run_id) == []


def test_live_pg_invalidation_scrub_leaves_other_columns_and_references_intact(
    invalidation_live_pg_url: str,
) -> None:
    """Anti-corruption: the write is scoped to the evidence column."""
    import psycopg

    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    original = _record(run_id, thread_id, "evref-0001", "evref-0002")
    store.append_run(original)
    before = PostgresThreadStore(invalidation_live_pg_url).list_runs(thread_id)[0]

    drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
        acknowledge=lambda _checkpoint: None,
    )

    with psycopg.connect(invalidation_live_pg_url) as conn:
        stored = conn.execute(
            "SELECT question, answer, citations, visual_claims, structured_evidence "
            "FROM fin_run WHERE id = %s",
            (run_id,),
        ).fetchone()
    after = PostgresThreadStore(invalidation_live_pg_url).list_runs(thread_id)[0]

    assert stored[0] == before.question
    assert stored[1] == before.answer
    assert stored[2] == []
    assert stored[3] == []
    assert stored[4] == [_stored("evref-0002")]
    assert after.structured_evidence == [_stored("evref-0002")]
    assert after.id == before.id and after.created_at == before.created_at


def test_live_pg_invalidation_checkpoint_survives_a_restart_and_resumes_from_it(
    invalidation_live_pg_url: str,
) -> None:
    """EP10: a fresh store instance reads the persisted watermark back."""
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(f"run-{uuid.uuid4()}", thread_id, "evref-0001"))
    drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
        acknowledge=lambda _checkpoint: None,
    )

    restarted = PostgresThreadStore(invalidation_live_pg_url)
    checkpoint = restarted.read_invalidation_checkpoint(_CONSUMER_ID)
    assert checkpoint.consumer_id == _CONSUMER_ID
    assert checkpoint.acked_checkpoint == "ck-2"
    assert checkpoint.applied_checkpoint == "ck-2"
    assert checkpoint.last_seq == 1

    requested: list[str | None] = []

    def fetch(cursor: str | None) -> object:
        requested.append(cursor)
        return _batch(_event("evref-0002", 2), next_checkpoint="ck-3")

    outcome = drain_invalidation_batch(
        restarted, consumer_id=_CONSUMER_ID, fetch=fetch, acknowledge=lambda _c: None
    )

    assert requested == ["ck-2"], "a restart resumes from the persisted cursor"
    assert outcome.last_seq == 2
    assert restarted.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-3"


def test_live_pg_invalidation_gap_refuses_without_advancing_anything(
    invalidation_live_pg_url: str,
) -> None:
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id, "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        drain_invalidation_batch(
            store,
            consumer_id=_CONSUMER_ID,
            fetch=lambda _cursor: _batch(
                _event("evref-0001", 1), next_checkpoint="ck-2", gap=True
            ),
            acknowledge=acks.append,
        )

    assert error.value.code == RETRIEVE_UNAVAILABLE
    assert acks == []
    assert _stored_refs(invalidation_live_pg_url, run_id) == ["evref-0001"]
    stored = PostgresThreadStore(invalidation_live_pg_url).read_invalidation_checkpoint(
        _CONSUMER_ID
    )
    assert stored.acked_checkpoint == ""
    assert stored.applied_checkpoint == ""


def test_live_pg_invalidation_redrain_after_a_crash_between_scrub_and_ack_converges(
    invalidation_live_pg_url: str,
) -> None:
    """Scrub committed, acknowledgement lost: the retry must scrub nothing
    again and still land the cursor."""
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id, "evref-0001"))

    def crash(checkpoint: str) -> None:
        raise RuntimeError("transport died after the scrub committed")

    with pytest.raises(RuntimeError):
        drain_invalidation_batch(
            store,
            consumer_id=_CONSUMER_ID,
            fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
            acknowledge=crash,
        )

    restarted = PostgresThreadStore(invalidation_live_pg_url)
    assert restarted.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == ""
    assert restarted.read_invalidation_checkpoint(_CONSUMER_ID).applied_checkpoint == "ck-2"
    assert _stored_refs(invalidation_live_pg_url, run_id) == []

    acks: list[str] = []
    outcome = drain_invalidation_batch(
        restarted,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
        acknowledge=acks.append,
    )

    assert outcome.scrubbed_entries == 0
    assert acks == ["ck-2"]
    assert restarted.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == "ck-2"


def test_live_pg_invalidation_reference_fin_never_stored_is_acknowledged(
    invalidation_live_pg_url: str,
) -> None:
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id, "evref-0001"))
    acks: list[str] = []

    outcome = drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-absent", 1), next_checkpoint="ck-2"),
        acknowledge=acks.append,
    )

    assert outcome.scrubbed_entries == 0
    assert acks == ["ck-2"]
    assert _stored_refs(invalidation_live_pg_url, run_id) == ["evref-0001"]


def test_live_pg_invalidation_scrub_runs_against_a_corrupt_row_and_still_confirms(
    invalidation_live_pg_url: str,
) -> None:
    """A row the write guard would refuse must still be removable by an
    invalidation — projection would refuse it, so the scrub matches by shape.
    """
    import psycopg

    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id))
    tampered = {**_stored("evref-0001"), "digest": "0" * 64}
    with psycopg.connect(invalidation_live_pg_url) as conn:
        conn.execute(
            "UPDATE fin_run SET structured_evidence = %s::jsonb WHERE id = %s",
            (json.dumps([tampered]), run_id),
        )

    outcome = drain_invalidation_batch(
        store,
        consumer_id=_CONSUMER_ID,
        fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
        acknowledge=lambda _c: None,
    )

    assert outcome.scrubbed_entries == 1
    assert _stored_refs(invalidation_live_pg_url, run_id) == []


def test_live_pg_invalidation_schema_upgrade_is_idempotent(invalidation_live_pg_url: str) -> None:
    """Constructing the store twice against the same database must not raise:
    the new checkpoint table follows the same CREATE ... IF NOT EXISTS rule the
    existing ``fin_run`` columns follow."""
    import psycopg

    PostgresThreadStore(invalidation_live_pg_url)
    PostgresThreadStore(invalidation_live_pg_url)

    with psycopg.connect(invalidation_live_pg_url) as conn:
        # WHY the predicate is on the schema: the fixture scopes ``search_path``
        # to a per-run schema, so this is the table the store just built. Without
        # it the query returns every schema's copy of the name, and this shared
        # database carries one in ``public`` too — so the assertion would compare
        # this branch's declared DDL against whatever another lane's migration
        # left behind, and report a shape disagreement that is not one. The set
        # compared below is the shape THIS tree declares, in THIS tree's schema.
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'fin_evidence_invalidation_checkpoint'"
            ).fetchall()
        }
    assert columns == {
        "consumer_id",
        "applied_checkpoint",
        "applied_at",
        "last_seq",
        "acked_checkpoint",
        # T33's recovery gate, migrated by the same ALTER ... IF NOT EXISTS rule
        # the columns above follow.
        "recovery_withheld",
        "recovery_reason",
    }


def test_live_pg_invalidation_unconfirmed_scrub_refuses_before_acknowledging(
    invalidation_live_pg_url: str,
) -> None:
    """A store that cannot confirm must not be acknowledged (the drain's
    ``evidence_unavailable`` exit, exercised through the real store seam)."""
    from fin.host.thread_store import EvidenceScrubOutcome

    class _NotConfirmed(PostgresThreadStore):
        def scrub_structured_evidence_payloads(self, **kwargs: Any) -> EvidenceScrubOutcome:
            return EvidenceScrubOutcome(scrubbed_entries=1, scrubbed_runs=1, remaining_entries=1)

    store = _NotConfirmed(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(f"run-{uuid.uuid4()}", thread_id, "evref-0001"))
    acks: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as error:
        drain_invalidation_batch(
            store,
            consumer_id=_CONSUMER_ID,
            fetch=lambda _cursor: _batch(_event("evref-0001", 1), next_checkpoint="ck-2"),
            acknowledge=acks.append,
        )

    assert error.value.code == EVIDENCE_UNAVAILABLE
    assert acks == []
    assert store.read_invalidation_checkpoint(_CONSUMER_ID).acked_checkpoint == ""

# ----- P2-I1: EP6 admission across the live JSONB boundary -----
#
# The admission lane's own proof. An in-memory pass cannot show that a
# snapshot admitted from a producer sibling lands in ``fin_run.structured_evidence``
# as jsonb, reads back byte-identically, and that the store's write guard
# raises on a record the admission lane could never have produced — leaving
# ZERO rows rather than a run row that looks evidence-free.


@pytest.fixture
def structured_admission_live_pg_url() -> Any:
    """A schema-scoped live URL, skipped when no live Postgres is configured.

    Named apart from ``invalidation_live_pg_url`` so the two lanes' live
    cases can both live in this file without sharing a schema.
    """
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip("FIN_CHECKPOINT_URL is not set; live Postgres admission tests are untested")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"fin_admission_test_{uuid.uuid4().hex}"
    scoped_url = make_conninfo(url, options=f"-c search_path={schema}")
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(scoped_url) as conn:
            assert conn.execute("SELECT current_schema()").fetchone() == (schema,)
        yield scoped_url
    finally:
        with psycopg.connect(url) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def _admission_sibling(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {**_BASE, "rows": [dict(_ROW)], "next_cursor": None}
    payload.update(over)
    return payload


def _admitted_record(**over: Any) -> dict[str, Any]:
    from fin.host.structured_evidence_admission import admit_structured_evidence

    admission = admit_structured_evidence(_admission_sibling(**over))
    assert admission.refusal is None
    assert len(admission.records) == 1
    return dict(admission.records[0])


def test_live_pg_admitted_structured_evidence_round_trips_through_the_jsonb_column(
    structured_admission_live_pg_url: str,
) -> None:
    """The whole point of the wiring: a same-turn producer sibling becomes a
    persisted ``fin_run.structured_evidence`` value, not an always-empty column."""
    import psycopg

    from fin.host.structured_evidence import project_stored_structured_evidence
    from fin.host.structured_snapshot import structured_snapshot_digest

    record = _admitted_record()
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store = PostgresThreadStore(structured_admission_live_pg_url)
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(
        RunRecord(
            id=run_id,
            thread_id=thread_id,
            question="who is the counterparty",
            status="done",
            structured_evidence=[record],
        )
    )

    with psycopg.connect(structured_admission_live_pg_url) as conn:
        raw, column_type = conn.execute(
            "SELECT structured_evidence, pg_typeof(structured_evidence)::text "
            "FROM fin_run WHERE id = %s",
            (run_id,),
        ).fetchone()
    assert column_type == "jsonb"
    assert raw == [record]

    read_back = store.list_runs(thread_id)[0]
    assert read_back.structured_evidence == [record]
    stored = read_back.structured_evidence[0]
    assert stored["digest"] == structured_snapshot_digest(stored["snapshot"])
    assert project_stored_structured_evidence(stored) is not None


def test_live_pg_a_refused_sibling_persists_zero_records_not_an_empty_success(
    structured_admission_live_pg_url: str,
) -> None:
    """A refusal may not be published as an evidence-free run.

    ``InMemoryThreadStore`` reads ``[]`` back for both an evidence-free run
    and a refused admission, so this pins that the two stay distinguishable
    only by the refusal being raised BEFORE the row is written.
    """
    import psycopg

    from fin.host.structured_evidence_admission import admit_structured_evidence

    admission = admit_structured_evidence(_admission_sibling(operation="aggregate"))
    assert admission.refusal is not None
    assert admission.records == ()

    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store = PostgresThreadStore(structured_admission_live_pg_url)
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(
        RunRecord(
            id=run_id,
            thread_id=thread_id,
            question="who is the counterparty",
            status="done",
            structured_evidence=admission.as_dicts(),
        )
    )

    with psycopg.connect(structured_admission_live_pg_url) as conn:
        raw = conn.execute(
            "SELECT structured_evidence FROM fin_run WHERE id = %s", (run_id,)
        ).fetchone()
    assert raw == ([],)


def test_live_pg_an_admitted_record_that_fails_the_write_guard_raises_and_leaves_zero_rows(
    structured_admission_live_pg_url: str,
) -> None:
    """The exception class of the admission input contract.

    A record the guard rejects must RAISE ``EvidenceContractWriteError`` and
    write nothing — never persist a run row whose structured evidence is
    silently empty, which a reader cannot tell from a run that never
    received a structured result.
    """
    import psycopg

    from fin.host.evidence_contract import MALFORMED_EVIDENCE_CONTRACT
    from fin.host.thread_store import EvidenceContractWriteError

    record = _admitted_record()
    # The snapshot is mutated AFTER its digest was computed, so the stored
    # row no longer hashes to its own digest — corruption the guard catches
    # and admission could never emit on its own.
    record["snapshot"]["value"]["canonical_name"] = "Someone Else"

    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store = PostgresThreadStore(structured_admission_live_pg_url)
    store.create_thread(id=thread_id, title="knowledge thread")

    with pytest.raises(EvidenceContractWriteError) as error:
        store.append_run(
            RunRecord(
                id=run_id,
                thread_id=thread_id,
                question="who is the counterparty",
                status="done",
                structured_evidence=[record],
            )
        )
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert store.list_runs(thread_id) == []
    with psycopg.connect(structured_admission_live_pg_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM fin_run WHERE id = %s", (run_id,)
        ).fetchone()
    assert count == (0,)


# ----- the drain's caller, against the real JSONB column -----
#
# The in-memory half of the scheduler's cases lives in
# ``test_structured_invalidation_drain_worker.py``. What an in-memory run cannot
# show is the one the cadence contract rests on: that a sweep driven by the
# caller commits its scrub on the real column, over a SEPARATE connection, before
# the acknowledgement leaves — and that an unreachable backend leaves the
# DURABLE watermark where it was, which is what makes "pending" survive a
# process restart rather than living in one process's memory.


def test_live_pg_scheduler_sweep_commits_the_scrub_before_the_acknowledgement(
    invalidation_live_pg_url: str,
) -> None:
    """The caller-driven ordering, read over a SECOND connection.

    The recording transport probes the JSONB column from another connection at
    the instant the acknowledgement is sent; the payload must already be gone.
    """
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id, "evref-0001", "evref-0002"))

    observed: list[list[str]] = []
    acks: list[str] = []

    def fetch(_cursor: str | None) -> object:
        return _batch(_event("evref-0001", 1), next_checkpoint="ck-2")

    def acknowledge(checkpoint: str) -> None:
        observed.append(_stored_refs(invalidation_live_pg_url, run_id))
        acks.append(checkpoint)

    worker = StructuredInvalidationDrainWorker(
        store,
        (fetch, acknowledge),
        consumer_id=_CONSUMER_ID,
        cadence=DrainCadence(
            30.0, wait=_FakeClock(stop_after=1), monotonic=time.monotonic
        ),
    )

    thread = worker.start()
    thread.join(timeout=30.0)
    assert not thread.is_alive()

    assert observed == [["evref-0002"]], (
        "the acknowledged reference must already be out of the column at ack time"
    )
    assert acks == ["ck-2"]
    attempt = worker.last_attempt
    assert attempt is not None and attempt.drain_completed is True
    assert attempt.scrubbed_entries == 1
    assert _stored_refs(invalidation_live_pg_url, run_id) == ["evref-0002"]


def test_live_pg_scheduler_leaves_the_durable_watermark_pending_when_unreachable(
    invalidation_live_pg_url: str,
) -> None:
    """Maintenance spec :328-330 across a restart: an unreachable owner writes
    nothing durable, so a FRESH store instance still sees the pending phase and
    the redelivered event is not skipped."""
    store = PostgresThreadStore(invalidation_live_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(run_id, thread_id, "evref-0001"))
    unreachable = _RecordingTransport([ConnectionError("backend unreachable")])

    worker = StructuredInvalidationDrainWorker(
        store,
        (unreachable.fetch, unreachable.acknowledge),
        consumer_id=_CONSUMER_ID,
        cadence=DrainCadence(
            30.0, wait=_FakeClock(stop_after=1), monotonic=time.monotonic
        ),
    )
    thread = worker.start()
    thread.join(timeout=30.0)
    assert not thread.is_alive()

    attempt = worker.last_attempt
    assert attempt is not None and attempt.drain_completed is False
    assert unreachable.acks == []

    # A restart: a new store instance and a new worker over the same database.
    restarted = PostgresThreadStore(invalidation_live_pg_url)
    durable = restarted.read_invalidation_checkpoint(_CONSUMER_ID)
    assert durable.acked_checkpoint == ""
    assert durable.applied_checkpoint == ""
    assert _stored_refs(invalidation_live_pg_url, run_id) == ["evref-0001"]

    recovered = _RecordingTransport(
        [_batch(_event("evref-0001", 1), next_checkpoint="ck-2")]
    )
    second = StructuredInvalidationDrainWorker(
        restarted,
        (recovered.fetch, recovered.acknowledge),
        consumer_id=_CONSUMER_ID,
        cadence=DrainCadence(30.0),
    )
    second.drain_once()

    assert recovered.cursors == [None], "resumed from the persisted cursor, not a reset"
    assert recovered.acks == ["ck-2"]
    assert _stored_refs(invalidation_live_pg_url, run_id) == []


def test_live_pg_admitting_the_same_sibling_twice_writes_the_same_digest(
    structured_admission_live_pg_url: str,
) -> None:
    """Repeat delivery of one producer result is stable on real storage."""
    from fin.host.structured_evidence_admission import admit_structured_evidence

    first = admit_structured_evidence(_admission_sibling())
    second = admit_structured_evidence(_admission_sibling())

    thread_id = f"thread-{uuid.uuid4()}"
    store = PostgresThreadStore(structured_admission_live_pg_url)
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(
        RunRecord(
            id=f"run-{uuid.uuid4()}",
            thread_id=thread_id,
            question="who is the counterparty",
            status="done",
            structured_evidence=first.as_dicts(),
        )
    )
    store.append_run(
        RunRecord(
            id=f"run-{uuid.uuid4()}",
            thread_id=thread_id,
            question="who is the counterparty",
            status="done",
            structured_evidence=second.as_dicts(),
        )
    )

    runs = store.list_runs(thread_id)
    assert [record["digest"] for record in runs[0].structured_evidence] == [
        record["digest"] for record in runs[1].structured_evidence
    ]
