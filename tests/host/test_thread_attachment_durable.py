"""A text attachment is durable for the research thread lifetime.

Replaces the turn-blob contract (single-use, process-local dict) with the
thread-owned durable attachment of ADRs 0083/0085/0086/0088/0089:
``uploaded`` on acceptance (raw bytes only, no derived work), ``ready``
only once a requested preparation has frozen units and a searchable
index, restart keeps bytes and the parsed version, and deleting removes
metadata, parsed units, index entries and finally the bytes — one attachment
at a time or a whole thread at once, always in that order, so a refusal that
leaves bytes behind leaves a tombstone behind too.

The order itself is pinned where only it can be seen: the byte store reports
what the durable record looked like at the instant it was called, and the
races and that report both run a second time against real Postgres rows on
two connections when ``FIN_CHECKPOINT_URL`` names a reachable server.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.object_store_errors import UnknownObjectKeyError
from fin.host.attachment_budgets import AttachmentBudgets
from fin.host.attachment_index import search
from fin.host.attachment_prepare import (
    AttachmentCleanupError,
    AttachmentNotFoundError,
    AttachmentNotReadyError,
    AttachmentPreparationService,
)
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
    PostgresAttachmentStore,
)
from fin.host.http import (
    get_attachment_budgets,
    get_attachment_service,
    get_graph,
    get_thread_store,
)
from fin.host.run_control import RunControl
from fin.host.scripted_model import ScriptedChatModel
from fin.host.thread_store import InMemoryThreadStore, PostgresThreadStore
from fin.host.workspace_http import _resume_one_attachment_wait
from fin.retrieve import ExtractedTreeReadPort

AAPL_2022Q3 = "AAPL-2022Q3"
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)

# The repo's gate for every Postgres-backed case: unset means the durable
# half of these promises is not exercised on this machine at all.
POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


REVENUE_TEXT = (
    "Q3 revenue was 89,493 million dollars.\n"
    "Operating cash flow stayed strong across the quarter.\n"
)

# The door reads its byte cap through this dependency, so every wired
# test app pins the deployed budget instead of reading the operator's
# process environment.
BUDGETS = AttachmentBudgets(
    max_upload_bytes=1_048_576,
    max_pdf_pages=8,
    max_vision_calls_per_run=16,
    max_vision_crop_calls_per_run=8,
)

# Grounded by the Soft-Track rules tier (mirrors the T2f suites in
# tests/host/test_attachment_send_wait.py): only a grounded attachment
# turn reaches the graph's preparation gate, so a send test that must
# show the durable pause has to ask a grounded question.
GROUNDED_QUESTION = "retrieve a summary of the attachment"


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


class ByteTouchSpy(GateObjectStore):
    """Byte store that reports the durable state while each delete is in flight.

    WHY the report is taken inside :meth:`delete`: once the door has returned,
    void-then-delete and delete-then-void leave exactly the same stored state,
    so no later read can tell the two apart. The byte store is the one caller
    that runs at the moment the first byte is touched, and what it sees then
    is either an already-voided attachment or a live one.
    """

    def __init__(self, inner: InMemoryObjectStore) -> None:
        super().__init__(inner)
        self.snapshots: list[dict[str, Any]] = []
        self.touched: list[str] = []
        self.report: Callable[[str, list[str]], dict[str, Any]] | None = None

    def delete(self, key: str) -> None:
        if self.report is not None:
            self.snapshots.append({"key": key, **self.report(key, list(self.touched))})
        super().delete(key)
        self.touched.append(key)


def _void_report(
    reader: Any,
    *,
    thread_id: str,
    attachments: Sequence[tuple[str, str, int]],
) -> Callable[[str, list[str]], dict[str, Any]]:
    """Report what ``reader`` can still reach while a byte is being deleted.

    ``attachments`` names every attachment the running deletion is on the hook
    for — id, object key, published version — so one report also shows whether
    a *sibling* of the key being removed was still readable at that instant.
    ``reader`` is a separate view of the durable state: another store instance
    and therefore another connection against Postgres, never the writer's own
    uncommitted transaction.
    """

    def report(key: str, touched: list[str]) -> dict[str, Any]:
        return {
            "already_removed": touched,
            "live_rows": [
                attachment_id
                for attachment_id, _, _ in attachments
                if reader.get(attachment_id) is not None
            ],
            "owed_keys": sorted(reader.pending_cleanup(thread_id)),
            "derived_left": [
                attachment_id
                for attachment_id, _, version in attachments
                if reader.units(attachment_id, version)
                or reader.indexed_versions(attachment_id)
            ],
        }

    return report


def _voided_once(object_key: str, owed: Sequence[str]) -> dict[str, Any]:
    """The one report a byte delete is allowed to produce: nothing reachable."""
    return {
        "key": object_key,
        "already_removed": [],
        "live_rows": [],
        "owed_keys": sorted(owed),
        "derived_left": [],
    }


# ----- durable backends -----


def _postgres_url_or_skip() -> str:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the durable half of the "
            "attachment delete order and its races is untested here"
        )
    return url


def _postgres_backend(url: str) -> dict[str, Any]:
    """A real thread row plus a factory of independent attachment sessions.

    Each store built here opens its own connection per call, so two of them
    are two server sessions: what one commits is what the other can read, and
    nothing else. The byte layer stays in-process, as it does in the durable
    Postgres suite — the S3 adapter's own contract lives in ``tests/corpus``.
    """
    threads = PostgresThreadStore(url)
    thread = threads.create_thread(title="attachment race")
    return {
        "backend": "postgres",
        "url": url,
        "threads": threads,
        "objects": InMemoryObjectStore(),
        "thread_id": thread.id,
        "new_store": lambda: PostgresAttachmentStore(url),
    }


def _drop_thread(url: str, thread_id: str) -> None:
    """Remove one test's thread row; its attachments cascade away with it.

    WHY not the whole table: several suites share this server, and a global
    delete would let one run decide another's row counts.
    """
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM fin_thread WHERE id = %s", (thread_id,))
        conn.commit()


def _blocked_writer_sessions(url: str, *, holder_pid: int) -> int:
    """How many sessions are waiting for a lock this very session is holding.

    WHY the wait chain instead of a lock-wait alone: the claim under test is
    that the delete running on one connection is the one parked behind the
    row this test holds on another, so the two ends of the wait have to be
    identified. A count of sessions named ``Lock`` could be answered by any
    other work sharing the server.
    """
    import psycopg

    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity waiting "
            "JOIN pg_locks wanted "
            "ON wanted.pid = waiting.pid AND NOT wanted.granted "
            "JOIN pg_locks held "
            "ON held.pid = %s AND held.granted "
            "AND held.locktype = wanted.locktype "
            "AND held.database IS NOT DISTINCT FROM wanted.database "
            "AND held.relation IS NOT DISTINCT FROM wanted.relation "
            "AND held.page IS NOT DISTINCT FROM wanted.page "
            "AND held.tuple IS NOT DISTINCT FROM wanted.tuple "
            "WHERE waiting.datname = current_database() "
            "AND waiting.pid <> pg_backend_pid() AND waiting.pid <> %s",
            (holder_pid, holder_pid),
        ).fetchone()
    return int(row[0])


def _wait_until_blocked(
    url: str, *, holder_pid: int, timeout: float = 10.0
) -> bool:
    """Poll :func:`_blocked_writer_sessions` until it names a blocked writer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _blocked_writer_sessions(url, holder_pid=holder_pid) >= 1:
            return True
        time.sleep(0.05)
    return False


