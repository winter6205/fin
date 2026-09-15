"""T3 (ADR 0044 / plans/session-compaction.md): thread-detail occupancy.

``GET /api/threads/{id}`` reports token occupancy derived from the
SAME figure the auto-compact gate computes —
``OccupancyCompactor.occupancy`` over the checkpointed messages vs
``SESSION_TOKEN_BUDGET``. The figure rides the thread-detail
response, never ``GET /api/status`` (ADR 0033 OUT-list: no
context-window percent on the boot surface).

Two design pins asserted here:

* the response value equals a fresh ``OccupancyCompactor().occupancy``
  over the SAME checkpoint messages the test reads through the
  compiled graph — one shared figure, no second estimator;
* ``GET /api/status`` still answers exactly the four ADR 0033
  fields — adding occupancy there would smuggle run-scoped
  telemetry onto a boot surface.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.http import app
from fin.host.session_compaction import (
    SESSION_TOKEN_BUDGET,
    OccupancyCompactor,
    count_tokens_approximately,
)

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


@pytest.fixture
def http_app() -> FastAPI:
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, ThreadStore

    store: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    default_graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: default_graph
    return store


def _install_graph(http_app: FastAPI) -> Any:
    """Bind a graph whose model answers ``done`` immediately."""
    from fin.host.http import get_graph

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="全部完成。")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _post_message(client: TestClient, thread_id: str, question: str) -> None:
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": question},
    ) as r:
        assert r.status_code == 200, r.status_code
        "".join(r.iter_text())


# ----- the shared figure -----


def test_thread_detail_occupancy_matches_compactor_figure(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """After a run, the detail response's occupancy equals
    ``OccupancyCompactor().occupancy`` over the SAME checkpoint
    messages — T1 and T3 share ONE figure (acceptance criterion)."""
    g = _install_graph(http_app)
    created = client.post("/api/threads", json={"first_message": "q"}).json()
    _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    messages = (
        g._graph.get_state({"configurable": {"thread_id": created["id"]}}).values
        or {}
    ).get("messages") or []
    assert messages, "test precondition: the run must have checkpointed messages"
    expected = OccupancyCompactor().occupancy(messages)
    expected_tokens = count_tokens_approximately(messages)

    detail = client.get(f"/api/threads/{created['id']}").json()
    occ = detail["occupancy"]
    assert occ is not None
    assert occ["estimated_tokens"] == expected_tokens
    assert occ["budget_tokens"] == SESSION_TOKEN_BUDGET
    assert occ["ratio"] == pytest.approx(expected)


def test_thread_detail_occupancy_absent_for_never_run_thread(
    client: TestClient, fresh_store
) -> None:
    """A freshly created thread has no checkpoint, hence no
    model-facing payload — the field is ``None`` and the UI stays
    neutral rather than showing a fabricated 0%."""
    created = client.post("/api/threads", json={"first_message": "q"}).json()
    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["occupancy"] is None


def test_thread_detail_unknown_thread_still_404(
    client: TestClient, fresh_store
) -> None:
    """Occupancy must not change the existing 404 semantics for an
    unknown thread id."""
    r = client.get("/api/threads/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


# ----- ADR 0033 guard: /api/status stays boot-scoped -----


def test_status_has_no_occupancy_field(client: TestClient, fresh_store) -> None:
    """HARD CONSTRAINT (ADR 0033 OUT-list, restated by ADR 0044):
    ``GET /api/status`` must NOT gain any occupancy /
    context-window percent field. The occupancy figure belongs to
    the thread-detail response only."""
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "knowledge_mcp",
        "retrieve_adapter_kind",
        "model_identity",
        "corpus_filing_count",
    }
    assert "occupancy" not in body
    assert "context_window_pct" not in body
