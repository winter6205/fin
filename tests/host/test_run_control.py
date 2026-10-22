"""Tests for the cooperative stop registry (T4).

Spec Focus 3 / SC5: "Stop: a scripted in-flight **run** ends without a
new HumanMessage; composer accepts a later send; concurrent POST still
409 ``{error: concurrent_run}`` before SSE."

The registry is the seam between the ``POST /stop`` request thread and
the SSE generator that drives the run. It never touches the graph lock:
a stop that had to acquire the per-thread lock could not run while the
lock is held, which is the only moment a stop is meaningful.
"""

from __future__ import annotations

import threading

import pytest

from fin.host.run_control import RunAdmissionError, RunControl


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


def test_a_disconnect_stop_is_fenced_to_the_run_the_response_owns() -> None:
    control = RunControl()
    old = control.begin("t-1", "run-old")
    control.end(old)
    current = control.begin("t-1", "run-current")

    assert (
        control.request_stop(
            "t-1", expected_run_id="run-old", reason="client_disconnect"
        )
        is None
    )
    assert current.is_set() is False
    assert control.request_stop(
        "t-1", expected_run_id="run-current", reason="client_disconnect"
    ) == "run-current"
    assert current.is_set() is True
    assert current.reason == "client_disconnect"


def test_a_stop_during_send_admission_reaches_the_run_when_it_begins() -> None:
    control = RunControl()
    admission = control.admit("t-1", stop_pending=True)

    stop = control.stop_current("t-1", reason="user_stop")

    assert stop is not None
    assert stop.pending is True
    assert stop.run_id is None
    assert control.active_run_id("t-1") is None

    token = control.begin("t-1", "run-1", admission=admission)

    assert token.is_set() is True
    assert token.reason == "user_stop"
    admission.release()
    control.end(token)


def test_a_failed_send_preflight_discards_its_pending_stop() -> None:
    control = RunControl()
    admission = control.admit("t-1", stop_pending=True)

    assert control.stop_current("t-1", reason="user_stop").pending is True

    # This is the same release taken by a preflight refusal before a run or
    # receipt exists. The next claim must start with a fresh stop state.
    admission.release()
    assert control.stop_current("t-1") is None

    later_admission = control.admit("t-1", stop_pending=True)
    later = control.begin("t-1", "run-later", admission=later_admission)
    assert later.is_set() is False
    assert later.reason is None
    control.end(later)
    later_admission.release()


def test_a_delayed_disconnect_does_not_cancel_a_new_pending_admission() -> None:
    control = RunControl()
    old = control.begin("t-1", "run-old")
    old_run_id = control.active_run_id("t-1")
    control.end(old)

    admission = control.admit("t-1", stop_pending=True)
    assert (
        control.request_stop(
            "t-1", expected_run_id=old_run_id, reason="client_disconnect"
        )
        is None
    )
    current = control.begin("t-1", "run-current", admission=admission)

    assert current.is_set() is False
    admission.release()
    control.end(current)


def test_first_stop_reason_remains_the_run_outcome() -> None:
    control = RunControl()
    token = control.begin("t-1", "run-1")

    control.request_stop("t-1", reason="user_stop")
    control.request_stop("t-1", reason="client_disconnect")

    assert token.reason == "user_stop"


def test_a_shutdown_stops_every_live_run_and_names_why() -> None:
    """Shutdown signals every run without waiting on any of them.

    The sweep must reach the auto-resumer's runs and the operator's, and
    must not wait for a run to finish: a server that blocks here would
    hang on exactly the run it is trying to end.
    """
    control = RunControl()
    operator = control.begin("t-1", "run-1")
    background = control.begin("t-2", "run-2")

    assert set(control.stop_all()) == {"run-1", "run-2"}
    assert operator.is_set() is True
    assert background.is_set() is True
    assert operator.reason == "application_shutdown"
    assert background.reason == "application_shutdown"


def test_a_shutdown_keeps_the_reason_the_run_already_was_stopped_for() -> None:
    """First stop reason remains the run outcome, shutdown or not."""
    control = RunControl()
    token = control.begin("t-1", "run-1")

    control.request_stop("t-1", reason="user_stop")
    control.stop_all()

    assert token.reason == "user_stop"


def test_a_shutdown_over_an_idle_registry_stops_nothing() -> None:
    control = RunControl()

    assert control.stop_all() == ()


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


def test_a_finished_claim_stops_occupying_the_registry() -> None:
    """A claim nobody holds leaves nothing behind in the registry.

    The registry is per application and long-lived, so a lock per thread
    EVER admitted would grow without bound for as long as the process
    runs — every thread the operator ever opened, kept alive by a
    dictionary. Holding the lock weakly retires it the moment the last
    claim referencing it is gone.
    """
    control = RunControl()

    for index in range(50):
        control.admit(f"t-{index}").release()

    assert len(control._admissions) == 0


def test_a_live_claim_keeps_the_registry_entry_its_next_caller_meets() -> None:
    """The lock stays the SAME lock while any claim still holds it.

    The registry must not hand a second caller a fresh lock while the
    first still owns the thread: both would acquire their own and both
    would drive the graph. Weak retirement is only safe because it waits
    for the last reference.
    """
    control = RunControl()

    first = control.admit("t-1")

    with pytest.raises(RunAdmissionError):
        control.admit("t-1")

    first.release()
    second = control.admit("t-1")
    assert second.thread_id == "t-1"
    second.release()


def test_two_concurrent_finalizers_release_one_claim_exactly_once() -> None:
    """Concurrent releases neither raise nor double-unlock.

    The run generator and the response both release the same claim, and a
    transport failure can land them on top of each other. Check-then-act
    without a guard let both see an unreleased claim, and the second
    unlocked a lock the first had already freed.
    """
    control = RunControl()
    claim = control.admit("t-1")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def release() -> None:
        barrier.wait(5.0)
        try:
            claim.release()
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=release) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert errors == []
    # The claim is genuinely free, not merely marked released.
    assert control.admit("t-1").thread_id == "t-1"
