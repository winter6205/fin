"""Boot-thread wiring for the attachment auto-resumer (T2f durable gate).

The resume worker is the second execution half of the durable graph
gate: a run paused on the ``attachment_preparation`` interrupt is driven
forward by this sweep once its preparation job has SETTLED — no operator
resend, no HTTP call. What this file pins:

* the lifecycle posture mirrored from the prep-worker boot tests: nothing
  spawns at import, ``start`` is the process singleton (a second call
  returns the live thread), a start racing a timed-out stop is refused
  with the typed error until the old sweep dies, a clean stop joins the
  loop, and a restart after a confirmed stop gets a fresh live thread;
* the end-to-end drive over the REAL in-process graph (``compile_graph``
  + ``ScriptedChatModel`` + ``InMemorySaver``) and the REAL
  ``AttachmentPreparationService`` on in-memory ports: a settled pause
  completes the SAME run_id through the one resume path
  ``POST /resume`` shares; an unsettled pause is left alone until a
  later sweep (the loop's re-check); a failed preparation reaches the
  turn row as the run's typed exit instead of hanging the pause forever;
* a process restart over the same durable state: a fresh service
  instance ``recover()``\\ s (re-adopting the interrupted ``running``
  job exactly once), drains it, and the waiting run resumes;
* the guards (exact interrupt-kind compare, live-run lock, pending
  nodes, checkpoint run_id, the never-``None`` string resume value) and
  the log-and-continue exception safety at both the per-thread and the
  whole-sweep level.

Fidelity: the end-to-end cases above run the real graph, the real
service, and the real ``_resume_run_stream`` — no stand-ins. The guard
and failure-injection cases replace only ``_resume_run_stream`` (the
one function the sweep calls to drive a resume) with a recording
stand-in, or duck-type the graph's checkpoint probes, so a failure can
be addressed to exactly one thread; the recording also pins the
``Command(resume=<string>)`` value shape the module promises. The
``serve()`` wiring case patches the composition root, the same posture
as ``tests/host/test_ingest_worker_boot.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fin.corpus.extracted import Locator
from fin.corpus.object_store import InMemoryObjectStore
from fin.host import EvidenceItem, ScriptedChatModel, compile_graph
from fin.host import workspace_http
from fin.host.attachment_job_store import InMemoryThreadAttachmentJobStore
from fin.host.attachment_prepare import AttachmentPreparationService
from fin.host.attachment_store import InMemoryAttachmentStore
from fin.host.graph import ATTACHMENT_PREPARATION_INTERRUPT_KIND
from fin.host.run_control import RunControl
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.host.workspace_http import (
    ATTACHMENT_RESUME_WORKER_THREAD_NAME,
    AttachmentResumeWorkerStoppingError,
    _check_attachment_resume_waits,
    _resume_attachment_waits_once,
    _resume_one_attachment_wait,
    start_attachment_resume_worker,
    stop_attachment_resume_worker,
)

RUN_ID = "run-t2f-resume"
REVENUE_TEXT = b"Q3 revenue was 89,493 million dollars.\n"

# Constants shared with the gate suite: a grounded question, pre-seeded
# run evidence and a >=32-char extractive answer so a resumed turn that
# clears the gate ends ``done`` at the citation gate on one model hop.
GROUNDED = "What was AAPL net sales in 2023 Q3?"
KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
EVIDENCE_TEXT = "Microsoft Corporation reported total revenue of 52,857 million dollars."
QUOTED_ANSWER = f"Per the filing: {EVIDENCE_TEXT[:40]}"
_ANSWER = AIMessage(content=QUOTED_ANSWER)

_ATT_PAYLOAD = {
    "kind": ATTACHMENT_PREPARATION_INTERRUPT_KIND,
    "attachment_ids": ["att-1"],
}
_CHOICE_PAYLOAD = {"kind": "choice_window", "attachment_ids": ["att-1"]}


@pytest.fixture(autouse=True)
def _resume_worker_singleton_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # The resume worker is module-global process state. Each case starts
    # from the never-booted posture and leaves no live thread behind,
    # whatever the case body did.
    monkeypatch.setattr(
        workspace_http, "_attachment_resume_worker", None, raising=False
    )
    yield
    stop_attachment_resume_worker(timeout=10.0)


def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true")


# ----- real-stack fixtures (end-to-end fidelity) -------------------------


def _input(text: str, attachments: list[dict[str, str]] | None = None) -> dict[str, Any]:
    state = reset_input_state(text, attachments=attachments)
    state["evidence"] = [
        EvidenceItem(
            locator=Locator(document_id="MSFT-2023Q1", version="e" * 64, unit=1),
            unit_text=EVIDENCE_TEXT,
        )
    ]
    return state


def _live_stack(tmp_path: Any) -> SimpleNamespace:
    """The resume sweep's whole world on real parts and in-memory ports."""
    threads = InMemoryThreadStore()
    jobs = InMemoryThreadAttachmentJobStore()
    objects = InMemoryObjectStore()
    attachment_store = InMemoryAttachmentStore(threads)
    service = AttachmentPreparationService(
        store=attachment_store, objects=objects, jobs=jobs
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[_ANSWER]),
        known_document_ids=KNOWN_IDS,
        attachment_service=service,
    )
    thread = threads.create_thread(title="resume boot")
    record = service.accept(
        thread_id=thread.id, filename="notes.txt", payload=REVENUE_TEXT
    )
    return SimpleNamespace(
        threads=threads,
        jobs=jobs,
        objects=objects,
        attachment_store=attachment_store,
        service=service,
        graph=graph,
        thread_id=thread.id,
        attachment_id=record.attachment_id,
    )


