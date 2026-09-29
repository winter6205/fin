"""The job-facing half of the preparation door: start-or-join, drain, restart.

``request_preparation`` is the graph gate's whole question — "is this
attachment's content usable, and if not, is durable work asked for it?" —
so every branch of its state machine is pinned here against the
in-memory twins (hermetic by the suite's rule):

* unknown / other-thread / tombstoned / blank or non-string locators all
  answer ``unknown`` + ``unknown_locator`` and reveal nothing;
* a searchable row answers ``ready`` with its version and mints no job;
* an ``uploaded`` row answers ``pending``, mints exactly ONE job, and is
  the first producer of a stored ``parsing`` status;
* a ``failed`` row answers ``failed`` with its stored reason and mints
  no job;
* ``drain_attachment_jobs`` executes claimed jobs through the existing
  preparation: success publishes, typed failure lands the code on both
  row and job, a crashed job never leaves the row stuck, a preparation
  that lands neither published, failed, nor gone fails the job as
  ``attachment_index_unavailable`` (never an empty success), and a
  tombstoned attachment voids its job without publishing anything;
* a join that finds a ``succeeded`` job whose published data the row no
  longer carries reopens it through the named repair seam, so the next
  drain re-publishes instead of parking a pending that never drains;
* ``retry_preparation`` re-asks a named failure through the SAME
  enqueue/reopen/parse-transition block the request door uses;
* ``recover`` re-adopts a job whose executor died, so a restart resumes
  durable work; plain ``uploaded`` rows are still not touched.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import pytest

from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.object_store_errors import UnknownObjectKeyError
from fin.host.attachment_job_store import InMemoryThreadAttachmentJobStore
from fin.host.attachment_prepare import (
    ATTACHMENT_INDEX_UNAVAILABLE,
    ATTACHMENT_NOT_TEXT,
    UNKNOWN_LOCATOR,
    AttachmentPreparationService,
)
from fin.host.attachment_store import (
    InMemoryAttachmentStore,
    PostgresAttachmentStore,
)
from fin.host.thread_store import InMemoryThreadStore, PostgresThreadStore

REVENUE_TEXT = b"Q3 revenue was 89,493 million dollars.\n"
NOT_TEXT_BYTES = b"\xff\xfe\x00\x01binary not utf-8"

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


@pytest.fixture
def postgres_url() -> str:
    """Same opt-in convention as the other Postgres-backed host suites."""
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the Postgres twin of the "
            "gate's stored ``parsing`` transition is untested here"
        )
    return url


class _Stack:
    """One thread, one service, and the job store it was wired with."""

    def __init__(
        self,
        threads: InMemoryThreadStore,
        service: AttachmentPreparationService,
        jobs: InMemoryThreadAttachmentJobStore,
        thread_id: str,
    ) -> None:
        self.threads = threads
        self.service = service
        self.jobs = jobs
        self.thread_id = thread_id


def _stack(**service_kwargs: Any) -> _Stack:
    threads = InMemoryThreadStore()
    jobs = InMemoryThreadAttachmentJobStore()
    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads),
        objects=InMemoryObjectStore(),
        jobs=jobs,
        **service_kwargs,
    )
    thread = threads.create_thread(title="jobs")
    return _Stack(threads, service, jobs, thread.id)


def _upload(stack: _Stack, payload: bytes = REVENUE_TEXT, filename: str = "notes.txt") -> str:
    record = stack.service.accept(
        thread_id=stack.thread_id, filename=filename, payload=payload
    )
    return record.attachment_id


# ----- request_preparation: the state machine -----


@pytest.mark.parametrize("attachment_id", ["never-existed", "", "   ", 42, None])
def test_unknown_locators_answer_unknown_and_reveal_nothing(
    attachment_id: object,
) -> None:
    stack = _stack()

    request = stack.service.request_preparation(stack.thread_id, attachment_id)  # type: ignore[arg-type]

    assert request.state == "unknown"
    assert request.code == UNKNOWN_LOCATOR
    assert request.version == 0
    assert stack.jobs.incomplete() == ()


def test_another_thread_s_attachment_reveals_nothing() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    other = stack.threads.create_thread(title="other")

    request = stack.service.request_preparation(other.id, attachment_id)

    assert (request.state, request.code, request.version) == (
        "unknown",
        UNKNOWN_LOCATOR,
        0,
    )
    assert stack.jobs.incomplete() == ()


def test_a_deleted_attachment_is_unknown() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.delete_attachment(
        thread_id=stack.thread_id, attachment_id=attachment_id
    )

    request = stack.service.request_preparation(stack.thread_id, attachment_id)

    assert (request.state, request.code) == ("unknown", UNKNOWN_LOCATOR)


def test_a_ready_attachment_answers_ready_with_its_version() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    prepared = stack.service.prepare(attachment_id)

    request = stack.service.request_preparation(stack.thread_id, attachment_id)

    assert request.state == "ready"
    assert request.code is None
    assert request.version == prepared.version
    assert stack.jobs.incomplete() == ()


def test_an_uploaded_attachment_starts_exactly_one_job_and_stores_parsing() -> None:
    stack = _stack()
    attachment_id = _upload(stack)

    request = stack.service.request_preparation(stack.thread_id, attachment_id)

    assert request.state == "pending"
    assert request.code is None
    assert request.version == 0
    stored = stack.service.store.get(attachment_id)
    assert stored is not None
    assert stored.status == "parsing"  # THE first producer of a stored parsing
    jobs = stack.jobs.incomplete()
    assert len(jobs) == 1
    assert jobs[0].attachment_id == attachment_id
    assert jobs[0].state == "pending"


def test_concurrent_requests_join_one_job() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    barrier = threading.Barrier(8)
    requests = []
    lock = threading.Lock()

    def ask() -> None:
        barrier.wait()
        outcome = stack.service.request_preparation(stack.thread_id, attachment_id)
        with lock:
            requests.append(outcome)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert [request.state for request in requests] == ["pending"] * 8
    assert len(stack.jobs.incomplete()) == 1


def test_a_failed_row_answers_with_its_reason_and_mints_no_job() -> None:
    stack = _stack()
    attachment_id = _upload(stack, payload=NOT_TEXT_BYTES)
    failed = stack.service.prepare(attachment_id)
    assert failed.status == "failed"

    request = stack.service.request_preparation(stack.thread_id, attachment_id)

    assert request.state == "failed"
    assert request.code == ATTACHMENT_NOT_TEXT
    assert stack.jobs.incomplete() == ()


# ----- retry_preparation: the operator door over the same block -----


def test_retry_preparation_moves_the_failed_row_and_requeues_the_job() -> None:
    """The retry door shares the request door's enqueue/reopen/transition.

    Pins the whole ``_ask_or_join`` contract from the retry side: a named
    ``failed`` row re-enqueues (the job store reopens its ``failed`` job),
    the row moves ``failed -> parsing`` with its retired reason cleared,
    and the answer is the same ``pending`` the request door gives.
    """
    stack = _stack()
    attachment_id = _upload(stack, payload=NOT_TEXT_BYTES)
    failed = stack.service.prepare(attachment_id)
    stack.service.request_preparation(stack.thread_id, attachment_id)  # -> failed exit
    assert stack.jobs.get(
        attachment_id=attachment_id, content_sha256=failed.sha256
    ) is None

    request = stack.service.retry_preparation(stack.thread_id, attachment_id)

    assert (request.state, request.code) == ("pending", None)
    stored = stack.service.store.get(attachment_id)
    assert stored is not None
    assert stored.status == "parsing"
    assert stored.failure_reason is None
    jobs = stack.jobs.incomplete()
    assert len(jobs) == 1
    assert jobs[0].state == "pending"
    assert stack.service.drain_attachment_jobs() == 1
    record = stack.service.store.get(attachment_id)
    assert record is not None
    assert record.status == "failed"
    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=record.sha256)
    assert job is not None
    assert job.state == "failed"
    assert job.failure_reason == ATTACHMENT_NOT_TEXT


def test_retry_preparation_on_a_ready_row_answers_ready() -> None:
    """Non-failed states answer exactly what the request door answers."""
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.prepare(attachment_id)

    request = stack.service.retry_preparation(stack.thread_id, attachment_id)

    assert request.state == "ready"
    assert stack.jobs.incomplete() == ()


# ----- drain_attachment_jobs: execution of claimed work -----


def test_drain_publishes_and_the_later_request_is_ready() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.request_preparation(stack.thread_id, attachment_id)

    drained = stack.service.drain_attachment_jobs()

    assert drained == 1
    record = stack.service.status(attachment_id)
    assert record is not None
    assert record.status == "ready"
    assert record.searchable is True
    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=record.sha256)
    assert job is not None
    assert job.state == "succeeded"
    assert stack.service.request_preparation(
        stack.thread_id, attachment_id
    ).state == "ready"


def test_drain_with_no_jobs_is_zero_and_touches_nothing() -> None:
    stack = _stack()
    attachment_id = _upload(stack)

    assert stack.service.drain_attachment_jobs() == 0
    assert stack.service.store.get(attachment_id).status == "uploaded"


def test_drain_honours_the_limit() -> None:
    stack = _stack()
    for _ in range(3):
        stack.service.request_preparation(stack.thread_id, _upload(stack))

    assert stack.service.drain_attachment_jobs(limit=2) == 2
    assert len(stack.jobs.incomplete()) == 1


def test_a_typed_failure_lands_on_row_and_job_not_an_empty_success() -> None:
    stack = _stack()
    attachment_id = _upload(stack, payload=NOT_TEXT_BYTES)
    stack.service.request_preparation(stack.thread_id, attachment_id)

    assert stack.service.drain_attachment_jobs() == 1

    record = stack.service.store.get(attachment_id)
    assert record is not None
    assert record.status == "failed"
    assert record.failure_reason == ATTACHMENT_NOT_TEXT
    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=record.sha256)
    assert job is not None
    assert job.state == "failed"
    assert job.failure_reason == ATTACHMENT_NOT_TEXT
    # A failed drain mints no published derived data.
    assert stack.service.units(attachment_id) == ()


def test_a_crashed_job_fails_the_row_with_a_typed_reason() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.request_preparation(stack.thread_id, attachment_id)

    def boom(_: str) -> Any:
        raise RuntimeError("executor crashed mid-run")

    monkey_target = stack.service
    original = monkey_target.prepare
    try:
        monkey_target.prepare = boom  # type: ignore[method-assign]
        assert monkey_target.drain_attachment_jobs() == 1
    finally:
        monkey_target.prepare = original  # type: ignore[method-assign]

    record = monkey_target.store.get(attachment_id)
    assert record is not None
    assert record.status == "failed"
    assert record.failure_reason == "source_unavailable"
    jobs = stack.jobs.incomplete()
    assert jobs == ()


def test_a_crashed_job_logs_the_originating_exception(caplog: pytest.LogCaptureFixture) -> None:
    """The crash belt never swallows the originating exception silently.

    ``_run_job``'s blanket ``except Exception`` names a typed failure the
    door can still say, but the row's reason is only a code: without the
    exception log, an operator reading ``source_unavailable`` cannot tell
    a crashed preparation from a storage outage (the exact gap the that pull request
    live door hit). The record must therefore carry the traceback.
    """
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.request_preparation(stack.thread_id, attachment_id)

    def boom(_: str) -> Any:
        raise RuntimeError("executor crashed mid-run")

    original = stack.service.prepare
    try:
        stack.service.prepare = boom  # type: ignore[method-assign]
        with caplog.at_level("ERROR", logger="fin.host.attachment_prepare"):
            assert stack.service.drain_attachment_jobs() == 1
    finally:
        stack.service.prepare = original  # type: ignore[method-assign]

    crash_records = [
        r
        for r in caplog.records
        if r.name == "fin.host.attachment_prepare"
        and r.exc_info is not None
        and r.exc_info[0] is RuntimeError
    ]
    assert crash_records, [
        (r.name, r.levelname, r.getMessage()) for r in caplog.records
    ]
    # The traceback must carry the exception's own text, not just its type.
    assert "executor crashed mid-run" in caplog.text
    assert attachment_id in caplog.text


def test_unreadable_bytes_log_the_store_exception_with_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``source_unavailable`` row must stay attributable to its cause.

    The reason is deliberately coarse — a missing key, a dead endpoint,
    and a credentials refusal all publish it — so the store exception is
    the only thing that distinguishes them. This is the leg the live
    that pull request door hit: a second process with a different bucket claimed
    the job, and the row read as an unexplained storage outage.
    """
    stack = _stack()
    attachment_id = _upload(stack)
    record = stack.service.store.get(attachment_id)

    def refuse(key: str) -> bytes:
        raise UnknownObjectKeyError(f"unknown object key: {key}")

    original = stack.service.objects.get
    try:
        stack.service.objects.get = refuse  # type: ignore[method-assign]
        with caplog.at_level("ERROR", logger="fin.host.attachment_prepare"):
            prepared = stack.service.prepare(attachment_id)
    finally:
        stack.service.objects.get = original  # type: ignore[method-assign]

    assert prepared.status == "failed"
    assert prepared.failure_reason == "source_unavailable"
    store_records = [
        r
        for r in caplog.records
        if r.name == "fin.host.attachment_prepare" and r.exc_info is not None
    ]
    assert store_records, [(r.name, r.getMessage()) for r in caplog.records]
    assert any(
        r.exc_info[0] is UnknownObjectKeyError for r in store_records
    ), [r.exc_info[0].__name__ for r in store_records if r.exc_info]
    assert record is not None
    assert record.object_key in caplog.text


