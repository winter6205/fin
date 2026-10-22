"""Workspace "work after the answer" routes (T4).

Spec Focus 3 / SC4-SC6 on the HTTP surface:

* watchlist CRUD (``/api/watchlist``) — empty is ``[]``, reload keeps
  the rows;
* in-app notices (``/api/notices``) — a watched filing that is in the
  LOCAL catalog surfaces in the workspace, nothing is sent outbound;
* Stop (``POST /api/threads/{id}/stop``) — the in-flight run ends with
  no new HumanMessage, a later send is accepted, and a concurrent send
  is still 409 ``{"error": "concurrent_run"}`` before any SSE header;
* export (``GET /api/threads/{id}/export``) — a file body for the
  thread's ``fin.comparison`` comparison or its citations; a missing
  run is a typed 404.

Fixture pattern mirrors ``tests/host/test_workspace_http.py``: a fresh
app per test with ``dependency_overrides`` for the graph, the thread
store, and (new here) the watchlist store.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

import csv
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import Citation, ScriptedChatModel, compile_graph
from fin.host.grounded.citation_gate import COMPANY_KIND
from fin.host.thread_store import InMemoryThreadStore, RunRecord
from fin.host.tools.compare_facts import COMPARISON_SCHEMA, COMPARISON_SCHEMA_VERSION
from fin.host.watchlist import JsonFileWatchlistStore


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    load_private_fixture("f8d906a074fb0edaa7b850fc49d304023186190ed8e2bf9cdcdffb49ee802cff")
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def watchlist_path(tmp_path: Path) -> Path:
    return tmp_path / "watchlist.json"


@pytest.fixture
def stores(http_app: FastAPI, watchlist_path: Path) -> InMemoryThreadStore:
    """Bind fresh thread + watchlist stores and a no-op graph."""
    from fin.host.http import get_graph, get_thread_store, get_watchlist_store

    thread_store = InMemoryThreadStore()
    watchlist_store = JsonFileWatchlistStore(watchlist_path)
    http_app.dependency_overrides[get_thread_store] = lambda: thread_store
    http_app.dependency_overrides[get_watchlist_store] = lambda: watchlist_store

    default_graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: default_graph
    return thread_store


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    return events


# ----- watchlist CRUD (SC4) -----


def test_empty_watchlist_is_an_empty_list(client: TestClient, stores) -> None:
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    assert r.json() == []


def test_create_list_delete_a_watch(client: TestClient, stores) -> None:
    created = client.post(
        "/api/watchlist", json={"issuer": "aapl", "forms": ["10-k", "8-K"]}
    )
    assert created.status_code == 200
    body = created.json()
    assert body["issuer"] == "AAPL"
    assert body["forms"] == ["10-K", "8-K"]
    assert body["id"]

    listed = client.get("/api/watchlist").json()
    assert [w["id"] for w in listed] == [body["id"]]

    deleted = client.delete(f"/api/watchlist/{body['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/watchlist").json() == []


def test_create_watch_without_an_issuer_is_a_typed_400(
    client: TestClient, stores
) -> None:
    r = client.post("/api/watchlist", json={"issuer": "  ", "forms": []})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_watch"}


def test_delete_unknown_watch_is_a_typed_404(client: TestClient, stores) -> None:
    r = client.delete("/api/watchlist/nope")
    assert r.status_code == 404
    assert r.json() == {"error": "watch_not_found"}


def test_watchlist_survives_a_process_restart(
    client: TestClient, http_app: FastAPI, stores, watchlist_path: Path
) -> None:
    """SC4: "after process restart ... list matches". A restart is a NEW
    store instance over the same backing document."""
    from fin.host.http import get_watchlist_store

    created = client.post(
        "/api/watchlist", json={"issuer": "AAPL", "forms": ["10-Q"]}
    ).json()

    restarted = JsonFileWatchlistStore(watchlist_path)
    http_app.dependency_overrides[get_watchlist_store] = lambda: restarted

    listed = client.get("/api/watchlist").json()
    assert [w["id"] for w in listed] == [created["id"]]
    assert listed[0]["forms"] == ["10-Q"]


# ----- in-app notices (Focus 3) -----


def test_a_watched_filing_in_the_local_catalog_surfaces_as_a_notice(
    client: TestClient, stores
) -> None:
    """The frozen-12 catalog already holds AAPL 10-Q filings, so a watch
    on that issuer + form has something to announce in-app."""
    client.post("/api/watchlist", json={"issuer": "AAPL", "forms": ["10-Q"]})

    r = client.get("/api/notices")
    assert r.status_code == 200
    notices = r.json()
    assert notices, "a watched issuer already in the catalog raised no notice"
    assert {n["issuer"] for n in notices} == {"AAPL"}
    assert all(n["form"] == "10-Q" for n in notices)
    assert any(n["document_id"].startswith("AAPL-") for n in notices)


def test_an_unwatched_issuer_raises_no_notice(client: TestClient, stores) -> None:
    client.post("/api/watchlist", json={"issuer": "ZZZZ", "forms": []})
    assert client.get("/api/notices").json() == []


def test_notices_are_not_duplicated_on_a_second_read(
    client: TestClient, stores
) -> None:
    client.post("/api/watchlist", json={"issuer": "AAPL", "forms": ["10-Q"]})
    first = client.get("/api/notices").json()
    second = client.get("/api/notices").json()
    assert [n["id"] for n in first] == [n["id"] for n in second]


def test_marking_a_notice_read_hides_it_from_the_unread_view(
    client: TestClient, stores
) -> None:
    client.post("/api/watchlist", json={"issuer": "AAPL", "forms": ["10-Q"]})
    notice = client.get("/api/notices").json()[0]

    r = client.post(f"/api/notices/{notice['id']}/read")
    assert r.status_code == 200

    unread = client.get("/api/notices", params={"unread_only": "true"}).json()
    assert notice["id"] not in [n["id"] for n in unread]


def test_marking_an_unknown_notice_read_is_a_typed_404(
    client: TestClient, stores
) -> None:
    r = client.post("/api/notices/nope/read")
    assert r.status_code == 404
    assert r.json() == {"error": "notice_not_found"}


# ----- stop (SC5) -----


def _blocking_graph(http_app: FastAPI) -> tuple[Any, threading.Event, threading.Event]:
    """A graph whose model blocks until ``release`` is set."""
    from fin.host.http import get_graph

    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = BlockingModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": AAPL_2022Q3,
                                "version": AAPL_2022Q3_VERSION,
                                "unit": 1,
                            },
                            "position": 0,
                        },
                        "id": "c-read",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="x" * 40),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: graph
    return graph, entered, release


def test_stop_ends_the_in_flight_run_without_a_new_human_message(
    client: TestClient, http_app: FastAPI, stores
) -> None:
    graph, entered, release = _blocking_graph(http_app)
    # The in-flight leg this graph drives is the bounded one: the deleted
    # whole-page tool is not bindable at all.
    tool_names = [tool.name for tool in graph._bound_tools]
    assert "read_source" in tool_names
    assert "read_file" not in tool_names

    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]
    frames: dict[str, Any] = {}

    def send() -> None:
        with client.stream(
            "POST", f"/api/threads/{tid}/messages", json={"question": "compare AAPL"}
        ) as r:
            frames["status_code"] = r.status_code
            frames["body"] = "".join(r.iter_text())

    with ThreadPoolExecutor(max_workers=2) as ex:
        future = ex.submit(send)
        assert entered.wait(timeout=5.0), "the run never entered the graph"

        stop = client.post(f"/api/threads/{tid}/stop")
        assert stop.status_code == 200
        assert stop.json()["stopped"] is True

        release.set()
        future.result(timeout=10.0)

    events = _parse_sse_events(frames["body"])
    kinds = [name for name, _ in events]
    assert kinds[-3:] == ["status", "answer", "citations"], kinds
    status_frame = dict(events[[n for n, _ in events].index("status")][1])
    assert status_frame["status"] == "refuse"
    assert status_frame["reason"] == "stopped"
    assert status_frame["assistant_message"]

    # SC5: the stop appends NO message of its own — the only human turn
    # on the thread is the operator's question.
    messages = graph._graph.get_state(
        {"configurable": {"thread_id": tid}}
    ).values["messages"]
    humans = [m for m in messages if isinstance(m, HumanMessage)]
    assert [m.content for m in humans] == ["compare AAPL"]

    # The stopped run is a terminal turn row, not a hole in history.
    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert [t["status"] for t in turns] == ["refuse"]
    assert turns[0]["status_reason"] == "stopped"
    assert turns[0]["in_flight"] is False


def test_after_a_stop_the_thread_accepts_a_new_send(
    client: TestClient, http_app: FastAPI, stores
) -> None:
    """SC5: "composer accepts a later send" — the lock is released and
    the next question runs on the same thread."""
    graph, entered, release = _blocking_graph(http_app)
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]

    def send(question: str) -> None:
        with client.stream(
            "POST", f"/api/threads/{tid}/messages", json={"question": question}
        ) as r:
            list(r.iter_text())

    with ThreadPoolExecutor(max_workers=2) as ex:
        future = ex.submit(send, "first question")
        assert entered.wait(timeout=5.0)
        client.post(f"/api/threads/{tid}/stop")
        release.set()
        future.result(timeout=10.0)

    second = client.post(
        f"/api/threads/{tid}/messages", json={"question": "second question"}
    )
    assert second.status_code == 200

    detail = client.get(f"/api/threads/{tid}").json()
    assert detail["in_flight"] is False
    assert [t["question"] for t in detail["turns"]] == [
        "first question",
        "second question",
    ]


def test_a_concurrent_send_is_still_409_while_a_run_is_stoppable(
    client: TestClient, http_app: FastAPI, stores
) -> None:
    """SC5: Stop does not weaken the concurrency rule — a second send
    during the SAME in-flight run is still 409 before any SSE header."""
    graph, entered, release = _blocking_graph(http_app)
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]

    def send() -> None:
        with client.stream(
            "POST", f"/api/threads/{tid}/messages", json={"question": "first"}
        ) as r:
            list(r.iter_text())

    with ThreadPoolExecutor(max_workers=2) as ex:
        future = ex.submit(send)
        assert entered.wait(timeout=5.0)

        concurrent = client.post(
            f"/api/threads/{tid}/messages", json={"question": "second"}
        )
        assert concurrent.status_code == 409
        assert concurrent.headers["content-type"].startswith("application/json")
        assert concurrent.json() == {"error": "concurrent_run"}

        release.set()
        time.sleep(0.05)
        client.post(f"/api/threads/{tid}/stop")
        future.result(timeout=10.0)


def test_stop_with_no_run_in_flight_is_a_204_no_op(
    client: TestClient, stores
) -> None:
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]
    r = client.post(f"/api/threads/{tid}/stop")
    assert r.status_code == 204
    assert r.content == b""


def test_stop_during_send_preflight_cancels_that_admitted_run(
    client: TestClient,
    http_app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop while send preflight is blocked follows its admission into the run."""
    from fin.host import workspace_http

    graph, model_entered, release_model = _blocking_graph(http_app)
    original_read = workspace_http._accepted_task_runtime_state
    preflight_entered = threading.Event()
    release_preflight = threading.Event()

    def blocked_read(graph: Any, thread_id: str):
        preflight_entered.set()
        assert release_preflight.wait(timeout=5.0), "preflight was not released"
        return original_read(graph, thread_id)

    monkeypatch.setattr(
        workspace_http, "_accepted_task_runtime_state", blocked_read
    )
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]
    sent: dict[str, Any] = {}

    def send() -> None:
        with client.stream(
            "POST", f"/api/threads/{tid}/messages", json={"question": "first"}
        ) as response:
            sent["status_code"] = response.status_code
            sent["body"] = "".join(response.iter_text())

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(send)
        stop = None
        try:
            assert preflight_entered.wait(timeout=5.0), "send did not enter preflight"
            stop = client.post(f"/api/threads/{tid}/stop")
        finally:
            release_preflight.set()
            release_model.set()
        future.result(timeout=10.0)

    assert stop is not None
    assert stop.status_code == 202
    assert stop.json() == {"stopped": True, "pending": True, "run_id": None}
    assert model_entered.is_set() is False

    events = _parse_sse_events(sent["body"])
    status = next(payload for name, payload in events if name == "status")
    assert status["status"] == "refuse"
    assert status["reason"] == "stopped"
    assert sent["status_code"] == 200
    receipt_response = client.get(
        f"/api/threads/{tid}/turns/{status['run_id']}/receipt"
    )
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["technical_result"] == "stopped"
    assert receipt["cancellation_reason"] == "user_stop"


