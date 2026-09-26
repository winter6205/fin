"""Thread attach: durable to the thread, searchable, never a push.

Covers the attach door's named boundaries plus the two product invariants
that survive G4: the text a turn references reaches that run's prompt
call-scoped (ADR 0047), and attaching NEVER pushes into the permanent
company corpus (ADR 0083). The corpus-ingest door is a separate control;
an attach failure must be named and must never look like ingest success.

Boundary classes exercised (defensive-contract-validator):

* empty      — empty upload body -> named 400 ``empty_attachment``.
* negative   — non-UTF-8 bytes are stored raw and reported ``uploaded``
               (the door does not classify; ADR 0089); the typed
               ``attachment_not_text`` failure comes from preparation.
               Unknown attachment id on send -> named 400.
* overflow   — upload over the configured ``max_upload_bytes`` budget ->
               named 413 ``attachment_too_large``, with no partial object
               and no attachment row.
* concurrent — 409 concurrent_run is unchanged; a 409'd send does NOT
               lose the referenced attachments (retry keeps them); two
               concurrent uploads complete as distinct attachments.
* exception  — failures are typed JSON errors, never silent 200s; a
               durable write failure is the named 500
               ``attachment_store_failed`` (and nothing half-stores).

Citation discipline (ADR 0046): the attachment text is injected
call-scoped at model time and NEVER into the ``evidence`` channel, so an
attachment-only span cannot pass the citation gate.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment_budgets import AttachmentBudgets
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

# The deployed budget the door reads its byte cap from. Tests take it as
# the default and shrink ``max_upload_bytes`` per case instead of
# shipping megabytes through the test process.
DEFAULT_BUDGETS = AttachmentBudgets(
    max_upload_bytes=1_048_576,
    max_pdf_pages=8,
    max_vision_calls_per_run=16,
    max_vision_crop_calls_per_run=8,
)


class _RecordingObjectStore(InMemoryObjectStore):
    """Byte store that remembers every key it was asked to put.

    The overflow clause is "no partial object", which needs the call
    history — the final contents alone cannot prove nothing landed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []

    def put(self, key, payload, *, content_type=None):  # type: ignore[no-untyped-def]
        self.puts.append(key)
        return super().put(key, payload, content_type=content_type)


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
    from fin.host.attachment_prepare import AttachmentPreparationService
    from fin.host.attachment_store import InMemoryAttachmentStore
    from fin.host.http import (
        get_attachment_budgets,
        get_attachment_service,
        get_graph,
        get_thread_store,
    )
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    objects = _RecordingObjectStore()
    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(store), objects=objects
    )
    http_app.dependency_overrides[get_thread_store] = lambda: store
    http_app.dependency_overrides[get_attachment_service] = lambda: service
    http_app.dependency_overrides[get_attachment_budgets] = lambda: DEFAULT_BUDGETS
    model = RecordingScriptedModel(script=[AIMessage(content="ok")])
    g = _compile_with(model)
    http_app.dependency_overrides[get_graph] = lambda: g
    return {
        "store": store,
        "model": model,
        "attachments": service,
        "objects": objects,
    }


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


def _override_upload_cap(http_app: FastAPI, cap: int) -> None:
    """Re-deploy the byte budget with a cap small enough for a test body."""
    from fin.host.http import get_attachment_budgets

    budgets = replace(DEFAULT_BUDGETS, max_upload_bytes=cap)
    http_app.dependency_overrides[get_attachment_budgets] = lambda: budgets


def _prepare(fresh_store, attachment_id: str) -> None:
    """Drive the preparation the upload door no longer starts itself.

    Attachment text reaches a turn only through ``searchable``, and
    preparation is no longer part of the upload — every turn-visibility
    case has to ask for it, exactly as the graph-triggered job will.
    """
    fresh_store["attachments"].prepare(attachment_id)


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
    assert fresh_store["objects"].puts == []
    assert fresh_store["attachments"].store.list_for_thread(thread_id) == []

    send = _send(client, thread_id, attachment_ids=["whatever"])
    assert send.status_code == 400
    assert send.json() == {"error": "attachment_not_found"}


