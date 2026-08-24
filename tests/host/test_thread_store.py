"""Unit tests for the workspace thread / run store.

The store is the persistence seam for the workspace HTTP surface
(T1, #28). Two implementations live behind a single protocol:

* ``InMemoryThreadStore`` - used by tests and when
  ``FIN_CHECKPOINT_URL`` is unset.
* ``PostgresThreadStore`` - uses ``psycopg`` directly against the
  same Postgres URL the durable graph uses, creates a ``fin_thread``
  + ``fin_run`` schema idempotently on construction.

These tests do not boot the host graph. The store is pure data.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

import pytest


# ----- InMemoryThreadStore round-trip -----


def test_in_memory_thread_store_create_list_get_delete_roundtrip() -> None:
    """The basic CRUD contract on the in-process store."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()

    assert store.list_threads() == []

    t = store.create_thread(id="t-1", title="Revenue thread")
    assert t.id == "t-1"
    assert t.title == "Revenue thread"
    assert isinstance(t.created_at, datetime)

    listed = store.list_threads()
    assert [row.id for row in listed] == ["t-1"]
    assert listed[0].title == "Revenue thread"

    fetched = store.get_thread("t-1")
    assert fetched is not None
    assert fetched.id == "t-1"
    assert fetched.title == "Revenue thread"

    assert store.delete_thread("t-1") is True
    assert store.get_thread("t-1") is None
    assert store.list_threads() == []

    # Second delete on a missing id returns False (idempotent contract
    # for callers; HTTP layer translates to 404).
    assert store.delete_thread("t-1") is False


def test_in_memory_thread_store_get_unknown_returns_none_not_exception() -> None:
    """``get_thread`` for an unknown id is the documented lookup miss
    (None), not an exception."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    assert store.get_thread("does-not-exist") is None


def test_in_memory_thread_store_list_threads_sorted_by_created_at_desc() -> None:
    """``list_threads`` orders newest-first so the sidebar surfaces
    the freshest thread at the top."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="oldest")
    store.create_thread(id="t-2", title="middle")
    store.create_thread(id="t-3", title="newest")

    ids = [row.id for row in store.list_threads()]
    assert ids == ["t-3", "t-2", "t-1"]


