"""P1: the durable run identity, terminal commit and end outcomes.

The executor is the only place a run's graph consumption meets the durable
Runtime. These cases pin the ordering a reader and a restarted process rely
on: the terminal projection, sequence and outbox commit BEFORE the turn row
and before the terminal event; a stop ends the run as ``cancelled`` before
its row; a pause releases only the worker lease; and a worker whose lease
was taken away never writes a row as if it still owned the run.

Every graph here is hand-scripted: the seam under test is what the executor
does around a run's emissions, so the emissions are fixed and no model is
called.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from fin.host.run_control import RunControl
from fin.host.run_execution import (
    ResumeRunRequest,
    RunEventKind,
    RunExecutor,
    RunRequest,
    RunTechnicalResult,
    _LeaseKeepalive,
    _renew_interval_seconds,
)
from fin.host.runtime.schema import (
    AttemptLease,
    CheckpointOwnershipLost,
    RenewalOutcome,
    RunEndRecord,
)
from fin.host.thread_store import InMemoryThreadStore

THREAD_ID = "thread-runtime-exec"
RUN_ID = "run-runtime-exec"
ATTEMPT_ID = "attempt-runtime-exec"
ANSWER = "AAPL net sales were 81.8 billion dollars."


# ----- doubles -------------------------------------------------------------


class _FakeRuntimeStore:
    """Records the one durable terminal write and answers terminal reads."""

    def __init__(
        self, log: list[tuple[Any, ...]], *, error: BaseException | None = None
    ) -> None:
        self._log = log
        self._error = error
        self.terminals: dict[str, dict[str, Any]] = {}

    def commit_terminal(self, **kwargs: Any) -> None:
        if self._error is not None:
            raise self._error
        self._log.append(("commit_terminal", kwargs["run_id"]))
        self.terminals[kwargs["run_id"]] = kwargs

    def read_terminal(self, run_id: str) -> dict[str, Any] | None:
        return self.terminals.get(run_id)


class _RecordingRuntime:
    """The coordinator surface the executor calls, with every call logged."""

    def __init__(
        self,
        *,
        log: list[tuple[Any, ...]] | None = None,
        terminal_error: BaseException | None = None,
        renew_result: bool = True,
        end_error: BaseException | None = None,
    ) -> None:
        self.log: list[tuple[Any, ...]] = [] if log is None else log
        self.store = _FakeRuntimeStore(self.log, error=terminal_error)
        self.renew_calls = 0
        self._renew_result = renew_result
        self._end_error = end_error
        self.ends: list[tuple[str, str, str | None, int | None]] = []

    def renew(self, *, attempt_id: str) -> RenewalOutcome:
        self.renew_calls += 1
        self.log.append(("renew", attempt_id))
        return RenewalOutcome(
            renewed=self._renew_result, lease=None, reason=None
        )

    def end(
        self,
        *,
        run_id: str,
        outcome: str,
        attempt_id: str | None = None,
        attempt_generation: int | None = None,
    ) -> RunEndRecord:
        self.log.append(("end", run_id, outcome))
        self.ends.append((run_id, outcome, attempt_id, attempt_generation))
        if self._end_error is not None:
            raise self._end_error
        return RunEndRecord(
            run_id=run_id,
            thread_id=THREAD_ID,
            state=outcome,
            reservation_released=True,
            attempts_released=1,
        )

    def wait(self, *, attempt_id: str) -> bool:
        self.log.append(("wait", attempt_id))
        return True


class _RecordingReceipts:
    """The receipt seam the executor reports to, reduced to its order."""

    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self._log = log

    def begin(self, **kwargs: Any) -> None:
        self._log.append(("receipts.begin", kwargs["turn_id"]))

    def resume_started(self, **kwargs: Any) -> None:
        self._log.append(("receipts.resume_started", kwargs["turn_id"]))

    def reconcile_accepted_inputs(self, **kwargs: Any) -> None:
        return None

    def paused(self, **kwargs: Any) -> None:
        self._log.append(("receipts.paused", kwargs["turn_id"]))

    def commit_terminal(self, **kwargs: Any) -> None:
        self._log.append(("turn_row", kwargs["run"].id))

    def stream_abandoned(self, **kwargs: Any) -> None:
        self._log.append(("receipts.abandoned", kwargs["turn_id"]))


class _ScriptedGraph:
    """A deterministic graph whose one seam the executor owns."""

    def __init__(
        self,
        script: list[tuple[str, Any]],
        *,
        raises: BaseException | None = None,
        on_next: Any = None,
        log: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._script = script
        self._raises = raises
        self._on_next = on_next
        self._log = log
        self.configs: list[dict[str, Any]] = []
        self.closed = False

    def stream(
        self, input_value: Any, *, config: dict[str, Any], stream_mode: Any
    ):
        self.configs.append(config)
        return self._drive()

    def _drive(self):
        try:
            for index, item in enumerate(self._script):
                if self._on_next is not None:
                    self._on_next(index)
                yield item
            if self._raises is not None:
                raise self._raises
        finally:
            self.closed = True

    def is_locked(self, thread_id: str) -> bool:
        return False

    def interrupt_pending(self, thread_id: str) -> Any:
        return None

    def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
        return ()

    def checkpoint_message_ids(self, thread_id: str) -> set[str]:
        return set()

    def finalize_abandoned_run(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        if self._log is not None:
            self._log.append(("finalize_abandoned", thread_id, status))


def _done_state() -> dict[str, Any]:
    return {
        "messages": [AIMessage(content=ANSWER)],
        "status": "done",
        "answer": ANSWER,
        "citations": [],
        "evidence": [],
    }


def _done_script() -> list[tuple[str, Any]]:
    return [("values", _done_state())]


def _paused_script() -> list[tuple[str, Any]]:
    interrupt = type(
        "_Interrupt",
        (),
        {"value": {"kind": "choice_window", "options": []}},
    )()
    return [("values", {"__interrupt__": [interrupt]})]


def _lease(*, seconds: float = 120.0) -> AttemptLease:
    return AttemptLease(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        thread_id=THREAD_ID,
        worker_id="worker-runtime-exec",
        attempt_generation=1,
        thread_generation=1,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        state="active",
    )


def _store_with_thread(store: InMemoryThreadStore) -> None:
    store.create_thread(id=THREAD_ID, title="runtime executor")


def _drain(stream: Any, log: list[tuple[Any, ...]]) -> tuple[list[Any], Any]:
    events: list[Any] = []
    while True:
        try:
            event = next(stream)
        except StopIteration as stop:
            return events, stop.value
        events.append(event)
        if event.kind is RunEventKind.TERMINAL:
            log.append(("terminal_event", event.data.run_id))


def _send(
    *,
    graph: _ScriptedGraph,
    runtime: _RecordingRuntime,
    store: InMemoryThreadStore,
    receipts: _RecordingReceipts | None = None,
    config: dict[str, Any] | None = None,
    attempt: AttemptLease | None = None,
    control: RunControl | None = None,
) -> tuple[list[Any], Any]:
    executor = RunExecutor(
        graph=graph,
        store=store,
        run_control=control or RunControl(),
        receipts=receipts,
        runtime=runtime,
    )
    stream = executor.send(
        RunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            question="What was AAPL net sales in 2023 Q3?",
            input_value={"messages": []},
            config=config or {"configurable": {"thread_id": THREAD_ID}},
            attempt=attempt,
        )
    )
    return _drain(stream, runtime.log)


# ----- identity ------------------------------------------------------------


def test_the_attempt_identity_rides_a_copied_config() -> None:
    """The request's own dict is never mutated, and no key is dropped."""
    runtime = _RecordingRuntime()
    graph = _ScriptedGraph(_done_script())
    store = InMemoryThreadStore()
    _store_with_thread(store)
    config = {
        "configurable": {
            "thread_id": THREAD_ID,
            "workspace_root": "/workspace",
        }
    }

    _send(
        graph=graph,
        runtime=runtime,
        store=store,
        config=config,
        attempt=_lease(),
    )

    assert config == {
        "configurable": {
            "thread_id": THREAD_ID,
            "workspace_root": "/workspace",
        }
    }
    seen = graph.configs[-1]["configurable"]
    assert seen["runtime_run_id"] == RUN_ID
    assert seen["runtime_attempt_id"] == ATTEMPT_ID
    assert seen["runtime_attempt_generation"] == 1
    assert seen["thread_generation"] == 1
    # The identity is additive: the caller's own keys survive.
    assert seen["thread_id"] == THREAD_ID
    assert seen["workspace_root"] == "/workspace"


