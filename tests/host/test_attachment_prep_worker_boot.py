"""Boot-thread wiring for the attachment preparation worker.

The worker is the process's executor for durable attachment jobs: a
start drains immediately (that is the restart-resumes-work clause), then
re-checks on an interval, and every drain failure is logged and survived
rather than killing the only executor. What is pinned here is the seam
shape, hermetically on in-memory ports:

* ``start`` is singleton-safe (a second call returns the live thread),
  the thread is a daemon with a fixed name, and ``stop`` joins it;
* a start racing a timed-out stop is refused until the old thread exits
  — one drain loop per worker at a time;
* the boot drain completes a job that ``request_preparation`` queued;
* a failing drain keeps looping (chosen posture: never silent death);
* nothing spawns at import and stopping an unstarted worker is a no-op.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment_job_store import InMemoryThreadAttachmentJobStore
from fin.host.attachment_prepare import AttachmentPreparationService
from fin.host.attachment_store import InMemoryAttachmentStore
from fin.host.attachment_prep_worker import (
    ATTACHMENT_PREP_WORKER_THREAD_NAME,
    AttachmentPrepWorker,
    AttachmentPrepWorkerStoppingError,
    _drain_attachment_queue,
)
from fin.host.thread_store import InMemoryThreadStore

REVENUE_TEXT = b"Q3 revenue was 89,493 million dollars.\n"


@pytest.fixture(autouse=True)
def _no_leaked_worker_thread() -> Any:
    # Every case that starts the real loop registers its worker here and
    # must not hand a live thread to the rest of the suite, whatever the
    # case body did.
    started: list[AttachmentPrepWorker] = []
    yield started
    for worker in started:
        worker.stop(timeout=10.0)


def _worker_stack() -> tuple[
    AttachmentPrepWorker, AttachmentPreparationService, InMemoryThreadAttachmentJobStore, str, str
]:
    threads = InMemoryThreadStore()
    jobs = InMemoryThreadAttachmentJobStore()
    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads),
        objects=InMemoryObjectStore(),
        jobs=jobs,
    )
    thread = threads.create_thread(title="worker boot")
    record = service.accept(
        thread_id=thread.id, filename="notes.txt", payload=REVENUE_TEXT
    )
    worker = AttachmentPrepWorker(service, interval_s=60.0)
    return worker, service, jobs, thread.id, record.attachment_id


def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true")


# ----- lifecycle -----


def test_nothing_spawns_until_start_and_stop_is_an_idempotent_noop() -> None:
    before = {thread.name for thread in threading.enumerate()}
    worker, _, _, _, _ = _worker_stack()

    worker.stop()  # never started: no-op
    worker.stop(timeout=1.0)  # still a no-op

    assert {thread.name for thread in threading.enumerate()} == before


def test_start_is_singleton_safe_daemon_and_stop_joins(
    _no_leaked_worker_thread: list[AttachmentPrepWorker],
) -> None:
    worker, _, _, _, _ = _worker_stack()
    _no_leaked_worker_thread.append(worker)

    first = worker.start()
    second = worker.start()

    assert second is first
    assert first.name == ATTACHMENT_PREP_WORKER_THREAD_NAME
    assert first.daemon
    worker.stop(timeout=10.0)
    assert not first.is_alive()
    # Restart is allowed after a clean stop: a fresh thread, not the dead one.
    third = worker.start()
    assert third is not first
    assert third.is_alive()
    worker.stop(timeout=10.0)


def test_start_after_a_timed_out_stop_refuses_until_the_old_loop_dies(
    _no_leaked_worker_thread: list[AttachmentPrepWorker],
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _GatedService:
        def drain_attachment_jobs(self) -> int:
            entered.set()
            if not release.wait(timeout=10.0):
                raise RuntimeError("gate never released")
            return 0

    worker = AttachmentPrepWorker(_GatedService(), interval_s=60.0)  # type: ignore[arg-type]
    _no_leaked_worker_thread.append(worker)

    first = worker.start()
    assert entered.wait(10.0)

    worker.stop(timeout=0.1)  # gated mid-drain: the join times out
    assert first.is_alive()
    with pytest.raises(AttachmentPrepWorkerStoppingError):
        worker.start()

    release.set()
    _wait_until(lambda: not first.is_alive())
    second = worker.start()
    assert second is not first
    worker.stop(timeout=10.0)


# ----- the boot drain runs durable work -----


def test_boot_drain_completes_a_job_queued_before_start(
    _no_leaked_worker_thread: list[AttachmentPrepWorker],
) -> None:
    worker, service, jobs, thread_id, attachment_id = _worker_stack()
    request = service.request_preparation(thread_id, attachment_id)
    assert request.state == "pending"
    _no_leaked_worker_thread.append(worker)

    thread = worker.start()
    try:
        _wait_until(lambda: service.status(attachment_id).status == "ready")
    finally:
        worker.stop(timeout=10.0)

    assert not thread.is_alive()
    record = service.store.get(attachment_id)
    assert record is not None
    assert jobs.incomplete() == ()
    job = jobs.get(attachment_id=attachment_id, content_sha256=record.sha256)
    assert job is not None
    assert job.state == "succeeded"


# ----- drain-failure posture: keep the loop, never silent death -----


def test_failed_drain_is_logged_and_the_loop_keeps_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    calls: list[int] = []

    class _FlakyService:
        def drain_attachment_jobs(self) -> int:
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("durable store is briefly down")
            stop.set()
            return 0

    thread = threading.Thread(
        target=_drain_attachment_queue,
        args=(_FlakyService(), stop, 0.01),
    )
    with caplog.at_level(logging.ERROR, logger="fin.host.attachment_prep_worker"):
        thread.start()
        thread.join(timeout=10.0)

    assert not thread.is_alive()
    assert len(calls) == 2
    failure_records = [
        record
        for record in caplog.records
        if record.name == "fin.host.attachment_prep_worker" and record.exc_info
    ]
    assert failure_records
    assert isinstance(failure_records[0].exc_info[1], RuntimeError)
