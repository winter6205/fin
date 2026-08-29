"""Memory row storage and the dream cursor, behind one contract.

ADR 0035 puts the memory working buffer in local Postgres. Both halves of
this module therefore ship two implementations behind one protocol: an
in-process store used by tests and by the process-local buffer, and a
Postgres store used when memory is ON with a configured ``FIN_CHECKPOINT_URL``.

``fin_memory`` and ``fin_memory_dream_cursor`` are independent tables with
idempotent DDL. They deliberately do not touch the checkpointer's tables —
memory bodies are not checkpoint state (``CONTEXT.md:38``). The cursor lives
here rather than in Redis for the same reason (``CONTEXT.md:42``).
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol, Sequence, cast

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fin.host.db_pool import PostgresPool
from fin.host.memory_port import DreamCursorError, DreamInputError


MEMORY_TABLE = "fin_memory"
CURSOR_TABLE = "fin_memory_dream_cursor"

# The cursor is a singleton row: one dream timeline per fin deployment.
CURSOR_ID = "default"

MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_memory (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    hit_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    disabled_reason TEXT,
    disabled_at TIMESTAMPTZ,
    transported_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fin_memory_dream_cursor (
    id TEXT PRIMARY KEY,
    last_dream_at TIMESTAMPTZ,
    seen_thread_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_status TEXT
);
"""

_MEMORY_COLUMNS = (
    "id, text, metadata, created_at, expires_at, hit_count, status, "
    "disabled_reason, disabled_at, transported_at"
)


MemoryStatus = Literal["active", "disabled"]


@dataclass
class MemoryRow:
    """One advisory memory row as the store sees it."""

    id: str
    text: str
    created_at: datetime
    metadata: dict[str, object] = field(default_factory=dict)
    expires_at: datetime | None = None
    hit_count: int = 0
    status: MemoryStatus = "active"
    disabled_reason: str | None = None
    disabled_at: datetime | None = None
    transported_at: datetime | None = None

    @property
    def thread_id(self) -> str | None:
        """Return the conversation that produced this row, if recorded.

        ``thread_id`` is the gate's only data source (SC8). A row without
        one is real memory that simply cannot be counted, so callers get
        ``None`` and decide how to degrade rather than dropping the row.
        """
        value = self.metadata.get("thread_id")
        if isinstance(value, str) and value.strip():
            return value
        return None


class MemoryStore(Protocol):
    """Row-level storage behind the memory buffer."""

    def add(self, row: MemoryRow) -> None:
        """Insert one row."""
        ...

    def rows(self, *, include_disabled: bool = True) -> list[MemoryRow]:
        """Return row copies ordered by creation time."""
        ...

    def bump_hit_counts(self, ids: Sequence[str]) -> None:
        """Add one hit to each id atomically; unknown ids are ignored."""
        ...

    def disable(self, row_id: str, *, reason: str, at: datetime) -> None:
        """Soft-disable one row, recording why and when.

        Unknown ids are ignored: callers select candidates from
        :meth:`rows`, so an id that has gone missing is a row another
        worker already retired, not an error.
        """
        ...

    def mark_transported(self, ids: Sequence[str], *, at: datetime) -> None:
        """Retire rows the backend confirmed it stored."""
        ...

    def delete_transported_before(self, cutoff: datetime) -> int:
        """Hard-delete transported rows older than ``cutoff``.

        Only transported rows are eligible; a local buffer row that was
        never handed to a backend is never deleted (SC19).
        """
        ...