def test_a_run_without_an_attempt_keeps_the_preexisting_path() -> None:
    """``runtime=None``/no attempt is the pre-P1 caller, byte for byte."""
    runtime = _RecordingRuntime()
    graph = _ScriptedGraph(_done_script())
    store = InMemoryThreadStore()
    _store_with_thread(store)
    config = {"configurable": {"thread_id": THREAD_ID}}
    executor = RunExecutor(
        graph=graph, store=store, run_control=RunControl(), runtime=runtime
    )

    list(
        executor.send(
            RunRequest(
                thread_id=THREAD_ID,
                run_id=RUN_ID,
                question="What was AAPL net sales in 2023 Q3?",
                input_value={"messages": []},
                config=config,
            )
        )
    )

    assert graph.configs[-1] == {"configurable": {"thread_id": THREAD_ID}}
    assert config == {"configurable": {"thread_id": THREAD_ID}}
    assert runtime.log == []


# ----- terminal ordering ---------------------------------------------------


def test_the_durable_terminal_commits_before_the_row_and_the_event() -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log=log)
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)

    events, outcome = _send(
        graph=_ScriptedGraph(_done_script(), log=log),
        runtime=runtime,
        store=store,
        receipts=receipts,
        attempt=_lease(),
    )

    assert outcome.technical_result is RunTechnicalResult.DONE
    assert events[-1].kind is RunEventKind.TERMINAL
    assert log == [
        ("commit_terminal", RUN_ID),
        ("turn_row", RUN_ID),
        ("terminal_event", RUN_ID),
    ]
    committed = runtime.store.terminals[RUN_ID]
    assert committed["technical_result"] == RunTechnicalResult.DONE.value
    assert committed["semantic_result"] == "answered"
    assert committed["payload"]["status"] == "done"
    assert committed["payload"]["answer"] == ANSWER
    assert committed["projection"]["kind"] == "answer"
    assert committed["projection"]["status"] == "done"
    assert committed["projection"]["answer"] == ANSWER
    assert committed["outbox_payload"] == {"run_id": RUN_ID, "kind": "terminal"}
    assert committed["attempt_id"] == ATTEMPT_ID
    assert committed["attempt_generation"] == 1