_BLOCKED_WRITER_PID_SQL = (
    "SELECT waiting.pid FROM pg_stat_activity waiting "
    "JOIN pg_locks wanted "
    "ON wanted.pid = waiting.pid AND NOT wanted.granted "
    "JOIN pg_locks held "
    "ON held.pid = %s AND held.granted "
    "AND held.locktype = wanted.locktype "
    "AND held.database IS NOT DISTINCT FROM wanted.database "
    "AND held.relation IS NOT DISTINCT FROM wanted.relation "
    "AND held.page IS NOT DISTINCT FROM wanted.page "
    "AND held.tuple IS NOT DISTINCT FROM wanted.tuple "
    "WHERE waiting.datname = current_database() "
    "AND waiting.pid <> pg_backend_pid() AND waiting.pid <> %s "
    "LIMIT 1"
)


def _blocked_writer_pid(
    url: str, *, holder_pid: int, timeout: float = 10.0
) -> int | None:
    """The pid of the session waiting on a lock this session holds.

    WHY the count alone is not enough: it says something waits, not which
    statement the waiter had reached. The claim under test is about a
    specific session's tombstone write, and that observation needs the
    waiter named.
    """
    import psycopg

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                _BLOCKED_WRITER_PID_SQL, (holder_pid, holder_pid)
            ).fetchone()
        if row is not None:
            return int(row[0])
        time.sleep(0.05)
    return None


def _tombstone_write_started(url: str, *, pid: int) -> bool:
    """Whether that session has begun writing tombstone rows at all.

    WHY a lock and not a row read: an uncommitted INSERT is invisible to
    every other session's SELECT, so row visibility cannot falsify "the
    tombstone write had not been reached" — a void that wrote first and
    parked afterwards would hide its write completely. An INSERT takes a
    granted relation lock on the tombstone table for the life of its
    transaction, so the write shows as started the moment it starts,
    committed or not.
    """
    import psycopg

    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_locks "
            "WHERE pid = %s AND granted AND locktype = 'relation' "
            "AND relation = 'fin_thread_attachment_tombstone'::regclass",
            (pid,),
        ).fetchone()
    return int(row[0]) > 0


@pytest.fixture
def postgres_url() -> str:
    """The DSN the Postgres-backed cases in this file share."""
    return _postgres_url_or_skip()


@pytest.fixture(params=["memory", "postgres"])
def race_stack(request) -> Iterator[dict[str, Any]]:
    """One live thread and one byte store, per durable attachment backend.

    The races and the void-before-any-byte order are the part of the contract
    a single process's dictionary cannot show, so the ``postgres`` parameter
    hands every caller its own store — its own connection and its own
    committed view. Opt-in by the repo's convention: with no reachable
    ``FIN_CHECKPOINT_URL`` that parameter skips and only the in-memory shape
    runs, which is exactly the evidence the durable half would be missing.
    """
    if request.param == "memory":
        threads = InMemoryThreadStore()
        thread = threads.create_thread(title="attachment race")
        shared = InMemoryAttachmentStore(threads)
        yield {
            "backend": "memory",
            "url": "",
            "threads": threads,
            "objects": InMemoryObjectStore(),
            "thread_id": thread.id,
            # In-memory durable state IS one dict, so every caller and every
            # reader of it is the same object by construction.
            "new_store": lambda: shared,
        }
        return

    url = _postgres_url_or_skip()
    stack = _postgres_backend(url)
    try:
        yield stack
    finally:
        _drop_thread(url, stack["thread_id"])


# ----- fixtures -----


def _compile_graph(
    model: ScriptedChatModel,
    *,
    attachment_service: AttachmentPreparationService | None = None,
) -> Any:
    from fin.host import compile_graph

    kwargs: dict[str, Any] = {
        "checkpointer": InMemorySaver(),
        "model": model,
        "known_document_ids": KNOWN_IDS,
        "read_port": _TREE_READ_PORT,
    }
    # WHY optional: the gate node only exists in the topology when an
    # attachment service is injected (the production composition root
    # always injects one). Durability tests that never pause keep the
    # plain graph; the T2f pause tests compile through this same seam.
    if attachment_service is not None:
        kwargs["attachment_service"] = attachment_service
    return compile_graph(**kwargs)


def _gate_wired_graph(
    service: AttachmentPreparationService,
) -> Any:
    """One compiled gate-wired graph instance.

    WHY a stable instance: the durable pause lives on THIS checkpoint, so
    every request and the shared resume must drive the same graph; the
    same shape is what the production app builds once at startup.
    """
    return _compile_graph(
        ScriptedChatModel(script=[AIMessage(content="ok")] * 6),
        attachment_service=service,
    )


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    """SSE frames as ``(event, data)`` pairs, as the T2f suites read them."""
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
    http_app.dependency_overrides[get_attachment_budgets] = lambda: BUDGETS
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


