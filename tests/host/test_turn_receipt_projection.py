"""What one run's observed facts project onto its turn's receipt.

The projector is the seam between the run lifecycle and the receipt
contract, so what is pinned here is the projection itself: that a turn
identity yields one receipt, that a lifecycle stretch adds its work to
the turn's total instead of replacing it, that a duplicate finalizer
changes nothing, and that a stage nobody clocked is reported as an
unknown rather than as a known absence.

These cases run unconditionally against the in-memory store: the
projection has no durability of its own to prove, and the durable
variants of the same contract live with the store itself.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from fin.host import turn_receipt_store as receipt_store_module
from fin.host import turn_receipts as receipts_module
from fin.host.run_budgets import STAGE_CLEANUP, STAGE_EXECUTE, StageOutcome
from fin.host.thread_store import InMemoryThreadStore, RunRecord
from fin.host.turn_receipt_store import (
    STAGE_METRIC_FIELDS,
    STAGE_METRIC_KEYS,
    InMemoryReceiptStore,
    ReceiptStore,
    TurnReceipt,
)
from fin.host.turn_receipts import (
    TurnReceiptProjector,
    failure_token,
    projected_stage_metrics,
)

THREAD_ID = "projection-thread"
TURN_ID = "projection-turn"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Clock:
    """A wall clock this test moves by hand.

    The deliberate wait is the only duration a receipt measures from
    something other than the run's own clock, so pinning both stores'
    clock is what makes "five seconds of waiting" an assertion instead
    of a near-miss.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(name="clock")
def _clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock(START)
    monkeypatch.setattr(receipts_module, "_now", clock)
    monkeypatch.setattr(receipt_store_module, "_now", clock)
    return clock


def _execute(*, elapsed_ms: int, complete: bool = True) -> StageOutcome:
    return StageOutcome(
        stage=STAGE_EXECUTE,
        elapsed_ms=elapsed_ms,
        budget_ms=120_000,
        exceeded=False,
        complete=complete,
    )


def _cleanup(*, elapsed_ms: int, complete: bool = True) -> StageOutcome:
    return StageOutcome(
        stage=STAGE_CLEANUP,
        elapsed_ms=elapsed_ms,
        budget_ms=1_000,
        exceeded=False,
        complete=complete,
    )


def _harness() -> tuple[TurnReceiptProjector, InMemoryThreadStore]:
    threads = InMemoryThreadStore()
    threads.create_thread(id=THREAD_ID, title="projection")
    return TurnReceiptProjector(InMemoryReceiptStore(threads)), threads


def _run(
    threads: InMemoryThreadStore,
    *,
    track_decision: dict[str, Any] | None = None,
    semantic_outcome: str | None = None,
) -> RunRecord:
    return RunRecord(
        id=TURN_ID,
        thread_id=THREAD_ID,
        question="what happened?",
        status="done",
        answer="an answer",
        track_decision=track_decision or {},
        semantic_outcome=semantic_outcome,
    )


def _begin(projector: TurnReceiptProjector) -> TurnReceipt:
    return projector.begin(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        accepted_input_version=1,
        started_at=START,
    )


# ----- the identity of the turn -----


def test_a_turn_identity_yields_one_receipt_however_often_it_starts() -> None:
    projector, _threads = _harness()

    first = _begin(projector)
    again = _begin(projector)

    assert again.revision == first.revision
    assert projector.read(THREAD_ID, TURN_ID) == first


def test_a_resumed_run_cannot_mint_a_second_receipt() -> None:
    projector, threads = _harness()
    _begin(projector)
    projector.paused(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        semantic_result="clarify",
        stage_metrics=(_execute(elapsed_ms=40),),
    )

    projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        stage_metrics=(_execute(elapsed_ms=60), _cleanup(elapsed_ms=2)),
        run=_run(threads),
    )

    assert [run.id for run in threads.list_runs(THREAD_ID)] == [TURN_ID]
    assert projector.read(THREAD_ID, TURN_ID) is not None


# ----- the terminal projection -----

