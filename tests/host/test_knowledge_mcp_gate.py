"""Conversation-entry hard gate when knowledge MCP is not 可达 (T2).

When the published snapshot is 缺 (``missing``) or 坏 (``fault``) and the
configured adapter is ``mcp``, the three conversation mouths refuse with
409 ``{"error": "knowledge_mcp_fault", "message": "知识 MCP 故障"}``:

* ``POST /api/threads``
* ``GET /api/threads/{id}`` for an existing id
* ``POST /api/threads/{id}/messages``

Contracts preserved:

* ``GET /api/threads`` (list) stays 200 and ungated;
* an unknown id stays 404 ``thread_not_found`` — the existence check
  runs BEFORE the gate;
* the 409 ``concurrent_run`` path is untouched;
* fake / scripted adapters and a never-probed snapshot (the hermetic
  default suite) are NOT gated.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_FAULT,
    KNOWLEDGE_MCP_MISSING,
    KNOWLEDGE_MCP_REACHABLE,
    knowledge_mcp_gate_closed,
    publish_knowledge_mcp_state,
)

KNOWN_IDS = {"AAPL-2022Q3"}

GATE_JSON = {"error": "knowledge_mcp_fault", "message": "知识 MCP 故障"}


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
    from fin.host.feedback import InMemoryFeedbackStore
    from fin.host.http import get_feedback_store, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    thread_store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: thread_store
    # GET /threads/{id} resolves feedback before the 409 gate. Operator
    # ``.env`` ``FIN_CHECKPOINT_URL`` must not pull Postgres into this
    # hermetic file (same override pattern as ``test_feedback_route``).
    http_app.dependency_overrides[get_feedback_store] = (
        lambda: InMemoryFeedbackStore()
    )
    return thread_store


@pytest.fixture
def graph(http_app: FastAPI) -> Any:
    from fin.host.http import get_graph

    built = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="answer")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: built
    return built


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "mcp")


def _assert_gate(r) -> None:
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == GATE_JSON


# ----- 缺 / 坏: the three mouths refuse -----


@pytest.mark.parametrize(
    "state", [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT]
)
def test_three_mouths_refuse_when_not_reachable(
    client: TestClient, store, graph, mcp_env: None, state: str
) -> None:
    publish_knowledge_mcp_state(state)
    existing = store.create_thread(title="existing")

    _assert_gate(client.post("/api/threads", json={"first_message": "q"}))
    _assert_gate(client.get(f"/api/threads/{existing.id}"))
    _assert_gate(
        client.post(
            f"/api/threads/{existing.id}/messages", json={"question": "q"}
        )
    )


@pytest.mark.parametrize(
    "state", [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT]
)
def test_list_stays_200_and_ungated(
    client: TestClient, store, mcp_env: None, state: str
) -> None:
    publish_knowledge_mcp_state(state)
    store.create_thread(title="existing")

    response = client.get("/api/threads")

    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.parametrize(
    "state", [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT]
)
def test_unknown_id_stays_404_after_the_gate(
    client: TestClient, store, graph, mcp_env: None, state: str
) -> None:
    """Ordering: the 404 wins for an unknown id, even while gated."""
    publish_knowledge_mcp_state(state)

    get_response = client.get("/api/threads/does-not-exist")
    assert get_response.status_code == 404
    assert get_response.json() == {"error": "thread_not_found"}

    send_response = client.post(
        "/api/threads/does-not-exist/messages", json={"question": "q"}
    )
    assert send_response.status_code == 404
    assert send_response.json() == {"error": "thread_not_found"}


def test_delete_and_attachments_and_stop_stay_ungated(
    client: TestClient, store, graph, mcp_env: None
) -> None:
    """Only the three conversation mouths gate; the rest keep their
    current contracts (DELETE 200 for an existing thread here)."""
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_MISSING)
    existing = store.create_thread(title="existing")

    response = client.delete(f"/api/threads/{existing.id}")

    assert response.status_code == 200


# ----- 可达: current contracts -----


def test_reachable_mcp_mouths_use_current_contracts(
    client: TestClient, store, graph, mcp_env: None
) -> None:
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_REACHABLE)

    created = client.post("/api/threads", json={"first_message": "q"})
    assert created.status_code == 200, created.text
    thread_id = created.json()["id"]

    detail = client.get(f"/api/threads/{thread_id}")
    assert detail.status_code == 200, detail.text

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "q"},
    ) as sent:
        assert sent.status_code == 200, sent.text
        assert sent.headers["content-type"].startswith("text/event-stream")
        list(sent.iter_text())


# ----- fake / scripted and never-probed are not gated -----


def test_fake_adapter_with_missing_snapshot_does_not_return_the_fault(
    client: TestClient, store, graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_MISSING)

    created = client.post("/api/threads", json={"first_message": "q"})
    assert created.status_code == 200
    thread_id = created.json()["id"]
    assert created.json().get("error") != "knowledge_mcp_fault"

    assert client.get(f"/api/threads/{thread_id}").status_code == 200

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "q"},
    ) as sent:
        assert sent.status_code == 200
        list(sent.iter_text())


def test_scripted_lane_with_missing_snapshot_does_not_return_the_fault(
    client: TestClient, store, graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scripted e2e lane forces the fake adapter while
    ``FIN_RETRIEVE_ADAPTER`` stays unset, so the gate must not misread
    it as the product ``mcp`` adapter."""
    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
    monkeypatch.setenv("FIN_SCRIPTED_MODEL", "1")
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_MISSING)

    assert knowledge_mcp_gate_closed() is False
    created = client.post("/api/threads", json={"first_message": "q"})
    assert created.status_code == 200
    assert created.json().get("error") != "knowledge_mcp_fault"


def test_never_probed_snapshot_leaves_mouths_unchanged(
    client: TestClient, store, graph, mcp_env: None
) -> None:
    """Regression guard for the rest of the default suite: no boot →
    no snapshot → gate open even when the adapter is ``mcp``."""
    assert knowledge_mcp_gate_closed() is False

    created = client.post("/api/threads", json={"first_message": "q"})
    assert created.status_code == 200
    assert client.get(f"/api/threads/{created.json()['id']}").status_code == 200
