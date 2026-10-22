"""The per-stage execution budgets a run is bounded by.

The values are the server-runtime spec's budget table, not measured
runtime results, and the table's own rule is that a deployment may tighten
a budget but never raise one. Both halves are pinned here: a budget that
disables the bound must fail at construction rather than silently run a
run forever, and a budget above the spec maximum must be refused rather
than quietly widening the contract.
"""

from __future__ import annotations

import math
import time

import pytest

from fin.host.run_budgets import (
    SPEC_CLEANUP_GRACE_MAX_S,
    SPEC_EXECUTE_STAGE_MAX_S,
    STAGE_CLEANUP,
    STAGE_EXECUTE,
    RunBudgetConfigError,
    RunBudgets,
    StageLedger,
)


def test_the_defaults_are_the_spec_table_values() -> None:
    budgets = RunBudgets()

    # 120 seconds per model invocation and the one-second cancellation
    # cleanup grace, as spelled in the spec's budget table.
    assert budgets.execute_stage_s == SPEC_EXECUTE_STAGE_MAX_S == 120.0
    assert budgets.cleanup_grace_s == SPEC_CLEANUP_GRACE_MAX_S == 1.0


def test_a_tightened_budget_is_admitted() -> None:
    budgets = RunBudgets(execute_stage_s=30.0, cleanup_grace_s=0.25)

    assert budgets.execute_stage_s == 30.0
    assert budgets.cleanup_grace_s == 0.25


@pytest.mark.parametrize(
    "kwargs",
    [
        {"execute_stage_s": 0.0},
        {"execute_stage_s": -1.0},
        {"execute_stage_s": math.inf},
        {"execute_stage_s": math.nan},
        {"execute_stage_s": True},
        {"execute_stage_s": "120"},
        {"execute_stage_s": None},
        {"cleanup_grace_s": 0.0},
        {"cleanup_grace_s": -0.5},
        {"cleanup_grace_s": math.inf},
        {"cleanup_grace_s": True},
    ],
)
def test_an_unusable_budget_fails_closed_at_construction(kwargs: dict) -> None:
    """A budget that cannot bound anything is a startup failure.

    Zero, negative, non-finite and non-numeric values would each leave the
    run unbounded while looking configured, which is the one outcome a
    budget must never produce.
    """
    with pytest.raises(RunBudgetConfigError):
        RunBudgets(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"execute_stage_s": SPEC_EXECUTE_STAGE_MAX_S + 1},
        {"execute_stage_s": 10_000.0},
        {"cleanup_grace_s": SPEC_CLEANUP_GRACE_MAX_S + 0.001},
    ],
)
def test_a_budget_above_the_spec_maximum_is_refused(kwargs: dict) -> None:
    """The spec lets a deployment tighten a budget and never raise one, so
    a larger bound is a config error rather than a wider contract."""
    with pytest.raises(RunBudgetConfigError):
        RunBudgets(**kwargs)


def test_a_ledger_reports_a_stage_that_stayed_inside_its_budget() -> None:
    ledger = StageLedger(RunBudgets(execute_stage_s=60.0))
    ledger.begin_execute()
    ledger.observe_progress()
    ledger.finish_execute()

    outcomes = {outcome.stage: outcome for outcome in ledger.outcomes()}

    assert STAGE_EXECUTE in outcomes
    execute = outcomes[STAGE_EXECUTE]
    assert execute.budget_ms == 60_000
    assert execute.exceeded is False
    assert execute.complete is True


def test_a_ledger_reports_the_stage_that_passed_its_budget() -> None:
    ledger = StageLedger(RunBudgets(execute_stage_s=0.01))
    ledger.begin_execute()
    time.sleep(0.03)
    assert ledger.execute_without_progress_s() >= 0.01
    assert ledger.execute_exceeded() is True
    ledger.finish_execute(interrupted=True)

    execute = ledger.outcomes()[0]

    assert execute.stage == STAGE_EXECUTE
    assert execute.exceeded is True
    # An interrupted stage's elapsed time is a lower bound on the work the
    # graph actually did, never a total.
    assert execute.complete is False


def test_the_cleanup_grace_marks_surviving_work_incomplete() -> None:
    ledger = StageLedger(RunBudgets(cleanup_grace_s=0.01))
    ledger.begin_execute()
    ledger.finish_execute()
    ledger.record_cleanup(elapsed_s=5.0)

    cleanup = next(
        outcome for outcome in ledger.outcomes() if outcome.stage == STAGE_CLEANUP
    )

    assert cleanup.budget_ms == 10
    assert cleanup.elapsed_ms >= 5_000
    assert cleanup.exceeded is True
    # Work that outlived the grace is still supervised, so the stage has no
    # observed end to report.
    assert cleanup.complete is False


def test_a_cleanup_inside_its_grace_is_complete() -> None:
    ledger = StageLedger(RunBudgets())
    ledger.begin_execute()
    ledger.finish_execute()
    ledger.record_cleanup(elapsed_s=0.0)

    cleanup = next(
        outcome for outcome in ledger.outcomes() if outcome.stage == STAGE_CLEANUP
    )

    assert cleanup.complete is True
    assert cleanup.exceeded is False