def test_a_finished_turn_reports_what_it_observed() -> None:
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        stage_metrics=(_execute(elapsed_ms=900), _cleanup(elapsed_ms=3)),
        run=_run(threads),
        terminal_at=START + timedelta(seconds=2),
    )

    assert committed is not None
    assert committed.execution_state == "terminal"
    assert committed.technical_result == "done"
    assert committed.semantic_result == "answered"
    assert committed.terminal_at == START + timedelta(seconds=2)
    assert committed.work_elapsed_ms == 900
    assert committed.accounting_complete is True
    assert committed.started_at == START
    assert tuple(committed.stage_metrics) == STAGE_METRIC_KEYS
    for entry in committed.stage_metrics.values():
        assert tuple(entry) == STAGE_METRIC_FIELDS


def test_a_finished_turn_projects_its_audited_route() -> None:
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        run=_run(
            threads,
            track_decision={
                "route_intent": " Retrieve ",
                "route_source": " JEV ",
            },
        ),
    )

    assert committed is not None
    assert committed.route_intent == "retrieve"
    assert committed.route_source == "jev"
    assert committed.semantic_result == "answered"


def test_a_validated_evidence_refusal_is_projected_from_the_run() -> None:
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        run=_run(
            threads,
            track_decision={
                "route_intent": "retrieve",
                "route_source": "fallback",
            },
            semantic_outcome="insufficient_evidence",
        ),
    )

    assert committed is not None
    assert committed.route_intent == "retrieve"
    assert committed.route_source == "fallback"
    assert committed.semantic_result == "evidence_refuse"


def test_an_explicit_policy_refusal_is_projected_without_reclassifying_text() -> None:
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        run=_run(
            threads,
            track_decision={
                "route_intent": "policy_refuse",
                "route_source": "rule",
            },
        ),
    )

    assert committed is not None
    assert committed.route_intent == "policy_refuse"
    assert committed.route_source == "rule"
    assert committed.semantic_result == "policy_refuse"


@pytest.mark.parametrize(
    "track_decision",
    [
        {},
        {"route_intent": "evidence_refuse", "route_source": "model"},
        {"route_source": "rule"},
    ],
)
def test_missing_or_unrecognized_route_facts_stay_unknown(
    track_decision: dict[str, Any],
) -> None:
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        run=_run(threads, track_decision=track_decision),
    )

    assert committed is not None
    assert committed.route_intent is None
    assert committed.route_source is None
    assert committed.semantic_result is None


def test_a_paused_document_clarification_keeps_its_route_audit() -> None:
    projector, _threads = _harness()
    _begin(projector)

    paused = projector.paused(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        semantic_result="clarify",
        track_decision={
            "route_intent": "retrieve",
            "route_source": "jev",
        },
    )

    assert paused is not None
    assert paused.execution_state == "paused"
    assert paused.semantic_result == "clarify"
    assert paused.route_intent == "retrieve"
    assert paused.route_source == "jev"


def test_a_second_finalizer_changes_nothing() -> None:
    projector, threads = _harness()
    _begin(projector)
    outcomes = (_execute(elapsed_ms=900), _cleanup(elapsed_ms=3))
    projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        stage_metrics=outcomes,
        run=_run(threads),
    )
    first = projector.read(THREAD_ID, TURN_ID)
    assert first is not None

    duplicate = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        stage_metrics=outcomes,
        run=_run(threads),
    )

    assert duplicate == first
    assert projector.read(THREAD_ID, TURN_ID) == first
    assert len(threads.list_runs(THREAD_ID)) == 1


def test_a_terminal_commit_without_a_receipt_still_leaves_its_row() -> None:
    projector, threads = _harness()

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        run=_run(threads),
    )

    assert committed is None
    assert threads.list_runs(THREAD_ID) == []


# ----- the twelve stages -----

def test_the_release_stage_is_projected_as_measured() -> None:
    metrics = projected_stage_metrics((_cleanup(elapsed_ms=7),))

    assert metrics["cleanup"] == {
        "calls": 1,
        "elapsed_ms": 7,
        "complete": True,
    }
    # The streamed stretch is the run's work, not one model invocation,
    # so it stays out of the model keys entirely.
    assert metrics["model"] == {"calls": 0, "elapsed_ms": 0, "complete": False}


