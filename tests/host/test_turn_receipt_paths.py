"""Every execution path ends on the one receipt of its logical turn.

A receipt that only some paths write is worse than none: a thread's
history would carry a receipt for the turns that happened to finish
normally and nothing at all for the ones that stopped, timed out or
crashed. This file drives the executor through each of those paths with
a real receipt store attached and asserts what is left behind: one
receipt per logical turn, terminal fields that name how the turn ended,
the executor's own measured stages, and a second finalizer that changes
nothing.

The turn identity under test is the graph's ``run_id``: a continuation,
a resume and a background auto-resume all report under it, which is what
makes "one logical turn, one receipt" an assertion about the code rather
than a claim about a schema.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

from langchain_core.messages import AIMessage, AIMessageChunk

from fin.host.run_budgets import RunBudgets, STAGE_CLEANUP
from fin.host.run_control import RunControl
from fin.host.run_execution import (
    ResumeRunRequest,
    RunExecutor,
    RunOutcome,
    RunRequest,
    RunTechnicalResult,
)
from fin.host.thread_store import InMemoryThreadStore
from fin.host.turn_receipt_store import (
    STAGE_METRIC_FIELDS,
    STAGE_METRIC_KEYS,
    InMemoryReceiptStore,
    TurnReceipt,
)
from fin.host.turn_receipts import TurnReceiptProjector
from fin.host.task.task_runtime_types import (
    INPUT_HISTORY_LIMIT,
    AcceptedInput,
    INPUT_MESSAGE,
    INPUT_REPLY,
    TaskRuntimeState,
)

THREAD_ID = "receipt-path-thread"
RUN_ID = "receipt-path-run"
QUESTION = "What did the quarter report?"
ANSWER = "The quarter reported a figure."


class _TaskFailure(RuntimeError):
    """A refusal Task Runtime raised, carrying its own typed cause."""

    kind = "task_history_failed"


class ScriptedGraph:
    """A stand-in for the compiled graph, deterministic end to end.

    ``stream`` returns a real generator because the executor's release
    path is ``generator.close()``; a plain iterator would hide a run
    that kept the thread's work alive after it ended.
    """

    def __init__(
        self,
        script: list[tuple[str, Any]],
        *,
        raises: BaseException | None = None,
        on_next: Any = None,
    ) -> None:
        self._script = script
        self._raises = raises
        self._on_next = on_next
        self.closed = False
        self.finalized: list[tuple[str, str, str]] = []

    def stream(
        self, input_value: Any, *, config: dict[str, Any], stream_mode: Any
    ) -> Iterator[Any]:
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

    def checkpoint_last_human_text(self, thread_id: str) -> str:
        return QUESTION

    def checkpoint_run_id(self, thread_id: str) -> str:
        return RUN_ID

    def finalize_abandoned_run(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        self.finalized.append((thread_id, status, status_reason))


def _answered_state() -> dict[str, Any]:
    return {
        "messages": [AIMessage(content=ANSWER)],
        "status": "done",
        "answer": ANSWER,
        "citations": [],
        "evidence": [],
    }


def _choice_window_frame() -> tuple[str, Any]:
    return (
        "values",
        {
            "__interrupt__": [
                type(
                    "_Interrupt",
                    (),
                    {"value": {"kind": "choice_window", "options": []}},
                )()
            ]
        },
    )


def _choice_window_graph() -> ScriptedGraph:
    return ScriptedGraph([_choice_window_frame()])


def _attachment_wait_graph() -> ScriptedGraph:
    return ScriptedGraph(
        [
            (
                "values",
                {
                    "__interrupt__": [
                        type(
                            "_Interrupt",
                            (),
                            {
                                "value": {
                                    "kind": "attachment_preparation",
                                    "attachment_ids": ["att-1"],
                                }
                            },
                        )()
                    ]
                },
            )
        ]
    )


def _harness() -> tuple[
    InMemoryThreadStore, TurnReceiptProjector, RunControl
]:
    threads = InMemoryThreadStore()
    threads.create_thread(id=THREAD_ID, title=QUESTION)
    return threads, TurnReceiptProjector(InMemoryReceiptStore(threads)), RunControl()


def _begin(projector: TurnReceiptProjector, input_version: int = 1) -> TurnReceipt:
    return projector.begin(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        accepted_input_version=input_version,
    )


def _executor(
    graph: ScriptedGraph,
    threads: InMemoryThreadStore,
    control: RunControl,
    receipts: TurnReceiptProjector,
    *,
    budgets: RunBudgets | None = None,
    publication_warnings: Any = None,
) -> RunExecutor:
    return RunExecutor(
        graph=graph,
        store=threads,
        run_control=control,
        receipts=receipts,
        budgets=budgets,
        publication_warnings=publication_warnings,
    )


def _send(
    executor: RunExecutor, *, run_id: str = RUN_ID
) -> RunOutcome:
    return _consume(
        executor.send(
            RunRequest(
                thread_id=THREAD_ID,
                run_id=run_id,
                question=QUESTION,
                input_value={"messages": []},
                config={"configurable": {"thread_id": THREAD_ID, "run_id": run_id}},
                thinking_type="adaptive",
            )
        )
    )


def _resume(executor: RunExecutor, *, resume_input: Any = None) -> RunOutcome:
    return _consume(
        executor.resume(
            ResumeRunRequest(
                thread_id=THREAD_ID,
                run_id=RUN_ID,
                resume_input=resume_input,
                resume_config={
                    "configurable": {"thread_id": THREAD_ID, "run_id": RUN_ID}
                },
            )
        )
    )


def _consume(stream: Any) -> RunOutcome:
    outcome: RunOutcome | None = None
    while True:
        try:
            next(stream)
        except StopIteration as stop:
            outcome = stop.value
            break
    assert outcome is not None
    return outcome


def _one_receipt(projector: TurnReceiptProjector) -> TurnReceipt:
    """The single receipt of this turn, or the case fails here.

    Read by enumeration, not by key: a key-addressed read answers the
    same whether the store holds one receipt for the turn or several, so
    it cannot see a stretch that filed itself under a second ``turn_id``.
    """
    receipts = projector.store.list_receipts(THREAD_ID)
    assert len(receipts) == 1, (
        f"expected exactly one receipt for the turn, got "
        f"{[r.turn_id for r in receipts]}"
    )
    receipt = receipts[0]
    assert receipt.turn_id == RUN_ID
    return receipt


def _assert_carries_all_twelve_stages(receipt: TurnReceipt) -> None:
    assert tuple(receipt.stage_metrics) == STAGE_METRIC_KEYS
    for entry in receipt.stage_metrics.values():
        assert tuple(entry) == STAGE_METRIC_FIELDS


# ----- start to completion -----


def test_a_finished_turn_leaves_one_receipt_naming_its_end() -> None:
    threads, receipts, control = _harness()
    started = _begin(receipts)

    graph = ScriptedGraph([("values", _answered_state())])
    outcome = _send(_executor(graph, threads, control, receipts))

    assert outcome.technical_result is RunTechnicalResult.DONE
    receipt = _one_receipt(receipts)
    assert receipt.turn_id == RUN_ID
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "done"
    assert receipt.semantic_result == "answered"
    assert receipt.terminal_at is not None
    assert receipt.started_at == started.started_at
    assert [row.id for row in threads.list_runs(THREAD_ID)] == [RUN_ID]
    _assert_carries_all_twelve_stages(receipt)


# ----- the model's own keys -----


def _call(node: str, *, tools: bool = False) -> tuple[str, Any]:
    """One chat-model call as the graph's message stream reports it.

    Chunks are the per-token deltas and are not calls; the aggregated
    ``AIMessage`` that ends one is, which is the whole discriminator.
    """
    message = AIMessage(content="x")
    if tools:
        message.tool_calls = [  # type: ignore[attr-defined]
            {"name": "retrieve", "args": {}, "id": "c1", "type": "tool_call"}
        ]
    return ("messages", (message, {"langgraph_node": node}))


def _chunk(node: str) -> tuple[str, Any]:
    return ("messages", (AIMessageChunk(content="x"), {"langgraph_node": node}))


def test_each_model_call_is_counted_once_and_landed_in_one_breakdown() -> None:
    """One invocation, one bucket, and the whole is the sum of the parts.

    The inclusive ``model`` key is what makes a double addition visible:
    if a call were counted under both the answer author and a breakdown,
    the parts would no longer add to the whole.
    """
    threads, receipts, control = _harness()
    _begin(receipts)

    graph = ScriptedGraph(
        [
            _chunk("model"),
            _call("reconcile_task"),
            _call("model", tools=True),
            _call("model"),
            _call("dialogue"),
            ("values", _answered_state()),
        ]
    )
    outcome = _send(_executor(graph, threads, control, receipts))

    assert outcome.model_calls == {
        "runtime_reconciliation": 1,
        "tool_planning": 1,
        "grounded_decision": 1,
        "other_model": 1,
    }
    receipt = _one_receipt(receipts)
    parts = ("runtime_reconciliation", "grounded_decision", "tool_planning", "other_model")
    for key in parts:
        assert receipt.stage_metrics[key]["calls"] == 1
    assert receipt.stage_metrics["model"]["calls"] == sum(
        receipt.stage_metrics[key]["calls"] for key in parts
    )
    assert receipt.stage_metrics["model"]["calls"] == 4
    # The one chunk in the script is a token of the first call, not a
    # call of its own: four aggregated messages, four calls.


def test_a_model_call_nothing_could_attribute_still_counts_once() -> None:
    """The inclusive total is every call the boundary saw.

    A call the stream reported without naming its node cannot be placed
    in the reconciliation or the answer author, but dropping it would
    make ``model`` a smaller number than the run really spent, with
    nothing on the row to say so.
    """
    threads, receipts, control = _harness()
    _begin(receipts)

    graph = ScriptedGraph(
        [("messages", (AIMessage(content="x"), {})), ("values", _answered_state())]
    )
    outcome = _send(_executor(graph, threads, control, receipts))

    assert outcome.model_calls == {"other_model": 1}
    receipt = _one_receipt(receipts)
    assert receipt.stage_metrics["model"]["calls"] == 1
    assert receipt.stage_metrics["other_model"]["calls"] == 1


def test_the_model_keys_are_open_because_nothing_times_those_calls() -> None:
    """Counting the invocations is not measuring how long they took.

    The counts are durably observed; the elapsed time is not observed at
    all, so the stage reports itself unfinished rather than complete at a
    zero it never measured.
    """
    threads, receipts, control = _harness()
    _begin(receipts)

    graph = ScriptedGraph([_call("model"), ("values", _answered_state())])
    _send(_executor(graph, threads, control, receipts))

    receipt = _one_receipt(receipts)
    assert receipt.stage_metrics["model"] == {
        "calls": 1,
        "elapsed_ms": 0,
        "complete": False,
    }
    # An open stage nobody timed must not open the turn's accounting.
    assert receipt.accounting_complete is True


def test_a_pause_and_its_continuation_report_one_turns_calls() -> None:
    """A continuation adds its calls to the prefix's, as it does elsewhere."""
    threads, receipts, control = _harness()
    _begin(receipts)

    _send(
        _executor(
            ScriptedGraph([_call("model", tools=True), _choice_window_frame()]),
            threads,
            control,
            receipts,
        )
    )
    paused = _one_receipt(receipts)
    assert paused.stage_metrics["tool_planning"]["calls"] == 1

    _resume(
        _executor(
            ScriptedGraph([_call("model"), ("values", _answered_state())]),
            threads,
            control,
            receipts,
        )
    )

    finished = _one_receipt(receipts)
    assert finished.stage_metrics["tool_planning"]["calls"] == 1
    assert finished.stage_metrics["grounded_decision"]["calls"] == 1
    assert finished.stage_metrics["model"]["calls"] == 2


