"""Historical replay of stored citation rows under the frozen v0 shape.

Spec ``specs/enterprise-evidence-contract.md`` §7 settles the legacy
compatibility rule for ``fin_run.citations``: the stored rows span three
real writer generations (``page``-era since T3, ``attachment_id`` /
``filename`` since the thread-attach era, the ``page`` -> ``unit`` rename
plus ``display`` at #321 T5), no migration shipped, so all three live in
the JSONB today. A v0 row is valid iff its keys are a subset of the nine
names, it carries ``document_id`` / ``version`` / ``quote``, EXACTLY ONE
of ``unit`` or ``page``, and no v1 lane marker; the defaults are frozen
facts of that shape (absent ``kind`` -> the document kind, a ``page``
reads as ``unit``, absent ``display`` -> ``page``). A contradictory,
corrupt, or out-of-set row is the per-record named refusal
``unsupported_evidence_contract``: visible and typed, the rest of the
history still serves, never a 500, never a silent drop.

The field sets below are the historical writers' shapes, pinned from
``fin.host.export._is_pre_locator_rename_row`` (the pre-rename row is
``{document_id, version, page, quote}``), the thread-attach era's
``attachment_id`` / ``filename`` slots, and the T5-era full row in
``tests/host/test_workspace_http.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


QUOTE = "Total net sales increased during the third quarter fiscal 2022"

# The three writer generations, as their writers actually shaped rows.
PAGE_ERA_ROW: dict[str, Any] = {
    "document_id": "AAPL-2022Q3",
    "version": "1",
    "page": 4,
    "quote": QUOTE,
}
ATTACH_ERA_ROW: dict[str, Any] = {
    "document_id": "",
    "attachment_id": "att-7",
    "version": "3",
    "page": 2,
    "filename": "contract.txt",
    "quote": QUOTE,
}
UNIT_DISPLAY_ERA_ROW: dict[str, Any] = {
    "kind": "company",
    "document_id": "AAPL-2022Q3",
    "attachment_id": "",
    "version": "7",
    "unit": 4,
    "display": "page",
    "filename": "",
    "quote": QUOTE,
}
# A pre-E5 ledger citation: full slots plus the retained snapshot in the
# five-key shape §4.4 shipped (the ``schema`` member arrives at E5).
LEDGER_ROW: dict[str, Any] = {
    "kind": "database-row",
    "document_id": "",
    "attachment_id": "",
    "version": "2026-03-01T09:14:22Z",
    "unit": 1,
    "display": "record",
    "filename": "",
    "quote": "contract C-2291 notice_days 30",
    "database_row": {
        "source": "contracts",
        "record_key": {"contract_id": "C-2291"},
        "queried_at": "2026-03-01T09:14:22Z",
        "fields": {"notice_days": "30"},
        "summary": "contract C-2291 notice_days 30",
    },
}

# The wire shape the history door returns for these rows: the frozen
# eight-key v0 row, and NO ``database_row`` key on a document row (the
# sibling is present only for a kind database-row — spec §2 — so a
# reload emits exactly the keys the live stream emitted, and the web
# decoder never sees a company row carrying the sibling). The key set
# and every value stay identical EXCEPT ``unit`` on the two pre-rename
# generations, where §7's frozen identity map now reads the stored
# ``page`` into the ``unit`` slot instead of the retired ``unit=0``
# blanket default.
def _today_wire_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "kind": "company",
        "document_id": "AAPL-2022Q3",
        "attachment_id": "",
        "version": "1",
        "unit": 0,
        "display": "page",
        "filename": "",
        "quote": QUOTE,
    }
    row.update(overrides)
    return row


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def store(http_app: FastAPI):
    """In-memory thread store + a graph stand-in with none of the live
    seams, so the history GET exercises exactly the read path."""
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, RunRecord

    thread_store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: thread_store

    class _GraphStandIn:
        """No ``is_locked`` / ``pending_nodes`` / ``occupancy`` seams:
        the route's documented ``hasattr`` tolerance covers it."""

    http_app.dependency_overrides[get_graph] = lambda: _GraphStandIn()

    def make_thread(citations: list[dict[str, Any]], run_id: str = "r-1") -> str:
        record = thread_store.create_thread(title="replay")
        thread_store.append_run(
            RunRecord(
                id=run_id,
                thread_id=record.id,
                question="q",
                status="done",
                answer="a",
                citations=citations,
            )
        )
        return record.id

    return make_thread


