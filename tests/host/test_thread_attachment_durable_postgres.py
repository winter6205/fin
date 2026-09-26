"""Thread attachments on real SQL rows: durability across a process.

The InMemory twin covers the lifecycle; this file covers the part that
only a real store can prove — metadata, frozen units, and derived
postings survive into a second store instance (a new process), a lost
derived layer is rebuilt at the SAME version, and the thread row's
foreign-key cascade removes every attachment row with it.

Skipped when ``FIN_CHECKPOINT_URL`` is unset, the repo's convention for
Postgres-backed tests. The byte layer stays the InMemory object store:
the S3 adapter's own contract is covered in ``tests/corpus``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterator

import pytest

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment_index import search
from fin.host.attachment_prepare import AttachmentPreparationService
from fin.host.attachment_store import PostgresAttachmentStore
from fin.host.thread_store import PostgresThreadStore

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

REVENUE_TEXT = (
    "Q3 revenue was 89,493 million dollars.\n"
    "Operating cash flow stayed strong across the quarter.\n"
)


@pytest.fixture
def postgres_url() -> str:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; Postgres-backed attachment "
            "durability is untested in this environment"
        )
    return url


@pytest.fixture
def stack(postgres_url: str) -> Iterator[dict[str, Any]]:
    """A real thread store, attachment store, and byte store on one URL."""
    _drop_all_threads(postgres_url)
    threads = PostgresThreadStore(postgres_url)
    objects = InMemoryObjectStore()
    yield {
        "url": postgres_url,
        "threads": threads,
        "objects": objects,
        "service": AttachmentPreparationService(
            store=PostgresAttachmentStore(postgres_url), objects=objects
        ),
    }
    _drop_all_threads(postgres_url)


def _drop_all_threads(postgres_url: str) -> None:
    """Empty the thread table; attachment rows leave with it by cascade.

    Both sides of the fixture do this: a run killed before its teardown would
    otherwise decide whether the next run's row counts hold.
    """
    import psycopg

    with psycopg.connect(postgres_url) as conn:
        conn.execute("DELETE FROM fin_thread")
        conn.commit()


def _boot(stack: dict[str, Any]) -> AttachmentPreparationService:
    """A restarted process: fresh SQL store instances over the same rows.

    The byte layer stays shared because this suite has no second object-store
    endpoint; the S3 adapter's own contract is covered in ``tests/corpus``.
    """
    return AttachmentPreparationService(
        store=PostgresAttachmentStore(stack["url"]), objects=stack["objects"]
    )


def _counts(stack: dict[str, Any]) -> tuple[int, int, int]:
    import psycopg

    with psycopg.connect(stack["url"]) as conn:
        rows = conn.execute(
            "SELECT"
            " (SELECT count(*) FROM fin_thread_attachment),"
            " (SELECT count(*) FROM fin_thread_attachment_unit),"
            " (SELECT count(*) FROM fin_thread_attachment_index)"
        ).fetchone()
    return (int(rows[0]), int(rows[1]), int(rows[2]))


def _attach(stack: dict[str, Any]) -> str:
    thread = stack["threads"].create_thread(title="pg durable")
    record = stack["service"].accept(
        thread_id=thread.id, filename="notes.txt", payload=REVENUE_TEXT.encode("utf-8")
    )
    return record.attachment_id


# ----- durable ready state across a process boundary -----


def test_ready_attachment_is_read_back_by_a_new_process(
    stack: dict[str, Any]
) -> None:
    attachment_id = _attach(stack)
    prepared = stack["service"].prepare(attachment_id)
    assert prepared.status == "ready"
    assert _counts(stack)[0] == 1

    restarted = _boot(stack)
    restored = restarted.status(attachment_id)
    assert restored is not None
    assert restored.status == "ready"
    assert restored.searchable is True
    assert restored.version == prepared.version
    assert restored.object_key == prepared.object_key
    assert restarted.units(attachment_id) == stack["service"].units(attachment_id)

    postings = restarted.store.index_postings(attachment_id, restored.version)
    hits = search(postings, "revenue")
    assert [hit.unit for hit in hits] == [1]


def test_a_parsing_row_is_resumed_by_the_next_process(
    stack: dict[str, Any]
) -> None:
    attachment_id = _attach(stack)
    # ``uploaded`` is a resting state: durable bytes nobody asked to
    # prepare. The row a new process must resume is one whose preparation
    # started and then lost its derived layer, so that is the premise.
    assert stack["service"].status(attachment_id).status == "uploaded"
    assert _counts(stack) == (1, 0, 0)

    stack["service"].prepare(attachment_id)
    stack["service"].store.clear_index(attachment_id)

    restarted = _boot(stack)
    assert restarted.status(attachment_id).status == "parsing"
    assert restarted.recover() == 1
    assert restarted.status(attachment_id).status == "ready"


def test_a_lost_index_is_rebuilt_at_the_same_version(
    stack: dict[str, Any]
) -> None:
    attachment_id = _attach(stack)
    prepared = stack["service"].prepare(attachment_id)
    stack["service"].store.clear_index(attachment_id)

    restarted = _boot(stack)
    stale = restarted.status(attachment_id)
    assert stale.status == "parsing"
    assert stale.searchable is False

    assert restarted.recover() == 1
    restored = restarted.status(attachment_id)
    assert restored.status == "ready"
    assert restored.version == prepared.version
    assert search(
        restarted.store.index_postings(attachment_id, restored.version), "cash"
    )


def test_rebuilding_without_its_bytes_names_the_exit(
    stack: dict[str, Any]
) -> None:
    attachment_id = _attach(stack)
    record = stack["service"].prepare(attachment_id)
    stack["service"].store.clear_index(attachment_id)
    stack["objects"].delete(record.object_key)

    restarted = _boot(stack)
    assert restarted.recover() == 1
    failed = restarted.status(attachment_id)
    assert failed.status == "failed"
    assert failed.failure_reason == "source_unavailable"
    # A rebuild that cannot read the bytes never publishes an empty index.
    assert restarted.store.index_postings(attachment_id, failed.version) == {}


# ----- deletion -----


def test_deleting_the_thread_removes_every_attachment_row(
    stack: dict[str, Any]
) -> None:
    thread = stack["threads"].create_thread(title="pg durable")
    attachment_id = stack["service"].accept(
        thread_id=thread.id,
        filename="notes.txt",
        payload=REVENUE_TEXT.encode("utf-8"),
    ).attachment_id
    stack["service"].prepare(attachment_id)
    assert _counts(stack)[1] >= 1 and _counts(stack)[2] >= 1

    stack["threads"].delete_thread(thread.id)

    assert _counts(stack) == (0, 0, 0)
    assert _boot(stack).status(attachment_id) is None


# ----- re-running against a database left dirty -----


def test_an_interrupted_run_leaves_attachment_rows(postgres_url: str) -> None:
    """Leave a thread and its ready attachment behind, as a killed process would.

    This case takes the URL only, so no fixture cleans the rows it writes: the
    next case must decide whether the suite can start from a dirty database.
    """
    import psycopg

    thread_id = f"interrupted-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(postgres_url) as conn:
        conn.execute(
            "INSERT INTO fin_thread (id, title, created_at) VALUES (%s, %s, now())",
            (thread_id, "interrupted run"),
        )
        conn.execute(
            "INSERT INTO fin_thread_attachment (attachment_id, thread_id, filename,"
            " size_bytes, sha256, object_key, status)"
            " VALUES (%s, %s, 'old.txt', 1, %s, %s, 'ready')",
            (f"orphan-{thread_id}", thread_id, "0" * 64, f"fin/{thread_id}/old.txt"),
        )
        conn.commit()


def test_a_fresh_run_clears_rows_left_behind(
    postgres_url: str, stack: dict[str, Any]
) -> None:
    """The suite is restartable: rows from an interrupted run do not decide it."""
    assert _counts(stack) == (0, 0, 0)
