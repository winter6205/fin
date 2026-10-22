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
* the ready landing performs the index-job submit handoff through the
  process-shared tracker, a boot resume re-submits a ready-but-untracked
  job without re-pushing, and an un-injected start wires the env-default
  tracker + path-backed ledger the status route also reads.

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

from fin.corpus.index_job import IndexJobLedger, IndexJobTracker
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore, ObjectPointer
from fin.host import workspace_http
from fin.host.application.lifetime import RuntimeHandle
from fin.host.workspace_http import (
    get_index_job_tracker,
    get_ingest_service,
    start_ingest_worker,
    stop_ingest_worker,
)
from fin.retrieve.fake_index_job_port import FakeIndexJobPort
from fin.retrieve.fake_push_port import FakePushPort

_MD_BYTES = (
    b"# Heading\n\nA first paragraph with enough body text to chunk.\n"
)
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
    def __init__(
        self,
        endpoint: IngestEndpoint,
        objects: Any,
        states: Any,
        index_port: FakeIndexJobPort,
        index_tracker: IndexJobTracker,
        index_ledger: IndexJobLedger,
    ) -> None:
        self.endpoint = endpoint
        self.objects = objects
        self.states = states
        self.index_port = index_port
        self.index_tracker = index_tracker
        self.index_ledger = index_ledger


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
        corpus_scope_id="ingest-worker-boot",
    )
    # The contract fake index-job port over an in-process ledger: the
    # boot tests pin that the drain performs the submit handoff and the
    # route reads the same tracker, without reaching a live service.
    index_port = FakeIndexJobPort()
    index_ledger = IndexJobLedger()
    index_tracker = IndexJobTracker(index_port, index_ledger)
    return _Stack(
        endpoint, objects, states, index_port, index_tracker, index_ledger
    )


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
    http_app.dependency_overrides[get_index_job_tracker] = (
        lambda: stack.index_tracker
    )
    return stack


def _start(stack: _Stack, journal_dir: Path, **kwargs: Any) -> Any:
    """Start the boot worker over the stack's fake index-job wiring."""
    return start_ingest_worker(
        endpoint=stack.endpoint,
        journal_dir=journal_dir,
        index_job_tracker=stack.index_tracker,
        index_job_ledger=stack.index_ledger,
        **kwargs,
    )


def _post(client: TestClient) -> Any:
    return client.post(
        "/api/corpus/ingest",
        files={"file": ("sample.md", _MD_BYTES, "text/markdown")},
        data={"company": "AAPL", "period": "2023Q3"},
    )


def _status(client: TestClient, job_id: str) -> dict[str, Any]:
    response = client.get(f"/api/corpus/ingest/{job_id}")
    assert response.status_code == 200
    return response.json()


