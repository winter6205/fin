"""T6a memory-store contracts, shared by both implementations.

``specs/memory-dream.md`` SC4-SC11: with memory ON and a Postgres URL
configured the working buffer lives in ``fin_memory``, and the dream cursor
lives in ``fin_memory_dream_cursor``. The in-process store and the Postgres
store implement one contract and are exercised by one suite, so the two
cannot drift silently (SC10).

Boundary classes (inherits the parent spec's five): empty (empty store,
never-dreamed cursor); negative (unknown ids, malformed metadata keys);
overflow (bounded by the buffer layer's ``MEMORY_LIMIT_CAP``, not here);
concurrent (``bump_hit_counts`` accumulates exactly under contention);
exception (pool closed / acquire timeout reuses T1's typed errors and is
covered in ``test_memory_buffer.py``).

The Postgres half is opt-in: without ``FIN_CHECKPOINT_URL`` those
parameterisations skip, keeping the default ``pytest -q`` hermetic (SC2).
"""

from __future__ import annotations

import os
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fin.host.memory_store import (
    DreamCursor,
    InProcessDreamCursorStore,
    InProcessMemoryStore,
    MemoryRow,
    MEMORY_SCHEMA_SQL,
    MEMORY_TABLE,
    CURSOR_TABLE,
)

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


def _configured_postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the Postgres memory store is "
            "untested in this environment"
        )
    return url


@pytest.fixture
def postgres_pool():
    """Yield an open pool, skipping when no Postgres URL is configured."""
    from fin.host.db_pool import PostgresPool

    pool = PostgresPool(_configured_postgres_url())
    pool.open(wait=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def postgres_store(postgres_pool):
    from fin.host.memory_store import PostgresMemoryStore

    store = PostgresMemoryStore(postgres_pool)
    try:
        yield store
    finally:
        _truncate(postgres_pool, MEMORY_TABLE)


@pytest.fixture
def postgres_cursors(postgres_pool):
    from fin.host.memory_store import PostgresDreamCursorStore

    store = PostgresDreamCursorStore(postgres_pool)
    try:
        yield store
    finally:
        _truncate(postgres_pool, CURSOR_TABLE)


def _truncate(pool, table: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table}")
        conn.commit()


@pytest.fixture(params=["in-process", "postgres"])
def store(request: pytest.FixtureRequest):
    """Parameterise the row-store contract over both implementations."""
    if request.param == "in-process":
        return InProcessMemoryStore()
    return request.getfixturevalue("postgres_store")


@pytest.fixture(params=["in-process", "postgres"])
def cursors(request: pytest.FixtureRequest):
    """Parameterise the cursor-store contract over both implementations."""
    if request.param == "in-process":
        return InProcessDreamCursorStore()
    return request.getfixturevalue("postgres_cursors")


def _row(
    row_id: str = "row-1",
    text: str = "quarterly revenue comparisons",
    *,
    expires_at: datetime | None = None,
    **metadata: object,
) -> MemoryRow:
    return MemoryRow(
        id=row_id,
        text=text,
        metadata=dict(metadata),
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        expires_at=expires_at,
    )


# ----- shared row-store contract -----


def test_empty_store_returns_no_rows(store) -> None:
    assert store.rows() == []
    assert store.rows(include_disabled=False) == []


def test_add_then_rows_returns_the_row_with_zero_hits(store) -> None:
    store.add(_row(thread_id="thread-1"))

    rows = store.rows()

    assert len(rows) == 1
    assert rows[0].id == "row-1"
    assert rows[0].text == "quarterly revenue comparisons"
    assert rows[0].metadata == {"thread_id": "thread-1"}
    assert rows[0].hit_count == 0
    assert rows[0].status == "active"
    assert rows[0].disabled_reason is None
    assert rows[0].disabled_at is None


def test_metadata_round_trips_nested_structures(store) -> None:
    store.add(_row(thread_id="thread-1", tags=["a", "b"], nested={"n": 1}))

    assert store.rows()[0].metadata == {
        "thread_id": "thread-1",
        "tags": ["a", "b"],
        "nested": {"n": 1},
    }


def test_rows_exclude_disabled_rows_when_asked(store) -> None:
    store.add(_row("row-1"))
    store.add(_row("row-2"))
    store.disable("row-1", reason="expired", at=datetime(2026, 8, 21, tzinfo=timezone.utc))

    assert [row.id for row in store.rows(include_disabled=False)] == ["row-2"]
    assert {row.id for row in store.rows()} == {"row-1", "row-2"}


def test_disable_records_reason_and_timestamp(store) -> None:
    store.add(_row())
    at = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)

    store.disable("row-1", reason="transported", at=at)

    row = store.rows()[0]
    assert row.status == "disabled"
    assert row.disabled_reason == "transported"
    assert row.disabled_at == at


def test_bump_hit_counts_is_cumulative(store) -> None:
    store.add(_row("row-1"))
    store.add(_row("row-2"))

    store.bump_hit_counts(["row-1"])
    store.bump_hit_counts(["row-1", "row-2"])

    hits = {row.id: row.hit_count for row in store.rows()}
    assert hits == {"row-1": 2, "row-2": 1}


