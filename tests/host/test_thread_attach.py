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
* malformed identity — every ``{attachment_id}`` door (status, retry, delete)
               answers the named 400 ``invalid_locator`` before the segment
               can reach the durable store as a query.

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
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.corpus.extracted import load_extracted_tree
from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment.attachment_budgets import AttachmentBudgets
from fin.host.scripted_model import ScriptedChatModel

from fin.retrieve import FakeRetrievePort


# ----- shared fixtures (mirror tests/host/test_workspace_http.py) -----


AAPL_2022Q3 = "AAPL-2022Q3"
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# The committed corpus units behind the graph's ``read_source`` company leg;
# the serve path itself never walks the tree (ADR 0046 / 0047).
_READ_ADAPTER = FakeRetrievePort(
    pages_by_doc={
        AAPL_2022Q3: load_extracted_tree(
            Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
        )[AAPL_2022Q3]
    }
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
    from fin.host.attachment.attachment_prepare import AttachmentPreparationService
    from fin.host.attachment.attachment_store import InMemoryAttachmentStore
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
        adapter=_READ_ADAPTER,
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


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("/abs/dir/note.md", "note.md"),
        ("..\\..\\windows\\note.md", "..\\..\\windows\\note.md"),
        ("note\x00\x01.md", "note.md"),
        ("   ", "attachment.txt"),
        ("quarterly 报告.md", "quarterly 报告.md"),
    ],
)
def test_malformed_filename_lands_as_a_display_name_only(
    client: TestClient, fresh_store, raw: str, expected: str
) -> None:
    """Input contract: a name is a label, never a path.

    The model and the card read this string, and the object key must not
    be derived from it, so traversal segments, control characters, and
    blank names reduce to a printable basename. The Windows separators
    survive because on this host they are not separators — what is
    pinned is that they reach nothing but the label.
    """
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"raw text", filename=raw)
    assert r.status_code == 200
    attachment_id = r.json()["attachment_id"]
    assert r.json()["filename"] == expected

    record = fresh_store["attachments"].status(attachment_id)
    assert record.filename == expected
    assert "/" not in record.filename
    assert all(char.isprintable() for char in record.filename)
    assert len(record.filename) <= 200
    assert record.object_key == f"thread-attachments/{thread_id}/{attachment_id}"
    owned = fresh_store["attachments"].list_for_thread(thread_id)
    assert [item.attachment_id for item in owned] == [attachment_id]


def test_overlong_filename_is_truncated_to_the_stored_bound(
    client: TestClient, fresh_store
) -> None:
    """200 characters is a bound the door enforces, not a display choice:
    the name is stored and replayed on every later read."""
    thread_id = _new_thread(client)
    r = _attach(client, thread_id, b"raw text", filename="a" * 500)
    assert r.status_code == 200
    assert r.json()["filename"] == "a" * 200
    stored = fresh_store["attachments"].status(r.json()["attachment_id"])
    assert len(stored.filename) == 200


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


def test_attach_non_text_bytes_are_stored_and_rejected_at_preparation(
    client: TestClient, fresh_store
) -> None:
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
    """Thread attach never resolves a body through the MCP ``read_source`` leg.

    The attach door hands the model the thread's own stored text (ADRs
    0047/0086); it is not a corpus locator. Re-routing attachment text
    through MCP would silently widen the company read surface, so the seam
    must short-circuit in the host graph before any MCP call. A spy on
    ``MCPRetrieveAdapter.read_slice`` is installed and the graph is rebuilt
    with the MCP adapter as its bound company leg; the assertion holds
    whether or not the model itself asks for ``read_source``.
    """
    from fin.host.http import get_graph
    from fin.retrieve import MCPRetrieveAdapter

    calls: list[dict[str, Any]] = []

    real_read_slice = MCPRetrieveAdapter.read_slice

    def _spy(self, source_ref, position=0):
        calls.append(dict(source_ref))
        return real_read_slice(self, source_ref, position)

    monkeypatch.setattr(MCPRetrieveAdapter, "read_slice", _spy)

    # Build a graph whose company read leg IS the MCP adapter so the spy
    # is on the actual seam, not a side branch.
    mcp_adapter = MCPRetrieveAdapter(
        command=None,
        known_document_ids=KNOWN_IDS,
    )
    model = RecordingScriptedModel(script=[AIMessage(content="ok")])
    graph = _compile_with_mcp_adapter(model, mcp_adapter)
    http_app.dependency_overrides[get_graph] = lambda: graph

    thread_id = _new_thread(client)
    attached = _attach(client, thread_id, ATTACH_SENTINEL.encode()).json()
    _prepare(fresh_store, attached["attachment_id"])
    sent = _send(client, thread_id, attachment_ids=[attached["attachment_id"]])
    assert sent.status_code == 200

    # No MCP company read happened on the attach turn.
    assert calls == [], (
        "thread attach triggered an MCP company read; the attach door must "
        "never resolve a body through read_source"
    )


