"""T9: the ``GET /api/observability`` route and per-turn recording.

The route exposes the process observability collector's snapshot plus
the empty-hit rewrite-retry counters. Per ADR 0033's spirit it is a
COUNTERS-ONLY surface: terminal-status counts, latency aggregates, and
retry counts — never per-run data, never question / answer / URL text,
never credentials. ``/api/status`` and ``/health`` are deliberately
untouched (boot-scoped / process-scoped respectively).

Recording contract: every SSE send turn that reaches a terminal status
(``done`` / ``refuse`` / ``step_cap``) records that status and the
turn's wall-clock latency on the SAME collector the route reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.observability import ObservabilityCollector


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3}


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    from fin.corpus.extracted import load_extracted_tree

    manifest = Path("data/corpus/manifest.json")
    extracted = Path("data/corpus/extracted")
    tree = load_extracted_tree(manifest, extracted)
    return tree[AAPL_2022Q3][0].text


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def fresh_collector(http_app: FastAPI) -> ObservabilityCollector:
    """Hermetic collector: the route reads THIS instance, so counters
    from other tests' traffic in the same process cannot leak in."""
    from fin.host.http import get_observability_collector

    collector = ObservabilityCollector()
    http_app.dependency_overrides[get_observability_collector] = (
        lambda: collector
    )
    return collector


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    from fin.host.http import get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, ThreadStore

    store: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    return store


def _install_done_graph(http_app: FastAPI, aapl_page1_text: str) -> None:
    """A scripted graph whose single step answers ``done`` with one
    citation grounded in the frozen AAPL page."""
    from fin.host.http import get_graph

    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "document_id": AAPL_2022Q3,
                        "version": AAPL_2022Q3_VERSION,
                        "page": 1,
                        "page_limit": 1,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=f"Per the filing: {aapl_page1_text[:80]}"),
    ]
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: g


def _send_turn(client: TestClient, question: str) -> None:
    created = client.post("/api/threads", json={"first_message": "hi"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": question},
    ) as response:
        assert response.status_code == 200
        list(response.iter_text())


# ----- the route -----


def test_observability_route_returns_empty_snapshot_before_traffic(
    client: TestClient, fresh_store, fresh_collector: ObservabilityCollector
) -> None:
    response = client.get("/api/observability")

    assert response.status_code == 200
    assert response.json() == {
        "counts": {},
        "latency_ms": {"count": 0, "total": 0.0, "average": 0.0},
        "rewrite_retry": {"count": 0, "exhausted": 0},
    }


def test_observability_route_counts_one_done_turn_after_scripted_send(
    client: TestClient,
    fresh_store,
    fresh_collector: ObservabilityCollector,
    http_app: FastAPI,
    aapl_page1_text: str,
) -> None:
    _install_done_graph(http_app, aapl_page1_text)
    _send_turn(client, "what was revenue?")

    snapshot = client.get("/api/observability").json()

    assert snapshot["counts"] == {"done": 1}
    assert snapshot["latency_ms"]["count"] == 1
    assert snapshot["latency_ms"]["total"] >= 0.0
    assert snapshot["latency_ms"]["average"] >= 0.0


def test_observability_route_accumulates_across_turns(
    client: TestClient,
    fresh_store,
    fresh_collector: ObservabilityCollector,
    http_app: FastAPI,
    aapl_page1_text: str,
) -> None:
    _install_done_graph(http_app, aapl_page1_text)
    _send_turn(client, "first question")
    _send_turn(client, "second question")

    snapshot = client.get("/api/observability").json()

    assert snapshot["counts"] == {"done": 2}
    assert snapshot["latency_ms"]["count"] == 2


def test_observability_route_never_leaks_run_content(
    client: TestClient,
    fresh_store,
    fresh_collector: ObservabilityCollector,
    http_app: FastAPI,
    aapl_page1_text: str,
) -> None:
    """Counters only: the question text must not appear anywhere in
    the payload (ADR 0033 spirit — no per-run data on status surfaces)."""
    _install_done_graph(http_app, aapl_page1_text)
    question = "what was the secret revenue figure xyzzy?"
    _send_turn(client, question)

    body = client.get("/api/observability").json()
    rendered = json.dumps(body)

    assert question not in rendered
    assert "xyzzy" not in rendered
    assert set(body) == {"counts", "latency_ms", "rewrite_retry"}
    assert set(body["counts"]) <= {"done", "refuse", "step_cap"}


def test_observability_route_resolves_the_process_default_collector(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Without an override the route reads the per-process singleton —
    the same collector the retrieve tool records rewrite retries on."""
    response = client.get("/api/observability")

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert set(body) == {"counts", "latency_ms", "rewrite_retry"}
    assert set(body["rewrite_retry"]) == {"count", "exhausted"}
