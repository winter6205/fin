"""Durable attachment-job boot sweep on real SQL rows (T2f, opt-in).

The in-memory resume-worker suite pins the sweep's BEHAVIOUR; the raw
Postgres job-store twin pins the state machine. What only a real
two-Postgres-instance run can prove — and what this file pins — is the
boot clause crossing an actual process boundary: a claim a dead process
left ``running`` and the attachment row it moved to ``parsing`` are
read back by a completely fresh ``PostgresAttachmentStore`` /
``PostgresThreadAttachmentJobStore`` stack, whose ``recover()`` +
``requeue_running`` re-adopts the work and a prepared service drains it
to ``ready``; a succeeded job is then never re-run; and two store
instances claiming concurrently have a single winner per job (the
``FOR UPDATE SKIP LOCKED`` CAS in action, over jobs a real
``request_preparation`` enqueued).

Skipped when ``FIN_CHECKPOINT_URL`` is unset, exactly the repo
convention of ``tests/host/test_thread_attachment_durable_postgres.py``
and ``tests/host/test_attachment_job_store_postgres.py``. The byte layer
stays the InMemory object store (the S3 adapter's own contract lives in
``tests/corpus``).
"""

from __future__ import annotations

import os
import threading
from typing import Iterator

import pytest

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment_job_store import (
    PostgresThreadAttachmentJobStore,
    ThreadAttachmentJob,
)
from fin.host.attachment_prepare import AttachmentPreparationService
from fin.host.attachment_store import PostgresAttachmentStore
from fin.host.thread_store import PostgresThreadStore

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

REVENUE_TEXT = (
    "Q3 revenue was 89,493 million dollars.\n"
    "Operating cash flow stayed strong across the quarter.\n"
)


@pytest.fixture
def postgres_url() -> Iterator[str]:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; Postgres-backed durable "
            "attachment-job boot is untested in this environment"
        )
    _clear(url)
    yield url
    _clear(url)


def _clear(postgres_url: str) -> None:
    """Empty the job table and the thread rows (attachments cascade).

    Both sides of the fixture do this, like the durable attachment
    suite: a run killed before its teardown must not decide whether the
    next run's row counts hold. Construction owns schema creation, so a
    fresh store instance guarantees each table exists before the first
    delete.
    """
    import psycopg

    PostgresThreadAttachmentJobStore(postgres_url)
    PostgresThreadStore(postgres_url)
    with psycopg.connect(postgres_url) as conn:
        conn.execute("DELETE FROM fin_thread_attachment_job")
        conn.execute("DELETE FROM fin_thread")
        conn.commit()


def _service(
    postgres_url: str, objects: InMemoryObjectStore
) -> AttachmentPreparationService:
    """One process's full attachment stack over the same DSN."""
    return AttachmentPreparationService(
        store=PostgresAttachmentStore(postgres_url),
        objects=objects,
        jobs=PostgresThreadAttachmentJobStore(postgres_url),
    )


def _attach_and_ask(
    service: AttachmentPreparationService, threads: PostgresThreadStore, name: str
) -> tuple[str, str, str]:
    """Accept a real file through the durable door and queue its job."""
    thread = threads.create_thread(title=name)
    record = service.accept(
        thread_id=thread.id,
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    )
    request = service.request_preparation(thread.id, record.attachment_id)
    assert request.state == "pending"
    return thread.id, record.attachment_id, record.sha256


# ----- the restart clause over real rows ------------------------------------