def test_the_receipt_reports_the_thread_s_unresolved_publications() -> None:
    """A turn that ran over an unsearchable document says so on its row.

    The count was a standing zero, which claimed no warnings rather than
    no measurement: nothing ever asked the thread's documents how many of
    them were still unsearchable. The executor asks at the commit, so a
    publication that settled while the turn ran is not warned about.
    """
    threads, receipts, control = _harness()
    _begin(receipts)
    asked: list[str] = []

    def unresolved(thread_id: str) -> int:
        asked.append(thread_id)
        return 3

    graph = ScriptedGraph([("values", _answered_state())])
    _send(
        _executor(
            graph,
            threads,
            control,
            receipts,
            publication_warnings=unresolved,
        )
    )

    receipt = _one_receipt(receipts)
    assert receipt.catalog_warning_count == 3
    assert asked == [THREAD_ID]


def test_a_run_with_no_attachment_lane_writes_no_warning_count() -> None:
    """No store to ask is not a clean thread; the column stays as it was.

    Reporting zero here would be a measurement of a store that does not
    exist, and would silently contradict a count an earlier stretch of
    the same turn established against a real one.
    """
    threads, receipts, control = _harness()
    _begin(receipts)

    graph = ScriptedGraph([("values", _answered_state())])
    _send(_executor(graph, threads, control, receipts))

    receipt = _one_receipt(receipts)
    assert receipt.catalog_warning_count == 0


