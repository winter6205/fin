"""Tests for the v1 host HTTP surface (T9).

The HTTP surface is a thin POST ``/ask`` route over the same compiled
graph object the CLI uses (ADR 0013 / spec success criteria item 8).
These tests cover the contract spelled out in the T9 ticket:

* **request shape** — ``{"question": "...", "thread_id": "<opt>"}``.
* **response shape** — ``{"status", "answer", "citations",
  "thread_id", "reason"}`` where ``citations`` is always present
  (empty list when status is not ``done``), ``reason`` is set on
  ``refuse`` and ``step_cap`` only.
* **thread_id echo / auto-create** — caller may pass a UUID; if
  missing, the route generates a UUID4 and returns it.
* **status mapping** — ``done`` → citations populated;
  ``step_cap`` → status ``step_cap``, empty citations;
  ``refuse`` → status ``refuse``, reason in body (e.g.
  ``"unknown_locator"``).
* **typed errors** — ``ConcurrentRunError`` → 409 with
  ``{"error": "concurrent_run"}``; ``IngestMissingError`` → 503
  with ``{"error": "ingest_missing"}``.
* **422** — FastAPI/Pydantic validation: missing ``question``.
* **factory idempotence** — ``compile_for_http(checkpointer=X,
  model=Y) is compile_for_http(checkpointer=X, model=Y)``.
* **no auth surface** — no Authorization-header processing, no
  cookie parsing on the FastAPI app.

All tests use ``ScriptedChatModel`` + ``InMemorySaver`` (no live
LLM, no Postgres). The graph is injected via FastAPI's
``dependency_overrides`` so we can drive the route without booting
the prod runtime composer (T8).

The committed extracted tree at ``data/corpus/extracted/`` is
required for tests that exercise the tool functions; the
``aapl_page1_text`` fixture seeds a ≥32-char extractive span for
the scripted ``done`` run.
"""

from __future__ import annotations

import concurrent.futures
import re
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.errors import IngestMissingError
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import ConcurrentRunError


# ----- corpus fixtures (mirror tests/host/test_graph.py) -----

AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Legacy tree-backed read adapter over the committed corpus. Only the
# scripted ``read_file`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
from fin.retrieve import ExtractedTreeReadPort

_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    """Real page-1 text from AAPL-2022Q3 used to seed a ≥32-char span."""
    from fin.corpus.extracted import load_extracted_tree

    manifest = Path("data/corpus/manifest.json")
    extracted = Path("data/corpus/extracted")
    tree = load_extracted_tree(manifest, extracted)
    return tree[AAPL_2022Q3][0].text


# ----- app / client fixtures -----


@pytest.fixture
def http_app() -> FastAPI:
    """Import the FastAPI app fresh per test so dependency_overrides
    from one test do not leak into another."""
    from fin.host.http import app, get_graph

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _install_graph(app: FastAPI, script: list[AIMessage], *, thread_id_for_model: str | None = None) -> object:
    """Build a compiled graph with a scripted model and inject it as
    the route's dependency. Returns the graph so tests can inspect
    state."""
    from fin.host.http import get_graph

    model = ScriptedChatModel(script=script)
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    app.dependency_overrides[get_graph] = lambda: g
    return g


def _scripted_read_then_answer(answer: str, *, document_id: str = AAPL_2022Q3, version: str = AAPL_2022Q3_VERSION) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "document_id": document_id,
                        "version": version,
                        "page": 1,
                        "page_limit": 1,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


# ----- 200 happy path -----


def test_ask_minimal_question_returns_200(client: TestClient, aapl_page1_text: str) -> None:
    """POST /ask with only ``question`` returns 200, JSON shape,
    and an auto-generated thread_id (UUID4)."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(client.app, _scripted_read_then_answer(answer))

    r = client.post("/ask", json={"question": "what was AAPL revenue in 2022 Q3?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["answer"] == answer
    assert isinstance(body["citations"], list) and len(body["citations"]) >= 1
    # thread_id is auto-generated as UUID4
    assert isinstance(body["thread_id"], str)
    uuid.UUID(body["thread_id"])  # raises if not a valid UUID
    cit = body["citations"][0]
    assert set(cit.keys()) == {"document_id", "version", "page", "quote"}
    assert cit["document_id"] == AAPL_2022Q3
    assert cit["version"] == AAPL_2022Q3_VERSION
    assert cit["page"] == 1
    assert cit["quote"] == aapl_page1_text[:80]


def test_ask_explicit_thread_id_echoed(client: TestClient, aapl_page1_text: str) -> None:
    """POST /ask with an explicit ``thread_id`` echoes it back."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(client.app, _scripted_read_then_answer(answer))

    known_thread_id = str(uuid.uuid4())
    r = client.post(
        "/ask",
        json={"question": "what was AAPL revenue in 2022 Q3?", "thread_id": known_thread_id},
    )
    assert r.status_code == 200
    assert r.json()["thread_id"] == known_thread_id