def test_a_dead_processes_claim_is_re_adopted_and_drained_by_the_next(
    postgres_url: str,
) -> None:
    """Instance A enqueues + claims; instance B recovers, drains, retires.

    The clause this pins end to end: job rows and the ``parsing``
    attachment status survive into brand-new store instances over the
    same DSN, ``recover()`` re-adopts the interrupted ``running`` claim
    (and rebuilds the half-prepared row), the drained job reaches
    ``succeeded``, and from then on nothing re-runs it — a repeated
    enqueue answers the terminal, a further drain claims zero, and the
    row is read back ``ready`` by yet another fresh instance.
    """
    objects = InMemoryObjectStore()
    threads = PostgresThreadStore(postgres_url)
    a = _service(postgres_url, objects)
    thread_id, attachment_id, sha256 = _attach_and_ask(a, threads, "pg boot durable")

    # The executor of this era dies holding the claim.
    claimed = a.jobs.claim_next()
    assert claimed is not None
    assert claimed.state == "running"

    # A new process: completely fresh SQL store instances over the same DSN.
    b = _service(postgres_url, objects)
    row = b.status(attachment_id)
    assert row is not None
    assert row.status == "parsing"  # the gate's ask survived the restart
    survivors = b.jobs.incomplete()
    assert [job.attachment_id for job in survivors] == [attachment_id]

    assert b.recover() == 1  # re-adopts the running claim AND rebuilds the row
    adopted = b.jobs.get(attachment_id=attachment_id, content_sha256=sha256)
    assert adopted is not None
    assert adopted.state == "pending"  # re-adopted, not duplicated

    assert b.drain_attachment_jobs() >= 1
    assert b.status(attachment_id).status == "ready"
    finished = b.jobs.get(attachment_id=attachment_id, content_sha256=sha256)
    assert finished is not None
    assert finished.state == "succeeded"

    # A succeeded job is never re-run: the join answers the terminal, a
    # further drain finds nothing, the settle answer is ready.
    joined = b.jobs.enqueue(
        attachment_id=attachment_id, content_sha256=sha256, target_version=99
    )
    assert joined.state == "succeeded"
    assert b.drain_attachment_jobs() == 0
    assert b.request_preparation(thread_id, attachment_id).state == "ready"

    third = _service(postgres_url, objects)
    assert third.status(attachment_id).status == "ready"
    survived = third.jobs.get(attachment_id=attachment_id, content_sha256=sha256)
    assert survived is not None
    assert survived.state == "succeeded"


# ----- the claim CAS over real rows, from two instances ---------------------


def test_two_store_instances_claim_concurrent_jobs_with_one_winner_each(
    postgres_url: str,
) -> None:
    """Two processes, two queued jobs: each job gets exactly one claimant.

    The jobs are the ones a real ``request_preparation`` enqueued (not
    hand-written rows), claimed through two distinct
    ``PostgresThreadAttachmentJobStore`` instances released off one
    barrier. A duplicate claim would be a double-parse of the same
    content; after both claims the queue is empty for a third instance.
    """
    objects = InMemoryObjectStore()
    threads = PostgresThreadStore(postgres_url)
    a = _service(postgres_url, objects)
    first = _attach_and_ask(a, threads, "pg claim 1")
    second = _attach_and_ask(a, threads, "pg claim 2")

    b = _service(postgres_url, objects)  # a second "process"
    claimants = [a.jobs, b.jobs]
    barrier = threading.Barrier(len(claimants))
    winners: list[ThreadAttachmentJob] = []
    lock = threading.Lock()

    def claim(store: PostgresThreadAttachmentJobStore) -> None:
        barrier.wait(timeout=30.0)
        job = store.claim_next()
        if job is not None:
            with lock:
                winners.append(job)

    racing = [threading.Thread(target=claim, args=(store,)) for store in claimants]
    for racer in racing:
        racer.start()
    for racer in racing:
        racer.join(timeout=30.0)

    assert len(winners) == 2
    keys = {(job.attachment_id, job.content_sha256) for job in winners}
    assert len(keys) == 2  # no job claimed twice
    assert all(job.state == "running" for job in winners)
    # The two winners are exactly the two queued jobs, in either order.
    assert keys == {(first[1], first[2]), (second[1], second[2])}

    # Nothing active is left for a third instance to pick up.
    c = _service(postgres_url, objects)
    assert c.jobs.claim_next() is None