def test_attach_stores_raw_bytes_and_defers_every_derived_step(
    client: TestClient, fresh_store
) -> None:
    """The upload door durably stores bytes and nothing else.

    ``TestClient`` runs any background task the route registered before
    handing back the response, so a row that still reads ``uploaded``
    after the response — no version, no units, no index — is proof the
    door scheduled no parsing, OCR, layout, vision, or indexing behind
    it. Preparation is reached only when it is asked for.
    """
    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    attachment_id = attached["attachment_id"]

    assert attached["status"] == "uploaded"
    assert attached["searchable"] is False
    assert attached["version"] == 0
    assert attached["unit_count"] == 0

    service = fresh_store["attachments"]
    record = service.status(attachment_id)
    assert record.status == "uploaded"
    assert record.version == 0
    assert service.units(attachment_id) == ()
    assert service.store.indexed_versions(attachment_id) == ()


def test_attach_without_filename_uses_the_display_default(
    client: TestClient, fresh_store
) -> None:
    """Input contract: the ``filename`` parameter is optional. A missing
    name yields the stored display default, never an empty label or a
    400 — the bytes are what the door is for."""
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/attachments", content=b"raw text"
    )
    assert r.status_code == 200
    assert r.json()["filename"] == "attachment.txt"
    assert fresh_store["attachments"].status(r.json()["attachment_id"]).filename == (
        "attachment.txt"
    )


def test_concurrent_uploads_complete_as_distinct_attachments(
    client: TestClient, fresh_store
) -> None:
    """Concurrency boundary: two uploads racing on one thread BOTH land
    as their own rows with their own ids. Neither is lost and neither
    overwrites the other."""
    thread_id = _new_thread(client)
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [
            ex.submit(_attach, client, thread_id, body, filename)
            for body, filename in (
                (b"first attach text", "one.md"),
                (b"second attach text", "two.md"),
            )
        ]
        responses = [future.result(timeout=10.0) for future in futures]

    assert [r.status_code for r in responses] == [200, 200]
    ids = {r.json()["attachment_id"] for r in responses}
    assert len(ids) == 2
    rows = fresh_store["attachments"].store.list_for_thread(thread_id)
    assert {row.attachment_id for row in rows} == ids
    assert {row.status for row in rows} == {"uploaded"}
    assert len(fresh_store["objects"].puts) == 2