def test_stop_during_resume_preflight_cancels_that_admitted_run(
    client: TestClient,
    http_app: FastAPI,
    stores,
) -> None:
    """A stop while resume inspects its checkpoint follows that claim."""
    from fin.host.http import get_graph

    pending_entered = threading.Event()
    release_pending = threading.Event()

    class ResumePreflightGraph:
        stream_entered = threading.Event()

        def is_locked(self, thread_id: str) -> bool:
            return False

        def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
            pending_entered.set()
            assert release_pending.wait(timeout=5.0), "resume preflight was not released"
            return ("resume_node",)

        def checkpoint_run_id(self, thread_id: str) -> str:
            return "resume-run"

        def stream(self, input_value: Any, *, config: dict, stream_mode: Any):
            self.stream_entered.set()
            if False:
                yield "values", {}

    graph = ResumePreflightGraph()
    http_app.dependency_overrides[get_graph] = lambda: graph
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]
    response_body: dict[str, Any] = {}

    def resume() -> None:
        with client.stream("POST", f"/api/threads/{tid}/resume") as response:
            response_body["status_code"] = response.status_code
            response_body["body"] = "".join(response.iter_text())

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(resume)
        stop = None
        try:
            assert pending_entered.wait(timeout=5.0), "resume did not inspect its checkpoint"
            stop = client.post(f"/api/threads/{tid}/stop")
        finally:
            release_pending.set()
        future.result(timeout=10.0)

    assert stop is not None
    assert stop.status_code == 202
    assert stop.json() == {"stopped": True, "pending": True, "run_id": None}
    assert graph.stream_entered.is_set() is False
    assert response_body["status_code"] == 200
    events = _parse_sse_events(response_body["body"])
    status = next(payload for name, payload in events if name == "status")
    assert status["status"] == "refuse"
    assert status["reason"] == "stopped"


