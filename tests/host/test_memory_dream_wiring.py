"""T6c wiring contracts for dream (``specs/memory-dream.md`` SC17-SC20).

``CONTEXT.md:46`` forbids running dream inside ``model_node``, so dream is
not a graph node: the HTTP layer schedules it as a background task once
the SSE stream has closed. These tests pin that shape — where dream runs,
when it runs, and that it can never reach the operator's turn.

Boundary classes: empty (no threads recorded, dream never scheduled);
negative (an unknown backend kind, a port that is OFF); overflow (not a
wiring concern — the batch cap lives in T6b); concurrent (out of scope
here, ``claim`` covers the window race in ``test_memory_dream.py``);
exception (a port whose ``dream`` raises must not break the turn).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from fin.host.memory_dream import NullDreamBackend
from fin.host.scripted_model import ScriptedChatModel
from fin.host.memory_port import MemoryConfigError, OffMemoryAdapter

_SSE_ROUTES = {"send_message", "resume_thread"}


class _RecordingMemoryPort:
    """Minimal ``MemoryPort`` that records when dream ran.

    ``on_dream`` runs inside ``dream()`` so a test can snapshot state at
    the moment dream fires — that is how the ordering claim is proven.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        explode: bool = False,
        on_dream=None,
    ) -> None:
        self.enabled = enabled
        self.dream_calls = 0
        self._explode = explode
        self._on_dream = on_dream

    def dream(self, *, limit: int = 8) -> dict[str, object]:
        self.dream_calls += 1
        if self._on_dream is not None:
            self._on_dream()
        if self._explode:
            raise RuntimeError("simulated dream backend outage")
        return {"status": "buffered", "transported": 0}


@pytest.fixture
def http_app() -> Any:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _wire_graph_and_store(http_app: FastAPI) -> tuple[Any, str]:
    """Mount a scripted graph that completes one turn, return (app, tid)."""
    from fin.host.graph import compile_graph
    from fin.host.http import get_graph, get_thread_store
    from fin.host.scripted_model import ScriptedChatModel
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    http_app.dependency_overrides[get_graph] = lambda: compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="Noted.")]),
        known_document_ids=set(),
    )
    thread = store.create_thread(title="dream wiring")
    return store, thread.id


def _leave_a_pending_checkpoint(
    graph: Any, *, thread_id: str, question: str
) -> tuple[str, ...]:
    """Crash a run between a complete tool-call message and ``tools``.

    ``TestClient`` buffers the HTTP response and drains the whole
    generator before ``iter_text()`` yields, so the send route can
    never crash mid-graph deterministically — the crash has to be
    driven at the graph proxy directly. Abandoning the generator
    right after the model-complete ``values`` frame releases the
    proxy's lock via its ``finally`` and leaves the checkpoint at
    ``next=('tools',)``, which is exactly the state ``POST /resume``
    needs to do work instead of answering 204.

    ``question`` must land on the research track: the Soft-Track
    ``dialogue`` leaf runs the model and goes straight to ``END``, so
    a crash there leaves no pending node to resume.
    """
    from fin.host.state import reset_input_state

    generator = graph.stream(
        reset_input_state(question),
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="values",
    )
    for state in generator:
        messages = state.get("messages") or []
        if messages and getattr(messages[-1], "tool_calls", None):
            # Model node has just written the complete tool-call
            # AIMessage; abandon BEFORE the tool node runs.
            break
    # Explicit close so the proxy's lock-releasing ``finally`` runs
    # now rather than whenever CPython gets around to collecting it.
    generator.close()

    pending = graph.pending_nodes(thread_id)
    assert pending == ("tools",), (
        f"crash precondition failed: expected a pending tool node, got {pending}"
    )
    return pending


# ----- SC17: dream is not a graph node -----


def test_compile_graph_has_no_dream_node() -> None:
    from fin.host.graph import compile_graph
    from fin.host.scripted_model import ScriptedChatModel

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="x")]),
        known_document_ids=set(),
    )

    nodes = set(graph._graph.nodes)

    assert "dream" not in nodes
    assert "model" in nodes
    assert "citation_gate" in nodes


def test_compile_graph_takes_no_dream_parameter() -> None:
    import inspect

    from fin.host.graph import compile_graph

    assert "dream" not in inspect.signature(compile_graph).parameters


# ----- SC20: the backend seam has its own config key -----