def test_a_paused_turn_reports_what_it_saw_and_the_continuation_keeps_it() -> None:
    """The pause measures; a continuation that cannot measures nothing.

    Asking again is only possible where a store is wired, so a stretch
    that cannot ask must not overwrite the prefix's answer with a zero
    it did not observe.
    """
    threads, receipts, control = _harness()
    _begin(receipts)

    def unresolved(thread_id: str) -> int:
        return 2

    _send(
        _executor(
            _choice_window_graph(),
            threads,
            control,
            receipts,
            publication_warnings=unresolved,
        )
    )

    paused = _one_receipt(receipts)
    assert paused.catalog_warning_count == 2

    _resume(_executor(ScriptedGraph([("values", _answered_state())]), threads, control, receipts))

    finished = _one_receipt(receipts)
    assert finished.catalog_warning_count == 2


def test_the_receipt_measures_the_run_that_produced_it() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)

    outcome = _send(
        _executor(
            ScriptedGraph([("values", _answered_state())]), threads, control, receipts
        )
    )

    receipt = _one_receipt(receipts)
    measured = {stage.stage: stage for stage in outcome.stage_metrics}
    # The release is the one executor stage that is one of the receipt's
    # twelve, and it is reported as the run's own clock observed it.
    assert receipt.stage_metrics["cleanup"]["calls"] == 1
    assert receipt.stage_metrics["cleanup"]["elapsed_ms"] == (
        measured[STAGE_CLEANUP].elapsed_ms
    )
    assert receipt.stage_metrics["cleanup"]["complete"] is (
        measured[STAGE_CLEANUP].complete
    )
    # The streamed stretch is the turn's work, not one model invocation,
    # so it stays on the work fields and out of the model keys.
    assert receipt.work_elapsed_ms == next(
        stage.elapsed_ms
        for stage in outcome.stage_metrics
        if stage.stage == "execute"
    )
    # A stage this executor does not clock stays unknown rather than
    # claiming an absence nobody observed.
    assert receipt.stage_metrics["model"] == {
        "calls": 0,
        "elapsed_ms": 0,
        "complete": False,
    }


