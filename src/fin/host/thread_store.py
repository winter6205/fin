"""Thread / run metadata storage for the workspace surface (T1, #28).

The workspace HTTP routes need a place to keep the product-level
metadata that does NOT belong to the LangGraph checkpointer:

* thread rows (``id``, ``title``, ``created_at``) — the sidebar and
  the turn table's identity;
* run rows (one per send: question, status, citations, ...) — the
  GET ``/api/threads/{id}`` turn table without dumping checkpoint
  internals.

Two implementations live behind one duck-typed protocol
(``ThreadStore``):

* :class:`InMemoryThreadStore` — plain dicts + a lock. Used by tests
  (no Postgres needed) and by dev boots with no
  ``FIN_CHECKPOINT_URL`` set.
* :class:`PostgresThreadStore` — ``psycopg`` directly against the
  SAME Postgres the durable graph uses (ADR 0015). Schema setup is
  idempotent; tables are host-owned (``fin_thread`` / ``fin_run``),
  NOT LangGraph checkpointer tables.

Why psycopg directly instead of through LangGraph? The checkpointer
owns *graph state*; this store owns *product presentation state*
(titles, turn rows). Mixing them into the checkpoint tuples would
couple two lifecycles (delete-thread must remove titles even when
the checkpoint write failed, and vice versa). A separate side-table
keeps each lifecycle explicit.

The default selector :func:`default_thread_store` mirrors the
existing ``compile_for_runtime`` pattern: read
``FIN_CHECKPOINT_URL`` from the environment and pick accordingly.
"""

from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

import psycopg


# ----- typed errors -----


class ThreadStoreError(Exception):
    """Base class for thread-store typed errors."""


class ThreadNotFoundError(ThreadStoreError):
    """A route addressed a thread_id the store does not know.

    The HTTP layer translates this to 404. The store raises it on
    operations that REQUIRE an existing row (append_run, list_runs,
    delete_thread) so misconfigured routes fail loudly.
    """


# ----- records -----


@dataclass(frozen=True)
class ThreadRecord:
    """One research thread's metadata row."""

    id: str
    title: str
    created_at: datetime


@dataclass(frozen=True)
class RunRecord:
    """One run (= one user send) on a research thread.

    This is the assistant-turn row for GET ``/api/threads/{id}``.
    T1 keeps ``thinking_text`` empty and ``tool_summaries`` empty;
    T3 populates them. ``citations`` is a list of plain dicts with
    keys ``document_id`` / ``version`` / ``page`` / ``quote`` (the
    same shape the citation gate emits).

    ``status`` is a closed enum — see :data:`RunStatus` — so
    downstream code (``AskResponse``, ``TurnRow``, the gate) can
    pattern-match without re-validating.

    ``assistant_message`` is the ordinary-person copy the SPA
    renders when ``status`` is ``refuse`` or ``step_cap`` (T2). It
    is the same string the workspace SSE ``status`` event carries
    in ``assistant_message``; the turn-row mirror keeps GET
    history consistent with the live surface so a reload does not
    lose the copy. Empty on ``done`` (the run produced a real
    ``answer``).
    """

    id: str
    thread_id: str
    question: str
    status: "RunStatus"
    status_reason: str = ""
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str = ""
    tool_summaries: list[str] = field(default_factory=list)
    assistant_message: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ----- enum + validation -----


RunStatus = Literal["done", "refuse", "step_cap"]
"""Closed status enum for one run. Mirrors the run-status enum
emitted by the citation gate (``done`` / ``refuse`` / ``step_cap``)
plus the HTTP layer's ``AskResponse`` / ``TurnRow`` literal types."""


