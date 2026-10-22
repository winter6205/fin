"""The thread-bound attachment storage contract (ADRs 0083/0085/0088).

These cases pin ``fin.host.attachment.attachment_store.AttachmentStore`` semantics:
liveness arbitration, derived-data-gated ``ready``, restart visibility, and
thread-cascade deletion. The deletion obligation is one contract with two
implementations, so the ``store_pair`` fixture runs those cases against both
instead of keeping a copied ``test_postgres_*`` twin for each; the remaining
Postgres cases are the shapes only that adapter has.

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

from fin.host.attachment.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
    StoredPageRegion,
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
    store: Any,
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
    store: Any, thread_id: str, *, version: int = 1
) -> AttachmentRecord:
    """An accepted row with BOTH derived layers written for ``version``."""
    record = _create(store, thread_id)
    store.record_units(attachment_id=record.attachment_id, version=version, units=UNITS)
    store.put_index(
        attachment_id=record.attachment_id, version=version, postings=POSTINGS
    )
    return record


def _force_parsing(store: InMemoryAttachmentStore, attachment_id: str) -> None:
    """Pin the stored shape of a row whose preparation is in flight.

    A preparation settles its row before the caller returns, so no door
    leaves one parked here; the resume surface is exercised against the
    row shape itself.
    """
    cell = store._attachments[attachment_id]
    cell.record = replace(cell.record, status="parsing")


# ----- backends -----


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

    from fin.host.attachment.attachment_store import PostgresAttachmentStore
    from fin.host.thread_store import PostgresThreadStore

    threads = PostgresThreadStore(postgres_store_url)
    store = PostgresAttachmentStore(postgres_store_url)
    yield threads, store
    with psycopg.connect(postgres_store_url) as conn:
        conn.execute("DELETE FROM fin_thread")
        conn.commit()


@pytest.fixture(params=["memory", "postgres"])
def store_pair(request) -> Iterator[tuple[Any, Any, str]]:
    """One thread store and one attachment store, per durable backend.

    WHY parameterized: the deletion obligation is one contract with two
    implementations, and a hand-copied ``test_postgres_*`` twin drifts from the
    assertion it is supposed to share. Opt-in by the repo's convention: with no
    reachable ``FIN_CHECKPOINT_URL`` the ``postgres`` parameter skips and only
    the in-memory leg runs — the evidence the durable half would be missing,
    not a substitute for it.
    """
    if request.param == "memory":
        threads = InMemoryThreadStore()
        yield threads, InMemoryAttachmentStore(threads), "memory"
        return
    threads, store = request.getfixturevalue("pg")
    yield threads, store, "postgres"


def _retained_obligations(backend: str, store: Any, thread_id: str) -> int:
    """How many obligation records a backend still holds for one thread.

    WHY per backend: "retired, not hidden" is only visible in the storage each
    implementation uses — a dict in memory, a row the foreign key cascaded
    away in Postgres. ``pending_cleanup()`` cannot show it: both answer the
    same empty list once the thread is gone.
    """
    if backend == "memory":
        return len(store._tombstones)
    import psycopg

    with psycopg.connect(os.environ[POSTGRES_URL_ENV]) as conn:
        row = conn.execute(
            "SELECT count(*) FROM fin_thread_attachment_tombstone "
            "WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return int(row[0])


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


# ----- page region boxes (spec item 10: a region is persisted, not invented) --

# The two boxes of the committed mixed-chart gold case, page 3, in the media
# box the fixture measures (612 x 792 pt): the 528 x 328 pt chart, and the
# 33 x 40 pt logotype on page 1 that the legibility floor rejects. Real
# shapes, because the crop lane must not be measured on invented geometry.
CHART_REGION = StoredPageRegion(
    page=3, kind="figure", left_pt=42.0, top_pt=100.0, right_pt=570.0, bottom_pt=428.0
)
LOGOTYPE_REGION = StoredPageRegion(
    page=1, kind="figure", left_pt=289.5, top_pt=322.0, right_pt=322.5, bottom_pt=362.0
)
REGIONS = (LOGOTYPE_REGION, CHART_REGION)


def test_recorded_regions_read_back_ordered_by_page_for_one_version(store_pair) -> None:
    threads, store, _backend = store_pair
    thread = threads.create_thread(title="region thread")
    record = _create(store, thread.id, filename="mixed.pdf")

    store.record_regions(
        attachment_id=record.attachment_id, version=1, regions=REGIONS
    )

    # The box is read back with the same four PDF-point edges it was written
    # with, and one page's regions stay addressable without touching another
    # page's: a crop is only ever taken from the page the run named.
    assert store.regions(record.attachment_id, 1, page=3) == (CHART_REGION,)
    assert store.regions(record.attachment_id, 1) == REGIONS
    assert store.regions(record.attachment_id, 2) == ()


def test_recording_regions_replaces_that_version_and_leaves_other_versions(store_pair) -> None:
    threads, store, _backend = store_pair
    thread = threads.create_thread(title="region versions")
    record = _create(store, thread.id, filename="mixed.pdf")
    aid = record.attachment_id
    store.record_regions(attachment_id=aid, version=1, regions=REGIONS)
    store.record_regions(attachment_id=aid, version=2, regions=(LOGOTYPE_REGION,))

    store.record_regions(attachment_id=aid, version=1, regions=(CHART_REGION,))

    # Re-preparation replaces one version's boxes rather than accumulating
    # them: a re-run layout pass must not leave a stale box croppable.
    assert store.regions(aid, 1) == (CHART_REGION,)
    assert store.regions(aid, 2) == (LOGOTYPE_REGION,)


def test_recording_no_regions_clears_the_version_and_is_not_an_error(store_pair) -> None:
    threads, store, _backend = store_pair
    thread = threads.create_thread(title="empty regions")
    record = _create(store, thread.id, filename="mixed.pdf")
    aid = record.attachment_id
    store.record_regions(attachment_id=aid, version=1, regions=REGIONS)

    store.record_regions(attachment_id=aid, version=1, regions=())

    # A page lane that located nothing is the whole-page fallback's input,
    # not a store fault, so it stores the absence honestly.
    assert store.regions(aid, 1) == ()


def test_record_regions_on_a_deleted_attachment_raises_thread_gone(store_pair) -> None:
    threads, store, _backend = store_pair
    thread = threads.create_thread(title="gone regions")
    aid = _create(store, thread.id, filename="mixed.pdf").attachment_id
    store.delete_for_thread(thread.id)
    with pytest.raises(AttachmentThreadGoneError):
        store.record_regions(attachment_id=aid, version=1, regions=REGIONS)
    assert store.regions(aid, 1) == ()


def test_a_malformed_region_is_refused_at_the_store_boundary(store_pair) -> None:
    threads, store, _backend = store_pair
    thread = threads.create_thread(title="bad regions")
    aid = _create(store, thread.id, filename="mixed.pdf").attachment_id

    with pytest.raises(AttachmentStoreError):
        store.record_regions(
            attachment_id=aid,
            version=1,
            regions=(
                StoredPageRegion(
                    page=1, kind="figure", left_pt=0.0, top_pt=0.0,
                    right_pt=0.0, bottom_pt=10.0,
                ),
            ),
        )
    assert store.regions(aid, 1) == ()


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


# ----- tombstone: access is voided before any byte cleanup -----


def test_tombstone_voids_access_and_returns_the_byte_obligation() -> None:
    _, store, thread_id = _threads_and_store()
    record = _prepare(store, thread_id)
    store.mark_ready(attachment_id=record.attachment_id, version=1, unit_count=2)
    aid = record.attachment_id

    key = store.tombstone(attachment_id=aid, thread_id=thread_id)

    assert key == f"threads/{thread_id}/{aid}"
    assert store.get(aid) is None
    assert store.list_for_thread(thread_id) == []
    assert store.units(aid, 1) == ()
    assert store.index_postings(aid, 1) == {}
    assert store.indexed_versions(aid) == ()
    assert store.incomplete() == []


def test_tombstone_blocks_every_late_derived_write() -> None:
    """The void is the arbitration point for a preparation that is still running."""
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    assert store.tombstone(attachment_id=aid, thread_id=thread_id) is not None

    with pytest.raises(AttachmentThreadGoneError):
        store.record_units(attachment_id=aid, version=1, units=UNITS)
    with pytest.raises(AttachmentThreadGoneError):
        store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_ready(attachment_id=aid, version=1, unit_count=2)
    with pytest.raises(AttachmentThreadGoneError):
        store.mark_failed(attachment_id=aid, version=1, reason="render_failed")

    assert store.units(aid, 1) == ()
    assert store.indexed_versions(aid) == ()


def test_tombstone_holds_the_obligation_until_cleanup_is_confirmed() -> None:
    """An unconfirmed deletion stays retryable, and never re-voids silently."""
    _, store, thread_id = _threads_and_store()
    aid = _create(store, thread_id).attachment_id
    key = f"threads/{thread_id}/{aid}"

    assert store.tombstone(attachment_id=aid, thread_id=thread_id) == key
    assert store.pending_cleanup(thread_id) == [key]
    # The retry path reaches the same obligation while the bytes are owed.
    assert store.tombstone(attachment_id=aid, thread_id=thread_id) == key

    store.confirm_cleanup(attachment_id=aid)

    assert store.pending_cleanup(thread_id) == []
    assert store.tombstone(attachment_id=aid, thread_id=thread_id) is None


def test_tombstone_of_another_threads_attachment_reveals_nothing(
    store_pair: tuple[Any, Any, str],
) -> None:
    """A refused void writes no obligation and touches no row.

    The claim the scripted statement-shape case used to pin — a miss leaves
    the tables untouched — is observable here instead: the other thread's row
    keeps every layer and neither thread gains a debt.
    """
    threads, store, backend = store_pair
    mine = threads.create_thread(title=f"{backend} mine")
    theirs = threads.create_thread(title=f"{backend} theirs")
    record = _prepare(store, theirs.id)

    assert store.tombstone(attachment_id=record.attachment_id, thread_id=mine.id) is None

    assert store.get(record.attachment_id) is not None
    assert store.units(record.attachment_id, 1) == UNITS
    assert store.indexed_versions(record.attachment_id) == (1,)
    assert store.pending_cleanup(mine.id) == []
    assert store.pending_cleanup(theirs.id) == []


def test_tombstone_of_an_unknown_attachment_is_a_miss_not_an_error() -> None:
    _, store, thread_id = _threads_and_store()

    assert store.tombstone(attachment_id="never-uploaded", thread_id=thread_id) is None
    assert store.pending_cleanup(thread_id) == []


def test_tombstone_refuses_a_blank_identity() -> None:
    _, store, thread_id = _threads_and_store()

    with pytest.raises(AttachmentStoreError):
        store.tombstone(attachment_id="   ", thread_id=thread_id)
    with pytest.raises(AttachmentStoreError):
        store.tombstone(attachment_id="att-1", thread_id="")


def test_thread_delete_tombstones_every_attachment_before_cleanup(
    store_pair: tuple[Any, Any, str],
) -> None:
    threads, store, backend = store_pair
    thread = threads.create_thread(title=f"{backend} thread tombstone")
    first = _prepare(store, thread.id)
    second = _create(store, thread.id)
    keys = sorted([first.object_key, second.object_key])

    assert sorted(store.delete_for_thread(thread.id)) == keys
    assert sorted(store.pending_cleanup(thread.id)) == keys
    assert store.get(first.attachment_id) is None
    assert store.list_for_thread(thread.id) == []

    # A retry while cleanup is outstanding reaches the same obligations, even
    # though no attachment row is left to void.
    assert store.delete_for_thread(thread.id) == []
    assert sorted(store.pending_cleanup(thread.id)) == keys

    store.confirm_thread_cleanup(thread.id)

    assert store.pending_cleanup(thread.id) == []
    assert store.tombstone(
        attachment_id=first.attachment_id, thread_id=thread.id
    ) is None


def test_thread_voiding_returns_the_obligation_it_recorded(
    store_pair: tuple[Any, Any, str],
) -> None:
    """One write answers both "what did I void" and "what is still owed".

    The thread-wide door used to enumerate the keys it was voiding and record
    the debt beside it, so the returned list and ``pending_cleanup()`` could
    name different bytes. Both now come from the same void, and a debt the
    single door already handed over is still readable beside the new one.
    """
    threads, store, backend = store_pair
    thread = threads.create_thread(title=f"{backend} thread void")
    owed = _create(store, thread.id, attachment_id=f"{backend}-owed")
    fresh = _create(store, thread.id, attachment_id=f"{backend}-fresh")

    assert store.tombstone(attachment_id=owed.attachment_id, thread_id=thread.id) == (
        owed.object_key
    )

    # Only the row still on record is voided here; the owed key stays owed.
    assert store.delete_for_thread(thread.id) == [fresh.object_key]
    assert sorted(store.pending_cleanup(thread.id)) == sorted(
        [owed.object_key, fresh.object_key]
    )

    # Both doors retry against the same records: nothing re-voids in silence
    # and one id never ends up with two debts.
    assert store.delete_for_thread(thread.id) == []
    assert store.tombstone(attachment_id=fresh.attachment_id, thread_id=thread.id) == (
        fresh.object_key
    )
    assert sorted(store.pending_cleanup(thread.id)) == sorted(
        [owed.object_key, fresh.object_key]
    )


def test_create_refuses_an_id_that_still_owes_a_cleanup(
    store_pair: tuple[Any, Any, str],
) -> None:
    """An attachment identity is not reusable until its bytes are confirmed.

    One obligation record per id cannot carry a second debt, so accepting the
    pairing stranded a key: the void that later claimed the new row recorded
    nothing for it and the retry kept answering with the older one. ``create()``
    is where that is decided, and the refusal lifts with the confirmation.
    """
    threads, store, backend = store_pair
    thread = threads.create_thread(title=f"{backend} recycle")
    other = threads.create_thread(title=f"{backend} other")
    first = _create(store, thread.id, attachment_id=f"{backend}-owed")

    assert store.tombstone(attachment_id=first.attachment_id, thread_id=thread.id) == (
        first.object_key
    )

    with pytest.raises(AttachmentStoreError):
        _create(store, thread.id, attachment_id=first.attachment_id)
    with pytest.raises(AttachmentStoreError):
        _create(store, other.id, attachment_id=first.attachment_id)

    # A refused attempt writes nothing: the debt and its answer are untouched.
    assert store.pending_cleanup(thread.id) == [first.object_key]
    assert store.pending_cleanup(other.id) == []
    assert store.get(first.attachment_id) is None
    assert store.tombstone(attachment_id=first.attachment_id, thread_id=thread.id) == (
        first.object_key
    )

    store.confirm_cleanup(attachment_id=first.attachment_id)

    assert store.pending_cleanup(thread.id) == []
    replayed = _create(store, other.id, attachment_id=first.attachment_id)
    assert replayed.thread_id == other.id
    assert store.get(first.attachment_id) == replayed


def test_thread_removal_retires_obligations_instead_of_hiding_them(
    store_pair: tuple[Any, Any, str],
) -> None:
    """The records go with the thread, not just their readability.

    ``pending_cleanup()`` used to answer ``[]`` from a filter while the
    obligation stayed in the map until some later probe of THAT id collected
    it, so the in-memory store retained a debt no thread could ever pay. The
    retirement step drops it where the Postgres cascade drops it.
    """
    threads, store, backend = store_pair
    thread = threads.create_thread(title=f"{backend} retire debt")
    record = _create(store, thread.id, attachment_id=f"{backend}-debt")

    assert store.tombstone(attachment_id=record.attachment_id, thread_id=thread.id) == (
        record.object_key
    )
    assert store.pending_cleanup(thread.id) == [record.object_key]

    assert threads.delete_thread(thread.id) is True

    assert store.pending_cleanup(thread.id) == []
    assert _retained_obligations(backend, store, thread.id) == 0
    assert store.tombstone(
        attachment_id=record.attachment_id, thread_id=thread.id
    ) is None


def test_the_cleanup_door_names_a_row_whose_thread_row_is_already_gone(
    store_pair: tuple[Any, Any, str],
) -> None:
    """No row leaves this store without a door having named its bytes.

    The two backends cannot answer identically and both answers are the
    contract: ``ON DELETE CASCADE`` removes the durable row in the same
    statement that removes the thread, so nothing is left to name, while the
    in-memory retirement is lazy and leaves a row that outlived its thread —
    which the thread-wide door hands over, because it is the one door with a
    channel for a key. Reads hide such a row rather than removing it, so a
    lookup or a lost derived write cannot pre-empt that naming.
    """
    threads, store, backend = store_pair
    thread = threads.create_thread(title=f"{backend} orphan row")
    record = _create(store, thread.id, attachment_id=f"{backend}-orphan")

    assert threads.delete_thread(thread.id) is True

    assert store.get(record.attachment_id) is None
    assert store.list_for_thread(thread.id) == []
    assert store.incomplete() == []
    with pytest.raises(AttachmentThreadGoneError):
        store.record_units(
            attachment_id=record.attachment_id, version=1, units=UNITS
        )

    keys = store.delete_for_thread(thread.id)
    if backend == "memory":
        assert keys == [record.object_key]
        assert store._attachments == {}
    else:
        assert keys == []

    # A debt cannot outlive the thread that owes it, and the door stays
    # idempotent once its rows are retired.
    assert store.pending_cleanup(thread.id) == []
    assert store.delete_for_thread(thread.id) == []


def test_confirm_cleanup_enters_the_same_retirement_step() -> None:
    """The fifth door retires like the other four.

    Confirming one id used to pop only that record, so a thread whose row was
    already gone kept its other rows and the rest of its obligations until some
    later door happened to sweep them.
    """
    threads, store, thread_id = _threads_and_store()
    owed = _create(store, thread_id, attachment_id="att-1")
    _create(store, thread_id, attachment_id="att-2")

    assert store.tombstone(attachment_id=owed.attachment_id, thread_id=thread_id) == (
        owed.object_key
    )
    assert threads.delete_thread(thread_id) is True

    store.confirm_cleanup(attachment_id=owed.attachment_id)

    assert store._tombstones == {}
    assert store._attachments == {}
    assert store.pending_cleanup(thread_id) == []


def test_thread_delete_obligations_are_scoped_to_their_thread() -> None:
    threads, store, thread_id = _threads_and_store()
    other = threads.create_thread(title="other")
    other_aid = _prepare(store, other.id).attachment_id
    _prepare(store, thread_id)

    store.delete_for_thread(thread_id)

    assert store.pending_cleanup(other.id) == []
    assert store.get(other_aid) is not None
    assert store.units(other_aid, 1) == UNITS


# ----- protocol surface -----


_PROTOCOL_METHODS = (
    "create",
    "get",
    "list_for_thread",
    "record_units",
    "units",
    "clear_units",
    "record_regions",
    "regions",
    "put_index",
    "index_postings",
    "indexed_versions",
    "clear_index",
    "mark_ready",
    "mark_failed",
    "incomplete",
    "delete_for_thread",
    "tombstone",
    "pending_cleanup",
    "confirm_cleanup",
    "confirm_thread_cleanup",
)


def test_in_memory_store_satisfies_the_protocol_surface() -> None:
    store = InMemoryAttachmentStore(InMemoryThreadStore())
    missing = [name for name in _PROTOCOL_METHODS if not callable(getattr(store, name, None))]
    assert missing == []


def test_postgres_store_satisfies_the_protocol_surface(
    postgres_store_url: str,
) -> None:
    """The durable store offers the same void-and-obligation surface.

    WHY a separate guard: the deletion door is one contract, and a method
    only the in-memory implementation carries would let a real deployment
    answer a delete while leaving bytes owed with no way to finish them.
    """
    from fin.host.attachment.attachment_store import PostgresAttachmentStore
    from fin.host.thread_store import PostgresThreadStore

    # The thread schema precedes the attachment schema, exactly as the
    # product's own builder orders it.
    PostgresThreadStore(postgres_store_url)
    store = PostgresAttachmentStore(postgres_store_url)
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
#
# The fixtures above are what reach a real server. Statement identity is not
# pinned anywhere: the same claim — that both deletion doors write one
# obligation and a refused void writes none — is observable through the adapter
# and is exercised against the server in the cases below and in the
# parameterized pair above.


def _pg_create(store: Any, thread_id: str, *, attachment_id: str | None = None) -> Any:
    return _create(store, thread_id, attachment_id=attachment_id)


def test_postgres_a_row_shadowed_by_another_threads_debt_still_goes_with_its_thread(
    pg: tuple[Any, Any],
) -> None:
    """A thread delete leaves no row behind, even one no void can record.

    ``create()`` refuses an owed id, so only the window between that check and
    a concurrent void can leave a live row whose id's single record belongs to
    another thread. The seed writes the shape directly, because no door is
    allowed to build it: retaining the row would contradict the promise that
    every attachment row goes with its thread, and silently dropping it would
    lose the key.
    """
    import psycopg

    threads, store = pg
    mine = threads.create_thread(title="pg shadow mine")
    theirs = threads.create_thread(title="pg shadow theirs")
    aid = f"pg-shadow-{uuid.uuid4().hex[:8]}"
    _pg_create(store, theirs.id, attachment_id=aid)
    theirs_key = f"threads/{theirs.id}/{aid}"
    assert store.tombstone(attachment_id=aid, thread_id=theirs.id) == theirs_key

    my_key = f"threads/{mine.id}/{aid}"
    with psycopg.connect(os.environ[POSTGRES_URL_ENV]) as conn:
        conn.execute(
            "INSERT INTO fin_thread_attachment (attachment_id, thread_id, "
            "filename, size_bytes, sha256, object_key, status) "
            "VALUES (%s, %s, 'notes.txt', 1024, 'ab', %s, 'uploaded')",
            (aid, mine.id, my_key),
        )
        conn.commit()

    assert store.delete_for_thread(mine.id) == [my_key]
    assert store.get(aid) is None
    assert store.pending_cleanup(mine.id) == []

    # The other thread's debt is untouched: one record, one debt, retryable.
    assert store.pending_cleanup(theirs.id) == [theirs_key]
    assert store.tombstone(attachment_id=aid, thread_id=theirs.id) == theirs_key


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
    from fin.host.attachment.attachment_store import PostgresAttachmentStore

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


def test_postgres_tombstone_voids_access_and_returns_the_byte_obligation(
    pg: tuple[Any, Any],
) -> None:
    threads, store = pg
    thread = threads.create_thread(title="pg tombstone")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)
    store.record_units(attachment_id=aid, version=1, units=UNITS)
    store.put_index(attachment_id=aid, version=1, postings=POSTINGS)
    store.mark_ready(attachment_id=aid, version=1, unit_count=2)

    assert store.tombstone(attachment_id=aid, thread_id=thread.id) == (
        f"threads/{thread.id}/{aid}"
    )

    assert store.get(aid) is None
    assert store.list_for_thread(thread.id) == []
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


def test_postgres_tombstone_obligation_survives_a_new_store_instance(
    pg: tuple[Any, Any], postgres_store_url: str
) -> None:
    """A restart reaches the same pending cleanup and can finish it."""
    from fin.host.attachment.attachment_store import PostgresAttachmentStore

    threads, store = pg
    thread = threads.create_thread(title="pg tombstone restart")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)
    key = store.tombstone(attachment_id=aid, thread_id=thread.id)

    reopened = PostgresAttachmentStore(postgres_store_url)

    assert reopened.pending_cleanup(thread.id) == [key]
    assert reopened.tombstone(attachment_id=aid, thread_id=thread.id) == key
    assert reopened.incomplete() == []

    reopened.confirm_cleanup(attachment_id=aid)

    assert reopened.pending_cleanup(thread.id) == []
    assert reopened.tombstone(attachment_id=aid, thread_id=thread.id) is None


def test_postgres_thread_delete_cascades_the_cleanup_obligation(
    pg: tuple[Any, Any],
) -> None:
    """The schema-level half of the retirement the doors express.

    ``fin_thread_attachment_tombstone.thread_id`` is the foreign key that
    retires an unpaid cleanup when its thread row goes, so a deleted thread
    keeps no debt no door can finish. The parameterized pair pins the same
    answer through the doors; this pins the constraint that makes it hold for a
    row no door will ever be called on again.
    """
    threads, store = pg
    thread = threads.create_thread(title="pg cascade debt")
    aid = f"pg-{uuid.uuid4().hex[:8]}"
    _pg_create(store, thread.id, attachment_id=aid)

    assert store.tombstone(attachment_id=aid, thread_id=thread.id) is not None
    assert store.pending_cleanup(thread.id) == [f"threads/{thread.id}/{aid}"]

    assert threads.delete_thread(thread.id) is True

    assert store.pending_cleanup(thread.id) == []
    assert _retained_obligations("postgres", store, thread.id) == 0
    assert store.tombstone(attachment_id=aid, thread_id=thread.id) is None


# ----- observation answer payload -----


def test_observation_answer_text_round_trips_on_both_backends(
    store_pair,
) -> None:
    """A persisted observation must carry the provider's answer, not only its
    provenance keys, on every backend — equality of the read-back envelope is
    the check that no layer dropped the payload."""
    from fin.host.attachment.attachment_observations import (
        COVERAGE_SCHEMA_VERSION,
        ObservationEnvelope,
    )

    threads, store, _backend = store_pair
    thread = threads.create_thread(title="observation answer")
    record = _create(store, thread.id)
    answer = "Chart shows Q3 revenue of 89,493 million dollars."
    observation = ObservationEnvelope(
        attachment_id=record.attachment_id,
        coverage_schema_version=COVERAGE_SCHEMA_VERSION,
        observation_version=1,
        attachment_version=1,
        source_unit=1,
        page=1,
        page_image_sha256="cd" * 32,
        provider="openai-compatible",
        model_version="provider-model-1",
        answer_text=answer,
        created_at=datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    stored = store.record_observation(
        attachment_id=record.attachment_id, observation=observation
    )
    read_back = store.observations(record.attachment_id, 1, 1, 1)

    assert stored == observation
    assert read_back == (observation,)
    assert read_back[0].answer_text == answer