# ----- pause, reply, completion -----


def test_a_pause_and_its_reply_end_as_one_receipt_and_one_run() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)
    executor = _executor(
        _choice_window_graph(), threads, control, receipts
    )

    paused = _send(executor)

    assert paused.choice_window is not None
    receipt = _one_receipt(receipts)
    assert receipt.execution_state == "paused"
    assert receipt.semantic_result == "clarify"
    assert receipt.technical_result is None
    assert threads.list_runs(THREAD_ID) == []

    counted = receipts.accepted_reply(
        thread_id=THREAD_ID, turn_id=RUN_ID, input_version=2
    )
    assert counted is not None
    assert counted.accepted_input_count == 2
    assert counted.accepted_input_version == 2
    assert counted.execution_state == "running"
    assert counted.semantic_result is None

    _resume(
        _executor(
            ScriptedGraph([("values", _answered_state())]), threads, control, receipts
        )
    )

    final = _one_receipt(receipts)
    assert final.turn_id == RUN_ID
    assert final.execution_state == "terminal"
    assert final.technical_result == "done"
    assert final.semantic_result == "answered"
    # One turn: the clarification did not mint a second identity.
    assert [row.id for row in threads.list_runs(THREAD_ID)] == [RUN_ID]


def test_the_paused_prefixs_work_and_the_remainders_add_up() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)
    prefix = _send(
        _executor(_choice_window_graph(), threads, control, receipts)
    )
    receipts.accepted_reply(
        thread_id=THREAD_ID, turn_id=RUN_ID, input_version=2
    )
    remainder = _resume(
        _executor(
            ScriptedGraph([("values", _answered_state())]), threads, control, receipts
        )
    )

    final = _one_receipt(receipts)
    stretches = sum(
        stage.elapsed_ms
        for stage in (*prefix.stage_metrics, *remainder.stage_metrics)
        if stage.stage == "execute"
    )
    assert final.work_elapsed_ms == stretches
    assert final.accounting_complete is True


