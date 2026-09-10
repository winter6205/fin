"""Thread attach: this-turn read-only, never a push (corpus-serve-ingest T4).

Covers spec Focus 3 / SC4, SC8 (no research-graph 切分) and ADR 0047's
**thread attach** door: a file dragged into the open research thread is
readable by the model on THAT turn only and never enters the corpus
push/ingest path. The corpus-ingest door is a separate control (T3);
an attach failure must be named and must never look like ingest
success (error-handling ACR line).

Boundary classes exercised (defensive-contract-validator):

* empty      — empty upload body -> named 400 ``empty_attachment``.
* negative   — non-UTF-8 bytes -> named 400 ``attachment_not_text``
               (attach is text-only BY CONTRACT: binary formats belong
               to the ingest door, where 切分 runs — ADR 0047);
               unknown attachment id on send -> named 400.
* overflow   — oversize upload -> named 413 ``attachment_too_large``.
* concurrent — 409 concurrent_run is unchanged; a 409'd send does NOT
               consume the referenced attachments (retry keeps them).
* exception  — failures are typed JSON errors, never silent 200s;
               a blob-store write failure is the named 500
               ``attachment_store_failed`` (and nothing half-stores).

Citation discipline (ADR 0046 / SC9): the attachment text is injected
call-scoped at model time and NEVER into the ``evidence`` channel, so
an attachment-only span cannot pass the citation gate.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.host.scripted_model import ScriptedChatModel

from fin.retrieve import ExtractedTreeReadPort


# ----- shared fixtures (mirror tests/host/test_workspace_http.py) -----


AAPL_2022Q3 = "AAPL-2022Q3"
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)

# A unique sentinel no corpus fixture contains; the recording model
# asserts it reaches (or never reaches) the prompt.
ATTACH_SENTINEL = "THREAD-ATTACH-SENTINEL revenue was 4,242 pesos"


class RecordingScriptedModel(ScriptedChatModel):
    """Scripted model that records the FULL prompt of every call.

    ``ScriptedChatModel._stream`` with neither ``stream_responses`` nor
    ``stream_script`` set delegates to ``_generate``, so overriding
    ``_generate`` records both the sync and the streamed path — and the
    workspace graph always streams.
    """

    seen_calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_calls.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    model = RecordingScriptedModel(script=[AIMessage(content="ok")])
    g = _compile_with(model)
    http_app.dependency_overrides[get_graph] = lambda: g
    return {"store": store, "model": model}


def _compile_with(model: ScriptedChatModel) -> Any:
    from fin.host import compile_graph

    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )


def _install_model(http_app: FastAPI, model: ScriptedChatModel) -> Any:
    from fin.host.http import get_graph

    g = _compile_with(model)
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _attach(client: TestClient, thread_id: str, content: bytes, filename: str = "note.md"):
    return client.post(
        f"/api/threads/{thread_id}/attachments",
        content=content,
        params={"filename": filename},
    )


def _send(
    client: TestClient,
    thread_id: str,
    question: str = "retrieve a summary of the attachment",
    attachment_ids: list[str] | None = None,
):
    # The default question names "retrieve" so the Soft-Track rules
    # tier routes it to the research leaf (model_node) — the only
    # leaf the attachment context is injected into.
    payload: dict[str, Any] = {"question": question}
    if attachment_ids is not None:
        payload["attachment_ids"] = attachment_ids
    return client.post(f"/api/threads/{thread_id}/messages", json=payload)


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "q"}).json()["id"]


# ----- attach endpoint: named boundaries -----


def test_attach_returns_attachment_identity_without_ingest_vocabulary(
    client: TestClient, fresh_store
) -> None:
    """200 body carries attach-scoped identity keys and NEVER ingest
    vocabulary — an attach response must not be mistakable for ingest
    success (error-handling ACR: attach failure/success keys are
    namespaced away from ingest)."""
    thread_id = _new_thread(client)
    content = ATTACH_SENTINEL.encode("utf-8")
    r = _attach(client, thread_id, content, filename="note.md")
    assert r.status_code == 200
    body = r.json()
    assert body["attachment_id"]
    assert body["filename"] == "note.md"
    assert body["size_bytes"] == len(content)
    assert body["scope"] == "thread_attach"
    # No ingest vocabulary anywhere in the response.
    for key in body:
        assert "ingest" not in key.lower()
        assert "push" not in key.lower()


def test_attach_empty_body_named_400(client: TestClient, fresh_store) -> None:
    """Empty upload -> named 400, never a silent 200, and nothing is
    stored (a later send cannot reference a phantom id)."""
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"", filename="empty.md")
    assert r.status_code == 400
    assert r.json() == {"error": "empty_attachment"}

    send = _send(client, thread_id, attachment_ids=["whatever"])
    assert send.status_code == 400
    assert send.json() == {"error": "attachment_not_found"}


def test_attach_oversize_named_413(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload beyond the cap -> named 413. The cap is a module
    constant so operators can tune it; the test shrinks it instead of
    shipping megabytes through the test process."""
    from fin.host import workspace_http

    monkeypatch.setattr(workspace_http, "ATTACHMENT_MAX_BYTES", 8)
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"x" * 16, filename="big.md")
    assert r.status_code == 413
    assert r.json() == {"error": "attachment_too_large"}