def _compile_with_mcp_adapter(model: ScriptedChatModel, mcp_adapter: Any) -> Any:
    from fin.host import compile_graph

    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=mcp_adapter,
    )


def test_attachment_text_cannot_satisfy_citation_gate(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """ADR 0046 / SC9 unchanged: the gate matches spans against
    THIS-run retrieve/read_source payloads only. An answer quoting a
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


# ----- single-attachment delete door: named boundaries -----


def _attachment_resource_path(thread_id: str, attachment_id: str) -> str:
    """One attachment's shipped URL, with the identity percent-encoded.

    The status door reads this path, the delete door voids it, and the retry
    door hangs ``/retry`` off it, so all three are asked about the same URL a
    client builds. Encoding keeps the test's judgement the route's: the
    identity arrives decoded, exactly as a browser sends it.
    """
    from fin.host.workspace_http import _attachment_delete_path

    return _attachment_delete_path(thread_id, quote(attachment_id, safe=""))


def _delete_attachment(
    client: TestClient, thread_id: str, attachment_id: str, **transport: Any
) -> Any:
    """``DELETE`` the shipped path for one attachment.

    Any extra keyword goes to the transport, so a test of malformed text or
    garbage bytes proves the route's judgement and not the HTTP client's.
    """
    return client.request(
        "DELETE", _attachment_resource_path(thread_id, attachment_id), **transport
    )


def _attachment_status(
    client: TestClient, thread_id: str, attachment_id: str
) -> Any:
    """``GET`` one attachment's status through the shipped door."""
    return client.get(_attachment_resource_path(thread_id, attachment_id))


def _retry_attachment(client: TestClient, thread_id: str, attachment_id: str) -> Any:
    """``POST`` the shipped retry path for one attachment."""
    return client.request(
        "POST", f"{_attachment_resource_path(thread_id, attachment_id)}/retry"
    )


# The malformed path identities every ``{attachment_id}`` door must refuse.
# One list and one set of case names, so the three doors are provably judged
# on identical input.
_MALFORMED_LOCATORS = ["   ", "a\tb", "line\nbreak", "x" * 5001]
_MALFORMED_LOCATOR_IDS = ["blank", "tab", "newline", "oversized"]


def _record_store_lookups(
    fresh_store, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Every attachment id a door was allowed to hand the durable store.

    The guard promises a malformed segment never becomes a query, so the
    returned list must stay empty. This spies rather than stubs: the store's
    own answer still comes back, so the same test can also prove the thread's
    real attachment was left alone.
    """
    seen: list[str] = []
    real_get = fresh_store["attachments"].store.get

    def _spy(attachment_id: str) -> Any:
        seen.append(attachment_id)
        return real_get(attachment_id)

    monkeypatch.setattr(fresh_store["attachments"].store, "get", _spy)
    return seen


@pytest.mark.parametrize(
    "attachment_id", _MALFORMED_LOCATORS, ids=_MALFORMED_LOCATOR_IDS
)
def test_delete_door_refuses_a_malformed_attachment_id_as_a_typed_validation_error(
    client: TestClient, fresh_store, attachment_id: str
) -> None:
    """A malformed path identity is a named 400, never a 500 and never a void.

    The value reaches the route decoded, so the route must judge it before
    touching any state — and must leave the thread's real attachment alone.
    """
    thread_id = _new_thread(client)
    stored = _attach(client, thread_id, ATTACH_SENTINEL.encode("utf-8")).json()

    response = _delete_attachment(client, thread_id, attachment_id)

    assert response.status_code == 400, response.text
    assert response.json() == {"error": "invalid_locator"}
    assert fresh_store["attachments"].status(stored["attachment_id"]) is not None


def test_delete_door_names_an_unknown_thread_and_touches_nothing(
    client: TestClient, fresh_store
) -> None:
    thread_id = _new_thread(client)
    stored = _attach(client, thread_id, ATTACH_SENTINEL.encode("utf-8")).json()

    response = _delete_attachment(
        client, f"no-such-thread-{thread_id}", stored["attachment_id"]
    )

    assert response.status_code == 404
    assert response.json() == {"error": "thread_not_found"}
    assert fresh_store["attachments"].status(stored["attachment_id"]) is not None


def test_delete_door_takes_no_request_body(client: TestClient, fresh_store) -> None:
    """A DELETE with a malformed body is the same call as one with no body.

    The route's whole input is the two path identities, so garbage bytes
    cannot change the outcome and cannot reach the stores. Two attachments,
    one deleted with each transport shape, proves the two calls agree without
    making the second one a repeat of the first.
    """
    thread_id = _new_thread(client)
    garbage = _attach(client, thread_id, ATTACH_SENTINEL.encode("utf-8")).json()[
        "attachment_id"
    ]
    plain = _attach(client, thread_id, "second file".encode("utf-8")).json()[
        "attachment_id"
    ]

    with_body = _delete_attachment(
        client,
        thread_id,
        garbage,
        content=b'{"not": "part of the contract"',
        headers={"content-type": "application/json"},
    )
    without_body = _delete_attachment(client, thread_id, plain)

    assert (with_body.status_code, without_body.status_code) == (200, 200)
    assert fresh_store["attachments"].status(garbage) is None
    assert fresh_store["attachments"].status(plain) is None


def test_delete_door_names_an_unknown_locator(client: TestClient, fresh_store) -> None:
    thread_id = _new_thread(client)

    response = _delete_attachment(client, thread_id, "att-never-uploaded")

    assert response.status_code == 400
    assert response.json() == {"error": "unknown_locator"}


def test_delete_door_maps_a_store_rejection_to_a_named_exit(
    client: TestClient, http_app: FastAPI, fresh_store
) -> None:
    """A void that the durable store refuses is 500 ``attachment_store_failed``.

    The door must not answer success for an attachment whose cleanup
    obligation could not be recorded, and must not answer a bare 500.
    """
    from fin.host.attachment.attachment_prepare import AttachmentPreparationService
    from fin.host.attachment.attachment_store import (
        AttachmentStoreError,
        InMemoryAttachmentStore,
    )
    from fin.host.http import get_attachment_service

    thread_id = _new_thread(client)
    stored = _attach(client, thread_id, ATTACH_SENTINEL.encode("utf-8")).json()
    aid = stored["attachment_id"]

    class _RejectingStore(InMemoryAttachmentStore):
        def tombstone(self, *, attachment_id: str, thread_id: str) -> str | None:
            raise AttachmentStoreError("durable store unavailable")

    rejecting = AttachmentPreparationService(
        store=_RejectingStore(fresh_store["store"]),
        objects=fresh_store["objects"],
    )
    http_app.dependency_overrides[get_attachment_service] = lambda: rejecting

    response = _delete_attachment(client, thread_id, aid)

    assert response.status_code == 500
    assert response.json() == {"error": "attachment_store_failed"}


# ----- the same locator guard on the other two {attachment_id} doors -----


@pytest.mark.parametrize(
    "attachment_id", _MALFORMED_LOCATORS, ids=_MALFORMED_LOCATOR_IDS
)
def test_status_door_refuses_a_malformed_attachment_id_as_a_typed_validation_error(
    client: TestClient,
    fresh_store,
    monkeypatch: pytest.MonkeyPatch,
    attachment_id: str,
) -> None:
    """The status door refuses garbage before the store sees it, as DELETE does.

    A malformed path segment is a client mistake, not an identity miss, so it
    must not become a query and must not borrow ``attachment_not_found`` — the
    answer the same door gives a well-formed id that no thread owns.
    """
    thread_id = _new_thread(client)
    stored = _attach(client, thread_id, ATTACH_SENTINEL.encode("utf-8")).json()
    handed_to_store = _record_store_lookups(fresh_store, monkeypatch)

    response = _attachment_status(client, thread_id, attachment_id)

    assert response.status_code == 400, response.text
    assert response.json() == {"error": "invalid_locator"}
    assert handed_to_store == []
    assert fresh_store["attachments"].store.get(stored["attachment_id"]) is not None


@pytest.mark.parametrize(
    "attachment_id", _MALFORMED_LOCATORS, ids=_MALFORMED_LOCATOR_IDS
)
def test_retry_door_refuses_a_malformed_attachment_id_as_a_typed_validation_error(
    client: TestClient,
    fresh_store,
    monkeypatch: pytest.MonkeyPatch,
    attachment_id: str,
) -> None:
    """The retry door refuses garbage before the store sees it, as DELETE does.

    Retry drives preparation, so a segment the store can never match must be
    refused at the door — not looked up, not prepared, and not answered with
    the store-level code an identity miss earns.
    """
    thread_id = _new_thread(client)
    stored = _attach(client, thread_id, ATTACH_SENTINEL.encode("utf-8")).json()
    handed_to_store = _record_store_lookups(fresh_store, monkeypatch)

    response = _retry_attachment(client, thread_id, attachment_id)

    assert response.status_code == 400, response.text
    assert response.json() == {"error": "invalid_locator"}
    assert handed_to_store == []
    untouched = fresh_store["attachments"].store.get(stored["attachment_id"])
    assert untouched is not None
    assert untouched.status == "uploaded"


def test_the_two_lookup_doors_still_name_a_wellformed_identity_miss(
    client: TestClient, fresh_store
) -> None:
    """The guard must not steal the identity miss's own named answer.

    A plausible id that no thread owns is a different mistake from garbage
    text, and both doors keep the code they had before the guard spread: 400
    ``attachment_not_found`` for an unknown id, 404 ``thread_not_found`` for an
    unknown thread.
    """
    thread_id = _new_thread(client)
    unknown_id = _attachment_status(client, thread_id, "att-never-uploaded")
    unknown_thread = _attachment_status(client, f"no-such-{thread_id}", "att-0k")

    assert (unknown_id.status_code, unknown_id.json()) == (
        400,
        {"error": "attachment_not_found"},
    )
    assert (unknown_thread.status_code, unknown_thread.json()) == (
        404,
        {"error": "thread_not_found"},
    )


def test_the_shipped_attachment_delete_path_is_the_one_the_chip_calls(
    client: TestClient,
) -> None:
    """Host route and web request must name one URL.

    ``web/src/api.ts`` has no router to consult, so this is where the two
    halves of the same door are pinned together: a template change on either
    side would otherwise leave the chip's delete calling a path that answers
    404.

    The template lives in ``sourceLocator.ts`` because the visual-claim lane
    resolves the same attachment URL. The pin therefore follows the string to
    its helper AND asserts that ``api.ts`` still reaches the door through
    that helper — delegation is the property worth pinning, since an inline
    copy here is exactly the drift that would fork the two lanes.
    """
    from fin.host.workspace_http import _attachment_delete_path

    web_src = Path(__file__).resolve().parents[2] / "web" / "src"
    locator_source = (web_src / "sourceLocator.ts").read_text(encoding="utf-8")
    helper = locator_source.split(
        "export function threadAttachmentUrl", 1
    )[1].split("\nexport ", 1)[0]
    assert "/api/threads/${encodeURIComponent(threadId)}/attachments/" in helper
    assert "encodeURIComponent(attachmentId)" in helper

    api_source = (web_src / "api.ts").read_text(encoding="utf-8")
    request = api_source.split(
        "export async function deleteThreadAttachment", 1
    )[1].split("\nexport ", 1)[0]
    assert "threadAttachmentUrl(threadId, attachmentId)" in request
    assert 'method: "DELETE"' in request
    assert "/retry" not in request

    shipped = "/api/threads/no-such-thread/attachments/no-such-attachment"
    assert _attachment_delete_path("no-such-thread", "no-such-attachment") == shipped

    missing_thread = client.request("DELETE", shipped)
    assert missing_thread.status_code == 404
    assert missing_thread.json() == {"error": "thread_not_found"}