def test_stop_on_an_unknown_thread_is_a_typed_404(
    client: TestClient, stores
) -> None:
    r = client.post("/api/threads/nope/stop")
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


# ----- export (SC6) -----


def _fact_locator() -> dict[str, Any]:
    """One company locator triple: the frozen-unit ordinal is ``unit``."""
    return {
        "document_id": AAPL_2022Q3,
        "version": AAPL_2022Q3_VERSION,
        "unit": 4,
    }


def _comparison_envelope(concept: str = "us-gaap:Revenues") -> dict:
    """A stored ``fin.comparison`` envelope, as a producer must mint it.

    ``schema`` and ``schema_version`` are an ATOMIC PAIR at every
    boundary (spec §2.1 rule 1, ADR 0093 decision 12): the token names
    the shape, the integer numbers it, and NO component infers, defaults
    or back-fills the number from the token. So the pair is stated here
    as the real producer does; an envelope naming the token and omitting
    the number is a PRODUCER TYPE BUG, and the export refuses it as
    ``malformed_evidence_contract`` by design rather than repairing it.
    (This fixture predated decision 12 and omitted the number, so the two
    export tests below were served a 422 instead of the CSV / JSON body
    they mean to pin.)
    """
    return {
        "schema": COMPARISON_SCHEMA,
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "kind": "table",
        "axis": "period",
        "concept": concept,
        "unit": "USD",
        "scale": "millions",
        "columns": [
            {"key": "2022-Q3", "label": "2022-Q3"},
            {"key": "2023-Q3", "label": "2023-Q3"},
        ],
        "rows": [
            {
                "key": "AAPL",
                "label": "AAPL",
                "kind": "issuer",
                "cells": [
                    {
                        "row": "AAPL",
                        "column": "2022-Q3",
                        "status": "hit",
                        "value": 194140.0,
                        "display": "194,140",
                        "unit": "USD",
                        "scale": "millions",
                        "locator": _fact_locator(),
                    },
                    {
                        "row": "AAPL",
                        "column": "2023-Q3",
                        "status": "hit",
                        "value": load_private_fixture("df9e4df0d00599a1820d5afef6c4cd83e960f0e8bc8f1a76340084c8c5597f23"),
                        "display": load_private_fixture("c18591bcf0070a92596e8fa66285e8cb7eafe482710c7f278506d2292ef3c207"),
                        "unit": "USD",
                        "scale": "millions",
                        "locator": _fact_locator(),
                    },
                ],
            }
        ],
        "deltas": [
            {
                "from": {"row": "AAPL", "column": "2022-Q3"},
                "to": {"row": "AAPL", "column": "2023-Q3"},
                "absolute": 99647.0,
                "percent": 51.33,
                "direction": "up",
                "display": "+99,647 (+51.33%)",
            }
        ],
        "missing": [],
        "hit_count": 2,
        "cell_count": 2,
        "locators": [_fact_locator()],
    }


