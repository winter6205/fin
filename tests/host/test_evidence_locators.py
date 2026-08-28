"""Run-scoped evidence locators on the workspace turn row (T1, #135).

Spec `specs/enterprise-kb-agent.md` Focus 5 / SC8: each terminal
workspace **run** persists the locators this run retrieved or read
(``evidence_locators`` on ``fin_run``) — NOT only the spans the
citation verifier passed. A refused run therefore still shows what
it looked at, and a follow-up run must not inherit the previous
run's locators as this run's evidence (ADR 0016 run-scoped reset).

Three seams are covered here:

* ``fin.host.thread_store`` — the field exists on ``RunRecord``,
  round-trips through ``InMemoryThreadStore``, and the Postgres
  schema declares the column (plus the additive migration).
* ``fin.host.run_payload`` — the locators are derived from the run's
  ``evidence`` list, deduplicated, and populated on ``refuse`` /
  ``step_cap`` too.
* ``fin.host.workspace_http`` — ``GET /api/threads/{id}`` exposes the
  field on the turn row, and run 2 on the same thread carries only
  its own pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


# ----- fixtures -----


@pytest.fixture(scope="module")
def aapl_pages() -> list[str]:
    """Page texts of the frozen AAPL-2022Q3 filing (no live SEC)."""
    from fin.corpus.extracted import load_extracted_tree

    tree = load_extracted_tree(
        Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
    )
    return [page.text for page in tree[AAPL_2022Q3]]


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def wired(http_app: FastAPI):
    """In-memory store + a scripted graph whose script tests mutate."""
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store

    model = ScriptedChatModel(script=[AIMessage(content="never reached")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: graph
    return store, model


def _read_call(page: int, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {
                    "document_id": AAPL_2022Q3,
                    "version": AAPL_2022Q3_VERSION,
                    "page": page,
                    "page_limit": 1,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _send(client: TestClient, thread_id: str, question: str) -> None:
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": question},
    ) as r:
        assert r.status_code == 200, r.status_code
        "".join(r.iter_text())


def _locator(page: int) -> dict[str, Any]:
    return {
        "document_id": AAPL_2022Q3,
        "version": AAPL_2022Q3_VERSION,
        "page": page,
    }


# ----- thread_store: the persisted column -----


def test_run_record_defaults_evidence_locators_to_empty_list() -> None:
    """A run that read nothing persists ``[]`` — never ``None``."""
    from fin.host.thread_store import RunRecord

    record = RunRecord(id="r", thread_id="t", question="q", status="done")
    assert record.evidence_locators == []


def test_in_memory_store_round_trips_evidence_locators() -> None:
    """``append_run`` -> ``list_runs`` keeps the locators, and the
    returned copy is isolated from store state (same deepcopy
    contract the citations list already has)."""
    from fin.host.thread_store import InMemoryThreadStore, RunRecord

    store = InMemoryThreadStore()
    store.create_thread(id="t-el", title="x")
    store.append_run(
        RunRecord(
            id="r-el",
            thread_id="t-el",
            question="q",
            status="refuse",
            status_reason="citation_repair_cap",
            evidence_locators=[_locator(1)],
        )
    )

    runs = store.list_runs("t-el")
    assert runs[0].evidence_locators == [_locator(1)]

    runs[0].evidence_locators.append(_locator(2))
    assert store.list_runs("t-el")[0].evidence_locators == [_locator(1)]


def test_postgres_schema_declares_evidence_locators_column() -> None:
    """Fresh installs get the column in ``CREATE TABLE``; existing
    ``fin_run`` tables get it through an additive ``ADD COLUMN IF NOT
    EXISTS`` so a durable boot does not raise on the first insert."""
    from fin.host.thread_store import _SCHEMA_SQL

    assert "evidence_locators JSONB NOT NULL DEFAULT '[]'::jsonb" in _SCHEMA_SQL
    assert (
        "ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS evidence_locators "
        "JSONB NOT NULL DEFAULT '[]'::jsonb" in _SCHEMA_SQL
    )


def test_postgres_insert_and_read_round_trip_evidence_locators(monkeypatch) -> None:
    """The INSERT serializes the locators as JSON and ``_row_to_run``
    parses them back. Stubbed psycopg: no live database."""
    import json

    from fin.host.thread_store import PostgresThreadStore, RunRecord

    captured: list[tuple] = []

    class _StubCursor:
        def __init__(self, value: Any) -> None:
            self._value = value

        def fetchone(self) -> Any:
            return self._value

    class _StubConn:
        def execute(self, sql: str, params: tuple | None = None) -> Any:
            if params is not None:
                captured.append(params)
            return _StubCursor(None)

        def commit(self) -> None:
            pass

    class _StubCM:
        def __enter__(self) -> _StubConn:
            return _StubConn()

        def __exit__(self, *exc: Any) -> bool:
            return False

    import fin.host.thread_store as ts_mod

    monkeypatch.setattr(ts_mod.psycopg, "connect", lambda *a, **kw: _StubCM())

    store = PostgresThreadStore.__new__(PostgresThreadStore)
    store._url = "postgresql://stub"
    store.append_run(
        RunRecord(
            id="r-pg",
            thread_id="t-pg",
            question="q",
            status="done",
            evidence_locators=[_locator(4)],
        )
    )

    assert captured, "append_run issued no INSERT"
    params = captured[-1]
    serialized = [p for p in params if isinstance(p, str) and "document_id" in p]
    assert serialized, params
    assert json.loads(serialized[-1]) == [_locator(4)]


# ----- run_payload: derived from this run's evidence -----


def _evidence_item(page: int, text: str) -> Any:
    from fin.corpus.extracted import Locator
    from fin.host.citation_gate import EvidenceItem

    return EvidenceItem(
        locator=Locator(
            document_id=AAPL_2022Q3, version=AAPL_2022Q3_VERSION, page=page
        ),
        page_text=text,
    )


def test_payload_evidence_locators_are_empty_without_evidence() -> None:
    from fin.host.run_payload import _run_to_response_payload

    payload = _run_to_response_payload({"status": "done", "answer": "a"})
    assert payload["evidence_locators"] == []


def test_payload_deduplicates_locators_in_first_seen_order() -> None:
    """Retrieve emits several chunks per page; the turn row lists each
    locator once, in the order the run first touched it."""
    from fin.host.run_payload import _run_to_response_payload

    payload = _run_to_response_payload(
        {
            "status": "done",
            "answer": "a",
            "evidence": [
                _evidence_item(4, "chunk one"),
                _evidence_item(1, "page one"),
                _evidence_item(4, "chunk two"),
            ],
        }
    )
    assert payload["evidence_locators"] == [_locator(4), _locator(1)]


def test_payload_keeps_locators_on_refuse_even_though_citations_are_empty() -> None:
    """SC8: the row records what the run READ, not what the verifier
    passed — a refused run stays auditable."""
    from fin.host.run_payload import _run_to_response_payload

    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": "citation_repair_cap",
            "evidence": [_evidence_item(1, "page one")],
        }
    )
    assert payload["citations"] == []
    assert payload["evidence_locators"] == [_locator(1)]


# ----- workspace HTTP: GET history + follow-up isolation -----


def test_get_history_turn_row_carries_this_run_evidence_locators(
    client: TestClient, wired, aapl_pages: list[str]
) -> None:
    """A ``done`` run that read page 1 exposes that locator on
    ``GET /api/threads/{id}``."""
    _store, model = wired
    created = client.post("/api/threads", json={"first_message": "q1"}).json()

    model._index = 0
    model.script = [
        _read_call(1, "c-1"),
        AIMessage(content=f"Per the filing: {aapl_pages[0][:80]}"),
    ]
    _send(client, created["id"], "q1")

    turns = client.get(f"/api/threads/{created['id']}").json()["turns"]
    assert len(turns) == 1
    assert turns[0]["status"] == "done"
    assert turns[0]["evidence_locators"] == [_locator(1)]


def test_refused_run_still_records_the_page_it_read(
    client: TestClient, wired, aapl_pages: list[str]
) -> None:
    """The run reads page 1 and then answers with a span that is not
    on it: citations are empty, ``evidence_locators`` is not."""
    _store, model = wired
    created = client.post("/api/threads", json={"first_message": "q1"}).json()

    model._index = 0
    model.script = [
        _read_call(1, "c-1"),
        AIMessage(content="Revenue moved higher, though I cannot quote the filing."),
    ]
    _send(client, created["id"], "q1")

    turn = client.get(f"/api/threads/{created['id']}").json()["turns"][0]
    assert turn["status"] == "refuse"
    assert turn["citations"] == []
    assert turn["evidence_locators"] == [_locator(1)]


def test_follow_up_run_does_not_inherit_previous_run_locators(
    client: TestClient, wired, aapl_pages: list[str]
) -> None:
    """SC8 second half: run 2 reads page 2 only, so its row must NOT
    list run 1's page 1 — the run-scoped evidence reset (ADR 0016) is
    what the persisted locators reflect."""
    _store, model = wired
    created = client.post("/api/threads", json={"first_message": "q1"}).json()
    tid = created["id"]

    model._index = 0
    model.script = [
        _read_call(1, "c-1"),
        AIMessage(content=f"Per the filing: {aapl_pages[0][:80]}"),
    ]
    _send(client, tid, "q1")

    model._index = 0
    model.script = [
        _read_call(2, "c-2"),
        AIMessage(content=f"Per page two: {aapl_pages[1][:80]}"),
    ]
    _send(client, tid, "q2")

    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns) == 2
    assert turns[0]["evidence_locators"] == [_locator(1)]
    assert turns[1]["evidence_locators"] == [_locator(2)]
    assert _locator(1) not in turns[1]["evidence_locators"]