def test_attach_oversize_content_length_refused_without_buffering(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input-boundary fix: a DECLARED oversize Content-Length is the
    named 413 BEFORE a single body byte is read into memory. The
    raising stream below is the tripwire — any full-body buffering
    surfaces its error instead of the named 413."""
    from fin.host import workspace_http

    monkeypatch.setattr(workspace_http, "ATTACHMENT_MAX_BYTES", 8)
    thread_id = _new_thread(client)

    def _never_buffered() -> Iterator[bytes]:
        raise AssertionError(
            "route buffered the body despite oversize Content-Length"
        )
        yield b""  # pragma: no cover - generator marker only

    r = client.post(
        f"/api/threads/{thread_id}/attachments",
        content=_never_buffered(),
        params={"filename": "big.md"},
        headers={"Content-Length": "9999"},
    )
    assert r.status_code == 413
    assert r.json() == {"error": "attachment_too_large"}


def test_attach_chunked_oversize_refused_by_bounded_read(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input-boundary fix: with NO Content-Length (chunked) the route
    reads in bounded segments and refuses at cap+1 bytes instead of
    buffering the whole body. (The mid-stream refusal itself is pinned
    by ``test_attach_bounded_body_read_stops_at_the_cap`` — the
    TestClient materializes a generator body on first receive, so the
    end-to-end probe can only pin the named 413 outcome.)"""
    from fin.host import workspace_http

    monkeypatch.setattr(workspace_http, "ATTACHMENT_MAX_BYTES", 8)
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/attachments",
        content=iter([b"x" * 16]),  # chunked: no Content-Length header
        params={"filename": "big.md"},
    )
    assert r.status_code == 413
    assert r.json() == {"error": "attachment_too_large"}


def test_attach_bounded_body_read_stops_at_the_cap() -> None:
    """Unit pin of the chunked leg: the bounded reader stops pulling
    from the stream once the running total passes the cap (three
    4-byte segments against cap 8 = refuse on the third), and joins
    the whole body when it fits."""
    import asyncio

    from fastapi import Request as FastAPIRequest

    from fin.host.workspace_http import _read_bounded_body

    def _request_from_segments(
        segments: list[bytes],
    ) -> tuple[FastAPIRequest, list[int]]:
        pulls: list[int] = []
        messages: list[dict[str, Any]] = [
            {"type": "http.request", "body": seg, "more_body": True}
            for seg in segments
        ] + [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive() -> dict[str, Any]:
            pulls.append(1)
            return messages.pop(0)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
        return FastAPIRequest(scope, receive=receive), pulls

    over, over_pulls = _request_from_segments([b"x" * 4] * 4)
    assert asyncio.run(_read_bounded_body(over, 8)) is None
    assert len(over_pulls) == 3  # 4+4 fit; the third segment trips it

    within, _ = _request_from_segments([b"x" * 4, b"x" * 4])
    assert asyncio.run(_read_bounded_body(within, 8)) == b"x" * 8


def test_attach_non_text_named_400(client: TestClient, fresh_store) -> None:
    """Non-UTF-8 bytes -> named 400 ``attachment_not_text``.

    Text-only is the CONTRACT, not a missing feature: binary formats
    (PDF/DOCX/...) belong to the corpus-ingest door where 切分 runs
    deterministically (ADR 0047). The attach path must never parse
    binaries, so undecodable bytes are rejected by name.
    """
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"\xff\xfe\x00\x9f", filename="x.bin")
    assert r.status_code == 400
    assert r.json() == {"error": "attachment_not_text"}