def test_a_release_that_survived_its_grace_still_counts_as_started() -> None:
    metrics = projected_stage_metrics((_cleanup(elapsed_ms=2500, complete=False),))

    assert metrics["cleanup"] == {
        "calls": 1,
        "elapsed_ms": 2500,
        "complete": False,
    }


def test_a_release_that_never_reached_is_not_a_known_absence() -> None:
    metrics = projected_stage_metrics((_cleanup(elapsed_ms=0, complete=False),))

    assert metrics["cleanup"] == {
        "calls": 0,
        "elapsed_ms": 0,
        "complete": False,
    }


def test_a_continuation_adds_its_stretch_to_the_turns_stage() -> None:
    first = projected_stage_metrics((_cleanup(elapsed_ms=3),))
    second = projected_stage_metrics((_cleanup(elapsed_ms=4),), first)

    assert second["cleanup"] == {
        "calls": 2,
        "elapsed_ms": 7,
        "complete": True,
    }


# ----- the model's own keys -----


def test_the_model_keys_report_the_calls_the_run_actually_observed() -> None:
    """``model`` is the inclusive total of the counts beneath it.

    Every key that is a breakdown is a disjoint slice of the same set of
    invocations, so the inclusive total is their sum — a reader can add
    the parts to the whole and land on the whole, which is the only
    shape in which "do not add the breakdown again" can be checked at
    all.
    """
    counts = {
        "runtime_reconciliation": 1,
        "grounded_decision": 4,
        "tool_planning": 2,
        "other_model": 3,
    }

    metrics = projected_stage_metrics((), None, counts)

    for key, expected in counts.items():
        assert metrics[key]["calls"] == expected
    assert metrics["model"]["calls"] == sum(counts.values())
    # A JEV-tier routing call is its own stage, never folded into the
    # model's inclusive total.
    assert metrics["jev"]["calls"] == 0


def test_the_model_keys_stay_open_because_nothing_times_those_calls() -> None:
    """Observed is not measured.

    The invocation counts are durably observed, but no clock runs around
    a model call here, so elapsed is a zero nobody measured and the
    stage reports itself unfinished rather than claiming a total.
    """
    metrics = projected_stage_metrics((), None, {"grounded_decision": 2})

    assert metrics["grounded_decision"] == {
        "calls": 2,
        "elapsed_ms": 0,
        "complete": False,
    }


def test_a_continuation_adds_its_calls_instead_of_replacing_them() -> None:
    first = projected_stage_metrics((), None, {"tool_planning": 2})
    second = projected_stage_metrics((), first, {"tool_planning": 1, "other_model": 4})

    assert second["tool_planning"]["calls"] == 3
    assert second["other_model"]["calls"] == 4
    assert second["model"]["calls"] == 7


def test_a_run_that_observed_no_model_call_reports_no_model_work() -> None:
    """The zero here IS a measurement: the boundary was watched.

    An observed zero and an un-watched stage read differently on the
    same column, and this is the watched one: the run's whole model
    surface went past without a single invocation reaching it.
    """
    watched = projected_stage_metrics((), None, {})
    unwatched = projected_stage_metrics((), None, None)

    assert watched["model"]["calls"] == 0
    assert watched["model"]["complete"] is False
    # Same numbers, and the row still cannot tell them apart: what
    # separates a measured zero from an unobserved one on this contract
    # is the caller's word, not the column.
    assert unwatched["model"]["calls"] == 0


def test_every_stage_carries_its_three_fields() -> None:
    metrics = projected_stage_metrics(())

    assert tuple(metrics) == STAGE_METRIC_KEYS
    for entry in metrics.values():
        assert tuple(entry) == STAGE_METRIC_FIELDS


# ----- work, waits and interruptions -----


