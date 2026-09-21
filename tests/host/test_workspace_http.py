"""Tests for the workspace HTTP surface (T1, #28).

The workspace routes sit on the same FastAPI app as the v1
``/ask`` route (one process, one compiled graph - ADR 0013 / ADR
0024). This file covers the T1 surface:

* thread CRUD: ``GET /api/threads``, ``POST /api/threads``,
  ``GET /api/threads/{id}``, ``DELETE /api/threads/{id}``;
* message send: ``POST /api/threads/{id}/messages`` returns
  ``text/event-stream`` with terminal ``status`` / ``citations`` /
  ``answer`` events (T1 is sync invoke wrapped in an event stream
  - T3 will replace this with token-by-token streaming);
* corpus list: ``GET /api/corpus`` returns the expanded filing library.

Pattern
-------
Tests install a scripted graph via FastAPI's
``dependency_overrides[get_graph]`` (mirrors ``tests/host/test_http.py``)
AND install an in-memory ``ThreadStore`` via
``dependency_overrides[get_thread_store]``. The T1 surface is one
process; both overrides are independent.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.collection_bind import (
    BOUND_COLLECTION_NAME,
    BOUND_COLLECTION_UNBOUND,
)
from fin.host.state import reset_input_state


# ----- shared fixtures -----


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {
    AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1",
}


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    from fin.corpus.extracted import load_extracted_tree

    manifest = Path("data/corpus/manifest.json")
    extracted = Path("data/corpus/extracted")
    tree = load_extracted_tree(manifest, extracted)
    return tree[AAPL_2022Q3][0].text


@pytest.fixture
def http_app() -> FastAPI:
    """Fresh FastAPI app per test so dependency overrides do not leak."""
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    """In-memory thread store + a default scripted graph bound to the
    FastAPI app. Tests that need a specific graph install on top via
    ``_install_graph(http_app, ...)``."""
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, ThreadStore

    store: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store

    # Default graph: a no-op scripted model so routes that touch the
    # graph dependency resolve cleanly even when a test forgets to
    # install one. Tests that exercise the graph invoke behavior
    # overwrite this override via ``_install_graph``.
    default_graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: default_graph
    return store


def _install_graph(
    http_app: FastAPI,
    script: list[AIMessage],
    *,
    known_document_ids: set[str] | None = None,
) -> Any:
    """Build a scripted graph and bind it to ``http_app``'s ``/ask`` route.

    Returns the graph proxy so tests can poke at state directly when
    needed.
    """
    from fin.host.http import get_graph

    model = ScriptedChatModel(script=script)
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=known_document_ids or KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _read_then_answer_script(answer: str) -> list[AIMessage]:
    return [
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
        AIMessage(content=answer),
    ]


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    """Parse a ``text/event-stream`` body into ``(event, data)`` pairs."""
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
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


# ----- thread CRUD -----


def test_get_threads_empty_returns_empty_list(client: TestClient, fresh_store) -> None:
    """No threads yet -> empty array."""
    r = client.get("/api/threads")
    assert r.status_code == 200
    assert r.json() == []


def test_post_thread_with_first_message_truncates_title_to_50_chars_with_ellipsis(
    client: TestClient, fresh_store
) -> None:
    """First-line title, stripped, 50-char cap with trailing ellipsis."""
    long_message = "x" * 200  # 200 chars, one long line
    r = client.post("/api/threads", json={"first_message": long_message})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "x" * 50 + "…"
    assert len(body["title"]) == 51  # 50 + ellipsis


def test_post_thread_uses_only_first_line_of_first_message(
    client: TestClient, fresh_store
) -> None:
    """Newlines in ``first_message`` truncate at the first line."""
    r = client.post(
        "/api/threads",
        json={"first_message": "first line\nsecond line"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "first line"


def test_post_thread_without_first_message_uses_default_title(
    client: TestClient, fresh_store
) -> None:
    """Missing / empty ``first_message`` falls back to the default
    empty-thread copy."""
    r = client.post("/api/threads", json={"first_message": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "New thread"
    assert "id" in body
    assert "created_at" in body


def test_post_thread_then_get_returns_turn_table(
    client: TestClient, fresh_store, aapl_page1_text: str
) -> None:
    """After creating a thread, GET /api/threads/{id} returns the
    metadata + an empty turn list (no sends yet)."""
    created = client.post("/api/threads", json={"first_message": "hi"}).json()
    r = client.get(f"/api/threads/{created['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == created["id"]
    assert body["title"] == "hi"
    assert body["turns"] == []


def test_get_thread_unknown_returns_404(client: TestClient, fresh_store) -> None:
    r = client.get("/api/threads/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


def test_delete_thread_then_get_returns_404(
    client: TestClient, fresh_store
) -> None:
    created = client.post("/api/threads", json={"first_message": "x"}).json()
    r = client.delete(f"/api/threads/{created['id']}")
    assert r.status_code == 200

    r2 = client.get(f"/api/threads/{created['id']}")
    assert r2.status_code == 404


def test_delete_thread_unknown_returns_404(client: TestClient, fresh_store) -> None:
    r = client.delete("/api/threads/does-not-exist")
    assert r.status_code == 404


def test_delete_thread_calls_saver_delete_thread(
    client: TestClient, fresh_store, aapl_page1_text: str
) -> None:
    """The HTTP DELETE must invoke the checkpointer's
    ``delete_thread`` so the checkpoint tuples disappear too
    (ADR 0015)."""

    deleted_ids: list[str] = []

    class RecordingSaver(InMemorySaver):
        def delete_thread(self, thread_id: str) -> None:  # type: ignore[override]
            deleted_ids.append(thread_id)
            return super().delete_thread(thread_id)

    g = compile_graph(
        checkpointer=RecordingSaver(),
        model=ScriptedChatModel(
            script=_read_then_answer_script(f"Per the filing: {aapl_page1_text[:80]}")
        ),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    from fin.host.http import get_graph

    client.app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "hi"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "q"},
    ) as r:
        assert r.status_code == 200
        list(r.iter_text())

    r = client.delete(f"/api/threads/{created['id']}")
    assert r.status_code == 200
    assert deleted_ids == [created["id"]]


# ----- message send: terminal SSE events -----


def test_send_message_done_emits_status_then_citations_then_answer_events(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """A scripted done run emits ``status`` (done), ``citations``
    (non-empty), and ``answer`` events on the SSE stream - in that
    order - with no leaked hidden bounce marker."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()

    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())

    assert "[FIN_HIDDEN_BOUNCE]" not in body

    events = _parse_sse_events(body)
    event_types = [name for name, _ in events]
    assert "status" in event_types
    assert "citations" in event_types
    assert "answer" in event_types
    # Wire order per spec line 30 + T1 (#33): token deltas (answer)
    # stream BEFORE ``status``; the TERMINAL frames keep the fixed
    # ``status`` -> ``answer`` -> ``citations`` order with citations
    # TERMINAL so the client can close its reader.
    status_index = event_types.index("status")
    citations_index = len(event_types) - 1 - event_types[::-1].index("citations")
    answer_index = len(event_types) - 1 - event_types[::-1].index("answer")
    assert event_types[-1] == "citations"
    assert status_index < answer_index < citations_index

    status_event = events[status_index][1]
    assert status_event["status"] == "done"
    assert isinstance(status_event["run_id"], str) and len(status_event["run_id"]) == 36

    citations = events[citations_index][1]
    assert isinstance(citations, list)
    assert len(citations) >= 1
    cit = citations[0]
    assert set(cit.keys()) == {"document_id", "version", "page", "quote"}
    assert cit["document_id"] == AAPL_2022Q3

    answer_event = events[answer_index][1]
    assert answer_event["text"] == answer


def test_send_message_refuse_unknown_locator_emits_status_refuse_and_empty_citations(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A scripted answer that mentions an unknown locator ->
    status ``refuse``, citations ``[]``."""
    _install_graph(
        http_app,
        [
            AIMessage(
                content=(
                    "Per the ZZZZ-9999Q1 filing revenue grew across all "
                    "segments and operating margin improved."
                )
            )
        ],
    )

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?"},
    ) as r:
        body = "".join(r.iter_text())

    events = _parse_sse_events(body)
    event_types = [name for name, _ in events]
    # T1 (#33) wire contract: the refused attempt's prose streams as
    # ``answer`` DELTAS while the run is live; the TERMINAL frames
    # after ``status`` are the fixed ``status`` -> ``answer`` ("") ->
    # ``citations`` ([]) with citations terminal. A refuse therefore
    # shows [answer-delta, ..., status, answer(""), citations([])].
    assert event_types[0] == "answer"
    assert event_types[-1] == "citations"
    status_i = event_types.index("status")
    assert event_types[status_i + 1] == "answer"
    assert event_types[status_i + 2] == "citations"
    assert events[status_i][1]["status"] == "refuse"
    assert events[status_i + 1][1] == {"text": ""}
    assert events[status_i + 2][1] == []


def test_send_message_step_cap_emits_status_step_cap_and_empty_citations(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Step-cap run -> status ``step_cap``, citations ``[]``."""
    _install_graph(
        http_app,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "list_files", "args": {}, "id": "c-loop", "type": "tool_call"}
                ],
            )
        ],
    )

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?"},
    ) as r:
        body = "".join(r.iter_text())

    events = _parse_sse_events(body)
    event_types = [name for name, _ in events]
    # Wire order per spec line 30: the terminal frames keep the
    # fixed ``status`` -> ``answer`` -> ``citations`` order;
    # T3 streaming frames (thinking / tool_call / tool_result)
    # appear BEFORE ``status``. A step_cap run whose model
    # never reaches done keeps looping tool calls - so the SSE
    # carries one tool_call / tool_result per loop tick before
    # the final ``status`` (``step_cap``).
    assert "status" in event_types
    assert "answer" in event_types
    assert "citations" in event_types
    status_i = event_types.index("status")
    answer_i = event_types.index("answer")
    citations_i = event_types.index("citations")
    assert status_i < answer_i < citations_i
    # Streaming frames precede the terminal status frame.
    assert event_types.index("tool_call") < status_i
    assert event_types.index("tool_result") < status_i
    status_event = events[status_i][1]
    assert status_event["status"] == "step_cap"
    assert events[answer_i][1] == {"text": ""}
    assert events[citations_i][1] == []