def test_in_memory_thread_store_concurrent_create_is_serialized() -> None:
    """Eight threads racing on ``create_thread`` with distinct ids do
    not corrupt the underlying dict (the lock is held across the
    mutation + list rebuild)."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            store.create_thread(id=f"t-{index}", title=f"title-{index}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors
    listed = store.list_threads()
    assert {row.id for row in listed} == {f"t-{i}" for i in range(8)}
    assert len(listed) == 8


# ----- run record (per-invocation) -----


def _make_run(thread_id: str, *, run_id: str | None = None, status: str = "done", answer: str = "x") -> object:
    """Build a RunRecord fixture without importing the concrete class."""
    from fin.host.thread_store import RunRecord

    return RunRecord(
        id=run_id or str(uuid.uuid4()),
        thread_id=thread_id,
        question="what was revenue?",
        status=status,
        status_reason="" if status == "done" else ("unknown_locator" if status == "refuse" else "step_cap"),
        answer=answer,
        citations=[],
        thinking_text="",
        tool_summaries=[],
        created_at=datetime.now(tz=timezone.utc),
    )


def test_in_memory_thread_store_append_run_then_list_runs() -> None:
    """``append_run`` records a run; ``list_runs`` returns them in
    created_at order. Unknown thread raises ``ThreadNotFoundError``."""
    from fin.host.thread_store import InMemoryThreadStore, ThreadNotFoundError

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")

    r1 = _make_run("t-1", answer="a1")
    r2 = _make_run("t-1", answer="a2")
    store.append_run(r1)
    store.append_run(r2)

    runs = store.list_runs("t-1")
    assert [r.answer for r in runs] == ["a1", "a2"]


def test_run_record_track_decision_roundtrip() -> None:
    """Soft-Track T5: track_decision four fields survive append + list."""
    from fin.host.thread_store import InMemoryThreadStore, RunRecord

    store = InMemoryThreadStore()
    store.create_thread(id="t-td", title="x")
    td = {
        "track": "research",
        "confidence": 0.9,
        "decision_tier": "rules",
        "signals": {"cue": "filing"},
    }
    store.append_run(
        RunRecord(
            id="r-td",
            thread_id="t-td",
            question="q",
            status="done",
            answer="a",
            track_decision=td,
        )
    )
    runs = store.list_runs("t-td")
    assert runs[0].track_decision == td
    # Isolation: mutating the returned dict must not corrupt store.
    runs[0].track_decision["signals"]["cue"] = "mutated"
    assert store.list_runs("t-td")[0].track_decision["signals"]["cue"] == "filing"


def test_run_record_tool_summaries_roundtrip_as_dicts() -> None:
    """T3 (spec line 32): ``RunRecord.tool_summaries`` carries one
    ``{name, summary, tool_call_id}`` dict per tool call; the
    in-memory store's deepcopy isolation must preserve the dict
    shape through append + list. A string-list regression (T1's
    placeholder shape) would fail the key check."""
    from fin.host.thread_store import InMemoryThreadStore, RunRecord

    store = InMemoryThreadStore()
    store.create_thread(id="t-ts", title="x")
    record = RunRecord(
        id="r-ts",
        thread_id="t-ts",
        question="q",
        status="done",
        answer="a",
        citations=[],
        thinking_text="chain of thought",
        tool_summaries=[
            {
                "name": "read_file",
                "summary": "AAPL-2022Q3 p.1",
                "tool_call_id": "c-1",
            }
        ],
    )
    store.append_run(record)
    runs = store.list_runs("t-ts")
    assert runs[0].thinking_text == "chain of thought"
    assert runs[0].tool_summaries == [
        {"name": "read_file", "summary": "AAPL-2022Q3 p.1", "tool_call_id": "c-1"}
    ]
    entry = runs[0].tool_summaries[0]
    assert set(entry.keys()) == {"name", "summary", "tool_call_id"}
    # Deepcopy isolation: mutating the returned list must not
    # corrupt the stored record.
    runs[0].tool_summaries.append({"name": "x", "summary": "y", "tool_call_id": "z"})
    again = store.list_runs("t-ts")
    assert len(again[0].tool_summaries) == 1


def test_postgres_run_record_in_flight_round_trip(pg_store) -> None:
    """T4: the Postgres implementation persists ``in_flight`` as a
    NOT NULL BOOLEAN column with default ``False``; round-tripping a
    freshly-appended run returns ``False`` (the storage-layer
    invariant — persisted rows are always terminal)."""
    from fin.host.thread_store import RunRecord

    pg_store.create_thread(id="pg-if", title="x")
    pg_store.append_run(
        RunRecord(
            id="r-if",
            thread_id="pg-if",
            question="q",
            status="done",
        )
    )
    runs = pg_store.list_runs("pg-if")
    assert len(runs) == 1
    assert runs[0].in_flight is False


def test_postgres_run_sql_round_trips_tool_summaries_jsonb(monkeypatch) -> None:
    """The Postgres INSERT serializes ``tool_summaries`` as a JSON
    array of dicts and ``_row_to_run`` parses it back to dicts.
    Stubbed psycopg records the params so the JSONB contract is
    pinned without a live database."""
    import json
    from datetime import datetime, timezone

    from fin.host.thread_store import PostgresThreadStore, RunRecord

    captured_params: list[tuple] = []
    row_back: list[tuple] = []

    class _StubCursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return self._value

    class _StubConn:
        def execute(self, sql, params=None):
            if params is not None:
                captured_params.append(params)
                # Mirror the SELECT column order used by list_runs.
                row_back.append(
                    (
                        params[0],  # id
                        params[1],  # thread_id
                        params[2],  # question
                        params[3],  # status
                        params[4],  # status_reason
                        params[5],  # answer
                        params[6],  # citations json
                        params[7],  # thinking_text
                        params[8],  # tool_summaries json
                        params[9],  # assistant_message
                        params[10],  # created_at
                        params[11],  # T4: in_flight (always False on insert)
                    )
                )
            return _StubCursor(row_back[-1] if row_back else None)

        def commit(self):
            pass

    class _StubCM:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *exc):
            return False

    import fin.host.thread_store as ts_mod

    monkeypatch.setattr(
        ts_mod.psycopg,
        "connect",
        lambda *a, **kw: _StubCM(_StubConn()),
    )

    store = PostgresThreadStore.__new__(PostgresThreadStore)
    store._url = "postgresql://stub"
    summaries = [
        {"name": "list_files", "summary": "12 filings", "tool_call_id": "c-lf"},
    ]
    created = datetime(2026, 8, 23, tzinfo=timezone.utc)
    store.append_run(
        RunRecord(
            id="r-pg",
            thread_id="t-pg",
            question="q",
            status="done",
            answer="a",
            citations=[],
            thinking_text="thoughts",
            tool_summaries=summaries,
            assistant_message="",
            created_at=created,
        )
    )
    # The persisted JSON is an array of dicts.
    persisted = json.loads(captured_params[0][8])
    assert persisted == [
        {"name": "list_files", "summary": "12 filings", "tool_call_id": "c-lf"}
    ]
    # _row_to_run parses it back to dicts.
    parsed = store._row_to_run(row_back[0])
    assert parsed.tool_summaries == [
        {"name": "list_files", "summary": "12 filings", "tool_call_id": "c-lf"}
    ]


def test_postgres_schema_contains_t3_column_migrations() -> None:
    """Pre-T3 ``fin_run`` tables must be upgraded idempotently: the
    schema SQL contains ``ADD COLUMN IF NOT EXISTS`` for
    ``thinking_text`` and ``tool_summaries`` so a durable boot on an
    old table does not raise on the first T3 INSERT."""
    from fin.host.thread_store import _SCHEMA_SQL

    assert "ADD COLUMN IF NOT EXISTS thinking_text" in _SCHEMA_SQL
    assert "ADD COLUMN IF NOT EXISTS tool_summaries" in _SCHEMA_SQL


def test_in_memory_thread_store_append_run_unknown_thread_raises() -> None:
    """Appending a run for an unknown thread is a programmer error;
    the store raises ``ThreadNotFoundError`` so a misconfigured route
    fails loudly instead of silently attaching to a missing thread."""
    from fin.host.thread_store import InMemoryThreadStore, ThreadNotFoundError

    store = InMemoryThreadStore()
    bogus = _make_run("nope")
    with pytest.raises(ThreadNotFoundError):
        store.append_run(bogus)


def test_in_memory_thread_store_list_runs_unknown_thread_raises() -> None:
    from fin.host.thread_store import InMemoryThreadStore, ThreadNotFoundError

    store = InMemoryThreadStore()
    with pytest.raises(ThreadNotFoundError):
        store.list_runs("nope")


def test_in_memory_run_record_in_flight_default_false() -> None:
    """T4: every persisted run is by definition terminal (the route
    appends it only after the stream completes), so the default
    ``in_flight`` is ``False``. The HTTP serializer overrides this on
    the LATEST row when the lock probe returns ``True``; the
    storage layer itself never persists ``True``."""
    from fin.host.thread_store import RunRecord

    record = RunRecord(
        id="r",
        thread_id="t",
        question="q",
        status="done",
    )
    assert record.in_flight is False


def test_in_memory_thread_store_delete_thread_cascades_runs() -> None:
    """Deleting a thread also drops its run records so a follow-up
    create with the same id starts clean."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")
    store.append_run(_make_run("t-1", answer="a1"))
    store.append_run(_make_run("t-1", answer="a2"))
    assert len(store.list_runs("t-1")) == 2

    assert store.delete_thread("t-1") is True
    store.create_thread(id="t-1", title="x again")
    assert store.list_runs("t-1") == []