def _retry(
    client: TestClient, thread_id: str, attachment_id: str
) -> dict:
    """Post the retry door and hand back its immediate wire record."""
    response = client.post(
        f"/api/threads/{thread_id}/attachments/{attachment_id}/retry"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _prepare_through_door(
    client: TestClient,
    service: AttachmentPreparationService,
    thread_id: str,
    attachment_id: str,
) -> dict:
    """Walk the public door to a SETTLED preparation, as an operator would.

    T2f changed the door's semantics: it only asks. The retry door answers
    immediately with the queued promise — ``parsing``, nothing searchable —
    and the work itself runs in the durable job it enqueues, so a test
    that needs published text drains that job (what the prep worker does)
    and reads the settled state back through the status door.
    """
    asked = _retry(client, thread_id, attachment_id)
    assert asked["status"] == "parsing", asked
    assert asked["searchable"] is False, asked
    service.drain_attachment_jobs()
    response = _status(client, thread_id, attachment_id)
    assert response.status_code == 200, response.text
    return response.json()


def _status(client: TestClient, thread_id: str, attachment_id: str) -> Any:
    return client.get(f"/api/threads/{thread_id}/attachments/{attachment_id}")


# ----- uploaded -> ready, searchable across turns -----


def test_upload_returns_uploaded_and_becomes_ready_with_searchable_index(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    assert body["status"] == "uploaded"
    assert body["scope"] == "thread_attach"
    assert body["attachment_id"]

    # T2f pin of the door's async shape: the retry answers immediately
    # with the queued promise -- parsing, version 0, exactly one durable
    # job -- and only the drain publishes. The old ready-inside-the-
    # request is gone and must not come back.
    asked = _retry(client, wired["thread_id"], body["attachment_id"])
    assert asked["status"] == "parsing"
    assert asked["version"] == 0
    assert asked["searchable"] is False
    assert len(wired["service"].jobs.incomplete()) == 1
    assert wired["service"].drain_attachment_jobs() == 1
    assert wired["service"].jobs.incomplete() == ()

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
    assert body["status"] == "uploaded"
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
    assert _prepare_through_door(
        client, wired["service"], wired["thread_id"], aid
    )["status"] == "ready"

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


def test_send_naming_an_uploaded_attachment_is_accepted_and_pauses(
    client: TestClient, http_app: FastAPI, wired: dict[str, Any]
) -> None:
    """T2f re-pin of the old 400: a stored ``uploaded`` row named in a
    grounded send is ACCEPTED, and the run parks on the durable gate —
    the wire closes on ``attachment_wait``, the row left ``uploaded``
    for ``parsing``, and exactly one job carries the promise."""
    graph = _gate_wired_graph(wired["service"])
    http_app.dependency_overrides[get_graph] = lambda: graph

    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="pending.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    response = client.post(
        f"/api/threads/{wired['thread_id']}/messages",
        json={
            "question": GROUNDED_QUESTION,
            "attachment_ids": [record.attachment_id],
        },
    )
    assert response.status_code == 200, response.text

    events = _parse_sse_events(response.text)
    waits = [data for name, data in events if name == "attachment_wait"]
    assert waits, f"no attachment_wait frame on the wire: {[n for n, _ in events]}"
    assert waits[-1]["kind"] == "attachment_preparation"
    assert waits[-1]["attachment_ids"] == [record.attachment_id]
    # The gate asked for the work through the durable job, not in the
    # request, and a pause persists no turn row: the run waits, it did
    # not answer.
    assert wired["service"].status(record.attachment_id).status == "parsing"
    assert len(wired["service"].jobs.incomplete()) == 1
    assert wired["threads"].list_runs(wired["thread_id"]) == []


def test_send_naming_a_failed_attachment_is_still_typed_not_ready(
    client: TestClient, http_app: FastAPI, wired: dict[str, Any]
) -> None:
    """The half of the old refusal the T2f contract keeps: a named
    ``failed`` row in a fresh send is still the typed 400 — a repeated
    turn must not re-spend a named failure; only the retry door re-asks."""
    graph = _gate_wired_graph(wired["service"])
    http_app.dependency_overrides[get_graph] = lambda: graph

    record = wired["service"].accept(
        thread_id=wired["thread_id"], filename="blank.txt", payload=b"   \n "
    )
    assert wired["service"].prepare(record.attachment_id).status == "failed"

    response = client.post(
        f"/api/threads/{wired['thread_id']}/messages",
        json={
            "question": GROUNDED_QUESTION,
            "attachment_ids": [record.attachment_id],
        },
    )
    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_ready"}
    # The refusal consumed nothing: the row keeps its named failure, no
    # job was queued by the turn, and no run was started.
    assert wired["service"].status(record.attachment_id).status == "failed"
    assert wired["service"].jobs.incomplete() == ()
    assert wired["threads"].list_runs(wired["thread_id"]) == []


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


def test_a_restart_never_prepares_an_upload_nobody_asked_to_prepare(
    wired: dict[str, Any], objects: InMemoryObjectStore
) -> None:
    """Restart boundary of the new lifecycle: an ``uploaded`` row holds
    durable bytes with no preparation requested yet, so recovery must
    leave it exactly as it was — nothing parsed, nothing indexed, no
    work started behind the operator.
    """
    restarted = AttachmentPreparationService(
        store=wired["service"].store, objects=objects
    )
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )

    assert restarted.recover() == 0
    after = restarted.status(record.attachment_id)
    assert after.status == "uploaded"
    assert after.version == 0
    assert restarted.units(record.attachment_id) == ()
    assert restarted.store.indexed_versions(record.attachment_id) == ()
    assert objects.get(record.object_key) == REVENUE_TEXT.encode("utf-8")


# ----- thread deletion cascades -----


