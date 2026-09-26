"""Durable storage for thread-owned attachments (ADRs 0083/0085/0088).

An attachment is a file an operator drops into one research thread. Fin
owns three layers of it, all bound to the owning ``thread_id``:

* the raw bytes, addressed by an :class:`~fin.corpus.object_store.ObjectStore`
  key — the model never sees a path;
* the parsed frozen units, one text row per ``(attachment_id, version, unit)``;
* the derived search index, one posting row per ``(attachment_id, version, term)``.

Every layer is removed with the thread, and no attachment row survives a
delete: the deletion is the arbitration point for a preparation that is
still running, which loses the race with :class:`AttachmentThreadGoneError`
instead of resurrecting its derived data.

Two implementations satisfy :class:`AttachmentStore`:

* :class:`InMemoryAttachmentStore` — dicts behind one lock, checked against a
  :class:`~fin.host.thread_store.ThreadStore` for thread liveness. The default
  test and no-Postgres boot.
* :class:`PostgresAttachmentStore` — the same invariants through foreign keys
  to ``fin_thread`` with ``ON DELETE CASCADE``.

``version`` is the parsed-content version: it is minted by a successful
preparation and stays put across restarts, so a rebuilt index addresses the
same text the operator attached.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol, Sequence


# ----- typed errors -----


class AttachmentStoreError(Exception):
    """Base class for attachment-store typed errors."""


class AttachmentThreadGoneError(AttachmentStoreError):
    """The attachment row or its thread row is gone.

    Raised by every write that would land derived data on a deleted
    attachment. The preparation worker treats it as "lose the race": the
    derived text and index it produced are discarded rather than written
    back, and the attachment is never reported ``ready``.
    """


# ----- records -----


AttachmentStatus = Literal["uploaded", "parsing", "ready", "failed"]

_STATUSES: tuple[AttachmentStatus, ...] = (
    "uploaded",
    "parsing",
    "ready",
    "failed",
)


@dataclass(frozen=True)
class ParsedAttachmentUnit:
    """One frozen unit of parsed attachment text for one version."""

    unit: int
    text: str


@dataclass(frozen=True)
class AttachmentRecord:
    """One thread-owned attachment's durable metadata row."""

    attachment_id: str
    thread_id: str
    filename: str
    size_bytes: int
    sha256: str
    object_key: str
    status: AttachmentStatus
    version: int
    unit_count: int
    indexed: bool
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def searchable(self) -> bool:
        """``ready`` with a usable derived index for the current version."""
        return self.status == "ready" and self.indexed


def _validate_status(value: str) -> AttachmentStatus:
    if value in _STATUSES:
        return value  # type: ignore[return-value]
    raise AttachmentStoreError(
        f"AttachmentRecord.status must be one of {_STATUSES}, got {value!r}"
    )


# ----- protocol -----


