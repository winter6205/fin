"""The queue-inclusive retrieval deadline (spec §"Result, error,
readiness, and degradation wire", final paragraph).

One monotonic deadline starts BEFORE waiting for the retrieve I/O
lock; lock wait, subprocess startup/handshake, and the response wait
all consume the same remaining budget:

* a WAITER timing out on the lock must not terminate the lock OWNER —
  it answers its own typed queue-stage failure and leaves the session
  usable;
* an OWNER that exhausts the budget quarantines its session (the
  terminal ``_failed`` flag + forced stop), so a late response can
  never satisfy a later request;
* cleanup (the forced stop) happens OUTSIDE the caller's remaining
  budget — the user-facing deadline is not extended by teardown;
* the stage the budget expired in is exactly one of ``queue`` (lock
  acquire), ``startup`` (``_ensure_started``), ``response``
  (``_read_response``) — and it names the stage the budget was SPENT
  in, not the one the call would have reached next.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from fin.retrieve.mcp_session import (
    MCPStdioDeadlineExceededError,
    MCPStdioSession,
    MCPStdioTransportError,
)

#: How long the fake child pretends to wait when the cleanup's wait is
#: UNCAPPED. Long enough for the bound to fail, short enough that the
#: suite does not hang.
_UNCAPPED_WAIT_SECONDS = 6.0

#: The cap the cleanup's own wait carries (the session's cap).
_CLEANUP_WAIT_CAP = 2.0

#: The budget of the cross-stage case, and the share of it the lock is
#: held for before the response wait begins.
_ONE_BUDGET_SECONDS = 0.25
_LOCK_HOLD_SECONDS = 0.15

#: The budget of the queue-fidelity case, and how long after it the
#: lock is granted.
_QUEUE_BUDGET_SECONDS = 0.02
_QUEUE_GRANT_AFTER_SECONDS = 0.04


class _FakeProcess:
    """A minimal stand-in for a LIVE child process the session manages.

    ``poll()`` returning ``None`` is "still running" — a return code
    would make the session read the child as exited before the call.

    ``wait`` behaves like ``Popen.wait``: a child that is already gone
    returns at once, a child that is still running consumes the whole
    timeout and raises ``TimeoutExpired``. Every wait is recorded, so a
    test can see the cap the cleanup used. A child built with
    ``survives_kill=True`` ignores the stop — the case the cap exists
    for.
    """

    def __init__(self, *, survives_kill: bool = False) -> None:
        self.killed = False
        self.terminated = False
        self.survives_kill = survives_kill
        self.wait_timeouts: list[float | None] = []
        self._reaped = False
        # A pipe-like stdin whose writes succeed silently.
        self.stdin = type("_Stdin", (), {"write": lambda self, b: len(b),
                                         "flush": lambda self: None})()

    def poll(self) -> int | None:
        return 0 if self._reaped else None

    def kill(self) -> None:
        self.killed = True
        if not self.survives_kill:
            self._reaped = True

    def terminate(self) -> None:
        self.terminated = True
        if not self.survives_kill:
            self._reaped = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._reaped:
            return 0
        if timeout is None:
            time.sleep(_UNCAPPED_WAIT_SECONDS)
            raise subprocess.TimeoutExpired("fake-mcp-child", _UNCAPPED_WAIT_SECONDS)
        time.sleep(timeout)
        raise subprocess.TimeoutExpired("fake-mcp-child", timeout)


class _LateGrantLock:
    """An I/O lock that is granted only after the caller's budget ran out.

    The real lock is granted when its owner releases it. Here the grant
    is deliberately late, so the budget is spent WAITING for the lock —
    the ``queue`` stage — and the stage the call reports must name it.
    """

    def __init__(self, grant_after: float) -> None:
        self._grant_after = grant_after
        self._origin: float | None = None
        self._granted = False

    def acquire(self, timeout: float | None = None, blocking: bool = True) -> bool:
        now = time.monotonic()
        if self._origin is None:
            self._origin = now
        waited = now - self._origin
        if self._granted:
            return True
        if waited >= self._grant_after:
            self._granted = True
            return True
        if not blocking:
            return False
        remaining = self._grant_after - waited
        if timeout is not None and timeout < remaining:
            time.sleep(timeout)
            return False
        time.sleep(remaining)
        self._granted = True
        return True

    def release(self) -> None:
        self._granted = False


def _spent_session() -> tuple[MCPStdioSession, _FakeProcess]:
    session = MCPStdioSession.__new__(MCPStdioSession)
    session._command = ("unused",)
    session._cwd = None
    session._timeout = 60.0
    session._explicit_env = None
    import queue

    session._responses = queue.Queue()
    session._io_lock = threading.RLock()
    session._closed = False
    session._failed = False
    session._stderr = bytearray()
    session._stderr_lock = threading.Lock()
    session._requests = []
    # A fake stdout reader thread: never answers, never EOFs — the
    # timed waits expire instead of reading a sentinel.
    session._process = None
    return session, _FakeProcess()


def test_waiter_timing_out_on_a_held_lock_does_not_kill_the_owner() -> None:
    """A waiter that exhausts its budget while ANOTHER thread holds the
    lock answers its own failure; the owner's session stays alive and
    its in-flight call completes."""
    session, process = _spent_session()
    session._process = process
    started = threading.Event()
    release = threading.Event()
    owner_response: dict[str, object] = {}

    def _owner() -> None:
        with session._io_lock:
            started.set()
            release.wait(5.0)
            owner_response["value"] = {"jsonrpc": "2.0", "id": 2, "result": {}}

    owner = threading.Thread(target=_owner)
    owner.start()
    try:
        assert started.wait(5.0)
        with pytest.raises(MCPStdioDeadlineExceededError) as excinfo:
            session.call({"jsonrpc": "2.0", "id": 9, "method": "x"}, 0.2)
        assert excinfo.value.stage == "queue"
        # The waiter did NOT terminate the owner: the session is not
        # quarantined and the owner's child was not killed. The
        # ``is_spent`` probe cannot say this while the owner still
        # holds the lock — it answers False without looking.
        assert session._failed is False
        assert process.killed is False
    finally:
        release.set()
        owner.join(5.0)
    assert owner.is_alive() is False
    # The owner's own call completed normally.
    assert owner_response["value"] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    # Only now, with no owner holding the lock, does the session's own
    # state answer for itself.
    assert session.is_spent is False


def test_owner_budget_exhaustion_in_the_response_stage_quarantines() -> None:
    """The lock OWNER that waits past its budget for the response
    quarantines the session: terminal ``_failed``, forced stop, and a
    later request is refused by name rather than satisfied by the late
    response."""
    session, process = _spent_session()
    fake = _FakeProcess()
    session._process = fake

    start = time.monotonic()
    with pytest.raises(MCPStdioDeadlineExceededError) as excinfo:
        session.call({"jsonrpc": "2.0", "id": 2, "method": "x"}, 0.2)
    elapsed = time.monotonic() - start

    assert excinfo.value.stage == "response"
    assert session.is_spent is True
    assert session._failed is True
    assert fake.killed is True
    # The forced stop must not extend the caller's budget: the teardown
    # stops the child at once and its own wait is capped, so the raise
    # arrives promptly. A teardown that left the child running would
    # wait out the cap twice and hold the caller for four seconds.
    assert elapsed < 2.0
    assert fake.wait_timeouts == [_CLEANUP_WAIT_CAP]
    # The late response the owner abandoned can never satisfy a later
    # request: the session is terminal, so the next call is refused by
    # name — the quarantine's own "has failed" transport error.
    with pytest.raises(MCPStdioTransportError):
        session.call({"jsonrpc": "2.0", "id": 3, "method": "x"}, 0.2)


def test_stage_startup_is_reported_for_a_cold_failed_start() -> None:
    """A session that cannot START its child within the budget reports
    the ``startup`` stage — the lock was won, the handshake was not.

    ``_ensure_started`` runs the real Popen path here: the command is a
    live process that prints nothing (``sleep``), so the initialize
    response never arrives and the STARTUP share of the budget
    expires."""
    session, _process = _spent_session()
    session._command = ("sleep", "30")
    session._process = None

    with pytest.raises(MCPStdioDeadlineExceededError) as excinfo:
        session.call({"jsonrpc": "2.0", "id": 2, "method": "x"}, 0.2)
    assert excinfo.value.stage == "startup"
    assert session.is_spent is True


def test_cleanup_cannot_extend_the_caller_budget() -> None:
    """The response-stage timeout raises after roughly the budget, not
    after budget-plus-cleanup: teardown runs its bounded wait outside
    the deadline clock."""
    session, _process = _spent_session()
    fake = _FakeProcess()
    session._process = fake

    start = time.monotonic()
    with pytest.raises(MCPStdioDeadlineExceededError):
        session.call({"jsonrpc": "2.0", "id": 2, "method": "x"}, 0.3)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    # The teardown stopped the child and waited for it ONCE, with the
    # cap: a teardown that left the child running would wait the cap
    # out twice and hold the caller for four seconds.
    assert fake.killed is True
    assert fake.wait_timeouts == [_CLEANUP_WAIT_CAP]


def test_the_cleanup_wait_stays_capped_when_the_child_will_not_die() -> None:
    """A child that ignores the stop makes the teardown wait its cap
    out — twice, because the first wait times out and the fallback
    waits again. BOTH waits are capped: an uncapped wait on a child
    that will not die is the open-ended hold the cap exists to prevent,
    so the caller is held for a bounded time and no more.
    """
    session, _process = _spent_session()
    fake = _FakeProcess(survives_kill=True)
    session._process = fake

    start = time.monotonic()
    with pytest.raises(MCPStdioDeadlineExceededError):
        session.call({"jsonrpc": "2.0", "id": 2, "method": "x"}, 0.3)
    elapsed = time.monotonic() - start
    # Every wait the teardown made carried the cap.
    assert fake.wait_timeouts == [_CLEANUP_WAIT_CAP, _CLEANUP_WAIT_CAP]
    # ... and the caller was held for those two capped waits at most.
    assert elapsed < 0.3 + 2 * _CLEANUP_WAIT_CAP + 0.5


def test_one_budget_covers_the_lock_wait_and_the_response_wait() -> None:
    """ONE budget for the whole call: the time spent waiting for the
    lock comes out of the SAME budget as the response wait.

    The lock is held for 0.15 s of a 0.25 s budget, so the response
    stage expires at ~0.25 s. A per-stage budget would give the
    response stage a FRESH 0.25 s and the call would end at ~0.40 s.
    """
    session, process = _spent_session()
    session._process = process
    held = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with session._io_lock:
            held.set()
            release.wait(5.0)

    holder = threading.Thread(target=_holder)
    holder.start()
    try:
        assert held.wait(5.0)
        start = time.monotonic()
        threading.Timer(_LOCK_HOLD_SECONDS, release.set).start()
        with pytest.raises(MCPStdioDeadlineExceededError) as excinfo:
            session.call(
                {"jsonrpc": "2.0", "id": 2, "method": "x"}, _ONE_BUDGET_SECONDS
            )
        elapsed = time.monotonic() - start
    finally:
        release.set()
        holder.join(5.0)
    assert excinfo.value.stage == "response"
    # The lock wait was paid out of the SAME budget: the call expired
    # at the budget, not at the lock wait plus a fresh budget.
    assert elapsed < _ONE_BUDGET_SECONDS + 0.1


def test_a_budget_spent_queueing_reports_the_queue_stage() -> None:
    """The stage names where the budget was SPENT. A lock granted
    after the deadline was spent waiting for it, so the call reports
    ``queue`` — not the ``startup`` it never reached."""
    session, _process = _spent_session()
    session._io_lock = _LateGrantLock(_QUEUE_GRANT_AFTER_SECONDS)

    with pytest.raises(MCPStdioDeadlineExceededError) as excinfo:
        session.call(
            {"jsonrpc": "2.0", "id": 9, "method": "x"}, _QUEUE_BUDGET_SECONDS
        )
    assert excinfo.value.stage == "queue"