def test_a_paused_prefix_and_its_continuation_report_one_work_total(
    clock: _Clock,
) -> None:
    projector, threads = _harness()
    _begin(projector)
    clock.advance(2)
    projector.paused(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        semantic_result="clarify",
        stage_metrics=(_execute(elapsed_ms=40), _cleanup(elapsed_ms=1)),
    )
    clock.advance(5)
    projector.accepted_reply(
        thread_id=THREAD_ID, turn_id=TURN_ID, input_version=2
    )
    clock.advance(1)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        stage_metrics=(_execute(elapsed_ms=60), _cleanup(elapsed_ms=2)),
        run=_run(threads),
    )

    assert committed is not None
    assert committed.work_elapsed_ms == 100
    assert committed.stage_metrics["cleanup"] == {
        "calls": 2,
        "elapsed_ms": 3,
        "complete": True,
    }
    # The five seconds the operator spent choosing is a deliberate wait,
    # never work: it is measured, and it is not in the work total.
    assert committed.user_wait_ms == 5_000
    assert committed.accepted_input_count == 2


def test_an_interrupted_prefix_keeps_the_turns_accounting_open() -> None:
    projector, threads = _harness()
    _begin(projector)
    projector.paused(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        semantic_result="clarify",
        stage_metrics=(_execute(elapsed_ms=40, complete=False),),
    )

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        stage_metrics=(_execute(elapsed_ms=60),),
        run=_run(threads),
    )

    assert committed is not None
    assert committed.work_elapsed_ms == 100
    assert committed.accounting_complete is False


def test_a_release_that_outran_its_grace_keeps_the_turns_accounting_open() -> None:
    """A clocked stage left unfinished cannot sit under a closed flag.

    ``accounting_complete`` and a stage's ``complete`` describe the same
    condition from two directions — both answer "are these measurements
    totals, or the lower bounds an interruption left" — so a turn whose
    release overran its grace is one unfinished measurement beneath a flag
    claiming nothing was left unfinished. The execute stage finished
    here; it is the release the ledger could not close.
    """
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        stage_metrics=(_execute(elapsed_ms=60), _cleanup(elapsed_ms=2500, complete=False)),
        run=_run(threads),
    )

    assert committed is not None
    assert committed.stage_metrics["cleanup"]["complete"] is False
    assert committed.accounting_complete is False


def test_a_stage_the_executor_never_clocks_is_not_an_unfinished_one() -> None:
    """The other ten keys are an unknown, not an interruption.

    A stage the executor does not time carries no observation that could
    be incomplete, so it cannot decide whether the turn's accounting is
    closed — otherwise every turn, however cleanly it ran, would report
    its accounting forever open.
    """
    projector, threads = _harness()
    _begin(projector)

    committed = projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        stage_metrics=(_execute(elapsed_ms=60), _cleanup(elapsed_ms=7)),
        run=_run(threads),
    )

    assert committed is not None
    assert committed.accounting_complete is True
    assert committed.stage_metrics["model"]["complete"] is False


# ----- a write that loses its row -----