class AttachmentStore(Protocol):
    """The thread-bound durability contract for attachments."""

    def create(
        self,
        *,
        thread_id: str,
        attachment_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> AttachmentRecord:
        """Write the metadata row for an accepted upload in ``uploaded``.

        ``version`` starts at 0 with no derived data. Raises
        :class:`AttachmentThreadGoneError` when ``thread_id`` is not a live
        thread, so an upload racing a delete cannot leave an orphan row
        behind its thread.
        """
        ...

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        """Look up one attachment; a miss returns ``None``, not an error."""
        ...

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        """Every attachment of one thread, oldest first."""
        ...

    def record_units(
        self,
        *,
        attachment_id: str,
        version: int,
        units: Sequence[ParsedAttachmentUnit],
    ) -> int:
        """Replace the parsed frozen units of ``version``; return the count.

        Raises :class:`AttachmentThreadGoneError` when the attachment or its
        thread was deleted while the parse was running.
        """
        ...

    def units(
        self, attachment_id: str, version: int
    ) -> tuple[ParsedAttachmentUnit, ...]:
        """The frozen units of one version, ordered by ``unit``."""
        ...

    def clear_units(self, attachment_id: str) -> None:
        """Drop every parsed unit of an attachment (re-preparation, delete)."""
        ...

    def put_index(
        self,
        *,
        attachment_id: str,
        version: int,
        postings: dict[str, Sequence[int]],
    ) -> None:
        """Replace the derived index of ``version`` and mark it usable."""
        ...

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        """The stored postings of one version; empty when it has none."""
        ...

    def indexed_versions(self, attachment_id: str) -> tuple[int, ...]:
        """Versions that currently hold derived index rows.

        At most one version of an attachment may be active at a time, so
        concurrent re-preparation is observable here.
        """
        ...

    def clear_index(self, attachment_id: str) -> None:
        """Drop every posting of an attachment and mark it not indexed."""
        ...

    def mark_ready(
        self, *, attachment_id: str, version: int, unit_count: int
    ) -> AttachmentRecord:
        """Publish ``version`` as ``ready``.

        Refuses a version whose derived data is not usable — the store
        checks parsed units and index postings for THAT version before the
        status flips, so a restart can never report a stale ``ready``.
        """
        ...

    def mark_failed(
        self, *, attachment_id: str, version: int, reason: str
    ) -> AttachmentRecord:
        """Record a named preparation failure."""
        ...

    def incomplete(self) -> list[AttachmentRecord]:
        """Attachments that must be resumed or rebuilt before they are usable.

        Covers ``parsing`` rows and any ``ready`` row whose parsed units or
        index postings for the current version are missing. ``uploaded``
        rows are deliberately NOT here: they hold durable bytes nobody has
        asked to prepare, and a restart must not start work on them.
        """
        ...

    def delete_for_thread(self, thread_id: str) -> list[str]:
        """Remove an entire thread's metadata, units, and postings.

        Returns the object keys whose raw bytes the caller must still
        remove. Idempotent: an unknown or already-removed thread yields an
        empty list and removes nothing.
        """
        ...


def _attachment(
    record: AttachmentRecord, **changes: object
) -> AttachmentRecord:
    """Return a copy of ``record`` with ``changes`` applied.

    Shared by both implementations so the status seam validates the closed
    enum on every transition instead of trusting the caller.
    """
    if "status" in changes:
        changes["status"] = _validate_status(str(changes["status"]))
    return replace(record, **changes)


# ----- shared helpers for the two implementations below -----
#
# Imported with the implementations so the contract header above stays free
# of driver- and clock-shaped names. ``psycopg`` is already a hard dependency
# of the thread store these tables hang off.

import psycopg
from datetime import timezone
from typing import Any

from fin.host.thread_store import ThreadStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: object) -> str:
    """Reject a blank identity before it can address an unreadable row."""
    if not isinstance(value, str) or not value.strip():
        raise AttachmentStoreError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_version(version: object) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise AttachmentStoreError(
            f"attachment version must be a non-negative int, got {version!r}"
        )
    return version


def _new_record(
    *,
    thread_id: str,
    attachment_id: str,
    filename: str,
    size_bytes: int,
    sha256: str,
    object_key: str,
) -> AttachmentRecord:
    """Build the ``uploaded`` baseline row both implementations persist.

    WHY one builder: every derived write is only safe if the row it addresses
    started from the same closed baseline (no version, no units, no index),
    and an attachment id that is blank or whitespace cannot be read back.
    """
    for name, value in (
        ("thread_id", thread_id),
        ("attachment_id", attachment_id),
        ("filename", filename),
        ("sha256", sha256),
        ("object_key", object_key),
    ):
        _require_text(name, value)
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise AttachmentStoreError(
            f"size_bytes must be a non-negative int, got {size_bytes!r}"
        )
    stamp = _now()
    return AttachmentRecord(
        attachment_id=attachment_id,
        thread_id=thread_id,
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        object_key=object_key,
        status="uploaded",
        version=0,
        unit_count=0,
        indexed=False,
        failure_reason=None,
        created_at=stamp,
        updated_at=stamp,
    )


def _normalized_units(
    units: Sequence[ParsedAttachmentUnit],
) -> dict[int, str]:
    """Coerce a unit batch to ``ordinal -> text``.

    WHY a mapping: ``unit`` is a one-based locator ordinal, so a batch that
    repeats an ordinal addresses the same slot and the later text wins — the
    same last-write rule the Postgres upsert applies.
    """
    rows: dict[int, str] = {}
    for item in units:
        if not isinstance(item, ParsedAttachmentUnit):
            raise AttachmentStoreError(
                f"record_units expects ParsedAttachmentUnit values, got {item!r}"
            )
        if item.unit < 1:
            raise AttachmentStoreError(
                f"parsed unit ordinal must be >= 1, got {item.unit}"
            )
        rows[int(item.unit)] = item.text
    return rows


