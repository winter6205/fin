"""Thread deletion retires the turn, and nothing brings it back.

A delete is one fact about one thread: the history, the receipts and the
thread itself leave together, and a writer that was already in flight when
the delete was admitted must not be able to put any of it back. Both
stores answer those two questions — does the write still land, and does the
cause get recorded before the rows go — so the cases run against both: the
in-memory half always, the durable half whenever ``FIN_CHECKPOINT_URL``
names a database.

The refusals live here for the same reason. An unknown thread, a draft
carrying a foreign contract and a transcript row that belongs to another
turn are all refused BEFORE the store is touched, and a refusal that left a
row or moved a revision behind it would be indistinguishable from a turn
that really happened.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from fin.host.thread_store import (
    InMemoryThreadStore,
    PostgresThreadStore,
    RunRecord,
    ThreadNotFoundError,
    ThreadStore,
)
from fin.host.turn_receipt_store import (
    InMemoryReceiptStore,
    PostgresReceiptStore,
    ReceiptContractError,
    ReceiptNotFoundError,
    ReceiptStore,
    TurnReceipt,
    _validate_receipt,
)

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

THREAD_ID = "receipt-delete-thread"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "postgres"])
def harness(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[ReceiptStore, ThreadStore]]:
    """Both implementations of the one deletion contract."""
    if request.param == "memory":
        threads = InMemoryThreadStore()
        yield InMemoryReceiptStore(threads), threads
        return
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the durable receipt store is "
            "untested in this environment"
        )
    threads = PostgresThreadStore(url)
    with _cleared(url):
        yield PostgresReceiptStore(url), threads


class _cleared:
    """Empty the host-owned rows so a case starts from nothing stored."""

    def __init__(self, url: str) -> None:
        self._url = url

    def __enter__(self) -> None:
        import psycopg

        PostgresReceiptStore(self._url)
        with psycopg.connect(self._url, autocommit=True) as conn:
            conn.execute("DELETE FROM fin_turn_receipt")
            conn.execute("DELETE FROM fin_thread")

    def __exit__(self, *exc: object) -> None:
        return None


def _seeded(
    store: ReceiptStore, threads: ThreadStore, *turn_ids: str
) -> dict[str, TurnReceipt]:
    """A thread with one receipt per named turn, and a transcript row."""
    threads.create_thread(id=THREAD_ID, title="deletion")
    receipts: dict[str, TurnReceipt] = {}
    for turn_id in turn_ids:
        receipts[turn_id] = store.begin_turn(
            thread_id=THREAD_ID,
            turn_id=turn_id,
            started_at=START,
            accepted_input_version=3,
        )
        threads.append_run(_run(turn_id))
    return receipts


def _run(turn_id: str) -> RunRecord:
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
        accounting_complete=True,
    )


# ----- the retirement itself -----

def test_deleting_a_thread_removes_its_history_and_its_receipts_together(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    _seeded(store, threads, "turn-1", "turn-2")

    store.delete_thread(THREAD_ID)

    assert threads.get_thread(THREAD_ID) is None
    for turn_id in ("turn-1", "turn-2"):
        assert store.read_receipt(THREAD_ID, turn_id) is None
    with pytest.raises(ThreadNotFoundError):
        threads.list_runs(THREAD_ID)


def test_the_deletion_cause_is_recorded_while_the_receipt_still_exists(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """The stamp is the transaction's own record, returned as it was written.

    The row cannot outlive the delete — that is the whole point — so the
    cause would be unprovable if the only place it appeared were a table
    nobody can read afterwards.
    """
    store, threads = harness
    _seeded(store, threads, "turn-1", "turn-2")

    retired = store.delete_thread(THREAD_ID)

    assert [r.turn_id for r in retired] == ["turn-1", "turn-2"]
    for receipt in retired:
        assert receipt.cancellation_reason == "thread_deleted"
        assert receipt.execution_state == "terminal"
        assert receipt.technical_result == "stopped"
        assert receipt.terminal_at is not None


def test_a_paused_turn_keeps_the_verdict_it_had_and_gains_the_cause(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """The thread is going away, not the answer the turn reached."""
    store, threads = harness
    paused = _seeded(store, threads, "turn-1")["turn-1"]
    store.write_receipt(
        replace(paused, execution_state="paused", semantic_result="clarify"),
        expected_revision=paused.revision,
    )

    (retired,) = store.delete_thread(THREAD_ID)

    assert retired.semantic_result == "clarify"
    assert retired.cancellation_reason == "thread_deleted"


def test_deleting_a_thread_this_store_does_not_hold_is_refused(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness

    with pytest.raises(ThreadNotFoundError):
        store.delete_thread(THREAD_ID)

    assert threads.get_thread(THREAD_ID) is None


def test_a_turn_that_had_already_failed_is_retired_as_not_a_failure(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """A deletion is not a failure, so it leaves no failure behind.

    The turn failed on its own terms before the thread went away. The
    deletion does not undo that history — the row keeps its own answer —
    but it does take over the outcome: the turn is retired as stopped by
    the deletion, and a ``stopped`` turn carrying a ``failure_kind`` is a
    combination this module's own validator refuses. Both stores stamp
    through one projection precisely so the two cannot drift into
    agreeing only on an illegal row.
    """
    store, threads = harness
    failed = _seeded(store, threads, "turn-1")["turn-1"]
    store.write_receipt(
        replace(
            failed,
            execution_state="terminal",
            technical_result="failed",
            failure_kind="graph_failed",
            semantic_result="evidence_refuse",
            terminal_at=START + timedelta(seconds=1),
        ),
        expected_revision=failed.revision,
    )

    (retired,) = store.delete_thread(THREAD_ID)

    assert retired.technical_result == "stopped"
    assert retired.cancellation_reason == "thread_deleted"
    assert retired.failure_kind is None
    # The turn's own verdict is the one thing the delete does not take.
    assert retired.semantic_result == "evidence_refuse"
    _validate_receipt(retired)


# ----- the fence a late writer meets -----

def test_a_late_write_cannot_recreate_a_receipt_for_a_deleted_thread(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """Every in-flight writer is refused, and none of them leaves a row."""
    store, threads = harness
    receipt = _seeded(store, threads, "turn-1")["turn-1"]
    draft = _terminal(receipt)
    store.delete_thread(THREAD_ID)

    with pytest.raises(ReceiptNotFoundError):
        store.write_receipt(draft, expected_revision=receipt.revision)
    with pytest.raises(ReceiptNotFoundError):
        store.commit_terminal(
            draft, expected_revision=receipt.revision, run=_run("turn-1")
        )
    with pytest.raises(ReceiptNotFoundError):
        store.record_accepted_input(
            thread_id=THREAD_ID,
            turn_id="turn-1",
            expected_revision=receipt.revision,
            input_version=9,
        )
    with pytest.raises(ThreadNotFoundError):
        store.begin_turn(
            thread_id=THREAD_ID,
            turn_id="turn-2",
            started_at=START,
            accepted_input_version=1,
        )

    assert store.read_receipt(THREAD_ID, "turn-1") is None
    assert store.read_receipt(THREAD_ID, "turn-2") is None
    with pytest.raises(ThreadNotFoundError):
        threads.list_runs(THREAD_ID)


def test_a_reused_thread_id_cannot_reopen_a_deleted_thread_in_process() -> None:
    """The in-process fence outlives the row a re-created thread would make.

    The durable store refuses the same writer through the foreign key,
    because the thread it was admitted against is gone; nothing else can
    refuse it, because a dict has no cascade to lean on. The id is
    remembered so a stale admission cannot come back as a fresh turn.
    """
    threads = InMemoryThreadStore()
    store = InMemoryReceiptStore(threads)
    _seeded(store, threads, "turn-1")
    store.delete_thread(THREAD_ID)
    threads.create_thread(id=THREAD_ID, title="re-created")

    with pytest.raises(ThreadNotFoundError):
        store.begin_turn(
            thread_id=THREAD_ID,
            turn_id="turn-1",
            started_at=START,
            accepted_input_version=1,
        )

    assert store.read_receipt(THREAD_ID, "turn-1") is None


# ----- refusals that write nothing -----

def test_beginning_a_turn_on_an_unknown_thread_writes_nothing(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness

    with pytest.raises(ThreadNotFoundError):
        store.begin_turn(
            thread_id="opaque-thread-id",
            turn_id="turn-1",
            started_at=START,
            accepted_input_version=1,
        )

    assert store.read_receipt("opaque-thread-id", "turn-1") is None
    threads.create_thread(id="opaque-thread-id", title="never reached")
    assert store.read_receipt("opaque-thread-id", "turn-1") is None


@pytest.mark.parametrize(
    "foreign",
    [
        {"schema_version": 2},
        {"execution_state": "aborted"},
        {"technical_result": "cancelled"},
        {"cancellation_reason": "operator_vacation"},
    ],
)
def test_a_draft_carrying_a_foreign_contract_is_refused_without_a_write(
    harness: tuple[ReceiptStore, ThreadStore],
    foreign: dict[str, object],
) -> None:
    store, threads = harness
    receipt = _seeded(store, threads, "turn-1")["turn-1"]

    with pytest.raises(ReceiptContractError):
        store.write_receipt(
            replace(receipt, **foreign), expected_revision=receipt.revision
        )
    with pytest.raises(ReceiptContractError):
        store.commit_terminal(
            replace(_terminal(receipt), **foreign),
            expected_revision=receipt.revision,
            run=_run("turn-1"),
        )

    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert stored is not None
    assert stored == receipt


def test_a_transcript_row_for_another_turn_is_refused_without_a_write(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    receipt = _seeded(store, threads, "turn-1")["turn-1"]
    runs_before = threads.list_runs(THREAD_ID)

    foreign = RunRecord(
        id="turn-other",
        thread_id=THREAD_ID,
        question="what was revenue?",
        status="done",
        answer="43",
    )
    with pytest.raises(ReceiptContractError):
        store.commit_terminal(
            _terminal(receipt),
            expected_revision=receipt.revision,
            run=foreign,
        )

    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert stored == receipt
    assert threads.list_runs(THREAD_ID) == runs_before


def test_a_commit_for_a_turn_that_has_no_receipt_writes_nothing(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """An unknown turn is not a licence to mint one.

    A finalizer that arrives for a turn no door ever began would otherwise
    have to write the receipt it is finalizing, which is a second turn
    arriving from nowhere.
    """
    store, threads = harness
    receipt = _seeded(store, threads, "turn-1")["turn-1"]
    unknown = replace(receipt, turn_id="turn-unknown")
    runs_before = threads.list_runs(THREAD_ID)

    with pytest.raises(ReceiptNotFoundError):
        store.commit_terminal(
            _terminal(unknown),
            expected_revision=receipt.revision,
            run=_run("turn-unknown"),
        )

    assert store.read_receipt(THREAD_ID, "turn-unknown") is None
    assert store.read_receipt(THREAD_ID, "turn-1") == receipt
    assert threads.list_runs(THREAD_ID) == runs_before