def test_bodyless_resume_reconciles_receipt_inputs_from_checkpoint_history() -> None:
    """A post-crash pause projects accepted inputs from durable task history."""
    threads = InMemoryThreadStore()
    threads.create_thread(id=THREAD_ID, title=QUESTION)
    receipt_store = InMemoryReceiptStore(threads)
    first_projector = TurnReceiptProjector(receipt_store)
    first = first_projector.begin(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        accepted_input_version=1,
    )
    first_projector.accepted_reply(
        thread_id=THREAD_ID, turn_id=RUN_ID, input_version=2
    )

    durable_checkpoint = TaskRuntimeState(
        input_version=1,
        input_history=(
            AcceptedInput(
                input_version=1,
                kind=INPUT_MESSAGE,
                payload=QUESTION,
                provenance="workspace_send",
                run_id=RUN_ID,
            ),
        ),
    )

    class CheckpointGraph(ScriptedGraph):
        def task_runtime_state(self, _thread_id: str) -> TaskRuntimeState:
            return durable_checkpoint

    rebuilt_projector = TurnReceiptProjector(receipt_store)
    outcome = _resume(
        _executor(
            CheckpointGraph([_choice_window_frame()]),
            threads,
            RunControl(),
            rebuilt_projector,
        )
    )

    assert outcome.choice_window is not None
    recovered = _one_receipt(rebuilt_projector)
    assert recovered.execution_state == "paused"
    assert recovered.accepted_input_count == 1
    assert recovered.accepted_input_version == 1
    assert recovered.started_at == first.started_at
    assert recovered.turn_id == RUN_ID


def test_resume_projects_new_checkpoint_inputs_after_a_stale_checkpoint() -> None:
    """A continuation accepted during resume survives pre-run reconciliation."""
    threads, receipts, control = _harness()
    _begin(receipts, input_version=1)
    receipts.accepted_reply(
        thread_id=THREAD_ID, turn_id=RUN_ID, input_version=2
    )
    checkpoint_before_resume = TaskRuntimeState(
        input_version=1,
        input_history=(
            AcceptedInput(1, INPUT_MESSAGE, QUESTION, "workspace_send", run_id=RUN_ID),
        ),
    )
    checkpoint_after_resume = TaskRuntimeState(
        input_version=2,
        input_history=(
            AcceptedInput(1, INPUT_MESSAGE, QUESTION, "workspace_send", run_id=RUN_ID),
            AcceptedInput(2, INPUT_MESSAGE, "clarification", "workspace_send", run_id=RUN_ID),
        ),
    )

    class AdvancingCheckpointGraph(ScriptedGraph):
        def __init__(self) -> None:
            super().__init__([_choice_window_frame()])
            self._checkpoint_reads = 0

        def task_runtime_state(self, _thread_id: str) -> TaskRuntimeState:
            self._checkpoint_reads += 1
            if self._checkpoint_reads == 1:
                return checkpoint_before_resume
            return checkpoint_after_resume

    graph = AdvancingCheckpointGraph()
    outcome = _resume(_executor(graph, threads, control, receipts))

    assert outcome.choice_window is not None
    recovered = _one_receipt(receipts)
    assert recovered.accepted_input_count == 2
    assert recovered.accepted_input_version == 2
    assert graph._checkpoint_reads == 2