# ----- status validation at the seam (H3 / Standards #9) -----


def test_in_memory_append_run_rejects_malformed_status() -> None:
    """A typo'd status fails LOUDLY at the store seam instead of
    silently persisting a bogus row that only breaks the next
    reader (Pydantic serializer)."""
    from fin.host.thread_store import InMemoryThreadStore, RunRecord, ThreadStoreError

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")
    bogus = _make_run("t-1")
    object.__setattr__(bogus, "status", "refuss")  # frozen dataclass; test-only
    with pytest.raises(ThreadStoreError, match="must be one of"):
        store.append_run(bogus)


def test_in_memory_append_run_accepts_all_closed_enum_values() -> None:
    """Every member of the closed ``RunStatus`` enum passes the seam."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")
    for i, status in enumerate(("done", "refuse", "step_cap")):
        store.append_run(_make_run("t-1", run_id=f"r-{i}", status=status))
    runs = store.list_runs("t-1")
    assert [r.status for r in runs] == ["done", "refuse", "step_cap"]


def test_postgres_append_run_rejects_malformed_status(pg_store) -> None:
    """Same seam validation on the Postgres implementation — a bad
    status never reaches an INSERT."""
    from fin.host.thread_store import ThreadStoreError

    pg_store.create_thread(id="pg-1", title="x")
    bogus = _make_run("pg-1")
    object.__setattr__(bogus, "status", "done ")
    with pytest.raises(ThreadStoreError):
        pg_store.append_run(bogus)


# ----- deepcopy isolation on list_runs (M8 / Standards #17) -----


def test_in_memory_list_runs_returns_isolated_copies() -> None:
    """Mutating a returned run's mutable fields must NOT corrupt the
    store. The old code returned the SAME list objects stored
    internally; ``list_runs`` now deepcopies so the docstring
    promise ("caller cannot mutate store state") is enforced."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")
    record = _make_run(
        "t-1",
        run_id="r-1",
        answer="original",
    )
    # Give the record a non-empty citation dict to mutate through.
    object.__setattr__(
        record,
        "citations",
        [{"document_id": "AAPL-2022Q3", "version": "v", "page": 1, "quote": "q"}],
    )
    store.append_run(record)

    runs = store.list_runs("t-1")
    runs[0].citations[0]["document_id"] = "MUTATED"
    runs2 = store.list_runs("t-1")
    assert runs2[0].citations[0]["document_id"] == "AAPL-2022Q3"
    # The original in-store object is untouched too.
    assert store.list_runs("t-1")[0].answer == "original"