# ----- stop / pause / failure ---------------------------------------------


def test_a_stop_ends_the_run_cancelled_before_its_row() -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log=log)
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)
    control = RunControl()

    events, outcome = _send(
        graph=_ScriptedGraph(
            _done_script(),
            on_next=lambda index: control.request_stop(THREAD_ID),
            log=log,
        ),
        runtime=runtime,
        store=store,
        receipts=receipts,
        attempt=_lease(),
        control=control,
    )

    assert outcome.technical_result is RunTechnicalResult.STOPPED
    assert ("end", RUN_ID, "cancelled") in log
    assert ("turn_row", RUN_ID) in log
    assert log.index(("end", RUN_ID, "cancelled")) < log.index(("turn_row", RUN_ID))
    # The end carries the worker identity, so a stale worker cannot end the
    # run a takeover now owns.
    assert runtime.ends == [(RUN_ID, "cancelled", ATTEMPT_ID, 1)]
    # A stopped run is a terminal turn, not a durable-terminal answer.
    assert ("commit_terminal", RUN_ID) not in log


def test_a_pause_releases_the_lease_and_ends_nothing() -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log=log)
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)

    events, outcome = _send(
        graph=_ScriptedGraph(_paused_script(), log=log),
        runtime=runtime,
        store=store,
        receipts=receipts,
        attempt=_lease(),
    )

    assert outcome.choice_window == {"kind": "choice_window", "options": []}
    assert events[-1].kind is RunEventKind.CHOICE_WINDOW
    assert ("wait", ATTEMPT_ID) in log
    assert not any(entry[0] == "end" for entry in log)
    assert ("commit_terminal", RUN_ID) not in log
    assert ("turn_row", RUN_ID) not in log
    assert store.list_runs(THREAD_ID) == []


def test_a_mid_flight_failure_commits_refuse_then_abandons_the_checkpoint() -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log=log)
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)

    _, outcome = _send(
        graph=_ScriptedGraph(
            [], raises=TimeoutError("provider read timed out after 120.0 s"), log=log
        ),
        runtime=runtime,
        store=store,
        receipts=receipts,
        attempt=_lease(),
    )

    assert outcome.technical_result is RunTechnicalResult.TIMEOUT
    assert ("commit_terminal", RUN_ID) in log
    assert ("turn_row", RUN_ID) in log
    assert log.index(("commit_terminal", RUN_ID)) < log.index(("turn_row", RUN_ID))
    assert log.index(("turn_row", RUN_ID)) < log.index(
        ("finalize_abandoned", THREAD_ID, "refuse")
    )
    committed = runtime.store.terminals[RUN_ID]
    assert committed["technical_result"] == RunTechnicalResult.TIMEOUT.value
    assert committed["semantic_result"] is None
    assert committed["payload"]["status"] == "refuse"


def test_an_abandoned_stream_ends_interrupted_without_a_row() -> None:
    """A client disconnect is crash-resume's case: no row, no checkpoint write."""
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log=log)
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)
    graph = _ScriptedGraph(_done_script(), log=log)
    executor = RunExecutor(
        graph=graph,
        store=store,
        run_control=RunControl(),
        receipts=receipts,
        runtime=runtime,
    )

    stream = executor.send(
        RunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            question="What was AAPL net sales in 2023 Q3?",
            input_value={"messages": []},
            config={"configurable": {"thread_id": THREAD_ID}},
            attempt=_lease(),
        )
    )
    next(stream)
    stream.close()

    assert ("end", RUN_ID, "interrupted") in log
    assert ("commit_terminal", RUN_ID) not in log
    assert ("turn_row", RUN_ID) not in log
    assert runtime.ends == [(RUN_ID, "interrupted", ATTEMPT_ID, 1)]
    assert not any(entry[0] == "finalize_abandoned" for entry in log)
    assert store.list_runs(THREAD_ID) == []


