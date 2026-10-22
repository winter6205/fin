"""Durable-store behaviour the in-process twin cannot demonstrate.

The shared contract in ``test_turn_receipt_store.py`` runs against both
implementations. What is left is what only a real database answers:
that the row survives the process, that a second writer racing the same
revision really loses, that a duplicate finalizer leaves exactly one
transcript row, and that a receipt never outlives its thread.

Skipped when ``FIN_CHECKPOINT_URL`` is unset (the repo's convention).
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from fin.host.thread_store import PostgresThreadStore, RunRecord
from fin.host.turn_receipt_store import (
    PostgresReceiptStore,
    ReceiptRevisionConflict,
    TurnReceipt,
)

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

THREAD_ID = "receipt-durable-thread"
TURN_ID = "receipt-pg-turn-1"
UNKNOWN_TURN_ID = "receipt-pg-turn-x"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def url() -> Iterator[str]:
    dsn = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the durable receipt store is "
            "untested in this environment"
        )
    PostgresReceiptStore(dsn)
    with _empty(dsn):
        yield dsn


class _empty:
    """Start from an empty thread table, and leave it that way."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def __enter__(self) -> None:
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM fin_turn_receipt")
            conn.execute("DELETE FROM fin_thread WHERE id = %s", (THREAD_ID,))
            conn.execute(
                "INSERT INTO fin_thread (id, title) VALUES (%s, %s)",
                (THREAD_ID, "durable"),
            )

    def __exit__(self, *exc: object) -> None:
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM fin_turn_receipt")
            conn.execute("DELETE FROM fin_thread WHERE id = %s", (THREAD_ID,))


def _run(turn_id: str = TURN_ID) -> RunRecord:
    return RunRecord(
        id=turn_id,
        thread_id=THREAD_ID,
        question="what was revenue?",
        status="done",
        answer="42",
    )


def _terminal(receipt: TurnReceipt) -> TurnReceipt:
    return replace(
        receipt,
        execution_state="terminal",
        technical_result="done",
        semantic_result="answered",
        terminal_at=START + timedelta(seconds=2),
        work_elapsed_ms=700,
        accounting_complete=True,
    )


def _count(url: str, sql: str, *args: object) -> int:
    import psycopg

    with psycopg.connect(url) as conn:
        return int(conn.execute(sql, args).fetchone()[0])


# ----- durability -----


def test_a_receipt_outlives_the_store_instance_that_wrote_it(url: str) -> None:
    first = PostgresReceiptStore(url)
    started = first.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=3,
    )

    reopened = PostgresReceiptStore(url)
    stored = reopened.read_receipt(THREAD_ID, TURN_ID)

    assert stored == started


def test_unobserved_answers_read_back_as_unknown(url: str) -> None:
    store = PostgresReceiptStore(url)
    started = store.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=0,
    )
    store.write_receipt(
        replace(
            started,
            work_elapsed_ms=4100,
            stage_metrics={
                **started.stage_metrics,
                "model": {"calls": 1, "elapsed_ms": 4100, "complete": True},
                "retrieve": {"calls": 0, "elapsed_ms": 0, "complete": False},
            },
        ),
        expected_revision=started.revision,
    )

    stored = PostgresReceiptStore(url).read_receipt(THREAD_ID, TURN_ID)

    assert stored.semantic_result is None
    assert stored.token_usage is None
    assert stored.citation_verified is None
    assert stored.accounting_complete is False
    # An interrupted stage keeps the observed lower bound and says so.
    assert stored.stage_metrics["retrieve"] == {
        "calls": 0,
        "elapsed_ms": 0,
        "complete": False,
    }


# ----- compare-and-swap across two writers -----


def test_the_second_writer_of_one_revision_loses(url: str) -> None:
    first_store = PostgresReceiptStore(url)
    second_store = PostgresReceiptStore(url)
    started = first_store.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=0,
    )
    # Both writers read the same revision before either writes.
    first_draft = replace(started, semantic_result="answered")
    second_draft = replace(started, semantic_result="evidence_refuse")

    first_store.write_receipt(first_draft, expected_revision=started.revision)

    with pytest.raises(ReceiptRevisionConflict) as conflict:
        second_store.write_receipt(second_draft, expected_revision=started.revision)

    assert conflict.value.actual == started.revision + 1
    stored = second_store.read_receipt(THREAD_ID, TURN_ID)
    assert stored.semantic_result == "answered"
    assert stored.revision == started.revision + 1


# ----- the terminal commit, counted in the database -----


def test_a_duplicate_terminal_write_leaves_one_transcript_row(url: str) -> None:
    store = PostgresReceiptStore(url)
    threads = PostgresThreadStore(url)
    started = store.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=0,
    )
    terminal = _terminal(started)

    store.commit_terminal(terminal, expected_revision=started.revision, run=_run())
    # The second finalizer carries the revision it originally read, not
    # the one the first one produced.
    store.commit_terminal(terminal, expected_revision=started.revision, run=_run())

    assert _count(url, "SELECT count(*) FROM fin_run WHERE id = %s", TURN_ID) == 1
    stored = store.read_receipt(THREAD_ID, TURN_ID)
    assert stored.revision == started.revision + 1
    assert stored.execution_state == "terminal"
    assert [row.id for row in threads.list_runs(THREAD_ID)] == [TURN_ID]


def test_a_terminal_commit_is_invisible_until_it_commits(url: str) -> None:
    """One transaction, so a reader never sees the receipt without the
    transcript row the same turn owes."""
    import psycopg

    store = PostgresReceiptStore(url)
    started = store.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=0,
    )
    store.commit_terminal(
        _terminal(started), expected_revision=started.revision, run=_run()
    )

    with psycopg.connect(url) as conn:
        receipts = conn.execute(
            "SELECT count(*) FROM fin_turn_receipt WHERE turn_id = %s", (TURN_ID,)
        ).fetchone()[0]
        runs = conn.execute(
            "SELECT count(*) FROM fin_run WHERE id = %s", (TURN_ID,)
        ).fetchone()[0]

    assert (receipts, runs) == (1, 1)


def test_a_receipt_for_a_thread_that_never_existed_is_refused(url: str) -> None:
    import psycopg

    from fin.host.thread_store import ThreadNotFoundError

    store = PostgresReceiptStore(url)
    with pytest.raises(ThreadNotFoundError):
        store.begin_turn(
            thread_id="no-such-thread", turn_id=UNKNOWN_TURN_ID, started_at=START,
            accepted_input_version=0,
        )
    assert (
        _count(
            url,
            "SELECT count(*) FROM fin_turn_receipt WHERE turn_id = %s",
            UNKNOWN_TURN_ID,
        )
        == 0
    )
    del psycopg


# ----- the same turn under two names must stay one turn -----


def test_a_resumed_turn_reuses_the_receipt_it_already_wrote(url: str) -> None:
    store = PostgresReceiptStore(url)
    started = store.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=2,
    )
    paused = store.write_receipt(
        replace(started, execution_state="paused"), expected_revision=started.revision
    )

    # Transport resume carries no new input, so neither the count nor
    # the projected version may move.
    resumed = store.begin_turn(
        thread_id=THREAD_ID, turn_id=TURN_ID, started_at=START,
        accepted_input_version=7,
    )

    assert resumed == paused
    assert resumed.accepted_input_count == 1
    assert resumed.accepted_input_version == 2
    assert _count(
        url,
        "SELECT count(*) FROM fin_turn_receipt WHERE turn_id = %s",
        TURN_ID,
    ) == 1