def test_attach_unknown_thread_404(client: TestClient, fresh_store) -> None:
    r = _attach(client, "no-such-thread", b"hello", filename="a.md")
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


def test_attach_store_write_failure_named_500(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception boundary: a blob-store write failure is a TYPED named
    500 ``attachment_store_failed`` — never a silent 200 and never a
    half-stored attachment (the send side cannot reference a phantom
    id). The store is the turn-blob dict, so the only realistic write
    failure is allocation/construction; the test injects it by
    replacing the blob constructor at module scope (the route resolves
    ``AttachmentBlob`` through module globals at call time)."""
    from fin.host import workspace_http

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise MemoryError("simulated blob allocation failure")

    monkeypatch.setattr(workspace_http, "AttachmentBlob", _boom)
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, ATTACH_SENTINEL.encode(), filename="x.md")
    assert r.status_code == 500
    assert r.json() == {"error": "attachment_store_failed"}
    # Nothing half-stored: a later send cannot reference an id that
    # was never durably held (named 400, not a stale re-read).
    send = _send(client, thread_id, attachment_ids=["whatever"])
    assert send.status_code == 400
    assert send.json() == {"error": "attachment_not_found"}


# ----- send path: named boundaries -----


def test_send_with_unknown_attachment_id_named_400_and_no_run(
    client: TestClient, fresh_store
) -> None:
    """Referencing an id that was never attached -> named 400 BEFORE
    any graph invoke (the scripted model records zero calls)."""
    thread_id = _new_thread(client)
    r = _send(client, thread_id, attachment_ids=["phantom-id"])
    assert r.status_code == 400
    assert r.json() == {"error": "attachment_not_found"}
    assert fresh_store["model"].seen_calls == []


def test_attachment_id_cannot_be_used_from_another_thread(
    client: TestClient, fresh_store
) -> None:
    """Attachments are bound to the thread that uploaded them; a
    sibling thread referencing the id gets the same named 400."""
    thread_a = _new_thread(client)
    thread_b = _new_thread(client)
    attached = _attach(client, thread_a, ATTACH_SENTINEL.encode()).json()
    r = _send(client, thread_b, attachment_ids=[attached["attachment_id"]])
    assert r.status_code == 400
    assert r.json() == {"error": "attachment_not_found"}


# ----- this-turn visibility / next-turn disappearance -----


def _model_saw_sentinel(model: RecordingScriptedModel) -> bool:
    return any(
        ATTACH_SENTINEL in str(message.content)
        for call in model.seen_calls
        for message in call
    )


def test_attachment_text_visible_this_turn_gone_next_turn(
    client: TestClient, fresh_store
) -> None:
    """The attached text reaches THIS run's model prompt (call-scoped
    injection, ADR 0047) and is absent from the NEXT run's prompts —
    ``reset_input_state`` clears the channel, so nothing persists in
    the checkpointed conversation."""
    thread_id = _new_thread(client)
    model: RecordingScriptedModel = fresh_store["model"]
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()

    first = _send(client, thread_id, attachment_ids=[attached["attachment_id"]])
    assert first.status_code == 200
    assert model.seen_calls, "graph never reached the model"
    assert _model_saw_sentinel(model), "attachment text never reached the model"

    model.seen_calls.clear()
    second = _send(client, thread_id, question="retrieve the follow-up summary")
    assert second.status_code == 200
    assert model.seen_calls, "second run never reached the model"
    assert not _model_saw_sentinel(model), "attachment leaked into next turn"


def test_attachment_text_visible_on_dialogue_turn(
    client: TestClient, fresh_store
) -> None:
    """ADR 0047 visibility is UNCONDITIONAL for the turn: "gives the
    model that file for this turn only". Soft-Track may route the
    attach turn to the dialogue leaf (a greeting carries no research
    cues), and that leaf's model call must still see the text —
    otherwise the this-turn contract silently breaks on non-research
    turns."""
    thread_id = _new_thread(client)
    model: RecordingScriptedModel = fresh_store["model"]
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()

    sent = _send(
        client,
        thread_id,
        question="hello there",
        attachment_ids=[attached["attachment_id"]],
    )
    assert sent.status_code == 200
    assert model.seen_calls, "dialogue run never reached the model"
    assert _model_saw_sentinel(model), "attachment text never reached the dialogue leaf"


def test_attachment_is_single_use_consumed_by_send(
    client: TestClient, fresh_store
) -> None:
    """Turn-blob semantics: the send that references the attachment
    consumes it; replaying the same id on a later send is a named 400
    rather than a silently stale re-read."""
    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    assert _send(
        client, thread_id, attachment_ids=[attached["attachment_id"]]
    ).status_code == 200
    replay = _send(
        client, thread_id, attachment_ids=[attached["attachment_id"]]
    )
    assert replay.status_code == 400
    assert replay.json() == {"error": "attachment_not_found"}


def test_concurrent_send_409_does_not_consume_attachments(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """409 concurrent_run semantics are unchanged AND the 409'd send
    keeps the referenced attachments: the retry after the in-flight
    run finishes still sees the text (concurrent boundary)."""
    entered = threading.Event()
    release = threading.Event()

    class BlockingRecordingModel(RecordingScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.seen_calls.append(list(messages))
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    blocking = BlockingRecordingModel(script=[AIMessage(content="answer")])
    fresh_store["model"] = blocking
    _install_model(http_app, blocking)

    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()

    first_status: dict[str, int] = {}

    def hit_first() -> None:
        r = _send(client, thread_id, question="retrieve the held lock summary")
        first_status["code"] = r.status_code

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(hit_first)
        assert entered.wait(timeout=5.0), "first send never entered the graph"
        racing = _send(
            client, thread_id, attachment_ids=[attached["attachment_id"]]
        )
        assert racing.status_code == 409
        assert racing.json() == {"error": "concurrent_run"}
        release.set()
        f1.result(timeout=10.0)
    assert first_status["code"] == 200

    # The 409 did NOT consume the attachment: the retry still sees it.
    model: RecordingScriptedModel = blocking
    model.seen_calls.clear()
    retry = _send(
        client, thread_id, attachment_ids=[attached["attachment_id"]]
    )
    assert retry.status_code == 200
    assert _model_saw_sentinel(model)


# ----- no push / no ingest / no citation-gate leak -----


def test_attach_and_send_never_ingest_or_push(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC4: attach + a full send turn records ZERO ingest/push calls.

    The corpus module's ingest entry (``run_ingest``) and the push
    record builders are replaced with tripwires — any attach-path or
    graph-node call into them fails the test. ADR 0047: the corpus
    door is the separate ingest endpoint (T3), never the attach door.
    """
    import fin.corpus.ingest as corpus_ingest
    import fin.corpus.push as corpus_push

    calls: list[str] = []

    def _tripwire(name: str):
        def _record(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"attach path called {name}")

        return _record

    monkeypatch.setattr(corpus_ingest, "run_ingest", _tripwire("run_ingest"))
    monkeypatch.setattr(
        corpus_push, "unit_push_records", _tripwire("unit_push_records")
    )
    monkeypatch.setattr(
        corpus_push,
        "document_push_records",
        _tripwire("document_push_records"),
    )

    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    sent = _send(client, thread_id, attachment_ids=[attached["attachment_id"]])
    assert sent.status_code == 200
    assert calls == []