def _pause_at_gate(stack: SimpleNamespace) -> None:
    """Run the grounded turn so the gate pauses it on its own interrupt.

    This is the exact durable wait shape: the checkpoint holds the
    ``attachment_preparation`` interrupt, the original run_id, and a
    queued (unsettled) job — with no turn row, because a paused run has
    nothing to persist.
    """
    stack.graph.invoke(
        _input(
            GROUNDED,
            attachments=[
                {
                    "attachment_id": stack.attachment_id,
                    "filename": "notes.txt",
                    "text": "",
                }
            ],
        ),
        config={"configurable": {"thread_id": stack.thread_id, "run_id": RUN_ID}},
    )
    payload = stack.graph.interrupt_pending(stack.thread_id)
    assert payload is not None
    assert payload["kind"] == ATTACHMENT_PREPARATION_INTERRUPT_KIND
    assert payload["attachment_ids"] == [stack.attachment_id]
    assert stack.graph.checkpoint_run_id(stack.thread_id) == RUN_ID
    assert stack.threads.list_runs(stack.thread_id) == []
    # The gate's ask queued exactly one active job.
    assert len(stack.jobs.incomplete()) == 1


def _settle(stack: SimpleNamespace) -> None:
    """Run the queued preparation job through the real executor."""
    assert stack.service.drain_attachment_jobs() >= 1
    assert stack.service.status(stack.attachment_id).status == "ready"
    assert stack.service.request_preparation(
        stack.thread_id, stack.attachment_id
    ).state == "ready"


# ----- duck-typed fixtures (guard / injection fidelity) ------------------


class _Outcome:
    def __init__(self, state: str) -> None:
        self.state = state