def _normalized_postings(
    postings: dict[str, Sequence[int]],
) -> dict[str, tuple[int, ...]]:
    """Coerce a posting table to ``term -> ordered unit ordinals``.

    WHY here and not downstream: a term with no unit is not a posting and an
    ordinal outside the locator space can never be cited, so both stores have
    to refuse the same shapes before either writes a row.
    """
    normalized: dict[str, tuple[int, ...]] = {}
    for raw_term, raw_units in postings.items():
        term = _require_text("index term", raw_term)
        try:
            ordered = tuple(int(unit) for unit in raw_units)
        except (TypeError, ValueError) as exc:
            raise AttachmentStoreError(
                f"posting {term!r} must map integer unit ordinals, got {raw_units!r}"
            ) from exc
        if not ordered:
            continue  # EXIT: an empty posting carries no locatable unit.
        if any(unit < 1 for unit in ordered):
            raise AttachmentStoreError(
                f"posting {term!r} must use one-based unit ordinals, got {ordered}"
            )
        normalized[term] = ordered
    return normalized


class _MutableAttachment:
    """Internal cell: one record plus the derived rows of every version."""

    __slots__ = ("postings", "record", "units")

    def __init__(self, record: AttachmentRecord) -> None:
        self.record = record
        self.units: dict[tuple[int, int], str] = {}
        self.postings: dict[int, dict[str, tuple[int, ...]]] = {}