def test_deleting_the_thread_removes_metadata_bytes_units_and_index(
    client: TestClient,
    wired: dict[str, Any],
    objects: InMemoryObjectStore,
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    prepared = _prepare_through_door(
        client, wired["service"], wired["thread_id"], aid
    )
    # WHY the re-read: the deletion promise below only means anything if
    # units and postings actually exist first -- the drain must have
    # published before the door is asked to remove layers.
    assert prepared["status"] == "ready" and prepared["searchable"] is True
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


def test_thread_deletion_tombstones_before_cleanup_and_stays_retriable(
    client: TestClient,
    wired: dict[str, Any],
    gate: GateObjectStore,
    objects: InMemoryObjectStore,
) -> None:
    """A thread delete blocks access first, so a failed cleanup is retryable.

    Replaces the earlier assertion that a failed cleanup left the attachment
    readable: the tombstone rule applies to thread deletion too, so access is
    voided before any byte removal is attempted while the obligation to
    finish the cleanup is retained.
    """
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    object_key = wired["service"].status(aid).object_key
    gate.delete_fails = True

    blocked = client.delete(f"/api/threads/{wired['thread_id']}")

    assert blocked.status_code == 500
    assert blocked.json() == {"error": "attachment_cleanup_failed"}
    assert wired["service"].status(aid) is None
    assert wired["service"].units(aid) == ()
    assert wired["service"].store.indexed_versions(aid) == ()
    assert wired["threads"].get_thread(wired["thread_id"]) is not None
    assert objects.get(object_key) == REVENUE_TEXT.encode("utf-8")
    assert wired["service"].store.pending_cleanup(wired["thread_id"]) == [object_key]

    gate.delete_fails = False
    retried = client.delete(f"/api/threads/{wired['thread_id']}")

    assert retried.status_code == 200, retried.text
    assert wired["threads"].get_thread(wired["thread_id"]) is None
    with pytest.raises(UnknownObjectKeyError):
        objects.get(object_key)
    assert wired["service"].store.pending_cleanup(wired["thread_id"]) == []


# ----- single-attachment deletion -----


def _delete_one(
    client: TestClient, thread_id: str, attachment_id: str
) -> Any:
    from fin.host.workspace_http import _attachment_delete_path

    return client.request("DELETE", _attachment_delete_path(thread_id, attachment_id))


def test_deleting_one_attachment_voids_access_then_removes_its_layers(
    client: TestClient,
    wired: dict[str, Any],
    objects: InMemoryObjectStore,
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    sibling = _attach(client, wired["thread_id"], "sibling notes".encode("utf-8"))
    object_key = wired["service"].status(aid).object_key

    deleted = _delete_one(client, wired["thread_id"], aid)

    assert deleted.status_code == 200, deleted.text
    assert wired["service"].status(aid) is None
    assert _status(client, wired["thread_id"], aid).status_code == 400
    assert wired["service"].units(aid) == ()
    assert wired["service"].store.index_postings(aid, 1) == {}
    assert wired["service"].store.indexed_versions(aid) == ()
    assert wired["service"].store.pending_cleanup(wired["thread_id"]) == []
    with pytest.raises(UnknownObjectKeyError):
        objects.get(object_key)
    # Only the named attachment is voided.
    assert [
        record.attachment_id for record in wired["service"].list_for_thread(
            wired["thread_id"]
        )
    ] == [sibling["attachment_id"]]


# ----- the promised order, read while the byte store is being touched -----


def test_the_delete_door_touches_no_byte_before_the_void_is_durable(
    client: TestClient,
    http_app: FastAPI,
    wired: dict[str, Any],
    objects: InMemoryObjectStore,
) -> None:
    """The door's own success path is the one that has to keep the order.

    A read taken after the response cannot tell void-then-delete from
    delete-then-void: both leave no row, no units, no postings, and no bytes.
    So the byte store reports what the durable record said at the instant it
    was asked to remove the key — access unreadable and the obligation already
    written — and that report, not the aftermath, is the proof.
    """
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    store = wired["service"].store
    published = wired["service"].status(aid)
    spy = ByteTouchSpy(objects)
    spy.report = _void_report(
        store,
        thread_id=wired["thread_id"],
        attachments=((aid, published.object_key, published.version),),
    )
    http_app.dependency_overrides[get_attachment_service] = lambda: (
        AttachmentPreparationService(store=store, objects=spy)
    )

    deleted = _delete_one(client, wired["thread_id"], aid)

    assert spy.snapshots == [
        _voided_once(published.object_key, owed=(published.object_key,))
    ], spy.snapshots
    assert deleted.status_code == 200, deleted.text
    assert spy.touched == [published.object_key]


def test_the_byte_delete_finds_the_attachment_already_voided(
    race_stack: dict[str, Any],
) -> None:
    """The same order, on each durable backend, read through a second view.

    The reporter gets its own attachment store, which against Postgres is its
    own connection: what it can see while the byte delete is in flight is a
    committed tombstone, never the deleting transaction's own uncommitted view
    of the row it is about to remove.
    """
    thread_id = race_stack["thread_id"]
    store = race_stack["new_store"]()
    spy = ByteTouchSpy(race_stack["objects"])
    service = AttachmentPreparationService(store=store, objects=spy)
    record = service.accept(
        thread_id=thread_id, filename="once.txt", payload=REVENUE_TEXT.encode("utf-8")
    )
    published = service.prepare(record.attachment_id)
    assert published.status == "ready"
    spy.report = _void_report(
        race_stack["new_store"](),
        thread_id=thread_id,
        attachments=((record.attachment_id, published.object_key, published.version),),
    )

    service.delete_attachment(thread_id=thread_id, attachment_id=record.attachment_id)

    assert spy.snapshots == [
        _voided_once(published.object_key, owed=(published.object_key,))
    ], spy.snapshots
    assert spy.touched == [published.object_key]
    assert store.get(record.attachment_id) is None
    assert store.pending_cleanup(thread_id) == []


def test_a_thread_delete_voids_every_attachment_before_the_first_byte(
    race_stack: dict[str, Any],
) -> None:
    """A thread's rows all go before any of its bytes is touched.

    Two attachments make this more than a restatement of the single delete: a
    door that voided one attachment, removed its bytes, then repeated that per
    file would leave the second attachment fully readable while the first file
    was already gone — the state the thread door promises never to show.
    """
    thread_id = race_stack["thread_id"]
    store = race_stack["new_store"]()
    spy = ByteTouchSpy(race_stack["objects"])
    service = AttachmentPreparationService(store=store, objects=spy)
    published = []
    for name in ("first.txt", "second.txt"):
        record = service.accept(
            thread_id=thread_id, filename=name, payload=REVENUE_TEXT.encode("utf-8")
        )
        published.append(service.prepare(record.attachment_id))
    owed = sorted(row.object_key for row in published)
    spy.report = _void_report(
        race_stack["new_store"](),
        thread_id=thread_id,
        attachments=tuple(
            (row.attachment_id, row.object_key, row.version) for row in published
        ),
    )

    service.delete_thread(thread_id)

    assert sorted(spy.touched) == owed
    assert sorted(row["key"] for row in spy.snapshots) == owed
    for row in spy.snapshots:
        # At the moment each byte is removed, every attachment of the thread is
        # already voided and every one of its keys is already owed.
        assert row["live_rows"] == [], row
        assert row["derived_left"] == [], row
        assert row["owed_keys"] == owed, row
    assert store.pending_cleanup(thread_id) == []
    for key in owed:
        with pytest.raises(UnknownObjectKeyError):
            spy.get(key)


def test_the_void_waits_for_a_row_another_session_holds(postgres_url: str) -> None:
    """The Postgres half of the order is a lock, and the lock is load-bearing.

    While a second connection holds the attachment row, the deletion must be
    parked before its first byte touch: a byte removed over an uncommitted
    void is exactly the state the door promises it cannot produce. Once that
    session lets the row go, the same delete finishes in the promised order.
    """
    import psycopg

    stack = _postgres_backend(postgres_url)
    thread_id = stack["thread_id"]
    holder = psycopg.connect(postgres_url)
    try:
        spy = ByteTouchSpy(stack["objects"])
        service = AttachmentPreparationService(
            store=stack["new_store"](), objects=spy
        )
        record = service.accept(
            thread_id=thread_id,
            filename="held.txt",
            payload=REVENUE_TEXT.encode("utf-8"),
        )
        published = service.prepare(record.attachment_id)
        spy.report = _void_report(
            stack["new_store"](),
            thread_id=thread_id,
            attachments=(
                (record.attachment_id, published.object_key, published.version),
            ),
        )
        # Another session takes the row the void is about to write: the same
        # lock ``tombstone`` asks for.
        holder_pid = int(holder.execute("SELECT pg_backend_pid()").fetchone()[0])
        holder.execute(
            "SELECT attachment_id FROM fin_thread_attachment "
            "WHERE attachment_id = %s FOR UPDATE",
            (record.attachment_id,),
        )
        outcomes: list[Any] = []

        def parked_delete() -> None:
            try:
                service.delete_attachment(
                    thread_id=thread_id, attachment_id=record.attachment_id
                )
                outcomes.append("ok")
            except Exception as exc:  # noqa: BLE001 - the parked outcome is the point
                outcomes.append(exc)

        worker = threading.Thread(target=parked_delete, daemon=True)
        worker.start()
        assert _wait_until_blocked(postgres_url, holder_pid=holder_pid), (
            "the void did not wait for the other session's row lock"
        )
        assert outcomes == []
        assert spy.touched == [], "a byte was removed while the void was not durable"
        assert service.status(record.attachment_id) is not None

        holder.commit()  # the other session lets the row go
        worker.join(timeout=10)

        assert outcomes == ["ok"], outcomes
        assert spy.snapshots == [
            _voided_once(published.object_key, owed=(published.object_key,))
        ], spy.snapshots
        assert service.status(record.attachment_id) is None
    finally:
        holder.rollback()
        holder.close()
        _drop_thread(postgres_url, thread_id)


def test_the_parked_void_has_not_reached_its_tombstone_write(postgres_url: str) -> None:
    """The row lock, not the statement order, is what parks the tombstone write.

    The byte-waiting claim holds without this lock by itself: a void that
    skipped the row lock would still wait at its later row removal, and the
    door touches no byte until ``tombstone`` returns. What only ``FOR UPDATE``
    enforces is narrower and is the actual claim: while another session holds
    the attachment row the way a derived write holds it, the parked void must
    not even have begun its tombstone write. A void that wrote first and
    parked afterwards is invisible through row reads — its write is
    uncommitted — so the proof is read where an in-flight INSERT leaves a
    trace no MVCC read can hide: the writer's granted lock on the tombstone
    table.
    """
    import psycopg

    stack = _postgres_backend(postgres_url)
    thread_id = stack["thread_id"]
    holder = psycopg.connect(postgres_url)
    try:
        spy = ByteTouchSpy(stack["objects"])
        service = AttachmentPreparationService(
            store=stack["new_store"](), objects=spy
        )
        record = service.accept(
            thread_id=thread_id,
            filename="held.txt",
            payload=REVENUE_TEXT.encode("utf-8"),
        )
        published = service.prepare(record.attachment_id)
        assert published.status == "ready"
        spy.report = _void_report(
            stack["new_store"](),
            thread_id=thread_id,
            attachments=(
                (record.attachment_id, published.object_key, published.version),
            ),
        )
        holder_pid = int(holder.execute("SELECT pg_backend_pid()").fetchone()[0])
        # A publishing writer's footprint on the row: taken for the life of
        # an open derived-write transaction.
        holder.execute(
            "SELECT attachment_id FROM fin_thread_attachment "
            "WHERE attachment_id = %s FOR UPDATE",
            (record.attachment_id,),
        )
        outcomes: list[Any] = []

        def parked_delete() -> None:
            try:
                service.delete_attachment(
                    thread_id=thread_id, attachment_id=record.attachment_id
                )
                outcomes.append("ok")
            except Exception as exc:  # noqa: BLE001 - the parked outcome is the point
                outcomes.append(exc)

        worker = threading.Thread(target=parked_delete, daemon=True)
        worker.start()
        void_pid = _blocked_writer_pid(postgres_url, holder_pid=holder_pid)
        assert void_pid is not None, (
            "the void did not wait for the other session's row lock"
        )
        assert not _tombstone_write_started(postgres_url, pid=void_pid), (
            "the void wrote its tombstone before waiting for the row: the "
            "wait it is credited with was for the row removal, not for the "
            "tombstone write"
        )
        assert stack["new_store"]().pending_cleanup(thread_id) == []
        assert outcomes == []
        assert spy.touched == []

        holder.commit()  # the publisher's transaction lets the row go
        worker.join(timeout=10)
        assert outcomes == ["ok"], outcomes
        assert spy.snapshots == [
            _voided_once(published.object_key, owed=(published.object_key,))
        ], spy.snapshots
        assert service.status(record.attachment_id) is None
    finally:
        holder.rollback()
        holder.close()
        _drop_thread(postgres_url, thread_id)


class _LivenessGate:
    """A thread store that freezes its Nth liveness answer until released.

    WHY block there: asking whether the thread is live is the only external
    call the in-memory void makes inside the critical section that must make
    the decision, the row removal, and the obligation record one atomic step
    — once from the sweep, again from the void's own row lookup. Freezing one
    numbered answer parks the void exactly where it has decided everything
    and published nothing, the one instant a test can open without touching
    the store.
    """

    def __init__(self, inner: InMemoryThreadStore, *, block_at: int) -> None:
        self.inner = inner
        self._block_at = block_at
        self._calls = 0
        self._armed = False
        self._released = False
        self.entered = threading.Event()
        self._release = threading.Event()

    def arm(self) -> None:
        self._armed = True

    def release(self) -> None:
        self._armed = False
        self._released = True
        self._release.set()

    def create_thread(self, *, title: str) -> Any:
        return self.inner.create_thread(title=title)

    def get_thread(self, thread_id: str) -> Any:
        if self._armed and not self._released:
            self._calls += 1
            if self._calls == self._block_at:
                self.entered.set()
                assert self._release.wait(10.0), "the liveness gate was never released"
        return self.inner.get_thread(thread_id)


def test_a_derived_write_racing_a_parked_void_meets_the_lock_not_the_window() -> None:
    """Mid-void, what a preparation can read is the lock, never a live row.

    A void parked between its liveness sweep and its tombstone write has
    removed no row and recorded no obligation, so the state it is holding
    open is fully publishable: a preparation allowed to read it would freeze
    units and readiness onto an attachment a deletion has already taken
    over, and nothing it reads could tell it otherwise. The store's lock is
    the entire defense, and the observable promise is the deletion contract
    itself — late work cannot publish — so the test asks for the ordering
    only while the void is provably parked, then for the loss the completed
    tombstone must hand back.
    """
    threads = InMemoryThreadStore()
    gate = _LivenessGate(threads, block_at=2)
    thread = threads.create_thread(title="void window")
    thread_id = thread.id
    store = InMemoryAttachmentStore(gate)
    service = AttachmentPreparationService(
        store=store, objects=InMemoryObjectStore()
    )
    record = service.accept(
        thread_id=thread_id,
        filename="late.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    outcomes: list[Any] = []
    published: list[Any] = []
    publication_done = threading.Event()

    def parked_void() -> None:
        try:
            service.delete_attachment(
                thread_id=thread_id, attachment_id=record.attachment_id
            )
            outcomes.append("void-ok")
        except Exception as exc:  # noqa: BLE001 - the outcome is the point
            outcomes.append(exc)

    def racing_publication() -> None:
        try:
            published.append(service.prepare(record.attachment_id))
        except Exception as exc:  # noqa: BLE001 - losing the race is one outcome
            outcomes.append(exc)
        finally:
            publication_done.set()

    gate.arm()
    void = threading.Thread(target=parked_void, daemon=True)
    void.start()
    assert gate.entered.wait(5.0), (
        "the void never reached its row lookup inside the void step"
    )
    publication = threading.Thread(target=racing_publication, daemon=True)
    publication.start()
    try:
        assert not publication_done.wait(0.5), (
            "a derived write completed while the void sat between its "
            "liveness sweep and its tombstone write: nothing was locking "
            "the tombstone write against it"
        )
    finally:
        gate.release()
    void.join(timeout=10)
    publication.join(timeout=10)

    assert published == [], published
    assert "void-ok" in outcomes, outcomes
    assert any(
        isinstance(outcome, (AttachmentThreadGoneError, AttachmentNotFoundError))
        for outcome in outcomes
    ), outcomes
    assert store.get(record.attachment_id) is None
    assert store.pending_cleanup(thread_id) == []


def test_deleting_one_attachment_refuses_a_late_preparation(
    race_stack: dict[str, Any],
) -> None:
    """A preparation in flight when the tombstone lands publishes nothing.

    Runs on both durable backends: the racing preparation and the deleting
    door hold separate attachment stores, which against Postgres means
    separate connections, so the derived write loses to a void another
    session has already committed rather than to a dictionary entry.
    """
    objects = race_stack["objects"]
    thread_id = race_stack["thread_id"]
    release = threading.Event()
    entered = threading.Event()

    class BlockingGet(GateObjectStore):
        def get(self, key: str) -> bytes:
            entered.set()
            assert release.wait(timeout=5)
            return self.inner.get(key)

    byte_store = BlockingGet(objects)
    store = race_stack["new_store"]()
    racer = AttachmentPreparationService(store=store, objects=byte_store)
    deleter = AttachmentPreparationService(
        store=race_stack["new_store"](), objects=byte_store
    )
    record = racer.accept(
        thread_id=thread_id, filename="race.txt", payload=REVENUE_TEXT.encode("utf-8")
    )

    outcome: list[Any] = []
    worker = threading.Thread(
        target=lambda: outcome.append(_attempt(racer.prepare, record.attachment_id)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=5)
    deleter.delete_attachment(thread_id=thread_id, attachment_id=record.attachment_id)
    release.set()
    worker.join(timeout=5)

    assert outcome and isinstance(outcome[0], AttachmentThreadGoneError)
    assert racer.status(record.attachment_id) is None
    assert store.units(record.attachment_id, 1) == ()
    assert store.indexed_versions(record.attachment_id) == ()
    assert store.get(record.attachment_id) is None
    assert store.pending_cleanup(thread_id) == []
    with pytest.raises(UnknownObjectKeyError):
        objects.get(record.object_key)


def test_deleting_one_attachment_keeps_the_tombstone_when_cleanup_fails(
    client: TestClient,
    wired: dict[str, Any],
    gate: GateObjectStore,
    objects: InMemoryObjectStore,
) -> None:
    """Cleanup failure is typed, observable, and retriable — never complete."""
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    aid = body["attachment_id"]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    object_key = wired["service"].status(aid).object_key
    gate.delete_fails = True

    blocked = _delete_one(client, wired["thread_id"], aid)

    assert blocked.status_code == 500
    assert blocked.json() == {"error": "attachment_cleanup_failed"}
    assert wired["service"].status(aid) is None
    assert wired["service"].units(aid) == ()
    assert objects.get(object_key) == REVENUE_TEXT.encode("utf-8")
    assert wired["service"].store.pending_cleanup(wired["thread_id"]) == [object_key]

    gate.delete_fails = False
    retried = _delete_one(client, wired["thread_id"], aid)

    assert retried.status_code == 200, retried.text
    with pytest.raises(UnknownObjectKeyError):
        objects.get(object_key)
    assert wired["service"].store.pending_cleanup(wired["thread_id"]) == []
    # The retry closed the obligation; the id is now simply unknown.
    assert _delete_one(client, wired["thread_id"], aid).status_code == 400


def test_repeated_and_foreign_deletion_of_one_attachment_names_the_identity(
    client: TestClient, wired: dict[str, Any]
) -> None:
    """Cross-thread, unknown, and stale ids share one named refusal."""
    aid = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))[
        "attachment_id"
    ]
    other = wired["threads"].create_thread(title="other thread")

    assert _delete_one(client, wired["thread_id"], "never-uploaded").status_code == 400
    assert _delete_one(client, other.id, aid).json() == {"error": "unknown_locator"}
    assert wired["service"].status(aid) is not None

    assert _delete_one(client, wired["thread_id"], aid).status_code == 200
    stale = _delete_one(client, wired["thread_id"], aid)
    assert stale.status_code == 400
    assert stale.json() == {"error": "unknown_locator"}
    assert _delete_one(client, other.id, aid).json() == {"error": "unknown_locator"}


def test_a_deleted_attachment_is_not_referenceable_by_a_later_turn(
    client: TestClient, wired: dict[str, Any]
) -> None:
    aid = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))[
        "attachment_id"
    ]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    assert _delete_one(client, wired["thread_id"], aid).status_code == 200

    response = client.post(
        f"/api/threads/{wired['thread_id']}/messages",
        json={"question": "hi", "attachment_ids": [aid]},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "attachment_not_found"}