def test_memory_dream_backend_uses_its_own_config_key() -> None:
    """ADR 0035: retrieve MCP and memory MCP must not share a config key."""
    from fin.host.memory_port import (
        DEFAULT_MEMORY_DREAM_BACKEND,
        MEMORY_DREAM_BACKEND_ENV_VAR,
        MEMORY_DREAM_BACKEND_KINDS,
    )

    assert MEMORY_DREAM_BACKEND_ENV_VAR != "FIN_RETRIEVE_ADAPTER"
    assert MEMORY_DREAM_BACKEND_ENV_VAR.startswith("FIN_MEMORY_")
    assert DEFAULT_MEMORY_DREAM_BACKEND == "null"
    # T6d adds "mcp" once iknow-memory exposes a memory tool.
    assert "mcp" not in MEMORY_DREAM_BACKEND_KINDS


def test_dream_backend_resolution_ignores_the_retrieve_env() -> None:
    from fin.host.compile import _resolve_memory_dream_backend

    assert _resolve_memory_dream_backend({}) == "null"
    assert (
        _resolve_memory_dream_backend({"FIN_RETRIEVE_ADAPTER": "fake"}) == "null"
    )
    assert (
        _resolve_memory_dream_backend({"FIN_MEMORY_DREAM_BACKEND": "null"})
        == "null"
    )


def test_build_memory_dream_backend_returns_the_null_backend_by_default() -> None:
    from fin.host.memory_port import build_memory_dream_backend

    assert isinstance(build_memory_dream_backend("null"), NullDreamBackend)


@pytest.mark.parametrize("kind", ["mcp", "", "   ", "MCP"])
def test_an_unwired_dream_backend_kind_is_a_typed_config_error(
    kind: str,
) -> None:
    """A misspelled kind must stop the boot, not fall back to ``null``."""
    from fin.host.memory_port import build_memory_dream_backend

    with pytest.raises(MemoryConfigError):
        build_memory_dream_backend(kind)


def test_the_wired_null_kind_tolerates_case_and_padding() -> None:
    from fin.host.memory_port import build_memory_dream_backend

    assert isinstance(build_memory_dream_backend(" NULL "), NullDreamBackend)


def test_the_runtime_memory_port_stays_off_until_enabled() -> None:
    """SC2: the default boot opens no Postgres connection at all."""
    from fin.host.compile import build_memory_port_for_runtime

    assert isinstance(build_memory_port_for_runtime(env={}), OffMemoryAdapter)


# ----- SC17: the HTTP layer runs dream after the stream closes -----


def test_dream_runs_after_the_turn_is_already_persisted(client: TestClient) -> None:
    """Dream fires once the generator is done, never during the turn.

    The probe cannot be "was the socket still open" — Starlette runs
    background tasks inside the same ASGI call, so by the time the test
    client has drained the body the task has already run. What matters
    is that dream saw a *finished* turn, so the port snapshots the run
    rows at the moment it fires: the turn's row is already there.
    """
    from fin.host.http import get_memory_port

    store, thread_id = _wire_graph_and_store(client.app)
    runs_at_dream: list[Any] = []
    port = _RecordingMemoryPort(
        on_dream=lambda: runs_at_dream.extend(store.list_runs(thread_id))
    )
    client.app.dependency_overrides[get_memory_port] = lambda: port

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "remember this"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: citations" in body
    assert port.dream_calls == 1
    assert runs_at_dream, "dream ran before the turn was persisted"


def test_both_sse_routes_carry_the_background_hook() -> None:
    """The resume route carries the same hook as the send route.

    Driving a crash-resume end to end needs the whole T4 harness; the
    hook itself is the contract under test, and a route cannot schedule
    a background task without declaring the parameter.
    """
    from typing import get_type_hints

    from fastapi import BackgroundTasks

    from fin.host.workspace_http import build_router

    router = build_router(get_graph=lambda: None, get_thread_store=lambda: None)
    declared = {
        route.endpoint.__name__: BackgroundTasks
        in get_type_hints(route.endpoint).values()
        for route in router.routes
        if getattr(route.endpoint, "__name__", "") in _SSE_ROUTES
    }

    assert declared == {"send_message": True, "resume_thread": True}


class _ToolCallThenAnswerModel(ScriptedChatModel):
    """Streamed script: one ``list_files`` call, then a plain answer.

    ``model_node`` always calls ``model.stream``, so the pending
    checkpoint has to be produced on the streaming surface: the
    first response carries a complete tool call (which routes to
    ``tools``), the second is the terminal answer the resumed run
    delivers.
    """

    def __init__(self) -> None:
        super().__init__(
            stream_responses=[
                [
                    AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": "list_files",
                                "args": "{}",
                                "id": "call-list",
                                "index": 0,
                            }
                        ],
                    )
                ],
                [AIMessageChunk(content="Resumed cleanly.")],
            ]
        )


