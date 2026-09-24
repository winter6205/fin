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
    import psycopg

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
    with psycopg.connect(postgres_url) as conn:
        conn.execute("DELETE FROM fin_thread")
        conn.commit()


def _boot(stack: dict[str, Any]) -> AttachmentPreparationService:
    """A fresh service over fresh store instances — a restarted process."""
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
    assert stack["service"].status(attachment_id).status == "parsing"
    assert _counts(stack) == (1, 0, 0)

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