def test_a_preparation_that_never_lands_fails_the_job_index_unavailable() -> None:
    """The drain's terminal else: a row neither published, failed, nor gone.

    ``prepare`` returning without landing any of the three outcomes is a
    broken promise, not a success: the job takes the storage exit
    ``attachment_index_unavailable`` and the sweep never blesses the row
    with an empty success.
    """
    stack = _stack()
    attachment_id = _upload(stack)
    sha = stack.service.store.get(attachment_id).sha256
    stack.service.request_preparation(stack.thread_id, attachment_id)

    original = stack.service.prepare

    def no_landing(_: str) -> Any:
        # Leaves the row exactly as the ask found it: ``parsing``, no
        # derived data — the shape the terminal else must refuse.
        return stack.service.store.get(attachment_id)

    stack.service.prepare = no_landing  # type: ignore[method-assign]
    try:
        assert stack.service.drain_attachment_jobs() == 1
    finally:
        stack.service.prepare = original  # type: ignore[method-assign]

    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=sha)
    assert job is not None
    assert job.state == "failed"
    assert job.failure_reason == ATTACHMENT_INDEX_UNAVAILABLE
    record = stack.service.status(attachment_id)
    assert record is not None
    assert record.searchable is False
    assert stack.service.units(attachment_id) == ()


