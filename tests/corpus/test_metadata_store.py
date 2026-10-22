"""The metadata pointer store: the durable link between a document and bytes.

The object store owns raw bytes; the pointer store owns the row that says
which bytes a document has. A refused accept must be able to take that row
back with it, so the store needs a delete as well as a put — a document that
keeps naming bytes no accept ever accepted is worse than one with no row
at all.

The in-memory cases pin the contract every implementation owes: put and get
round-trip, delete removes the row, deleting an absent row is not an error,
and an empty identifier is refused everywhere. The Postgres case pins the
same delete against a real database, in the ``_test`` database
``FIN_TEST_DSN`` names and nothing else.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from fin.corpus.metadata_store import (
    InMemoryMetadataStore,
    MetadataPointerNotFoundError,
    PostgresMetadataStore,
)
from fin.corpus.object_store import ObjectPointer

TEST_DSN_ENV = "FIN_TEST_DSN"

_DOCUMENT_ID = "doc_pointer_store_case"
_POINTER = ObjectPointer(
    key=f"filings/{_DOCUMENT_ID}/v1",
    size_bytes=17,
    sha256="0" * 64,
    content_type="text/markdown",
)


# ---------- the in-memory store ----------


def test_the_in_memory_store_round_trips_a_pointer() -> None:
    """What was put is what comes back, field for field."""
    store = InMemoryMetadataStore()

    store.put_pointer(_DOCUMENT_ID, _POINTER)

    pointer = store.get_pointer(_DOCUMENT_ID)
    assert pointer.document_id == _DOCUMENT_ID
    assert pointer.pointer == _POINTER


def test_deleting_a_pointer_removes_it() -> None:
    """The deleted document has no pointer any more."""
    store = InMemoryMetadataStore()
    store.put_pointer(_DOCUMENT_ID, _POINTER)

    store.delete_pointer(_DOCUMENT_ID, object_key=_POINTER.key)

    with pytest.raises(MetadataPointerNotFoundError):
        store.get_pointer(_DOCUMENT_ID)


def test_deleting_a_pointer_twice_is_a_no_op() -> None:
    """The compensation may run twice; the second run is not a failure.

    A refused accept that compensates twice — a retried cleanup, a second
    refusal of the same upload — must not raise: there is simply nothing
    left to remove.
    """
    store = InMemoryMetadataStore()
    store.put_pointer(_DOCUMENT_ID, _POINTER)

    store.delete_pointer(_DOCUMENT_ID, object_key=_POINTER.key)
    store.delete_pointer(_DOCUMENT_ID, object_key=_POINTER.key)

    with pytest.raises(MetadataPointerNotFoundError):
        store.get_pointer(_DOCUMENT_ID)


def test_a_delete_that_names_another_object_leaves_the_row_alone() -> None:
    """The delete is scoped to the object the cleanup itself wrote.

    A concurrent winner can commit its own pointer row for the same
    document; the loser's cleanup must not remove it.
    """
    store = InMemoryMetadataStore()
    winner = ObjectPointer(
        key=f"filings/{_DOCUMENT_ID}/winner",
        size_bytes=23,
        sha256="1" * 64,
        content_type="text/markdown",
    )
    store.put_pointer(_DOCUMENT_ID, winner)

    store.delete_pointer(_DOCUMENT_ID, object_key=_POINTER.key)

    assert store.get_pointer(_DOCUMENT_ID).pointer == winner


@pytest.mark.parametrize("document_id", ["", "   "])
def test_an_empty_document_id_is_refused_everywhere(document_id: str) -> None:
    """A blank identifier names no document, so no operation accepts one."""
    store = InMemoryMetadataStore()

    with pytest.raises(ValueError):
        store.put_pointer(document_id, _POINTER)
    with pytest.raises(ValueError):
        store.get_pointer(document_id)
    with pytest.raises(ValueError):
        store.delete_pointer(document_id, object_key=_POINTER.key)


# ---------- the Postgres store ----------


def test_the_postgres_store_deletes_the_pointer_row() -> None:
    """The durable delete removes the row, and deleting again is a no-op.

    The database is the one ``FIN_TEST_DSN`` names and nothing else; a
    database that does not end in ``_test`` is refused rather than touched.
    """
    dsn = os.environ.get(TEST_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(
            f"{TEST_DSN_ENV} is unset; the durable pointer delete is untested "
            "in this environment"
        )
    database = conninfo_to_dict(dsn).get("dbname", "")
    assert database.endswith("_test"), (
        f"the pointer store tests refuse a database that is not a _test "
        f"database: {database}"
    )
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == database

    document_id = f"doc_pointer_delete_{uuid.uuid4().hex[:10]}"
    store = PostgresMetadataStore(dsn)
    try:
        store.put_pointer(document_id, _POINTER)
        assert store.get_pointer(document_id).pointer == _POINTER

        # a delete that names another object leaves the row alone
        store.delete_pointer(document_id, object_key="filings/other/v9")
        assert store.get_pointer(document_id).pointer == _POINTER

        store.delete_pointer(document_id, object_key=_POINTER.key)

        with pytest.raises(MetadataPointerNotFoundError):
            store.get_pointer(document_id)
        store.delete_pointer(document_id, object_key=_POINTER.key)
    finally:
        store.delete_pointer(document_id, object_key=_POINTER.key)