def test_dream_also_runs_after_a_resumed_stream_closes(
    client: TestClient,
) -> None:
    """The resume route schedules dream too, once its SSE is drained.

    Dream is registered on both SSE routes, but registration is not
    proof: resume exits early on 204 and never reaches the stream.
    This drives the real thing — a run crashed between a complete
    tool-call message and the tool node, then resumed — so the
    assertion covers the path where a turn actually ran. The port
    snapshots the run rows when dream fires, proving dream saw the
    finished remainder rather than firing mid-stream.
    """
    from fin.host.graph import compile_graph
    from fin.host.http import get_graph, get_memory_port

    store, thread_id = _wire_graph_and_store(client.app)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_ToolCallThenAnswerModel(),
        known_document_ids=set(),
    )
    client.app.dependency_overrides[get_graph] = lambda: graph
    _leave_a_pending_checkpoint(
        graph,
        thread_id=thread_id,
        question="What was Apple's revenue in Q3 2022?",
    )

    runs_at_dream: list[Any] = []
    port = _RecordingMemoryPort(
        on_dream=lambda: runs_at_dream.extend(store.list_runs(thread_id))
    )
    client.app.dependency_overrides[get_memory_port] = lambda: port

    with client.stream(
        "POST", f"/api/threads/{thread_id}/resume", json={}
    ) as response:
        # 200 (not 204): the crash left a pending tool node, so the
        # resume really ran a turn.
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: citations" in body
    assert port.dream_calls == 1
    assert runs_at_dream, "dream ran before the resumed turn was persisted"
    assert runs_at_dream[-1].status in {"done", "refuse", "step_cap"}
    # The remainder finished, so a second Resume has nothing to do.
    assert graph.pending_nodes(thread_id) == ()


def test_a_resume_with_nothing_to_resume_does_not_schedule_dream(
    client: TestClient,
) -> None:
    """204 means no turn ran, so there is nothing to dream about."""
    from fin.host.http import get_memory_port

    port = _RecordingMemoryPort()
    client.app.dependency_overrides[get_memory_port] = lambda: port
    _store, thread_id = _wire_graph_and_store(client.app)

    response = client.post(f"/api/threads/{thread_id}/resume", json={})

    assert response.status_code == 204
    assert port.dream_calls == 0


def test_an_off_memory_port_is_never_asked_to_dream(client: TestClient) -> None:
    """SC2: memory OFF must not even start dream."""
    from fin.host.http import get_memory_port

    port = _RecordingMemoryPort(enabled=False)
    client.app.dependency_overrides[get_memory_port] = lambda: port
    _store, thread_id = _wire_graph_and_store(client.app)

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "remember this"},
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert port.dream_calls == 0


def test_the_default_off_port_dreams_to_nothing() -> None:
    """The unwired default is the OFF adapter, which dreams to no-op."""
    from fin.host.memory_port import build_memory_port

    assert isinstance(build_memory_port(env={}), OffMemoryAdapter)
    assert build_memory_port(env={}).dream() == {
        "status": "disabled",
        "transported": 0,
    }


def test_a_204_empty_question_does_not_schedule_dream(
    client: TestClient,
) -> None:
    from fin.host.http import get_memory_port

    port = _RecordingMemoryPort()
    client.app.dependency_overrides[get_memory_port] = lambda: port
    _store, thread_id = _wire_graph_and_store(client.app)

    response = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": "   "}
    )

    assert response.status_code == 204
    assert port.dream_calls == 0


# ----- SC21: dream failures never reach the foreground turn -----


def test_a_failing_dream_does_not_break_the_turn(client: TestClient) -> None:
    from fin.host.http import get_memory_port

    port = _RecordingMemoryPort(explode=True)
    client.app.dependency_overrides[get_memory_port] = lambda: port
    _store, thread_id = _wire_graph_and_store(client.app)

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "remember this"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    # The turn delivered its terminal frames; only dream failed.
    assert "event: citations" in body
    assert port.dream_calls == 1


def test_a_failing_dream_still_leaves_the_run_persisted(
    client: TestClient,
) -> None:
    from fin.host.http import get_memory_port

    port = _RecordingMemoryPort(explode=True)
    client.app.dependency_overrides[get_memory_port] = lambda: port
    store, thread_id = _wire_graph_and_store(client.app)

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "remember this"},
    ) as response:
        "".join(response.iter_text())

    runs = store.list_runs(thread_id)
    assert runs and runs[-1].status in {"done", "refuse", "step_cap"}


def test_the_memory_port_provider_is_a_process_singleton() -> None:
    """One pool per process: the Postgres buffer opens its own pool."""
    from fin.host.http import get_memory_port

    assert get_memory_port() is get_memory_port()