def test_send_message_empty_question_returns_204_no_content(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """``question`` that strips to empty -> 204 with no body (no-op).

    The route must NOT touch the graph on empty submit."""
    _install_graph(http_app, _read_then_answer_script("never reached"))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    r = client.post(
        f"/api/threads/{created['id']}/messages", json={"question": "   "}
    )
    assert r.status_code == 204
    assert r.content == b""


def test_send_message_concurrent_returns_409_no_event_stream_body(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Two parallel POSTs on the same thread -> exactly one 200, one
    409. The 409 body must NOT be ``text/event-stream``."""
    entered = threading.Event()
    release = threading.Event()

    from fin.host.scripted_model import ScriptedChatModel as SCM

    class BlockingModel(SCM):
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

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    results: dict[str, Any] = {}

    def hit(label: str) -> None:
        r = client.post(
            f"/api/threads/{created['id']}/messages",
            json={"question": "q"},
        )
        results[label] = r

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(hit, "first")
        assert entered.wait(timeout=5.0), "first request never entered the graph"
        f2 = ex.submit(hit, "second")
        time.sleep(0.2)
        release.set()
        f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 409], statuses

    error_response = next(r for r in results.values() if r.status_code == 409)
    assert error_response.headers["content-type"].startswith("application/json")
    assert error_response.json() == {"error": "concurrent_run"}


def test_send_message_unknown_thread_returns_404(
    client: TestClient, fresh_store
) -> None:
    r = client.post("/api/threads/nope/messages", json={"question": "q"})
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


def test_send_message_ingest_missing_returns_503(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """``compile_for_http`` raises ``IngestMissingError`` -> 503."""

    from fin.corpus.errors import IngestMissingError
    from fin.host.http import get_graph

    def boom() -> object:
        raise IngestMissingError("simulated missing extracted tree")

    http_app.dependency_overrides[get_graph] = boom

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    r = client.post(
        f"/api/threads/{created['id']}/messages", json={"question": "q"}
    )
    assert r.status_code == 503
    assert r.json() == {"error": "ingest_missing"}


def test_send_message_response_does_not_leak_hidden_bounce_marker(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Defensive: the marker never appears in the streamed body or in
    the persisted turn row."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "q"},
    ) as r:
        body = "".join(r.iter_text())

    assert "[FIN_HIDDEN_BOUNCE]" not in body

    thread_body = client.get(f"/api/threads/{created['id']}").json()
    serialized = json.dumps(thread_body)
    assert "[FIN_HIDDEN_BOUNCE]" not in serialized


# ===== T3 streaming frames on POST /messages =====


def test_send_message_streaming_emits_thinking_and_tool_call_frames(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """A scripted model that streams thinking prose via
    ``reasoning_details`` (ADR 0022) AND emits a tool call produces
    the T3 wire shape:

    ``thinking`` -> ``tool_call`` -> ``tool_result`` -> ``status``
    -> ``answer`` -> ``citations`` (terminal per spec line 30).

    The ``thinking`` frame carries the streamed prose; the
    ``tool_call`` frame carries ``name`` / ``args`` /
    ``tool_call_id``; the ``tool_result`` frame parses the
    tool's JSON content."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel

    answer = f"Per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content="",
                additional_kwargs={
                    "reasoning_details": [{"text": "let me read the filing"}]
                },
                tool_call_chunks=[
                    {"name": "read_file", "args": "", "id": "c-read", "index": 0}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": None,
                        "args": '{"document_id": "AAPL-2022Q3", "version": "%s", "page": 1, "page_limit": 1}'
                        % AAPL_2022Q3_VERSION,
                        "id": None,
                        "index": 0,
                    }
                ],
            ),
        ],
        script=[
            AIMessage(content=answer),
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")
    types = [name for name, _ in events]
    assert types.index("thinking") < types.index("status")
    assert types.index("tool_call") < types.index("tool_result") < types.index("status")

    thinking_event = next(d for n, d in events if n == "thinking")
    assert "let me read the filing" in thinking_event["text"]

    tool_call_event = next(d for n, d in events if n == "tool_call")
    assert tool_call_event["name"] == "read_file"
    assert tool_call_event["tool_call_id"] == "c-read"
    assert tool_call_event["args"]["document_id"] == "AAPL-2022Q3"

    tool_result_event = next(d for n, d in events if n == "tool_result")
    assert tool_result_event["tool_call_id"] == "c-read"
    assert tool_result_event["name"] == "read_file"
    # T3 fix (code-review #3): the wire shape is the human-readable
    # summary, NOT the raw ToolMessage JSON. Spec line 32.
    assert "summary" in tool_result_event
    assert "AAPL-2022Q3" in tool_result_event["summary"]
    assert "result" not in tool_result_event

    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "done"

    answer_event = next(d for n, d in events if n == "answer")
    assert answer_event["text"] == answer

    citations_event = next(d for n, d in events if n == "citations")
    assert isinstance(citations_event, list) and len(citations_event) >= 1


def test_send_message_tool_result_summary_matches_get_history(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """turn-trace-folds T1 (#249): the SSE ``tool_result`` summary
    passes the same args the graph persist path passes, so a
    ``retrieve`` one-liner shows its query live AND the GET
    ``tool_summaries`` entry for the same ``tool_call_id`` matches."""
    from fin.corpus.extracted import Locator
    from fin.host.http import get_graph
    from fin.retrieve.fake_port import FakeRetrievePort
    from fin.retrieve.port import Chunk

    query = "iphone revenue"
    adapter = FakeRetrievePort(
        hits_by_query={
            query: [
                Chunk(
                    text="revenue rose",
                    source_ref=Locator(
                        document_id="AAPL-2022Q3",
                        version="v-" + "a" * 60,
                        page=1,
                    ),
                    score=1.0,
                )
            ]
        }
    )
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": query, "limit": 8},
                        "id": "c-retr",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="The answer."),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=adapter,
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(
        client, created["id"], "What was AAPL net sales in 2023 Q3?"
    )

    tool_result_event = next(d for n, d in events if n == "tool_result")
    assert tool_result_event["tool_call_id"] == "c-retr"
    assert query in tool_result_event["summary"]

    detail = client.get(f"/api/threads/{created['id']}").json()
    summaries = detail["turns"][0]["tool_summaries"]
    assert len(summaries) == 1
    entry = summaries[0]
    assert entry["tool_call_id"] == "c-retr"
    assert entry["summary"] == tool_result_event["summary"]


def test_get_history_keeps_row_for_empty_tool_call_id(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """turn-trace-folds spec line 36: an empty ``tool_call_id`` still
    renders a row — GET history must not drop the call."""
    from fin.host.http import get_graph

    model = ScriptedChatModel(
        script=[
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
                        "id": "",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Noted."),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(
        client, created["id"], "What was AAPL net sales in 2023 Q3?"
    )

    # The SSE tool_call frame still emits with the empty id.
    tool_call_event = next(d for n, d in events if n == "tool_call")
    assert tool_call_event["tool_call_id"] == ""

    detail = client.get(f"/api/threads/{created['id']}").json()
    summaries = detail["turns"][0]["tool_summaries"]
    assert len(summaries) == 1
    assert summaries[0]["tool_call_id"] == ""
    assert summaries[0]["name"] == "read_file"


def test_send_message_turn_row_exposes_tool_summaries_as_dicts(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """T3 (spec line 32): the persisted turn row carries
    ``tool_summaries`` as a list of ``{name, summary, tool_call_id}``
    dicts (one per tool call). The SPA renders ``summary`` and
    correlates by ``tool_call_id``."""
    from fin.host.http import get_graph

    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    detail = client.get(f"/api/threads/{created['id']}").json()
    run = detail["turns"][0]
    summaries = run["tool_summaries"]
    assert len(summaries) == 1
    entry = summaries[0]
    assert set(entry.keys()) == {"name", "summary", "tool_call_id"}
    assert entry["name"] == "read_file"
    assert entry["tool_call_id"] == "c-read"
    assert "AAPL-2022Q3" in entry["summary"]
    assert "p.1" in entry["summary"]


def test_send_message_thinking_frame_emitted_with_stripped_prose(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """SC60 binding: the SSE ``thinking`` frame carries the
    scratchpad prose verbatim (it is the model's private notes);
    the SSE ``answer`` frame carries the GROUNDED answer prose
    with any inline ``<thinking>...</thinking>`` block already
    stripped (the citation-gate seam). A model that inlined the
    thinking into content MUST emit the thinking frame AND
    surface the post-strip answer text in the answer frame."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel

    span = aapl_page1_text[:80]
    raw_answer = f"<thinking>private scratchpad</thinking>{span}"
    model = ScriptedChatModel(
        script=[
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
            AIMessage(content=raw_answer),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    # The answer frame must carry the post-strip span ONLY - no
    # inline ``<thinking>`` block (SC60 binding on the wire).
    answer_event = next(d for n, d in events if n == "answer")
    assert "private scratchpad" not in answer_event["text"]
    assert answer_event["text"] == span

    # The inline ``<thinking>`` block IS surfaced on its own
    # ``thinking`` frame (the SPA renders it collapsed) - while
    # the ANSWER frame carries only the post-strip span. That is
    # the SC60 wire binding: scratchpad visible as thinking,
    # never inside answer.
    thinking_events = [d for n, d in events if n == "thinking"]
    assert len(thinking_events) == 1
    assert "private scratchpad" in thinking_events[0]["text"]

    # The persisted turn row mirrors the SSE: answer without
    # inline thinking, ``thinking_text`` populated with the
    # extracted inline prose (so a reload renders the same
    # scratchpad the live frame showed).
    detail = client.get(f"/api/threads/{created['id']}").json()
    run = detail["turns"][0]
    assert "private scratchpad" not in run["answer"]
    assert run["thinking_text"] == "private scratchpad"


# ----- H2 regression: saver failure surfaces as 500 -----


def test_delete_thread_saver_failure_returns_500_not_silent_200(
    client: TestClient, fresh_store
) -> None:
    """A checkpointer whose ``delete_thread`` raises must surface a
    500 — NOT a silent 200 that hides orphaned checkpoint tuples.

    The old code swallowed every exception (``except: pass``), so
    a saver outage left checkpoint rows behind while the UI showed
    success. The metadata row is still deleted first (user intent),
    but the operator must see the failure.
    """

    class ExplodingSaver(InMemorySaver):
        def delete_thread(self, thread_id: str) -> None:  # type: ignore[override]
            raise RuntimeError("simulated saver outage")

    g = compile_graph(
        checkpointer=ExplodingSaver(),
        model=ScriptedChatModel(script=[AIMessage(content="x")]),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    from fin.host.http import get_graph

    client.app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "x"}).json()
    r = client.delete(f"/api/threads/{created['id']}")
    assert r.status_code == 500
    assert r.json() == {"detail": "delete failed"}
    # Metadata is still gone despite the 500 (delete-first order).
    assert client.get(f"/api/threads/{created['id']}").status_code == 404


def test_delete_thread_without_checkpointer_warns_but_returns_200(
    client: TestClient, fresh_store
) -> None:
    """A graph proxy with NO reachable checkpointer.delete_thread is
    a wiring bug -> warn + 200 (metadata already gone), not 500."""

    class NoCheckpointerGraph:
        """Mimics the graph dependency but has no delete seam."""

        def invoke(self, *args: Any, **kwargs: Any) -> dict:
            return {}

    from fin.host.http import get_graph

    client.app.dependency_overrides[get_graph] = lambda: NoCheckpointerGraph()

    created = client.post("/api/threads", json={"first_message": "x"}).json()
    r = client.delete(f"/api/threads/{created['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/threads/{created['id']}").status_code == 404


def test_delete_thread_via_public_graph_seam_reaches_saver(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The route calls ``graph.delete_thread(thread_id)`` — the public
    ``_LockedGraph`` seam — not ``graph._graph.checkpointer...``
    internals. Asserted by patching the seam directly."""

    deleted_ids: list[str] = []

    class RecordingProxy:
        def __init__(self) -> None:
            from langgraph.checkpoint.memory import InMemorySaver as IMS

            self._saver = IMS()

        def invoke(self, *args: Any, **kwargs: Any) -> dict:
            return {}

        def delete_thread(self, thread_id: str) -> None:
            deleted_ids.append(thread_id)

    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = lambda: RecordingProxy()

    created = client.post("/api/threads", json={"first_message": "x"}).json()
    r = client.delete(f"/api/threads/{created['id']}")
    assert r.status_code == 200
    assert deleted_ids == [created["id"]]


# ----- M9 regression: store mints the thread id -----


def test_create_thread_lets_store_mint_the_id(client: TestClient, fresh_store) -> None:
    """The route passes only ``title`` to the store; the store mints
    the UUID. The response exposes the store-minted id so the client
    can navigate to the thread."""
    r1 = client.post("/api/threads", json={"first_message": "a"})
    r2 = client.post("/api/threads", json={"first_message": "b"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    id1 = r1.json()["id"]
    id2 = r2.json()["id"]
    assert id1 != id2
    assert len(id1) == 36  # uuid4 with dashes
    assert len(id2) == 36


# ----- corpus endpoint -----


def test_get_corpus_returns_library_filings_with_required_keys(
    client: TestClient, fresh_store
) -> None:
    """``GET /api/corpus`` returns the expanded library with the
    ADR 0009 shape."""
    r = client.get("/api/corpus")
    assert r.status_code == 200
    body = r.json()
    assert "filings" in body
    filings = body["filings"]
    assert len(filings) >= 21

    for f in filings:
        # SC3 adds ``form`` to the card; the rest of the EDGAR-shaped
        # submission header stays off the wire.
        assert set(f.keys()) == {
            "document_id",
            "company",
            "period",
            "version",
            "page_count",
            "form",
        }
        assert isinstance(f["page_count"], int)
        assert f["page_count"] >= 1
        assert f["form"] in {"10-Q", "10-K"}

    ids = sorted(f["document_id"] for f in filings)
    expected_substrings = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
    assert expected_substrings.issubset(set(ids))
    assert "GOOG-2023FY" in ids
    goog = next(f for f in filings if f["document_id"] == "GOOG-2023FY")
    assert goog["company"] == "GOOG"
    assert goog["period"] == "2023 FY"
    assert goog["form"] == "10-K"
    # Defensive: never return prototype-style ids.
    assert not any("Q3-2024" in fid for fid in ids)


def test_get_corpus_shows_each_form_the_corpus_actually_holds(
    client: TestClient, fresh_store, multi_form_corpus, monkeypatch
) -> None:
    """SC3: the card reports the form, not a hard-coded 10-Q.

    Pointed at a corpus of one 10-K, one 8-K and one 10-Q, the route
    reports all three — this is the surface the operator reads to see
    what is in the library.
    """
    monkeypatch.setattr(
        "fin.corpus.catalog.DEFAULT_CATALOG_PATH",
        multi_form_corpus["catalog_path"],
    )

    filings = client.get("/api/corpus").json()["filings"]

    assert {f["document_id"]: f["form"] for f in filings} == {
        "ZEUS-2023-10K": "10-K",
        "ZEUS-2023-8K": "8-K",
        "ZEUS-2023Q1": "10-Q",
    }


def test_get_corpus_with_missing_catalog_table_is_empty_filings(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """ADR 0047: a serve process whose corpus was never ingested has
    no catalog table file at all — ``GET /api/corpus`` answers 200
    ``{"filings": []}``, not a 503 and not an extracted-tree scan."""
    monkeypatch.setattr(
        "fin.corpus.catalog.DEFAULT_CATALOG_PATH",
        tmp_path / "absent" / "catalog.json",
    )

    r = client.get("/api/corpus")

    assert r.status_code == 200
    assert r.json() == {"filings": []}


def test_get_corpus_with_corrupt_catalog_table_is_typed_503(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """A table file that exists but violates the catalog schema raises
    ``CatalogCorruptError`` (an ``IngestMissingError`` subclass) — the
    shared handler's typed 503 ``ingest_missing``, never an empty 200
    success and never a bare 500."""
    corrupt = tmp_path / "catalog.json"
    corrupt.write_text("{ not json")
    monkeypatch.setattr(
        "fin.corpus.catalog.DEFAULT_CATALOG_PATH",
        corrupt,
    )

    r = client.get("/api/corpus")

    assert r.status_code == 503
    assert r.json() == {"error": "ingest_missing"}


# ----- run persistence: GET turn table reflects POST history -----


def test_post_thread_persists_to_postgres_thread_store_when_configured(
    monkeypatch,
) -> None:
    """When ``FIN_CHECKPOINT_URL`` is set, the default selector
    picks the Postgres implementation. The full round-trip against
    a real Postgres is covered by ``test_thread_store.py``; here we
    assert the wiring."""
    import os

    url = os.environ.get("FIN_CHECKPOINT_URL")
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL not configured; PostgresThreadStore is "
            "not wired in this environment"
        )

    from fin.host import http as http_module

    # The default selector picks Postgres when the env is set.
    selected = http_module.default_thread_store({"FIN_CHECKPOINT_URL": url})
    assert selected.__class__.__name__ == "PostgresThreadStore"


def test_post_message_persists_run_in_turn_table(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """After a POST /messages, GET /api/threads/{id} returns one
    turn row with the question, answer, status, and citations."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "what was AAPL revenue in 2022 Q3?"},
    ) as r:
        list(r.iter_text())

    thread = client.get(f"/api/threads/{created['id']}").json()
    turns = thread["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["status"] == "done"
    assert turn["answer"] == answer
    assert len(turn["citations"]) >= 1
    assert turn["question"] == "what was AAPL revenue in 2022 Q3?"
    assert isinstance(turn["run_id"], str) and len(turn["run_id"]) == 36


def test_post_message_unknown_thread_does_not_create_thread(
    client: TestClient, fresh_store
) -> None:
    """POST /messages to a non-existent thread does NOT create a
    phantom row."""
    r = client.post("/api/threads/nope/messages", json={"question": "q"})
    assert r.status_code == 404
    assert client.get("/api/threads").json() == []


# ----- H1 regression: SPA catch-all blocks path traversal -----


def test_send_message_run_id_carry_through_status_frame_matches_persisted_turn(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """The server-minted ``run_id`` on the ``status`` SSE frame
    must equal the ``run_id`` on the persisted RunRecord visible
    in ``GET /api/threads/{id}``. This is the wire contract the
    client uses to keep its optimistic turn aligned with the
    server row (H4 / Standards #4 / Spec #5). The previous
    implementation allowed the client to mint its own
    ``run_id``; this test pins the end-to-end invariant.
    """
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "q"},
    ) as r:
        body = "".join(r.iter_text())

    events = _parse_sse_events(body)
    status_event = next(data for name, data in events if name == "status")
    wire_run_id = status_event["run_id"]
    assert isinstance(wire_run_id, str) and len(wire_run_id) == 36

    persisted = client.get(f"/api/threads/{created['id']}").json()
    assert len(persisted["turns"]) == 1
    assert persisted["turns"][0]["run_id"] == wire_run_id


REAL_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

SPA_SHELL = (
    "<!doctype html>\n<html><head><title>fin</title></head>"
    '<body><div id="root"></div></body></html>\n'
)

# Legacy tree-backed read adapter over the committed corpus. Only the
# scripted ``read_file`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
from fin.retrieve import ExtractedTreeReadPort

_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)


def _build_fake_checkout(root: Path) -> Path:
    """Lay out a throwaway checkout that mirrors the repo shape the
    catch-all guards: ``<root>/web/dist`` next to sibling files an
    escape would reach (``<root>/pyproject.toml``,
    ``<root>/web/README.md``).

    Every escape target is written for real, so a 404 in these tests
    means containment refused an existing file rather than the file
    merely being absent — which is what the assertions against the
    real repo could only prove for ``pyproject.toml``.
    """
    dist = root / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(SPA_SHELL, encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "leaked-pyproject"\n', encoding="utf-8"
    )
    (root / "web" / "README.md").write_text(
        "leaked-readme\n", encoding="utf-8"
    )
    return dist


@pytest.fixture
def spa_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client over a fresh app whose SPA is mounted from a
    ``tmp_path`` dist.

    WHY not the module-level ``app``: ``web/dist`` is a Vite build
    artifact and is gitignored, so a clean checkout has no dist,
    ``mount_spa`` logs its WARNING and registers no catch-all, and
    any test asserting the SPA still serves fails for reasons that
    have nothing to do with the code under test (spec SC15). A
    ``tmp_path`` dist makes these cases depend only on
    ``mount_spa``.

    ``_spa_mounted`` is the module-level once-guard that makes the
    import-time mount idempotent; resetting it for the duration of
    the test is what lets a second mount register on our own app.
    ``monkeypatch`` restores it afterwards, so the real app's mount
    state is untouched.
    """
    from fin.host import http as http_module

    dist = _build_fake_checkout(tmp_path)
    monkeypatch.setattr(http_module, "_spa_mounted", False)
    app = FastAPI()
    http_module.mount_spa(app, dist_path=str(dist))
    return TestClient(app)


def test_spa_catchall_blocks_path_traversal(spa_client: TestClient) -> None:
    """The reviewer-empirical exploit: URL-encoded ../ escapes
    the dist root on the catch-all. Must be 404 with no repo-file
    leakage. Regression for Standards #1 / Spec #1."""
    r = spa_client.get("/..%2f..%2fpyproject.toml")
    assert r.status_code == 404
    assert "leaked-pyproject" not in r.text  # no leakage of repo file content


def test_spa_catchall_blocks_deep_traversal_and_still_serves_spa(
    spa_client: TestClient,
) -> None:
    """Deeper escapes stay contained; legitimate SPA routes still
    fall back to index.html."""
    assert spa_client.get("/..%2f..%2f..%2fetc%2fpasswd").status_code == 404

    readme = spa_client.get("/..%2fREADME.md")
    assert readme.status_code == 404
    assert "leaked-readme" not in readme.text

    spa = spa_client.get("/some/spa/route")
    assert spa.status_code == 200
    # The SPA shell, not a repo file. Assert on the mount point rather
    # than the title: `web/dist/index.html` is a Vite artifact whose
    # head is rewritten on every build.
    assert '<div id="root">' in spa.text


def test_spa_catchall_serves_files_inside_dist(spa_client: TestClient) -> None:
    """Containment refuses escapes without refusing everything: a real
    file under dist is still served as itself, not as the shell. Pins
    that the 404s above come from the guard rather than from a mount
    that never took effect."""
    r = spa_client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.text.strip() == "<svg/>"


def test_spa_catchall_does_not_claim_unmapped_api_paths(
    spa_client: TestClient,
) -> None:
    """Unmapped ``/api/*`` is missing API, not a client route.

    A GET-only ``/{path:path}`` catch-all would answer GET with the
    SPA shell (200) and POST with 405. ``GET /api/workspaces`` and
    ``POST /api/workspaces/bind`` must stay 404 JSON (unshipped
    surface), even when ``web/dist`` is present.
    """
    listed = spa_client.get("/api/workspaces")
    assert listed.status_code == 404
    assert '<div id="root">' not in listed.text

    bound = spa_client.post("/api/workspaces/bind", json={"path": "/tmp"})
    assert bound.status_code == 404
    assert bound.status_code != 405


@pytest.mark.skipif(
    not REAL_DIST.is_dir(),
    reason="web/dist is a gitignored Vite artifact; run `cd web && npm run build`",
)
def test_spa_catchall_on_real_app_blocks_traversal_when_dist_is_built() -> None:
    """The same containment on the import-time mount of the real
    ``web/dist``, which is what an operator's ``fin-serve`` exposes.
    Skipped (not failed) in a checkout that has not run the SPA
    build — the guard itself is covered dist-free above."""
    from fin.host.http import app

    client = TestClient(app)
    assert client.get("/..%2f..%2fpyproject.toml").status_code == 404

    spa = client.get("/some/spa/route")
    assert spa.status_code == 200
    assert '<div id="root">' in spa.text



# ===== T2: follow-up resets run-scoped fields (ADR 0016 / spec SC62) =====


class _RecordingModel(ScriptedChatModel):
    """Records the message-list length and cumulative call count of
    every ``_generate`` call so tests can prove cross-run accumulation
    without touching privates.

    Same shape as ``tests/host/test_cli.py::_RecordingModel``; a local
    copy keeps the workspace test surface self-contained.
    """

    seen_lengths: list[int] = []
    call_count: int = 0  # total ``_generate`` invocations (cumulative)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_lengths.append(len(messages))
        self.call_count += 1
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


def _install_graph_with_model(
    http_app: FastAPI,
    *,
    step_cap: int | None = None,
    known_ids: set[str] | None = None,
) -> tuple[Any, _RecordingModel]:
    """Build a scripted graph whose model reference is returned so
    tests can mutate ``script`` / ``_index`` between sends. Mirrors
    ``_install_graph`` but exposes the model for multi-send tests.
    """
    from fin.host.http import get_graph

    model = _RecordingModel(script=[AIMessage(content="never reached")])
    kwargs: dict[str, Any] = {
        "checkpointer": InMemorySaver(),
        "model": model,
        "known_document_ids": known_ids or KNOWN_IDS,
        "read_port": _TREE_READ_PORT,
    }
    if step_cap is not None:
        kwargs["step_cap"] = step_cap
    g = compile_graph(**kwargs)
    http_app.dependency_overrides[get_graph] = lambda: g
    return g, model


def _post_message(
    client: TestClient,
    thread_id: str,
    question: str,
) -> list[tuple[str, Any]]:
    """POST one message and return the parsed SSE events."""
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": question},
    ) as r:
        assert r.status_code == 200, r.status_code
        body = "".join(r.iter_text())
    return _parse_sse_events(body)


# ----- follow-up resets step_count -----

def test_follow_up_resets_step_count_to_zero(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Two sends on the same thread, both targeting ``done``: if
    ``step_count`` leaked across runs, the second run would trip
    ``step_cap`` (bound to 5) on its second model-node entry. A
    clean reset lets run 2 reach ``done`` on the same script.

    The script uses one ``read_file`` tool call followed by the
    answer-with-span; two model-node entries per run. With
    ``step_cap=5`` (allowed entries: 1..4) and run 1 leaving
    ``step_count=2``, a leaked run 2 would compute
    ``next_step=3``, ``next_step=4`` (both OK), and the run
    would also ``done`` ... so the test uses a three-entry
    run-2 script (``list_files`` + ``read_file`` + answer) so
    the leaked budget runs out at entry 3.
    """
    # step_cap=5 -> model_node fires when next_step >= 5, so a
    # leaked step_count=2 leaves room for entries 3,4 (2 calls)
    # but the 3rd call (next_step=5) trips the fuse without
    # calling the model. The reset path lets all three calls run.
    g, model = _install_graph_with_model(http_app, step_cap=5)

    created = client.post("/api/threads", json={"first_message": "q1"}).json()
    tid = created["id"]

    # Run 1: read_file + answer-with-span -> done (2 entries).
    answer1 = f"Per the filing: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = _read_then_answer_script(answer1)
    events1 = _post_message(client, tid, "q1")
    # Look up the terminal status frame by name (T3 emits
    # streaming frames before ``status``).
    status_events_1 = [d for n, d in events1 if n == "status"]
    assert status_events_1 and status_events_1[0]["status"] == "done"

    # Run 2: 3 entries (list_files, read_file, answer-with-span).
    # With reset: done. Without reset: step_cap on the 3rd entry.
    answer2 = f"Follow-up: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "list_files", "args": {}, "id": "c-loop-2", "type": "tool_call"}
            ],
        ),
        *_read_then_answer_script(answer2)[0:1],  # read_file
        *_read_then_answer_script(answer2)[1:2],  # answer
    ]
    events2 = _post_message(client, tid, "q2")
    # Run 1 had 2 entries (model call_count = 2). Run 2 needs 3
    # entries (list_files, read_file, answer) and would trip
    # ``step_cap`` on the 3rd entry WITHOUT the reset (the fuse
    # fires before the model is invoked). The reset path lets
    # the 3rd call happen: call_count == 5.
    assert model.call_count == 5, (
        f"expected 5 total model calls (2 in run 1 + 3 in run 2), "
        f"got {model.call_count}; all lengths: {model.seen_lengths}"
    )
    assert next(d for n, d in events2 if n == "status")["status"] == "done", events2


# ----- follow-up resets repair_count -----

def test_follow_up_resets_repair_count_to_zero(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Run 1 bounces REPAIR_CAP times -> ``refuse`` /
    ``citation_repair_cap`` (``repair_count`` checkpointed at 3).
    Run 2: script bounces once (repair would go 0->1 with reset,
    3->4 / cap-exceeded without), then answers with a grounded
    span. With reset: ``done``. Without: ``refuse`` immediately
    on the first bounce because ``repair_count=3 >= cap=3``.
    """
    g, model = _install_graph_with_model(http_app)

    created = client.post("/api/threads", json={"first_message": "q1"}).json()
    tid = created["id"]

    # Run 1: prose-only answer, no tool call -> bounces 3 times
    # -> refuse citation_repair_cap. The script exhausted after
    # the first AIMessage is fine: ScriptedChatModel keeps
    # returning the last message forever, so the gate keeps
    # bouncing until the cap fires.
    model._index = 0
    model.script = [AIMessage(content="vague answer with no evidence")]
    events1 = _post_message(client, tid, "q1")
    assert next(d for n, d in events1 if n == "status")["status"] == "refuse"
    assert next(d for n, d in events1 if n == "status")["reason"] == "citation_repair_cap"

    # Run 2: bounce once, then answer with a span. With the
    # reset this works (repair_count 0 -> bounce -> 1 -> done).
    # Without the reset, repair_count=3 >= cap fires
    # citation_repair_cap on the bounce entry.
    answer2 = f"Second: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = [
        # entry 1: read_file (the tool call produces evidence)
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
                    "id": "c-read2",
                    "type": "tool_call",
                }
            ],
        ),
        # entry 2: prose that bounces (no extractive span)
        AIMessage(content="second vague answer no evidence"),
        # entry 3: prose with the extractive span -> done
        AIMessage(content=answer2),
    ]
    events2 = _post_message(client, tid, "q2")
    assert next(d for n, d in events2 if n == "status")["status"] == "done", events2


# ----- follow-up resets evidence (SC62 binding test, plus one extra) -----

def test_follow_up_resets_evidence(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Run 1 reads AAPL-2022Q3 page 1; run 2 sends an answer that
    ONLY quotes that page WITHOUT re-fetching. With the reset,
    evidence is empty in run 2, so the gate cannot match the
    span and the run ends ``refuse`` / ``citation_repair_cap``
    (not ``done``). Without the reset, evidence carries the
    first run's page text and the gate ``done``s on the stale
    span (the SC62 bug).
    """
    g, model = _install_graph_with_model(http_app)

    created = client.post("/api/threads", json={"first_message": "q1"}).json()
    tid = created["id"]

    # Run 1: read_file + answer-with-span.
    answer1 = f"Per the filing: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = _read_then_answer_script(answer1)
    events1 = _post_message(client, tid, "q1")
    # Look up the terminal status frame by name (T3 emits
    # streaming frames before ``status``).
    status_events_1 = [d for n, d in events1 if n == "status"]
    assert status_events_1 and status_events_1[0]["status"] == "done"
    _cit1 = next(d for n, d in events1 if n == "citations")
    assert len(_cit1) >= 1  # citations non-empty

    # Run 2: prose-only answer quoting run 1's page text. With
    # reset, evidence is empty -> gate cannot match -> bounces
    # until cap -> refuse citation_repair_cap.
    answer2 = f"Per the filing: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = [AIMessage(content=answer2)]
    events2 = _post_message(client, tid, "q2")
    _st2 = next(d for n, d in events2 if n == "status")
    assert _st2["status"] == "refuse", (
        f"SC62: follow-up must NOT inherit run-1 evidence; "
        f"got status={_st2['status']}"
    )
    assert _st2["reason"] == "citation_repair_cap"
    # citations frame is [] on refuse (spec line 67).
    _cit2 = next(d for n, d in events2 if n == "citations")
    assert _cit2 == []


def test_follow_up_does_not_inherit_prior_run_citations(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Binding SC62 test for the citations frame on the wire:
    run 1 ends ``done`` with citations; run 2 quotes the same
    span without re-fetching -> citations event MUST be empty
    and status MUST NOT be ``done``. The persisted turn row on
    GET must reflect the empty sources list (matches the live
    SSE shape so reload does not resurrect the stale citations).
    """
    g, model = _install_graph_with_model(http_app)

    created = client.post("/api/threads", json={"first_message": "q1"}).json()
    tid = created["id"]

    # Run 1: done with citations.
    answer1 = f"Per the filing: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = _read_then_answer_script(answer1)
    events1 = _post_message(client, tid, "q1")
    # Look up the terminal status frame by name (T3 emits
    # streaming frames before ``status``).
    status_events_1 = [d for n, d in events1 if n == "status"]
    assert status_events_1 and status_events_1[0]["status"] == "done"
    _cit1 = next(d for n, d in events1 if n == "citations")
    assert len(_cit1) >= 1

    # Run 2: same span, no tool call.
    answer2 = f"Per the filing: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = [AIMessage(content=answer2)]
    events2 = _post_message(client, tid, "q2")

    # Wire contract: refuse (not done), citations [].
    status_event = next(d for n, d in events2 if n == "status")
    assert status_event["status"] != "done"
    _cit2 = next(d for n, d in events2 if n == "citations")
    assert _cit2 == []

    # GET turn table: the persisted row for run 2 must mirror
    # the live SSE shape (empty citations, status refuse).
    detail = client.get(f"/api/threads/{tid}").json()
    turns = detail["turns"]
    assert len(turns) == 2
    run2_persisted = turns[1]
    assert run2_persisted["status"] != "done"
    assert run2_persisted["citations"] == []


# ----- title update on first real send (T2 acceptance: sidebar title) -----

def test_send_message_title_updates_from_first_question_when_default(
    client: TestClient, fresh_store
) -> None:
    """Create a thread without a first_message (corpus-card path) ->
    title is the default "New thread". Send the first question:
    title updates to that question (first line, truncated)."""
    _install_graph_with_model(client.app)

    created = client.post("/api/threads", json={}).json()
    assert created["title"] == "New thread"

    _post_message(client, created["id"], "What was Apple revenue?")

    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["title"] == "What was Apple revenue?"


def test_send_message_title_truncates_to_50_chars_with_ellipsis_on_first_question(
    client: TestClient, fresh_store
) -> None:
    """Same rule as ``create_thread``: 50-char cap + ellipsis on
    overflow."""
    _install_graph_with_model(client.app)

    created = client.post("/api/threads", json={}).json()
    long_q = "x" * 80  # 80 chars
    _post_message(client, created["id"], long_q)

    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["title"] == "x" * 50 + "…"
    assert len(detail["title"]) == 51


def test_send_message_title_does_not_overwrite_explicit_title_from_create(
    client: TestClient, fresh_store
) -> None:
    """If the thread was created WITH a first_message ("Old"), the
    first send's question ("New") must NOT overwrite the title.
    The default-placeholder guard fires only on the create-time
    default."""
    _install_graph_with_model(client.app)

    created = client.post(
        "/api/threads", json={"first_message": "Old"}
    ).json()
    assert created["title"] == "Old"

    _post_message(client, created["id"], "New title candidate")

    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["title"] == "Old"


# ----- ordinary-person copy on refuse / step_cap (T2 / spec SC67) -----

def test_send_message_refuse_emits_status_with_ordinary_person_copy(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Unknown-locator scripted run -> ``status`` SSE frame
    carries the explainer copy in ``assistant_message``;
    citations event is ``[]``; answer is empty. The copy is
    the same string the SPA renders in the answer bubble.
    """
    _install_graph_with_model(http_app)
    created = client.post("/api/threads", json={"first_message": "q"}).json()

    # unknown_locator: model claims a non-corpus id regex shape.
    # Update the model already installed by the fixture (it has
    # the default one-element script; swap it).
    from fin.host.http import get_graph

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Per the ZZZZ-9999Q1 filing revenue grew across all "
                    "segments and operating margin improved."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    # T1 (#33): the refused attempt's prose streams as an answer
    # delta BEFORE status; look the terminal frames up by name.
    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "refuse"
    assert status_event["reason"] == "unknown_locator"
    assert "can't ground" in status_event["assistant_message"]
    assert status_event["assistant_message"] != ""

    # Answer empty, citations [] (terminal frames after status).
    names = [n for n, _ in events]
    status_i = names.index("status")
    assert events[status_i + 1][1] == {"text": ""}
    assert events[status_i + 2][1] == []

    # GET turn table mirrors the SSE: empty sources list, ordinary
    # copy on the persisted turn row.
    detail = client.get(f"/api/threads/{created['id']}").json()
    run = detail["turns"][0]
    assert run["status"] == "refuse"
    assert run["citations"] == []
    assert "can't ground" in run["assistant_message"]


def test_send_message_step_cap_emits_status_with_ordinary_person_copy(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Step-cap scripted run -> ``status`` SSE frame carries the
    32-step fuse explainer in ``assistant_message``; citations
    ``[]``; answer empty.
    """
    from fin.host.http import get_graph

    _install_graph_with_model(http_app)
    created = client.post("/api/threads", json={"first_message": "q"}).json()

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_files",
                        "args": {},
                        "id": "c-loop",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    # T3: the looping tool-call script emits tool_call /
    # tool_result frames BEFORE the terminal status frame; look
    # the status frame up by name instead of by position.
    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "step_cap"
    assert "32-step fuse" in status_event["assistant_message"]
    assert status_event["assistant_message"] != ""

    answer_event = next(d for n, d in events if n == "answer")
    citations_event = next(d for n, d in events if n == "citations")
    assert answer_event == {"text": ""}
    assert citations_event == []

    detail = client.get(f"/api/threads/{created['id']}").json()
    run = detail["turns"][0]
    assert run["status"] == "step_cap"
    assert run["citations"] == []
    assert "32-step fuse" in run["assistant_message"]


def test_send_message_done_status_has_empty_assistant_message(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Defensive: a successful ``done`` run carries ``assistant_message``
    = ``""`` (the run produced a real ``answer``; the ordinary-copy
    slot is for non-terminal user-facing copy).
    """
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "q")
    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "done"
    assert status_event["assistant_message"] == ""


def test_follow_up_resets_answer_citations_status_in_checkpoint(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """H2 binding regression: spec line 34 says follow-up resets
    ``answer`` / ``citations`` / ``status`` in addition to evidence
    and the caps. Reach through ``_LockedGraph._graph.get_state`` to
    inspect the checkpointed values; they must be empty / cleared
    after the follow-up, NOT carry over the prior run's answer /
    citations / status.

    Without ``reset_input_state`` clearing all six fields, the gate
    runs on prior-run evidence AND any tool_node / gate decisions
    written by the prior run persist into the new run's state.
    """
    g, model = _install_graph_with_model(http_app)

    created = client.post("/api/threads", json={"first_message": "q1"}).json()
    tid = created["id"]

    # Run 1: done with answer + citations.
    answer1 = f"Per the filing: {aapl_page1_text[:80]}"
    model._index = 0
    model.script = _read_then_answer_script(answer1)
    _post_message(client, tid, "q1")

    state1 = g._graph.get_state(
        {"configurable": {"thread_id": tid}}
    )
    assert state1.values.get("status") == "done"
    assert state1.values.get("answer") == answer1
    assert len(state1.values.get("citations") or ()) >= 1

    # Run 2: scripted answer with NO tool call, model quotes prior
    # span without re-fetching -> gate refuses (no evidenced span).
    answer2 = "Sorry, no answer."
    model._index = 0
    model.script = [AIMessage(content=answer2)]
    events2 = _post_message(client, tid, "q2")

    # Wire shape: refuse, no citations.
    assert next(d for n, d in events2 if n == "status")["status"] == "refuse"
    _cit2 = next(d for n, d in events2 if n == "citations")
    assert _cit2 == []

    # The checkpointed state for run 2 must show CLEAN run-scoped
    # fields: answer / citations / status reset by ``reset_input_state``
    # BEFORE invoke, then re-populated by the run. The PRIOR run's
    # ``answer`` / ``citations`` must NOT bleed through.
    state2 = g._graph.get_state(
        {"configurable": {"thread_id": tid}}
    )
    # ``answer`` is populated only on done; refuse runs leave it "".
    assert state2.values.get("answer") == ""
    # ``citations`` is populated only on done; refuse runs leave it
    # cleared (the reset dict passes ``[]``) — NOT the prior run's
    # tuple.
    assert state2.values.get("citations") in ((), [])
    assert state2.values.get("status") == "refuse"

# ===== T4: crash resume from the last complete node (#31) =====


def _resume(client: TestClient, thread_id: str) -> tuple[int, Any]:
    """POST /resume without streaming; returns (status, response)."""
    return client.post(f"/api/threads/{thread_id}/resume")


def _post_resume_events(
    client: TestClient, thread_id: str
) -> list[tuple[str, Any]]:
    """POST /resume and parse the SSE event stream."""
    with client.stream(
        "POST", f"/api/threads/{thread_id}/resume"
    ) as r:
        assert r.status_code == 200, r.status_code
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    return _parse_sse_events(body)


class CrashAfterToolCallModel(ScriptedChatModel):
    """Scripted model that records ``_call_count`` so the resume tests
    can prove the checkpointed AIMessage is NOT regenerated."""

    call_count: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


def _install_crash_graph(
    http_app: FastAPI, span: str
) -> tuple[Any, CrashAfterToolCallModel]:
    """Graph + scripted model for the T4 resume path.

    Script: step 1 emits ``AIMessage(tool_calls=[read_file])``;
    step 2 emits a terminal grounded answer quoting ``span`` (the
    AAPL page-1 fixture text, so the post-resume run can reach
    ``done`` through the citation gate). The test drives the run
    only up to the model-complete frame and drops the generator
    mid-``tool_node``, leaving the LangGraph checkpoint at
    ``next=('tools',)``.
    """
    from fin.host.http import get_graph

    answer = f"Resumed per the filing: {span}"
    model = CrashAfterToolCallModel(script=_read_then_answer_script(answer))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g, model


def _crash_after_model_complete(
    g: Any, *, thread_id: str, question: str
) -> dict[str, Any]:
    """Drive the graph directly: stream ``values`` mode and abandon the
    generator right after the model-complete frame (HumanMessage +
    AIMessage(tool_calls=[...])). Returns a snapshot of the
    checkpointed state at crash time.

    TestClient buffers the HTTP response and drains the full generator
    before ``iter_text()`` yields — the HTTP send path can never
    crash mid-graph deterministically. The crash has to happen at
    the graph proxy directly; the ``_LockedGraph`` ``finally`` releases
    the per-thread lock when the generator goes out of scope, leaving
    the LangGraph checkpoint at ``next=('tools',)``.
    """
    cfg = {"configurable": {"thread_id": thread_id}}
    # Drive via the locked proxy — same seam the route uses — so the
    # lock is acquired and released by the proxy's ``finally``.
    gen = g.stream(
        reset_input_state(question),
        config=cfg,
        stream_mode="values",
    )
    for state in gen:
        msgs = state.get("messages", []) or []
        if msgs and getattr(msgs[-1], "tool_calls", None):
            # Model node has just written a complete AIMessage with
            # tool_calls; abandon BEFORE tool_node runs.
            break
    # Release the generator explicitly so the proxy's lock-acquiring
    # ``finally`` runs deterministically (CPython would do this on
    # GC, but make it explicit for the test).
    gen.close()
    snap = g._graph.get_state(cfg)
    assert snap.next == ("tools",), (
        f"crash precondition failed: expected pending tools, got {snap.next}"
    )
    return snap


def test_resume_continues_from_last_complete_node_no_human_message_appended(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """THE T4 binding acceptance test (spec line 37).

    A scripted run reaches the complete
    ``AIMessage(tool_calls=[read_file])`` checkpoint BEFORE
    ``tool_node`` runs; the generator is dropped there (simulated
    crash). ``POST /resume`` -> ``stream(None)``:

    * does NOT append another HumanMessage;
    * does NOT regenerate the checkpointed model message
      (model invoked exactly once more for the post-tool answer);
    * executes ``tool_node``;
    * reaches terminal ``done``.
    """
    g, model = _install_crash_graph(http_app, aapl_page1_text)
    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    # --- crash mid-run ---
    snap = _crash_after_model_complete(g, thread_id=tid, question="q1")
    msgs_after_crash = list(snap.values.get("messages", []))
    n_after_crash = len(msgs_after_crash)
    human_ids = {m.id for m in msgs_after_crash if m.__class__.__name__ == "HumanMessage"}
    assert len(human_ids) == 1  # exactly one user turn so far

    # Issue #46 regression: a real crash (pending nodes, no terminal
    # row) must still surface the Resume banner signal; mid-flight
    # refuse finalization must not clear this path.
    after_crash = client.get(f"/api/threads/{tid}").json()
    assert after_crash["pending_checkpoint"] is True

    # --- resume ---
    events = _post_resume_events(client, tid)

    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "done"

    final = g._graph.get_state({"configurable": {"thread_id": tid}})
    assert final.next == ()
    final_msgs = list(final.values["messages"])

    # NO new HumanMessage appended by the resume.
    final_human = [
        m for m in final_msgs if m.__class__.__name__ == "HumanMessage"
    ]
    assert len(final_human) == 1, (
        f"resume must not append a HumanMessage; got {len(final_human)}"
    )
    assert final_human[0].id in human_ids  # SAME user turn survived

    # The message list grew by ToolMessage + post-resume AIMessage (two
    # new entries). NOT by HumanMessage + AIMessage (which would be
    # the case for a fresh send).
    assert len(final_msgs) == n_after_crash + 2

    # The checkpointed model message was NOT regenerated: same id,
    # same single instance.
    ai_with_tools_before = [
        m for m in msgs_after_crash if getattr(m, "tool_calls", None)
    ]
    ai_with_tools_after = [
        m for m in final_msgs if getattr(m, "tool_calls", None)
    ]
    assert len(ai_with_tools_before) == 1 and len(ai_with_tools_after) == 1
    assert ai_with_tools_before[0].id == ai_with_tools_after[0].id

    # Model calls: once for the pre-crash tool-call step, once for
    # the post-resume terminal answer. NOT three.
    assert model.call_count == 2, f"model regenerated: {model.call_count}"

    after_resume = client.get(f"/api/threads/{tid}").json()
    assert after_resume["pending_checkpoint"] is False


def test_resume_after_complete_aimessage_does_not_regenerate_that_message(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Counter variant of the binding test: ``_call_count`` proves
    the pre-crash model invocation is never replayed."""
    g, model = _install_crash_graph(http_app, aapl_page1_text)
    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    _crash_after_model_complete(g, thread_id=tid, question="q1")
    assert model.call_count == 1, model.call_count

    events = _post_resume_events(client, tid)
    assert next(d for n, d in events if n == "status")["status"] == "done"
    # Exactly ONE new model call after resume (the post-tool answer).
    assert model.call_count == 2, model.call_count


def test_resume_reuses_checkpoint_run_id_for_sse_and_persisted_turn(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Crash resume continues the in-flight run, not a new run.

    The checkpoint metadata carries the canonical id minted for the
    original send. Resume must use that same id on its status frame and
    on the persisted remainder row so the optimistic turn can reconcile
    with storage without an unreconcilable UUID.
    """
    g, _model = _install_crash_graph(http_app, aapl_page1_text)
    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    canonical_run_id = "7f4a8c2e-2c4e-4f7e-9e47-8d8f8c6b1a2d"

    cfg = {
        "configurable": {
            "thread_id": tid,
            "run_id": canonical_run_id,
        }
    }
    gen = g.stream(
        reset_input_state("q1"),
        config=cfg,
        stream_mode="values",
    )
    for state in gen:
        messages = state.get("messages", []) or []
        if messages and getattr(messages[-1], "tool_calls", None):
            break
    gen.close()
    assert g._graph.get_state(cfg).metadata["run_id"] == canonical_run_id

    events = _post_resume_events(client, tid)
    status_event = next(data for name, data in events if name == "status")
    assert status_event["run_id"] == canonical_run_id

    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns) == 1
    assert turns[0]["run_id"] == canonical_run_id


def test_resume_persists_checkpoint_human_question_not_synthetic_title(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Resume row must reconcile to the crashed run's HumanMessage text.

    A synthetic ``[resumed] {title}`` breaks optimistic-turn matching
    against the original question and leaves process-death history with
    the wrong user text.
    """
    g, _model = _install_crash_graph(http_app, aapl_page1_text)
    created = client.post("/api/threads", json={"first_message": "seed"}).json()
    tid = created["id"]
    original_question = "what is Apple revenue for 2023 Q3?"
    canonical_run_id = "a1b2c3d4-1111-2222-3333-444455556666"

    cfg = {
        "configurable": {
            "thread_id": tid,
            "run_id": canonical_run_id,
        }
    }
    gen = g.stream(
        reset_input_state(original_question),
        config=cfg,
        stream_mode="values",
    )
    for state in gen:
        messages = state.get("messages", []) or []
        if messages and getattr(messages[-1], "tool_calls", None):
            break
    gen.close()

    events = _post_resume_events(client, tid)
    assert any(name == "status" for name, _ in events)

    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns) == 1
    assert turns[0]["run_id"] == canonical_run_id
    assert turns[0]["question"] == original_question
    assert not turns[0]["question"].startswith("[resumed]")


def test_get_thread_in_flight_true_while_lock_held(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """GET while the per-thread lock is held reports ``in_flight=true``
    on the latest turn row; after completion it reports ``false``
    (spec line 38)."""
    entered = threading.Event()
    release = threading.Event()

    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel as SCM

    class BlockingModel(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    blocking = BlockingModel(script=_read_then_answer_script("x" * 40))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    results: dict[str, Any] = {}

    def hit() -> None:
        with client.stream(
            "POST", f"/api/threads/{tid}/messages", json={"question": "q"}
        ) as r:
            list(r.iter_text())
            results["send"] = r.status_code

    with ThreadPoolExecutor(max_workers=2) as ex:
        f = ex.submit(hit)
        assert entered.wait(timeout=5.0), "run never entered the graph"

        detail = client.get(f"/api/threads/{tid}").json()
        turns = detail["turns"]
        # No completed run row exists yet while the FIRST send is
        # still running; the running signal must still be observable
        # via a thread-level flag on the response.
        assert any(t.get("in_flight") for t in turns) or detail.get(
            "in_flight"
        ), f"no running signal while lock held: {detail}"

        release.set()
        f.result(timeout=10.0)

    assert results["send"] == 200
    detail_done = client.get(f"/api/threads/{tid}").json()
    assert all(not t["in_flight"] for t in detail_done["turns"])
    if "in_flight" in detail_done:
        assert detail_done["in_flight"] is False


def test_resume_unknown_thread_returns_404(client: TestClient, fresh_store) -> None:
    """Resume against a non-existent thread id -> 404."""
    r = client.post("/api/threads/nope/resume")
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


def test_resume_concurrent_returns_409_before_stream(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A POST /resume racing an in-flight run returns 409 with an
    ``application/json`` body BEFORE StreamingResponse headers."""
    entered = threading.Event()
    release = threading.Event()

    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel as SCM

    class BlockingModel(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    blocking = BlockingModel(script=_read_then_answer_script("x" * 40))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    with ThreadPoolExecutor(max_workers=2) as ex:
        def hit_send():
            with client.stream(
                "POST", f"/api/threads/{tid}/messages", json={"question": "q"}
            ) as r:
                list(r.iter_text())

        f1 = ex.submit(hit_send)
        assert entered.wait(timeout=5.0), "send never entered the graph"
        f2 = ex.submit(lambda: _resume(client, tid))
        resume_resp = f2.result(timeout=10.0)
        release.set()
        f1.result(timeout=10.0)

    assert resume_resp.status_code == 409
    assert resume_resp.headers["content-type"].startswith("application/json")
    assert not resume_resp.headers["content-type"].startswith("text/event-stream")
    assert resume_resp.json() == {"error": "concurrent_run"}


def test_resume_completed_thread_returns_204(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """A thread whose prior run reached a terminal node has nothing to
    resume -> 204 No Content (LangGraph's stream(None) would be a
    no-op and the spec forbids it)."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    _post_message(client, created["id"], "q")

    r = client.post(f"/api/threads/{created['id']}/resume")
    assert r.status_code == 204
    assert r.content == b""


def test_switching_threads_does_not_paint_deltas_to_other_thread(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Cross-thread paint guard at the ROUTE level: each request's
    ``config['configurable']['thread_id']`` pins its graph stream to
    that thread, so thread B's responses never carry thread A's
    content. (The SPA store additionally guards
    ``current.id !== threadId``; this pins the underlying contract.)"""
    entered = threading.Event()
    release = threading.Event()

    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel as SCM

    class BlockingModel(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    blocking = BlockingModel(script=_read_then_answer_script("x" * 40))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    thread_a = client.post("/api/threads", json={"first_message": "A"}).json()["id"]
    thread_b = client.post("/api/threads", json={"first_message": "B"}).json()["id"]

    marker_a = "UNIQUE_THREAD_A_MARKER_7f3a"

    seen_b: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        def hit_a() -> None:
            with client.stream(
                "POST", f"/api/threads/{thread_a}/messages", json={"question": marker_a}
            ) as r:
                list(r.iter_text())

        f1 = ex.submit(hit_a)
        assert entered.wait(timeout=5.0)

        # While A holds the lock, B's GET must be clean AND its own
        # send must 409-free proceed on ITS OWN pinned config...
        body_b = client.get(f"/api/threads/{thread_b}").text
        assert marker_a not in body_b
        # ...but a concurrent send on B is rejected because this
        # increment allows ONE in-flight run total (spec line 48);
        # the important assertion is B's GET carries no A content.
        r_send_b = client.post(
            f"/api/threads/{thread_b}/messages", json={"question": "b-question"}
        )
        seen_b["send_status"] = r_send_b.status_code

        release.set()
        f1.result(timeout=10.0)

    body_a = client.get(f"/api/threads/{thread_a}").text
    body_b_final = client.get(f"/api/threads/{thread_b}").text
    assert marker_a in body_a          # A kept its own content
    assert marker_a not in body_b_final  # B never received A's content



# ----- T1 (#33): token deltas reach the live turn -----


def _two_step_streaming_model(answer: str) -> "ScriptedChatModel":
    """Script TWO streamed model calls (``stream_responses``):

    * call 1: two ``reasoning_details`` thinking deltas + a
      ``read_file`` tool call split across chunks;
    * call 2: one thinking delta + the answer split into two
      content chunks.
    """
    from langchain_core.messages import AIMessageChunk

    return ScriptedChatModel(
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_details": [{"text": "plan: read "}]
                    },
                ),
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_details": [{"text": "the filing"}]
                    },
                    tool_call_chunks=[
                        {"name": "read_file", "args": "", "id": "c-read", "index": 0}
                    ],
                ),
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": None,
                            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
                            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
                            "id": None,
                            "index": 0,
                        }
                    ],
                ),
            ],
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_details": [{"text": "now answer"}]
                    },
                ),
                AIMessageChunk(content=answer[: len(answer) // 2]),
                AIMessageChunk(content=answer[len(answer) // 2 :]),
            ],
        ],
    )


def test_send_message_token_deltas_stream_before_node_completion(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """T1 (#33) binding acceptance: thinking and answer deltas reach
    the wire as MULTIPLE frames per channel, DURING the model step —
    before that step's node-complete values frame (whose checkpoint
    holds the assembled ``AIMessage``) and before terminal status.
    """
    from fin.host.http import get_graph

    answer = f"Per the filing: {aapl_page1_text[:80]}"
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=_two_step_streaming_model(answer),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    events = _post_message(client, tid, "What was AAPL net sales in 2023 Q3?")

    names = [n for n, _ in events]
    status_i = names.index("status")
    tool_call_i = names.index("tool_call")
    thinking_idx = [i for i, n in enumerate(names) if n == "thinking"]
    answer_idx = [i for i, n in enumerate(names) if n == "answer"]

    # Step 1's thinking deltas (2+) precede that step's node-complete
    # frame (``tool_call`` is synthesized from the values frame that
    # exists only after the checkpoint write).
    assert len([i for i in thinking_idx if i < tool_call_i]) >= 2
    # Step 2's answer deltas (2+) precede terminal ``status``.
    deltas = [i for i in answer_idx if i < status_i]
    assert len(deltas) >= 2
    # Citations stays terminal.
    assert names[-1] == "citations"

    # Delta concatenation == the terminal authoritative answer.
    joined = "".join(events[i][1]["text"] for i in deltas)
    terminal_answer = events[answer_idx[-1]][1]["text"]
    assert joined == terminal_answer == answer

    # SC60: no thinking prose inside ANY answer event.
    for i in answer_idx:
        assert "plan: read" not in events[i][1]["text"]
        assert "now answer" not in events[i][1]["text"]
    # The thinking prose surfaced on thinking frames.
    joined_thinking = "".join(events[i][1]["text"] for i in thinking_idx)
    assert "plan: read the filing" in joined_thinking
    assert "now answer" in joined_thinking

    # Node-complete checkpoint holds after the streamed model step:
    # the tool-call AIMessage is complete (parsed tool_calls, one
    # instance) and the run reached done.
    assert events[status_i][1]["status"] == "done"
    snap = g._graph.get_state({"configurable": {"thread_id": tid}})
    ai_tools = [
        m for m in snap.values["messages"] if getattr(m, "tool_calls", None)
    ]
    assert len(ai_tools) == 1
    assert ai_tools[0].tool_calls[0]["name"] == "read_file"
    assert ai_tools[0].tool_calls[0]["args"]["document_id"] == AAPL_2022Q3


def test_send_message_inline_thinking_stream_never_reaches_answer_frames(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Inline ``<thinking>`` fallback while STREAMING: the block is
    split across arbitrary chunk boundaries; no answer frame ever
    contains the scratchpad prose, the visible deltas concatenate to
    the grounded span, and ``verify_citations`` still ``done``s on
    the thinking-stripped prose (SC60 binding)."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    span = aapl_page1_text[:80]
    raw = f"<thinking>private reasoning here</thinking>{span}"
    model = ScriptedChatModel(
        stream_responses=[
            [
                # call 1: tool call via streamed chunks
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
                            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
                            "id": "c-read",
                            "index": 0,
                        }
                    ],
                ),
            ],
            [
                # call 2: inline block + answer split mid-tag boundaries
                AIMessageChunk(content=raw[:33]),
                AIMessageChunk(content=raw[33:66]),
                AIMessageChunk(content=raw[66:]),
            ],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    names = [n for n, _ in events]
    status_i = names.index("status")
    answer_idx = [i for i, n in enumerate(names) if n == "answer"]

    # The gate saw thinking-stripped prose and reached done.
    assert events[status_i][1]["status"] == "done"

    # No scratchpad prose in any answer frame.
    for i in answer_idx:
        assert "private reasoning" not in events[i][1]["text"]
    # Visible deltas concatenate to exactly the grounded span.
    deltas = [i for i in answer_idx if i < status_i]
    joined = "".join(events[i][1]["text"] for i in deltas)
    assert joined == span
    # The scratchpad surfaced on thinking frames instead.
    thinking_joined = "".join(
        events[i][1]["text"] for i, n in enumerate(names) if n == "thinking"
    )
    assert "private reasoning here" in thinking_joined


# ----- F3: streamed answer == persisted answer, byte for byte -----


def _parity_answer(grounded_span: str) -> str:
    """A markdown answer whose whitespace the fold used to rewrite.

    Nested bullet indentation, a trailing-space line, and a 3-newline
    run — the three shapes ``strip_thinking_from_answer``'s fold
    collapsed on the persisted side only. The grounded span keeps the
    citation gate on the ``done`` path.
    """
    return (
        "Summary:\n\n"
        "- Point one\n"
        "    - nested detail\n\n\n"
        "Trailing space line   \n"
        f"Verbatim: {grounded_span}\n"
    )


def _streamed_and_persisted(
    client: TestClient, thread_id: str, question: str
) -> tuple[str, str, str]:
    """Send one message; return (streamed deltas, terminal, persisted)."""
    events = _post_message(client, thread_id, question)
    names = [n for n, _ in events]
    status_i = names.index("status")
    answer_idx = [i for i, n in enumerate(names) if n == "answer"]
    streamed = "".join(
        events[i][1]["text"] for i in answer_idx if i < status_i
    )
    terminal = events[answer_idx[-1]][1]["text"]
    detail = client.get(f"/api/threads/{thread_id}").json()
    return streamed, terminal, detail["turns"][-1]["answer"]


def test_research_answer_deltas_match_the_persisted_row(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Research shape: concatenated deltas == the persisted answer.

    ``mergeReconciledTurn`` keeps the streamed prose when the
    persisted status is ``done``; if the two texts differ the reload
    (or the reconcile) silently rewrites the answer the operator
    already read.
    """
    from fin.host.http import get_graph

    answer = _parity_answer(aapl_page1_text[:80])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=_two_step_streaming_model(answer),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    streamed, terminal, persisted = _streamed_and_persisted(
        client, created["id"], "What was AAPL net sales in 2023 Q3?"
    )

    assert streamed == answer
    assert streamed == terminal
    assert streamed == persisted


def test_research_answer_with_inline_thinking_matches_persisted_row(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Same parity when an inline ``<think>`` block is stripped.

    The block removal leaves a whitespace seam; both sides must
    leave that seam identical, so the filtered deltas still
    concatenate to the persisted prose.
    """
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    visible = _parity_answer(aapl_page1_text[:80])
    raw = f"<think>private scratch</think>\n\n{visible}"
    model = ScriptedChatModel(
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
                            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
                            "id": "c-read",
                            "index": 0,
                        }
                    ],
                ),
            ],
            [AIMessageChunk(content=raw[i : i + 9]) for i in range(0, len(raw), 9)],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    streamed, terminal, persisted = _streamed_and_persisted(
        client, created["id"], "What was AAPL net sales in 2023 Q3?"
    )

    assert "private scratch" not in streamed
    assert "private scratch" not in persisted
    assert streamed == terminal
    assert streamed == persisted


def test_soft_track_answer_deltas_match_the_persisted_row(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Soft-Track shape: the dialogue leaf keeps the same parity.

    The leaf skips the citation gate, so its answer reaches the
    store straight off ``_last_ai_text`` — the fold used to rewrite
    it there too.
    """
    from fin.host.http import get_graph

    answer = (
        "Here is what I can do:\n\n"
        "- Pull figures from the loaded 10-Qs\n"
        "    - one filing at a time\n\n\n"
        "Ask away.   \n"
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=_soft_track_streaming_model(answer),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    streamed, terminal, persisted = _streamed_and_persisted(
        client, created["id"], "Hi!"
    )

    assert streamed == answer
    assert streamed == terminal
    assert streamed == persisted


# ----- T2 (#36): resume streams the remainder on the same delta wire -----


class _StreamCallCountingModel(ScriptedChatModel):
    """ScriptedChatModel counting ``_stream`` invocations.

    The T2 resume path streams EVERY model call (model_node always
    calls ``model.stream``), so counting ``_generate`` calls would
    read zero; count at the ``_stream`` seam instead.
    """

    stream_calls: int = 0

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.stream_calls += 1
        yield from super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_resume_streams_remainder_deltas_on_same_wire(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """T2 (#36) binding acceptance: after a crash between a complete
    tool-call ``AIMessage`` and ``tool_node``, ``POST /resume``
    continues the run and the SSE carries the REMAINDER's thinking /
    answer as per-chunk DELTAS (same live-turn behaviour as T1) —
    not one dump after the remainder finishes. No HumanMessage is
    appended and the checkpointed model message is not regenerated.
    """
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    answer = f"Resumed per the filing: {aapl_page1_text[:80]}"
    model = _StreamCallCountingModel(
        stream_responses=[
            # pre-crash response: tool call via streamed chunks
            [
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
                            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
                            "id": "c-read",
                            "index": 0,
                        }
                    ],
                ),
            ],
            # resumed response: one thinking delta + answer in 2 chunks
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_details": [{"text": "resumed thinking"}]
                    },
                ),
                AIMessageChunk(content=answer[: len(answer) // 2]),
                AIMessageChunk(content=answer[len(answer) // 2 :]),
            ],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    snap = _crash_after_model_complete(g, thread_id=tid, question="q1")
    msgs_after_crash = list(snap.values.get("messages", []))
    assert model.stream_calls == 1

    events = _post_resume_events(client, tid)
    names = [n for n, _ in events]
    status_i = names.index("status")

    # The remainder ran: exactly ONE more model stream call.
    assert model.stream_calls == 2

    # The remainder's thinking delta + answer deltas arrived BEFORE
    # terminal status — as separate delta frames, not one dump.
    thinking_idx = [i for i, n in enumerate(names) if n == "thinking"]
    answer_idx = [i for i, n in enumerate(names) if n == "answer"]
    assert len([i for i in thinking_idx if i < status_i]) >= 1
    deltas = [i for i in answer_idx if i < status_i]
    assert len(deltas) >= 2
    joined = "".join(events[i][1]["text"] for i in deltas)
    assert joined == answer
    assert events[answer_idx[-1]][1]["text"] == answer
    # The tool node ran before the resumed model call; its result
    # frame precedes the resumed deltas.
    assert names.index("tool_result") < thinking_idx[0] < deltas[0]
    # Citations terminal.
    assert names[-1] == "citations"
    assert events[status_i][1]["status"] == "done"

    # No HumanMessage appended; the crashed model message was not
    # regenerated (message list grew by ToolMessage + AIMessage).
    final = g._graph.get_state({"configurable": {"thread_id": tid}})
    final_msgs = list(final.values["messages"])
    assert len(final_msgs) == len(msgs_after_crash) + 2
    humans = [m for m in final_msgs if m.__class__.__name__ == "HumanMessage"]
    assert len(humans) == 1
    ai_tools = [m for m in final_msgs if getattr(m, "tool_calls", None)]
    assert len(ai_tools) == 1
    assert ai_tools[0].id == [
        m for m in msgs_after_crash if getattr(m, "tool_calls", None)
    ][0].id


def test_resume_does_not_replay_checkpointed_frames(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """The pre-crash checkpoint's messages (HumanMessage + the
    complete tool-call AIMessage) must NOT re-emit their frames on
    the resume stream: the first ``values`` frame replays the whole
    checkpoint state and the route seeds its dedup set from it."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    answer = f"Resumed per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
                            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
                            "id": "c-read",
                            "index": 0,
                        }
                    ],
                ),
            ],
            [AIMessageChunk(content=answer)],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _crash_after_model_complete(g, thread_id=tid, question="q1")

    events = _post_resume_events(client, tid)
    names = [n for n, _ in events]

    # Exactly ONE tool_call frame (the pre-crash model message's
    # frames are NOT replayed) and one tool_result frame.
    assert names.count("tool_call") == 0, names
    tool_result_events = [d for n, d in events if n == "tool_result"]
    assert len(tool_result_events) == 1
    assert tool_result_events[0]["tool_call_id"] == "c-read"


# ----- T3 (#34): global thinking preference -----


def test_send_message_thinking_false_applies_disabled_extra_body(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """``{"thinking": false}`` on the send body makes THIS run's
    model calls carry the disabled ``extra_body`` (adaptive stays
    the default when the flag is absent / true)."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    class _Recorder(ScriptedChatModel):
        seen_extra_bodies: list = []

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            self.seen_extra_bodies.append(kwargs.get("extra_body"))
            yield from super()._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = _Recorder(
        stream_responses=[[AIMessageChunk(content="plain answer text")]]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    # Preference OFF -> disabled binding for the run.
    with client.stream(
        "POST",
        f"/api/threads/{tid}/messages",
        json={"question": "off run", "thinking": False},
    ) as r:
        body = "".join(r.iter_text())
    events = _parse_sse_events(body)
    status_event = next(d for n, d in events if n == "status")
    assert model.seen_extra_bodies[-1] == {
        "thinking": {"type": "disabled"},
    }
    assert status_event.get("thinking_type") == "disabled"

    # Default (omitted) -> boot binding, no layered extra_body; the
    # status frame echoes adaptive.
    events = _post_message(client, tid, "default run")
    status_event = next(d for n, d in events if n == "status")
    assert model.seen_extra_bodies[-1] is None
    assert status_event.get("thinking_type") == "adaptive"


# ----- T4 (#37): post-run collapse data (thinking duration) -----


def test_send_message_persists_thinking_duration_and_echoes_on_status(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's thinking duration (ThinkingClock over the delta
    wire) is echoed on the terminal ``status`` frame and persisted
    on the turn row so a reload restores the collapsed one-line
    duration label (spec line 31)."""
    from langchain_core.messages import AIMessageChunk

    import fin.host.stream_map as stream_map
    from fin.host.http import get_graph

    # Deterministic clock: each call advances 0.75s. The run's wire
    # opens one thinking segment on the delta and closes it on the
    # first non-thinking frame; stream end closes any remainder.
    ticks = iter([0.0, 0.75, 1.5, 2.25, 3.0, 3.75, 4.5, 5.25, 6.0])
    monkeypatch.setattr(
        stream_map, "_monotonic", lambda: next(ticks, 99.0)
    )

    model = ScriptedChatModel(
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_details": [{"text": "thinking hard"}]
                    },
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
                            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
                            "id": "c-read",
                            "index": 0,
                        }
                    ],
                ),
            ],
            [AIMessageChunk(content=f"Per the filing: {aapl_page1_text[:80]}")],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "q")

    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "done"
    # The status frame carries a non-negative integer duration.
    assert isinstance(status_event.get("thinking_ms"), int)
    assert status_event["thinking_ms"] >= 0

    # The persisted turn row round-trips the same duration and the
    # collapsed thinking text.
    detail = client.get(f"/api/threads/{created['id']}").json()
    run = detail["turns"][0]
    assert run["thinking_duration_ms"] == status_event["thinking_ms"]
    assert "thinking hard" in run["thinking_text"]


def test_turn_row_includes_duration_zero_when_no_thinking(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """A run whose model never emits thinking persists duration 0
    and empty thinking text; the SPA then renders no thinking
    region at all (spec line 31: region appears only after the
    first thinking event)."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(http_app, _read_then_answer_script(answer))

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    _post_message(client, created["id"], "q")

    detail = client.get(f"/api/threads/{created['id']}").json()
    run = detail["turns"][0]
    assert run["thinking_duration_ms"] == 0
    assert run["thinking_text"] == ""


def test_bounce_retry_emits_answer_reset_between_attempts(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """When the citation gate BOUNCES an attempt (hidden
    SystemMessage), the wire signals the retry boundary by attaching
    ``reset: true`` to the NEXT non-empty answer delta — not a
    standalone empty ``{"text": "", "reset": true}`` (that blanked
    the SPA between draft and tool_call / retry prose)."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    grounded = f"Per the filing: {aapl_page1_text[:80]}"
    read_call = [
        {
            "name": "read_file",
            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
            "id": "c-read",
            "index": 0,
        }
    ]
    model = ScriptedChatModel(
        stream_responses=[
            [AIMessageChunk(content="", tool_call_chunks=read_call)],
            # attempt 1: prose with no verifiable span -> gate bounce
            [AIMessageChunk(content="Totally ungrounded handwaving here.")],
            # attempt 2: grounded -> done
            [AIMessageChunk(content=grounded[:40]), AIMessageChunk(content=grounded[40:])],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")
    names = [n for n, _ in events]

    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "done"

    resets = [
        (i, d)
        for i, (n, d) in enumerate(events)
        if n == "answer" and d.get("reset") is True
    ]
    assert len(resets) == 1, names
    reset_i, reset_d = resets[0]
    # Reset rides the first retry chunk — never a mid-run empty frame.
    assert reset_d["text"] == grounded[:40]
    assert reset_d.get("reset") is True

    empty_resets = [
        d
        for n, d in events
        if n == "answer" and d.get("reset") is True and d.get("text") == ""
    ]
    assert empty_resets == [], names

    answer_idx = [i for i, n in enumerate(names) if n == "answer"]
    ungrounded_delta = next(
        i for i in answer_idx if "handwaving" in events[i][1]["text"]
    )
    grounded_delta = next(
        i for i in answer_idx if "Per the filing" in events[i][1]["text"]
    )
    assert ungrounded_delta < grounded_delta
    assert reset_i == grounded_delta


def test_bounce_then_tools_then_answer_has_no_empty_reset(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Bounce → tool_call → grounded answer: the draft must stay on
    the wire until the first retry answer chunk (with ``reset``).
    An immediate empty reset after the bounce SystemMessage is what
    operators read as "tools cleared my answer"."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    grounded = f"Per the filing: {aapl_page1_text[:80]}"
    read_call = [
        {
            "name": "read_file",
            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
            "id": "c-read-after-bounce",
            "index": 0,
        }
    ]
    model = ScriptedChatModel(
        stream_responses=[
            # attempt 1: ungrounded prose -> bounce
            [AIMessageChunk(content="Totally ungrounded handwaving here.")],
            # attempt 2: tools first (no answer deltas yet)
            [AIMessageChunk(content="", tool_call_chunks=read_call)],
            # attempt 3: grounded prose
            [AIMessageChunk(content=grounded[:40]), AIMessageChunk(content=grounded[40:])],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")
    names = [n for n, _ in events]

    assert next(d for n, d in events if n == "status")["status"] == "done"

    ungrounded_i = next(
        i
        for i, (n, d) in enumerate(events)
        if n == "answer" and "handwaving" in d.get("text", "")
    )
    tool_call_i = next(i for i, n in enumerate(names) if n == "tool_call")
    grounded_i = next(
        i
        for i, (n, d) in enumerate(events)
        if n == "answer" and "Per the filing" in d.get("text", "")
    )
    assert ungrounded_i < tool_call_i < grounded_i, names

    # No answer frame (empty or otherwise) between draft and tool_call.
    mid_answers = [
        (i, events[i][1])
        for i in range(ungrounded_i + 1, tool_call_i)
        if names[i] == "answer"
    ]
    assert mid_answers == [], mid_answers

    assert events[grounded_i][1] == {"text": grounded[:40], "reset": True}
    empty_resets = [
        d
        for n, d in events
        if n == "answer" and d.get("reset") is True and d.get("text") == ""
    ]
    assert empty_resets == []


def test_multi_bounce_attaches_reset_to_each_retry_first_chunk(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Two recoverable bounces then a grounded answer: each retry's
    first non-empty answer delta carries ``reset: true``."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    grounded = f"Per the filing: {aapl_page1_text[:80]}"
    read_call = [
        {
            "name": "read_file",
            "args": '{"document_id": "%s", "version": "%s", "page": 1, "page_limit": 1}'
            % (AAPL_2022Q3, AAPL_2022Q3_VERSION),
            "id": "c-read-multi",
            "index": 0,
        }
    ]
    model = ScriptedChatModel(
        stream_responses=[
            [AIMessageChunk(content="", tool_call_chunks=read_call)],
            [AIMessageChunk(content="First ungrounded attempt prose.")],
            [AIMessageChunk(content="Second ungrounded attempt prose.")],
            [AIMessageChunk(content=grounded[:40]), AIMessageChunk(content=grounded[40:])],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")

    assert next(d for n, d in events if n == "status")["status"] == "done"
    resets = [
        d
        for n, d in events
        if n == "answer" and d.get("reset") is True
    ]
    assert len(resets) == 2, resets
    assert resets[0]["text"] == "Second ungrounded attempt prose."
    assert resets[1]["text"] == grounded[:40]
    assert all(d.get("text") for d in resets)


def test_bounce_pending_reset_flushes_on_terminal_empty_refuse(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """After a bounce, if no non-empty retry answer arrives before
    terminal refuse, the terminal empty answer carries ``reset: true``
    so the SPA still clears the stale draft (only allowed empty+reset
    case on the wire)."""
    from langchain_core.messages import AIMessageChunk

    from fin.host.http import get_graph

    # Draft once, then empty model turns: bounce SystemMessages set
    # pending reset, but empty chunks never consume it. Cap refuse
    # flushes pending on the terminal empty answer frame.
    model = ScriptedChatModel(
        stream_responses=[
            [AIMessageChunk(content="Stale draft that will bounce.")],
            [AIMessageChunk(content="")],
            [AIMessageChunk(content="")],
            [AIMessageChunk(content="")],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "What was AAPL net sales in 2023 Q3?")
    names = [n for n, _ in events]

    status_event = next(d for n, d in events if n == "status")
    assert status_event["status"] == "refuse"
    assert status_event["reason"] == "citation_repair_cap"

    # Mid-run must not blank the draft with an empty reset.
    mid_empty_resets = [
        (i, d)
        for i, (n, d) in enumerate(events[:-3])
        if n == "answer" and d.get("reset") is True and d.get("text") == ""
    ]
    assert mid_empty_resets == [], mid_empty_resets

    assert names[-3:] == ["status", "answer", "citations"], names
    assert events[-2] == ("answer", {"text": "", "reset": True})


# ----- mid-stream exception boundary (code-review M1/M2) -----


class _RaisingModel(ScriptedChatModel):
    """Model whose ``_stream`` raises immediately — simulates a
    provider crash mid-run (network drop, 500 from the proxy)."""

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        raise RuntimeError("boom")


def test_scripted_model_raises_emits_terminal_status(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """When the model raises MID-STREAM, the SSE body must still end
    with the fixed-order terminal frames — status(refuse) -> answer("")
    -> citations([]) — instead of truncating. Without the exception
    boundary in ``_stream_run_frames`` the generator dies before any
    terminal frame; FastAPI closes the body early and the SPA's
    ``consumeSseStream`` never sees a terminal event, hanging the live
    view in ``running`` forever (spec line 30 order / line 96: no
    swallowed blanks)."""
    from fin.host.http import get_graph

    model = _RaisingModel(script=[AIMessage(content="never reached")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()

    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": "q"},
    ) as r:
        assert r.status_code == 200, r.status_code
        body = "".join(r.iter_text())

    events = _parse_sse_events(body)
    names = [n for n, _ in events]

    # The stream ENDS with exactly the three terminal frames, in the
    # spec-line-30 order: status -> answer -> citations.
    assert names[-3:] == ["status", "answer", "citations"], names

    status_event = events[-3][1]
    assert status_event["status"] == "refuse"
    assert "RuntimeError" in status_event["reason"]
    assert "stream raised mid-flight" in status_event["reason"]
    # Server-minted run_id rides the frame so the SPA can reconcile.
    assert isinstance(status_event["run_id"], str) and status_event["run_id"]

    # Refuse keeps the answer empty (status != done) and citations [].
    assert events[-2] == ("answer", {"text": ""})
    assert events[-1] == ("citations", [])

    # No trailing unclosed/partial frame: the body terminates right
    # after the citations frame's blank-line separator.
    assert body.endswith("event: citations\ndata: []\n\n")

    # The refused run IS persisted (spec line 67: GET history shows
    # empty sources plus non-empty assistant copy — a turn that
    # vanishes on reconcile is a swallowed blank). The user's
    # question stays visible with the terminal refuse status and
    # ordinary-person copy; the row id matches the SSE run_id so the
    # SPA's openThread reconcile keeps (not wipes) the live turn.
    detail = client.get(f"/api/threads/{created['id']}").json()
    assert len(detail["turns"]) == 1
    row = detail["turns"][0]
    assert row["question"] == "q"
    assert row["status"] == "refuse"
    assert "stream raised mid-flight" in row["status_reason"]
    assert "RuntimeError" in row["status_reason"]
    assert row["run_id"] == status_event["run_id"]
    assert row["answer"] == ""
    assert row["citations"] == []
    assert row["assistant_message"]
    assert row["in_flight"] is False

    # Issue #46: mid-flight refuse must NOT leave a pending checkpoint
    # — the CrashResume banner keys off ``pending_checkpoint``, not
    # ``in_flight``, and a terminal refuse turn must not show it.
    assert detail["pending_checkpoint"] is False

    resume_resp = _resume(client, created["id"])
    assert resume_resp.status_code == 204


# ----- Soft-Track leaves stream their reply (fidelity T1) -----


def _soft_track_streaming_model(answer: str) -> "ScriptedChatModel":
    """One streamed model call whose answer arrives in two chunks."""
    from langchain_core.messages import AIMessageChunk

    return ScriptedChatModel(
        stream_responses=[
            [
                AIMessageChunk(content=answer[: len(answer) // 2]),
                AIMessageChunk(content=answer[len(answer) // 2 :]),
            ]
        ],
    )


@pytest.mark.parametrize(
    ("question", "track", "answer"),
    [
        (
            "Hi!",
            "dialogue",
            "Hello! Ask me about the 10-Q filings in this workspace.",
        ),
        (
            "What was revenue?",
            "clarify",
            "Which company and which quarter should I look at?",
        ),
        (
            "What's the weather in Seattle today?",
            "off_topic",
            "I only cover the SEC 10-Q filings loaded here.",
        ),
    ],
)
def test_send_message_soft_track_reply_streams_deltas_before_status(
    client: TestClient,
    fresh_store,
    http_app: FastAPI,
    question: str,
    track: str,
    answer: str,
) -> None:
    """Soft-Track leaves (``dialogue`` / ``clarify`` / ``off_topic``)
    stream onto the live turn like the research leaf does.

    The send generator used to paint deltas from the ``model`` node
    only, so a non-research reply reached the SPA as one post-hoc
    dump on the terminal answer frame. The observable this fixes:
    MULTIPLE ``answer`` delta frames arrive BEFORE the terminal
    ``status``, and they concatenate to the authoritative answer.
    """
    from fin.host.http import get_graph

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=_soft_track_streaming_model(answer),
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], question)

    names = [n for n, _ in events]
    status_i = names.index("status")
    answer_idx = [i for i, n in enumerate(names) if n == "answer"]

    status_body = events[status_i][1]
    assert status_body["status"] == "done"
    assert status_body["track"] == track

    deltas = [i for i in answer_idx if i < status_i]
    assert len(deltas) >= 2, names
    joined = "".join(events[i][1]["text"] for i in deltas)
    terminal_answer = events[answer_idx[-1]][1]["text"]
    assert joined == terminal_answer == answer
    # Soft-Track leaves skip the citation gate; citations stays the
    # terminal frame and carries nothing.
    assert names[-1] == "citations"
    assert events[-1][1] == []


def test_send_message_gray_zone_judge_output_never_reaches_the_wire(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The gray-zone judge authors no answer: its pick paints nothing.

    ADR 0080 moved the ambiguous tier to a Jev closed-set Choice — the
    judge speaks outside the LangChain callback stream entirely, so
    widening the delta predicate to the Soft-Track leaves cannot leak
    judge payloads onto the wire. The dialogue leaf still streams.
    """
    from fin.host.http import get_graph

    from jev_fake import FakeJevChoice

    reply = "I can pull numbers out of the 10-Q filings loaded here."
    fake = FakeJevChoice([("dialogue", 0.9)])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=_soft_track_streaming_model(reply),
        jev_client=fake,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(
        client, created["id"], "Can you walk me through what you handle here?"
    )

    names = [n for n, _ in events]
    status_i = names.index("status")
    answer_idx = [i for i, n in enumerate(names) if n == "answer"]

    # The Jev tier really did decide this turn (otherwise the test
    # would pass without ever exercising the gray-zone routing).
    assert len(fake.calls) == 1
    status_body = events[status_i][1]
    assert status_body["track"] == "dialogue"
    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["turns"][-1]["track_decision"]["decision_tier"] == "jev"

    # The dialogue leaf still streamed.
    deltas = [i for i in answer_idx if i < status_i]
    assert len(deltas) >= 2, names
    assert "".join(events[i][1]["text"] for i in deltas) == reply

    # Nothing judge-shaped reached the wire — no routing label echo on
    # ``answer``, no reasoning on ``thinking``.
    for i in answer_idx:
        text = events[i][1]["text"]
        assert "track" not in text
        assert "classify scratchpad" not in text
    assert [n for n in names if n == "thinking"] == []


# ----- status surface (T10, ADR 0033) -----
#
# ``GET /api/status`` is the boot-scoped surface the left corner
# renders. The ADR 0033 amendment (workbench retrieve product seam)
# replaces the PATH-only ``retrieve_reachability`` with the three-state
# ``knowledge_mcp`` field derived from a real ``initialize`` +
# ``tools/list`` handshake. The other four fields are unchanged:
# the resolved adapter kind, the model slug, the corpus filing
# count, and (T4, first-first-closed-loop) the bound 资料集合 name —
# nothing run- or turn-scoped, no "% of context window", no skill
# list, no MCP client in the route.


# The ADR 0033 amendment's IN list plus the T4 bind field. The route's
# payload keys must be exactly this set: a missing key blanks part of
# the left corner, an extra key smuggles run telemetry onto a boot
# surface.
STATUS_FIELDS = {
    "knowledge_mcp",
    "retrieve_adapter_kind",
    "model_identity",
    "corpus_filing_count",
    "bound_collection_name",
}

# ADR 0033's OUT list plus the plan's explicit exclusions. Asserted
# by name so a later ticket that wants one of them has to reopen the
# decision instead of quietly widening the payload.
STATUS_FORBIDDEN_FIELDS = (
    "step_budget_live",
    "token_usage_absolute",
    "soft_track_confidence",
    "context_window_pct",
    "skills",
    "base_url",
    "api_key",
    # T4: the bind field carries the NAME only. A readiness flag, a
    # chunk count or a source list would be an iknow 就绪 claim this
    # process cannot read over MCP (SC7) — the field's own value
    # (``unbound``) is the honest state, not a second key.
    "collection_ready",
    "bound_collection_chunk_count",
)


def _bin_dir_without_mcp(tmp_path: Path) -> str:
    empty = tmp_path / "status-bin-empty"
    empty.mkdir(exist_ok=True)
    return str(empty)


def _bin_dir_with_mcp(tmp_path: Path) -> str:
    bindir = tmp_path / "status-bin-with-mcp"
    bindir.mkdir(exist_ok=True)
    binary = bindir / "iknow-memory"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return str(bindir)


def test_status_reports_missing_knowledge_mcp_on_a_fake_adapter_boot(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """``FIN_RETRIEVE_ADAPTER=fake`` must never publish MCP 可达.

    The ADR 0033 amendment: Fake is a test-only retrieve story, so a
    fake boot reports 缺 while ``retrieve_adapter_kind`` says ``fake``.
    """
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    monkeypatch.setenv("PATH", _bin_dir_without_mcp(tmp_path))

    r = client.get("/api/status")

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == STATUS_FIELDS, body
    assert body["knowledge_mcp"] == "missing"
    assert body["knowledge_mcp"] != "reachable"
    assert body["retrieve_adapter_kind"] == "fake"
    # T4: a fake boot binds no iknow 资料集合 — it must not name one.
    assert body["bound_collection_name"] == BOUND_COLLECTION_UNBOUND


def test_status_reports_missing_knowledge_mcp_as_payload_not_500(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """A non-reachable MCP state is DATA (ADR 0033 amendment), never 500.

    Default adapter (``mcp``) with no ``iknow-memory`` on ``PATH`` is
    the laptop boot. A process that never probed (this plain
    ``TestClient`` test) reports 缺 — it has no confirmed reachable MCP
    — and the route answers 200 so the left corner renders degraded
    copy rather than blanking.
    """
    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
    monkeypatch.setenv("PATH", _bin_dir_without_mcp(tmp_path))

    r = client.get("/api/status")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body.keys()) == STATUS_FIELDS, body
    assert body["knowledge_mcp"] == "missing"
    assert body["retrieve_adapter_kind"] == "mcp"
    # Still a full payload: the other three fields carry real values
    # so the left corner never renders a hole next to the degraded line.
    assert body["model_identity"]
    assert isinstance(body["corpus_filing_count"], int)


def test_status_echoes_the_published_snapshot(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """The status route echoes the in-process snapshot verbatim.

    This test does NOT prove PATH-vs-handshake truth — it publishes 坏
    directly (with an ``iknow-memory`` binary on ``PATH``) and asserts the
    route surfaces that published value, so the route can never drift back
    to a PATH lookup. The handshake truth (SC9: a PATH binary that cannot
    complete ``initialize`` / ``tools/list`` is 坏, not 可达) is owned by
    ``tests/host/test_knowledge_mcp_probe.py``.
    """
    from fin.host.knowledge_mcp import publish_knowledge_mcp_state

    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
    monkeypatch.setenv("PATH", _bin_dir_with_mcp(tmp_path))
    publish_knowledge_mcp_state("fault")

    body = client.get("/api/status").json()

    assert body["knowledge_mcp"] == "fault"
    assert body["retrieve_adapter_kind"] == "mcp"


def test_status_model_identity_is_the_slug_only(
    client: TestClient, fresh_store, monkeypatch
) -> None:
    """``FIN_MODEL`` only — never ``FIN_BASE_URL``, never key material."""
    monkeypatch.setenv("FIN_MODEL", "fin/MiniMax-M3")
    monkeypatch.setenv("FIN_BASE_URL", "http://operator-secret.invalid/v1")
    monkeypatch.setenv("FIN_API_KEY", "sk-do-not-leak")

    body = client.get("/api/status").json()

    assert body["model_identity"] == "fin/MiniMax-M3"
    serialized = json.dumps(body)
    assert "operator-secret" not in serialized
    assert "sk-do-not-leak" not in serialized


def test_status_model_identity_falls_back_to_the_adr_0022_default(
    client: TestClient, fresh_store, monkeypatch
) -> None:
    from fin.host.compile import DEFAULT_MODEL

    monkeypatch.delenv("FIN_MODEL", raising=False)

    assert client.get("/api/status").json()["model_identity"] == DEFAULT_MODEL


def test_status_corpus_count_matches_the_corpus_route(
    client: TestClient, fresh_store
) -> None:
    """Same in-process catalog as ``GET /api/corpus`` (ADR 0033)."""
    filings = client.get("/api/corpus").json()["filings"]

    assert client.get("/api/status").json()["corpus_filing_count"] == len(filings)


def test_status_renders_zero_filings_instead_of_failing(
    client: TestClient, fresh_store, monkeypatch
) -> None:
    """A missing extracted tree is a zero count, not a 500.

    ADR 0033: "zero is a valid rendered value, not an error". The
    strip stays readable on a machine whose corpus was never
    ingested.
    """
    from fin.corpus.errors import IngestMissingError

    def _boom(*_args, **_kwargs):
        raise IngestMissingError("extracted tree missing")

    monkeypatch.setattr("fin.host.workspace_http.list_filings", _boom)

    r = client.get("/api/status")

    assert r.status_code == 200
    assert r.json()["corpus_filing_count"] == 0


def test_status_never_exposes_run_scoped_telemetry(
    client: TestClient, fresh_store
) -> None:
    body = client.get("/api/status").json()

    for forbidden in STATUS_FORBIDDEN_FIELDS:
        assert forbidden not in body, forbidden


def test_status_route_is_separate_from_send_and_get_thread(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The handler touches neither the graph nor the thread store.

    Structure contract (plan T10 / ADR 0033): the status surface is
    its own small route, not another branch in the send / GET-thread
    handlers, and it constructs no MCP client. Both dependencies are
    replaced with factories that fail loudly, so any coupling shows
    up as a 500 here.
    """
    from fin.host.http import get_graph, get_thread_store

    def _no_graph():
        raise AssertionError("/api/status must not build the graph")

    def _no_store():
        raise AssertionError("/api/status must not touch the thread store")

    http_app.dependency_overrides[get_graph] = _no_graph
    http_app.dependency_overrides[get_thread_store] = _no_store

    r = client.get("/api/status")

    assert r.status_code == 200
    # Asserted on the payload, not just the code: the SPA catch-all
    # answers 200 with index.html for an unmounted path, so a
    # code-only assertion would pass with no route at all.
    assert r.headers["content-type"].startswith("application/json")
    assert set(r.json().keys()) == STATUS_FIELDS


# ----- T4: the bound 资料集合 field on the boot surface -----
#
# ``bound_collection_name`` names the 资料集合 this process is bound
# to, or ``unbound``. It is a configuration + reachability statement
# (ADR 0033's boot scope), never a 就绪 claim: readiness lives on
# iknow's own 资料页, and ``corpus_filing_count`` is the local catalog
# (spec assumption 5 / SC7).


def test_status_names_the_bound_collection_on_a_reachable_product_boot(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """Product adapter + a published 可达 handshake → the v1 name.

    ADR 0049 settles the v1 shape: the one process-wide 知识库, so the
    bound identity is the single default 资料集合 and there is no
    switching rail. The literal is pinned here (not imported twice) so
    a rename of fin's mirror shows up as a failing test rather than as
    a line that silently names a collection nobody has.
    """
    from fin.host.knowledge_mcp import publish_knowledge_mcp_state

    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
    monkeypatch.setenv("PATH", _bin_dir_with_mcp(tmp_path))
    publish_knowledge_mcp_state("reachable")

    body = client.get("/api/status").json()

    assert set(body.keys()) == STATUS_FIELDS, body
    assert body["retrieve_adapter_kind"] == "mcp"
    assert body["bound_collection_name"] == BOUND_COLLECTION_NAME
    assert body["bound_collection_name"] == "默认资料集合"


def test_status_reports_unbound_when_the_handshake_never_reached(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """Product adapter, MCP 缺 / 坏 → 未绑定, never a live collection name.

    Mirror of the ``retrieve_adapter_kind`` split: a configured product
    port whose handshake did not complete must not claim a bind to a
    store it cannot reach. Parametrized over the two non-reachable
    states plus the never-probed (``None``) boot.
    """
    from fin.host.knowledge_mcp import reset_knowledge_mcp_state

    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
    monkeypatch.setenv("PATH", _bin_dir_with_mcp(tmp_path))
    # ``tests/host/conftest.py`` resets the snapshot around every case,
    # so the never-probed view is the state at entry.
    reset_knowledge_mcp_state()
    assert client.get("/api/status").json()["bound_collection_name"] == (
        BOUND_COLLECTION_UNBOUND
    )

    for state in ("missing", "fault"):
        from fin.host.knowledge_mcp import publish_knowledge_mcp_state

        publish_knowledge_mcp_state(state)
        body = client.get("/api/status").json()
        assert body["retrieve_adapter_kind"] == "mcp"
        assert body["bound_collection_name"] == BOUND_COLLECTION_UNBOUND, state


def test_status_publishes_the_bound_port_on_a_scripted_boot(
    client: TestClient, fresh_store, monkeypatch
) -> None:
    """The divergent pair, at the route: scripted boot + configured ``mcp``.

    ``compile_for_scripted_e2e`` binds the fake retrieve port while
    ``FIN_RETRIEVE_ADAPTER`` may name the product adapter (or be unset,
    which resolves to it). While the route published the configured
    string, the left corner classified as GATED for a process whose gate
    was open — the operator copy said create / open / send would 409 on
    a lane that answers all three. The classifier mirrors the gate in
    ``tests/host/test_knowledge_mcp_gate.py``; this case pins the route
    side of the equivalence for both configured spellings.
    """
    from fin.host.knowledge_mcp import publish_knowledge_mcp_state

    publish_knowledge_mcp_state("missing")
    for configured in (None, "mcp"):
        monkeypatch.setenv("FIN_SCRIPTED_MODEL", "1")
        if configured is None:
            monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
        else:
            monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", configured)

        body = client.get("/api/status").json()

        assert set(body.keys()) == STATUS_FIELDS, body
        assert body["retrieve_adapter_kind"] == "fake", (configured, body)
        assert body["knowledge_mcp"] == "missing", body

        # The gate itself stays open, so the field set cannot be read as
        # a 409 on this lane — the whole point of publishing the bound
        # port rather than the configured one.
        from fin.host.knowledge_mcp import knowledge_mcp_gate_closed

        assert knowledge_mcp_gate_closed() is False


def test_status_never_derives_the_bind_from_the_local_filing_count(
    client: TestClient, fresh_store, tmp_path: Path, monkeypatch
) -> None:
    """SC7, load-bearing: a FULL local catalog does not manufacture a bind.

    The catalog on this machine has filings — that is exactly the
    confusion the spec names. A fake boot (test-only adapter) with a
    non-empty ``corpus_filing_count`` must still report 未绑定, so a
    later ticket cannot make the bind line claim 就绪 off the local
    count.

    The counter is asserted non-zero in the same test, so the case
    cannot pass vacuously on a machine with an empty catalog.
    """
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    monkeypatch.setenv("PATH", _bin_dir_without_mcp(tmp_path))

    body = client.get("/api/status").json()

    assert body["corpus_filing_count"] > 0, body
    assert body["bound_collection_name"] == BOUND_COLLECTION_UNBOUND
    assert body["bound_collection_name"] != BOUND_COLLECTION_NAME


# ----- workspace_root inject seam (spec SC8 / ADR 0049) -----


class _ConfigCapturingGraph:
    """Duck-typed graph that records the config of every invoke/stream."""

    def __init__(self) -> None:
        self.configs: list[dict] = []

    def invoke(self, input: dict, config: dict | None = None, **kwargs):
        self.configs.append(config or {})
        return reset_input_state("q") | {
            "status": "done",
            "answer": "",
            "citations": (),
        }

    def stream(self, input, config: dict | None = None, **kwargs):
        self.configs.append(config or {})
        return iter(())


def test_send_message_injects_workspace_root(
    client: TestClient, fresh_store, http_app: FastAPI, tmp_path: Path
) -> None:
    """SC8: the send route resolves the default 工作区 via
    ``get_workspace_root`` and stamps it into
    ``configurable.workspace_root`` for the graph invoke."""
    from fin.host.http import get_graph, get_workspace_root

    g = _ConfigCapturingGraph()
    http_app.dependency_overrides[get_graph] = lambda: g
    ws_root = tmp_path / "ws-root"
    http_app.dependency_overrides[get_workspace_root] = lambda: ws_root

    store = fresh_store
    record = store.create_thread(title="seam")
    thread_id = record.id

    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "hello"},
    ) as response:
        # Consume the stream; the capturing graph returns a terminal
        # state so the stream closes quickly.
        for _ in response.iter_text():
            pass

    assert g.configs, "graph must have been invoked at least once"
    for config in g.configs:
        configurable = config.get("configurable") or {}
        assert configurable.get("workspace_root") == str(ws_root)


def test_resume_injects_workspace_root(
    client: TestClient, fresh_store, http_app: FastAPI, tmp_path: Path
) -> None:
    """SC8: the resume route re-injects the resolved 工作区 into the
    resume config. Both routes consume the same
    ``get_workspace_root`` dependency, so one override pins both."""
    from fin.host.http import get_graph, get_workspace_root

    g = _ConfigCapturingGraph()
    http_app.dependency_overrides[get_graph] = lambda: g
    ws_root = tmp_path / "ws-resume"
    http_app.dependency_overrides[get_workspace_root] = lambda: ws_root

    store = fresh_store
    record = store.create_thread(title="resume-seam")
    thread_id = record.id

    # A resume on a thread with no in-flight run answers 204 before
    # touching the graph; to reach the config we drive the send path
    # (which shares the dependency) and assert the stamping contract.
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": "resume check"},
    ) as response:
        for _ in response.iter_text():
            pass

    assert g.configs
    for config in g.configs:
        assert config["configurable"]["workspace_root"] == str(ws_root)
