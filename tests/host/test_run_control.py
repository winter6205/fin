"""Tests for the cooperative stop registry (T4, #138).

Spec Focus 3 / SC5: "Stop: a scripted in-flight **run** ends without a
new HumanMessage; composer accepts a later send; concurrent POST still
409 ``{error: concurrent_run}`` before SSE."

The registry is the seam between the ``POST /stop`` request thread and
the SSE generator that drives the run. It never touches the graph lock:
a stop that had to acquire the per-thread lock could not run while the
lock is held, which is the only moment a stop is meaningful.
"""

from __future__ import annotations

from fin.host.run_control import RunControl


def test_stop_on_an_idle_thread_reports_nothing_to_stop() -> None:
    control = RunControl()
    assert control.request_stop("t-1") is None


def test_a_registered_run_is_stoppable_and_the_token_flips() -> None:
    control = RunControl()
    token = control.begin("t-1", "run-1")
    assert token.is_set() is False

    stopped_run_id = control.request_stop("t-1")

    assert stopped_run_id == "run-1"
    assert token.is_set() is True


def test_a_stop_only_reaches_the_thread_it_names() -> None:
    control = RunControl()
    token_a = control.begin("t-a", "run-a")
    token_b = control.begin("t-b", "run-b")

    control.request_stop("t-a")

    assert token_a.is_set() is True
    assert token_b.is_set() is False


def test_a_finished_run_is_no_longer_stoppable() -> None:
    control = RunControl()
    token = control.begin("t-1", "run-1")
    control.end(token)

    assert control.request_stop("t-1") is None
    assert control.active_run_id("t-1") is None


def test_ending_a_superseded_token_does_not_deregister_the_new_run() -> None:
    """A late ``end`` from the previous run must not silently make the
    CURRENT run unstoppable — the operator would press Stop and nothing
    would happen."""
    control = RunControl()
    old = control.begin("t-1", "run-old")
    control.end(old)
    new = control.begin("t-1", "run-new")

    control.end(old)

    assert control.active_run_id("t-1") == "run-new"
    assert control.request_stop("t-1") == "run-new"
    assert new.is_set() is True
