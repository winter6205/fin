"""Appending one run id twice is a no-op on both transcript stores.

A logical turn can reach its finalizer twice — a stop that races the
natural end, a resume that replays a settled turn, a crash between two
finalizer attempts. The transcript row is keyed by ``fin_run.id``, so
the second attempt must neither raise nor duplicate history, on either
implementation.
"""

from __future__ import annotations

import os

import pytest

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


def _run(run_id: str, thread_id: str):
    from fin.host.thread_store import RunRecord

    return RunRecord(
        id=run_id,
        thread_id=thread_id,
        question="what was revenue?",
        status="done",
        answer="42",
    )


def test_in_memory_append_of_a_known_run_id_is_a_no_op() -> None:
    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")
    store.append_run(_run("r-1", "t-1"))

    store.append_run(_run("r-1", "t-1"))

    runs = store.list_runs("t-1")
    assert [row.id for row in runs] == ["r-1"]


def test_in_memory_append_keeps_the_first_row_when_the_repeat_differs() -> None:
    """The first row is the turn's answer; a replay does not overwrite it."""
    from dataclasses import replace

    from fin.host.thread_store import InMemoryThreadStore

    store = InMemoryThreadStore()
    store.create_thread(id="t-1", title="x")
    store.append_run(_run("r-1", "t-1"))

    store.append_run(replace(_run("r-1", "t-1"), answer="rewritten"))

    assert [row.answer for row in store.list_runs("t-1")] == ["42"]


@pytest.fixture
def postgres_store_url() -> str:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the durable transcript store is "
            "untested in this environment"
        )
    return url


def test_postgres_append_of_a_known_run_id_is_a_no_op(postgres_store_url: str) -> None:
    import psycopg

    from fin.host.thread_store import PostgresThreadStore

    store = PostgresThreadStore(postgres_store_url)
    store.create_thread(id="pg-idem", title="x")
    store.append_run(_run("r-pg-1", "pg-idem"))

    # A duplicate finalizer must not raise UniqueViolation.
    store.append_run(_run("r-pg-1", "pg-idem"))

    assert [row.id for row in store.list_runs("pg-idem")] == ["r-pg-1"]
    with psycopg.connect(postgres_store_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fin_run WHERE id = %s", ("r-pg-1",)
        ).fetchone()[0] == 1
        conn.execute("DELETE FROM fin_thread WHERE id = %s", ("pg-idem",))
        conn.commit()