def _thread_with_run(client: TestClient, store, **run_kwargs) -> str:
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]
    store.append_run(
        RunRecord(
            id="run-export-1",
            thread_id=tid,
            question="compare AAPL",
            status="done",
            answer="x" * 40,
            **run_kwargs,
        )
    )
    return tid


def test_export_comparison_returns_a_csv_file_body(
    client: TestClient, stores
) -> None:
    tid = _thread_with_run(client, stores, comparisons=[_comparison_envelope()])

    r = client.get(f"/api/threads/{tid}/export", params={"kind": "comparison"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert ".csv" in r.headers["content-disposition"]
    body = r.text
    # Bound to the T3 envelope: schema line, the tool's cell displays,
    # and the tool's delta string (never recomputed here).
    assert COMPARISON_SCHEMA in body
    assert load_private_fixture("c18591bcf0070a92596e8fa66285e8cb7eafe482710c7f278506d2292ef3c207") in body
    assert "+99,647 (+51.33%)" in body
    assert AAPL_2022Q3 in body
    # The locator trail section is keyed by the frozen-unit ordinal; the
    # removed generic ``page`` column must not come back.
    assert "document_id,version,unit" in body
    assert "document_id,version,page" not in body


def test_export_comparison_as_json_is_the_envelope_verbatim(
    client: TestClient, stores
) -> None:
    envelope = _comparison_envelope()
    tid = _thread_with_run(client, stores, comparisons=[envelope])

    r = client.get(
        f"/api/threads/{tid}/export",
        params={"kind": "comparison", "format": "json"},
    )

    assert r.status_code == 200
    assert r.json()["comparisons"] == [envelope]


def test_export_citations_returns_a_csv_file_body(
    client: TestClient, stores
) -> None:
    """The citation export speaks the gate's own unit identity: the CSV
    trail is ``kind,source_id,version,unit,quote`` — ``source_id`` being
    whichever id that kind opens with — and a locator payload never
    carries the removed generic ``page`` ordinal."""
    citations = [
        asdict(
            Citation(
                kind=COMPANY_KIND,
                document_id=AAPL_2022Q3,
                attachment_id="",
                version=AAPL_2022Q3_VERSION,
                unit=1,
                display="page",
                filename="",
                quote="Total net sales increased during the third quarter",
            )
        )
    ]
    tid = _thread_with_run(client, stores, citations=citations)

    r = client.get(f"/api/threads/{tid}/export", params={"kind": "citations"})

    assert r.status_code == 200
    header = r.text.splitlines()[0]
    assert header == "kind,source_id,version,unit,quote"
    assert "page" not in header.split(",")
    assert citations[0]["quote"] in r.text
    # The row is the gate's unit identity, not a page ordinal: one row
    # per citation, in the header's column order, led by the kind whose
    # ``source_id`` the reader opens.
    rows = list(csv.reader(r.text.splitlines()))
    assert rows[1:] == [
        [
            COMPANY_KIND,
            AAPL_2022Q3,
            AAPL_2022Q3_VERSION,
            "1",
            citations[0]["quote"],
        ]
    ]


def test_export_from_a_thread_with_no_run_is_a_typed_404(
    client: TestClient, stores
) -> None:
    tid = client.post("/api/threads", json={"first_message": "q"}).json()["id"]

    r = client.get(f"/api/threads/{tid}/export", params={"kind": "comparison"})

    assert r.status_code == 404
    assert r.json() == {"error": "run_not_found"}


def test_export_with_an_unknown_run_id_is_a_typed_404(
    client: TestClient, stores
) -> None:
    tid = _thread_with_run(client, stores, comparisons=[_comparison_envelope()])

    r = client.get(
        f"/api/threads/{tid}/export",
        params={"kind": "comparison", "run_id": "nope"},
    )

    assert r.status_code == 404
    assert r.json() == {"error": "run_not_found"}


def test_export_when_the_run_has_nothing_of_that_kind_is_a_typed_404(
    client: TestClient, stores
) -> None:
    tid = _thread_with_run(client, stores, comparisons=[_comparison_envelope()])

    r = client.get(f"/api/threads/{tid}/export", params={"kind": "citations"})

    assert r.status_code == 404
    assert r.json() == {"error": "export_empty"}


def test_export_on_an_unknown_thread_is_a_typed_404(
    client: TestClient, stores
) -> None:
    r = client.get("/api/threads/nope/export", params={"kind": "comparison"})
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


def test_export_with_an_unknown_kind_is_a_typed_400(
    client: TestClient, stores
) -> None:
    tid = _thread_with_run(client, stores, comparisons=[_comparison_envelope()])

    r = client.get(f"/api/threads/{tid}/export", params={"kind": "spreadsheet"})

    assert r.status_code == 400
    assert r.json() == {"error": "unsupported_export"}
