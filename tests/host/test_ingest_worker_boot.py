"""Tests for the ingest worker's product boot wiring.

The corpus-ingest contract's resume clause — *"after restart, a
queued/running/interrupted job resumes from durable state without
re-upload"* — is pinned class-level in
``tests/corpus/test_ingest_worker.py``; what lives here is the seam
that makes it run inside a real boot:

* the HTTP routes never start the worker and never drain a queue
  (accept-and-return stays the door's whole job);
* ``start_ingest_worker`` is the explicit, idempotent per-process start
  path and ``stop_ingest_worker`` joins the thread cleanly;
* a start racing a timed-out stop is refused (typed) until the old
  thread dies — one drain loop per process even across stop-then-start;
* a boot drain drives an HTTP-submitted ``pending`` job to ``ready``
  and the status door polls the real persisted states throughout;
* a fresh worker started over the same durable state resumes a job a
  previous process left mid-run — no re-upload (the accept wrote the
  object exactly once);
* a failing drain is logged with exception context and the loop keeps
  running (chosen posture: keep the loop, never silent thread death);
* ``serve()`` starts the drain before it binds and stops it after the
  server returns.

Everything runs on the in-memory ports and a tmp journal dir: hermetic,
and the default suite never spawns the thread except in the tests that
opt in (each stops it again at teardown).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore, ObjectPointer
from fin.host import workspace_http
from fin.host.workspace_http import (
    get_ingest_service,
    start_ingest_worker,
    stop_ingest_worker,
)
from fin.retrieve.fake_push_port import FakePushPort

_MD_BYTES = (
    b"# Heading\n\nA first paragraph with enough body text to chunk.\n"
)
JOB = "AAPL-2023Q3"
WORKER_THREAD_NAME = "fin-ingest-worker"


@pytest.fixture(autouse=True)
def _no_leaked_worker_thread() -> Any:
    # Every case that starts the real loop must not hand a live thread
    # to the rest of the suite, whatever the case body did.
    yield
    stop_ingest_worker(timeout=10.0)


class _RecordingObjectStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectPointer:
        self.puts.append(key)
        return super().put(key, payload, content_type=content_type)


class _Stack:
    def __init__(self, endpoint: IngestEndpoint, objects: Any, states: Any) -> None:
        self.endpoint = endpoint
        self.objects = objects
        self.states = states


def _fresh_endpoint() -> _Stack:
    objects = _RecordingObjectStore()
    states = InMemoryIngestStateStore()
    push = FakePushPort()
    endpoint = IngestEndpoint(
        push_port=push,
        invalidation_port=push,
        object_store=objects,
        metadata_store=InMemoryMetadataStore(),
        state_store=states,
    )
    return _Stack(endpoint, objects, states)


@pytest.fixture
def http_app() -> Any:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _install(http_app: FastAPI) -> _Stack:
    stack = _fresh_endpoint()
    http_app.dependency_overrides[get_ingest_service] = lambda: stack.endpoint
    return stack


def _post(client: TestClient) -> Any:
    return client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"company": "AAPL", "period": "2023Q3"},
    )


def _status(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/api/corpus/ingest/{JOB}")
    assert response.status_code == 200
    return response.json()


def _wait_for_state(
    client: TestClient, wanted: str, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = _status(client)
        if body["state"] == wanted:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job never reached {wanted!r}: last body {body!r}")


# ----- the door never drains on its own --------------------------------


def test_http_accept_leaves_the_job_pending_with_no_worker_thread(
    http_app: FastAPI, client: TestClient
) -> None:
    stop_ingest_worker()  # pin: nothing earlier in this process may leak in
    _install(http_app)

    response = _post(client)

    assert response.status_code == 200
    body = _status(client)
    assert body["state"] == "pending"
    assert [
        thread
        for thread in threading.enumerate()
        if thread.name == WORKER_THREAD_NAME
    ] == []


# ----- explicit start / stop -------------------------------------------


def test_start_is_idempotent_and_stop_joins_cleanly(
    tmp_path: Path,
) -> None:
    stack = _fresh_endpoint()
    journals = tmp_path / "journals"

    first = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    second = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)

    assert second is first
    assert first.name == WORKER_THREAD_NAME
    assert first.daemon
    stop_ingest_worker(timeout=10.0)
    assert not first.is_alive()
    # After a stop, starting again is allowed (the restart leg of a
    # long-lived process): a fresh thread, not the dead one.
    third = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    assert third is not first
    assert third.is_alive()
    stop_ingest_worker(timeout=10.0)


# ----- boot drain over the real HTTP door -------------------------------


def test_boot_drain_drives_an_http_queued_job_to_ready(
    http_app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    stack = _install(http_app)
    submission = _post(client).json()
    assert submission["state"] == "pending"

    start_ingest_worker(
        endpoint=stack.endpoint,
        journal_dir=tmp_path / "journals",
        check_interval_seconds=60.0,
    )

    body = _wait_for_state(client, "ready")
    assert body["job_id"] == submission["job_id"]
    assert body["text_ready"] is True
    # The bytes were fetched through the durable pointer, never
    # re-supplied: the object store saw exactly the accept-time write.
    assert len(stack.objects.puts) == 1


def test_fresh_worker_resumes_an_interrupted_job_without_reupload(
    http_app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    stack = _install(http_app)
    _post(client)
    # Model the dead process: the job got as far as extracting before
    # the interrupt. Only the durable store survives to the next boot.
    stack.states.transition(JOB, IngestState.FETCHING)
    stack.states.transition(JOB, IngestState.EXTRACTING)

    start_ingest_worker(
        endpoint=stack.endpoint,
        journal_dir=tmp_path / "journals",
        check_interval_seconds=60.0,
    )

    body = _wait_for_state(client, "ready")
    assert body["text_ready"] is True
    assert len(stack.objects.puts) == 1


# ----- drain-failure posture --------------------------------------------


def test_failed_drain_is_logged_and_the_loop_keeps_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class _FlakyWorker:
        def run_pending(self) -> None:
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("durable store is briefly down")
            stop.set()

    thread = threading.Thread(
        target=workspace_http._drain_ingest_queue,
        args=(_FlakyWorker(), stop, 0.01),
    )
    with caplog.at_level(logging.ERROR, logger="fin.host.workspace_http"):
        thread.start()
        thread.join(timeout=10.0)

    assert not thread.is_alive()
    # Chosen posture: keep the loop. The failed drain is logged with
    # exception context and the next check still runs.
    assert len(calls) == 2
    failure_records = [
        record
        for record in caplog.records
        if record.name == "fin.host.workspace_http" and record.exc_info
    ]
    assert failure_records
    assert isinstance(failure_records[0].exc_info[1], RuntimeError)


# ----- stop-then-start cannot yield two drain loops ---------------------


class _GatedWorkerFactory:
    """Stand-in for ``workspace_http.IngestWorker`` with a blockable drain.

    ``run_pending`` enters (sets ``entered``) then blocks on ``release``,
    so a stop's join is guaranteed to time out while the thread is alive
    and mid-sweep — the exact interleaving the singleton invariant must
    survive.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, endpoint: Any, **kwargs: Any) -> "_GatedWorkerFactory":
        return self

    def run_pending(self) -> None:
        self.entered.set()
        if not self.release.wait(timeout=10.0):
            raise RuntimeError("gate never released")


