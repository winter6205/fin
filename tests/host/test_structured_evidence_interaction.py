"""Real interaction proof for EP6 admission and EP9/EP10 scrubbing.

These cases drive the real Postgres store and the real HTTP projection. The
unit suites prove each function; this file proves the pieces are joined at the
seams that only exist end to end: admission into a persisted run row, the turn
table reading it back, and a durable invalidation drain whose acknowledgement
is ordered after the scrub commits.

A fake retrieve port stands in for IK because IK emits no ``structured_result``
yet (P1-I7 owns that producer). Everything downstream of the transport is real.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from fin.host.evidence.structured_evidence_admission import admit_structured_evidence
from fin.host.evidence.structured_evidence_invalidation import (
    StructuredInvalidationRefusal,
    drain_invalidation_batch,
)
from fin.host.thread_store import PostgresThreadStore, RunRecord, ThreadNotFoundError
from invalidation_batch import build_batch, build_event


STRUCTURED_RESULT: dict[str, Any] = {
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
    "rows": [
        {
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
    ],
    "next_cursor": None,
}


@pytest.fixture
def live_pg_url() -> str:
    url = os.environ.get("FIN_CHECKPOINT_URL")
    if not url:
        pytest.skip("FIN_CHECKPOINT_URL is not set; live interaction is not selected")
    return url


def _store_with_run(
    url: str, records: list[dict[str, Any]]
) -> tuple[PostgresThreadStore, str, str]:
    store = PostgresThreadStore(url)
    thread_id = f"itp-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    store.create_thread(id=thread_id, title="interaction thread")
    store.append_run(
        RunRecord(
            id=run_id,
            thread_id=thread_id,
            question="Which organizations are in scope?",
            status="done",
            status_reason="",
            answer="Northwind Logistics.",
            structured_evidence=list(records),
        )
    )
    return store, thread_id, run_id


def _admitted_records() -> list[dict[str, Any]]:
    return admit_structured_evidence(STRUCTURED_RESULT).as_dicts()


def _event(evidence_ref: str, seq: int) -> dict[str, Any]:
    return build_event(evidence_ref, seq=seq)


def _batch(*events: dict[str, Any], next_checkpoint: str, gap: bool = False) -> dict:
    return build_batch(*events, next_checkpoint=next_checkpoint, gap=gap)


def test_admitted_structured_evidence_survives_a_real_jsonb_round_trip(
    live_pg_url: str,
) -> None:
    """The EP6 storage seam: admission -> run row -> read back intact."""
    store, thread_id, _ = _store_with_run(live_pg_url, _admitted_records())

    stored = PostgresThreadStore(live_pg_url).list_runs(thread_id)[0].structured_evidence
    assert len(stored) == 1
    assert stored[0]["snapshot"]["evidence_ref"] == "evref-0001"
    assert stored[0]["snapshot"]["kind"] == "structured-result"
    assert stored[0]["digest"]


def test_the_turn_table_projects_the_admitted_snapshot(live_pg_url: str) -> None:
    """The projection seam a reload uses; without it the run is unreadable."""
    from fin.host.workspace_http import _serialize_run

    store, thread_id, _ = _store_with_run(live_pg_url, _admitted_records())

    row = _serialize_run(store.list_runs(thread_id)[0])
    assert len(row["structured_evidence"]) == 1
    assert row["structured_evidence"][0]["snapshot"]["evidence_ref"] == "evref-0001"


def test_a_refused_sibling_persists_no_structured_evidence(live_pg_url: str) -> None:
    """T29: refuse WITHOUT success-shaped empty evidence."""
    refused = admit_structured_evidence(
        {"schema": "iknow.structured_result", "schema_version": 1}
    )
    assert refused.admitted is False
    assert refused.refusal is not None

    store, thread_id, _ = _store_with_run(live_pg_url, refused.as_dicts())
    assert store.list_runs(thread_id)[0].structured_evidence == []


def test_a_drain_acknowledges_only_after_the_payload_is_gone(live_pg_url: str) -> None:
    """EP9 ordering, measured at the instant the ack is issued."""
    store, thread_id, _ = _store_with_run(live_pg_url, _admitted_records())
    assert len(store.list_runs(thread_id)[0].structured_evidence) == 1

    payload_visible_at_ack: list[int] = []

    def _acknowledge(checkpoint: str) -> None:
        # WHY read the store HERE: the claim under test is that the payload
        # is already absent when the ack goes out, so the assertion has to
        # be made from inside the ack, not after the drain returns.
        payload_visible_at_ack.append(
            len(store.list_runs(thread_id)[0].structured_evidence)
        )

    outcome = drain_invalidation_batch(
        store,
        consumer_id="interaction-order",
        fetch=lambda _cursor: _batch(
            _event("evref-0001", 1), next_checkpoint="ck-1"
        ),
        acknowledge=_acknowledge,
    )

    # A refusal is RAISED, never returned on the outcome: an outcome that
    # carries a refusal could be ignored by a caller and still ack.
    assert outcome.scrubbed_entries >= 1
    assert payload_visible_at_ack == [0]
    assert store.list_runs(thread_id)[0].structured_evidence == []


def test_a_gap_refuses_without_acknowledging_or_scrubbing(live_pg_url: str) -> None:
    """EP10: a gap never skips, never resets, never acks."""
    store, thread_id, _ = _store_with_run(live_pg_url, _admitted_records())
    acked: list[str] = []

    with pytest.raises(StructuredInvalidationRefusal) as caught:
        drain_invalidation_batch(
            store,
            consumer_id="interaction-gap",
            fetch=lambda _cursor: _batch(
                _event("evref-0001", 1), next_checkpoint="ck-1", gap=True
            ),
            acknowledge=lambda checkpoint: acked.append(checkpoint),
        )

    assert caught.value.code == "retrieve_unavailable"
    assert not acked
    assert len(store.list_runs(thread_id)[0].structured_evidence) == 1


def test_an_interrupted_ack_redelivers_after_a_restart(live_pg_url: str) -> None:
    """D3: a crash between scrub and ack resumes; it never skips the event."""
    store, thread_id, _ = _store_with_run(live_pg_url, _admitted_records())

    def _fetch(cursor: str | None) -> dict:
        return _batch(_event("evref-0001", 1), next_checkpoint="ck-1")

    with pytest.raises(RuntimeError):
        drain_invalidation_batch(
            store,
            consumer_id="interaction-restart",
            fetch=_fetch,
            acknowledge=lambda _c: (_ for _ in ()).throw(RuntimeError("ack lost")),
        )
    assert store.list_runs(thread_id)[0].structured_evidence == []

    # A second store instance stands in for the process restart.
    restarted = PostgresThreadStore(live_pg_url)
    resumed: list[str] = []
    outcome = drain_invalidation_batch(
        restarted,
        consumer_id="interaction-restart",
        fetch=_fetch,
        acknowledge=lambda checkpoint: resumed.append(checkpoint),
    )

    assert resumed == ["ck-1"], "the restart must redeliver, not skip"
    assert store.read_invalidation_checkpoint("interaction-restart").acked_checkpoint == "ck-1"


def test_a_scrub_leaves_the_rest_of_the_run_row_untouched(live_pg_url: str) -> None:
    """Anti-corruption: the write is scoped to the evidence column."""
    import psycopg

    store, thread_id, run_id = _store_with_run(live_pg_url, _admitted_records())
    before = store.list_runs(thread_id)[0]

    drain_invalidation_batch(
        store,
        consumer_id="interaction-scope",
        fetch=lambda _cursor: _batch(
            _event("evref-0001", 1), next_checkpoint="ck-1"
        ),
        acknowledge=lambda _c: None,
    )

    with psycopg.connect(live_pg_url) as conn:
        row = conn.execute(
            "SELECT question, answer, citations, visual_claims "
            "FROM fin_run WHERE id = %s",
            (run_id,),
        ).fetchone()
    after = store.list_runs(thread_id)[0]

    assert row[0] == before.question
    assert row[1] == before.answer
    assert list(row[2]) == before.citations
    assert list(row[3]) == before.visual_claims
    assert after.structured_evidence == []


def test_the_thread_foreign_key_still_rejects_an_unknown_thread(
    live_pg_url: str,
) -> None:
    store = PostgresThreadStore(live_pg_url)
    with pytest.raises(ThreadNotFoundError):
        store.append_run(
            RunRecord(
                id=f"run-{uuid.uuid4()}",
                thread_id=f"absent-{uuid.uuid4()}",
                question="q",
                status="done",
                status_reason="",
                answer="a",
                structured_evidence=_admitted_records(),
            )
        )

def test_the_turn_table_withholds_an_expired_snapshot() -> None:
    """EP11 local deadline: the answer stays, the citation goes unavailable.

    Uses no database: the withholding decision is computed from the frozen
    snapshot, so a record whose deadline has passed must project as an
    unavailable citation without a backend outcome (P2-D2).
    """
    from fin.host.evidence.structured_evidence_admission import admit_structured_evidence
    from fin.host.workspace_http import _serialize_run

    live = admit_structured_evidence(STRUCTURED_RESULT).as_dicts()
    assert live

    def _with_deadline(row: dict[str, Any], expires_at: str) -> dict[str, Any]:
        # Rebuild through the real admission path so the digest recomputes;
        # a hand-edited record is refused by the store write guard, which is
        # the store correctly refusing a tampered payload.
        payload = json.loads(json.dumps(STRUCTURED_RESULT))
        payload["rows"][0]["evidence_expires_at"] = expires_at
        payload["queried_at"] = (datetime.fromisoformat(expires_at) - timedelta(days=365)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )
        records = admit_structured_evidence(payload).as_dicts()
        assert records, "the rebuilt sibling must still admit"
        return records[0]

    def _run_with(record: dict[str, Any]) -> dict[str, Any]:
        return _serialize_run(
            RunRecord(
                id="run-1",
                thread_id="thread-1",
                question="q",
                status="done",
                status_reason="",
                answer="Northwind Logistics.",
                structured_evidence=[record],
            )
        )

    assert _run_with(live[0])["structured_evidence"][0]["snapshot"]

    expired = _with_deadline(live[0], "2020-01-01T00:00:00Z")
    served = _run_with(expired)["structured_evidence"][0]
    assert served["snapshot"] == {}, "an expired payload must not be served"
    assert served["error"] == "evidence_expired"

    row = _run_with(expired)
    assert row["answer"] == "Northwind Logistics.", "the answer survives expiry"