def _turns(client: TestClient, thread_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/threads/{thread_id}")
    assert response.status_code == 200, response.text
    return response.json()["turns"]


# ----- the three generations decode; wire shape unchanged -----


def test_page_era_row_decodes_on_history_read(client: TestClient, store) -> None:
    thread_id = store([dict(PAGE_ERA_ROW)])
    turn = _turns(client, thread_id)[0]
    assert turn["citations"] == [
        _today_wire_row(unit=4),  # §7: a page reads as unit (identity map)
    ]
    assert turn["rejected_citations"] == []


def test_attach_era_row_decodes_on_history_read(client: TestClient, store) -> None:
    thread_id = store([dict(ATTACH_ERA_ROW)])
    turn = _turns(client, thread_id)[0]
    assert turn["citations"] == [
        _today_wire_row(
            document_id="",
            attachment_id="att-7",
            version="3",
            unit=2,  # §7 identity map, replacing the retired unit=0 default
            filename="contract.txt",
        )
    ]
    assert turn["rejected_citations"] == []


def test_unit_display_era_row_decodes_byte_identical(client: TestClient, store) -> None:
    """A T5-era row carries every v0 slot itself: nothing is defaulted,
    so the decoded row is byte-identical to what the read returned
    before §7 settled the shape."""
    thread_id = store([dict(UNIT_DISPLAY_ERA_ROW)])
    turn = _turns(client, thread_id)[0]
    assert turn["citations"] == [
        _today_wire_row(version="7", unit=4)
    ]
    assert turn["rejected_citations"] == []


def test_pre_e5_ledger_row_keeps_its_snapshot_values(client: TestClient, store) -> None:
    """§7.4: a historical snapshot reads back with its original values.
    The row carries the v1 lane marker, so v0 defaulting does not apply
    to it — and it must not be refused either: the marker is the lane's
    own shipped shape, and dropping it would silently delete the
    operator's ledger history."""
    thread_id = store([dict(LEDGER_ROW)])
    turn = _turns(client, thread_id)[0]
    row = turn["citations"][0]
    assert row["kind"] == "database-row"
    assert row["database_row"] == LEDGER_ROW["database_row"]
    assert row["version"] == "2026-03-01T09:14:22Z"
    assert turn["rejected_citations"] == []


def test_an_oversize_stored_snapshot_is_refused_by_name(client: TestClient, store) -> None:
    """Replay (§7) applies only the v0 KEY-SET projection, so a
    lane-marked v1 row that is shape-legal but whose CONTENT breaks a §3
    bound was served unrefused before the read re-ran the validator
    home. A snapshot whose canonical serialization exceeds 32 KiB is the
    named ``database_row_overflow`` refusal — the same code the write
    and wire boundaries name — never a silent accept."""
    huge = "x" * (33 * 1024)
    row = dict(LEDGER_ROW, quote=huge)
    row["database_row"] = dict(LEDGER_ROW["database_row"], summary=huge)
    thread_id = store([row])
    turn = _turns(client, thread_id)[0]
    assert turn["citations"] == []
    assert turn["rejected_citations"] == [
        {"citation_index": 0, "error": "database_row_overflow"}
    ]


def test_a_corrupt_content_stored_snapshot_is_refused_by_name(
    client: TestClient, store
) -> None:
    """The other §7 replay seam: a lane-marked v1 row whose snapshot
    content is corrupt (here a non-strict ``queried_at``) is shape-legal
    under the key-set projection, so only a content re-check names it.
    The refusal is the home's ``malformed_evidence_contract``, visible
    per record while the rest of the history still serves."""
    row = dict(LEDGER_ROW)
    row["database_row"] = dict(
        LEDGER_ROW["database_row"], queried_at="2026-03-01T09:14:22+00:00"
    )
    thread_id = store([row])
    turn = _turns(client, thread_id)[0]
    assert turn["citations"] == []
    assert turn["rejected_citations"] == [
        {"citation_index": 0, "error": "malformed_evidence_contract"}
    ]


# ----- contradictory / corrupt / out-of-set rows: named per-record refusal -----


def test_a_row_with_both_unit_and_page_does_not_decode() -> None:
    """The coercion-removal pin: ``unit`` AND ``page`` is a
    contradiction (§7.1), and the blanket defaults that used to swallow
    it are gone."""
    from pydantic import ValidationError

    from fin.host.workspace_http import CitationPayload

    row = dict(UNIT_DISPLAY_ERA_ROW, page=9)
    with pytest.raises(ValidationError):
        CitationPayload.model_validate(row)


def test_a_row_with_neither_unit_nor_page_does_not_decode() -> None:
    from pydantic import ValidationError

    from fin.host.workspace_http import CitationPayload

    row = dict(UNIT_DISPLAY_ERA_ROW)
    del row["unit"]
    with pytest.raises(ValidationError):
        CitationPayload.model_validate(row)


def test_a_lane_marker_without_kind_cannot_default_an_identity() -> None:
    """§7.1: reading an absent ``kind`` as the document kind is legal
    ONLY while no lane marker rides the row — that is what keeps the
    v0 defaulting from turning a ledger snapshot into a text citation."""
    from pydantic import ValidationError

    from fin.host.workspace_http import CitationPayload

    row = dict(LEDGER_ROW)
    del row["kind"]
    with pytest.raises(ValidationError):
        CitationPayload.model_validate(row)


def test_a_key_outside_the_closed_set_is_a_refusal() -> None:
    """v0 is a closed generative shape, not 'anything without schema'."""
    from pydantic import ValidationError

    from fin.host.workspace_http import CitationPayload

    with pytest.raises(ValidationError):
        CitationPayload.model_validate(dict(UNIT_DISPLAY_ERA_ROW, bogus=1))


def test_refused_rows_are_named_per_record_while_history_still_serves(
    client: TestClient, store
) -> None:
    """§7.3 + §6.1 negative row: one corrupt record must not blank the
    turn, the thread, or the rest of the list, and must never be
    silently dropped or replaced with an empty citations array."""
    both = dict(UNIT_DISPLAY_ERA_ROW, page=9)
    junk = dict(UNIT_DISPLAY_ERA_ROW, bogus=1)
    thread_id = store(
        [dict(UNIT_DISPLAY_ERA_ROW), both, dict(PAGE_ERA_ROW), junk, "not-a-row"]
    )
    turn = _turns(client, thread_id)[0]

    assert [row["quote"] for row in turn["citations"]] == [QUOTE, QUOTE]
    assert [row["unit"] for row in turn["citations"]] == [4, 4]
    assert turn["rejected_citations"] == [
        {"citation_index": 1, "error": "unsupported_evidence_contract"},
        {"citation_index": 3, "error": "unsupported_evidence_contract"},
        {"citation_index": 4, "error": "unsupported_evidence_contract"},
    ]


def test_empty_citations_keep_their_defined_meaning(client: TestClient, store) -> None:
    """Empty history evidence is defined-empty, not a failure
    (§6.1 empty row at the persistence boundary)."""
    thread_id = store([])
    turn = _turns(client, thread_id)[0]
    assert turn["citations"] == []
    assert turn["rejected_citations"] == []


def test_stored_document_row_history_matches_the_stream_key_set(
    client: TestClient, store
) -> None:
    """The reload invariant at the replay seam: a stored full-slot
    company row reads back on history GET with EXACTLY the key set the
    SSE projection emits — eight keys, no ``database_row`` sibling — so
    the web decoder sees the same shape live and after reload."""
    from fin.host.workspace_http import _citations_wire_projection

    thread_id = store([dict(UNIT_DISPLAY_ERA_ROW)])
    turn = _turns(client, thread_id)[0]
    stream_rows, refusals = _citations_wire_projection([dict(UNIT_DISPLAY_ERA_ROW)])
    assert refusals == []

    history_row = turn["citations"][0]
    assert "database_row" not in history_row
    assert set(history_row) == set(stream_rows[0])


def test_refused_rows_stay_named_per_record_after_the_omission_fix(
    client: TestClient, store
) -> None:
    """The omission rule lives on the citation dump only: a row that
    matches no frozen shape is still the per-record named
    ``unsupported_evidence_contract`` refusal, the good sibling rows
    still serve without the sibling key, and the refusal entries are
    byte-unchanged."""
    thread_id = store([dict(UNIT_DISPLAY_ERA_ROW), dict(UNIT_DISPLAY_ERA_ROW, bogus=1)])
    turn = _turns(client, thread_id)[0]

    assert len(turn["citations"]) == 1
    assert "database_row" not in turn["citations"][0]
    assert turn["rejected_citations"] == [
        {"citation_index": 1, "error": "unsupported_evidence_contract"}
    ]


# ----- the model seam itself: no blanket defaults remain -----


def test_citation_payload_carries_no_blanket_defaults() -> None:
    """The §7 matcher replaced the permissive per-field defaults: a
    row must either state its slots or match the frozen v0 shape —
    the model no longer papers over a missing identity."""
    from fin.host.workspace_http import CitationPayload

    for name in ("kind", "document_id", "attachment_id", "version",
                 "unit", "display", "filename", "quote"):
        assert CitationPayload.model_fields[name].is_required(), name