def _matching_units(wired: dict[str, Any], attachment_id: str, version: int) -> list[int]:
    """What one query can reach through the identity's own postings."""
    postings = wired["service"].store.index_postings(attachment_id, version)
    return [hit.unit for hit in search(postings, "revenue")]


def test_a_voided_attachment_matches_no_query_while_its_sibling_still_does(
    client: TestClient, wired: dict[str, Any]
) -> None:
    """Retrieval reaches only what the tombstone left standing.

    The postings of a voided identity read as empty, so no turn can be
    answered out of a file the operator deleted, while a sibling prepared at
    the same version keeps matching.
    """
    aid = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))[
        "attachment_id"
    ]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    sibling = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))[
        "attachment_id"
    ]
    _prepare_through_door(client, wired["service"], wired["thread_id"], sibling)
    # The drain must have published both rows: the sibling promise below
    # (still matching after its twin is voided) is only real over two
    # built indexes.
    assert wired["service"].status(aid).status == "ready"
    assert wired["service"].status(sibling).status == "ready"
    version = wired["service"].status(aid).version
    assert _matching_units(wired, aid, version)
    assert _matching_units(wired, sibling, version)

    assert _delete_one(client, wired["thread_id"], aid).status_code == 200

    assert _matching_units(wired, aid, version) == []
    assert _matching_units(wired, sibling, version)


