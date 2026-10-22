"""Bounded durable writes on the checkpointer connection.

Spec ``specs/qa-chain-server-runtime.md`` fixes two hard budgets that the
checkpointer path did not implement:

* **Database acquisition: 2 seconds per acquisition** (connection AND lock
  acquisition are bounded);
* **Persistence phase: 5 seconds per phase** (bounded statements and commit;
  each transaction phase is subject to one deadline).

Without them a run blocked on a lock held against the checkpoint tables
waits forever: the measurement behind this module showed a client
disconnect and an explicit ``POST /threads/{id}/stop`` both failing to free
the thread until an operator released the lock by hand.

The bound is split by acquisition kind (see ``compile.py``):

* ``connect_timeout`` rides in the conninfo — libpq bounds session
  establishment itself;
* ``statement_timeout`` / ``lock_timeout`` are SET on the open session
  AFTER ``setup()``, so the CIC migrations are never bounded (a timeout
  mid-``CREATE INDEX CONCURRENTLY`` leaves an INVALID index no retry
  repairs).

Layers of coverage:

* pure conninfo tests (no database) prove the acquisition bound reaches
  the URL the saver opens;
* pure binding tests (no database) prove the session bounds reach the
  saver's live connection;
* a live test proves the bound actually ends a blocked durable write, that
  it ends inside the phase budget rather than hanging, and that the
  connection stays usable afterwards so the failure is one run's, not the
  process's.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
import uuid
from concurrent.futures import wait as wait_for_futures
from typing import Any

import pytest
from psycopg import conninfo

from fin.host import compile as compile_module
from fin.host.compile import (
    CHECKPOINT_CONNECT_TIMEOUT_SECONDS,
    CHECKPOINT_LOCK_TIMEOUT_MS,
    CHECKPOINT_STATEMENT_TIMEOUT_MS,
)

POSTGRES_URL_ENV = "FIN_TEST_DSN"


# ----- pure: the acquisition bound reaches the URL the saver opens -----


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://fin:fin@localhost:5433/fin?sslmode=disable",
        "host=localhost port=5433 dbname=fin",
    ],
)
def test_bounded_conninfo_keeps_the_addressed_library(url: str) -> None:
    """Rewriting the conninfo must not move the connection.

    A checkpoint library is identified by ``dbname``; a rewrite that dropped
    or renamed it would silently point durable state at another database —
    the exact loss ADR 0015's explicit-URL rule exists to prevent.
    """
    bounded = compile_module.bounded_checkpoint_conninfo(url)
    assert conninfo.conninfo_to_dict(bounded)["dbname"] == "fin"


def test_bounded_conninfo_adds_the_acquisition_bound() -> None:
    """The conninfo carries libpq's own session-establishment bound.

    Session GUCs are deliberately NOT in the conninfo: they would bound
    ``setup()``'s ``CREATE INDEX CONCURRENTLY`` migrations too, and a
    timeout there leaves an INVALID index no retry repairs.
    """
    bounded = compile_module.bounded_checkpoint_conninfo(
        "postgresql://fin:fin@localhost:5433/fin?sslmode=disable"
    )
    settings = conninfo.conninfo_to_dict(bounded)

    assert settings["connect_timeout"] == str(CHECKPOINT_CONNECT_TIMEOUT_SECONDS)
    assert "options" not in settings


def test_bounded_conninfo_is_idempotent() -> None:
    """Rewriting an already-bounded URL is a no-op, not a doubled bound.

    Compile is re-run on hot paths (and by tests); appending a second
    ``connect_timeout`` every time would grow the string without end.
    """
    once = compile_module.bounded_checkpoint_conninfo(
        "postgresql://fin:fin@localhost:5433/fin?sslmode=disable"
    )
    twice = compile_module.bounded_checkpoint_conninfo(once)
    assert conninfo.conninfo_to_dict(twice) == conninfo.conninfo_to_dict(once)


def test_bounded_conninfo_preserves_operator_supplied_parameters() -> None:
    """An operator's own conninfo parameters ride along.

    Silently dropping a caller-supplied parameter (here the GUC ``options``
    the session bounds below are designed to respect) would change
    behaviour the operator asked for, which the "do not change the happy
    path" constraint forbids.
    """
    bounded = compile_module.bounded_checkpoint_conninfo(
        "postgresql://fin:fin@localhost:5433/fin?options=-c%20search_path%3Dmyschema"
    )
    settings = conninfo.conninfo_to_dict(bounded)
    assert settings["connect_timeout"] == str(CHECKPOINT_CONNECT_TIMEOUT_SECONDS)
    assert "search_path=myschema" in settings["options"]


def test_unparseable_url_is_reported_verbatim_not_rewritten() -> None:
    """A URL libpq cannot parse is left alone.

    The rewrite is a bound, not a validator: boot must still fail where it
    always failed (libpq's own error), not in a new helper with a new
    message an operator has never seen.
    """
    url = "this is not a conninfo"
    with pytest.raises(Exception):
        conninfo.conninfo_to_dict(url)
    assert compile_module.bounded_checkpoint_conninfo(url) == url


# ----- pure: the session bounds respect the operator and reach the saver -----


def test_session_bounds_default_to_both_gucs() -> None:
    """Without operator options, both spec budgets are handed to the session."""
    bounds = dict(
        compile_module.checkpoint_session_bounds(
            "postgresql://fin:fin@localhost:5433/fin"
        )
    )
    assert bounds == {
        "statement_timeout": CHECKPOINT_STATEMENT_TIMEOUT_MS,
        "lock_timeout": CHECKPOINT_LOCK_TIMEOUT_MS,
    }


def test_session_bounds_leave_an_operator_tighter_guc_alone() -> None:
    """An operator who already tightened a bound keeps their tighter one.

    Spec line 117: explicit settings "may tighten them but may not raise
    them". Re-SETting the larger default would RAISE a bound.
    """
    bounds = dict(
        compile_module.checkpoint_session_bounds(
            "postgresql://fin:fin@localhost:5433/fin"
            "?options=-c%20statement_timeout%3D500%20-c%20lock_timeout%3D100"
        )
    )
    assert bounds == {}


def test_session_bounds_fill_only_the_guc_the_operator_left_open() -> None:
    """A tighter ``statement_timeout`` does not waive our ``lock_timeout``."""
    bounds = dict(
        compile_module.checkpoint_session_bounds(
            "postgresql://fin:fin@localhost:5433/fin"
            "?options=-c%20statement_timeout%3D500"
        )
    )
    assert bounds == {"lock_timeout": CHECKPOINT_LOCK_TIMEOUT_MS}


def test_session_bounds_ignore_a_looser_operator_guc() -> None:
    """An operator value LOOSER than ours is not "tighter" — ours applies.

    The may-not-raise rule cuts the other way too: a 60s operator timeout
    must not defeat the spec's 2s acquisition budget. Our ``lock_timeout``
    stays in the bounds and, being SET after connect, overrides the
    operator's session default for this connection.
    """
    bounds = dict(
        compile_module.checkpoint_session_bounds(
            "postgresql://fin:fin@localhost:5433/fin"
            "?options=-c%20lock_timeout%3D60000"
        )
    )
    assert bounds == {
        "statement_timeout": CHECKPOINT_STATEMENT_TIMEOUT_MS,
        "lock_timeout": CHECKPOINT_LOCK_TIMEOUT_MS,
    }


def test_bounded_conninfo_keeps_an_operator_tighter_connect_timeout() -> None:
    """A tighter operator ``connect_timeout`` survives; a looser one does not.

    Same may-not-raise rule as the session GUCs: a 1s operator bound is
    kept verbatim, while a 60s one would RAISE the acquisition budget and
    is replaced with the spec's 2s.
    """
    tighter = compile_module.bounded_checkpoint_conninfo(
        "postgresql://fin:fin@localhost:5433/fin?connect_timeout=1"
    )
    assert conninfo.conninfo_to_dict(tighter)["connect_timeout"] == "1"

    looser = compile_module.bounded_checkpoint_conninfo(
        "postgresql://fin:fin@localhost:5433/fin?connect_timeout=60"
    )
    assert conninfo.conninfo_to_dict(looser)["connect_timeout"] == str(
        CHECKPOINT_CONNECT_TIMEOUT_SECONDS
    )


def test_session_bounds_for_an_unparseable_url_are_empty() -> None:
    """No parse, no bounds — the connection never opens to receive them."""
    url = "this is not a conninfo"
    with pytest.raises(Exception):
        conninfo.conninfo_to_dict(url)
    assert compile_module.checkpoint_session_bounds(url) == ()


def test_run_call_context_registry_is_thread_scoped_and_clears_on_exit() -> None:
    """Checkpoint callback context stays isolated and is removed after a run."""
    from fin.host.run_call_supervisor import (
        RunCallContextRegistry,
        current_run_call_context,
        run_call_context,
    )
    from fin.host.run_control import StopToken

    registry = RunCallContextRegistry()
    thread_ids = ("registry-thread-a", "registry-thread-b")
    tokens = {
        thread_id: StopToken(thread_id, f"run-{uuid.uuid4()}")
        for thread_id in thread_ids
    }
    ready = threading.Barrier(3)
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_binding(thread_id: str) -> None:
        try:
            with run_call_context(tokens[thread_id], None):
                with registry.bind_thread(thread_id):
                    ready.wait(timeout=5)
                    if not release.wait(timeout=5):
                        raise TimeoutError("registry binding was not released")
        except BaseException as exc:
            errors.append(exc)

    binders = [
        threading.Thread(target=hold_binding, args=(thread_id,))
        for thread_id in thread_ids
    ]
    for binder in binders:
        binder.start()

    try:
        ready.wait(timeout=5)
        restored: dict[str, Any] = {}
        for thread_id in thread_ids:
            with registry.activate_thread(thread_id):
                context = current_run_call_context()
                restored[thread_id] = context.stop_token if context else None
            assert current_run_call_context() is None
    finally:
        release.set()
        for binder in binders:
            binder.join(timeout=5)

    assert not errors
    assert all(not binder.is_alive() for binder in binders)
    assert restored == tokens

    for thread_id in thread_ids:
        with registry.activate_thread(thread_id):
            assert current_run_call_context() is None


class _RecordingSession:
    """Stands in for the saver's psycopg connection and records statements."""

    statements: list[str] = []

    def execute(self, query: str | bytes, params: Any = None) -> Any:
        _RecordingSession.statements.append(query if isinstance(query, str) else query.decode())
        return None


class _RecordingSaver:
    """Stands in for ``PostgresSaver`` with a bindable ``conn`` and a URL."""

    url = ""
    conn: _RecordingSession | None = None
    lock: Any

    def setup(self) -> None:
        return None

    @classmethod
    def from_conn_string(
        cls, url: str, *, pipeline: bool = False
    ) -> "_RecordingCM":
        _RecordingSaver.url = url
        return _RecordingCM()


class _RecordingCM:
    def __enter__(self) -> _RecordingSaver:
        saver = _RecordingSaver()
        saver.conn = _RecordingSession()
        saver.lock = threading.Lock()
        return saver

    def __exit__(self, *exc: object) -> None:
        return None


def test_binding_sets_both_bounds_on_the_saver_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, not just the helpers: the OPENED saver session is bound.

    A helper that computes bounds nobody SETs would leave the production
    path unbounded while this file went green. The compile must hand the
    saver the bounded conninfo AND SET the session GUCs after ``setup()``.
    """
    pg_mod = types.ModuleType("langgraph.checkpoint.postgres")
    pg_mod.PostgresSaver = _RecordingSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", pg_mod)
    monkeypatch.setattr(compile_module, "_build_default_model", lambda **_kw: object())
    monkeypatch.setattr(compile_module, "build_jev_client", lambda **_kw: None)
    monkeypatch.setattr(compile_module, "_resolve_retrieve_adapter", lambda _env: "fake")
    compiled_savers: list[Any] = []

    def _compile_graph(**kwargs: Any) -> object:
        compiled_savers.append(kwargs["checkpointer"])
        return object()

    monkeypatch.setattr(compile_module, "compile_graph", _compile_graph)

    _RecordingSession.statements.clear()
    runtime = compile_module.compile_runtime_handle(
        checkpointer_url="postgresql://fin:fin@localhost:5433/fin?sslmode=disable"
    )
    runtime.close()

    settings = conninfo.conninfo_to_dict(_RecordingSaver.url)
    assert settings["connect_timeout"] == str(CHECKPOINT_CONNECT_TIMEOUT_SECONDS)
    assert isinstance(compiled_savers[0].lock, compile_module._RunAwareCheckpointLock)
    assert sorted(_RecordingSession.statements) == [
        "SET lock_timeout = 2000",
        "SET statement_timeout = 5000",
    ]


def test_binding_a_fake_saver_without_a_session_is_skipped() -> None:
    """Fakes without a connection (existing compile-test fakes) bind nothing.

    ``bind_checkpoint_session_bounds`` must not invent a connection to
    satisfy the bind; it must be a no-op there.
    """

    class _Bare:
        pass

    compile_module.bind_checkpoint_session_bounds(_Bare(), "postgresql://localhost/fin")
    compile_module.bind_checkpoint_session_bounds(_Bare(), "not a conninfo")


# ----- live: a blocked durable write ends inside the bound -----


def _test_dsn() -> str | None:
    return (os.environ.get(POSTGRES_URL_ENV) or "").strip() or None


def _block_checkpoint_tables(dsn: str) -> Any:
    """Take an exclusive lock on the checkpoint tables from a second connection.

    This is the measured condition: a run blocked in its durable write while
    something else holds those tables. Returns an open blocker connection;
    the caller closes it (releasing the lock) inside its own ``finally``.
    """
    import psycopg

    blocker = psycopg.connect(dsn)
    blocker.execute(
        "LOCK TABLE checkpoints, checkpoint_blobs, checkpoint_writes "
        "IN ACCESS EXCLUSIVE MODE"
    )
    return blocker


def _put(saver: Any, thread_id: str, checkpoint_id: str) -> None:
    saver.put(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        {
            "v": 4,
            "ts": "2026-10-19T00:00:00+00:00",
            "id": checkpoint_id,
            "channel_values": {"messages": ["blocked"]},
        },
        {"source": "input", "step": 1},
        {},
    )


def test_blocked_checkpoint_write_fails_within_the_persistence_bound() -> None:
    """A locked-out durable write fails FAST — and loudly, never silently.

    Proves the three properties the defect's fix depends on:

    1. it ENDS (the pre-fix behaviour was an unbounded wait);
    2. it ends inside the persistence-phase budget, not at some much
       larger value that would leave the thread just as stuck;
    3. it RAISES rather than returning, so a lost checkpoint can never be
       mistaken for a successful one.

    Skips (reported, never counted as a pass) when ``FIN_TEST_DSN`` is
    unset — see ``.claude/rules/test-storage.md``.
    """
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip(
            "FIN_TEST_DSN is not set; the blocked-write bound was not measured"
        )

    import psycopg

    from langgraph.checkpoint.postgres import PostgresSaver

    with psycopg.connect(dsn) as identity:
        assert identity.execute("SELECT current_database()").fetchone()[0].endswith(
            "_test"
        )

    bounded = compile_module.bounded_checkpoint_conninfo(dsn)
    with PostgresSaver.from_conn_string(bounded) as saver:
        saver.setup()
        compile_module.bind_checkpoint_session_bounds(saver, dsn)
        blocker = _block_checkpoint_tables(dsn)
        try:
            started = time.monotonic()
            with pytest.raises(psycopg.OperationalError) as raised:
                _put(saver, "bounded-thread", "11111111-1111-1111-1111-111111111111")
            elapsed = time.monotonic() - started
        finally:
            blocker.rollback()
            blocker.close()

        assert isinstance(raised.value, psycopg.errors.LockNotAvailable), (
            "a lock-blocked durable write must fail as a lock timeout, "
            f"not as some unrelated driver error: {raised.value!r}"
        )
        assert "lock timeout" in str(raised.value), (
            "the cancellation must come from lock_timeout, not from an "
            "unrelated failure"
        )
        # Server-side the wait is exactly the bound; client elapsed adds the
        # round trip and the saver's multi-statement pipeline, so the
        # ceiling asserted here is the SPEC's persistence-phase budget,
        # not the bare lock value (measured overhead ~5ms).
        assert elapsed < CHECKPOINT_STATEMENT_TIMEOUT_MS / 1000, (
            "the blocked write must end inside the persistence-phase bound"
        )
        # The connection survives the cancelled statement: one blocked run is
        # one run's failure, and the server keeps serving every other thread.
        started = time.monotonic()
        _put(saver, "bounded-thread", "22222222-2222-2222-2222-222222222222")
        assert time.monotonic() - started < 5
        assert list(saver.list({"configurable": {"thread_id": "bounded-thread"}}))


def test_unbounded_checkpoint_connection_waits_indefinitely() -> None:
    """The control: WITHOUT the bound, the same lock makes ``put`` hang.

    Proves the bound — not luck, not the machine being idle — is what ends
    the wait. Runs the unbounded write on a daemon thread and fails if it
    is still blocked after the bound would have expired; the thread is left
    blocked on purpose and dies with the process.
    """
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip(
            "FIN_TEST_DSN is not set; the unbounded control was not measured"
        )

    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
        blocker = _block_checkpoint_tables(dsn)
        finished = threading.Event()

        def _run() -> None:
            try:
                _put(saver, "control-thread", "33333333-3333-3333-3333-333333333333")
            except BaseException:
                pass
            finally:
                finished.set()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            assert not finished.wait(
                CHECKPOINT_LOCK_TIMEOUT_MS / 1000
            ), (
                "an unbounded connection returned inside the bound, so this "
                "run cannot prove the bound is what ends the wait"
            )
        finally:
            blocker.rollback()
            blocker.close()


class _ObservedLock:
    """Thread lock that records which saver workers have queued for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition()
        self.attempted_by: set[int] = set()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._condition:
            self.attempted_by.add(threading.get_ident())
            self._condition.notify_all()
        if not blocking:
            return self._lock.acquire(False)
        if timeout < 0:
            return self._lock.acquire()
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> _ObservedLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.release()
        return False

    def wait_for_attempts(self, thread_ids: set[int], timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: thread_ids <= self.attempted_by,
                timeout=timeout,
            )


def test_cancelled_run_drops_checkpoint_calls_waiting_on_saver_lock() -> None:
    """A stopped run must not drain its checkpoint queue into durable state."""
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip("FIN_TEST_DSN is not set; queued checkpoint cancellation was not measured")

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.pregel._executor import BackgroundExecutor

    from fin.host.run_call_supervisor import RunCallCancelled, run_call_context
    from fin.host.run_control import StopToken

    with PostgresSaver.from_conn_string(compile_module.bounded_checkpoint_conninfo(dsn)) as saver:
        saver.setup()
        compile_module.bind_checkpoint_session_bounds(saver, dsn)
        thread_id = f"queued-{uuid.uuid4()}"
        observed_lock = _ObservedLock()
        saver.lock = observed_lock  # type: ignore[assignment]
        compile_module._bind_checkpoint_run_cancellation(saver)

        worker_count = 3
        barrier = threading.Barrier(worker_count + 1)
        worker_ids: set[int] = set()
        worker_ids_guard = threading.Lock()
        token = StopToken(thread_id, f"run-{uuid.uuid4()}")

        def _queued_put() -> None:
            with worker_ids_guard:
                worker_ids.add(threading.get_ident())
            barrier.wait(timeout=5)
            _put(saver, thread_id, str(uuid.uuid4()))

        with run_call_context(token, None):
            with BackgroundExecutor({"max_concurrency": worker_count}) as submit:
                futures = [
                    submit(_queued_put, __reraise_on_exit__=False)
                    for _ in range(worker_count)
                ]
                with observed_lock:
                    barrier.wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while len(worker_ids) < worker_count and time.monotonic() < deadline:
                        time.sleep(0.005)
                    assert len(worker_ids) == worker_count
                    assert observed_lock.wait_for_attempts(worker_ids, timeout=5)

                    token.stop(reason="client_disconnect")
                    done, _ = wait_for_futures(futures, timeout=1)
                    completed_before_release = len(done) == worker_count

        cancelled = 0
        for future in futures:
            try:
                future.result()
            except RunCallCancelled:
                cancelled += 1
        checkpoints = list(saver.list({"configurable": {"thread_id": thread_id}}))
        assert (
            completed_before_release
            and cancelled == worker_count
            and checkpoints == []
        ), (
            "cancelled saver-lock waiters must exit before lock release without "
            f"writing checkpoints; completed={completed_before_release}, "
            f"cancelled={cancelled}, late_checkpoints={len(checkpoints)}"
        )


def _wait_for_backend_lock_wait(dsn: str, backend_pid: int, timeout: float) -> bool:
    import psycopg

    deadline = time.monotonic() + timeout
    with psycopg.connect(dsn) as observer:
        while time.monotonic() < deadline:
            row = observer.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                (backend_pid,),
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return True
            time.sleep(0.02)
    return False


def test_stopped_run_maps_active_checkpoint_timeout_to_run_cancellation() -> None:
    """An active SQL lock timeout after stop is a typed run cancellation."""
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip("FIN_TEST_DSN is not set; active SQL cancellation was not measured")

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.pregel._executor import BackgroundExecutor

    from fin.host.run_call_supervisor import RunCallCancelled, run_call_context
    from fin.host.run_control import StopToken

    with PostgresSaver.from_conn_string(compile_module.bounded_checkpoint_conninfo(dsn)) as saver:
        saver.setup()
        compile_module.bind_checkpoint_session_bounds(saver, dsn)
        compile_module._bind_checkpoint_run_cancellation(saver)
        thread_id = f"active-{uuid.uuid4()}"
        backend_pid = int(
            saver.conn.execute("SELECT pg_backend_pid()").fetchone()["pg_backend_pid"]
        )
        blocker = _block_checkpoint_tables(dsn)
        token = StopToken(thread_id, f"run-{uuid.uuid4()}")
        started = time.monotonic()
        try:
            with run_call_context(token, None):
                with BackgroundExecutor({"max_concurrency": 1}) as submit:
                    future = submit(
                        _put,
                        saver,
                        thread_id,
                        str(uuid.uuid4()),
                        __reraise_on_exit__=False,
                    )
                    reached_database_lock = _wait_for_backend_lock_wait(
                        dsn, backend_pid, timeout=5
                    )
                    token.stop(reason="client_disconnect")
                    done, _ = wait_for_futures([future], timeout=4)
                    completed_before_release = future in done
            elapsed = time.monotonic() - started
        finally:
            blocker.rollback()
            blocker.close()

        with pytest.raises(RunCallCancelled):
            future.result()
        assert reached_database_lock
        assert completed_before_release
        assert elapsed < CHECKPOINT_STATEMENT_TIMEOUT_MS / 1000
        assert token.reason == "client_disconnect"
        assert list(saver.list({"configurable": {"thread_id": thread_id}})) == []


def test_state_graph_commit_put_writes_uses_stopped_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner done-callback must not let writes outlive a stopped run."""
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip("FIN_TEST_DSN is not set; LangGraph callback cancellation was not measured")

    from concurrent.futures import Future
    from contextlib import contextmanager
    from functools import wraps
    from typing import TypedDict

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.pregel._runner import FuturesDict

    from fin.host.graph import _LockedGraph
    from fin.host.run_call_supervisor import (
        RunCallCancelled,
        RunCallContextRegistry,
        current_run_call_context,
        run_call_context,
    )
    from fin.host.run_control import StopToken

    class State(TypedDict):
        left: str
        right: str

    left_started = threading.Event()
    right_started = threading.Event()
    release_node = threading.Event()
    node_callback_registered = threading.Event()
    graph_finished = threading.Event()
    callback_entered = threading.Event()
    callback_finished = threading.Event()
    callback_guard = threading.Lock()
    target_task_ids: set[str] = set()
    observed_task_ids: set[str] = set()
    callback_thread_ids: set[int] = set()
    callback_context_matches: list[bool] = []
    callback_errors: list[type[BaseException]] = []
    graph_errors: list[BaseException] = []

    def hold_left(_state: State) -> dict[str, str]:
        left_started.set()
        if not release_node.wait(timeout=10):
            raise TimeoutError("test node was not released")
        return {"left": "finished"}

    def hold_right(_state: State) -> dict[str, str]:
        right_started.set()
        if not release_node.wait(timeout=10):
            raise TimeoutError("test node was not released")
        return {"right": "finished"}

    original_setitem = FuturesDict.__setitem__

    def observe_runner_callback(
        futures: FuturesDict[Any, Any],
        future: Future[Any],
        task: Any,
    ) -> None:
        original_setitem(futures, future, task)
        if getattr(task, "name", None) in {"left", "right"}:
            with callback_guard:
                target_task_ids.add(str(task.id))
                if len(target_task_ids) == 2:
                    node_callback_registered.set()

    monkeypatch.setattr(FuturesDict, "__setitem__", observe_runner_callback)

    registry = RunCallContextRegistry()
    recovered_callback_context = threading.Event()
    with PostgresSaver.from_conn_string(
        compile_module.bounded_checkpoint_conninfo(dsn)
    ) as saver:
        saver.setup()
        compile_module.bind_checkpoint_session_bounds(saver, dsn)
        thread_id = f"langgraph-callback-{uuid.uuid4()}"
        token = StopToken(thread_id, f"run-{uuid.uuid4()}")
        observer_lock = _ObservedLock()
        saver.lock = observer_lock  # type: ignore[assignment]

        original_put_writes = saver.put_writes

        @wraps(original_put_writes)
        def observe_put_writes(
            config: Any,
            writes: Any,
            task_id: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            with callback_guard:
                is_target = task_id in target_task_ids
            if not is_target:
                return original_put_writes(config, writes, task_id, *args, **kwargs)
            context = current_run_call_context()
            with callback_guard:
                callback_thread_ids.add(threading.get_ident())
                callback_context_matches.append(
                    context is not None and context.stop_token is token
                )
                observed_task_ids.add(task_id)
                if target_task_ids <= observed_task_ids:
                    callback_entered.set()
            try:
                return original_put_writes(config, writes, task_id, *args, **kwargs)
            except BaseException as exc:
                with callback_guard:
                    callback_errors.append(type(exc))
                raise
            finally:
                callback_finished.set()

        saver.put_writes = observe_put_writes  # type: ignore[method-assign]
        compile_module._bind_checkpoint_run_cancellation(saver, registry)

        original_activate_thread = registry.activate_thread

        @contextmanager
        def observe_context_recovery(target_thread_id: str | None) -> Any:
            context_was_missing = current_run_call_context() is None
            with original_activate_thread(target_thread_id):
                context = current_run_call_context()
                if (
                    context_was_missing
                    and target_thread_id == thread_id
                    and context is not None
                    and context.stop_token is token
                ):
                    recovered_callback_context.set()
                yield

        monkeypatch.setattr(registry, "activate_thread", observe_context_recovery)

        builder = StateGraph(State)
        builder.add_node("left", hold_left)
        builder.add_node("right", hold_right)
        builder.add_edge(START, "left")
        builder.add_edge(START, "right")
        builder.add_edge("left", END)
        builder.add_edge("right", END)
        compiled = builder.compile(checkpointer=saver)
        graph = _LockedGraph(
            compiled_graph=compiled,
            recursion_limit=25,
            run_call_context_registry=registry,
        )

        def run_graph() -> None:
            try:
                with run_call_context(token, None):
                    list(
                        graph.stream(
                            {"value": "start"},
                            {"configurable": {"thread_id": thread_id}},
                            stream_mode="values",
                        )
                    )
            except BaseException as exc:
                graph_errors.append(exc)
            finally:
                graph_finished.set()

        worker = threading.Thread(target=run_graph, daemon=True)
        worker.start()
        lock_held = False
        try:
            assert left_started.wait(timeout=5)
            assert right_started.wait(timeout=5)
            assert node_callback_registered.wait(timeout=5)

            callback_entered.clear()
            callback_finished.clear()
            with callback_guard:
                callback_context_matches.clear()
                callback_errors.clear()
                callback_thread_ids.clear()
                observed_task_ids.clear()
            observer_lock.acquire()
            lock_held = True
            release_node.set()

            assert callback_entered.wait(timeout=5)
            with callback_guard:
                callback_threads = set(callback_thread_ids)
            assert observer_lock.wait_for_attempts(callback_threads, timeout=5)
            token.stop(reason="client_disconnect")
            callback_cancelled_before_release = callback_finished.wait(timeout=1)
            graph_finished_before_release = graph_finished.wait(timeout=1)
        finally:
            release_node.set()
            if lock_held:
                observer_lock.release()
            worker.join(timeout=5)

        with callback_guard:
            contexts_match = bool(callback_context_matches) and all(
                callback_context_matches
            )
            callback_cancelled = bool(callback_errors) and all(
                error is RunCallCancelled for error in callback_errors
            )

        assert node_callback_registered.is_set()
        assert recovered_callback_context.is_set()
        assert contexts_match
        assert callback_cancelled_before_release
        assert graph_finished_before_release
        assert callback_cancelled
        assert graph_finished.is_set()
        assert graph_errors and all(
            isinstance(error, RunCallCancelled) for error in graph_errors
        )
        assert token.reason == "client_disconnect"