def _wait_for_state(
    client: TestClient, job_id: str, wanted: str, timeout: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = _status(client, job_id)
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
    body = _status(client, response.json()["job_id"])
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

    first = _start(stack, journals)
    second = _start(stack, journals)

    assert second is first
    assert first.name == WORKER_THREAD_NAME
    assert first.daemon
    stop_ingest_worker(timeout=10.0)
    assert not first.is_alive()
    # After a stop, starting again is allowed (the restart leg of a
    # long-lived process): a fresh thread, not the dead one.
    third = _start(stack, journals)
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

    _start(
        stack,
        tmp_path / "journals",
        check_interval_seconds=60.0,
    )

    body = _wait_for_state(client, submission["job_id"], "ready")
    assert body["job_id"] == submission["job_id"]
    assert body["text_ready"] is True
    # The bytes were fetched through the durable pointer, never
    # re-supplied: the object store saw exactly the accept-time write.
    assert len(stack.objects.puts) == 1
    # The same drain performed the index-job handoff, and the status
    # route reads it through the one shared tracker: the graph face is
    # the submitted jobs' polled pass-through, never a fin claim.
    assert body["graph"] is not None
    assert body["graph"]["jobs"], body["graph"]
    assert stack.index_port.submit_calls == len(body["graph"]["jobs"])


def test_fresh_worker_resumes_an_interrupted_job_without_reupload(
    http_app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    stack = _install(http_app)
    job_id = _post(client).json()["job_id"]
    # Model the dead process: the job got as far as extracting before
    # the interrupt. Only the durable store survives to the next boot.
    stack.states.transition(job_id, IngestState.FETCHING)
    stack.states.transition(job_id, IngestState.EXTRACTING)

    _start(
        stack,
        tmp_path / "journals",
        check_interval_seconds=60.0,
    )

    body = _wait_for_state(client, job_id, "ready")
    assert body["text_ready"] is True
    assert len(stack.objects.puts) == 1


def test_boot_resume_submits_a_ready_but_untracked_job_without_repush(
    http_app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    """The index-job resume edge at the boot level.

    A previous process landed the text but died before the submit (no
    ledger entries). The boot drain must re-attempt the submit from the
    durable ready-edge pair map — no re-run, no re-push (object writes
    and push-port count stay at the accept's) — and the status door
    then passes the job state through.
    """
    stack = _install(http_app)
    job_id = _post(client).json()["job_id"]
    untracked = workspace_http.IngestWorker(
        stack.endpoint, journal_dir=tmp_path / "dead-process-journals"
    )
    assert [
        outcome.state
        for outcome in untracked.run_pending()
    ] == [IngestState.READY]
    assert stack.index_port.submit_calls == 0
    pushed_before = stack.endpoint._push_port.written_total()

    _start(
        stack,
        tmp_path / "journals",
        check_interval_seconds=60.0,
    )

    body = _wait_for_state(client, job_id, "ready")
    assert body["text_ready"] is True
    graph = body["graph"]
    assert graph is not None and graph["jobs"], body
    assert stack.index_port.submit_calls == len(graph["jobs"])
    assert len(stack.objects.puts) == 1
    assert stack.endpoint._push_port.written_total() == pushed_before


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
        self.init_kwargs = kwargs
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

    first = _start(stack, journals)
    assert gated.entered.wait(10.0)

    stop_ingest_worker(timeout=0.1)  # thread is gated mid-sweep: join times out
    assert first.is_alive()

    with pytest.raises(workspace_http.IngestWorkerStoppingError):
        _start(stack, journals)
    assert _live_worker_threads() == [first]

    gated.release.set()
    deadline = time.monotonic() + 10.0
    while first.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not first.is_alive()

    second = _start(stack, journals)
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

    first = _start(stack, journals)
    stop_ingest_worker(timeout=10.0)
    assert not first.is_alive()

    second = _start(stack, journals)
    assert second is not first
    assert second.is_alive()
    stop_ingest_worker(timeout=10.0)


# ----- the boot's index-job wiring ---------------------------------------


def test_start_without_injection_takes_the_process_shared_index_job_tracker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Requirement-level pin: worker and route share one tracker.

    A start that injects no tracker resolves the process singleton —
    the env-selected port (fake boot: the contract fake, no network)
    and the ledger path-backed beside the other ingest journals — and
    hands the same pair to the worker that :func:`get_index_job_tracker`
    answers with, so a submit the worker makes is what the status route
    reads.
    """
    monkeypatch.setattr(workspace_http, "_index_job_singleton", None)
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    monkeypatch.setenv(
        "FIN_CORPUS_INDEX_JOB_LEDGER_PATH",
        str(tmp_path / "index_jobs.jsonl"),
    )
    gated = _GatedWorkerFactory()
    gated.release.set()
    monkeypatch.setattr(workspace_http, "IngestWorker", gated)
    stack = _fresh_endpoint()

    start_ingest_worker(
        endpoint=stack.endpoint, journal_dir=tmp_path / "journals"
    )

    tracker, ledger = workspace_http._get_index_job_components()
    assert gated.init_kwargs["index_job_tracker"] is tracker
    assert gated.init_kwargs["index_job_ledger"] is ledger
    assert workspace_http.get_index_job_tracker() is tracker
    assert isinstance(tracker._port, FakeIndexJobPort)  # type: ignore[attr-defined]
    assert ledger.path == tmp_path / "index_jobs.jsonl"
    stop_ingest_worker(timeout=10.0)


# ----- serve() wiring ----------------------------------------------------


def test_serve_starts_the_drain_before_binding_and_stops_after(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import fin.corpus.ingest_storage as ingest_storage
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module
    import uvicorn

    events: list[str] = []
    # The boot refuses an undeclared durable checkpointer before it opens
    # anything, so this deployment has to state one. The compile itself is
    # stubbed below, so the URL is a declared placeholder, not a connection.
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setattr(composition_module, "apply_operator_settings", lambda: None)
    monkeypatch.setattr(composition_module, "attachment_budgets", lambda: None)
    monkeypatch.setattr(
        ingest_storage, "preflight_ingest_storage", lambda: None
    )
    monkeypatch.setattr(composition_module, "publish_knowledge_mcp", lambda: None)
    # The boot seam is the closeable runtime handle, not the graph-only
    # compile it delegates to, so that is what has to be stubbed here.
    monkeypatch.setattr(
        composition_module,
        "_compile_http_runtime",
        lambda **_kwargs: RuntimeHandle(None),
    )
    monkeypatch.setattr(
        composition_module, "start_application_ingest_worker", lambda: events.append("start")
    )
    monkeypatch.setattr(
        composition_module, "stop_ingest_worker", lambda: events.append("stop")
    )
    # The same declared ``FIN_CHECKPOINT_URL`` also selects the durable
    # store for the attachment seam, so every seam past the ingest worker
    # is stubbed too: the subject of this test is the boot ORDER, not
    # what each step builds.
    monkeypatch.setattr(composition_module, "default_attachment_service", lambda: None)
    monkeypatch.setattr(
        composition_module, "start_application_attachment_prep_worker", lambda service: None
    )
    monkeypatch.setattr(composition_module, "default_thread_store", lambda: None)
    monkeypatch.setattr(composition_module, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        composition_module, "start_application_attachment_resume_worker", lambda **kwargs: None
    )
    monkeypatch.setattr(
        composition_module, "start_application_invalidation_drain_worker", lambda *, store: None
    )
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: events.append("serve")
    )

    http_module.serve()

    assert events == ["start", "serve", "stop"]