# ----- repair: a succeeded job whose published data went missing -----


def test_a_succeeded_job_that_lost_its_derived_data_reopens_and_republishes() -> None:
    """The request door's ``succeeded``-join branch, produced end to end.

    The job says published; the row can no longer honor that (its units
    and postings went missing afterwards). The join must take the named
    ``reopen`` decision — not a pending that never drains, because the
    job store will not re-run a ``succeeded`` row on an ordinary
    enqueue — and the next drain re-publishes the content.
    """
    stack = _stack()
    attachment_id = _upload(stack)
    sha = stack.service.store.get(attachment_id).sha256
    stack.service.request_preparation(stack.thread_id, attachment_id)
    assert stack.service.drain_attachment_jobs() == 1
    published = stack.service.store.get(attachment_id)
    assert published is not None
    assert published.searchable

    # Damage the published derived data through the store's public doors.
    stack.service.store.clear_units(attachment_id)
    stack.service.store.clear_index(attachment_id)

    request = stack.service.request_preparation(stack.thread_id, attachment_id)

    assert request.state == "pending"
    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=sha)
    assert job is not None
    assert job.state == "pending"  # reopened, not the old succeeded terminal
    assert stack.service.drain_attachment_jobs() == 1
    record = stack.service.status(attachment_id)
    assert record is not None
    assert record.status == "ready"
    assert record.searchable is True
    assert stack.service.units(attachment_id)
    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=sha)
    assert job is not None
    assert job.state == "succeeded"