class InProcessMemoryStore:
    """Process-local row store guarded by a lock.

    Used by the in-process buffer and by tests that must stay hermetic.
    It is deliberately not durable: restart persistence is the Postgres
    store's contract, not this one's.
    """

    def __init__(self) -> None:
        self._rows: dict[str, MemoryRow] = {}
        self._lock = threading.RLock()

    def add(self, row: MemoryRow) -> None:
        with self._lock:
            self._rows[row.id] = copy.deepcopy(row)

    def rows(self, *, include_disabled: bool = True) -> list[MemoryRow]:
        with self._lock:
            rows = list(self._rows.values())
        if not include_disabled:
            rows = [row for row in rows if row.status == "active"]
        rows.sort(key=lambda row: (row.created_at, row.id))
        return [copy.deepcopy(row) for row in rows]

    def bump_hit_counts(self, ids: Sequence[str]) -> None:
        with self._lock:
            for row_id in ids:
                row = self._rows.get(row_id)
                if row is not None:
                    row.hit_count += 1

    def disable(self, row_id: str, *, reason: str, at: datetime) -> None:
        with self._lock:
            row = self._rows.get(row_id)
            if row is None:
                return
            row.status = "disabled"
            row.disabled_reason = reason
            row.disabled_at = at

    def mark_transported(self, ids: Sequence[str], *, at: datetime) -> None:
        with self._lock:
            for row_id in ids:
                row = self._rows.get(row_id)
                if row is None:
                    continue
                row.status = "disabled"
                row.disabled_reason = "transported"
                row.disabled_at = at
                row.transported_at = at

    def delete_transported_before(self, cutoff: datetime) -> int:
        with self._lock:
            stale = [
                row_id
                for row_id, row in self._rows.items()
                if row.transported_at is not None and row.transported_at <= cutoff
            ]
            for row_id in stale:
                del self._rows[row_id]
        return len(stale)


class PostgresMemoryStore:
    """Row store backed by ``fin_memory`` over an explicit Postgres pool.

    Hit counting is a single ``UPDATE ... SET hit_count = hit_count + 1`` so
    concurrent recalls accumulate instead of overwriting each other (SC7).
    """

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool
        self.setup()

    def setup(self) -> None:
        """Create the memory tables if they are missing. Safe to re-run."""
        ensure_memory_schema(self._pool)

    def add(self, row: MemoryRow) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO fin_memory "
                    f"({_MEMORY_COLUMNS}) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        row.id,
                        row.text,
                        Jsonb(row.metadata),
                        row.created_at,
                        row.expires_at,
                        row.hit_count,
                        row.status,
                        row.disabled_reason,
                        row.disabled_at,
                        row.transported_at,
                    ),
                )
            conn.commit()

    def rows(self, *, include_disabled: bool = True) -> list[MemoryRow]:
        sql = f"SELECT {_MEMORY_COLUMNS} FROM fin_memory"
        if not include_disabled:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY created_at, id"

        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                records = cur.fetchall()
        return [_row_from_record(record) for record in records]

    def bump_hit_counts(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fin_memory SET hit_count = hit_count + 1 "
                    "WHERE id = ANY(%s)",
                    (list(ids),),
                )
            conn.commit()

    def disable(self, row_id: str, *, reason: str, at: datetime) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fin_memory SET status = 'disabled', "
                    "disabled_reason = %s, disabled_at = %s WHERE id = %s",
                    (reason, at, row_id),
                )
            conn.commit()

    def mark_transported(self, ids: Sequence[str], *, at: datetime) -> None:
        if not ids:
            return
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fin_memory SET status = 'disabled', "
                    "disabled_reason = 'transported', disabled_at = %s, "
                    "transported_at = %s WHERE id = ANY(%s)",
                    (at, at, list(ids)),
                )
            conn.commit()

    def delete_transported_before(self, cutoff: datetime) -> int:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM fin_memory WHERE transported_at IS NOT NULL "
                    "AND transported_at <= %s",
                    (cutoff,),
                )
                deleted = cur.rowcount
            conn.commit()
        return deleted


