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
from datetime import datetime
from typing import Literal, Protocol, Sequence, cast

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fin.host.db_pool import PostgresPool


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
        """Soft-disable one row, recording why and when."""
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
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(MEMORY_SCHEMA_SQL)
            conn.commit()

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


def _row_from_record(record: dict[str, object]) -> MemoryRow:
    metadata = record["metadata"]
    return MemoryRow(
        id=str(record["id"]),
        text=str(record["text"]),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        created_at=_as_datetime(record["created_at"]),
        expires_at=_as_datetime(record["expires_at"]),
        hit_count=int(record["hit_count"]),  # type: ignore[arg-type]
        status=cast(MemoryStatus, record["status"]),
        disabled_reason=_as_optional_str(record["disabled_reason"]),
        disabled_at=_as_datetime(record["disabled_at"]),
        transported_at=_as_datetime(record["transported_at"]),
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


class InProcessDreamCursorStore:
    """Process-local cursor. Loses its position on restart, like its store."""

    def __init__(self) -> None:
        self._cursor = DreamCursor()
        self._lock = threading.RLock()

    def load(self) -> DreamCursor:
        with self._lock:
            return self._cursor

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


class PostgresDreamCursorStore:
    """Cursor backed by the singleton ``fin_memory_dream_cursor`` row."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool
        self.setup()

    def setup(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(MEMORY_SCHEMA_SQL)
            conn.commit()

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

        seen = record["seen_thread_ids"]
        seen_ids = frozenset(seen) if isinstance(seen, list) else frozenset()
        return DreamCursor(
            last_dream_at=_as_datetime(record["last_dream_at"]),
            seen_thread_ids=seen_ids,
            last_status=_as_optional_str(record["last_status"]),
        )

    def advance(
        self,
        *,
        at: datetime,
        seen_thread_ids: frozenset[str],
        last_status: str,
    ) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {CURSOR_TABLE} "
                    "(id, last_dream_at, seen_thread_ids, last_status) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "last_dream_at = EXCLUDED.last_dream_at, "
                    "seen_thread_ids = EXCLUDED.seen_thread_ids, "
                    "last_status = EXCLUDED.last_status",
                    (CURSOR_ID, at, Jsonb(sorted(seen_thread_ids)), last_status),
                )
            conn.commit()


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