def _validate_status(value: object) -> "RunStatus":
    """Normalize ``value`` to a closed ``RunStatus``.

    Why this seam: ``RunRecord.status`` is the contract between
    the graph layer (gate result), the persistence layer
    (ThreadStore.append_run), and the HTTP layer (AskResponse /
    TurnRow). A typo at any call site (e.g. ``"refuss"``) would
    otherwise pass through unchecked and only fail at the
    Pydantic serializer on read. Failing at the seam keeps the
    invariant local to one helper.

    The default branch collapses anything outside the closed
    set to ``"refuse"`` — the same defensive default the
    ``/ask`` handler uses (see ``fin.host.http``). The caller
    still needs to deal with the answer / citations, but the
    status enum is now consistent.
    """
    if value in ("done", "refuse", "step_cap"):
        return value  # type: ignore[return-value]
    raise ThreadStoreError(
        f"RunRecord.status must be one of 'done'|'refuse'|'step_cap', got {value!r}"
    )


# ----- protocol -----


class ThreadStore(Protocol):
    """Duck-typed contract both implementations satisfy."""

    def create_thread(self, *, id: str | None = None, title: str) -> ThreadRecord:
        """Atomically allocate a new thread row and return its
        ``ThreadRecord``. ``id`` is caller-supplied UUID; when
        ``None`` the store mints one (the v1 /api/threads route
        relies on this so thread ids are stable across the
        HTTP layer and the persistence layer). ``title`` is
        pre-derived from the first user message by the HTTP
        layer (first line, 50-char cap, ellipsis on overflow).
        Idempotent on duplicate ``id`` — raises
        ``ThreadStoreError`` rather than silently overwriting."""
        ...

    def list_threads(self) -> list[ThreadRecord]:
        """Return all thread metadata rows newest-first
        (``created_at`` descending). The HTTP layer serializes
        this for the sidebar; ordering matters so the freshest
        thread surfaces at the top. Returns fresh copies so a
        caller mutating the list cannot corrupt the store."""
        ...

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        """Lookup miss returns ``None`` (NOT an exception): the
        HTTP layer maps ``None`` to a 404 with a stable error
        code. ``ThreadNotFoundError`` is reserved for operations
        that REQUIRE an existing row (``append_run`` /
        ``list_runs``) so misconfigured routes fail loudly.
        Lock covers the dict read so a concurrent
        ``delete_thread`` does not produce a torn view."""
        ...

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        """Set ``title`` on the thread row; return the refreshed
        ``ThreadRecord``. Concrete bodies live in
        :class:`InMemoryThreadStore` and :class:`PostgresThreadStore`.
        """
        ...

    def delete_thread(self, thread_id: str) -> bool:
        """Drop the metadata row AND every associated run row.
        In-memory: a single ``del self._threads[thread_id]``
        (runs live on the thread cell). Postgres: the FK has
        ``ON DELETE CASCADE`` so the run rows disappear with
        the thread. Returns ``True`` on success, ``False`` if
        the thread id is unknown (the HTTP layer maps that to
        404 — same shape as a lookup miss)."""
        ...

    def append_run(self, record: RunRecord) -> None:
        """Append a finished run to its thread's run list.

        Why the per-thread lock covers BOTH the dict lookup
        AND the list append: a concurrent ``delete_thread`` on
        the same id must not produce a partial run record
        (``ThreadRecord`` gone, ``RunRecord`` orphaned). The
        Postgres path uses an FK so the cascade is enforced by
        the database; the in-memory path enforces the same
        invariant by ordering operations under one lock.

        ``record.status`` is validated at this seam (closed
        enum) so a typo at the call site fails here, not at
        the next reader.
        """
        ...

    def list_runs(self, thread_id: str) -> list[RunRecord]:
        """Return the runs for ``thread_id`` in ``created_at``
        ascending order (oldest first — the same order the
        chat UI renders the turn table).

        Returns DEEPCOPIES of the persisted records. The
        docstring on ``RunRecord`` warns callers not to mutate
        the returned objects; ``list_runs`` enforces that by
        handing back independent copies so a stray
        ``run.citations.append(...)`` cannot reach into store
        state.
        """
        ...

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        """Set ``title`` on the thread row; return the refreshed
        ``ThreadRecord``.

        Raises ``ThreadNotFoundError`` when ``thread_id`` is
        unknown — the workspace HTTP layer translates that to 404
        the same way ``append_run`` does (lookups via
        ``get_thread`` map miss -> ``None`` -> 404; writes via
        this method raise because a write to a missing row is
        a misconfigured caller, not a user-visible state).

        Why the explicit method (vs. ``get_thread`` + ``create_thread``):
        the workspace send path calls this only on the "first
        real send updates the default-empty-thread title" branch
        (T2 acceptance); a generic rename surface is out of scope
        (spec forbids thread rename chrome).
        """
        ...