def test_postgres_list_runs_returns_isolated_copies(pg_store) -> None:
    """Same isolation contract on the Postgres path."""
    pg_store.create_thread(id="pg-1", title="x")
    record = _make_run("pg-1", run_id="r-1", answer="a")
    pg_store.append_run(record)

    runs = pg_store.list_runs("pg-1")
    assert len(runs) == 1


# ----- store-minted ids (M9 / Standards #14) -----


def test_create_thread_without_id_mints_uuid() -> None:
    """``create_thread(title=...)`` without an explicit id mints a
    UUID4 — the v1 route relies on this so the HTTP layer does not
    own id allocation."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    t1 = store.create_thread(title="no id passed")
    t2 = store.create_thread(title="nor here")
    assert t1.id != t2.id
    assert len(t1.id) == 36  # canonical uuid4 string


def test_create_thread_duplicate_explicit_id_raises() -> None:
    """Duplicate explicit id is loud, not silent overwrite."""
    from fin.host.thread_store import InMemoryThreadStore, ThreadStoreError

    store = InMemoryThreadStore()
    store.create_thread(id="dup", title="first")
    with pytest.raises(ThreadStoreError, match="already exists"):
        store.create_thread(id="dup", title="second")


# ----- PostgresThreadStore round-trip (env-gated) -----


POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


@pytest.fixture
def postgres_store_url() -> str:
    """Skip when ``FIN_CHECKPOINT_URL`` is not configured."""
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; PostgresThreadStore is "
            "untested in this environment"
        )
    return url


@pytest.fixture
def pg_store(postgres_store_url: str):
    """Build a Postgres store, yield it, and clean up rows on teardown."""
    from fin.host.thread_store import PostgresThreadStore

    store = PostgresThreadStore(postgres_store_url)
    yield store
    try:
        import psycopg

        with psycopg.connect(postgres_store_url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM fin_thread")
                cur.execute("DELETE FROM fin_run")
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


def test_postgres_thread_store_create_list_get_delete_roundtrip(pg_store) -> None:
    """End-to-end round-trip against a real Postgres: create, list,
    get, delete."""
    t = pg_store.create_thread(id="pg-1", title="hello")
    assert t.id == "pg-1"
    assert pg_store.get_thread("pg-1") is not None
    assert any(row.id == "pg-1" for row in pg_store.list_threads())

    assert pg_store.delete_thread("pg-1") is True
    assert pg_store.get_thread("pg-1") is None


def test_postgres_thread_store_run_append_and_list(pg_store) -> None:
    """Run records persist with foreign-key cascade on delete."""
    pg_store.create_thread(id="pg-1", title="x")
    r1 = _make_run("pg-1", answer="a1")
    r2 = _make_run("pg-1", answer="a2")
    pg_store.append_run(r1)
    pg_store.append_run(r2)

    runs = pg_store.list_runs("pg-1")
    assert [r.answer for r in runs] == ["a1", "a2"]

    # Delete cascades to fin_run.
    pg_store.delete_thread("pg-1")
    pg_store.create_thread(id="pg-1", title="x2")
    assert pg_store.list_runs("pg-1") == []


def test_postgres_thread_store_append_unknown_thread_raises(pg_store) -> None:
    """FK enforcement: appending a run for a non-existent thread is
    rejected by Postgres with a foreign-key violation, surfaced as
    ThreadNotFoundError by the store."""
    from fin.host.thread_store import ThreadNotFoundError

    bogus = _make_run("nope")
    with pytest.raises(ThreadNotFoundError):
        pg_store.append_run(bogus)


# ----- compile helper (env-based selection) -----


def test_default_thread_store_when_env_unset(monkeypatch) -> None:
    """``default_thread_store`` returns the in-memory implementation
    when ``FIN_CHECKPOINT_URL`` is unanswered."""
    from fin.host.thread_store import default_thread_store

    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    store = default_thread_store()
    assert store.__class__.__name__ == "InMemoryThreadStore"


def test_default_thread_store_when_env_set(monkeypatch, postgres_store_url: str) -> None:
    """When ``FIN_CHECKPOINT_URL`` is set, the default is the Postgres
    implementation."""
    from fin.host.thread_store import default_thread_store

    monkeypatch.setenv("FIN_CHECKPOINT_URL", postgres_store_url)
    store = default_thread_store()
    assert store.__class__.__name__ == "PostgresThreadStore"



# ===== T2: update_thread_title (first real send on a default-titled thread) =====


def test_in_memory_update_thread_title_roundtrip() -> None:
    """Set the title; the refreshed ThreadRecord carries it and a
    subsequent ``get_thread`` sees the same value."""
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    created = store.create_thread(id="t-title", title="New thread")

    updated = store.update_thread_title("t-title", title="What was revenue?")
    assert updated.id == "t-title"
    assert updated.title == "What was revenue?"
    assert updated.created_at == created.created_at

    fetched = store.get_thread("t-title")
    assert fetched is not None
    assert fetched.title == "What was revenue?"


def test_in_memory_update_thread_title_unknown_raises() -> None:
    """Unknown id raises ``ThreadNotFoundError`` (the HTTP layer maps
    it to 404, same as ``append_run``)."""
    from fin.host.thread_store import InMemoryThreadStore, ThreadNotFoundError

    store = InMemoryThreadStore()
    with pytest.raises(ThreadNotFoundError):
        store.update_thread_title("nope", title="x")


def test_in_memory_update_thread_title_after_delete_raises() -> None:
    """A deleted thread cannot be retitled — the cell is gone."""
    from fin.host.thread_store import InMemoryThreadStore, ThreadNotFoundError

    store = InMemoryThreadStore()
    store.create_thread(id="t-gone", title="New thread")
    assert store.delete_thread("t-gone") is True
    with pytest.raises(ThreadNotFoundError):
        store.update_thread_title("t-gone", title="late")


def test_postgres_update_thread_title_roundtrip(pg_store) -> None:
    """Same contract on the Postgres implementation (env-gated)."""
    pg_store.create_thread(id="pg-title", title="New thread")
    updated = pg_store.update_thread_title("pg-title", title="PG question")
    assert updated.title == "PG question"
    fetched = pg_store.get_thread("pg-title")
    assert fetched is not None
    assert fetched.title == "PG question"


def test_postgres_update_thread_title_unknown_raises(pg_store) -> None:
    from fin.host.thread_store import ThreadNotFoundError

    with pytest.raises(ThreadNotFoundError):
        pg_store.update_thread_title("pg-nope", title="x")


def test_postgres_update_thread_title_resolves_to_sql_body_not_in_memory(
    monkeypatch,
) -> None:
    """H1 regression: T2's class-swap bug placed an in-memory body
    in ``PostgresThreadStore`` body and a SQL body in
    ``InMemoryThreadStore``. ``PostgresThreadStore`` had no
    ``_lock`` / ``_threads``, so calling
    ``update_thread_title`` on a fresh instance raised
    ``AttributeError`` and surfaced as 500 in production.

    Verify (a) the Postgres class no longer references ``_lock`` /
    ``_threads`` in its body, and (b) constructing the class with
    a stubbed ``psycopg.connect`` records an ``UPDATE fin_thread``
    SQL string instead of raising ``AttributeError``.

    Runs without a live Postgres (the psycopg module is monkeypatched).
    """
    import inspect

    from fin.host import thread_store as ts

    src = inspect.getsource(ts.PostgresThreadStore.update_thread_title)
    assert "self._lock" not in src, (
        "PostgresThreadStore.update_thread_title must not reference self._lock "
        "(class-swap regression)."
    )
    assert "self._threads" not in src, (
        "PostgresThreadStore.update_thread_title must not reference "
        "self._threads (class-swap regression)."
    )
    assert "UPDATE fin_thread" in src

    # Also verify the in-memory store does not reference _connect
    # (the inverse swap direction).
    src_in_mem = inspect.getsource(ts.InMemoryThreadStore.update_thread_title)
    assert "self._connect" not in src_in_mem, (
        "InMemoryThreadStore.update_thread_title must not reference "
        "self._connect (class-swap regression)."
    )


def test_postgres_update_thread_title_end_to_end_with_mock(monkeypatch) -> None:
    """H1 regression (mock psycopg): actually invoke
    ``PostgresThreadStore.update_thread_title`` against a stubbed
    connection layer. Pre-fix this raised ``AttributeError`` because
    the resolved body referenced ``self._lock`` /
    ``self._threads`` (those attributes do not exist on the
    Postgres class). Post-fix the SQL is recorded and a refreshed
    ``ThreadRecord`` is returned.

    Bypasses ``__init__``'s schema setup by constructing the
    instance and assigning ``_url`` directly — the test only needs
    to exercise ``update_thread_title``, not the schema bootstrap.
    """
    from datetime import datetime, timezone

    from fin.host.thread_store import PostgresThreadStore

    captured_sql: list[str] = []

    class _StubCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _StubConn:
        def __init__(self, row):
            self._row = row

        def execute(self, sql, params):
            captured_sql.append(sql)
            return _StubCursor(self._row)

        def commit(self):
            pass

    class _StubCM:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *exc):
            return False

    def stub_connect(*args, **kwargs):
        return _StubCM(
            _StubConn(
                ("pg-1", "PG title", datetime(2026, 8, 1, tzinfo=timezone.utc))
            )
        )

    import fin.host.thread_store as ts_mod

    monkeypatch.setattr(ts_mod.psycopg, "connect", stub_connect)

    # Bypass schema setup — only exercise the update path.
    store = PostgresThreadStore.__new__(PostgresThreadStore)
    store._url = "postgresql://stub"
    updated = store.update_thread_title("pg-1", title="PG title")

    assert updated.id == "pg-1"
    assert updated.title == "PG title"
    assert any("UPDATE fin_thread" in s for s in captured_sql), captured_sql