class _StubService:
    """Stand-in for the settle check: answers one scripted state."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[tuple[str, str]] = []

    def request_preparation(self, thread_id: str, attachment_id: str) -> _Outcome:
        self.calls.append((thread_id, attachment_id))
        return _Outcome(self.state)


class _FakeGraph:
    """The four checkpoint probes ``_resume_one_attachment_wait`` reads."""

    def __init__(
        self,
        payloads: dict[str, Any] | None = None,
        run_ids: dict[str, str] | None = None,
        locked: tuple[str, ...] = (),
        pending_overrides: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.payloads = dict(payloads or {})
        self.run_ids = dict(run_ids or {})
        self.locked = set(locked)
        self.pending_overrides = dict(pending_overrides or {})
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def is_locked(self, thread_id: str) -> bool:
        return thread_id in self.locked

    def interrupt_pending(self, thread_id: str) -> dict[str, Any] | None:
        if self.entered is not None:
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(timeout=10.0), "gate never released"
        return self.payloads.get(thread_id)

    def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
        if thread_id in self.pending_overrides:
            return self.pending_overrides[thread_id]
        return ("ensure_attachment_ready",) if self.payloads.get(thread_id) else ()

    def checkpoint_run_id(self, thread_id: str) -> str | None:
        return self.run_ids.get(thread_id)


def _install_resume_recorder(
    monkeypatch: pytest.MonkeyPatch, raises_for: str | None = None
) -> list[tuple[str, str, Any, dict[str, Any]]]:
    """Record (and optionally poison) the shared resume drive.

    ``_resume_one_attachment_wait`` calls exactly ``_resume_run_stream``
    to drive a resume, so this is the honest stand-in seam: the
    recording pins the resume VALUE and CONFIG the sweep builds, and
    ``raises_for`` addresses a failure to one thread.
    """
    driven: list[tuple[str, str, Any, dict[str, Any]]] = []

    def _fake(
        graph: Any,
        store: Any,
        *,
        run_control: Any,
        thread_id: str,
        run_id: str,
        resume_input: Any,
        resume_config: dict[str, Any],
    ) -> Iterator[str]:
        driven.append((thread_id, run_id, resume_input, resume_config))
        if thread_id == raises_for:
            raise RuntimeError("poisoned checkpoint")
        return iter(())

    monkeypatch.setattr(workspace_http, "_resume_run_stream", _fake)
    return driven


def _guard_stack() -> tuple[InMemoryThreadStore, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="guard")
    return threads, thread.id


# ----- lifecycle ----------------------------------------------------------


def test_nothing_spawns_until_start_and_stop_is_an_idempotent_noop() -> None:
    before = {thread.name for thread in threading.enumerate()}
    assert ATTACHMENT_RESUME_WORKER_THREAD_NAME not in before

    stop_attachment_resume_worker()  # never started: no-op
    stop_attachment_resume_worker(timeout=1.0)  # still a no-op

    assert {thread.name for thread in threading.enumerate()} == before


def test_start_is_singleton_safe_daemon_and_restart_after_clean_stop(
    tmp_path: Any,
) -> None:
    threads, tid = _guard_stack()
    graph = _FakeGraph({tid: None})
    kwargs: dict[str, Any] = {
        "graph": graph,
        "store": threads,
        "service": _StubService("ready"),
        "workspace_root": tmp_path,
        "check_interval_seconds": 60.0,
    }

    first = start_attachment_resume_worker(**kwargs)
    second = start_attachment_resume_worker(**kwargs)

    assert second is first
    assert first.name == ATTACHMENT_RESUME_WORKER_THREAD_NAME
    assert first.daemon

    stop_attachment_resume_worker(timeout=10.0)
    assert not first.is_alive()

    # Start-after-stopped per the module contract: a restart re-spawns
    # the sweep over the same seams rather than refusing.
    third = start_attachment_resume_worker(**kwargs)
    assert third is not first
    assert third.is_alive()
    stop_attachment_resume_worker(timeout=10.0)
    assert not third.is_alive()
    # A stop of the now-dead worker is again a no-op, twice over.
    stop_attachment_resume_worker(timeout=1.0)


def test_start_after_a_timed_out_stop_refuses_until_the_old_sweep_dies(
    tmp_path: Any,
) -> None:
    threads, tid = _guard_stack()
    graph = _FakeGraph({tid: None})
    graph.entered = threading.Event()
    graph.release = threading.Event()
    kwargs: dict[str, Any] = {
        "graph": graph,
        "store": threads,
        "service": _StubService("ready"),
        "workspace_root": tmp_path,
        "check_interval_seconds": 60.0,
    }

    first = start_attachment_resume_worker(**kwargs)
    assert graph.entered.wait(10.0)

    stop_attachment_resume_worker(timeout=0.1)  # gated mid-sweep: join times out
    assert first.is_alive()

    with pytest.raises(AttachmentResumeWorkerStoppingError):
        start_attachment_resume_worker(**kwargs)
    assert [
        thread
        for thread in threading.enumerate()
        if thread.name == ATTACHMENT_RESUME_WORKER_THREAD_NAME
    ] == [first]

    graph.release.set()
    _wait_until(lambda: not first.is_alive())
    second = start_attachment_resume_worker(**kwargs)
    assert second is not first
    assert second.is_alive()
    stop_attachment_resume_worker(timeout=10.0)


# ----- the auto-resumer drives settled pauses (real graph + service) -----


def test_unsettled_pause_is_left_alone_and_resumes_on_a_later_sweep(
    tmp_path: Any,
) -> None:
    """The settle clause, tick by tick, with no thread spawned.

    A job still queued/running answers ``pending``: the sweep drives
    nothing and the run stays paused; the NEXT sweep — after the job
    settles — is what resumes it. This is the loop's re-check semantics
    minus the timer.
    """
    stack = _live_stack(tmp_path)
    _pause_at_gate(stack)

    assert stack.service.request_preparation(
        stack.thread_id, stack.attachment_id
    ).state == "pending"
    resumed = _resume_attachment_waits_once(
        stack.graph, stack.threads, stack.service, RunControl(),
        workspace_root=tmp_path,
    )
    assert resumed == 0
    assert stack.threads.list_runs(stack.thread_id) == []
    assert stack.graph.interrupt_pending(stack.thread_id) is not None

    _settle(stack)
    resumed = _resume_attachment_waits_once(
        stack.graph, stack.threads, stack.service, RunControl(),
        workspace_root=tmp_path,
    )
    assert resumed == 1
    rows = stack.threads.list_runs(stack.thread_id)
    assert [row.id for row in rows] == [RUN_ID]  # the SAME run completes
    assert rows[0].status == "done"
    assert rows[0].answer == QUOTED_ANSWER
    assert stack.graph.interrupt_pending(stack.thread_id) is None
    # A settled pause is not driven twice.
    assert (
        _resume_attachment_waits_once(
            stack.graph, stack.threads, stack.service, RunControl(),
            workspace_root=tmp_path,
        )
        == 0
    )


def test_background_resumer_completes_the_paused_run_with_no_operator_call(
    tmp_path: Any,
) -> None:
    """The boot posture end to end: the started thread does the resume.

    No HTTP app, client, or operator resend exists anywhere in this
    case: the worker thread's own periodic sweep is the only driver,
    which is exactly the clause the durable gate promises.
    """
    stack = _live_stack(tmp_path)
    _pause_at_gate(stack)

    thread = start_attachment_resume_worker(
        graph=stack.graph,
        store=stack.threads,
        service=stack.service,
        workspace_root=tmp_path,
        check_interval_seconds=0.05,
    )
    try:
        _settle(stack)
        _wait_until(lambda: stack.threads.list_runs(stack.thread_id) != [])
    finally:
        stop_attachment_resume_worker(timeout=10.0)

    rows = stack.threads.list_runs(stack.thread_id)
    assert [row.id for row in rows] == [RUN_ID]
    assert rows[0].status == "done"
    assert stack.graph.interrupt_pending(stack.thread_id) is None
    record = stack.attachment_store.get(stack.attachment_id)
    assert record is not None
    job = stack.jobs.get(
        attachment_id=stack.attachment_id, content_sha256=record.sha256
    )
    assert job is not None and job.state == "succeeded"
    assert not thread.is_alive()


def test_failed_preparation_reaches_the_turn_row_as_the_typed_refuse(
    tmp_path: Any,
) -> None:
    """``failed`` is SETTLED: the pause must not hang forever.

    The gate re-evaluates on its own and refuses with the typed reason,
    so the auto-resumer drives the run to its turn row instead of
    waiting for a success that will never come.
    """
    stack = _live_stack(tmp_path)
    _pause_at_gate(stack)
    record = stack.attachment_store.get(stack.attachment_id)
    assert record is not None
    stack.objects.delete(record.object_key)  # the job's bytes go unreadable

    stack.service.drain_attachment_jobs()
    assert stack.service.status(stack.attachment_id).status == "failed"

    resumed = _resume_attachment_waits_once(
        stack.graph, stack.threads, stack.service, RunControl(),
        workspace_root=tmp_path,
    )
    assert resumed == 1
    rows = stack.threads.list_runs(stack.thread_id)
    assert [row.id for row in rows] == [RUN_ID]
    assert rows[0].status == "refuse"
    assert rows[0].answer == ""
    assert stack.graph.interrupt_pending(stack.thread_id) is None


# ----- restart over the same durable state --------------------------------


def test_restart_re_adopts_interrupted_job_resumes_wait_without_duplicates(
    tmp_path: Any,
) -> None:
    """The process-death clause, hermetically: fresh service, same stores.

    A ``running`` job belongs to an executor that died with the old
    process; ``recover()`` (via ``requeue_running``) re-adopts it to
    ``pending`` exactly once — two recover calls mint no second active
    job — the drained job settles, and the waiting run resumes with its
    original run_id and no operator resend.
    """
    stack = _live_stack(tmp_path)
    _pause_at_gate(stack)
    # Kill the process mid-job: claimed, running, never finished.
    claimed = stack.jobs.claim_next()
    assert claimed is not None
    assert claimed.state == "running"
    assert stack.service.status(stack.attachment_id).status == "parsing"

    # The new process: a fresh service instance over the SAME durable
    # store objects (the in-memory twins of the Postgres rows).
    service_b = AttachmentPreparationService(
        store=stack.attachment_store, objects=stack.objects, jobs=stack.jobs
    )
    assert service_b.recover() == 1
    # A second boot sweep finds nothing to re-adopt and no duplicate
    # active job for the one content key.
    assert service_b.recover() == 0
    active = stack.jobs.incomplete()
    assert [job.attachment_id for job in active] == [stack.attachment_id]

    assert service_b.drain_attachment_jobs() >= 1
    assert service_b.status(stack.attachment_id).status == "ready"
    assert stack.jobs.incomplete() == ()

    resumed = _resume_attachment_waits_once(
        stack.graph, stack.threads, service_b, RunControl(),
        workspace_root=tmp_path,
    )
    assert resumed == 1
    rows = stack.threads.list_runs(stack.thread_id)
    assert [row.id for row in rows] == [RUN_ID]
    assert rows[0].status == "done"


# ----- guards on the interrupt kind and the checkpoint probes -------------


def test_choice_window_pause_is_never_auto_resumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Exact-kind compare, not a deny-list: only the gate's kind drives.

    A ``choice_window`` pause waits on the operator's pick; the sweep
    must not answer it — it does not even ask the service whether work
    settled.
    """
    threads, tid = _guard_stack()
    graph = _FakeGraph({tid: _CHOICE_PAYLOAD}, run_ids={tid: "run-choice"})
    service = _StubService("ready")
    driven = _install_resume_recorder(monkeypatch)

    ok = _resume_one_attachment_wait(
        graph, threads, service, RunControl(), tid, workspace_root=tmp_path
    )
    assert ok is False
    assert driven == []
    assert service.calls == []