def test_attach_does_not_trigger_mcp_read_source(
    client: TestClient, fresh_store, http_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2: thread attach never resolves a body through MCP ``read_source``.

    The attach door is "give the model that file for this turn only"
    (ADR 0047). Re-routing attachment text through MCP would silently
    expand the read_file tool's surface, so the seam must short-circuit
    in the host graph before any MCP call. A spy on
    ``MCPRetrieveAdapter.read`` is installed and the graph is rebuilt
    with the MCP adapter as its ``read_port``; the assertion holds
    whether or not the model itself asks for ``read_file``.
    """
    from fin.host.http import get_graph
    from fin.retrieve import MCPRetrieveAdapter

    calls: list[tuple[str, str]] = []

    real_read = MCPRetrieveAdapter.read

    def _spy(self, document_id, version, page=1, page_limit=1):
        calls.append((document_id, version))
        return real_read(self, document_id, version, page, page_limit)

    monkeypatch.setattr(MCPRetrieveAdapter, "read", _spy)

    # Build a graph whose read port IS the MCP adapter so the spy
    # is on the actual seam, not a side branch.
    mcp_adapter = MCPRetrieveAdapter(
        command=None,
        known_document_ids=KNOWN_IDS,
    )
    model = RecordingScriptedModel(script=[AIMessage(content="ok")])
    graph = _compile_with_mcp_read_port(model, mcp_adapter)
    http_app.dependency_overrides[get_graph] = lambda: graph

    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    sent = _send(client, thread_id, attachment_ids=[attached["attachment_id"]])
    assert sent.status_code == 200

    # No MCP ``read`` happened on the attach turn.
    assert calls == [], (
        "thread attach triggered an MCP read; the attach door must "
        "never resolve a body through read_source"
    )


def _compile_with_mcp_read_port(model: ScriptedChatModel, mcp_adapter: Any) -> Any:
    from fin.host import compile_graph

    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=mcp_adapter,
    )


def test_attachment_text_cannot_satisfy_citation_gate(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """ADR 0046 / SC9 unchanged: the gate matches spans against
    THIS-run retrieve/read_file payloads only. An answer quoting a
    32+ char span that exists ONLY in the attachment text bounces and
    ends in the typed refuse — the model read the text but cannot
    cite it."""
    # A span > MIN_SPAN_LEN (32) that lives only in the attachment.
    attachment_span = "Unique attachment claim: the widget margin was 42.7 percent for fiscal 2031."
    assert len(attachment_span) >= 32

    model = RecordingScriptedModel(
        script=[AIMessage(content=f"Per the file: {attachment_span}")]
    )
    _install_model(http_app, model)

    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, attachment_span.encode()).json()
    sent = _send(client, thread_id, attachment_ids=[attached["attachment_id"]])
    assert sent.status_code == 200

    events = _parse_sse_events(sent.text)
    statuses = [data for event, data in events if event == "status"]
    assert statuses, "no terminal status frame"
    final = statuses[-1]
    # The attachment-only span never becomes a verified citation.
    assert final["status"] == "refuse"
    assert final["reason"] == "citation_repair_cap"


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


# ----- state contract -----


def test_reset_input_state_defaults_to_no_attachments_and_accepts_them() -> None:
    """``reset_input_state`` gains the attachment channel: default run
    input carries NO attachments (follow-up turns start clean), an
    explicit list rides the same input, and the channel is
    overwrite-semantics (a later ``reset_input_state("q")`` clears a
    prior run's attachments)."""
    from fin.host.state import reset_input_state

    bare = reset_input_state("q")
    assert bare["attachments"] == []

    carried = reset_input_state(
        "q",
        attachments=[
            {"attachment_id": "a1", "filename": "n.md", "text": ATTACH_SENTINEL}
        ],
    )
    assert carried["attachments"] == [
        {"attachment_id": "a1", "filename": "n.md", "text": ATTACH_SENTINEL}
    ]

    assert reset_input_state("again")["attachments"] == []