def test_drain_after_the_thread_row_dies_voids_the_job_and_publishes_nothing() -> None:
    """The interrupted-cleanup shape: the job outlives its thread's rows."""
    stack = _stack()
    attachment_id = _upload(stack)
    sha = stack.service.store.get(attachment_id).sha256
    stack.service.request_preparation(stack.thread_id, attachment_id)
    # The thread store, not the service door: rows are hidden exactly as
    # the FK cascade hides them, and no forget ran.
    stack.threads.delete_thread(stack.thread_id)

    assert stack.service.drain_attachment_jobs() == 1

    job = stack.jobs.get(attachment_id=attachment_id, content_sha256=sha)
    assert job is not None
    assert job.state == "voided"
    assert stack.service.store.get(attachment_id) is None
    assert stack.service.store.index_postings(attachment_id, 1) == {}


def test_deletion_mid_flight_retires_the_job_and_publishes_nothing() -> None:
    """The delete lands between the byte read and the derived write."""
    stack = _stack()
    attachment_id = _upload(stack)
    sha = stack.service.store.get(attachment_id).sha256
    stack.service.request_preparation(stack.thread_id, attachment_id)

    real_get = stack.service.objects.get
    deleted: list[bool] = []

    def get_then_delete(key: str) -> bytes:
        payload = real_get(key)
        if not deleted:
            deleted.append(True)
            stack.service.delete_attachment(
                thread_id=stack.thread_id, attachment_id=attachment_id
            )
        return payload

    stack.service.objects.get = get_then_delete  # type: ignore[method-assign]

    assert stack.service.drain_attachment_jobs() == 1

    # The confirmed cleanup forgot the row outright: a dead attachment
    # leaves no job to re-run, and the lost derived write is the store's
    # documented race outcome.
    assert stack.jobs.get(attachment_id=attachment_id, content_sha256=sha) is None
    assert stack.service.store.get(attachment_id) is None
    assert stack.service.store.index_postings(attachment_id, 1) == {}


