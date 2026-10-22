"""The attachment job store on real SQL rows: durability across a process.

The in-memory twin's contract suite covers the state machine; this file
covers the parts only a real store can prove — the single-winner claim
CAS across TWO store instances (two processes), an enqueue join that
holds across instances, and job rows that survive into a new instance so
a restart resumes unfinished work.

Skipped when ``FIN_CHECKPOINT_URL`` is unset, the repo's convention for
Postgres-backed tests (same as
``tests/host/test_thread_attachment_durable_postgres.py``).
"""

from __future__ import annotations

import os
import threading
from typing import Iterator

import pytest

from fin.host.attachment.attachment_job_store import (
    PostgresThreadAttachmentJobStore,
    ThreadAttachmentJob,
)

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def postgres_url() -> str:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; Postgres-backed attachment job "
            "durability is untested in this environment"
        )
    _clear(url)
    yield url
    _clear(url)


def _clear(postgres_url: str) -> None:
    import psycopg

    with psycopg.connect(postgres_url) as conn:
        # Construction owns schema creation; a fresh store instance
        # guarantees the table exists before the first delete.
        PostgresThreadAttachmentJobStore(postgres_url)
        conn.execute("DELETE FROM fin_thread_attachment_job")
        conn.commit()


def test_enqueue_joins_across_store_instances(postgres_url: str) -> None:
    first = PostgresThreadAttachmentJobStore(postgres_url)
    second = PostgresThreadAttachmentJobStore(postgres_url)

    job = first.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    joined = second.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=1
    )

    assert job.state == "pending"
    assert joined.state == "pending"
    assert len(second.incomplete()) == 1


def test_claim_across_two_instances_has_exactly_one_winner(
    postgres_url: str
) -> None:
    store = PostgresThreadAttachmentJobStore(postgres_url)
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    claimants = [
        PostgresThreadAttachmentJobStore(postgres_url) for _ in range(4)
    ]
    barrier = threading.Barrier(len(claimants))
    winners: list[ThreadAttachmentJob] = []
    lock = threading.Lock()

    def claim(claimant: PostgresThreadAttachmentJobStore) -> None:
        barrier.wait()
        job = claimant.claim_next()
        if job is not None:
            with lock:
                winners.append(job)

    threads = [
        threading.Thread(target=claim, args=(claimant,)) for claimant in claimants
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert len(winners) == 1
    assert winners[0].state == "running"


def test_job_rows_survive_into_a_new_process(postgres_url: str) -> None:
    store = PostgresThreadAttachmentJobStore(postgres_url)
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    claimed = store.claim_next()  # the executor of this era dies here
    assert claimed is not None

    restarted = PostgresThreadAttachmentJobStore(postgres_url)
    assert restarted.requeue_running() == 1
    adopted = restarted.claim_next()
    assert adopted is not None
    assert adopted.attachment_id == "att-1"

    restarted.mark_succeeded(attachment_id="att-1", content_sha256=SHA_A)
    fresh = PostgresThreadAttachmentJobStore(postgres_url)
    later = fresh.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=1
    )
    assert later.state == "succeeded"  # a succeeded job is never re-run


def test_forget_removes_only_its_own_attachment(postgres_url: str) -> None:
    store = PostgresThreadAttachmentJobStore(postgres_url)
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.enqueue(attachment_id="att-2", content_sha256=SHA_B, target_version=1)

    assert store.forget(attachment_id="att-1") == 1

    remaining = store.incomplete()
    assert [job.attachment_id for job in remaining] == ["att-2"]
