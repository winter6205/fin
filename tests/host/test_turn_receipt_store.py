"""The turn-receipt contract, asserted against both stores at once.

``InMemoryReceiptStore`` and ``PostgresReceiptStore`` are twins, so the
contract cases below are written once and run against both: the
in-memory half always runs, and the durable half skips unless
``FIN_CHECKPOINT_URL`` names a database (the repo's convention for
Postgres-backed cases, same as ``test_thread_store.py``). A behaviour
that only holds on one of them is a drift, and the duplicate ids in the
failure output name which one.

What is pinned here: the key, the revision a compare-and-swap
advances, the replay that must not move anything, the stale writer
that must lose, and the terminal commit that lands the receipt and its
transcript row together or not at all.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, cast
from uuid import uuid4

import pytest

from fin.host.thread_store import (
    InMemoryThreadStore,
    PersistenceUnavailableError,
    PostgresThreadStore,
    RunRecord,
    RunStatus,
    ThreadNotFoundError,
    ThreadStore,
    ThreadStoreError,
)
from fin.host.turn_receipt_store import (
    CancellationReason,
    STAGE_METRIC_KEYS,
    InMemoryReceiptStore,
    PostgresReceiptStore,
    ReceiptContractError,
    ReceiptNotFoundError,
    ReceiptRevisionConflict,
    ReceiptStore,
    TurnReceipt,
    zero_stage_metrics,
)
from fin.host.turn_receipt_store import _content_of
from fin.host.turn_receipts import TurnReceiptProjector

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

THREAD_ID = "receipt-thread"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "postgres"])
def harness(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[ReceiptStore, ThreadStore]]:
    """Both implementations of the one contract, plus their transcripts."""
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

        from fin.host.turn_receipt_store import PostgresReceiptStore as _store

        _store(self._url)
        with psycopg.connect(self._url, autocommit=True) as conn:
            conn.execute("DELETE FROM fin_turn_receipt")
            conn.execute("DELETE FROM fin_thread")

    def __exit__(self, *exc: object) -> None:
        return None


def _begin(
    store: ReceiptStore, threads: ThreadStore, turn_id: str = "turn-1"
) -> TurnReceipt:
    if threads.get_thread(THREAD_ID) is None:
        threads.create_thread(id=THREAD_ID, title="receipts")
    return store.begin_turn(
        thread_id=THREAD_ID,
        turn_id=turn_id,
        started_at=START,
        accepted_input_version=4,
    )


def _run(turn_id: str = "turn-1") -> RunRecord:
    return RunRecord(
        id=turn_id,
        thread_id=THREAD_ID,
        question="what was revenue?",
        status="done",
        answer="42",
    )


def _terminal(receipt: TurnReceipt, **over: object) -> TurnReceipt:
    values: dict[str, object] = {
        "execution_state": "terminal",
        "technical_result": "done",
        "semantic_result": "answered",
        "terminal_at": START + timedelta(seconds=3),
        "work_elapsed_ms": 1234,
        "accounting_complete": True,
        "stage_metrics": {
            **receipt.stage_metrics,
            "model": {"calls": 2, "elapsed_ms": 900, "complete": True},
        },
        "token_usage": {"input_tokens": 11, "output_tokens": 5},
    }
    values.update(over)
    return replace(receipt, **values)


# ----- keying -----


def test_a_receipt_is_addressed_by_its_thread_and_turn(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    _begin(store, threads, turn_id="turn-1")
    _begin(store, threads, turn_id="turn-2")

    assert store.read_receipt(THREAD_ID, "turn-1") is not None
    assert store.read_receipt(THREAD_ID, "turn-2") is not None
    # A turn id reused on another thread is a different turn, not a
    # second receipt for the first one.
    threads.create_thread(id="other-thread", title="other")
    store.begin_turn(
        thread_id="other-thread",
        turn_id="turn-1",
        started_at=START,
        accepted_input_version=0,
    )
    assert store.read_receipt(THREAD_ID, "turn-1").revision == 1
    assert store.read_receipt("other-thread", "turn-1").revision == 1


def test_reading_an_unknown_turn_is_absent_not_an_error(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, _threads = harness
    assert store.read_receipt(THREAD_ID, "never-started") is None


def test_begin_turn_mints_one_running_receipt(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)

    assert started.schema_version == 1
    assert started.revision == 1
    assert started.execution_state == "running"
    assert started.technical_result is None
    assert started.terminal_at is None
    # The count belongs to this turn and starts at one; the version is
    # Task Runtime's, so it can already be well above one.
    assert started.accepted_input_count == 1
    assert started.accepted_input_version == 4
    assert started.started_at == START
    assert started.updated_at == START
    assert started.accounting_complete is False
    assert started.exported_revision == 0
    assert started.segment_seq == 0
    # Key ORDER is not part of the contract: a JSONB column returns its
    # own normalised order, so only the set is pinned.
    assert set(started.stage_metrics) == set(STAGE_METRIC_KEYS)
    assert started.stage_metrics["cleanup"] == {
        "calls": 0,
        "elapsed_ms": 0,
        "complete": True,
    }


def test_beginning_the_same_turn_twice_is_a_no_op(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    first = _begin(store, threads)

    again = store.begin_turn(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        started_at=START + timedelta(hours=1),
        accepted_input_version=99,
    )

    assert again == first
    assert store.read_receipt(THREAD_ID, "turn-1") == first


# ----- revision and compare-and-swap -----


def test_a_committed_change_advances_the_revision_by_one(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)

    paused = store.write_receipt(
        replace(started, execution_state="paused"), expected_revision=started.revision
    )

    assert paused.revision == started.revision + 1
    assert paused.execution_state == "paused"
    # The turn's original start is not the pause's start.
    assert paused.started_at == started.started_at


def test_replaying_the_same_change_moves_nothing(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    draft = replace(started, execution_state="paused")
    paused = store.write_receipt(draft, expected_revision=started.revision)

    replay = store.write_receipt(draft, expected_revision=started.revision)

    assert replay == paused
    assert store.read_receipt(THREAD_ID, "turn-1") == paused


def test_export_cursor_survives_old_draft_and_old_acknowledgement(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    old_draft = replace(started, semantic_result="answered")

    assert store.acknowledge_export(THREAD_ID, started.turn_id, started.revision)
    acknowledged = store.read_receipt(THREAD_ID, started.turn_id)
    assert acknowledged is not None
    assert acknowledged.exported_revision == started.revision
    assert acknowledged.revision == started.revision
    assert acknowledged.updated_at == started.updated_at

    committed = store.write_receipt(old_draft, expected_revision=started.revision)
    assert committed.revision == started.revision + 1
    assert committed.exported_revision == started.revision

    assert store.acknowledge_export(
        THREAD_ID, started.turn_id, committed.revision
    )
    assert not store.acknowledge_export(
        THREAD_ID, started.turn_id, started.revision
    )
    current = store.read_receipt(THREAD_ID, started.turn_id)
    assert current is not None
    assert current.exported_revision == committed.revision
    assert current.revision == committed.revision
    assert current.updated_at == committed.updated_at
    assert store.list_pending_receipts(limit=10) == ()


@pytest.mark.parametrize("limit", [0, -1, 257, True])
def test_pending_export_scan_rejects_unbounded_or_invalid_limits(
    harness: tuple[ReceiptStore, ThreadStore], limit: object
) -> None:
    store, threads = harness
    _begin(store, threads)

    with pytest.raises(ReceiptContractError):
        store.list_pending_receipts(limit=cast(int, limit))


@pytest.mark.parametrize(
    "after",
    [[], ("", "turn"), ("thread", ""), ("x" * 257, "turn")],
)
def test_pending_export_scan_rejects_invalid_cursors(
    harness: tuple[ReceiptStore, ThreadStore], after: object
) -> None:
    store, _threads = harness

    with pytest.raises(ReceiptContractError):
        store.list_pending_receipts(after=cast(tuple[str, str], after))


def test_postgres_export_scan_and_ack_fail_with_bounded_table_locks(
    tmp_path: Path,
) -> None:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip("FIN_TEST_DSN is not set; durable lock checks were not run")

    import psycopg

    home = tmp_path
    with psycopg.connect(url) as identity_conn:
        database_name = identity_conn.execute("SELECT current_database()").fetchone()[0]
        assert database_name.endswith("_test")
        has_identity_table = identity_conn.execute(
            "SELECT to_regclass('public.fin_instance') IS NOT NULL"
        ).fetchone()[0]
        identity = None
        if has_identity_table:
            rows = identity_conn.execute("SELECT id::text FROM fin_instance").fetchall()
            assert len(rows) <= 1
            identity = rows[0][0] if rows else None
    if identity is not None:
        home.mkdir(parents=True, exist_ok=True)
        identity_file = home / "instance_id"
        identity_file.write_text(f"{identity}\n", encoding="utf-8")
        identity_file.chmod(0o600)

    thread_id = f"receipt-lock-{uuid4().hex}"
    turn_id = f"turn-{uuid4().hex}"
    threads = PostgresThreadStore(url, fin_home=home)
    store = PostgresReceiptStore(url, fin_home=home)
    try:
        threads.create_thread(id=thread_id, title="receipt lock test")
        store.begin_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            started_at=START,
            accepted_input_version=0,
        )
        with psycopg.connect(url) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "LOCK TABLE fin_turn_receipt IN ACCESS EXCLUSIVE MODE"
                )
                started = time.monotonic()
                with pytest.raises(PersistenceUnavailableError):
                    store.list_pending_receipts(limit=1)
                scan_elapsed = time.monotonic() - started
        with psycopg.connect(url) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT 1 FROM fin_turn_receipt "
                    "WHERE thread_id = %s AND turn_id = %s FOR UPDATE",
                    (thread_id, turn_id),
                )
                started = time.monotonic()
                with pytest.raises(PersistenceUnavailableError):
                    store.acknowledge_export(thread_id, turn_id, 1)
                ack_elapsed = time.monotonic() - started
        assert scan_elapsed < 1.5
        assert ack_elapsed < 1.0
        receipt = store.read_receipt(thread_id, turn_id)
        assert receipt is not None
        assert receipt.exported_revision == 0
    finally:
        if threads.get_thread(thread_id) is not None:
            threads.delete_thread(thread_id)


def test_a_stale_writer_loses_instead_of_clobbering(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    paused = store.write_receipt(
        replace(started, execution_state="paused"), expected_revision=started.revision
    )

    with pytest.raises(ReceiptRevisionConflict) as conflict:
        store.write_receipt(
            replace(started, semantic_result="answered"),
            expected_revision=started.revision,
        )

    assert (conflict.value.expected, conflict.value.actual) == (
        started.revision,
        paused.revision,
    )
    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert stored.revision == paused.revision
    assert stored.execution_state == "paused"
    # The losing write carried a claim about the answer; refusing it
    # must leave no trace of that claim.
    assert stored.semantic_result is None


def test_a_conflicting_update_for_the_same_key_is_refused(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    first = store.write_receipt(
        replace(started, semantic_result="answered"), expected_revision=started.revision
    )

    with pytest.raises(ReceiptRevisionConflict):
        store.write_receipt(
            replace(started, semantic_result="evidence_refuse"),
            expected_revision=started.revision,
        )

    assert store.read_receipt(THREAD_ID, "turn-1").semantic_result == "answered"
    assert store.read_receipt(THREAD_ID, "turn-1").revision == first.revision


def test_writing_a_turn_that_has_no_receipt_is_refused(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    _begin(store, threads)
    never_started = TurnReceipt(thread_id=THREAD_ID, turn_id="never-started")

    with pytest.raises(ReceiptNotFoundError):
        store.write_receipt(never_started, expected_revision=1)


def test_the_original_start_is_not_a_writable_field(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)

    with pytest.raises(ReceiptContractError):
        store.write_receipt(
            replace(started, started_at=START + timedelta(days=1)),
            expected_revision=started.revision,
        )

    assert store.read_receipt(THREAD_ID, "turn-1").started_at == START


# ----- accepted inputs -----


def test_each_new_accepted_input_counts_once(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)

    counted = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=started.revision,
        input_version=9,
    )

    assert counted.accepted_input_count == 2
    assert counted.accepted_input_version == 9
    assert counted.revision == started.revision + 1


def test_replaying_an_accepted_input_version_moves_nothing(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    counted = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=started.revision,
        input_version=9,
    )

    replay = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=counted.revision,
        input_version=9,
    )

    assert replay == counted


def test_an_accepted_reply_opens_the_next_segment(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """``segment_seq`` is what trace span ids are derived from, so a
    duplicate delivery that bumped it would put one execution segment
    under two roots."""
    store, threads = harness
    started = _begin(store, threads)
    counted = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=started.revision,
        input_version=9,
    )

    replay = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=counted.revision,
        input_version=9,
    )
    resumed = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=replay.revision,
        input_version=10,
    )

    assert replay.segment_seq == counted.segment_seq == 1
    assert resumed.segment_seq == 2
    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert stored is not None
    assert stored.segment_seq == 2


def test_no_other_write_inside_a_segment_moves_the_counter(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """A segment bump inside ``_CONTENT_FIELDS`` would make every
    content replay look like a content change, so the counter is only
    advanced by the accepted-input write path."""
    store, threads = harness
    started = _begin(store, threads)
    counted = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=started.revision,
        input_version=9,
    )
    # A draft cannot carry a segment of its own choosing.
    moved = store.write_receipt(
        replace(counted, work_elapsed_ms=99, segment_seq=77),
        expected_revision=counted.revision,
    )

    assert moved.segment_seq == 1
    assert _content_of(moved) != _content_of(counted)
    replayed = store.write_receipt(
        replace(moved, work_elapsed_ms=99, segment_seq=0),
        expected_revision=moved.revision,
    )
    assert replayed == moved


def test_an_accepted_input_version_may_leave_a_gap(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """Task Runtime's version is authoritative; the receipt does not
    assume its own projection plus one, because an intervening accepted
    input on another turn moves it too."""
    store, threads = harness
    started = _begin(store, threads)
    counted = store.record_accepted_input(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        expected_revision=started.revision,
        input_version=17,
    )

    assert counted.accepted_input_count == 2
    assert counted.accepted_input_version == 17


# ----- terminal commit -----


def test_the_terminal_receipt_and_the_transcript_row_land_together(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    terminal = _terminal(started)

    done = store.commit_terminal(
        terminal, expected_revision=started.revision, run=_run()
    )

    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert done.revision == started.revision + 1
    assert stored.execution_state == "terminal"
    assert stored.technical_result == "done"
    assert stored.terminal_at == START + timedelta(seconds=3)
    assert stored.work_elapsed_ms == 1234
    assert stored.accounting_complete is True
    assert stored.stage_metrics["model"] == {
        "calls": 2,
        "elapsed_ms": 900,
        "complete": True,
    }
    assert stored.token_usage == {"input_tokens": 11, "output_tokens": 5}
    assert [row.id for row in threads.list_runs(THREAD_ID)] == ["turn-1"]


def test_a_duplicate_finalizer_is_a_no_op(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    terminal = _terminal(started)
    done = store.commit_terminal(
        terminal, expected_revision=started.revision, run=_run()
    )

    again = store.commit_terminal(
        terminal, expected_revision=started.revision, run=_run()
    )

    assert again == done
    assert [row.id for row in threads.list_runs(THREAD_ID)] == ["turn-1"]


def test_a_finalizer_for_another_turn_is_refused(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)

    with pytest.raises(ReceiptContractError):
        store.commit_terminal(
            _terminal(started),
            expected_revision=started.revision,
            run=_run(turn_id="some-other-run"),
        )

    assert threads.list_runs(THREAD_ID) == []


def test_a_transcript_write_that_fails_leaves_neither_half(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """The transcript row's own validation is the failure injected here.

    It runs after the receipt write has been issued, which is the only
    way to show that a failure in the second half of the commit takes
    the first half with it.
    """
    store, threads = harness
    started = _begin(store, threads)
    paused = store.write_receipt(
        replace(started, execution_state="paused"), expected_revision=started.revision
    )
    terminal = _terminal(
        paused,
        technical_result="failed",
        semantic_result=None,
        failure_kind="internal",
    )
    # A run whose status is outside the closed enum: the row the
    # transcript write refuses, not a row it would have written.
    broken = RunRecord(
        id="turn-1",
        thread_id=THREAD_ID,
        question="what was revenue?",
        status=cast("RunStatus", "not-a-status"),
    )

    with pytest.raises(ThreadStoreError):
        store.commit_terminal(terminal, expected_revision=paused.revision, run=broken)

    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert stored.revision == paused.revision
    assert stored.execution_state == "paused"
    assert stored.terminal_at is None
    assert threads.list_runs(THREAD_ID) == []


# ----- the shape the table itself refuses -----

@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"execution_state": "finished"}, id="unknown-execution-state"),
        pytest.param({"execution_state": "terminal"}, id="terminal-without-result"),
        pytest.param({"failure_kind": "kaboom"}, id="unlisted-failure-kind"),
        pytest.param({"failure_kind": "internal"}, id="failure-kind-without-failure"),
        pytest.param({"not_ready_cause": "index"}, id="cause-without-stage"),
        pytest.param(
            {"not_ready_cause": "busy", "not_ready_stage": "index"},
            id="unobserved-pair",
        ),
        pytest.param({"exported_revision": 99}, id="export-ahead-of-revision"),
        pytest.param({"segment_seq": -1}, id="negative-segment-seq"),
        pytest.param({"work_elapsed_ms": -1}, id="negative-work"),
        pytest.param({"accepted_input_count": 0}, id="no-accepted-input"),
        pytest.param({"stage_metrics": {"model": {}}}, id="one-stage-only"),
        pytest.param(
            {"token_usage": {"guesses": 3}},
            id="unlisted-token-key",
        ),
        pytest.param(
            {"token_usage": {"input_tokens": "lots"}},
            id="estimated-token-count",
        ),
    ],
)
def test_a_draft_the_table_could_not_hold_is_refused(
    harness: tuple[ReceiptStore, ThreadStore], over: dict[str, object]
) -> None:
    store, threads = harness
    started = _begin(store, threads)

    with pytest.raises(ReceiptContractError):
        store.write_receipt(
            replace(started, **over), expected_revision=started.revision
        )


def test_a_terminal_draft_needs_its_technical_result_and_time(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    terminal = _terminal(started)

    with pytest.raises(ReceiptContractError):
        store.commit_terminal(
            replace(terminal, terminal_at=None),
            expected_revision=started.revision,
            run=_run(),
        )

    with pytest.raises(ReceiptContractError):
        store.commit_terminal(
            replace(terminal, execution_state="paused"),
            expected_revision=started.revision,
            run=_run(),
        )

    assert store.read_receipt(THREAD_ID, "turn-1").execution_state == "running"
    assert threads.list_runs(THREAD_ID) == []


def test_an_unstarted_stage_is_a_known_zero(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    started = _begin(store, threads)
    metrics = zero_stage_metrics()

    started = store.write_receipt(
        replace(
            started,
            work_elapsed_ms=900,
            stage_metrics={
                **metrics,
                "model": {"calls": 1, "elapsed_ms": 900, "complete": True},
                # Interrupted mid-run: what was observed, marked as a
                # lower bound rather than passed off as a total.
                "cleanup": {"calls": 0, "elapsed_ms": 0, "complete": False},
            },
        ),
        expected_revision=started.revision,
    )

    stored = store.read_receipt(THREAD_ID, "turn-1")
    assert stored.accounting_complete is False
    assert stored.stage_metrics["cleanup"]["complete"] is False
    assert stored.stage_metrics["retrieve"] == {
        "calls": 0,
        "elapsed_ms": 0,
        "complete": True,
    }


# ----- isolation of what a caller is handed back -----

def test_a_read_receipt_cannot_be_mutated_into_the_store(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    _begin(store, threads)

    read = store.read_receipt(THREAD_ID, "turn-1")
    assert read is not None
    # The record is frozen, but the JSONB payloads inside it are not.
    read.stage_metrics["model"] = {"calls": 99, "elapsed_ms": 99, "complete": True}

    fresh = store.read_receipt(THREAD_ID, "turn-1")
    assert fresh.revision == 1
    assert fresh.stage_metrics["model"] == {
        "calls": 0,
        "elapsed_ms": 0,
        "complete": True,
    }


# ----- enumeration, the only seam that can count receipts -----

def test_listing_receipts_reports_every_turn_in_order(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    """Enumeration is the seam that makes the count observable.

    A key-addressed read answers the same whether the thread holds one
    receipt or four, so "exactly one receipt per turn" is unprovable
    without a listing. Ordered by ``turn_id`` so a case is not
    asserting on a row order the database chose.
    """
    store, threads = harness
    for turn_id in ("turn-3", "turn-1", "turn-2"):
        _begin(store, threads, turn_id)

    listed = store.list_receipts(THREAD_ID)

    assert [receipt.turn_id for receipt in listed] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]


def test_listing_receipts_leaves_another_threads_rows_alone(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    _begin(store, threads, "turn-1")
    threads.create_thread(id="receipt-other-thread", title="other")
    store.begin_turn(
        thread_id="receipt-other-thread",
        turn_id="turn-other",
        started_at=START,
        accepted_input_version=1,
    )

    assert [r.turn_id for r in store.list_receipts(THREAD_ID)] == ["turn-1"]


def test_listing_receipts_of_an_unknown_thread_is_empty_not_an_error(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, _threads = harness

    assert store.list_receipts("receipt-no-such-thread") == ()


def test_a_receipt_failure_names_the_receipt_store_not_the_thread_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed error names the store that failed.

    ``guarded_connection`` is shared with the thread store, so a receipt
    action that lost its database would otherwise be reported as the
    thread store being unreachable — sending an operator to the wrong
    component entirely.
    """
    import psycopg

    import fin.host.thread_store as threads_module

    def down(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("the database is down")

    # The seam under test is ``thread_store.guarded_connection``, so the
    # driver it opens with is the one to take down.
    monkeypatch.setattr(threads_module.psycopg, "connect", down)

    from fin.host.turn_receipt_store import PostgresReceiptStore

    store = PostgresReceiptStore.__new__(PostgresReceiptStore)
    store._url = "postgresql://stub"

    with pytest.raises(PersistenceUnavailableError) as caught:
        store.read_receipt("t-noddl", "turn-1")

    assert "the receipt store" in str(caught.value)


def test_a_refused_delete_leaves_the_receipts_it_had(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal that dropped receipts first would mutate before raising.

    ``delete_thread`` used to remove the thread's receipts and only then
    discover the thread was unknown — so the one outcome that reports
    nothing was removed is exactly the one that removed them.
    """
    threads = InMemoryThreadStore()
    store = InMemoryReceiptStore(threads)
    _begin(store, threads, "turn-1")

    with pytest.raises(ThreadNotFoundError):
        store.delete_thread("no-such-thread")

    assert store.read_receipt(THREAD_ID, "turn-1") is not None


def test_a_legacy_resume_reopens_the_same_receipt_without_accepting_input(
    harness: tuple[ReceiptStore, ThreadStore],
) -> None:
    store, threads = harness
    _begin(store, threads)
    projector = TurnReceiptProjector(store)
    paused = projector.paused(
        thread_id=THREAD_ID, turn_id="turn-1", semantic_result="clarify"
    )
    assert paused is not None

    running = projector.resume_started(thread_id=THREAD_ID, turn_id="turn-1")

    assert running is not None
    assert running.execution_state == "running"
    assert running.semantic_result is None
    assert running.accepted_input_count == paused.accepted_input_count
    assert running.accepted_input_version == paused.accepted_input_version
    assert running.segment_seq == paused.segment_seq
    assert running.stage_metrics == paused.stage_metrics
    assert store.read_receipt(THREAD_ID, "turn-1") == running
    assert threads.list_runs(THREAD_ID) == []


@pytest.mark.parametrize("reason", ["user_stop", "client_disconnect"])
def test_a_stopped_resume_terminalizes_the_receipt_without_a_transcript_row(
    harness: tuple[ReceiptStore, ThreadStore], reason: CancellationReason
) -> None:
    store, threads = harness
    _begin(store, threads)
    projector = TurnReceiptProjector(store)
    paused = projector.paused(
        thread_id=THREAD_ID, turn_id="turn-1", semantic_result="clarify"
    )
    assert paused is not None
    running = projector.resume_started(thread_id=THREAD_ID, turn_id="turn-1")
    assert running is not None

    stopped = projector.stopped(
        thread_id=THREAD_ID,
        turn_id="turn-1",
        cancellation_reason=reason,
    )

    assert stopped is not None
    assert stopped.execution_state == "terminal"
    assert stopped.technical_result == "stopped"
    assert stopped.cancellation_reason == reason
    assert stopped.terminal_at is not None
    assert stopped.accepted_input_count == running.accepted_input_count
    assert stopped.accepted_input_version == running.accepted_input_version
    assert stopped.segment_seq == running.segment_seq
    assert stopped.stage_metrics == running.stage_metrics
    assert store.read_receipt(THREAD_ID, "turn-1") == stopped
    assert threads.list_runs(THREAD_ID) == []