def _row_from_record(record: dict[str, object]) -> MemoryRow:
    metadata = record["metadata"]
    if not isinstance(metadata, dict):
        raise DreamInputError(
            f"memory row metadata is not an object: {metadata!r}"
        )
    return MemoryRow(
        id=str(record["id"]),
        text=str(record["text"]),
        metadata=dict(metadata),
        created_at=_as_created_at(record["created_at"]),
        expires_at=_as_datetime(record["expires_at"]),
        hit_count=int(record["hit_count"]),  # type: ignore[arg-type]
        status=cast(MemoryStatus, record["status"]),
        disabled_reason=_as_optional_str(record["disabled_reason"]),
        disabled_at=_as_datetime(record["disabled_at"]),
        transported_at=_as_datetime(record["transported_at"]),
    )


def ensure_memory_schema(pool: PostgresPool) -> None:
    """Create the memory tables if they are missing. Safe to re-run.

    Shared by both Postgres stores so the DDL is stated once, and so
    constructing a buffer's two stores runs it once per boot path
    rather than twice.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MEMORY_SCHEMA_SQL)
        conn.commit()


def _as_created_at(value: object) -> datetime:
    """Coerce the non-nullable ``created_at`` column.

    Loud rather than silent, for the same reason
    :func:`parse_dream_cursor` rejects corruption instead of resetting:
    a row whose creation time is unusable would otherwise reach the
    scorer as ``None``, where ``now - row.created_at`` raises
    ``TypeError`` — outside the ``MemoryDreamError`` family ``run()``
    catches, leaving the cursor claimed and dream stalled for 24h.
    """
    if isinstance(value, datetime):
        return value
    raise DreamInputError(
        f"memory row created_at is not a datetime: {value!r}"
    )


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return None


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


@dataclass(frozen=True)
class DreamCursor:
    """Where the last dream left off.

    ``seen_thread_ids`` are the conversations already counted toward a
    previous gate, so "new distinct threads" is
    ``current_threads - seen_thread_ids``.
    """

    last_dream_at: datetime | None = None
    seen_thread_ids: frozenset[str] = frozenset()
    last_status: str | None = None


def parse_dream_cursor(
    last_dream_at: object,
    seen_thread_ids: object,
    last_status: object,
) -> DreamCursor:
    """Build a cursor from a stored payload, rejecting corruption.

    Both cursor stores funnel through here so a corrupt cursor raises
    :class:`DreamCursorError` in the process-local store and in Postgres
    alike, instead of one silently resetting and the other crashing.
    """
    if last_dream_at is not None and not isinstance(last_dream_at, datetime):
        raise DreamCursorError(
            f"dream cursor last_dream_at is not a datetime: {last_dream_at!r}"
        )
    if isinstance(seen_thread_ids, (str, bytes)) or not isinstance(
        seen_thread_ids, (list, tuple, set, frozenset)
    ):
        raise DreamCursorError(
            f"dream cursor seen_thread_ids is not a list of ids: "
            f"{seen_thread_ids!r}"
        )
    if not all(isinstance(value, str) for value in seen_thread_ids):
        raise DreamCursorError(
            f"dream cursor seen_thread_ids holds a non-string: "
            f"{seen_thread_ids!r}"
        )
    if last_status is not None and not isinstance(last_status, str):
        raise DreamCursorError(
            f"dream cursor last_status is not a string: {last_status!r}"
        )
    return DreamCursor(
        last_dream_at=last_dream_at,
        seen_thread_ids=frozenset(seen_thread_ids),
        last_status=last_status,
    )


class DreamCursorStore(Protocol):
    """Durable dream cursor, deliberately not in Redis (``CONTEXT.md:42``)."""

    def load(self) -> DreamCursor:
        """Return the stored cursor, or the never-dreamed default."""
        ...

    def advance(
        self,
        *,
        at: datetime,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> None:
        """Replace the cursor with the state after one dream window."""
        ...

    def claim(
        self,
        *,
        at: datetime,
        min_interval: timedelta,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> bool:
        """Take the window that opened at ``at``, or report a loss.

        Compare-and-swap on ``last_dream_at``: the write only lands when
        the previous dream is at least ``min_interval`` old. Concurrent
        callers therefore lose the race instead of running the dream twice
        (SC16).
        """
        ...


class InProcessDreamCursorStore:
    """Process-local cursor. Loses its position on restart, like its store."""

    def __init__(self) -> None:
        self._cursor = DreamCursor()
        self._lock = threading.RLock()

    def load(self) -> DreamCursor:
        with self._lock:
            cursor = self._cursor
        return parse_dream_cursor(
            cursor.last_dream_at, sorted(cursor.seen_thread_ids), cursor.last_status
        )

    def advance(
        self,
        *,
        at: datetime,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> None:
        with self._lock:
            self._cursor = DreamCursor(
                last_dream_at=at,
                seen_thread_ids=frozenset(seen_thread_ids),
                last_status=last_status,
            )

    def claim(
        self,
        *,
        at: datetime,
        min_interval: timedelta,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> bool:
        with self._lock:
            previous = self._cursor.last_dream_at
            if previous is not None and at - previous < min_interval:
                return False
            self._cursor = DreamCursor(
                last_dream_at=at,
                seen_thread_ids=frozenset(seen_thread_ids),
                last_status=last_status,
            )
            return True


class PostgresDreamCursorStore:
    """Cursor backed by the singleton ``fin_memory_dream_cursor`` row."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool
        self.setup()

    def setup(self) -> None:
        """Create the memory tables if they are missing. Safe to re-run."""
        ensure_memory_schema(self._pool)

    def load(self) -> DreamCursor:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT last_dream_at, seen_thread_ids, last_status "
                    f"FROM {CURSOR_TABLE} WHERE id = %s",
                    (CURSOR_ID,),
                )
                record = cur.fetchone()

        if record is None:
            return DreamCursor()

        return parse_dream_cursor(
            record["last_dream_at"],
            record["seen_thread_ids"],
            record["last_status"],
        )

    def _upsert(
        self,
        *,
        at: datetime,
        seen_thread_ids: frozenset[str],
        last_status: str,
        not_before: datetime | None,
    ) -> int:
        """Write the singleton cursor row and report how many rows moved.

        ``not_before`` turns the write into a compare-and-swap: the row is
        only replaced when the previous dream is old enough, so the second
        of two concurrent callers moves nothing and loses the window.
        """
        guard = ""
        params: list[object] = [
            CURSOR_ID,
            at,
            Jsonb(sorted(seen_thread_ids)),
            last_status,
        ]
        if not_before is not None:
            guard = (
                f" WHERE {CURSOR_TABLE}.last_dream_at IS NULL OR "
                f"{CURSOR_TABLE}.last_dream_at <= %s"
            )
            params.append(not_before)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {CURSOR_TABLE} "
                    "(id, last_dream_at, seen_thread_ids, last_status) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "last_dream_at = EXCLUDED.last_dream_at, "
                    "seen_thread_ids = EXCLUDED.seen_thread_ids, "
                    "last_status = EXCLUDED.last_status" + guard,
                    params,
                )
                moved = cur.rowcount
            conn.commit()
        return moved

    def advance(
        self,
        *,
        at: datetime,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> None:
        self._upsert(
            at=at,
            seen_thread_ids=seen_thread_ids,
            last_status=last_status,
            not_before=None,
        )

    def claim(
        self,
        *,
        at: datetime,
        min_interval: timedelta,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> bool:
        return (
            self._upsert(
                at=at,
                seen_thread_ids=seen_thread_ids,
                last_status=last_status,
                not_before=at - min_interval,
            )
            > 0
        )


__all__ = [
    "CURSOR_ID",
    "CURSOR_TABLE",
    "DreamCursor",
    "DreamCursorStore",
    "InProcessDreamCursorStore",
    "InProcessMemoryStore",
    "MEMORY_SCHEMA_SQL",
    "MEMORY_TABLE",
    "MemoryRow",
    "MemoryStatus",
    "MemoryStore",
    "PostgresDreamCursorStore",
    "PostgresMemoryStore",
]