def _live_worker_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == WORKER_THREAD_NAME
    ]


def test_start_after_timed_out_stop_refuses_until_old_thread_dies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chosen lifecycle contract: typed refusal, then a clean restart.

    A stop whose join times out keeps the live thread registered, so a
    later start sees it: ``start_ingest_worker`` raises
    ``IngestWorkerStoppingError`` instead of spawning a second drain
    loop, and starts fresh once the old thread has exited.
    """
    gated = _GatedWorkerFactory()
    monkeypatch.setattr(workspace_http, "IngestWorker", gated)
    stack = _fresh_endpoint()
    journals = tmp_path / "journals"

    first = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    assert gated.entered.wait(10.0)

    stop_ingest_worker(timeout=0.1)  # thread is gated mid-sweep: join times out
    assert first.is_alive()

    with pytest.raises(workspace_http.IngestWorkerStoppingError):
        start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    assert _live_worker_threads() == [first]

    gated.release.set()
    deadline = time.monotonic() + 10.0
    while first.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not first.is_alive()

    second = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    assert second is not first
    assert _live_worker_threads() == [second]
    stop_ingest_worker(timeout=10.0)


def test_clean_stop_still_allows_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal is only for a still-alive stopping thread."""
    gated = _GatedWorkerFactory()
    gated.release.set()  # drains return immediately
    monkeypatch.setattr(workspace_http, "IngestWorker", gated)
    stack = _fresh_endpoint()
    journals = tmp_path / "journals"

    first = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    stop_ingest_worker(timeout=10.0)
    assert not first.is_alive()

    second = start_ingest_worker(endpoint=stack.endpoint, journal_dir=journals)
    assert second is not first
    assert second.is_alive()
    stop_ingest_worker(timeout=10.0)


# ----- serve() wiring ----------------------------------------------------


def test_serve_starts_the_drain_before_binding_and_stops_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fin.corpus.ingest_storage as ingest_storage
    import fin.host.http as http_module
    import uvicorn

    events: list[str] = []
    monkeypatch.setattr(http_module, "apply_dotenv", lambda: None)
    monkeypatch.setattr(http_module, "get_attachment_budgets", lambda: None)
    monkeypatch.setattr(
        ingest_storage, "preflight_ingest_storage", lambda: None
    )
    monkeypatch.setattr(http_module, "publish_knowledge_mcp", lambda: None)
    monkeypatch.setattr(http_module, "compile_for_http", lambda: None)
    monkeypatch.setattr(
        http_module, "start_ingest_worker", lambda: events.append("start")
    )
    monkeypatch.setattr(
        http_module, "stop_ingest_worker", lambda: events.append("stop")
    )
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: events.append("serve")
    )

    http_module.serve()

    assert events == ["start", "serve", "stop"]
