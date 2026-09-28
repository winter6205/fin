"""The durable attachment preparation job store: join, claim CAS, reopen rules.

The job is the unit of preparation work keyed by ``(attachment_id,
content_sha256)``. What this file pins is the store contract itself,
because the start-or-join promise the graph gate codes against lives or
dies here:

* ``enqueue`` creates ONE row per key and joins the existing row on a
  repeat call — concurrent duplicate requests can never mint a second
  active job;
* ``claim_next`` is a single-winner CAS: among racing claimants exactly
  one leaves with the job;
* terminal states are honest: a ``succeeded`` job is never re-run by an
  enqueue, a ``failed`` job is reopened by a fresh enqueue (the caller
  only enqueues when the attachment row itself asked again), ``voided``
  is terminal;
* ``requeue_running`` is the boot sweep that re-adopts jobs whose
  executor died, ``incomplete`` is the same read, and ``forget`` is the
  deletion cleanup.
"""

from __future__ import annotations

import threading
from typing import Callable

import pytest

from fin.host.attachment_job_store import (
    InMemoryThreadAttachmentJobStore,
    ThreadAttachmentJob,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _race(workers: int, body: Callable[[int], object]) -> list[object]:
    """Run ``body`` on ``workers`` threads released at the same instant."""
    barrier = threading.Barrier(workers)
    results: list[object] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        outcome = body(index)
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=run, args=(index,)) for index in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert len(results) == workers
    return results


# ----- enqueue: start-or-join -----


def test_enqueue_creates_one_pending_job_with_the_requested_version() -> None:
    store = InMemoryThreadAttachmentJobStore()

    job = store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)

    assert isinstance(job, ThreadAttachmentJob)
    assert job.attachment_id == "att-1"
    assert job.content_sha256 == SHA_A
    assert job.target_version == 1
    assert job.state == "pending"
    assert job.failure_reason is None
    assert store.incomplete() == (job,)


def test_a_repeat_enqueue_joins_the_existing_job() -> None:
    store = InMemoryThreadAttachmentJobStore()
    first = store.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=1
    )

    second = store.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=1
    )

    assert second.state == first.state
    assert len(store.incomplete()) == 1


def test_concurrent_duplicate_requests_never_create_a_second_job() -> None:
    store = InMemoryThreadAttachmentJobStore()

    _race(
        16,
        lambda _: store.enqueue(
            attachment_id="att-1", content_sha256=SHA_A, target_version=1
        ),
    )

    pending = store.incomplete()
    assert len(pending) == 1
    assert pending[0].attachment_id == "att-1"


def test_different_content_keys_are_different_jobs() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)

    other = store.enqueue(
        attachment_id="att-1", content_sha256=SHA_B, target_version=2
    )

    assert other.state == "pending"
    assert len(store.incomplete()) == 2


# ----- claim: single winner -----


def test_claim_next_moves_pending_to_running_and_empty_is_none() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)

    job = store.claim_next()

    assert job is not None
    assert job.state == "running"
    assert store.claim_next() is None


def test_racing_claims_have_exactly_one_winner() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)

    outcomes = _race(8, lambda _: store.claim_next())

    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1


# ----- terminal transitions and the reopen rule -----


def test_a_succeeded_job_is_terminal_and_is_not_rerun_by_enqueue() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    claimed = store.claim_next()
    assert claimed is not None

    store.mark_succeeded(attachment_id="att-1", content_sha256=SHA_A)

    joined = store.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=1
    )
    assert joined.state == "succeeded"
    assert store.claim_next() is None


def test_a_failed_job_carries_its_reason_and_reopens_on_a_fresh_enqueue() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.claim_next()

    store.mark_failed(
        attachment_id="att-1", content_sha256=SHA_A, reason_code="attachment_empty_text"
    )
    failed = store.get(attachment_id="att-1", content_sha256=SHA_A)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.failure_reason == "attachment_empty_text"

    reopened = store.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=2
    )
    assert reopened.state == "pending"
    assert reopened.failure_reason is None
    assert reopened.target_version == 2
    assert store.claim_next() is not None


def test_mark_voided_is_terminal_for_every_later_enqueue() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)

    store.mark_voided(attachment_id="att-1", content_sha256=SHA_A)

    joined = store.enqueue(
        attachment_id="att-1", content_sha256=SHA_A, target_version=1
    )
    assert joined.state == "voided"
    assert store.claim_next() is None


def test_reopen_moves_a_succeeded_job_back_to_pending() -> None:
    """The repair seam the service uses when the row lost its derived data."""
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.claim_next()
    store.mark_succeeded(attachment_id="att-1", content_sha256=SHA_A)

    reopened = store.reopen(
        attachment_id="att-1", content_sha256=SHA_A, target_version=2
    )

    assert reopened.state == "pending"
    assert reopened.target_version == 2
    assert store.claim_next() is not None


# ----- boot sweep, incomplete read, deletion cleanup -----


def test_requeue_running_re_adopts_orphans_and_reports_the_count() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.enqueue(attachment_id="att-2", content_sha256=SHA_A, target_version=1)
    assert store.claim_next() is not None
    assert store.claim_next() is not None

    reset = store.requeue_running()

    assert reset == 2
    assert {job.state for job in store.incomplete()} == {"pending"}


def test_incomplete_covers_pending_and_running_by_default() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.enqueue(attachment_id="att-2", content_sha256=SHA_A, target_version=1)
    store.enqueue(attachment_id="att-3", content_sha256=SHA_A, target_version=1)
    claimed = store.claim_next()
    assert claimed is not None
    assert store.claim_next() is not None  # att-2 must be RUNNING to finish
    store.mark_succeeded(attachment_id="att-2", content_sha256=SHA_A)

    active = store.incomplete()
    only_pending = store.incomplete(states=("pending",))

    assert {job.attachment_id for job in active} == {"att-1", "att-3"}
    assert [job.attachment_id for job in only_pending] == ["att-3"]


def test_forget_removes_only_that_attachment_s_rows() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.enqueue(attachment_id="att-1", content_sha256=SHA_B, target_version=1)
    store.enqueue(attachment_id="att-2", content_sha256=SHA_A, target_version=1)

    forgotten = store.forget(attachment_id="att-1")

    assert forgotten == 2
    assert store.get(attachment_id="att-1", content_sha256=SHA_A) is None
    assert [job.attachment_id for job in store.incomplete()] == ["att-2"]


# ----- input contracts at the public entry -----


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attachment_id": "", "content_sha256": SHA_A, "target_version": 1},
        {"attachment_id": "  ", "content_sha256": SHA_A, "target_version": 1},
        {"attachment_id": "att-1", "content_sha256": "", "target_version": 1},
        {"attachment_id": "att-1", "content_sha256": SHA_A, "target_version": 0},
        {"attachment_id": "att-1", "content_sha256": SHA_A, "target_version": -3},
    ],
)
def test_enqueue_refuses_blank_ids_and_non_positive_versions(
    kwargs: dict[str, object],
) -> None:
    store = InMemoryThreadAttachmentJobStore()

    with pytest.raises(ValueError):
        store.enqueue(**kwargs)  # type: ignore[arg-type]


def test_mark_failed_refuses_a_blank_reason_code() -> None:
    store = InMemoryThreadAttachmentJobStore()
    store.enqueue(attachment_id="att-1", content_sha256=SHA_A, target_version=1)
    store.claim_next()

    with pytest.raises(ValueError):
        store.mark_failed(attachment_id="att-1", content_sha256=SHA_A, reason_code="")