def test_concurrent_bump_hit_counts_accumulate_exactly(store) -> None:
    store.add(_row())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _: store.bump_hit_counts(["row-1"]),
                range(64),
            )
        )

    assert store.rows()[0].hit_count == 64


def test_bump_hit_counts_ignores_unknown_ids(store) -> None:
    store.add(_row())

    store.bump_hit_counts(["row-1", "does-not-exist"])

    assert store.rows()[0].hit_count == 1


def test_rows_returns_copies_so_callers_cannot_mutate_the_store(store) -> None:
    store.add(_row(thread_id="thread-1"))

    rows = store.rows()
    rows[0].metadata["thread_id"] = "tampered"
    rows[0].hit_count = 99

    assert store.rows()[0].metadata == {"thread_id": "thread-1"}
    assert store.rows()[0].hit_count == 0


def test_thread_id_is_read_from_row_metadata(store) -> None:
    store.add(_row("with-thread", thread_id="thread-7"))
    store.add(_row("without-thread"))

    thread_ids = {row.id: row.thread_id for row in store.rows()}

    assert thread_ids == {"with-thread": "thread-7", "without-thread": None}


# ----- shared cursor-store contract -----


def test_cursor_load_returns_the_never_dreamed_default(cursors) -> None:
    assert cursors.load() == DreamCursor()


def test_cursor_advance_then_load_round_trips(cursors) -> None:
    at = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)

    cursors.advance(
        at=at,
        seen_thread_ids=frozenset({"thread-1", "thread-2"}),
        last_status="buffered",
    )

    cursor = cursors.load()
    assert cursor.last_dream_at == at
    assert cursor.seen_thread_ids == frozenset({"thread-1", "thread-2"})
    assert cursor.last_status == "buffered"


def test_cursor_advance_overwrites_the_previous_window(cursors) -> None:
    cursors.advance(
        at=datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc),
        seen_thread_ids=frozenset({"thread-1"}),
        last_status="ok",
    )

    cursors.advance(
        at=datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc),
        seen_thread_ids=frozenset({"thread-2", "thread-3"}),
        last_status="buffered",
    )

    cursor = cursors.load()
    assert cursor.seen_thread_ids == frozenset({"thread-2", "thread-3"})
    assert cursor.last_status == "buffered"


# ----- Postgres-only contracts (opt-in) -----


def test_postgres_rows_survive_a_new_store_instance(postgres_pool) -> None:
    from fin.host.memory_store import PostgresMemoryStore

    PostgresMemoryStore(postgres_pool).add(_row(thread_id="thread-1"))

    try:
        reopened = PostgresMemoryStore(postgres_pool)
        assert [row.id for row in reopened.rows()] == ["row-1"]
        assert reopened.rows()[0].metadata == {"thread_id": "thread-1"}
    finally:
        _truncate(postgres_pool, MEMORY_TABLE)


def test_postgres_setup_is_idempotent(postgres_pool) -> None:
    from fin.host.memory_store import PostgresMemoryStore

    store = PostgresMemoryStore(postgres_pool)
    try:
        store.setup()
        store.setup()
        store.add(_row())
        store.setup()

        assert [row.id for row in store.rows()] == ["row-1"]
    finally:
        _truncate(postgres_pool, MEMORY_TABLE)


def test_postgres_cursor_survives_a_new_store_instance(postgres_pool) -> None:
    from fin.host.memory_store import PostgresDreamCursorStore

    at = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
    PostgresDreamCursorStore(postgres_pool).advance(
        at=at, seen_thread_ids=frozenset({"thread-9"}), last_status="ok"
    )

    try:
        assert PostgresDreamCursorStore(postgres_pool).load() == DreamCursor(
            last_dream_at=at,
            seen_thread_ids=frozenset({"thread-9"}),
            last_status="ok",
        )
    finally:
        _truncate(postgres_pool, CURSOR_TABLE)


# ----- schema contracts (no Postgres required) -----


def test_schema_ddl_touches_only_the_two_memory_tables() -> None:
    tables = {MEMORY_TABLE, CURSOR_TABLE}
    mentioned = {
        word.strip("(,;").strip('"')
        for word in MEMORY_SCHEMA_SQL.replace("\n", " ").split()
        if word.strip("(,;").strip('"').startswith("fin_")
    }

    assert mentioned == tables
    assert "fin_thread" not in mentioned
    assert "fin_run" not in mentioned


def test_pyproject_declares_psycopg_pool_explicitly() -> None:
    """SC3: ``db_pool`` imports psycopg_pool, so it is a direct dependency.

    It only reached the environment as a transitive dependency of
    ``langgraph-checkpoint-postgres``. An undeclared direct import is an
    implicit dependency that breaks the moment that parent drops it.
    """
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    declared = {str(dep).split("[")[0].split(">=")[0].strip() for dep in dependencies}
    assert "psycopg-pool" in declared


def test_expiry_and_hit_count_survive_a_round_trip(store) -> None:
    expires_at = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=1)
    store.add(_row(expires_at=expires_at))

    row = store.rows()[0]

    assert row.expires_at == expires_at
    assert row.hit_count == 0