def test_settled_none_and_plain_crash_shapes_are_not_driven(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    threads, tid = _guard_stack()
    service = _StubService("ready")
    driven = _install_resume_recorder(monkeypatch)

    no_interrupt = _FakeGraph({tid: None}, run_ids={tid: "run-1"})
    assert (
        _resume_one_attachment_wait(
            no_interrupt, threads, service, RunControl(), tid,
            workspace_root=tmp_path,
        )
        is False
    )
    wrong_shape = _FakeGraph({tid: "not-a-dict"}, run_ids={tid: "run-1"})
    assert (
        _resume_one_attachment_wait(
            wrong_shape, threads, service, RunControl(), tid,
            workspace_root=tmp_path,
        )
        is False
    )
    # A terminal checkpoint: pending nodes empty means the run already
    # reached its turn row — nothing to resume.
    finalized = _FakeGraph(
        {tid: _ATT_PAYLOAD},
        run_ids={tid: "run-1"},
        pending_overrides={tid: ()},
    )
    assert (
        _resume_one_attachment_wait(
            finalized, threads, service, RunControl(), tid,
            workspace_root=tmp_path,
        )
        is False
    )
    # A live run holds the per-thread lock: out of reach.
    locked = _FakeGraph(
        {tid: _ATT_PAYLOAD}, run_ids={tid: "run-1"}, locked=(tid,)
    )
    assert (
        _resume_one_attachment_wait(
            locked, threads, service, RunControl(), tid,
            workspace_root=tmp_path,
        )
        is False
    )
    # A store/graph without the checkpoint probes is not a resume-capable
    # seam at all.
    assert (
        _resume_one_attachment_wait(
            object(), threads, service, RunControl(), tid,
            workspace_root=tmp_path,
        )
        is False
    )
    assert driven == []


def test_pending_job_blocks_the_resume_even_for_the_gate_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    threads, tid = _guard_stack()
    graph = _FakeGraph({tid: _ATT_PAYLOAD}, run_ids={tid: "run-1"})
    service = _StubService("pending")
    driven = _install_resume_recorder(monkeypatch)

    ok = _resume_one_attachment_wait(
        graph, threads, service, RunControl(), tid, workspace_root=tmp_path
    )
    assert ok is False
    assert service.calls == [(tid, "att-1")]
    assert driven == []


def test_run_id_is_read_from_the_checkpoint_and_a_pause_without_one_waits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The resume completes the checkpoint's run, never a minted second id."""
    threads, tid = _guard_stack()
    service = _StubService("ready")
    driven = _install_resume_recorder(monkeypatch)

    graph = _FakeGraph({tid: _ATT_PAYLOAD}, run_ids={tid: "run-checkpoint"})
    ok = _resume_one_attachment_wait(
        graph, threads, service, RunControl(), tid, workspace_root=tmp_path
    )
    assert ok is True
    thread_id, run_id, resume_input, resume_config = driven[0]
    assert (thread_id, run_id) == (tid, "run-checkpoint")
    assert resume_config["configurable"]["thread_id"] == tid
    assert resume_config["configurable"]["run_id"] == "run-checkpoint"
    assert resume_config["configurable"]["workspace_root"] == str(tmp_path)
    # The pinned value shape: langgraph chokes on ``Command(resume=None)``
    # in this version, so the auto-resumer always sends a string sentinel.
    assert isinstance(resume_input, Command)
    assert isinstance(resume_input.resume, str) and resume_input.resume

    # No checkpoint run_id: leave it for an operator, drive nothing.
    driven.clear()
    orphan = _FakeGraph({tid: _ATT_PAYLOAD}, run_ids={})
    assert (
        _resume_one_attachment_wait(
            orphan, threads, service, RunControl(), tid, workspace_root=tmp_path
        )
        is False
    )
    assert driven == []


# ----- exception safety ----------------------------------------------------


def test_poisoned_thread_is_logged_and_the_sweep_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Any
) -> None:
    """One bad checkpoint cannot starve the rest of the sweep."""
    threads = InMemoryThreadStore()
    bad = threads.create_thread(title="poisoned").id
    ok = threads.create_thread(title="healthy").id
    graph = _FakeGraph(
        {bad: _ATT_PAYLOAD, ok: _ATT_PAYLOAD},
        run_ids={bad: "run-bad", ok: "run-ok"},
    )
    driven = _install_resume_recorder(monkeypatch, raises_for=bad)

    with caplog.at_level(logging.ERROR, logger="fin.host.workspace_http"):
        resumed = _resume_attachment_waits_once(
            graph, threads, _StubService("ready"), RunControl(),
            workspace_root=tmp_path,
        )

    assert resumed == 1  # the poisoned thread is NOT counted as resumed
    # Both threads were reached: the failure is contained to its own row.
    assert {entry[0] for entry in driven} == {bad, ok}
    assert sum(1 for entry in driven if entry[0] == ok) == 1
    failure_records = [
        record
        for record in caplog.records
        if record.name == "fin.host.workspace_http" and record.exc_info
    ]
    assert failure_records
    assert isinstance(failure_records[0].exc_info[1], RuntimeError)


