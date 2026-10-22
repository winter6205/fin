"""Persistence for the metadata pointer to an object-store filing.

The object store owns raw bytes. This port owns the durable association
between a corpus document and its :class:`ObjectPointer`. Local tests use
the in-memory implementation; a Postgres implementation is selected only
when an explicit metadata/checkpoint DSN is configured.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import psycopg

from fin.corpus.object_store import ObjectPointer


class MetadataStoreError(RuntimeError):
    """Base class for metadata pointer failures."""


class MetadataPointerNotFoundError(MetadataStoreError):
    """No object pointer has been recorded for a document."""


@dataclass(frozen=True)
class MetadataPointer:
    """One document-to-object pointer row."""

    document_id: str
    object: ObjectPointer

    @property
    def pointer(self) -> ObjectPointer:
        """Alias for callers that avoid the ``object`` field name."""
        return self.object


@runtime_checkable
class MetadataStore(Protocol):
    """Port for persisting raw object pointers."""

    def put_pointer(self, document_id: str, pointer: ObjectPointer) -> None: ...

    def get_pointer(self, document_id: str) -> MetadataPointer: ...

    def delete_pointer(self, document_id: str, *, object_key: str) -> None: ...


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must not be empty")


class InMemoryMetadataStore:
    """Thread-safe pointer store for tests and local no-Postgres runs."""

    def __init__(self) -> None:
        self._pointers: dict[str, MetadataPointer] = {}
        self._lock = threading.RLock()

    def put_pointer(self, document_id: str, pointer: ObjectPointer) -> None:
        _validate_document_id(document_id)
        if not isinstance(pointer, ObjectPointer):
            raise TypeError("pointer must be an ObjectPointer")
        with self._lock:
            self._pointers[document_id] = MetadataPointer(
                document_id=document_id,
                object=pointer,
            )

    def get_pointer(self, document_id: str) -> MetadataPointer:
        _validate_document_id(document_id)
        with self._lock:
            pointer = self._pointers.get(document_id)
        if pointer is None:
            raise MetadataPointerNotFoundError(
                f"no metadata pointer for {document_id!r}"
            )
        return pointer

    def delete_pointer(self, document_id: str, *, object_key: str) -> None:
        """Remove the row only while it names ``object_key``; otherwise a no-op."""
        _validate_document_id(document_id)
        with self._lock:
            pointer = self._pointers.get(document_id)
            if pointer is not None and pointer.object.key == object_key:
                del self._pointers[document_id]


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_corpus_object_pointer (
    document_id TEXT PRIMARY KEY,
    object_key TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    content_type TEXT
)
"""


class PostgresMetadataStore:
    """Postgres-backed metadata pointer store."""

    def __init__(self, connection_url: str) -> None:
        if not isinstance(connection_url, str) or not connection_url.strip():
            raise ValueError("metadata connection URL must not be empty")
        self._url = connection_url
        with self._connect() as connection:
            connection.execute(_SCHEMA_SQL)
            connection.commit()

    def _connect(self) -> Any:
        return psycopg.connect(self._url)

    def put_pointer(self, document_id: str, pointer: ObjectPointer) -> None:
        _validate_document_id(document_id)
        if not isinstance(pointer, ObjectPointer):
            raise TypeError("pointer must be an ObjectPointer")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fin_corpus_object_pointer "
                "(document_id, object_key, size_bytes, sha256, content_type) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (document_id) DO UPDATE SET "
                "object_key = EXCLUDED.object_key, "
                "size_bytes = EXCLUDED.size_bytes, "
                "sha256 = EXCLUDED.sha256, "
                "content_type = EXCLUDED.content_type",
                (
                    document_id,
                    pointer.key,
                    pointer.size_bytes,
                    pointer.sha256,
                    pointer.content_type,
                ),
            )
            connection.commit()

    def get_pointer(self, document_id: str) -> MetadataPointer:
        _validate_document_id(document_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT object_key, size_bytes, sha256, content_type "
                "FROM fin_corpus_object_pointer WHERE document_id = %s",
                (document_id,),
            ).fetchone()
        if row is None:
            raise MetadataPointerNotFoundError(
                f"no metadata pointer for {document_id!r}"
            )
        return MetadataPointer(
            document_id=document_id,
            object=ObjectPointer(
                key=str(row[0]),
                size_bytes=int(row[1]),
                sha256=str(row[2]),
                content_type=row[3],
            ),
        )

    def delete_pointer(self, document_id: str, *, object_key: str) -> None:
        """Remove the row only while it names ``object_key``; otherwise a no-op."""
        _validate_document_id(document_id)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM fin_corpus_object_pointer "
                "WHERE document_id = %s AND object_key = %s",
                (document_id, object_key),
            )
            connection.commit()


def metadata_store_from_env(
    environ: Mapping[str, str] | None = None,
) -> MetadataStore:
    """Select Postgres only when an explicit DSN is configured."""
    values = os.environ if environ is None else environ
    dsn = (
        values.get("FIN_METADATA_DSN")
        or values.get("FIN_METADATA_URL")
        or values.get("FIN_CHECKPOINT_URL")
        or ""
    ).strip()
    return PostgresMetadataStore(dsn) if dsn else InMemoryMetadataStore()


default_metadata_store = metadata_store_from_env
create_metadata_store = metadata_store_from_env


__all__ = [
    "InMemoryMetadataStore",
    "MetadataPointer",
    "MetadataPointerNotFoundError",
    "MetadataStore",
    "MetadataStoreError",
    "PostgresMetadataStore",
    "create_metadata_store",
    "default_metadata_store",
    "metadata_store_from_env",
]