def test_a_refused_cleanup_names_the_owed_bytes_in_the_log(
    client: TestClient,
    wired: dict[str, Any],
    gate: GateObjectStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Observable means an operator can see WHICH bytes are still owed.

    The 500 names the failure class; only the log carries the thread and key
    the retry has to be pointed at, so a silent refusal would leave the
    obligation unreadable even though the tombstone is held.
    """
    aid = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))[
        "attachment_id"
    ]
    _prepare_through_door(client, wired["service"], wired["thread_id"], aid)
    object_key = wired["service"].status(aid).object_key
    gate.delete_fails = True

    with caplog.at_level(logging.WARNING, logger="fin.host.attachment_prepare"):
        assert _delete_one(client, wired["thread_id"], aid).status_code == 500

    named = [record.getMessage() for record in caplog.records]
    assert any(
        object_key in message and wired["thread_id"] in message
        for message in named
    ), named

    refused = [
        record
        for record in caplog.records
        if record.name == "fin.host.attachment_prepare"
        and object_key in record.getMessage()
    ]
    # WHY a count: one refused key is one fact about one leftover. The same
    # key named twice, once per layer that happened to notice it, is how an
    # operator learns to skim the line and miss the third one.
    assert len(refused) == 1, [record.getMessage() for record in refused]
    line = refused[0].getMessage()
    # WHY the verdict in the line: a record still owns this key, so the same
    # door can finish the job on retry. That is the whole difference between a
    # blocked cleanup and an orphan, and the reader has to see it here.
    assert "still owed" in line, line
    # WHY the traceback: the byte store's own reason is the actionable half of
    # a refusal, and a message that only formats the exception drops it.
    assert refused[0].exc_info, line


def test_concurrent_deletes_of_one_attachment_expose_nothing(
    race_stack: dict[str, Any],
) -> None:
    """Two deletes of the same id cannot resurrect it or strand a record.

    Each racer holds its own attachment store, so on the Postgres parameter
    the second void decides against the first one's committed rows and its
    obligation record, the way two doors in two processes would.
    """
    objects = race_stack["objects"]
    thread_id = race_stack["thread_id"]
    release = threading.Event()
    started = threading.Event()

    class SlowDelete(GateObjectStore):
        def delete(self, key: str) -> None:
            started.set()
            assert release.wait(timeout=5)
            self.inner.delete(key)

    byte_store = SlowDelete(objects)
    store = race_stack["new_store"]()
    service = AttachmentPreparationService(store=store, objects=byte_store)
    other = AttachmentPreparationService(
        store=race_stack["new_store"](), objects=byte_store
    )
    record = service.accept(
        thread_id=thread_id,
        filename="twice.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    outcomes: list[Any] = []
    guard = threading.Lock()

    def worker(racer: AttachmentPreparationService) -> None:
        try:
            racer.delete_attachment(thread_id=thread_id, attachment_id=record.attachment_id)
            outcome: Any = "ok"
        except Exception as exc:  # noqa: BLE001 - the race outcome is the assertion
            outcome = exc
        with guard:
            outcomes.append(outcome)

    first = threading.Thread(target=worker, args=(service,), daemon=True)
    first.start()
    assert started.wait(timeout=5)
    second = threading.Thread(target=worker, args=(other,), daemon=True)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(outcomes) == 2
    assert not any(
        isinstance(outcome, (AttachmentCleanupError, AttachmentStoreError))
        for outcome in outcomes
    )
    assert service.status(record.attachment_id) is None
    # The loser of the race reached the same answer through the obligation the
    # winner left behind, and neither door stranded a byte or a row.
    assert other.status(record.attachment_id) is None
    assert store.pending_cleanup(thread_id) == []
    with pytest.raises(UnknownObjectKeyError):
        objects.get(record.object_key)


def _attempt(call: Any, argument: str) -> Any:
    try:
        return call(argument)
    except Exception as exc:  # noqa: BLE001 - records the typed race outcome
        return exc


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


# ----- input classes at the public door -----


def test_empty_body_is_rejected(client: TestClient, wired: dict[str, Any]) -> None:
    response = client.post(
        f"/api/threads/{wired['thread_id']}/attachments", content=b""
    )
    assert response.status_code == 400
    assert response.json() == {"error": "empty_attachment"}


def test_non_utf8_body_is_rejected(
    client: TestClient, http_app: FastAPI, wired: dict[str, Any]
) -> None:
    """Raw bytes are the contract: undecodable content still uploads, the
    typed rejection lands when the durable job reads it back, and a run
    that already started on this id takes the refusal on its turn row —
    never an empty success, never a stale ``ready``.
    """
    graph = _gate_wired_graph(wired["service"])
    http_app.dependency_overrides[get_graph] = lambda: graph

    response = client.post(
        f"/api/threads/{wired['thread_id']}/attachments",
        content=b"\xff\xfe\x00\x01",
    )
    assert response.status_code == 200, response.text
    uploaded = response.json()
    assert uploaded["status"] == "uploaded"
    assert uploaded["failure_reason"] is None

    # T2f: the upload is referenceable, so a grounded send accepts the
    # stored row and parks on the gate; the deferred rejection is met by
    # the job, on the run the turn already started.
    send = client.post(
        f"/api/threads/{wired['thread_id']}/messages",
        json={
            "question": GROUNDED_QUESTION,
            "attachment_ids": [uploaded["attachment_id"]],
        },
    )
    assert send.status_code == 200, send.text
    events = _parse_sse_events(send.text)
    waits = [data for name, data in events if name == "attachment_wait"]
    assert waits, f"no attachment_wait frame on the wire: {[n for n, _ in events]}"
    run_id = str(waits[-1]["run_id"])
    assert wired["threads"].list_runs(wired["thread_id"]) == []

    assert wired["service"].drain_attachment_jobs() == 1
    rejected = wired["service"].status(uploaded["attachment_id"])
    assert rejected.status == "failed"
    assert rejected.failure_reason == "attachment_not_text"
    assert rejected.searchable is False

    # The settled pause is driven by the shared resume — the same path
    # the prep worker takes — and the run answers as the gate's typed
    # refusal under the run id the pause carried.
    assert (
        _resume_one_attachment_wait(
            graph,
            wired["threads"],
            wired["service"],
            RunControl(),
            wired["thread_id"],
        )
        is True
    )
    turns = wired["threads"].list_runs(wired["thread_id"])
    assert [row.id for row in turns] == [run_id]
    assert turns[0].status == "refuse"
    assert turns[0].status_reason == "attachment_not_text"
    assert turns[0].answer == ""
    assert turns[0].citations == []


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

    # T2f shape of the retry door: it re-asks and answers immediately.
    # The failed row leaves the door ``parsing`` with its retired reason
    # cleared, exactly one durable job queued, nothing parsed inside the
    # request.
    asked = _retry(client, wired["thread_id"], record.attachment_id)
    assert asked["status"] == "parsing"
    assert asked["failure_reason"] is None
    jobs = wired["service"].jobs.incomplete()
    assert len(jobs) == 1
    assert jobs[0].state == "pending"

    assert wired["service"].drain_attachment_jobs() == 1
    settled = _status(client, wired["thread_id"], record.attachment_id).json()
    assert settled["status"] == "ready"
    assert settled["searchable"] is True
    assert wired["service"].units(record.attachment_id) == (
        ParsedAttachmentUnit(unit=1, text=REVENUE_TEXT),
    )


def test_redriving_a_lost_index_keeps_the_published_version_addressable(
    wired: dict[str, Any], gate: GateObjectStore, client: TestClient
) -> None:
    # Re-preparation writes a NEW version, so the version earlier answers
    # already cited has to survive an attempt that then fails.
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    attachment_id = body["attachment_id"]
    _prepare_through_door(
        client, wired["service"], wired["thread_id"], attachment_id
    )
    published = wired["service"].status(attachment_id)
    assert published.status == "ready"

    wired["service"].store.clear_index(attachment_id)
    gate.get_fails = True

    # T2f shape: a row that lost its index reads ``parsing``, so the
    # retry door queues the rebuild (reopening the succeeded job) and
    # answers immediately; the failed verdict arrives on the row only
    # after the drain ran the preparation.
    asked = _retry(client, wired["thread_id"], attachment_id)
    assert asked["status"] == "parsing"
    assert asked["version"] == published.version
    assert len(wired["service"].jobs.incomplete()) == 1
    assert wired["service"].drain_attachment_jobs() == 1

    settled = _status(client, wired["thread_id"], attachment_id).json()
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == "source_unavailable"
    # A failed re-drive stamps no new version: the text published at
    # ``published.version`` stays exactly where earlier citations left it.
    assert settled["version"] == published.version
    assert wired["service"].store.units(
        attachment_id, published.version
    ) == (ParsedAttachmentUnit(unit=1, text=REVENUE_TEXT),)


def test_a_ready_attachment_with_lost_text_is_not_injected_as_content(
    wired: dict[str, Any]
) -> None:
    # EXIT: a unit row whose text is gone is not an empty document — the turn
    # gets the typed not-ready exit instead of a content block naming nothing.
    thread_id = wired["thread_id"]
    record = wired["service"].accept(
        thread_id=thread_id,
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    attachment_id = record.attachment_id
    wired["service"].store.record_units(
        attachment_id=attachment_id,
        version=1,
        units=(ParsedAttachmentUnit(unit=1, text=""),),
    )
    wired["service"].store.put_index(
        attachment_id=attachment_id, version=1, postings={"revenue": [1]}
    )
    wired["service"].store.mark_ready(
        attachment_id=attachment_id, version=1, unit_count=1
    )

    with pytest.raises(AttachmentNotReadyError):
        wired["service"].resolve_for_turn(
            thread_id=thread_id, attachment_id=attachment_id
        )


def test_the_list_door_reports_a_row_that_lost_its_index_as_parsing(
    client: TestClient, wired: dict[str, Any]
) -> None:
    published = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    lost = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    for attachment in (published, lost):
        _prepare_through_door(
            client, wired["service"], wired["thread_id"],
            attachment["attachment_id"],
        )
    wired["service"].store.clear_index(lost["attachment_id"])

    listed = client.get(f"/api/threads/{wired['thread_id']}/attachments")
    assert listed.status_code == 200, listed.text
    rows = listed.json()["attachments"]
    assert [(row["status"], row["searchable"]) for row in rows] == [
        ("ready", True),
        ("parsing", False),
    ]
    assert rows[0]["scope"] == "thread_attach"


def test_every_attachment_door_answers_with_one_shape(
    client: TestClient, wired: dict[str, Any]
) -> None:
    body = _attach(client, wired["thread_id"], REVENUE_TEXT.encode("utf-8"))
    attachment_id = body["attachment_id"]
    thread_path = f"/api/threads/{wired['thread_id']}/attachments"
    doors = [
        body,
        _status(client, wired["thread_id"], attachment_id).json(),
        client.get(thread_path).json()["attachments"][0],
        client.post(f"{thread_path}/{attachment_id}/retry").json(),
    ]
    shape = {
        "attachment_id",
        "failure_reason",
        "filename",
        "scope",
        "searchable",
        "size_bytes",
        "status",
        "unit_count",
        "version",
    }
    for record in doors:
        assert set(record) == shape
        assert isinstance(record["version"], int)
        assert record["attachment_id"] == attachment_id


def test_concurrent_repreparation_publishes_one_index_version(
    wired: dict[str, Any], gate: GateObjectStore
) -> None:
    attempts = 4
    record = wired["service"].accept(
        thread_id=wired["thread_id"],
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    gate.get_fails = True
    assert wired["service"].prepare(record.attachment_id).status == "failed"
    gate.get_fails = False

    gate_open = threading.Barrier(attempts)
    results: list[Any] = []

    def race() -> None:
        gate_open.wait(timeout=10)
        results.append(_attempt(wired["service"].prepare, record.attachment_id))

    workers = [threading.Thread(target=race) for _ in range(attempts)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert len(results) == attempts
    assert all(
        isinstance(result, AttachmentRecord) and result.status == "ready"
        for result in results
    ), results
    final = wired["service"].status(record.attachment_id)
    assert final.status == "ready"
    assert wired["service"].store.indexed_versions(record.attachment_id) == (
        final.version,
    )
    assert [
        hit.unit
        for hit in search(
            wired["service"].store.index_postings(
                record.attachment_id, final.version
            ),
            "revenue",
        )
    ] == [1]