# ----- in-memory implementation -----


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryThreadStore:
    """Dict-backed store guarded by a single ``threading.Lock``.

    The lock covers BOTH the dict mutation and any read that walks
    the dict (list/get), so a concurrent delete cannot produce a
    torn view to a reader. Runs live on the thread record itself so
    delete cascades naturally.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, _MutableThread] = {}

    # -- threads --

    def create_thread(self, *, id: str | None = None, title: str) -> ThreadRecord:
        # WHY the lock spans (id-mint, duplicate-check, dict-write):
        # two callers racing with the SAME id would otherwise both
        # pass the `not in` check and one would silently overwrite
        # the other. Locking the read + write together makes the
        # duplicate-id path loud (ThreadStoreError) instead of
        # silent corruption.
        with self._lock:
            thread_id = id or str(uuid.uuid4())
            if thread_id in self._threads:
                raise ThreadStoreError(f"thread {thread_id!r} already exists")
            record = _MutableThread(id=thread_id, title=title, created_at=_now())
            self._threads[thread_id] = record
            return ThreadRecord(
                id=record.id, title=record.title, created_at=record.created_at
            )

    def list_threads(self) -> list[ThreadRecord]:
        # WHY copy semantics: the returned list is a fresh
        # list of fresh ``ThreadRecord`` (frozen dataclasses).
        # A caller cannot mutate the store by mutating the
        # return value. Sort happens under the lock so the
        # ordering reflects a consistent snapshot.
        with self._lock:
            rows = sorted(
                self._threads.values(), key=lambda r: r.created_at, reverse=True
            )
            return [
                ThreadRecord(id=r.id, title=r.title, created_at=r.created_at)
                for r in rows
            ]

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        # WHY the lock covers the dict lookup: a concurrent
        # ``delete_thread`` for the same id could otherwise race
        # the .get() and let the caller see a row that is about
        # to be deleted. Holding the lock for the duration of
        # the lookup keeps the miss/hit decision atomic.
        with self._lock:
            record = self._threads.get(thread_id)
            if record is None:
                return None
            return ThreadRecord(
                id=record.id, title=record.title, created_at=record.created_at
            )

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        # WHY the lock spans (lookup, mutate, return): a
        # concurrent ``delete_thread`` for the same id must NOT
        # race the lookup and produce a half-applied title on
        # a doomed cell. Same invariant the rest of the store
        # holds — one lock, atomic read-then-write.
        with self._lock:
            record = self._threads.get(thread_id)
            if record is None:
                raise ThreadNotFoundError(
                    f"cannot update title on unknown thread {thread_id!r}"
                )
            record.title = title
            return ThreadRecord(
                id=record.id, title=record.title, created_at=record.created_at
            )

    def delete_thread(self, thread_id: str) -> bool:
        # WHY a single ``del`` (no run cleanup): runs live on the
        # ``_MutableThread`` cell, so dropping the cell drops
        # everything atomically — no partial state where the
        # metadata is gone but a run is still attached. The
        # Postgres path achieves the same invariant via FK
        # ``ON DELETE CASCADE``.
        with self._lock:
            if thread_id not in self._threads:
                return False
            del self._threads[thread_id]
            return True

    # -- runs --

    def append_run(self, record: RunRecord) -> None:
        # WHY the lock covers BOTH the dict lookup AND the
        # list append: a concurrent ``delete_thread`` on the
        # same id must NOT leave a partial run record (the
        # thread gone, the run orphaned). Holding the lock
        # across the two operations serializes delete vs
        # append. ``_validate_status`` enforces the closed
        # enum at this seam so a typo at the call site fails
        # here, not at the next reader (Standards #9).
        _validate_status(record.status)
        with self._lock:
            thread = self._threads.get(record.thread_id)
            if thread is None:
                raise ThreadNotFoundError(
                    f"cannot append run to unknown thread {record.thread_id!r}"
                )
            thread.runs.append(record)

    def list_runs(self, thread_id: str) -> list[RunRecord]:
        # WHY deepcopy (not just ``list(thread.runs)``):
        # ``RunRecord`` is a frozen dataclass, but its
        # ``citations`` and ``tool_summaries`` fields are
        # mutable lists. A caller could mutate those lists
        # through the returned record and corrupt the store.
        # ``copy.deepcopy`` hands back an independent object
        # so the docstring promise — "caller cannot mutate
        # store state through the copy" — is enforced, not
        # merely stated.
        with self._lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
            return [copy.deepcopy(run) for run in thread.runs]


@dataclass
class _MutableThread:
    """Internal mutable cell so runs can be appended post-create."""

    id: str
    title: str
    created_at: datetime
    runs: list[RunRecord] = field(default_factory=list)


# ----- postgres implementation -----


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_thread (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fin_run (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    question          TEXT NOT NULL,
    status            TEXT NOT NULL,
    status_reason     TEXT NOT NULL DEFAULT '',
    answer            TEXT NOT NULL DEFAULT '',
    citations         JSONB NOT NULL DEFAULT '[]'::jsonb,
    thinking_text     TEXT NOT NULL DEFAULT '',
    tool_summaries    JSONB NOT NULL DEFAULT '[]'::jsonb,
    assistant_message TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- T2 column migration: add ``assistant_message`` to existing
-- ``fin_run`` tables without breaking fresh boots. ``ADD COLUMN
-- IF NOT EXISTS`` is supported since Postgres 9.6; the column is
-- NULL-safe via the table default so old rows read back as the
-- empty string.
ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS assistant_message TEXT NOT NULL DEFAULT '';
"""


