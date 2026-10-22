"""The structured lane's own persisted record.

The lane is stored beside the citation channel rather than inside it, for
the same reason visual claims are: a structured value can support a
structured claim and must never ride the quote channel. These cases pin
that separation, the write guard, and the round-trip — and pin the one
thing that matters most on a replay, that a stored snapshot's digest is
recomputed rather than trusted.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from fin.host.evidence.evidence_contract import MALFORMED_EVIDENCE_CONTRACT
from fin.host.evidence.structured_evidence import (
    build_stored_record,
    project_stored_structured_evidence,
    structured_snapshots_of,
)
from fin.host.thread_store import (
    InMemoryThreadStore,
    RunRecord,
    _outgoing_evidence_error,
)
from fin.host.evidence.structured_snapshot import structured_snapshot_digest

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

_LEGACY_CITATION = {
    "document_id": "doc-legacy",
    "version": "v1",
    "page": 2,
    "quote": "a previously stored quoted passage",
}
_PROJECTED_LEGACY_CITATION = {
    "kind": "company",
    "document_id": "doc-legacy",
    "attachment_id": "",
    "version": "v1",
    "unit": 2,
    "display": "page",
    "filename": "",
    "quote": "a previously stored quoted passage",
}


def _sibling(**over: object) -> dict[str, object]:
    payload: dict[str, object] = {**_BASE, "rows": [dict(_ROW)], "next_cursor": None}
    payload.update(over)
    return payload


def _record(**over: object) -> RunRecord:
    snapshot = structured_snapshots_of(_sibling())[0]
    defaults: dict[str, object] = {
        "id": "run-1",
        "thread_id": "thread-1",
        "question": "who is the counterparty",
        "status": "done",
        "structured_evidence": [build_stored_record(snapshot)],
    }
    defaults.update(over)
    return RunRecord(**defaults)  # type: ignore[arg-type]


def test_a_valid_stored_record_survives_the_write_guard() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record())
    runs = store.list_runs("thread-1")
    assert len(runs) == 1
    stored = runs[0].structured_evidence
    assert len(stored) == 1
    assert project_stored_structured_evidence(stored[0]) is not None


def test_the_round_trip_preserves_the_snapshot_and_its_digest() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    original = _record()
    store.append_run(original)
    read_back = store.list_runs("thread-1")[0].structured_evidence[0]
    assert read_back == original.structured_evidence[0]
    assert read_back["digest"] == structured_snapshot_digest(read_back["snapshot"])


def test_a_tampered_snapshot_is_refused_by_the_durable_write_guard() -> None:
    """The guard lives on the Postgres ``append_run`` boundary
    (``thread_store.py:1123``); the in-memory store does not run it, so
    this pins the guard function directly rather than pretending the
    in-memory path enforces it.
    """
    record = _record()
    record.structured_evidence[0]["snapshot"]["value"]["canonical_name"] = "Someone Else"
    refusal = _outgoing_evidence_error(record)
    assert refusal is not None
    assert refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_tampered_digest_is_refused_by_the_durable_write_guard() -> None:
    record = _record()
    record.structured_evidence[0]["digest"] = "0" * 64
    refusal = _outgoing_evidence_error(record)
    assert refusal is not None
    assert refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_an_off_shape_stored_row_is_refused_by_the_durable_write_guard() -> None:
    record = _record(structured_evidence=[{"snapshot": {}, "digest": "x", "selector": {}}])
    refusal = _outgoing_evidence_error(record)
    assert refusal is not None
    assert refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_valid_record_passes_the_durable_write_guard() -> None:
    assert _outgoing_evidence_error(_record()) is None


def test_a_structured_record_never_appears_on_the_citation_channel() -> None:
    """The separation the whole lane rests on."""
    snapshot = structured_snapshots_of(_sibling())[0]
    record = _record(structured_evidence=[build_stored_record(snapshot)])
    assert record.citations == []
    assert all(row.get("kind") != "structured-result" for row in record.citations)


def test_a_run_without_structured_evidence_reads_back_empty_not_none() -> None:
    store = InMemoryThreadStore()
    store.create_thread(id="thread-1", title="t")
    store.append_run(_record(structured_evidence=[]))
    assert store.list_runs("thread-1")[0].structured_evidence == []


@pytest.fixture
def structured_evidence_pg_url():
    """Create a schema-scoped URL so live tests never touch shared tables."""
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip("FIN_CHECKPOINT_URL is not set; live Postgres evidence tests are untested")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"fin_structured_evidence_test_{uuid.uuid4().hex}"
    scoped_url = make_conninfo(url, options=f"-c search_path={schema}")
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    try:
        with psycopg.connect(scoped_url) as conn:
            assert conn.execute("SELECT current_schema()").fetchone() == (schema,)
        yield scoped_url
    finally:
        with psycopg.connect(url) as conn:
            drop_schema = sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            )
            conn.execute(drop_schema)


def test_postgres_roundtrips_structured_jsonb_without_changing_legacy_citations(
    structured_evidence_pg_url: str,
) -> None:
    """The durable store keeps the structured snapshot in its own JSONB lane."""
    import psycopg

    from fin.host.thread_store import PostgresThreadStore

    store = PostgresThreadStore(structured_evidence_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    original = _record(
        id=run_id,
        thread_id=thread_id,
        citations=[dict(_LEGACY_CITATION)],
    )
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(original)

    with psycopg.connect(structured_evidence_pg_url) as conn:
        raw_citations, raw_structured, structured_type = conn.execute(
            "SELECT citations, structured_evidence, pg_typeof(structured_evidence)::text "
            "FROM fin_run WHERE id = %s",
            (run_id,),
        ).fetchone()
    assert raw_citations == [_LEGACY_CITATION]
    assert structured_type == "jsonb"
    assert raw_structured == original.structured_evidence

    read_back = store.list_runs(thread_id)[0]
    assert read_back.structured_evidence == original.structured_evidence
    stored = read_back.structured_evidence[0]
    assert stored["digest"] == structured_snapshot_digest(stored["snapshot"])
    assert read_back.citations == [_PROJECTED_LEGACY_CITATION]


def test_postgres_refuses_corrupt_structured_evidence_jsonb_as_store_failure(
    structured_evidence_pg_url: str,
) -> None:
    """A wrong JSONB shape must not masquerade as an evidence-free run."""
    import psycopg

    from fin.host.thread_store import PersistenceUnavailableError, PostgresThreadStore

    store = PostgresThreadStore(structured_evidence_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    store.append_run(_record(id=run_id, thread_id=thread_id))

    with psycopg.connect(structured_evidence_pg_url) as conn:
        conn.execute(
            "UPDATE fin_run SET structured_evidence = %s::jsonb WHERE id = %s",
            (json.dumps({"unexpected": "object"}), run_id),
        )

    with pytest.raises(PersistenceUnavailableError) as error:
        store.list_runs(thread_id)
    assert error.value.code == "persistence_unavailable"


def test_postgres_refuses_a_tampered_selector_before_writing(
    structured_evidence_pg_url: str,
) -> None:
    import psycopg

    from fin.host.thread_store import (
        EvidenceContractWriteError,
        PostgresThreadStore,
    )

    store = PostgresThreadStore(structured_evidence_pg_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="knowledge thread")
    record = _record(id=run_id, thread_id=thread_id)
    record.structured_evidence[0]["selector"]["result_id"] = "different-result"

    with pytest.raises(EvidenceContractWriteError) as error:
        store.append_run(record)
    assert error.value.code == MALFORMED_EVIDENCE_CONTRACT
    assert store.list_runs(thread_id) == []
    with psycopg.connect(structured_evidence_pg_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM fin_run WHERE id = %s", (run_id,)
        ).fetchone()
    assert count == (0,)


def test_postgres_bootstrap_upgrades_a_legacy_table_idempotently(
    structured_evidence_pg_url: str,
) -> None:
    """Existing runs gain an empty structured lane while citations survive."""
    import psycopg

    from fin.host.thread_store import PostgresThreadStore

    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    with psycopg.connect(structured_evidence_pg_url) as conn:
        conn.execute(
            "CREATE TABLE fin_thread ("
            "id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.execute(
            "CREATE TABLE fin_run ("
            "id TEXT PRIMARY KEY, "
            "thread_id TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE, "
            "question TEXT NOT NULL, status TEXT NOT NULL, "
            "status_reason TEXT NOT NULL DEFAULT '', "
            "answer TEXT NOT NULL DEFAULT '', citations JSONB NOT NULL DEFAULT '[]'::jsonb, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.execute("INSERT INTO fin_thread (id, title) VALUES (%s, %s)", (thread_id, "legacy"))
        conn.execute(
            "INSERT INTO fin_run (id, thread_id, question, status, citations) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (run_id, thread_id, "old question", "done", json.dumps([_LEGACY_CITATION])),
        )

    PostgresThreadStore(structured_evidence_pg_url)
    PostgresThreadStore(structured_evidence_pg_url)

    with psycopg.connect(structured_evidence_pg_url) as conn:
        stored_lane, lane_type = conn.execute(
            "SELECT structured_evidence, pg_typeof(structured_evidence)::text "
            "FROM fin_run WHERE id = %s",
            (run_id,),
        ).fetchone()
    assert stored_lane == []
    assert lane_type == "jsonb"
    old_run = PostgresThreadStore(structured_evidence_pg_url).list_runs(thread_id)[0]
    assert old_run.structured_evidence == []
    assert old_run.citations == [_PROJECTED_LEGACY_CITATION]
