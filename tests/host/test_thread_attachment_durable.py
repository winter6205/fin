"""A text attachment is durable for the research thread lifetime.

Replaces the turn-blob contract (single-use, process-local dict) with the
thread-owned durable attachment of ADRs 0083/0085/0086/0088: ``parsing``
on acceptance, ``ready`` only once the derived index is searchable,
restart keeps bytes and the parsed version, and deleting the thread
removes metadata, bytes, parsed units, and index entries.

"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.object_store_errors import UnknownObjectKeyError
from fin.host.attachment_prepare import AttachmentPreparationService
from fin.host.attachment_store import (
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.http import get_attachment_service, get_graph, get_thread_store
from fin.host.scripted_model import ScriptedChatModel
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve import ExtractedTreeReadPort

AAPL_2022Q3 = "AAPL-2022Q3"
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)

REVENUE_TEXT = (
    "Q3 revenue was 89,493 million dollars.\n"
    "Operating cash flow stayed strong across the quarter.\n"
)


class GateObjectStore:
    """Delegates to a real object store; the test flips the fault gates."""

    def __init__(self, inner: InMemoryObjectStore) -> None:
        self.inner = inner
        self.get_fails = False
        self.delete_fails = False

    def put(self, key: str, payload: bytes, *, content_type: str | None = None):
        return self.inner.put(key, payload, content_type=content_type)

    def get(self, key: str) -> bytes:
        if self.get_fails:
            raise RuntimeError("object store unreachable")
        return self.inner.get(key)

    def get_pointer(self, key: str):
        return self.inner.get_pointer(key)

    def delete(self, key: str) -> None:
        if self.delete_fails:
            raise RuntimeError("object store unreachable")
        self.inner.delete(key)


# ----- fixtures -----


def _compile_graph(model: ScriptedChatModel) -> Any:
    from fin.host import compile_graph

    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def threads() -> InMemoryThreadStore:
    return InMemoryThreadStore()


@pytest.fixture
def objects() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def gate(objects: InMemoryObjectStore) -> GateObjectStore:
    return GateObjectStore(objects)


@pytest.fixture
def service(
    threads: InMemoryThreadStore, gate: GateObjectStore
) -> AttachmentPreparationService:
    return AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads), objects=gate
    )


@pytest.fixture
def wired(
    http_app: FastAPI,
    threads: InMemoryThreadStore,
    service: AttachmentPreparationService,
) -> dict[str, Any]:
    thread = threads.create_thread(title="durable thread")
    http_app.dependency_overrides[get_thread_store] = lambda: threads
    http_app.dependency_overrides[get_attachment_service] = lambda: service
    http_app.dependency_overrides[get_graph] = lambda: _compile_graph(
        ScriptedChatModel(script=[AIMessage(content="ok")] * 6)
    )
    return {"threads": threads, "thread_id": thread.id, "service": service}


def _attach(client: TestClient, thread_id: str, payload: bytes) -> dict:
    response = client.post(
        f"/api/threads/{thread_id}/attachments",
        params={"filename": "notes.txt"},
        content=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _status(client: TestClient, thread_id: str, attachment_id: str) -> Any:
    return client.get(f"/api/threads/{thread_id}/attachments/{attachment_id}")


# ----- parsing -> ready, searchable across turns -----


def test_upload_returns_parsing_and_becomes_ready_with_searchable_index(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    assert body["status"] == "parsing"
    assert body["scope"] == "thread_attach"
    assert body["attachment_id"]

    response = _status(client, wired["thread_id"], body["attachment_id"])
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "ready"
    assert record["searchable"] is True
    assert record["unit_count"] >= 1
    assert record["version"] >= 1
    assert record["filename"] == "notes.txt"


def test_an_unprepared_attachment_never_reads_as_ready(
    client: TestClient, wired: dict[str, Any]
) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="pending.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    body = _status(client, wired["thread_id"], record.attachment_id).json()
    assert body["status"] == "parsing"
    assert body["searchable"] is False


def test_thread_lists_its_own_attachments(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    listing = client.get(f"/api/threads/{wired['thread_id']}/attachments")
    assert listing.status_code == 200, listing.text
    assert [row["attachment_id"] for row in listing.json()["attachments"]] == [
        body["attachment_id"]
    ]


def test_a_later_turn_can_reference_the_same_attachment_again(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]

    for turn in range(2):
        response = client.post(
            f"/api/threads/{wired['thread_id']}/messages",
            json={
                "question": f"what did revenue say ({turn})?",
                "attachment_ids": [aid],
            },
        )
        assert response.status_code == 200, response.text
    assert _status(client, wired["thread_id"], aid).json()["status"] == "ready"


def test_send_naming_a_parsing_attachment_is_typed_not_ready(
    client: TestClient, wired: dict[str, Any]
) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="pending.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    response = client.post(
        f"/api/threads/{wired['thread_id']}/messages",
        json={"question": "read it", "attachment_ids": [record.attachment_id]},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_ready"}


# ----- thread isolation -----


def test_another_thread_cannot_read_or_reference_the_attachment(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    other = wired["threads"].create_thread(title="other")

    leak = _status(client, other.id, body["attachment_id"])
    assert leak.status_code == 400
    assert leak.json() == {"error": "attachment_not_found"}

    send = client.post(
        f"/api/threads/{other.id}/messages",
        json={"question": "hi", "attachment_ids": [body["attachment_id"]]},
    )
    assert send.status_code == 400
    assert send.json() == {"error": "attachment_not_found"}


# ----- restart -----


def test_restart_keeps_bytes_and_version_and_restores_index_before_ready(
    wired: dict[str, Any], threads: InMemoryThreadStore, objects: InMemoryObjectStore
) -> None:
    store = wired["service"].store
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    prepared = wired["service"].prepare(record.attachment_id)
    assert prepared.status == "ready"
    object_key = prepared.object_key

    store.clear_index(record.attachment_id)
    restarted = AttachmentPreparationService(store=store, objects=objects)
    assert restarted.status(record.attachment_id).status == "parsing"

    assert restarted.recover() == 1
    restored = restarted.status(record.attachment_id)
    assert restored.status == "ready"
    assert restored.version == prepared.version
    assert restored.object_key == object_key
    assert restarted.units(record.attachment_id) == (
        ParsedAttachmentUnit(unit=1, text=REVENUE_TEXT),
    )
    assert objects.get(object_key) == REVENUE_TEXT.encode("utf-8")


def test_recover_of_completed_work_is_a_no_op(wired: dict[str, Any]) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    wired["service"].prepare(record.attachment_id)
    restarted = AttachmentPreparationService(
        store=wired["service"].store, objects=wired["service"].objects
    )
    assert restarted.recover() == 0
    assert restarted.status(record.attachment_id).status == "ready"


def test_restart_rebuilds_lost_units_from_retained_bytes(
    wired: dict[str, Any], threads: InMemoryThreadStore, objects: InMemoryObjectStore
) -> None:
    """A crash before the unit write rebuilds parsed text from the bytes."""
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    wired["service"].prepare(record.attachment_id)
    wired["service"].store.clear_units(record.attachment_id)
    wired["service"].store.clear_index(record.attachment_id)

    restarted = AttachmentPreparationService(
        store=wired["service"].store, objects=objects
    )
    assert restarted.recover() == 1
    assert restarted.status(record.attachment_id).status == "ready"
    assert restarted.units(record.attachment_id) == (
        ParsedAttachmentUnit(unit=1, text=REVENUE_TEXT),
    )


# ----- thread deletion cascades -----


def test_deleting_the_thread_removes_metadata_bytes_units_and_index(
    client: TestClient,
    wired: dict[str, Any],
    objects: InMemoryObjectStore,
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    object_key = wired["service"].status(aid).object_key
    assert wired["service"].units(aid)

    deleted = client.delete(f"/api/threads/{wired['thread_id']}")
    assert deleted.status_code == 200, deleted.text

    assert wired["service"].status(aid) is None
    assert wired["service"].units(aid) == ()
    assert wired["service"].store.indexed_versions(aid) == ()
    with pytest.raises(UnknownObjectKeyError):
        objects.get(object_key)


def test_deleted_attachment_id_yields_unknown_locator_on_a_new_thread(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    assert client.delete(f"/api/threads/{wired['thread_id']}").status_code == 200

    fresh = wired["threads"].create_thread(title="next")
    response = client.post(
        f"/api/threads/{fresh.id}/messages",
        json={"question": "hi", "attachment_ids": [aid]},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_found"}


def test_cleanup_failure_is_typed_observable_and_retriable(
    client: TestClient,
    wired: dict[str, Any],
    gate: GateObjectStore,
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    gate.delete_fails = True

    blocked = client.delete(f"/api/threads/{wired['thread_id']}")
    assert blocked.status_code == 500
    assert blocked.json() == {"error": "attachment_cleanup_failed"}
    assert wired["service"].status(aid) is not None
    assert wired["threads"].get_thread(wired["thread_id"]) is not None

    gate.delete_fails = False
    retried = client.delete(f"/api/threads/{wired['thread_id']}")
    assert retried.status_code == 200, retried.text
    assert wired["service"].status(aid) is None


# ----- delete racing preparation -----


def test_delete_during_preparation_cannot_resurrect_the_index(
    wired: dict[str, Any], threads: InMemoryThreadStore, objects: InMemoryObjectStore
) -> None:
    release = threading.Event()
    entered = threading.Event()

    class BlockingGet(GateObjectStore):
        def get(self, key: str) -> bytes:
            entered.set()
            assert release.wait(timeout=5)
            return self.inner.get(key)

    store = InMemoryAttachmentStore(threads)
    racer = AttachmentPreparationService(store=store, objects=BlockingGet(objects))
    record = racer.accept(
        thread_id=wired["thread_id"],
        filename="race.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )

    outcome: list[Any] = []
    worker = threading.Thread(
        target=lambda: outcome.append(_attempt(racer.prepare, record.attachment_id)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=5)
    racer.delete_thread(wired["thread_id"])
    release.set()
    worker.join(timeout=5)

    assert outcome and isinstance(outcome[0], AttachmentThreadGoneError)
    assert store.get(record.attachment_id) is None
    with pytest.raises(UnknownObjectKeyError):
        objects.get(record.object_key)


def _attempt(call: Any, argument: str) -> Any:
    try:
        return call(argument)
    except Exception as exc:  # noqa: BLE001 - records the typed race outcome
        return exc


# ----- input classes at the public door -----


def test_empty_body_is_rejected(client: TestClient, wired: dict[str, Any]) -> None:
    response = client.post(
        f"/api/threads/{wired['thread_id']}/attachments", content=b""
    )
    assert response.status_code == 400
    assert response.json() == {"error": "empty_attachment"}


def test_non_utf8_body_is_rejected(
    client: TestClient, wired: dict[str, Any]
) -> None:
    response = client.post(
        f"/api/threads/{wired['thread_id']}/attachments",
        content=b"\xff\xfe\x00\x01",
    )
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_text"}


def test_unknown_thread_upload_is_404(
    client: TestClient, wired: dict[str, Any]
) -> None:
    response = client.post(
        f"/api/threads/{uuid.uuid4()}/attachments",
        params={"filename": "x.txt"},
        content=b"hello",
    )
    assert response.status_code == 404
    assert response.json() == {"error": "thread_not_found"}


def test_unknown_attachment_status_is_typed(
    client: TestClient, wired: dict[str, Any]
) -> None:
    response = _status(client, wired["thread_id"], "nope-404")
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_found"}


def test_whitespace_attachment_fails_with_a_named_reason(
    wired: dict[str, Any]
) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"], filename="blank.txt", payload=b"   \n "
    )
    failed = wired["service"].prepare(record.attachment_id)
    assert failed.status == "failed"
    assert failed.failure_reason == "attachment_empty_text"


def test_object_store_outage_during_preparation_is_typed(
    wired: dict[str, Any], gate: GateObjectStore
) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    gate.get_fails = True
    failed = wired["service"].prepare(record.attachment_id)
    assert failed.status == "failed"
    assert failed.failure_reason == "source_unavailable"


def test_failed_preparation_retries_to_ready(
    wired: dict[str, Any], gate: GateObjectStore, client: TestClient
) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    gate.get_fails = True
    assert wired["service"].prepare(record.attachment_id).status == "failed"
    gate.get_fails = False

    response = client.post(
        f"/api/threads/{wired['thread_id']}/attachments/{record.attachment_id}/retry"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    assert wired["service"].units(record.attachment_id) == (
        ParsedAttachmentUnit(unit=1, text=REVENUE_TEXT),
    )


def test_concurrent_retry_keeps_one_active_index_version(
    wired: dict[str, Any]
) -> None:
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    results = [_attempt(wired["service"].prepare, record.attachment_id)]
    threads = [
        threading.Thread(
            target=lambda: results.append(
                _attempt(wired["service"].prepare, record.attachment_id)
            )
        )
        for _ in range(3)
    ]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(timeout=10)

    assert len(results) == 4
    final = wired["service"].status(record.attachment_id)
    assert final.status == "ready"
    assert wired["service"].store.indexed_versions(record.attachment_id) == (
        final.version,
    )