class InMemoryAttachmentStore:
    """Dict-backed attachment store guarded by a single ``threading.Lock``.

    The default boot without Postgres and the reference the Postgres
    implementation is measured against. Thread liveness is delegated to a
    :class:`~fin.host.thread_store.ThreadStore`, so "the thread is gone" has
    one authority in both implementations instead of two dictionaries that
    can disagree.
    """

    def __init__(self, threads: ThreadStore) -> None:
        self._threads = threads
        self._lock = threading.Lock()
        self._attachments: dict[str, _MutableAttachment] = {}

    # -- race arbitration --

    def _live_cell(self, attachment_id: str) -> _MutableAttachment:
        """The cell for ``attachment_id``, or a typed loss of the race.

        WHY it also drops the cell: dropping the attachment rows when their
        thread row is already gone is the in-memory analogue of the Postgres
        ``ON DELETE CASCADE``, and it is the only place the orphan can be
        collected — a preparation that resumes against a dead thread must
        discard its derived data rather than re-attach it.

        Callers hold ``self._lock``, which is what makes the liveness check
        and the following mutation one atomic decision.
        """
        cell = self._attachments.get(attachment_id)
        if cell is None:
            raise AttachmentThreadGoneError(
                f"attachment {attachment_id!r} is gone; discarding derived write"
            )
        if self._threads.get_thread(cell.record.thread_id) is None:
            del self._attachments[attachment_id]
            raise AttachmentThreadGoneError(
                f"thread {cell.record.thread_id!r} is gone for attachment "
                f"{attachment_id!r}; discarding derived write"
            )
        return cell

    def _owned(self, attachment_id: str) -> _MutableAttachment | None:
        """Read-path lookup: hidden when the thread row is gone.

        WHY reads filter too: a metadata row that outlived its thread would
        make ``get()`` disagree with what the Postgres cascade guarantees.
        """
        cell = self._attachments.get(attachment_id)
        if cell is None:
            return None
        if self._threads.get_thread(cell.record.thread_id) is None:
            return None
        return cell

    # -- metadata rows --

    def create(
        self,
        *,
        thread_id: str,
        attachment_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> AttachmentRecord:
        record = _new_record(
            thread_id=thread_id,
            attachment_id=attachment_id,
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            object_key=object_key,
        )
        # WHY liveness, duplicate check, and insert share one critical
        # section: releasing the lock between them lets a thread delete land
        # in the gap and leave an attachment row no thread owns anymore.
        with self._lock:
            if self._threads.get_thread(thread_id) is None:
                raise AttachmentThreadGoneError(
                    f"thread {thread_id!r} is not live for attachment "
                    f"{attachment_id!r}; refusing to create an orphan row"
                )
            if attachment_id in self._attachments:
                raise AttachmentStoreError(
                    f"attachment {attachment_id!r} already exists"
                )
            self._attachments[attachment_id] = _MutableAttachment(record)
            return _attachment(record)

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: a lookup miss is the documented ``None``, not an error.
            return None if cell is None else _attachment(cell.record)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        # WHY insertion order: attachment ids are minted per upload, so dict
        # order is the arrival order and does not depend on two uploads
        # landing in the same clock tick.
        with self._lock:
            if self._threads.get_thread(thread_id) is None:
                return []  # EXIT: a thread with no live row has no attachments.
            return [
                _attachment(cell.record)
                for cell in self._attachments.values()
                if cell.record.thread_id == thread_id
            ]

    def incomplete(self) -> list[AttachmentRecord]:
        """Rows to resume: ``parsing``, or ``ready`` with derived data lost."""
        with self._lock:
            rows: list[AttachmentRecord] = []
            live: dict[str, bool] = {}
            for cell in self._attachments.values():
                record = cell.record
                thread_id = record.thread_id
                if thread_id not in live:
                    live[thread_id] = self._threads.get_thread(thread_id) is not None
                # WHY the liveness filter: an attachment whose thread row went
                # first is the Postgres cascade's job to remove, so it is
                # neither a resume candidate nor a listing.
                if not live[thread_id]:
                    continue
                if record.status == "parsing":
                    rows.append(_attachment(record))
                elif record.status == "ready" and not self._derived_present(cell):
                    rows.append(_attachment(record))
            return rows

    @staticmethod
    def _derived_present(cell: _MutableAttachment) -> bool:
        version = cell.record.version
        has_units = any(key[0] == version for key in cell.units)
        has_postings = bool(cell.postings.get(version))
        return has_units and has_postings

    # -- parsed frozen units --

    def record_units(
        self,
        *,
        attachment_id: str,
        version: int,
        units: Sequence[ParsedAttachmentUnit],
    ) -> int:
        _require_version(version)
        rows = _normalized_units(units)
        with self._lock:
            cell = self._live_cell(attachment_id)
            cell.units = {
                key: text for key, text in cell.units.items() if key[0] != version
            }
            for ordinal, text in rows.items():
                cell.units[(version, ordinal)] = text
            cell.record = _attachment(cell.record, updated_at=_now())
            return len(rows)

    def units(self, attachment_id: str, version: int) -> tuple[ParsedAttachmentUnit, ...]:
        _require_version(version)
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: an attachment no live thread owns reads back as no units,
            # which is what the Postgres cascade guarantees.
            if cell is None:
                return ()
            rows = {
                ordinal: text
                for (stored_version, ordinal), text in cell.units.items()
                if stored_version == version
            }
            return tuple(
                ParsedAttachmentUnit(unit=ordinal, text=rows[ordinal])
                for ordinal in sorted(rows)
            )

    def clear_units(self, attachment_id: str) -> None:
        with self._lock:
            cell = self._owned(attachment_id)
            if cell is None:
                return  # EXIT: nothing to drop; cleanup is idempotent.
            cell.units = {}
            cell.record = _attachment(cell.record, updated_at=_now())

    # -- derived search index --

    def put_index(
        self,
        *,
        attachment_id: str,
        version: int,
        postings: dict[str, Sequence[int]],
    ) -> None:
        _require_version(version)
        rows = _normalized_postings(postings)
        with self._lock:
            cell = self._live_cell(attachment_id)
            if rows:
                cell.postings[version] = rows
            else:
                cell.postings.pop(version, None)
            # WHY ``indexed`` follows the rows: an empty posting set is not a
            # usable index, so it must not advertise searchability.
            cell.record = _attachment(
                cell.record, indexed=bool(rows), updated_at=_now()
            )

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        _require_version(version)
        with self._lock:
            cell = self._owned(attachment_id)
            stored = None if cell is None else cell.postings.get(version)
            # EXIT: a version with no postings reads back empty, never an error.
            return {} if not stored else dict(stored)

    def indexed_versions(self, attachment_id: str) -> tuple[int, ...]:
        with self._lock:
            cell = self._owned(attachment_id)
            if cell is None:
                return ()  # EXIT: no live owner, so no active index version.
            return tuple(
                sorted(version for version, rows in cell.postings.items() if rows)
            )

    def clear_index(self, attachment_id: str) -> None:
        with self._lock:
            cell = self._owned(attachment_id)
            if cell is None:
                return  # EXIT: nothing to drop; cleanup is idempotent.
            cell.postings = {}
            cell.record = _attachment(cell.record, indexed=False, updated_at=_now())

    # -- publication --

    def mark_ready(
        self, *, attachment_id: str, version: int, unit_count: int
    ) -> AttachmentRecord:
        _require_version(version)
        if isinstance(unit_count, bool) or not isinstance(unit_count, int) or unit_count < 1:
            raise AttachmentStoreError(
                f"mark_ready needs a positive unit_count, got {unit_count!r}"
            )
        with self._lock:
            cell = self._live_cell(attachment_id)
            stored_units = sum(1 for key in cell.units if key[0] == version)
            stored_postings = cell.postings.get(version)
            # WHY the count is compared with the rows instead of trusted:
            # ``ready`` is the promise that the operator's text is citable, so
            # the row may only claim what is actually addressed by units.
            if stored_units != unit_count:
                raise AttachmentStoreError(
                    f"cannot publish version {version} of {attachment_id!r} as "
                    f"ready: {unit_count} units claimed, {stored_units} stored"
                )
            if not stored_postings:
                raise AttachmentStoreError(
                    f"cannot publish version {version} of {attachment_id!r} as "
                    "ready: no index postings stored for that version"
                )
            cell.record = _attachment(
                cell.record,
                status="ready",
                version=version,
                unit_count=unit_count,
                indexed=True,
                failure_reason=None,
                updated_at=_now(),
            )
            return _attachment(cell.record)

    def mark_failed(
        self, *, attachment_id: str, version: int, reason: str
    ) -> AttachmentRecord:
        _require_version(version)
        reason = _require_text("failure reason", reason)
        with self._lock:
            cell = self._live_cell(attachment_id)
            # WHY ``version`` is not stamped: a parsed-content version is
            # minted by a successful preparation, so an abandoned attempt
            # must not take credit for one, and an earlier published version
            # stays addressable while its rows are still on disk.
            cell.record = _attachment(
                cell.record,
                status="failed",
                failure_reason=reason,
                updated_at=_now(),
            )
            return _attachment(cell.record)

    # -- deletion --

    def delete_for_thread(self, thread_id: str) -> list[str]:
        # WHY one lock spans the collect-and-drop: units and postings live on
        # the cell, so removing the cell removes every derived layer in the
        # same step, and a preparation that arrives afterwards loses the
        # race in ``_live_cell``.
        with self._lock:
            keys = [
                cell.record.object_key
                for cell in self._attachments.values()
                if cell.record.thread_id == thread_id
            ]
            for attachment_id in list(self._attachments):
                if self._attachments[attachment_id].record.thread_id == thread_id:
                    del self._attachments[attachment_id]
            # EXIT: an unknown or already-removed thread has no rows to
            # return, which is what makes this call idempotent.
            return keys


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_thread_attachment (
    attachment_id   TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    sha256          TEXT NOT NULL,
    object_key      TEXT NOT NULL,
    status          TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 0,
    unit_count      INTEGER NOT NULL DEFAULT 0,
    indexed         BOOLEAN NOT NULL DEFAULT FALSE,
    failure_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_thread_order_idx
    ON fin_thread_attachment (thread_id, created_at);

CREATE TABLE IF NOT EXISTS fin_thread_attachment_unit (
    attachment_id   TEXT NOT NULL
        REFERENCES fin_thread_attachment(attachment_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    unit            INTEGER NOT NULL,
    text            TEXT NOT NULL,
    PRIMARY KEY (attachment_id, version, unit)
);

CREATE TABLE IF NOT EXISTS fin_thread_attachment_index (
    attachment_id   TEXT NOT NULL
        REFERENCES fin_thread_attachment(attachment_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    term            TEXT NOT NULL,
    units           TEXT NOT NULL,
    PRIMARY KEY (attachment_id, version, term)
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_index_lookup_idx
    ON fin_thread_attachment_index (attachment_id, version);
"""


class PostgresAttachmentStore:
    """psycopg-backed attachment store on the shared ``FIN_CHECKPOINT_URL``.

    Three tables, each carrying ``thread_id`` as an ``ON DELETE CASCADE``
    foreign key to ``fin_thread``: the cascade is what makes a thread delete
    atomic across metadata, parsed units, and postings, and it is the reason
    a late derived write can be detected instead of silently persisted.

    Each public call opens its own short-lived connection, matching
    :class:`~fin.host.thread_store.PostgresThreadStore`: the workspace is a
    local operator server, and this keeps a second pool off the boot path.
    Schema creation is idempotent and runs once per construction.
    """

    _COLUMNS = (
        "attachment_id, thread_id, filename, size_bytes, sha256, object_key, "
        "status, version, unit_count, indexed, failure_reason, created_at, "
        "updated_at"
    )
    # WHY the liveness touch is the first statement of every derived write:
    # it takes the attachment row's lock and drops the row from the
    # transaction's view if ``fin_thread`` no longer has the thread, so a zero
    # rowcount means the delete won and this write must be discarded.
    # ``indexed`` uses COALESCE so a unit-only write cannot revoke a usable
    # index and a term with no unit leaves nothing to mark.
    _TOUCH_SQL = (
        "UPDATE fin_thread_attachment "
        "SET updated_at = now(), indexed = COALESCE(%s, indexed) "
        "WHERE attachment_id = %s "
        "AND thread_id IN (SELECT id FROM fin_thread) "
        "RETURNING thread_id"
    )

    def __init__(self, connection_url: str) -> None:
        self._url = connection_url
        self._setup_lock = threading.Lock()
        with self._setup_lock:
            try:
                with self._connect() as conn:
                    conn.execute(_SCHEMA_SQL)
                    conn.commit()
            except psycopg.errors.UndefinedTable as exc:
                raise AttachmentStoreError(
                    "fin_thread must exist before the attachment tables can "
                    "reference it: construct the Postgres thread store first"
                ) from exc

    def _connect(self) -> Any:
        """A connection that closes when the caller's block ends.

        WHY ``closing``: psycopg's own connection context manager only commits
        or rolls back, so an explicitly unclosed connection would hold a
        server slot until garbage collection — and this store is called once
        per attachment operation for the life of the process.
        """
        import contextlib

        return contextlib.closing(psycopg.connect(self._url))

    @staticmethod
    def _gone(attachment_id: str) -> AttachmentThreadGoneError:
        return AttachmentThreadGoneError(
            f"attachment {attachment_id!r} is gone; discarding derived write"
        )

    def _touch(self, conn: Any, attachment_id: str, *, indexed: bool | None) -> str:
        row = conn.execute(self._TOUCH_SQL, (indexed, attachment_id)).fetchone()
        if row is None:
            raise self._gone(attachment_id)
        return str(row[0])

    @staticmethod
    def _row_to_record(row: Sequence[Any]) -> AttachmentRecord:
        return AttachmentRecord(
            attachment_id=str(row[0]),
            thread_id=str(row[1]),
            filename=str(row[2]),
            size_bytes=int(row[3]),
            sha256=str(row[4]),
            object_key=str(row[5]),
            status=_validate_status(str(row[6])),
            version=int(row[7]),
            unit_count=int(row[8]),
            indexed=bool(row[9]),
            failure_reason=None if row[10] is None else str(row[10]),
            created_at=row[11],
            updated_at=row[12],
        )

    @staticmethod
    def _encode_units(units: Sequence[int]) -> str:
        """Postings store one comma-separated ordinal list per term.

        WHY a text column instead of an array: the read boundary hands out
        plain tuples and the shape has to survive both a driver with and
        without array codecs configured on the connection.
        """
        return ",".join(str(unit) for unit in units)

    @staticmethod
    def _decode_units(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split(",") if part)

    # -- metadata rows --

    def create(
        self,
        *,
        thread_id: str,
        attachment_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> AttachmentRecord:
        record = _new_record(
            thread_id=thread_id,
            attachment_id=attachment_id,
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            object_key=object_key,
        )
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"INSERT INTO fin_thread_attachment ({self._COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {self._COLUMNS}",
                    (
                        record.attachment_id,
                        record.thread_id,
                        record.filename,
                        record.size_bytes,
                        record.sha256,
                        record.object_key,
                        record.status,
                        record.version,
                        record.unit_count,
                        record.indexed,
                        record.failure_reason,
                        record.created_at,
                        record.updated_at,
                    ),
                ).fetchone()
                conn.commit()
        except psycopg.errors.ForeignKeyViolation as exc:
            raise AttachmentThreadGoneError(
                f"thread {thread_id!r} is not live for attachment "
                f"{attachment_id!r}; refusing to create an orphan row"
            ) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise AttachmentStoreError(
                f"attachment {attachment_id!r} already exists"
            ) from exc
        assert row is not None
        return self._row_to_record(row)

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM fin_thread_attachment "
                "WHERE attachment_id = %s",
                (attachment_id,),
            ).fetchone()
            # EXIT: a lookup miss is the documented ``None``, not an error.
            return None if row is None else self._row_to_record(row)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM fin_thread_attachment "
                "WHERE thread_id = %s ORDER BY created_at ASC, attachment_id ASC",
                (thread_id,),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def incomplete(self) -> list[AttachmentRecord]:
        # WHY the predicate is set-based: a row that is ``ready`` without
        # units or postings for ITS OWN version is a lost rebuild, and the
        # two NOT EXISTS clauses answer that in one round trip.
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM fin_thread_attachment a "
                "WHERE status = 'parsing' "
                "OR (status = 'ready' AND ("
                "NOT EXISTS (SELECT 1 FROM fin_thread_attachment_unit u "
                "WHERE u.attachment_id = a.attachment_id AND u.version = a.version) "
                "OR NOT EXISTS (SELECT 1 FROM fin_thread_attachment_index i "
                "WHERE i.attachment_id = a.attachment_id AND i.version = a.version "
                "AND i.units <> ''))) "
                "ORDER BY created_at ASC, attachment_id ASC"
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    # -- parsed frozen units --

    def record_units(
        self,
        *,
        attachment_id: str,
        version: int,
        units: Sequence[ParsedAttachmentUnit],
    ) -> int:
        _require_version(version)
        rows = _normalized_units(units)
        with self._connect() as conn:
            try:
                thread_id = self._touch(conn, attachment_id, indexed=None)
                conn.execute(
                    "DELETE FROM fin_thread_attachment_unit "
                    "WHERE attachment_id = %s AND version = %s",
                    (attachment_id, version),
                )
                for ordinal, text in rows.items():
                    conn.execute(
                        "INSERT INTO fin_thread_attachment_unit "
                        "(attachment_id, thread_id, version, unit, text) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (attachment_id, version, unit) "
                        "DO UPDATE SET text = EXCLUDED.text",
                        (attachment_id, thread_id, version, ordinal, text),
                    )
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return len(rows)

    def units(self, attachment_id: str, version: int) -> tuple[ParsedAttachmentUnit, ...]:
        _require_version(version)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT unit, text FROM fin_thread_attachment_unit "
                "WHERE attachment_id = %s AND version = %s ORDER BY unit ASC",
                (attachment_id, version),
            ).fetchall()
            # EXIT: a version that was never written or already cascaded
            # away reads back as no units.
            return () if not rows else tuple(
                ParsedAttachmentUnit(unit=int(row[0]), text=str(row[1])) for row in rows
            )

    def clear_units(self, attachment_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fin_thread_attachment_unit WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.execute(
                "UPDATE fin_thread_attachment SET updated_at = now() "
                "WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.commit()

    # -- derived search index --

    def put_index(
        self,
        *,
        attachment_id: str,
        version: int,
        postings: dict[str, Sequence[int]],
    ) -> None:
        _require_version(version)
        rows = _normalized_postings(postings)
        with self._connect() as conn:
            try:
                thread_id = self._touch(conn, attachment_id, indexed=bool(rows))
                conn.execute(
                    "DELETE FROM fin_thread_attachment_index "
                    "WHERE attachment_id = %s AND version = %s",
                    (attachment_id, version),
                )
                for term, units in rows.items():
                    conn.execute(
                        "INSERT INTO fin_thread_attachment_index "
                        "(attachment_id, thread_id, version, term, units) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (attachment_id, version, term) "
                        "DO UPDATE SET units = EXCLUDED.units",
                        (
                            attachment_id,
                            thread_id,
                            version,
                            term,
                            self._encode_units(units),
                        ),
                    )
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        _require_version(version)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT term, units FROM fin_thread_attachment_index "
                "WHERE attachment_id = %s AND version = %s AND units <> '' "
                "ORDER BY term ASC",
                (attachment_id, version),
            ).fetchall()
            return {str(row[0]): self._decode_units(str(row[1])) for row in rows}

    def indexed_versions(self, attachment_id: str) -> tuple[int, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT version FROM fin_thread_attachment_index "
                "WHERE attachment_id = %s AND units <> '' ORDER BY version ASC",
                (attachment_id,),
            ).fetchall()
            return tuple(int(row[0]) for row in rows)

    def clear_index(self, attachment_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fin_thread_attachment_index WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.execute(
                "UPDATE fin_thread_attachment SET indexed = FALSE, updated_at = now() "
                "WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.commit()

    # -- publication --

    def mark_ready(
        self, *, attachment_id: str, version: int, unit_count: int
    ) -> AttachmentRecord:
        _require_version(version)
        if isinstance(unit_count, bool) or not isinstance(unit_count, int) or unit_count < 1:
            raise AttachmentStoreError(
                f"mark_ready needs a positive unit_count, got {unit_count!r}"
            )
        with self._connect() as conn:
            try:
                self._touch(conn, attachment_id, indexed=None)
                stored_units = int(
                    conn.execute(
                        "SELECT count(*) FROM fin_thread_attachment_unit "
                        "WHERE attachment_id = %s AND version = %s",
                        (attachment_id, version),
                    ).fetchone()[0]
                )
                stored_postings = int(
                    conn.execute(
                        "SELECT count(*) FROM fin_thread_attachment_index "
                        "WHERE attachment_id = %s AND version = %s AND units <> ''",
                        (attachment_id, version),
                    ).fetchone()[0]
                )
                # WHY the counts come from the rows and not the caller:
                # ``ready`` is the promise that the operator's text is both
                # readable and citable for THIS version, so the row may only
                # claim what the derived tables actually address.
                if stored_units != unit_count or stored_postings == 0:
                    raise AttachmentStoreError(
                        f"cannot publish version {version} of {attachment_id!r} "
                        f"as ready: {stored_units} units and {stored_postings} "
                        "postings stored"
                    )
                row = conn.execute(
                    "UPDATE fin_thread_attachment SET status = 'ready', "
                    "version = %s, unit_count = %s, indexed = TRUE, "
                    "failure_reason = NULL, updated_at = now() "
                    "WHERE attachment_id = %s "
                    "AND thread_id IN (SELECT id FROM fin_thread) "
                    f"RETURNING {self._COLUMNS}",
                    (version, unit_count, attachment_id),
                ).fetchone()
                if row is None:
                    raise self._gone(attachment_id)
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return self._row_to_record(row)

    def mark_failed(
        self, *, attachment_id: str, version: int, reason: str
    ) -> AttachmentRecord:
        _require_version(version)
        reason = _require_text("failure reason", reason)
        with self._connect() as conn:
            try:
                # WHY ``version`` is not written: a parsed-content version is
                # minted by a successful preparation, so an abandoned attempt
                # must not take credit for one, and an earlier published
                # version stays addressable while its rows are still on disk.
                row = conn.execute(
                    "UPDATE fin_thread_attachment SET status = 'failed', "
                    "failure_reason = %s, updated_at = now() "
                    "WHERE attachment_id = %s "
                    "AND thread_id IN (SELECT id FROM fin_thread) "
                    f"RETURNING {self._COLUMNS}",
                    (reason, attachment_id),
                ).fetchone()
                if row is None:
                    raise self._gone(attachment_id)
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return self._row_to_record(row)

    # -- deletion --

    def delete_for_thread(self, thread_id: str) -> list[str]:
        # WHY the keys are read before the delete in the same transaction:
        # the caller still has to remove the raw bytes, and the cascade below
        # is the only place that knows every attachment this thread owned.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT object_key FROM fin_thread_attachment "
                "WHERE thread_id = %s ORDER BY created_at ASC, attachment_id ASC",
                (thread_id,),
            ).fetchall()
            conn.execute(
                "DELETE FROM fin_thread_attachment WHERE thread_id = %s",
                (thread_id,),
            )
            conn.commit()
            # EXIT: an unknown or already-removed thread has no rows, which is
            # what makes this call idempotent.
            return [str(row[0]) for row in rows]


__all__ = [
    "AttachmentRecord",
    "AttachmentStore",
    "AttachmentStoreError",
    "AttachmentStatus",
    "AttachmentThreadGoneError",
    "InMemoryAttachmentStore",
    "ParsedAttachmentUnit",
    "PostgresAttachmentStore",
]
