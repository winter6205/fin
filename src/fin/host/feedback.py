"""Thumbs feedback persistence for workspace turns (T8).

Feedback is deliberately a side record keyed by ``run_id``.  It contains no
answer, citation, or evidence fields, so changing a thumb cannot participate
in citation verification or retrieve ranking.  HTTP wiring is a later
workspace ticket; callers inject a store into the persistence seam here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

FeedbackValue = Literal["up", "down"]
MAX_RUN_ID_LENGTH = 255


class FeedbackError(Exception):
    """Base class for typed feedback failures."""


class InvalidFeedbackError(FeedbackError):
    """The run id or thumb value is outside the feedback contract."""


class FeedbackPersistenceError(FeedbackError):
    """The feedback store could not complete a persistence operation."""


@dataclass(frozen=True)
class FeedbackRecord:
    """The latest thumb selected for one run."""

    run_id: str
    value: FeedbackValue
    created_at: datetime
    updated_at: datetime


class FeedbackStore(Protocol):
    """Persistence contract shared by memory and PostgreSQL stores."""

    def set_feedback(self, run_id: str, value: str) -> FeedbackRecord:
        """Create or replace the latest feedback for ``run_id``."""
        ...

    def get_feedback(self, run_id: str) -> FeedbackRecord | None:
        """Return the latest feedback, or ``None`` when no thumb exists."""
        ...

    def list_feedback(self) -> list[FeedbackRecord]:
        """Return all feedback records in newest-update-first order."""
        ...


def _validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str):
        raise InvalidFeedbackError("run_id must be a non-empty string")
    normalized = run_id.strip()
    if not normalized:
        raise InvalidFeedbackError("run_id must be a non-empty string")
    if len(normalized) > MAX_RUN_ID_LENGTH:
        raise InvalidFeedbackError(
            f"run_id must not exceed {MAX_RUN_ID_LENGTH} characters"
        )
    return normalized


def _validate_value(value: object) -> FeedbackValue:
    if value == "up":
        return "up"
    if value == "down":
        return "down"
    raise InvalidFeedbackError("feedback value must be 'up' or 'down'")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryFeedbackStore:
    """Thread-safe store used by tests and local callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, FeedbackRecord] = {}

    def set_feedback(self, run_id: str, value: str) -> FeedbackRecord:
        normalized_id = _validate_run_id(run_id)
        normalized_value = _validate_value(value)
        now = _now()
        with self._lock:
            prior = self._records.get(normalized_id)
            record = FeedbackRecord(
                run_id=normalized_id,
                value=normalized_value,
                created_at=prior.created_at if prior else now,
                updated_at=now,
            )
            self._records[normalized_id] = record
            return record

    def get_feedback(self, run_id: str) -> FeedbackRecord | None:
        normalized_id = _validate_run_id(run_id)
        with self._lock:
            return self._records.get(normalized_id)

    def list_feedback(self) -> list[FeedbackRecord]:
        with self._lock:
            return sorted(
                self._records.values(),
                key=lambda record: record.updated_at,
                reverse=True,
            )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_run_feedback (
    run_id      TEXT PRIMARY KEY REFERENCES fin_run(id) ON DELETE CASCADE,
    value       TEXT NOT NULL CHECK (value IN ('up', 'down')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresFeedbackStore:
    """PostgreSQL implementation using an injected pool-like object.

    The pool only needs a ``connection()`` context manager.  This accepts the
    T1 ``PostgresPool`` as well as a small fake pool in unit tests, without
    coupling T8 to pool construction or workspace route ownership.
    """

    def __init__(self, pool: Any, *, initialize: bool = True) -> None:
        self._pool = pool
        if initialize:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        try:
            with self._pool.connection() as connection:
                connection.execute(_SCHEMA_SQL)
                connection.commit()
        except FeedbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate storage boundary
            raise FeedbackPersistenceError(
                "feedback storage could not initialize"
            ) from exc

    def set_feedback(self, run_id: str, value: str) -> FeedbackRecord:
        normalized_id = _validate_run_id(run_id)
        normalized_value = _validate_value(value)
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO fin_run_feedback (run_id, value)
                    VALUES (%s, %s)
                    ON CONFLICT (run_id) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                    RETURNING run_id, value, created_at, updated_at
                    """,
                    (normalized_id, normalized_value),
                ).fetchone()
                connection.commit()
            if row is None:
                raise FeedbackPersistenceError(
                    "feedback storage returned no persisted record"
                )
            return _row_to_record(row)
        except FeedbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate storage boundary
            raise FeedbackPersistenceError(
                "feedback storage could not persist feedback"
            ) from exc

    def get_feedback(self, run_id: str) -> FeedbackRecord | None:
        normalized_id = _validate_run_id(run_id)
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT run_id, value, created_at, updated_at
                    FROM fin_run_feedback
                    WHERE run_id = %s
                    """,
                    (normalized_id,),
                ).fetchone()
            return _row_to_record(row) if row is not None else None
        except FeedbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate storage boundary
            raise FeedbackPersistenceError(
                "feedback storage could not read feedback"
            ) from exc

    def list_feedback(self) -> list[FeedbackRecord]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id, value, created_at, updated_at
                    FROM fin_run_feedback
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            return [_row_to_record(row) for row in rows]
        except FeedbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate storage boundary
            raise FeedbackPersistenceError(
                "feedback storage could not list feedback"
            ) from exc


def _row_to_record(row: Any) -> FeedbackRecord:
    run_id, value, created_at, updated_at = row
    return FeedbackRecord(
        run_id=_validate_run_id(run_id),
        value=_validate_value(value),
        created_at=_coerce_datetime(created_at),
        updated_at=_coerce_datetime(updated_at),
    )


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise FeedbackPersistenceError("feedback storage returned an invalid timestamp")


def set_feedback(
    run_id: str,
    value: str,
    *,
    store: FeedbackStore,
) -> FeedbackRecord:
    """Persist one thumb through an explicitly injected store."""
    return store.set_feedback(run_id, value)


# Module-level cache so ``default_feedback_store()`` returns the same
# instance for the life of the process. Mirrors
# ``fin.host.thread_store._DEFAULT_STORE_CACHE``: the Postgres path owns
# a connection pool, so constructing one per request would exhaust the
# database's connections.
_DEFAULT_FEEDBACK_STORE_CACHE: dict[str, FeedbackStore] = {}


def default_feedback_store(env: Any = None) -> FeedbackStore:
    """Pick the feedback store implementation from the environment.

    Mirrors ``default_thread_store``'s degradation semantics exactly:
    ``FIN_CHECKPOINT_URL`` set -> Postgres store over a
    process-lifetime pool; unset -> in-memory store. ADR 0035/0036
    make Postgres the SSOT when the checkpoint URL is configured, so
    thumbs ride the same storage the runs they point at do — there is
    no third "file" option to invent. Tests inject via FastAPI's
    ``dependency_overrides`` so neither branch matters there.
    """
    import os

    source = env if env is not None else os.environ
    url = (source.get("FIN_CHECKPOINT_URL") or "").strip()
    cache_key = url or "<memory>"
    cached = _DEFAULT_FEEDBACK_STORE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if not url:
        cached = InMemoryFeedbackStore()
    else:
        # Imported lazily so the in-memory path (and every import of
        # this module) never pulls psycopg in.
        from fin.host.db_pool import create_postgres_pool

        pool = create_postgres_pool(url)
        # The pool ships closed by design (db_pool); the feedback
        # store is a process-lifetime singleton, so it is the owner
        # of the pool's open lifecycle. ``initialize=True`` then
        # creates ``fin_run_feedback`` on this same connection pool.
        pool.open()
        cached = PostgresFeedbackStore(pool)
    _DEFAULT_FEEDBACK_STORE_CACHE[cache_key] = cached
    return cached


__all__ = [
    "FeedbackError",
    "FeedbackPersistenceError",
    "FeedbackRecord",
    "FeedbackStore",
    "FeedbackValue",
    "InvalidFeedbackError",
    "InMemoryFeedbackStore",
    "MAX_RUN_ID_LENGTH",
    "PostgresFeedbackStore",
    "default_feedback_store",
    "set_feedback",
]