def test_checkpoint_input_projection_is_idempotent_and_bounded() -> None:
    threads, receipts, _control = _harness()
    first = _begin(receipts, input_version=1)
    complete_history = (
        AcceptedInput(1, INPUT_MESSAGE, QUESTION, "workspace_send", run_id=RUN_ID),
        AcceptedInput(3, INPUT_MESSAGE, "follow-up", "workspace_send", run_id=RUN_ID),
    )

    reconciled = receipts.reconcile_accepted_inputs(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        input_history=complete_history,
    )
    assert reconciled is not None
    assert reconciled.accepted_input_count == 2
    assert reconciled.accepted_input_version == 3
    assert reconciled.started_at == first.started_at

    repeated = receipts.reconcile_accepted_inputs(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        input_history=complete_history,
    )
    assert repeated is not None
    assert repeated.revision == reconciled.revision

    bounded_history = tuple(
        AcceptedInput(
            input_version=version,
            kind=(
                INPUT_REPLY
                if version in {301, 500}
                else INPUT_MESSAGE
            ),
            payload=f"input-{version}",
            provenance="workspace_send",
            run_id=RUN_ID if version in {301, 500} else "other-turn",
        )
        for version in range(501 - INPUT_HISTORY_LIMIT, 501)
    )
    bounded = receipts.reconcile_accepted_inputs(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        input_history=bounded_history,
    )
    assert bounded is not None
    assert bounded.accepted_input_count == 4
    assert bounded.accepted_input_version == 500
    assert bounded.accounting_complete is False
    bounded_again = receipts.reconcile_accepted_inputs(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        input_history=bounded_history,
    )
    assert bounded_again is not None
    assert bounded_again.revision == bounded.revision


def test_an_attachment_wait_pauses_the_same_turn_without_a_result() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)

    outcome = _send(
        _executor(_attachment_wait_graph(), threads, control, receipts)
    )

    assert outcome.attachment_wait is not None
    receipt = _one_receipt(receipts)
    assert receipt.execution_state == "paused"
    # Waiting for a document is not a semantic verdict about the turn.
    assert receipt.semantic_result is None
    assert threads.list_runs(THREAD_ID) == []


def test_a_redrawn_window_does_not_become_a_second_pause() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)
    _send(_executor(_choice_window_graph(), threads, control, receipts))
    first = _one_receipt(receipts)

    _resume(
        _executor(_choice_window_graph(), threads, control, receipts)
    )

    again = _one_receipt(receipts)
    assert again.turn_id == first.turn_id
    assert again.execution_state == "paused"
    assert threads.list_runs(THREAD_ID) == []


# ----- stop, timeout and failure -----


def test_a_stopped_turn_leaves_one_receipt_naming_the_operator() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)

    outcome = _send(
        _executor(
            ScriptedGraph(
                [("values", _answered_state())],
                on_next=lambda index: control.request_stop(THREAD_ID),
            ),
            threads,
            control,
            receipts,
        )
    )

    assert outcome.technical_result is RunTechnicalResult.STOPPED
    receipt = _one_receipt(receipts)
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "stopped"
    assert receipt.cancellation_reason == "user_stop"
    assert receipt.terminal_at is not None
    assert [row.id for row in threads.list_runs(THREAD_ID)] == [RUN_ID]


def test_a_timed_out_turn_leaves_one_receipt_naming_the_deadline() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)
    stalled = ScriptedGraph(
        [("values", _answered_state())],
        on_next=lambda index: time.sleep(0.2) if index == 0 else None,
    )

    outcome = _send(
        _executor(
            stalled,
            threads,
            control,
            receipts,
            budgets=RunBudgets(execute_stage_s=0.05),
        )
    )

    assert outcome.technical_result is RunTechnicalResult.TIMEOUT
    receipt = _one_receipt(receipts)
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "timeout"
    assert receipt.failure_kind == "model_timeout"
    assert receipt.cancellation_reason == "phase_deadline"
    # An interrupted turn keeps the lower bound it observed and says so.
    assert receipt.accounting_complete is False
    assert [row.id for row in threads.list_runs(THREAD_ID)] == [RUN_ID]