def test_attach_oversize_named_413(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Upload beyond the deployed byte budget -> named 413. The cap is
    read through the injected ``AttachmentBudgets`` dependency, so the
    test re-deploys the budget rather than patching a module constant."""
    _override_upload_cap(http_app, 8)
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"x" * 16, filename="big.md")
    assert r.status_code == 413
    assert r.json() == {"error": "attachment_too_large"}


def test_attach_exact_budget_fits_and_one_byte_over_leaves_nothing(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Overflow boundary, decided at the cap: exactly ``max_upload_bytes``
    is stored, one byte more is the named 413 — and the refusal leaves
    neither a partial object in the byte store nor an attachment row."""
    _override_upload_cap(http_app, 8)
    thread_id = _new_thread(client)

    exact = _attach(client, thread_id, b"x" * 8, filename="exact.md")
    assert exact.status_code == 200
    exact_id = exact.json()["attachment_id"]
    assert exact.json()["size_bytes"] == 8

    over = _attach(client, thread_id, b"x" * 9, filename="over.md")
    assert over.status_code == 413
    assert over.json() == {"error": "attachment_too_large"}

    service = fresh_store["attachments"]
    assert [row.attachment_id for row in service.store.list_for_thread(thread_id)] == [
        exact_id
    ]
    assert fresh_store["objects"].puts == [
        f"thread-attachments/{thread_id}/{exact_id}"
    ]


def test_attach_oversize_content_length_refused_without_buffering(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Input-boundary fix: a DECLARED oversize Content-Length is the
    named 413 BEFORE a single body byte is read into memory, and the
    refusal stores nothing. The raising stream below is the tripwire —
    any full-body buffering surfaces its error instead of the named
    413."""
    _override_upload_cap(http_app, 8)
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
    assert fresh_store["objects"].puts == []
    assert fresh_store["attachments"].store.list_for_thread(thread_id) == []


def test_attach_chunked_oversize_refused_by_bounded_read(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Input-boundary fix: with NO Content-Length (chunked) the route
    reads in bounded segments and refuses at cap+1 bytes instead of
    buffering the whole body. (The mid-stream refusal itself is pinned
    by ``test_attach_bounded_body_read_stops_at_the_cap`` — the
    TestClient materializes a generator body on first receive, so the
    end-to-end probe can only pin the named 413 outcome.)"""
    _override_upload_cap(http_app, 8)
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/attachments",
        content=iter([b"x" * 16]),  # chunked: no Content-Length header
        params={"filename": "big.md"},
    )
    assert r.status_code == 413
    assert r.json() == {"error": "attachment_too_large"}
    assert fresh_store["attachments"].store.list_for_thread(thread_id) == []


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
    """Non-UTF-8 bytes are STORED and reported ``uploaded``.

    The door no longer classifies: it takes raw bytes and nothing else
    (ADR 0089). The named ``attachment_not_text`` rejection is the
    preparation's, so it surfaces when preparation is asked for.
    """
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"\xff\xfe\x00\x9f", filename="x.bin")
    assert r.status_code == 200
    uploaded = r.json()
    assert uploaded["status"] == "uploaded"
    assert uploaded["searchable"] is False
    attachment_id = uploaded["attachment_id"]

    service = fresh_store["attachments"]
    assert service.status(attachment_id).status == "uploaded"
    assert service.units(attachment_id) == ()

    failed = service.prepare(attachment_id)
    assert failed.status == "failed"
    assert failed.failure_reason == "attachment_not_text"
    shown = client.get(f"/api/threads/{thread_id}/attachments/{attachment_id}")
    assert shown.status_code == 200
    assert shown.json()["status"] == "failed"
    assert shown.json()["failure_reason"] == "attachment_not_text"


def test_attach_unknown_thread_404(client: TestClient, fresh_store) -> None:
    r = _attach(client, "no-such-thread", b"hello", filename="a.md")
    assert r.status_code == 404
    assert r.json() == {"error": "thread_not_found"}