class PostgresThreadStore:
    """psycopg-backed store against the shared ``FIN_CHECKPOINT_URL``.

    Each public call opens its own short-lived connection — the v1
    workspace is a local operator server with low traffic, and this
    avoids holding a second long-lived pool next to the checkpointer's.
    Schema creation is idempotent and runs once per construction.
    """

    def __init__(self, connection_url: str) -> None:
        self._url = connection_url
        self._setup_lock = threading.Lock()
        with self._setup_lock:
            with self._connect() as conn:
                conn.execute(_SCHEMA_SQL)
                conn.commit()

    def _connect(self) -> Any:
        return psycopg.connect(self._url)

    @staticmethod
    def _row_to_thread(row: Any) -> ThreadRecord:
        return ThreadRecord(id=row[0], title=row[1], created_at=row[2])

    @staticmethod
    def _row_to_run(row: Any) -> RunRecord:
        import json

        # WHY ``list(...)`` on the already-deserialized
        # path: the rows coming back from psycopg can be
        # either JSON strings (driver default) or already
        # ``list`` instances depending on the codec; both
        # branches coerce to a fresh list so the
        # caller cannot reach into the driver's buffer.
        # ``list_runs`` follows up with a deepcopy on the
        # outer record for full isolation.
        return RunRecord(
            id=row[0],
            thread_id=row[1],
            question=row[2],
            status=_validate_status(row[3]),
            status_reason=row[4],
            answer=row[5],
            citations=copy.deepcopy(
                json.loads(row[6]) if isinstance(row[6], str) else list(row[6])
            ),
            thinking_text=row[7],
            tool_summaries=copy.deepcopy(
                json.loads(row[8]) if isinstance(row[8], str) else list(row[8])
            ),
            # ``assistant_message`` defaults to "" on the Python
            # side if the row predates the T2 migration; the
            # SQL column also defaults to '' for fresh installs.
            # We coerce ``None`` to ``""`` defensively in case
            # an older row predates the ADD COLUMN.
            assistant_message=row[9] or "",
            created_at=row[10],
        )

    # -- threads --

    def create_thread(self, *, id: str | None = None, title: str) -> ThreadRecord:
        # WHY ``ON CONFLICT DO NOTHING`` would mask the
        # duplicate-id case in the in-memory store. The v1
        # caller never reuses an id (id is UUID4), so the
        # simpler INSERT is sufficient; PK violation here is
        # a programmer error and surfaces as an exception.
        with self._connect() as conn:
            thread_id = id or str(uuid.uuid4())
            row = conn.execute(
                "INSERT INTO fin_thread (id, title) VALUES (%s, %s) "
                "RETURNING id, title, created_at",
                (thread_id, title),
            ).fetchone()
            conn.commit()
            assert row is not None
            return self._row_to_thread(row)

    def list_threads(self) -> list[ThreadRecord]:
        # WHY ``ORDER BY created_at DESC``: matches the
        # in-memory implementation's ordering so the sidebar
        # is rendered the same way across deployments.
        # Fresh-copy semantics: each row is mapped to a new
        # ``ThreadRecord`` dataclass, so a caller cannot
        # mutate the row by mutating the return value.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at FROM fin_thread "
                "ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_thread(r) for r in rows]

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        # WHY ``None`` on miss (vs ``ThreadNotFoundError``):
        # ``get_thread`` is the lookup-or-miss seam; the HTTP
        # layer translates miss to 404. Reserve the exception
        # for operations that REQUIRE an existing row
        # (``append_run`` / ``list_runs``).
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, title, created_at FROM fin_thread WHERE id = %s",
                (thread_id,),
            ).fetchone()
            return self._row_to_thread(row) if row else None

    def update_thread_title(self, thread_id: str, *, title: str) -> ThreadRecord:
        # WHY ``UPDATE ... RETURNING``: a single round-trip
        # both mutates the row and yields the refreshed
        # ``ThreadRecord`` so the HTTP layer can serialize the
        # updated title without a follow-up ``get_thread``.
        # Unknown id maps to ``ThreadNotFoundError`` so the
        # caller sees the same exception the in-memory store
        # raises (the HTTP layer translates it to 404).
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE fin_thread SET title = %s WHERE id = %s "
                "RETURNING id, title, created_at",
                (title, thread_id),
            ).fetchone()
            conn.commit()
            if row is None:
                raise ThreadNotFoundError(
                    f"cannot update title on unknown thread {thread_id!r}"
                )
            return self._row_to_thread(row)

    def delete_thread(self, thread_id: str) -> bool:
        # WHY FK cascade does the run cleanup: the schema
        # declares ``fin_run.thread_id`` REFERENCES
        # ``fin_thread(id) ON DELETE CASCADE``, so the row
        # count returned by this DELETE only reflects the
        # thread row, not the cascaded run rows. The HTTP
        # layer relies on the cascade to keep
        # ``GET /api/threads/{id}`` from resurrecting orphan
        # runs after a delete.
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM fin_thread WHERE id = %s", (thread_id,)
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    # -- threads (continued) --

    # -- runs --

    def append_run(self, record: RunRecord) -> None:
        # WHY validate the status at the seam: the in-memory
        # and Postgres implementations share the
        # ``RunStatus`` enum (``Literal["done","refuse","step_cap"]``)
        # via ``_validate_status``. Failing fast here keeps
        # a typo at the call site from silently writing a
        # bogus status row.
        # WHY the FK violation is mapped to ``ThreadNotFoundError``:
        # the in-memory store raises that for an unknown
        # ``thread_id``; surfacing the same exception type
        # across implementations lets the HTTP layer
        # uniformly treat it as "thread gone" without
        # branching on the underlying engine.
        import json

        _validate_status(record.status)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO fin_run (id, thread_id, question, status, "
                    "status_reason, answer, citations, thinking_text, "
                    "tool_summaries, assistant_message, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.id,
                        record.thread_id,
                        record.question,
                        record.status,
                        record.status_reason,
                        record.answer,
                        json.dumps(record.citations),
                        record.thinking_text,
                        json.dumps(record.tool_summaries),
                        record.assistant_message,
                        record.created_at,
                    ),
                )
                conn.commit()
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ThreadNotFoundError(
                f"cannot append run to unknown thread {record.thread_id!r}"
            ) from exc

    def list_runs(self, thread_id: str) -> list[RunRecord]:
        # WHY existence check FIRST: ``fin_run.thread_id`` is
        # a FK so an unknown thread yields zero rows. The
        # explicit ``SELECT 1`` lets us raise
        # ``ThreadNotFoundError`` (matching the in-memory
        # store's contract) instead of returning ``[]``,
        # which the HTTP layer would translate to a 200 with
        # an empty turn table — a different shape than 404.
        # WHY deepcopy on the returned rows: same invariant
        # as the in-memory path — caller cannot mutate
        # store state through the returned records.
        import json

        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM fin_thread WHERE id = %s", (thread_id,)
            ).fetchone()
            if exists is None:
                raise ThreadNotFoundError(f"unknown thread {thread_id!r}")
            rows = conn.execute(
                "SELECT id, thread_id, question, status, status_reason, "
                "answer, citations, thinking_text, tool_summaries, "
                "assistant_message, created_at "
                "FROM fin_run WHERE thread_id = %s ORDER BY created_at ASC",
                (thread_id,),
            ).fetchall()
            return [
                self._row_to_run(r).__class__(
                    **{
                        field.name: (
                            copy.deepcopy(getattr(self._row_to_run(r), field.name))
                            if field.name in ("citations", "tool_summaries")
                            else getattr(self._row_to_run(r), field.name)
                        )
                        for field in self._row_to_run(r).__dataclass_fields__.values()
                    }
                )
                for r in rows
            ]