# ----- recover: restart resumes durable work -----


def test_recover_re_adopts_a_running_job_left_by_a_dead_executor() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.request_preparation(stack.thread_id, attachment_id)
    claimed = stack.jobs.claim_next()  # an executor that then died
    assert claimed is not None
    assert claimed.state == "running"

    restarted = AttachmentPreparationService(
        store=stack.service.store,
        objects=stack.service.objects,
        jobs=stack.jobs,
    )
    restarted.recover()

    job = restarted.jobs.get(
        attachment_id=attachment_id, content_sha256=claimed.content_sha256
    )
    assert job is not None
    assert job.state == "pending"
    assert restarted.drain_attachment_jobs() == 1
    record = restarted.status(attachment_id)
    assert record is not None
    assert record.status == "ready"


def test_recover_still_leaves_plain_uploaded_rows_alone() -> None:
    stack = _stack()
    attachment_id = _upload(stack)

    assert stack.service.recover() == 0
    assert stack.service.store.get(attachment_id).status == "uploaded"
    assert stack.jobs.incomplete() == ()


def test_a_service_built_without_a_job_store_still_works() -> None:
    """Existing constructions are unchanged; the default store is in-memory."""
    threads = InMemoryThreadStore()
    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads), objects=InMemoryObjectStore()
    )
    thread = threads.create_thread(title="default")
    attachment_id = service.accept(
        thread_id=thread.id, filename="notes.txt", payload=REVENUE_TEXT
    ).attachment_id

    pending = service.request_preparation(thread.id, attachment_id)
    assert pending.state == "pending"
    assert service.drain_attachment_jobs() == 1
    assert service.status(attachment_id).status == "ready"


# ----- deletion cleans durable job rows -----


def test_confirming_deletion_forgets_the_attachment_s_jobs() -> None:
    stack = _stack()
    attachment_id = _upload(stack)
    stack.service.request_preparation(stack.thread_id, attachment_id)

    stack.service.delete_attachment(
        thread_id=stack.thread_id, attachment_id=attachment_id
    )

    assert stack.jobs.incomplete() == ()


def test_thread_deletion_forgets_every_job_of_the_thread() -> None:
    stack = _stack()
    first = _upload(stack)
    second = _upload(stack)
    stack.service.request_preparation(stack.thread_id, first)
    stack.service.request_preparation(stack.thread_id, second)

    stack.service.delete_thread(stack.thread_id)

    assert stack.jobs.incomplete() == ()


# ----- the Postgres twin of the gate: stored parsing on real SQL rows -----


def test_request_preparation_stores_parsing_on_a_real_row(
    postgres_url: str,
) -> None:
    """The ``uploaded -> parsing`` SQL branch is only provable on Postgres.

    The byte layer stays the in-memory object store, as in
    ``test_thread_attachment_durable_postgres.py``; everything the gate
    reads or writes is a real row.
    """
    threads = PostgresThreadStore(postgres_url)
    thread = threads.create_thread(title="pg gate")
    service = AttachmentPreparationService(
        store=PostgresAttachmentStore(postgres_url),
        objects=InMemoryObjectStore(),
        jobs=InMemoryThreadAttachmentJobStore(),
    )
    try:
        attachment_id = service.accept(
            thread_id=thread.id, filename="notes.txt", payload=REVENUE_TEXT
        ).attachment_id
        assert service.store.get(attachment_id).status == "uploaded"

        request = service.request_preparation(thread.id, attachment_id)

        assert request.state == "pending"
        stored = service.store.get(attachment_id)
        assert stored is not None
        assert stored.status == "parsing"  # written by the SQL transition
        # The gate's claim CAS is the job store's; the drain still owns
        # the row's whole lifecycle through the real attachment store.
        assert service.drain_attachment_jobs() == 1
        assert service.status(attachment_id).status == "ready"
        assert (
            service.request_preparation(thread.id, attachment_id).state == "ready"
        )
    finally:
        threads.delete_thread(thread.id)