def test_a_mid_flight_failure_leaves_one_receipt_naming_the_cause() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)

    outcome = _send(
        _executor(
            ScriptedGraph([], raises=RuntimeError("provider exploded")),
            threads,
            control,
            receipts,
        )
    )

    assert outcome.technical_result is RunTechnicalResult.FAILED
    receipt = _one_receipt(receipts)
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "failed"
    # An unclassifiable raise is the generic bucket, not a named cause.
    assert receipt.failure_kind == "internal"
    # A failed turn established no answer, so it claims none.
    assert receipt.semantic_result is None
    assert [row.id for row in threads.list_runs(THREAD_ID)] == [RUN_ID]


def test_a_typed_refusal_keeps_its_own_token_on_the_receipt() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)

    _send(
        _executor(ScriptedGraph([], raises=_TaskFailure("no history")),
                  threads, control, receipts)
    )

    receipt = _one_receipt(receipts)
    assert receipt.technical_result == "failed"
    assert receipt.failure_kind == "task_history_failed"


# ----- resume, auto-resume and recovery -----


def test_a_continuation_after_a_pause_never_mints_a_second_receipt() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)
    _send(_executor(_choice_window_graph(), threads, control, receipts))
    paused = _one_receipt(receipts)

    _resume(
        _executor(
            ScriptedGraph([("values", _answered_state())]), threads, control, receipts
        )
    )

    final = _one_receipt(receipts)
    assert final.started_at == paused.started_at
    assert final.revision > paused.revision
    assert len(threads.list_runs(THREAD_ID)) == 1


def test_a_restarted_process_resume_closes_the_turn_it_reopened() -> None:
    threads, receipts, _control = _harness()
    _begin(receipts)
    # The run paused and wrote nothing; the process then went away, so
    # the executor that reopens the checkpoint holds no memory of it.
    _send(_executor(_choice_window_graph(), threads, _control, receipts))
    paused = _one_receipt(receipts)

    recovered = _resume(
        _executor(
            ScriptedGraph([("values", _answered_state())]),
            threads,
            RunControl(),
            TurnReceiptProjector(receipts.store),
        )
    )

    assert recovered.technical_result is RunTechnicalResult.DONE
    final = _one_receipt(receipts)
    assert final.turn_id == paused.turn_id
    # The original start survives the restart: a recovery reports under
    # the turn it recovered, not under a turn it began.
    assert final.started_at == paused.started_at
    assert final.technical_result == "done"
    assert len(threads.list_runs(THREAD_ID)) == 1


# ----- a second finalizer -----


def test_a_second_finalizer_writes_no_row_and_moves_no_revision() -> None:
    threads, receipts, control = _harness()
    _begin(receipts)
    _send(
        _executor(
            ScriptedGraph([("values", _answered_state())]), threads, control, receipts
        )
    )
    first = _one_receipt(receipts)

    _send(
        _executor(
            ScriptedGraph([("values", _answered_state())]), threads, control, receipts
        )
    )

    assert _one_receipt(receipts) == first
    assert len(threads.list_runs(THREAD_ID)) == 1


def test_a_turn_without_a_receipt_still_leaves_its_history() -> None:
    threads, _receipts, control = _harness()

    _send(
        _bare_executor(
            ScriptedGraph([("values", _answered_state())]), threads, control
        )
    )

    assert [row.id for row in threads.list_runs(THREAD_ID)] == [RUN_ID]


def _bare_executor(
    graph: ScriptedGraph, threads: InMemoryThreadStore, control: RunControl
) -> RunExecutor:
    """An executor with no receipt wired, as every pre-receipt caller builds it."""
    return RunExecutor(graph=graph, store=threads, run_control=control)


def test_a_stop_on_an_unwired_executor_still_persists_its_row() -> None:
    threads, _receipts, control = _harness()

    _send(
        _bare_executor(
            ScriptedGraph(
                [("values", _answered_state())],
                on_next=lambda index: control.request_stop(THREAD_ID),
            ),
            threads,
            control,
        )
    )

    assert [row.status_reason for row in threads.list_runs(THREAD_ID)] == ["stopped"]