def test_a_stale_worker_cannot_end_an_abandoned_run_it_no_longer_owns() -> None:
    """A refused fenced end is local cleanup, not a second failure."""
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(
        log=log, end_error=CheckpointOwnershipLost("attempt_not_active")
    )
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)
    executor = RunExecutor(
        graph=_ScriptedGraph(_done_script(), log=log),
        store=store,
        run_control=RunControl(),
        receipts=receipts,
        runtime=runtime,
    )

    stream = executor.send(
        RunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            question="What was AAPL net sales in 2023 Q3?",
            input_value={"messages": []},
            config={"configurable": {"thread_id": THREAD_ID}},
            attempt=_lease(),
        )
    )
    next(stream)
    stream.close()

    assert runtime.ends == [(RUN_ID, "interrupted", ATTEMPT_ID, 1)]
    assert ("turn_row", RUN_ID) not in log


def test_a_fenced_worker_never_writes_a_row() -> None:
    """``CheckpointOwnershipLost`` escapes instead of becoming a turn row."""
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(
        log=log, terminal_error=CheckpointOwnershipLost("attempt_not_active")
    )
    receipts = _RecordingReceipts(log)
    store = InMemoryThreadStore()
    _store_with_thread(store)
    executor = RunExecutor(
        graph=_ScriptedGraph(_done_script(), log=log),
        store=store,
        run_control=RunControl(),
        receipts=receipts,
        runtime=runtime,
    )

    with pytest.raises(CheckpointOwnershipLost):
        list(
            executor.send(
                RunRequest(
                    thread_id=THREAD_ID,
                    run_id=RUN_ID,
                    question="What was AAPL net sales in 2023 Q3?",
                    input_value={"messages": []},
                    config={"configurable": {"thread_id": THREAD_ID}},
                    attempt=_lease(),
                )
            )
        )

    assert not any(entry[0] == "turn_row" for entry in log)
    assert ("terminal_event", RUN_ID) not in log
    assert store.list_runs(THREAD_ID) == []


def test_a_resume_failure_ends_interrupted_before_the_maintenance_write() -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log=log)
    graph = _ScriptedGraph([], raises=RuntimeError("provider exploded"), log=log)
    store = InMemoryThreadStore()
    _store_with_thread(store)
    executor = RunExecutor(
        graph=graph,
        store=store,
        run_control=RunControl(),
        runtime=runtime,
    )

    stream = executor.resume(
        ResumeRunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            resume_input=None,
            resume_config={"configurable": {"thread_id": THREAD_ID}},
            attempt=_lease(),
        )
    )
    _, outcome = _drain(stream, log)

    assert outcome.final_state is None
    assert ("end", RUN_ID, "interrupted") in log
    assert ("finalize_abandoned", THREAD_ID, "refuse") in log
    assert log.index(("end", RUN_ID, "interrupted")) < log.index(
        ("finalize_abandoned", THREAD_ID, "refuse")
    )
    assert ("commit_terminal", RUN_ID) not in log


# ----- lease keepalive -----------------------------------------------------


def test_the_renewal_interval_is_a_third_of_the_remaining_lease() -> None:
    assert _renew_interval_seconds(_lease(seconds=0.0)) == 1.0
    interval = _renew_interval_seconds(_lease(seconds=60.0))
    assert 19.0 <= interval <= 21.0


def test_the_keepalive_renews_until_it_is_stopped() -> None:
    runtime = _RecordingRuntime()
    keepalive = _LeaseKeepalive(runtime, _lease(seconds=0.0)).start()
    try:
        deadline = time.monotonic() + 3.0
        while runtime.renew_calls < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        keepalive.stop()

    assert runtime.renew_calls >= 1
    calls = runtime.renew_calls
    time.sleep(0.05)
    assert runtime.renew_calls == calls


def test_a_refused_renewal_is_not_retried_into_a_fabricated_success() -> None:
    runtime = _RecordingRuntime(renew_result=False)
    keepalive = _LeaseKeepalive(runtime, _lease(seconds=0.0)).start()
    deadline = time.monotonic() + 3.0
    while keepalive._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    keepalive.stop()

    assert not keepalive._thread.is_alive()
    assert runtime.renew_calls == 1