def test_failed_sweep_is_logged_and_the_loop_keeps_running(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Chosen posture, loop level: keep sweeping, never silent death."""
    stop = threading.Event()
    calls: list[int] = []

    def _flaky_once(*args: Any, **kwargs: Any) -> int:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("checkpoint store is briefly down")
        stop.set()
        return 0

    monkeypatch.setattr(
        workspace_http, "_resume_attachment_waits_once", _flaky_once
    )

    thread = threading.Thread(
        target=_check_attachment_resume_waits,
        args=(None, None, None, RunControl(), None, stop, 0.01),
    )
    with caplog.at_level(logging.ERROR, logger="fin.host.workspace_http"):
        thread.start()
        thread.join(timeout=10.0)

    assert not thread.is_alive()
    assert len(calls) == 2
    failure_records = [
        record
        for record in caplog.records
        if record.name == "fin.host.workspace_http" and record.exc_info
    ]
    assert failure_records
    assert isinstance(failure_records[0].exc_info[1], RuntimeError)


# ----- serve() wiring -------------------------------------------------------


def test_serve_starts_the_resumer_before_binding_and_stops_it_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Boot owns the threads: start before bind, stop before the prep drain.

    Mirrors the ingest worker's wiring pin: importing and mounting never
    touch these seams, only ``serve()`` does, and the stop order is the
    reverse — no new resume is driven once its prep drain is gone. The
    resume worker must receive the SAME attachment service the prep
    drain runs (one settle-check authority) plus the graph, thread
    store, and workspace root the boot resolved.
    """
    import fin.corpus.ingest_storage as ingest_storage
    import fin.host.http as http_module
    import uvicorn

    events: list[str] = []
    graph_stub = object()
    store_stub = object()
    service_stub = object()

    monkeypatch.setattr(http_module, "apply_dotenv", lambda: None)
    monkeypatch.setattr(http_module, "get_attachment_budgets", lambda: None)
    monkeypatch.setattr(
        ingest_storage, "preflight_ingest_storage", lambda: None
    )
    monkeypatch.setattr(http_module, "publish_knowledge_mcp", lambda: None)
    monkeypatch.setattr(http_module, "compile_for_http", lambda: graph_stub)
    monkeypatch.setattr(http_module, "default_thread_store", lambda: store_stub)
    monkeypatch.setattr(http_module, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        http_module, "default_attachment_service", lambda: service_stub
    )
    monkeypatch.setattr(http_module, "start_ingest_worker", lambda: events.append("ingest_start"))
    prep_calls: list[Any] = []
    monkeypatch.setattr(
        http_module,
        "start_attachment_prep_worker",
        lambda service: events.append("prep_start") or prep_calls.append(service),
    )
    resume_kwargs: dict[str, Any] = {}

    def _resume_start(**kwargs: Any) -> None:
        events.append("resume_start")
        resume_kwargs.update(kwargs)

    monkeypatch.setattr(http_module, "start_attachment_resume_worker", _resume_start)
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: events.append("serve")
    )
    monkeypatch.setattr(
        http_module, "stop_attachment_resume_worker", lambda: events.append("resume_stop")
    )
    monkeypatch.setattr(
        http_module, "stop_attachment_prep_worker", lambda: events.append("prep_stop")
    )
    monkeypatch.setattr(
        http_module, "stop_ingest_worker", lambda: events.append("ingest_stop")
    )

    http_module.serve()

    assert events == [
        "ingest_start",
        "prep_start",
        "resume_start",
        "serve",
        "resume_stop",
        "prep_stop",
        "ingest_stop",
    ]
    assert prep_calls == [service_stub]
    assert resume_kwargs == {
        "graph": graph_stub,
        "store": store_stub,
        "service": service_stub,
        "workspace_root": tmp_path,
    }