class _RacingReceiptStore:
    """A store whose row moves out from under the writer's own read.

    The other observer is a real write through the real store, so the
    conflict the projector has to survive is the one it would meet in
    production rather than a stubbed exception.
    """

    def __init__(self, inner: InMemoryReceiptStore) -> None:
        self._inner = inner
        self._raced = False

    def read_receipt(self, thread_id: str, turn_id: str) -> TurnReceipt | None:
        return self._inner.read_receipt(thread_id, turn_id)

    def write_receipt(
        self, draft: TurnReceipt, *, expected_revision: int
    ) -> TurnReceipt:
        if not self._raced:
            self._raced = True
            self._inner.write_receipt(
                replace(draft, semantic_result="clarify"),
                expected_revision=expected_revision,
            )
        return self._inner.write_receipt(draft, expected_revision=expected_revision)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_lost_revision_race_is_said_out_loud_and_keeps_the_stored_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stored row stands, and the operator is told it stood.

    A compare-and-swap that quietly returns somebody else's row reads
    as a write that landed, so the loss itself is what gets logged —
    the same signal the terminal commit already emits for the same race.
    """
    threads = InMemoryThreadStore()
    threads.create_thread(id=THREAD_ID, title="projection")
    inner = InMemoryReceiptStore(threads)
    projector = TurnReceiptProjector(cast("ReceiptStore", _RacingReceiptStore(inner)))
    _begin(projector)

    with caplog.at_level(logging.WARNING, logger=receipts_module.__name__):
        stood = projector.paused(
            thread_id=THREAD_ID,
            turn_id=TURN_ID,
            semantic_result="answered",
        )

    assert stood is not None
    assert stood.semantic_result == "clarify"
    losses = [
        record.getMessage()
        for record in caplog.records
        if "lost a revision race" in record.getMessage()
    ]
    assert len(losses) == 1
    assert TURN_ID in losses[0]
    assert THREAD_ID in losses[0]


# ----- a wire that ends without an outcome -----

def test_a_paused_turn_is_not_a_lost_client() -> None:
    """A deliberate wait ends its stream too, and that is not a disconnect.

    Every stream ends through one ``finally``, so the recorder cannot
    use "the wire is over" as its signal. A turn sitting in a pause is
    waiting for the operator on purpose, and relabelling it would end a
    turn that is still answerable.
    """
    projector, _threads = _harness()
    _begin(projector)
    paused = projector.paused(
        thread_id=THREAD_ID, turn_id=TURN_ID, semantic_result="clarify"
    )
    assert paused is not None

    assert (
        projector.stream_abandoned(thread_id=THREAD_ID, turn_id=TURN_ID) is None
    )

    stored = projector.read(THREAD_ID, TURN_ID)
    assert stored is not None
    assert stored.execution_state == "paused"
    assert stored.cancellation_reason is None
    assert stored.terminal_at is None


def test_a_turn_that_already_ended_is_left_exactly_as_it_ended() -> None:
    """A finished turn keeps its own result, instants and totals."""
    projector, threads = _harness()
    _begin(projector)
    projector.commit_terminal(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        technical_result="done",
        semantic_result="answered",
        stage_metrics=(_execute(elapsed_ms=900), _cleanup(elapsed_ms=3)),
        run=_run(threads),
        terminal_at=START + timedelta(seconds=2),
    )
    finished = projector.read(THREAD_ID, TURN_ID)
    assert finished is not None

    assert projector.stream_abandoned(thread_id=THREAD_ID, turn_id=TURN_ID) is None

    assert projector.read(THREAD_ID, TURN_ID) == finished


def test_a_stream_with_no_receipt_records_nothing() -> None:
    """A turn row without a projection stays without one.

    These paths also persist turns that predate the receipt; a missing
    projection is no reason to mint one here.
    """
    projector, _threads = _harness()

    assert (
        projector.stream_abandoned(thread_id=THREAD_ID, turn_id=TURN_ID) is None
    )

    assert projector.read(THREAD_ID, TURN_ID) is None


# ----- accepted inputs -----


def test_a_valid_reply_counts_once_and_records_its_own_version() -> None:
    projector, _threads = _harness()
    _begin(projector)

    counted = projector.accepted_reply(
        thread_id=THREAD_ID, turn_id=TURN_ID, input_version=7
    )

    assert counted is not None
    assert counted.accepted_input_count == 2
    assert counted.accepted_input_version == 7
    assert counted.execution_state == "running"
    assert counted.semantic_result is None


def test_a_repeated_reply_version_adds_nothing() -> None:
    projector, _threads = _harness()
    _begin(projector)
    counted = projector.accepted_reply(
        thread_id=THREAD_ID, turn_id=TURN_ID, input_version=7
    )
    assert counted is not None

    repeated = projector.accepted_reply(
        thread_id=THREAD_ID, turn_id=TURN_ID, input_version=7
    )

    assert repeated == counted
    assert repeated.accepted_input_count == 2


# ----- how a failure names itself -----


def test_a_typed_cause_keeps_its_exact_token() -> None:
    assert failure_token("task_history_failed") == "task_history_failed"
    assert failure_token("checkpoint_failed") == "checkpoint_failed"


def test_a_model_deadline_names_the_model_timeout() -> None:
    assert failure_token("timeout") == "model_timeout"


def test_a_transport_the_receipt_vocabulary_does_not_name_stays_out() -> None:
    assert failure_token("auth") is None
    assert failure_token("rate_limit") is None
    assert failure_token(None) is None
