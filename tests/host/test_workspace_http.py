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
* corpus list: ``GET /api/corpus`` returns the real 12 filings.

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
        json={"question": "q"},
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
    # Wire order per spec line 30: status -> answer -> citations,
    # with citations TERMINAL so the client can close its reader.
    status_index = event_types.index("status")
    citations_index = event_types.index("citations")
    answer_index = event_types.index("answer")
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
        json={"question": "q"},
    ) as r:
        body = "".join(r.iter_text())

    events = _parse_sse_events(body)
    event_types = [name for name, _ in events]
    # Spec line 30 order: status -> answer (empty) -> citations ([]).
    assert event_types == ["status", "answer", "citations"]
    assert events[0][1]["status"] == "refuse"
    assert events[1][1] == {"text": ""}
    assert events[2][1] == []


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
        json={"question": "q"},
    ) as r:
        body = "".join(r.iter_text())

    events = _parse_sse_events(body)
    event_types = [name for name, _ in events]
    # Spec line 30 order: status -> answer (empty) -> citations ([]).
    assert event_types == ["status", "answer", "citations"]
    assert events[0][1]["status"] == "step_cap"
    assert events[1][1] == {"text": ""}
    assert events[2][1] == []


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


def test_get_corpus_returns_twelve_filings_with_required_keys(
    client: TestClient, fresh_store
) -> None:
    """``GET /api/corpus`` returns the frozen 12 filings with the
    ADR 0009 shape."""
    r = client.get("/api/corpus")
    assert r.status_code == 200
    body = r.json()
    assert "filings" in body
    filings = body["filings"]
    assert len(filings) == 12

    for f in filings:
        assert set(f.keys()) == {"document_id", "company", "period", "version", "page_count"}
        assert isinstance(f["page_count"], int)
        assert f["page_count"] >= 1

    ids = sorted(f["document_id"] for f in filings)
    expected_substrings = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
    assert expected_substrings.issubset(set(ids))
    # Defensive: never return prototype-style ids.
    assert not any("Q3-2024" in fid for fid in ids)


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
        json={"question": "what was revenue?"},
    ) as r:
        list(r.iter_text())

    thread = client.get(f"/api/threads/{created['id']}").json()
    turns = thread["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["status"] == "done"
    assert turn["answer"] == answer
    assert len(turn["citations"]) >= 1
    assert turn["question"] == "what was revenue?"
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


def test_spa_catchall_blocks_path_traversal():
    """The reviewer-empirical exploit: URL-encoded ../ escapes
    web/dist on the catch-all. Must be 404 with no repo-file
    leakage. Regression for Standards #1 / Spec #1."""
    from fin.host.http import app as http_app

    client = TestClient(http_app)
    r = client.get("/..%2f..%2fpyproject.toml")
    assert r.status_code == 404
    assert "pyproject" not in r.text  # no leakage of repo file content


def test_spa_catchall_blocks_deep_traversal_and_still_serves_spa():
    """Deeper escapes stay contained; legitimate SPA routes still
    fall back to index.html."""
    from fin.host.http import app as http_app

    client = TestClient(http_app)
    assert client.get("/..%2f..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/..%2fREADME.md").status_code == 404

    spa = client.get("/some/spa/route")
    assert spa.status_code == 200
    assert "fin workspace" in spa.text