def test_attach_store_write_failure_named_500(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception boundary: a durable write failure is a TYPED named 500
    ``attachment_store_failed`` — never a silent 200 and never a
    half-stored attachment (the send side cannot reference a phantom
    id). The byte store is the first durable layer the door writes, so
    its refusal is the failure injected here, and nothing may be left
    behind by it."""
    from fin.corpus.object_store import InMemoryObjectStore

    class RefusingObjectStore(InMemoryObjectStore):
        def put(self, key, payload, *, content_type=None):  # type: ignore[no-untyped-def]
            raise MemoryError("simulated blob allocation failure")

    service = fresh_store["attachments"]
    monkeypatch.setattr(service, "objects", RefusingObjectStore())
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, ATTACH_SENTINEL.encode(), filename="x.md")
    assert r.status_code == 500
    assert r.json() == {"error": "attachment_store_failed"}
    # Nothing half-stored: the thread owns no attachment record, and a
    # later send cannot reference an id that was never durably held.
    assert service.store.list_for_thread(thread_id) == []
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


# ----- host send gate: an upload in flight holds its own thread -----


class _BlockingPutStore:
    """Delegates to the real byte store; ``put`` waits for the test.

    The window the send gate must cover is exactly "``put`` has not
    returned yet", so the test has to hold that window open from the
    inside and be told when it is open — a sleep cannot say both.
    """

    def __init__(self, inner: InMemoryObjectStore) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def put(self, key, payload, *, content_type=None):  # type: ignore[no-untyped-def]
        self.entered.set()
        assert self.release.wait(
            timeout=5.0
        ), "the upload gate was never released; the upload is stuck"
        return self.inner.put(key, payload, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def get_pointer(self, key: str):
        return self.inner.get_pointer(key)

    def delete(self, key: str) -> None:
        self.inner.delete(key)


def _hold_an_upload(
    client: TestClient, thread_id: str, gate: _BlockingPutStore
) -> tuple[ThreadPoolExecutor, Any]:
    """Start one upload on its own thread and wait until it is inside
    the byte store — the only ordering the next send may rely on."""
    in_flight = ThreadPoolExecutor(max_workers=1)
    started = in_flight.submit(
        _attach, client, thread_id, ATTACH_SENTINEL.encode()
    )
    assert gate.entered.wait(timeout=5.0), "upload never reached the byte store"
    return in_flight, started


def test_send_is_refused_while_an_upload_is_in_flight(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrency boundary, host half of the send gate: while this
    thread's bytes are still being stored, a send is the named 409 with
    NO run started and no turn row, title, or lock touched — and the
    same send proceeds once the upload lands."""
    thread_id = _new_thread(client)
    store = fresh_store["store"]
    title_before = store.get_thread(thread_id).title
    gate = _BlockingPutStore(fresh_store["objects"])
    monkeypatch.setattr(fresh_store["attachments"], "objects", gate)

    in_flight, started = _hold_an_upload(client, thread_id, gate)

    blocked = _send(client, thread_id)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json() == {"error": "attachment_upload_in_progress"}
    assert fresh_store["model"].seen_calls == [], "gated send reached the graph"
    assert store.list_runs(thread_id) == []
    assert store.get_thread(thread_id).title == title_before

    gate.release.set()
    landed = started.result(timeout=10.0)
    in_flight.shutdown(wait=True)

    assert landed.status_code == 200, landed.text
    assert landed.json()["status"] == "uploaded"
    proceeded = _send(client, thread_id)
    assert proceeded.status_code == 200, proceeded.text
    assert fresh_store["model"].seen_calls, "send after the upload never ran"


def test_send_is_refused_by_an_upload_the_message_does_not_name(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is per thread, not per reference: the message names a
    ready attachment while a SECOND file is still writing its bytes, so
    the send is refused even though the pending record — the id the
    reference rule would have checked — does not exist yet."""
    thread_id = _new_thread(client)
    ready = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    _prepare(fresh_store, ready["attachment_id"])
    gate = _BlockingPutStore(fresh_store["objects"])
    monkeypatch.setattr(fresh_store["attachments"], "objects", gate)

    in_flight, started = _hold_an_upload(client, thread_id, gate)

    named_elsewhere = _send(
        client, thread_id, attachment_ids=[ready["attachment_id"]]
    )
    assert named_elsewhere.status_code == 409, named_elsewhere.text
    assert named_elsewhere.json() == {"error": "attachment_upload_in_progress"}
    assert fresh_store["model"].seen_calls == []

    gate.release.set()
    assert started.result(timeout=10.0).status_code == 200
    in_flight.shutdown(wait=True)

    model = fresh_store["model"]
    model.seen_calls.clear()
    resumed = _send(client, thread_id, attachment_ids=[ready["attachment_id"]])
    assert resumed.status_code == 200, resumed.text
    assert _model_saw_sentinel(model)


def test_every_upload_refusal_leaves_the_send_gate_closed_again(
    client: TestClient,
    fresh_store,
    http_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``finally`` half of the gate: every named refusal path of the
    upload door hands the thread back. One leak would hold that thread's
    sends at 409 for the life of the process — and a thread the door
    never found must still answer its own 404, not the gate's 409."""
    thread_id = _new_thread(client)
    service = fresh_store["attachments"]

    refused = client.post(
        "/api/threads/no-such-thread/attachments",
        content=b"hello",
        params={"filename": "a.md"},
    )
    assert refused.status_code == 404
    ghost = _send(client, "no-such-thread")
    assert (ghost.status_code, ghost.json()) == (
        404,
        {"error": "thread_not_found"},
    )

    _override_upload_cap(http_app, 8)

    def _never_read() -> Iterator[bytes]:
        raise AssertionError("route buffered a declared-oversize body")
        yield b""  # pragma: no cover - generator marker only

    declared = client.post(
        f"/api/threads/{thread_id}/attachments",
        content=_never_read(),
        params={"filename": "big.md"},
        headers={"Content-Length": "9999"},
    )
    assert declared.status_code == 413
    assert _send(client, thread_id).status_code == 200

    streamed = client.post(
        f"/api/threads/{thread_id}/attachments",
        content=iter([b"x" * 16]),
        params={"filename": "big.md"},
    )
    assert streamed.status_code == 413
    assert _send(client, thread_id).status_code == 200

    empty = _attach(client, thread_id, b"", filename="empty.md")
    assert empty.status_code == 400
    assert _send(client, thread_id).status_code == 200

    class _FailingStore(InMemoryObjectStore):
        def put(self, key, payload, *, content_type=None):  # type: ignore[no-untyped-def]
            raise MemoryError("simulated blob allocation failure")

    monkeypatch.setattr(service, "objects", _FailingStore())
    broken = _attach(client, thread_id, b"123", filename="x.md")
    assert broken.status_code == 500
    assert broken.json() == {"error": "attachment_store_failed"}

    sent = _send(client, thread_id)
    assert sent.status_code == 200, sent.text
    assert service.store.list_for_thread(thread_id) == []


def test_an_upload_in_flight_does_not_hold_another_thread(
    client: TestClient, fresh_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread isolation of the gate: a send on a different thread is
    untouched by the bytes this thread is still writing."""
    busy = _new_thread(client)
    idle = _new_thread(client)
    gate = _BlockingPutStore(fresh_store["objects"])
    monkeypatch.setattr(fresh_store["attachments"], "objects", gate)

    in_flight, started = _hold_an_upload(client, busy, gate)

    elsewhere = _send(client, idle)
    assert elsewhere.status_code == 200, elsewhere.text
    assert fresh_store["model"].seen_calls, "other thread's run never ran"
    held = _send(client, busy)
    assert held.status_code == 409, held.text
    assert held.json() == {"error": "attachment_upload_in_progress"}

    gate.release.set()
    assert started.result(timeout=10.0).status_code == 200
    in_flight.shutdown(wait=True)

    released = _send(client, busy)
    assert released.status_code == 200, released.text


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
    _prepare(fresh_store, attached["attachment_id"])

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
    """ADR 0047 visibility is UNCONDITIONAL for the run that names the
    attachment. Soft-Track may route the attach turn to the dialogue leaf (a
    greeting carries no research
    cues), and that leaf's model call must still see the text —
    otherwise the attach contract silently breaks on non-research
    turns."""
    thread_id = _new_thread(client)
    model: RecordingScriptedModel = fresh_store["model"]
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    _prepare(fresh_store, attached["attachment_id"])

    sent = _send(
        client,
        thread_id,
        question="hello there",
        attachment_ids=[attached["attachment_id"]],
    )
    assert sent.status_code == 200
    assert model.seen_calls, "dialogue run never reached the model"
    assert _model_saw_sentinel(model), "attachment text never reached the dialogue leaf"


def test_attachment_remains_referenceable_for_the_thread_lifetime(
    client: TestClient, fresh_store
) -> None:
    """Thread-lifetime contract (ADR 0088): a send that references an
    attachment does not consume it, so a later turn re-reads the same id
    instead of forcing the operator to re-attach."""
    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    _prepare(fresh_store, attached["attachment_id"])
    assert _send(
        client, thread_id, attachment_ids=[attached["attachment_id"]]
    ).status_code == 200
    replay = _send(
        client, thread_id, attachment_ids=[attached["attachment_id"]]
    )
    assert replay.status_code == 200
    model: RecordingScriptedModel = fresh_store["model"]
    assert _model_saw_sentinel(model), "replay turn lost the attachment text"


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
    _prepare(fresh_store, attached["attachment_id"])

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
    # The upload alone is already silent on both doors.
    assert calls == []
    _prepare(fresh_store, attached["attachment_id"])
    sent = _send(client, thread_id, attachment_ids=[attached["attachment_id"]])
    assert sent.status_code == 200
    assert calls == []


def test_attach_does_not_trigger_mcp_read_source(
    client: TestClient, fresh_store, http_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread attach never resolves a body through MCP ``read_source``.

    The attach door hands the model the thread's own stored text (ADRs
    0047/0086); it is not a corpus locator. Re-routing attachment text
    through MCP would silently
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
    _prepare(fresh_store, attached["attachment_id"])
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
    _prepare(fresh_store, attached["attachment_id"])
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