def test_ask_done_includes_citations(client: TestClient, aapl_page1_text: str) -> None:
    """Scripted done run → response has non-empty citations list,
    status ``done``."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(client.app, _scripted_read_then_answer(answer))

    r = client.post("/ask", json={"question": "What was AAPL net sales in 2023 Q3?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert len(body["citations"]) >= 1
    # Citation shape matches the spec — never has a model-invented version
    cit = body["citations"][0]
    assert cit["version"] == AAPL_2022Q3_VERSION
    assert cit["quote"] == aapl_page1_text[:80]


# ----- step_cap -----


def test_ask_step_cap_returns_status_step_cap_with_empty_citations(client: TestClient) -> None:
    """Scripted infinite loop trips ``step_cap`` → status
    ``step_cap``, citations list is empty (per spec: "always
    present")."""
    loop_call = AIMessage(
        content="",
        tool_calls=[{"name": "list_files", "args": {}, "id": "c-loop", "type": "tool_call"}],
    )
    _install_graph(client.app, [loop_call])

    r = client.post("/ask", json={"question": "What was AAPL net sales in 2023 Q3?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "step_cap"
    assert body["citations"] == []
    # reason populated on step_cap per the schema
    assert body.get("reason") == "step_cap"


# ----- refuse -----


def test_ask_refuse_unknown_locator_status_refuse_with_reason(client: TestClient) -> None:
    """Scripted answer with an unknown document_id → status
    ``refuse``, ``reason`` in body."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Per the ZZZZ-9999Q1 filing, revenue was up across all "
                    "segments this quarter and operating margin improved."
                )
            )
        ]
    )
    _install_graph(client.app, model.script)

    r = client.post("/ask", json={"question": "What was AAPL net sales in 2023 Q3?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "refuse"
    assert body["reason"] == "unknown_locator"
    # Refuse has empty citations
    assert body["citations"] == []


def test_ask_refuse_citation_repair_cap_reason(client: TestClient, aapl_page1_text: str) -> None:
    """Scripted bounce-only loop → status ``refuse``, reason
    ``citation_repair_cap``."""
    _install_graph(
        client.app,
        _scripted_read_then_answer(
            # Deliberately no ≥32-char span from the page → bounces
            answer="vague answer with no evidence",
        ),
    )
    r = client.post("/ask", json={"question": "What was AAPL net sales in 2023 Q3?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "refuse"
    assert body["reason"] == "citation_repair_cap"
    assert body["citations"] == []


# ----- 422 validation -----


def test_ask_without_question_returns_422(client: TestClient) -> None:
    """POST /ask without ``question`` → 422 (Pydantic).

    The dependency override is installed even though the handler
    never runs: FastAPI resolves ``Depends(get_graph)`` before
    validating the body, so the graph must be injectable for the
    422 path to be reachable.
    """
    _install_graph(client.app, [AIMessage(content="never reached")])
    r = client.post("/ask", json={})
    assert r.status_code == 422
    detail = r.json().get("detail", [])
    # Pydantic reports the missing field by name
    assert any("question" in str(item).lower() for item in detail)


def test_ask_with_non_json_body_returns_422(client: TestClient) -> None:
    """POST /ask with non-JSON bytes and a JSON content-type → 422."""
    _install_graph(client.app, [AIMessage(content="never reached")])
    r = client.post(
        "/ask",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


# ----- 409 ConcurrentRunError -----


def test_ask_concurrent_run_returns_409(http_app: FastAPI) -> None:
    """Two POST /ask calls on the same ``thread_id`` simultaneously
    → exactly one returns 200, the other returns 409 with
    ``{"error": "concurrent_run"}``."""

    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    blocking = BlockingModel(script=[AIMessage(content="answer")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )

    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = lambda: g
    c = TestClient(http_app)

    results: dict[str, object] = {}

    def hit(label: str) -> None:
        r = c.post(
            "/ask",
            json={"question": "q", "thread_id": "t-http-race"},
        )
        results[label] = r

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(hit, "first")
        # Wait until the first call is inside model_node holding the lock.
        assert entered.wait(timeout=5.0), "first request never entered the graph"
        f2 = ex.submit(hit, "second")
        # Give the second request time to attempt the lock and raise.
        time.sleep(0.2)
        release.set()
        f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    statuses = sorted([r.status_code for r in results.values()])  # type: ignore[union-attr]
    assert statuses == [200, 409], f"unexpected statuses: {statuses}"

    error_body = next(r.json() for r in results.values() if r.status_code == 409)  # type: ignore[union-attr]
    assert error_body == {"error": "concurrent_run"}


# ----- 503 IngestMissingError -----


def test_ask_ingest_missing_returns_503(client: TestClient) -> None:
    """IngestMissingError raised during dep resolution → 503 with
    ``{"error": "ingest_missing"}`` body."""

    from fin.host.http import get_graph

    def boom() -> object:
        raise IngestMissingError("simulated missing extracted tree")

    client.app.dependency_overrides[get_graph] = boom

    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 503
    assert r.json() == {"error": "ingest_missing"}


# ----- factory idempotence -----


def test_compile_for_http_idempotent_same_args() -> None:
    """``compile_for_http`` with the SAME checkpointer + model
    returns the SAME object (T6 / T7 / T9 share one graph)."""
    from fin.host.http import compile_for_http

    cp = InMemorySaver()
    m = ScriptedChatModel(script=[AIMessage(content="x")])
    g1 = compile_for_http(checkpointer=cp, model=m, known_document_ids=KNOWN_IDS)
    g2 = compile_for_http(checkpointer=cp, model=m, known_document_ids=KNOWN_IDS)
    assert g1 is g2


def test_compile_for_http_distinct_args_yield_distinct_graphs() -> None:
    """Different args yield different graph objects (no leakage
    between distinct sessions)."""
    from fin.host.http import compile_for_http

    m1 = ScriptedChatModel(script=[AIMessage(content="a")])
    m2 = ScriptedChatModel(script=[AIMessage(content="b")])
    g1 = compile_for_http(checkpointer=InMemorySaver(), model=m1, known_document_ids=KNOWN_IDS)
    g2 = compile_for_http(checkpointer=InMemorySaver(), model=m2, known_document_ids=KNOWN_IDS)
    assert g1 is not g2


# ----- no auth surface -----


def test_http_app_has_no_auth_or_cookie_processing() -> None:
    """The FastAPI app must not process Authorization headers or
    cookies — v1 ships no auth (ADR 0013 / spec).

    Asserted two ways: (a) no security dependencies registered on
    any route; (b) a request carrying an ``Authorization`` header
    reaches the validation path (422 for missing body) — never
    401/403 — and no ``Set-Cookie`` header is emitted on response.
    """
    from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer, OAuth2  # noqa: F401

    from fin.host import http as http_module
    from fin.host.http import get_graph

    app = http_module.app
    # Inject a minimal graph so we can reach the route.
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="x")]),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    app.dependency_overrides[get_graph] = lambda: g

    # (a) No security dependencies registered on any route.
    security_classes = (HTTPBearer, APIKeyHeader, OAuth2)
    for route in app.routes:
        if hasattr(route, "dependant"):
            deps = (route.dependant.dependencies if route.dependant else [])
            for dep in deps:
                if isinstance(getattr(dep, "call", None), security_classes):
                    pytest.fail(
                        f"route {route.path!r} has security dependency {dep.call!r}"
                    )

    # (b) Send a request with an Authorization header — must reach
    # the validation path (422 for missing body), never 401/403.
    c = TestClient(app)
    r = c.post(
        "/ask",
        json={},
        headers={"Authorization": "Bearer should-be-ignored"},
    )
    assert r.status_code == 422  # validation, not auth
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}
    app.dependency_overrides.clear()


# ----- hidden bounce marker hygiene -----


def test_ask_response_does_not_leak_hidden_bounce_marker(client: TestClient, aapl_page1_text: str) -> None:
    """ADR 0021: the hidden bounce template marker must NEVER appear
    in the HTTP response. The v1 host graph stamps ``[FIN_HIDDEN_BOUNCE]``
    into internal SystemMessages; the HTTP surface must strip any
    leak before serializing."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(client.app, _scripted_read_then_answer(answer))

    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 200
    raw = r.text
    assert "[FIN_HIDDEN_BOUNCE]" not in raw


# ----- thread_id auto-create -----


def test_ask_thread_id_auto_create_is_uuid4(client: TestClient, aapl_page1_text: str) -> None:
    """Missing ``thread_id`` → server returns a fresh UUID4 (ADR
    0016: auto-create UUID if omitted, return it to the caller)."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(client.app, _scripted_read_then_answer(answer))

    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 200
    thread_id = r.json()["thread_id"]
    parsed = uuid.UUID(thread_id)
    assert parsed.version == 4


# ----- error message hygiene (defensive) -----


def test_ask_error_bodies_have_no_sensitive_leakage(client: TestClient) -> None:
    """Concurrent / ingest-missing error bodies must be the spec'd
    flat shape — no extra detail, no stack traces."""
    from fin.host.http import get_graph

    def boom() -> object:
        raise IngestMissingError("internal: sha256=abc123, /secret/path/page.pdf")

    client.app.dependency_overrides[get_graph] = boom
    r = client.post("/ask", json={"question": "q"})
    assert r.status_code == 503
    # Body must be exactly {"error": "ingest_missing"} — the internal
    # path / sha must not leak (security-boundaries: error info does
    # not leak sensitive details).
    body = r.json()
    assert body == {"error": "ingest_missing"}
    assert "sha256" not in r.text
    assert "secret" not in r.text



# ===== T2: /ask follow-up resets run-scoped fields (ADR 0016 / SC62) =====


def test_ask_follow_up_resets_evidence(client: TestClient, aapl_page1_text: str) -> None:
    """Binding SC62 mirror for ``POST /ask``: two sequential asks on
    the SAME ``thread_id``. Run 1 reads AAPL-2022Q3 page 1 and ends
    ``done``; run 2 answers with the same span WITHOUT re-fetching.
    With the T2 reset, evidence is empty in run 2 so the gate cannot
    match -> refuse. Without it, the stale evidence leaks in and the
    gate ``done``s (the bug).
    """
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model_script_holder: dict[str, list[AIMessage]] = {}

    from fin.host.http import get_graph

    # A scripted model whose script can be swapped between runs.
    class _SwappableModel(ScriptedChatModel):
        @property
        def script(self):  # type: ignore[override]
            return model_script_holder.get("script", [])

        @script.setter
        def script(self, value):
            model_script_holder["script"] = value

    model = _SwappableModel(script=_scripted_read_then_answer(answer))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    client.app.dependency_overrides[get_graph] = lambda: g

    thread_id = str(uuid.uuid4())

    r1 = client.post(
        "/ask",
        json={"question": "q1", "thread_id": thread_id},
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "done"
    assert len(r1.json()["citations"]) >= 1

    # Run 2: same span, no tool call. Must NOT inherit run-1 evidence.
    model.script = [AIMessage(content=answer)]

    r2 = client.post(
        "/ask",
        json={"question": "q2", "thread_id": thread_id},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "refuse", (
        f"SC62: /ask follow-up must not inherit prior-run evidence; got {body}"
    )
    assert body["reason"] == "citation_repair_cap"
    assert body["citations"] == []


# ----- workspace_root inject seam (spec SC8 / ADR 0049) -----


class _ConfigCapturingGraph:
    """Duck-typed graph that records the config of every invoke."""

    def __init__(self) -> None:
        self.configs: list[dict] = []

    def invoke(self, input: dict, config: dict | None = None, **kwargs):
        self.configs.append(config or {})
        return {
            "status": "done",
            "answer": "ok",
            "citations": (),
        }


def test_ask_injects_workspace_root(
    http_app: FastAPI, client: TestClient, tmp_path
) -> None:
    """SC8: ``/ask`` resolves the default 工作区 via
    ``get_workspace_root`` and stamps it into
    ``configurable.workspace_root`` for the graph invoke."""
    from fin.host.http import get_graph, get_workspace_root

    g = _ConfigCapturingGraph()
    http_app.dependency_overrides[get_graph] = lambda: g
    ws_root = tmp_path / "ws-ask"
    http_app.dependency_overrides[get_workspace_root] = lambda: ws_root

    r = client.post("/ask", json={"question": "q", "thread_id": "t-ask-seam"})
    assert r.status_code == 200

    assert g.configs, "graph must have been invoked once"
    configurable = g.configs[0].get("configurable") or {}
    assert configurable.get("workspace_root") == str(ws_root)
    assert configurable.get("thread_id") == "t-ask-seam"
