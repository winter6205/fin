"""The developer receipt view: one turn's receipt, and a 404 without one.

A receipt answers questions no other surface can — how the turn ended,
what it measured, what was still unknown when it did — so it has to be
readable by whoever is debugging the turn rather than reconstructed from
the run logs. It is read on its OWN route: ``/api/status`` is polled and
boot-scoped, and the thread view is the conversation, so neither can
grow a per-turn accounting without breaking one of those contracts.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.thread_store import InMemoryThreadStore, ThreadStore
from fin.host.turn_receipt_store import STAGE_METRIC_KEYS

KNOWN_IDS = {"AAPL-2022Q3"}


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def store(http_app: FastAPI) -> ThreadStore:
    from fin.host.http import get_graph, get_thread_store

    memory: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: memory
    http_app.dependency_overrides[get_graph] = lambda: compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="Net sales grew.")]),
        known_document_ids=KNOWN_IDS,
    )
    return memory


def _receipt_url(thread_id: str, turn_id: str) -> str:
    return f"/api/threads/{thread_id}/turns/{turn_id}/receipt"


def _answered_turn(client: TestClient, store: ThreadStore) -> tuple[str, str]:
    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]
    assert (
        client.post(
            f"/api/threads/{thread_id}/messages", json={"question": "q"}
        ).status_code
        == 200
    )
    (turn_id,) = [run.id for run in store.list_runs(thread_id)]
    return thread_id, turn_id


def test_the_view_returns_the_receipt_of_a_finished_turn(
    client: TestClient, store: ThreadStore
) -> None:
    thread_id, turn_id = _answered_turn(client, store)

    response = client.get(_receipt_url(thread_id, turn_id))

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert body["turn_id"] == turn_id
    assert body["execution_state"] == "terminal"
    assert body["technical_result"] == "done"
    assert body["revision"] >= 1
    assert set(body["stage_metrics"]) == set(STAGE_METRIC_KEYS)
    assert body["terminal_at"].endswith("+00:00")


def test_the_view_reports_a_turn_that_has_not_ended(
    client: TestClient, store: ThreadStore
) -> None:
    """A running receipt is readable, not hidden until it ends.

    A turn that is stuck is exactly the turn an operator needs to read,
    and a view that only answered for finished turns would be empty
    during the incident it exists for.
    """
    from fin.host.turn_receipts import default_receipts

    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]
    default_receipts(store).begin(
        thread_id=thread_id,
        turn_id="run-open",
        accepted_input_version=1,
    )

    body = client.get(_receipt_url(thread_id, "run-open")).json()

    assert body["execution_state"] == "running"
    assert body["technical_result"] is None
    assert body["terminal_at"] is None


def test_the_view_404s_for_a_turn_that_has_no_receipt(
    client: TestClient, store: ThreadStore
) -> None:
    thread_id = client.post(
        "/api/threads", json={"first_message": "研究"}
    ).json()["id"]

    response = client.get(_receipt_url(thread_id, "run-never-began"))

    assert response.status_code == 404
    assert response.json() == {"error": "receipt_not_found"}


def test_the_view_404s_for_an_unknown_thread(client: TestClient) -> None:
    response = client.get(_receipt_url("does-not-exist", "run-1"))

    assert response.status_code == 404
    assert response.json() == {"error": "receipt_not_found"}


def test_the_view_carries_no_conversation_or_source_text(
    client: TestClient, store: ThreadStore
) -> None:
    """The receipt's own vocabulary, and nothing outside it.

    The stored row holds no question, answer or source identity, so a
    payload carrying one would mean the view invented it.
    """
    from fin.host.turn_receipt_store import _COLUMNS

    thread_id, turn_id = _answered_turn(client, store)

    body = client.get(_receipt_url(thread_id, turn_id)).json()

    assert set(body) == set(_COLUMNS)
    text = " ".join(str(value) for value in body.values())
    for absent in ("Net sales grew", "AAPL-2022Q3", "revenue"):
        assert absent not in text, absent


def test_the_view_is_not_on_the_status_surface(client: TestClient) -> None:
    """It has its own route; the boot-scoped one did not grow a field."""
    assert "receipt" not in client.get("/api/status").json()