# ----- env-based default selection -----


# Module-level cache so ``default_thread_store()`` returns the
# SAME store across calls within a process. The in-memory store is
# pure state; without caching, every FastAPI dependency resolution
# would mint a fresh dict and threads created in one request would
# vanish from the next. The Postgres store caches against URL
# identity so two callers asking for the same URL share the
# already-set-up schema state.
_DEFAULT_STORE_CACHE: dict[str, ThreadStore] = {}


def default_thread_store(env: Any = None) -> ThreadStore:
    """Pick the right store implementation based on the environment.

    Matches the existing ``compile_for_runtime`` pattern:
    ``FIN_CHECKPOINT_URL`` set -> Postgres store against that URL;
    unset -> in-memory store. Tests inject via FastAPI's
    ``dependency_overrides`` so neither branch matters there.

    The selection is CACHED: subsequent calls return the same
    instance so thread writes from one request are visible to
    reads from the next. This matches the v1 process model where
    one ``uvicorn fin.host.http:app`` process owns the metadata
    store for its lifetime.
    """
    import os

    source = env if env is not None else os.environ
    url = (source.get("FIN_CHECKPOINT_URL") or "").strip()
    cache_key = url or "<memory>"
    cached = _DEFAULT_STORE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if not url:
        cached = InMemoryThreadStore()
    else:
        cached = PostgresThreadStore(url)
    _DEFAULT_STORE_CACHE[cache_key] = cached
    return cached


# Re-exported so callers can ``isinstance`` without importing psycopg.
__all__ = [
    "InMemoryThreadStore",
    "PostgresThreadStore",
    "RunRecord",
    "RunStatus",
    "ThreadNotFoundError",
    "ThreadRecord",
    "ThreadStore",
    "ThreadStoreError",
    "default_thread_store",
]
