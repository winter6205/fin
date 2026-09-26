"""The thread-bound attachment storage contract (ADRs 0083/0085/0088).

These cases pin ``fin.host.attachment_store.AttachmentStore`` semantics:
liveness arbitration, derived-data-gated ``ready``, restart visibility, and
thread-cascade deletion. Both implementations must answer the same
assertions, so every in-memory contract has a Postgres twin.

Validation basis: specs/thread-attachment-source-read.md Boundaries item 1
and Success Criteria 2/3. The Postgres half follows the existing repo
convention (``tests/host/test_thread_store.py``): it is env-gated on
``FIN_CHECKPOINT_URL`` and skips when no Postgres is reachable, because a
green skip on an unavailable optional service is that file's contract, not
a substitute for it.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest

from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.thread_store import InMemoryThreadStore

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

# Two units and a posting table that mixes ASCII and CJK terms plus a
# multi-unit posting: the shapes that stress the comma-encoded unit column.
UNITS = (
    ParsedAttachmentUnit(unit=1, text="Q3 revenue was 89,493 million dollars."),
    ParsedAttachmentUnit(unit=2, text="营业收入 89,493 百万美元。"),
)
POSTINGS: dict[str, list[int]] = {"revenue": [1], "dollars": [1, 2], "营业收入": [2]}


def _threads_and_store() -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="Revenue thread")
    return threads, InMemoryAttachmentStore(threads), thread.id


def _create(
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    attachment_id: str | None = None,
    filename: str = "notes.txt",
) -> AttachmentRecord:
    aid = attachment_id or f"att-{uuid.uuid4().hex[:8]}"
    return store.create(
        thread_id=thread_id,
        attachment_id=aid,
        filename=filename,
        size_bytes=1_024,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/{aid}",
    )


def _prepare(
    store: InMemoryAttachmentStore, thread_id: str, *, version: int = 1
) -> AttachmentRecord:
    """An accepted row with BOTH derived layers written for ``version``."""
    record = _create(store, thread_id)
    store.record_units(attachment_id=record.attachment_id, version=version, units=UNITS)
    store.put_index(
        attachment_id=record.attachment_id, version=version, postings=POSTINGS
    )
    return record


def _force_parsing(store: InMemoryAttachmentStore, attachment_id: str) -> None:
    """Pin the stored shape of a row whose preparation has already started.

    Production writes ``parsing`` only from the graph-triggered preparation
    job, so the resume surface is exercised against the row shape itself.
    """
    cell = store._attachments[attachment_id]
    cell.record = replace(cell.record, status="parsing")


# ----- create / get / list -----


def test_create_starts_in_uploaded_with_no_parsed_version() -> None:
    _, store, thread_id = _threads_and_store()

    record = _create(store, thread_id, attachment_id="att-1")

    assert record.attachment_id == "att-1"
    assert record.thread_id == thread_id
    assert record.filename == "notes.txt"
    assert record.size_bytes == 1_024
    assert record.sha256 == "ab" * 32
    assert record.object_key == f"threads/{thread_id}/att-1"
    assert record.status == "uploaded"
    assert record.version == 0
    assert record.unit_count == 0
    assert record.indexed is False
    assert record.searchable is False
    assert record.failure_reason is None
    assert isinstance(record.created_at, datetime)
    assert record.created_at.tzinfo is not None
    assert record.updated_at.tzinfo is not None
    assert record.created_at.utcoffset() == timezone.utc.utcoffset(None)

    fetched = store.get("att-1")
    assert fetched == record


def test_get_unknown_attachment_is_a_lookup_miss_not_an_error() -> None:
    _, store, _ = _threads_and_store()
    assert store.get("nope") is None


def test_create_on_an_unknown_thread_raises_thread_gone() -> None:
    """An upload racing a delete must not leave an orphan row behind."""
    _, store, _ = _threads_and_store()
    with pytest.raises(AttachmentThreadGoneError):
        _create(store, "ghost-thread", attachment_id="att-1")
    assert store.get("att-1") is None


def test_create_after_the_thread_is_deleted_raises_thread_gone() -> None:
    threads, store, thread_id = _threads_and_store()
    assert threads.delete_thread(thread_id) is True
    with pytest.raises(AttachmentThreadGoneError):
        _create(store, thread_id)


def test_duplicate_attachment_id_is_loud_not_a_silent_overwrite() -> None:
    _, store, thread_id = _threads_and_store()
    _create(store, thread_id, attachment_id="att-dup")
    with pytest.raises(AttachmentStoreError):
        _create(store, thread_id, attachment_id="att-dup")
    assert store.get("att-dup").filename == "notes.txt"


def test_create_rejects_blank_identity_fields() -> None:
    """A blank id addresses a row nothing can read back, so it fails here."""
    _, store, thread_id = _threads_and_store()
    with pytest.raises(AttachmentStoreError):
        store.create(
            thread_id="",
            attachment_id="att-1",
            filename="notes.txt",
            size_bytes=1,
            sha256="ab" * 32,
            object_key="k",
        )
    with pytest.raises(AttachmentStoreError):
        store.create(
            thread_id=thread_id,
            attachment_id="  ",
            filename="notes.txt",
            size_bytes=1,
            sha256="ab" * 32,
            object_key="k",
        )
    assert store.list_for_thread(thread_id) == []


def test_list_for_thread_is_oldest_first_and_scoped_to_one_thread() -> None:
    threads, store, thread_id = _threads_and_store()
    other = threads.create_thread(title="other")

    first = _create(store, thread_id, attachment_id="att-1")
    second = _create(store, thread_id, attachment_id="att-2", filename="b.txt")
    _create(store, other.id, attachment_id="att-3")

    assert [row.attachment_id for row in store.list_for_thread(thread_id)] == [
        first.attachment_id,
        second.attachment_id,
    ]
    assert store.list_for_thread(other.id)[0].filename == "notes.txt"
    assert store.list_for_thread("never-a-thread") == []


def test_read_back_records_are_independent_of_store_state() -> None:
    """Frozen records, so independence means the derived containers too."""
    _, store, thread_id = _threads_and_store()
    record = _prepare(store, thread_id)

    postings = store.index_postings(record.attachment_id, 1)
    postings["revenue"] = (99,)
    postings["injected"] = (1,)
    assert store.index_postings(record.attachment_id, 1) == {
        "revenue": (1,),
        "dollars": (1, 2),
        "营业收入": (2,),
    }

    units = store.units(record.attachment_id, 1)
    assert isinstance(units, tuple)
    assert units == UNITS


# ----- parsed frozen units -----


def test_record_units_replaces_the_version_and_returns_the_count() -> None:
    _, store, thread_id = _threads_and_store()
    record = _create(store, thread_id)
    aid = record.attachment_id

    assert store.record_units(attachment_id=aid, version=1, units=UNITS) == 2
    assert store.units(aid, 1) == UNITS

    replacement = (ParsedAttachmentUnit(unit=1, text="only one unit"),)
    assert store.record_units(attachment_id=aid, version=1, units=replacement) == 1
    assert store.units(aid, 1) == replacement
    assert store.record_units(attachment_id=aid, version=1, units=()) == 0
    assert store.units(aid, 1) == ()


def test_record_units_orders_by_unit_ordinal_not_insertion() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.record_units(
        attachment_id=aid,
        version=1,
        units=(
            ParsedAttachmentUnit(unit=3, text="third"),
            ParsedAttachmentUnit(unit=1, text="first"),
            ParsedAttachmentUnit(unit=2, text="second"),
        ),
    )
    assert [unit.unit for unit in store.units(aid, 1)] == [1, 2, 3]


def test_units_addresses_one_version_only() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id, version=1).attachment_id
    store.record_units(attachment_id=aid, version=2, units=(UNITS[1],))
    assert store.units(aid, 1) == UNITS
    assert store.units(aid, 2) == (UNITS[1],)
    assert store.units(aid, 7) == ()


def test_clear_units_drops_every_version_and_leaves_status_alone() -> None:
    """Status is the publication seam; ``incomplete()`` reports the loss."""
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    store.record_units(attachment_id=aid, version=2, units=(UNITS[0],))

    store.clear_units(aid)

    assert store.units(aid, 1) == ()
    assert store.units(aid, 2) == ()
    assert store.get(aid).status == "uploaded"


def test_record_units_on_a_deleted_attachment_raises_thread_gone() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.delete_for_thread(thread_id)
    with pytest.raises(AttachmentThreadGoneError):
        store.record_units(attachment_id=aid, version=1, units=UNITS)
    assert store.units(aid, 1) == ()


def test_record_units_on_a_deleted_thread_raises_thread_gone() -> None:
    threads, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    threads.delete_thread(thread_id)
    with pytest.raises(AttachmentThreadGoneError):
        store.record_units(attachment_id=aid, version=1, units=UNITS)


def test_record_units_rejects_a_non_positive_unit_ordinal() -> None:
    """`unit` is a one-based ordinal in the locator contract."""
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    with pytest.raises(AttachmentStoreError):
        store.record_units(
            attachment_id=aid, version=1, units=(ParsedAttachmentUnit(unit=0, text="x"),)
        )


# ----- derived index -----


def test_put_index_round_trip_and_marks_the_attachment_indexed() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id

    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)

    assert store.index_postings(aid, 1) == {
        "revenue": (1,),
        "dollars": (1, 2),
        "营业收入": (2,),
    }
    assert store.indexed_versions(aid) == (1,)
    assert store.get(aid).indexed is True


def test_put_index_replaces_the_version_it_writes() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    store.put_index(attachment_id=aid, version=1, postings={"revenue": [2]})
    assert store.index_postings(aid, 1) == {"revenue": (2,)}


def test_put_index_with_no_terms_leaves_no_active_version() -> None:
    """An empty posting set is not a usable index."""
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.put_index(attachment_id=aid, version=1, postings={})
    assert store.indexed_versions(aid) == ()
    assert store.index_postings(aid, 1) == {}


def test_clear_index_drops_postings_and_revokes_the_indexed_flag() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    assert store.get(aid).searchable is True

    store.clear_index(aid)

    assert store.indexed_versions(aid) == ()
    assert store.index_postings(aid, 1) == {}
    after = store.get(aid)
    assert after.indexed is False
    assert after.searchable is False


def test_put_index_on_a_deleted_attachment_raises_thread_gone() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.delete_for_thread(thread_id)
    with pytest.raises(AttachmentThreadGoneError):
        store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    assert store.indexed_versions(aid) == ()


def test_index_postings_of_an_unknown_attachment_is_empty() -> None:
    _, store, _ = _threads_and_store()
    assert store.index_postings("nope", 1) == {}
    assert store.indexed_versions("nope") == ()


# ----- ready / failed publication -----


def test_mark_ready_publishes_the_version_and_unit_count() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id

    ready = store.mark_ready(attachment_id=aid, version=1, unit_count=2)

    assert ready.status == "ready"
    assert ready.version == 1
    assert ready.unit_count == 2
    assert ready.indexed is True
    assert ready.searchable is True
    assert ready.failure_reason is None
    assert store.get(aid) == ready


def test_mark_ready_refuses_a_version_without_parsed_units() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    with pytest.raises(AttachmentStoreError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    assert store.get(aid).status == "uploaded"


def test_mark_ready_refuses_a_version_without_index_postings() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.record_units(attachment_id=aid, version=1, units=UNITS)
    with pytest.raises(AttachmentStoreError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    assert store.get(aid).searchable is False


def test_mark_ready_refuses_a_version_whose_derived_data_landed_elsewhere() -> None:
    """The check is per-version, so a stale attempt cannot publish."""
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id, version=2).attachment_id
    with pytest.raises(AttachmentStoreError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    assert store.get(aid).status == "uploaded"


def test_mark_ready_refuses_a_unit_count_that_is_not_on_disk() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    with pytest.raises(AttachmentStoreError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=5)
    assert store.get(aid).status == "uploaded"


def test_mark_ready_on_a_deleted_attachment_raises_thread_gone() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    store.delete_for_thread(thread_id)
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)


def test_mark_ready_on_an_unknown_attachment_raises_thread_gone() -> None:
    _, store, _ = _threads_and_store()
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_ready(attachment_id="never-created", version=1, unit_count=1)


def test_mark_failed_records_the_named_reason_without_minting_a_version() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id

    failed = store.mark_failed(attachment_id=aid, version=1, reason="render_failed")

    assert failed.status == "failed"
    assert failed.failure_reason == "render_failed"
    assert failed.version == 0
    assert failed.unit_count == 0
    assert failed.searchable is False
    assert store.get(aid) == failed


def test_mark_failed_keeps_the_previously_published_version_visible() -> None:
    """A later failure is an event, not an erasure of what is on disk."""
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)

    failed = store.mark_failed(
        attachment_id=aid, version=2, reason="attachment_index_unavailable"
    )

    assert failed.status == "failed"
    assert failed.version == 1
    assert failed.searchable is False
    assert store.units(aid, 1) == UNITS


def test_mark_failed_rejects_an_empty_reason() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    with pytest.raises(AttachmentStoreError):
        store.mark_failed(attachment_id=aid, version=1, reason="   ")
    assert store.get(aid).status == "uploaded"


def test_mark_failed_on_a_deleted_attachment_raises_thread_gone() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    store.delete_for_thread(thread_id)
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_failed(attachment_id=aid, version=1, reason="vision_call_failed")


# ----- incomplete(): the restart resume surface -----


def test_incomplete_lists_every_parsing_row() -> None:
    _, store, thread_id = _threads_and_store()
    first = _create(store, thread_id)
    second = _create(store, thread_id)
    _force_parsing(store, first.attachment_id)
    _force_parsing(store, second.attachment_id)

    assert [row.attachment_id for row in store.incomplete()] == [
        first.attachment_id,
        second.attachment_id,
    ]


def test_incomplete_does_not_list_uploaded_rows() -> None:
    """``uploaded`` is durable bytes that nobody asked to prepare yet.

    Listing it would make a restart parse an attachment the operator only
    stored, which is the upload-trigger behavior ADR 0089 removed.
    """
    _, store, thread_id = _threads_and_store()
    _create(store, thread_id)
    _create(store, thread_id)

    assert store.incomplete() == []


def test_incomplete_lists_a_ready_row_whose_index_was_lost() -> None:
    """A rebuilt process must not report a stale ``ready``."""
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    assert store.incomplete() == []

    store.clear_index(aid)
    listed = store.incomplete()
    assert [row.attachment_id for row in listed] == [aid]
    assert listed[0].status == "ready"
    assert listed[0].searchable is False


def test_incomplete_lists_a_ready_row_whose_units_were_lost() -> None:
    _, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)

    store.clear_units(aid)

    assert [row.attachment_id for row in store.incomplete()] == [aid]


def test_incomplete_excludes_failed_rows_and_ready_rows_from_other_versions() -> None:
    _, store, thread_id = _threads_and_store()
    failed = _create(store, thread_id)
    store.mark_failed(attachment_id=failed.attachment_id, version=1, reason="render_failed")
    ready = _prepare(store, thread_id)
    store.mark_ready(attachment_id=ready.attachment_id, version=1, unit_count=2)
    # Derived data for an older version does not make a newer version complete.
    store.put_index(attachment_id=ready.attachment_id, version=3, postings=POSTINGS)

    assert store.incomplete() == []


# ----- thread deletion -----


def test_delete_for_thread_returns_object_keys_and_removes_every_layer() -> None:
    _, store, thread_id = _threads_and_store()
    first = _prepare(store, thread_id)
    second = _create(store, thread_id)

    keys = store.delete_for_thread(thread_id)

    assert keys == [
        f"threads/{thread_id}/{first.attachment_id}",
        f"threads/{thread_id}/{second.attachment_id}",
    ]
    assert store.get(first.attachment_id) is None
    assert store.list_for_thread(thread_id) == []
    assert store.units(first.attachment_id, 1) == ()
    assert store.indexed_versions(first.attachment_id) == ()
    assert store.incomplete() == []


def test_delete_for_thread_is_idempotent_and_thread_scoped() -> None:
    threads, store, thread_id = _threads_and_store()
    aid = _prepare(store, thread_id).attachment_id
    other = threads.create_thread(title="other")
    other_aid = _prepare(store, other.id).attachment_id

    assert store.delete_for_thread("never-a-thread") == []
    assert store.delete_for_thread(thread_id) == [f"threads/{thread_id}/{aid}"]
    assert store.delete_for_thread(thread_id) == []
    assert store.get(other_aid) is not None
    assert store.indexed_versions(other_aid) == (1,)


def test_delete_outranks_a_late_writer_under_real_concurrency() -> None:
    """No derived row, no metadata row, and no ``ready`` survives a delete."""
    threads, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    start = threading.Barrier(5)
    outcomes: list[Any] = []
    guard = threading.Lock()

    def worker() -> None:
        start.wait(timeout=5)
        try:
            store.record_units(attachment_id=aid, version=1, units=UNITS)
            store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
            outcome = store.mark_ready(attachment_id=aid, version=1, unit_count=2)
        except Exception as exc:  # noqa: BLE001 - the race outcome is the assertion
            outcome = exc
        with guard:
            outcomes.append(outcome)

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for worker_thread in workers:
        worker_thread.start()
    start.wait(timeout=5)
    deleted = store.delete_for_thread(thread_id)
    for worker_thread in workers:
        worker_thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in workers)
    assert deleted == [f"threads/{thread_id}/{aid}"]
    assert len(outcomes) == 4
    for outcome in outcomes:
        assert isinstance(
            outcome, (AttachmentRecord, AttachmentThreadGoneError, AttachmentStoreError)
        )
    assert store.get(aid) is None
    assert store.units(aid, 1) == ()
    assert store.indexed_versions(aid) == ()
    assert threads.get_thread(thread_id) is not None


# ----- protocol surface -----


_PROTOCOL_METHODS = (
    "create",
    "get",
    "list_for_thread",
    "record_units",
    "units",
    "clear_units",
    "put_index",
    "index_postings",
    "indexed_versions",
    "clear_index",
    "mark_ready",
    "mark_failed",
    "incomplete",
    "delete_for_thread",
)


def test_in_memory_store_satisfies_the_protocol_surface() -> None:
    store = InMemoryAttachmentStore(InMemoryThreadStore())
    missing = [name for name in _PROTOCOL_METHODS if not callable(getattr(store, name, None))]
    assert missing == []


def test_in_memory_store_only_asks_the_thread_store_for_liveness() -> None:
    """``InMemoryAttachmentStore(threads)`` may not write thread state."""

    class CountingThreads:
        def __init__(self, inner: InMemoryThreadStore) -> None:
            self.inner = inner
            self.get_thread_calls = 0

        def get_thread(self, thread_id: str) -> Any:
            self.get_thread_calls += 1
            return self.inner.get_thread(thread_id)

    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="t")
    probe = CountingThreads(threads)
    store = InMemoryAttachmentStore(probe)  # type: ignore[arg-type]

    aid = _create(store, thread.id).attachment_id
    store.record_units(attachment_id=aid, version=1, units=UNITS)
    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    store.delete_for_thread(thread.id)

    assert probe.get_thread_calls > 0
    assert threads.get_thread(thread.id) is not None


# ----- Postgres (env-gated on FIN_CHECKPOINT_URL) -----


@pytest.fixture
def postgres_store_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; PostgresAttachmentStore is "
            "untested in this environment"
        )
    return url


@pytest.fixture
def pg(postgres_store_url: str) -> Iterator[tuple[Any, Any]]:
    """Thread store + attachment store on one URL, with row cleanup."""
    import psycopg

    from fin.host.attachment_store import PostgresAttachmentStore
    from fin.host.thread_store import PostgresThreadStore

    threads = PostgresThreadStore(postgres_store_url)
    store = PostgresAttachmentStore(postgres_store_url)
    yield threads, store
    with psycopg.connect(postgres_store_url) as conn:
        conn.execute("DELETE FROM fin_thread")
        conn.commit()


def _pg_create(store: Any, thread_id: str, *, attachment_id: str | None = None) -> Any:
    return _create(store, thread_id, attachment_id=attachment_id)


def test_postgres_round_trip_uploaded_to_ready(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg attach")
    aid = f"pg-{uuid.uuid4().hex[:8]}"

    record = _pg_create(store, thread.id, attachment_id=aid)
    assert record.status == "uploaded"
    assert record.version == 0

    assert store.record_units(attachment_id=aid, version=1, units=UNITS) == 2
    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    ready = store.mark_ready(attachment_id=aid, version=1, unit_count=2)

    assert ready.status == "ready"
    assert ready.searchable is True
    assert store.units(aid, 1) == UNITS
    assert store.index_postings(aid, 1) == {
        "revenue": (1,),
        "dollars": (1, 2),
        "营业收入": (2,),
    }
    assert store.indexed_versions(aid) == (1,)
    assert [row.attachment_id for row in store.list_for_thread(thread.id)] == [aid]


def test_postgres_a_second_store_instance_sees_the_ready_version(
    pg: tuple[Any, Any], postgres_store_url: str
) -> None:
    """Restart reads the same rows: bytes, version, and index survive."""
    from fin.host.attachment_store import PostgresAttachmentStore

    threads, store = pg
    thread = threads.create_thread(title="pg restart")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)
    store.record_units(attachment_id=aid, version=4, units=UNITS)
    store.put_index(attachment_id=aid, version=4, postings=POSTINGS)
    store.mark_ready(attachment_id=aid, version=4, unit_count=2)

    reopened = PostgresAttachmentStore(postgres_store_url)
    assert reopened.get(aid) == store.get(aid)
    assert reopened.units(aid, 4) == UNITS
    assert reopened.indexed_versions(aid) == (4,)
    assert reopened.incomplete() == []


def test_postgres_thread_delete_cascades_and_refuses_late_writes(
    pg: tuple[Any, Any]
) -> None:
    """The FK is the arbitration point: no derived row outlives the thread."""
    threads, store = pg
    thread = threads.create_thread(title="pg cascade")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)
    store.record_units(attachment_id=aid, version=1, units=UNITS)
    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)

    assert threads.delete_thread(thread.id) is True

    assert store.get(aid) is None
    assert store.units(aid, 1) == ()
    assert store.index_postings(aid, 1) == {}
    assert store.indexed_versions(aid) == ()
    assert store.incomplete() == []
    with pytest.raises(AttachmentThreadGoneError):
        store.record_units(attachment_id=aid, version=1, units=UNITS)
    with pytest.raises(AttachmentThreadGoneError):
        store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_failed(attachment_id=aid, version=1, reason="render_failed")


def test_postgres_create_on_unknown_thread_raises_thread_gone(
    pg: tuple[Any, Any]
) -> None:
    _, store = pg
    with pytest.raises(AttachmentThreadGoneError):
        _pg_create(store, f"missing-{uuid.uuid4().hex}")


def test_postgres_mark_ready_refuses_missing_derived_data(
    pg: tuple[Any, Any]
) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg stale")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)
    store.record_units(attachment_id=aid, version=1, units=UNITS)

    with pytest.raises(AttachmentStoreError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    assert store.get(aid).status == "uploaded"

    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    store.clear_index(aid)
    with pytest.raises(AttachmentStoreError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)


def test_postgres_incomplete_after_index_loss(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg incomplete")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)
    store.record_units(attachment_id=aid, version=2, units=UNITS)
    store.put_index(attachment_id=aid, version=2, postings=POSTINGS)
    store.mark_ready(attachment_id=aid, version=2, unit_count=2)
    assert store.incomplete() == []

    store.clear_index(aid)

    assert [row.attachment_id for row in store.incomplete()] == [aid]
    assert store.delete_for_thread(thread.id) == [f"threads/{thread.id}/{aid}"]


def test_postgres_incomplete_does_not_list_uploaded_rows(pg: tuple[Any, Any]) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg uploaded idle")
    _pg_create(store, thread.id, attachment_id=f"pg-{uuid.uuid4().hex[:8]}")

    assert store.incomplete() == []
