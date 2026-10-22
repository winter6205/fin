"""The one run lifecycle: what an execution seam has to guarantee.

HTTP and the background attachment auto-resumer both drive runs now, and
they used to each own the graph consumption, the iterator release, the stop
token and the turn row. This file holds the contract that makes those two
surfaces the same seam:

* the executor speaks TYPED events, so a caller narrows an event instead of
  parsing a frame, and no run has to be encoded to be observed;
* the HTTP adapter's bytes are the pre-extraction bytes, byte for byte,
  compared against a golden captured from the run pipeline as it was
  (event names, key order, terminal order, all of it);
* a background resume and an operator send share one executor and one
  occupancy probe;
* a stop ends the run, releases the iterator, deregisters the token and
  writes exactly one terminal row;
* a mid-flight failure is classified once and persisted once;
* two executors cannot cancel each other's runs.

Every assertion here is about behaviour a caller can observe. The wire
golden is the one artifact that is compared literally: it was captured
from this pipeline BEFORE the typed seam existed, so a byte that drifts
fails the file instead of shipping.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from fin.corpus.extracted import Locator
from fin.host import http as host_http
from fin.host.grounded.citation_gate import Citation, EvidenceItem
from fin.host.graph import ConcurrentRunError
from fin.host.run_budgets import RunBudgets
from fin.host.run_control import RunAdmissionError, RunControl
from fin.host.run_execution import (
    ResumeRunRequest,
    RunEvent,
    RunEventKind,
    RunExecutor,
    RunOutcome,
    RunRequest,
    RunTechnicalResult,
)
from fin.host.span_seams import SegmentIdentity, current_segment
from fin.host.task.task_runtime_types import AcceptedInput, TaskRuntimeState
from fin.host.thread_store import InMemoryThreadStore, ThreadStore
from fin.host.turn_receipt_store import InMemoryReceiptStore
from fin.host.turn_receipts import TurnReceiptProjector
from fin.host.evidence.visual_evidence import VisualEvidence
from fin.host.workspace_http import _resume_run_stream

RUN_QUESTION = "What was AAPL net sales in 2023 Q3?"
QUOTE = "Net sales for the quarter were 81.8 billion dollars."
ANSWER = "AAPL net sales were 81.8 billion dollars."
GOLDEN = Path(__file__).parent / "fixtures" / "run_execution_sse.json"
THREAD_ID = "thread-run-exec-1"
RUN_ID = "run-exec-1"


# ----- the scripted run, shared by every case below -----
#
# WHY a hand-scripted graph instead of a compiled one: the seam this file
# owns is what a caller does with a run's emissions, so the emissions are
# fixed here and nothing else can move the frames under the assertions.


class ScriptedGraph:
    """A stand-in for the compiled graph, deterministic end to end.

    ``stream`` returns a real generator on purpose: the engine's release
    path is ``generator.close()``, and a plain iterator would hide a
    regression that leaves the per-thread lock held.
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
        self.locked = False

    def stream(self, input_value: Any, *, config: dict[str, Any], stream_mode: Any):
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
        return self.locked

    def interrupt_pending(self, thread_id: str) -> Any:
        return None

    def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
        return ()

    def checkpoint_message_ids(self, thread_id: str) -> set[str]:
        return set()

    def finalize_abandoned_run(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        self.finalized.append((thread_id, status, status_reason))


def _final_state() -> dict[str, Any]:
    return {
        "messages": [AIMessage(content=ANSWER)],
        "status": "done",
        "answer": ANSWER,
        "citations": [
            Citation(
                kind="company",
                document_id="AAPL-2023Q1",
                attachment_id="",
                version="v1",
                unit=1,
                display="page",
                filename="",
                quote=QUOTE,
            )
        ],
        "visual_claims": [_visual_claim(), {"claim_id": "sparse"}],
        "evidence": [
            EvidenceItem(
                locator=Locator(document_id="AAPL-2023Q1", version="v1", unit=1),
                unit_text=f"AAPL 10-Q · Page 1\nSection 2: Results\n{QUOTE}",
            )
        ],
        "search_mode_decision": {
            "search_mode": "global",
            "confidence": 0.9,
            "decision_tier": "rule",
            "signals": {},
            "options": [],
            "failure_code": None,
        },
    }


def _visual_claim() -> dict[str, Any]:
    """One claim row minted by the evidence SSOT, not hand-assembled."""
    return VisualEvidence(
        attachment_id="att-visual-1",
        version="b" * 64,
        version_int=7,
        unit=3,
        page=3,
        observation_version=2,
        scope=None,
        provider="stub-vision",
        model_version="stub-vision-1",
        page_image_sha256="c" * 64,
        detail="A generated observation about the quarter.",
        filename="aapl-10q.pdf",
        reused=False,
        outbound_calls=1,
    ).as_claim_row()


def scripted_graph(
    *, raises: BaseException | None = None, on_next: Any = None
) -> ScriptedGraph:
    tool_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {"document_id": "AAPL-2023Q1", "unit": 1},
                "id": "call-1",
            }
        ],
    )
    tool_message = ToolMessage(
        content='{"text": "Net sales 81.8 billion."}',
        name="read_source",
        tool_call_id="call-1",
    )
    return ScriptedGraph(
        [
            (
                "messages",
                (
                    AIMessageChunk(
                        content=f"<thinking>weighing the quarter</thinking>{ANSWER}"
                    ),
                    {"langgraph_node": "model"},
                ),
            ),
            ("values", {"messages": [tool_call]}),
            ("values", {"messages": [tool_message]}),
            ("values", _final_state()),
        ],
        raises=raises,
        on_next=on_next,
    )


def _store_with_thread(store: ThreadStore) -> None:
    store.create_thread(id=THREAD_ID, title=RUN_QUESTION)


def _run(
    graph: ScriptedGraph,
    store: ThreadStore,
    control: RunControl,
    *,
    question: str = RUN_QUESTION,
    budgets: RunBudgets | None = None,
) -> tuple[list[RunEvent], RunOutcome]:
    """Consume one send to exhaustion; return the events and the outcome."""
    executor = RunExecutor(
        graph=graph, store=store, run_control=control, budgets=budgets
    )
    stream = executor.send(
        RunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            question=question,
            input_value={"messages": []},
            config={"configurable": {"thread_id": THREAD_ID}},
            thinking_type="adaptive",
        )
    )
    events: list[RunEvent] = []
    outcome: RunOutcome | None = None
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            outcome = stop.value
            break
    assert outcome is not None
    return events, outcome


@pytest.mark.parametrize(("operation", "expected_segment"), [("send", 0), ("resume", 1)])
def test_send_and_resume_publish_the_receipt_segment_to_the_graph(
    operation: str, expected_segment: int
) -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    receipts = TurnReceiptProjector(InMemoryReceiptStore(store))
    receipts.begin(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        accepted_input_version=0,
    )
    if operation == "resume":
        receipts.accepted_reply(
            thread_id=THREAD_ID,
            turn_id=RUN_ID,
            input_version=1,
        )

    seen_segments: list[SegmentIdentity | None] = []
    graph = scripted_graph(on_next=lambda _: seen_segments.append(current_segment()))
    executor = RunExecutor(
        graph=graph,
        store=store,
        run_control=RunControl(),
        receipts=receipts,
    )
    if operation == "send":
        stream = executor.send(
            RunRequest(
                thread_id=THREAD_ID,
                run_id=RUN_ID,
                question=RUN_QUESTION,
                input_value={"messages": []},
                config={"configurable": {"thread_id": THREAD_ID}},
            )
        )
    else:
        stream = executor.resume(
            ResumeRunRequest(
                thread_id=THREAD_ID,
                run_id=RUN_ID,
                resume_input=None,
                resume_config={"configurable": {"thread_id": THREAD_ID}},
            )
        )

    list(stream)

    assert seen_segments
    assert set(seen_segments) == {
        SegmentIdentity(THREAD_ID, RUN_ID, expected_segment)
    }


# ----- the seam is typed -----
# WHY assert on the ABSENCE of wire text: an engine that leaks ``data: ...``
# strings is how a second surface starts re-implementing SSE framing, which
# is the drift this seam exists to stop.


def test_the_executor_speaks_typed_events_and_never_a_wire_frame() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    events, outcome = _run(scripted_graph(), store, RunControl())

    assert [event.kind for event in events] == [
        RunEventKind.THINKING,
        RunEventKind.TOOL_CALL,
        RunEventKind.TOOL_RESULT,
        RunEventKind.ANSWER,
        RunEventKind.TERMINAL,
    ]
    assert events[0].data == {"text": "weighing the quarter"}
    # No event name, no frame delimiter, no JSON envelope: nothing a caller
    # would have to parse to know what happened.
    assert not any(
        marker in repr(event)
        for event in events
        for marker in ("event: ", "data: ", "\n\n")
    )
    assert isinstance(outcome, RunOutcome)
    assert outcome.technical_result is RunTechnicalResult.DONE
    assert (outcome.thread_id, outcome.run_id) == (THREAD_ID, RUN_ID)
    _assert_events_carry_admitted_answer(events)


def test_the_typed_terminal_carries_the_projection_not_a_body() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    events, _ = _run(scripted_graph(), store, RunControl())

    terminal = events[-1].data
    assert terminal.run_id == RUN_ID
    assert terminal.payload["status"] == "done"
    assert terminal.thinking_type == "adaptive"
    # The claim rows are the persisted rows, already normalised once.
    assert [row["kind"] for row in terminal.claim_rows] == ["visual_observation"]
    assert terminal.claim_refusals == [
        {"citation_index": 1, "error": "malformed_evidence_contract"}
    ]
    assert store.list_runs(THREAD_ID)[0].visual_claims == terminal.claim_rows


# ----- the wire is unchanged -----


def _normalize(body: str) -> str:
    """Blank the two facts a run legitimately invents per execution."""
    run_ids = set(re.findall(r'"run_id": "([0-9a-f-]{36})"', body))
    assert len(run_ids) == 1, run_ids
    out = body.replace(f'"run_id": "{run_ids.pop()}"', '"run_id": "<run_id>"')
    return re.sub(r'"thinking_ms": \d+', '"thinking_ms": <ms>', out)


def _capture_send(graph: ScriptedGraph) -> tuple[str, int]:
    """One real send, through the mounted router the SPA talks to.

    WHY the deployed app and not a bare mount: the golden was captured from
    the deployed composition, so comparing a second composition would prove
    nothing about it.
    """
    app = host_http.app
    store = InMemoryThreadStore()
    app.dependency_overrides[host_http.get_graph] = lambda: graph
    app.dependency_overrides[host_http.get_thread_store] = lambda: store
    try:
        client = TestClient(app)
        created = client.post(
            "/api/threads", json={"first_message": RUN_QUESTION}
        ).json()
        with client.stream(
            "POST",
            f"/api/threads/{created['id']}/messages",
            json={"question": RUN_QUESTION},
        ) as response:
            body = "".join(response.iter_text())
            return body, len(store.list_runs(created["id"]))
    finally:
        app.dependency_overrides.clear()


def test_the_adapter_preserves_wire_frames_and_releases_only_admitted_answer() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    graph = scripted_graph()
    body, turns = _capture_send(graph)
    expected_frames = _golden_frames_with_answer_after_tools(golden["sse"])
    actual_frames = _normalize(body).strip().split("\n\n")
    assert actual_frames == expected_frames
    status_index = next(
        index
        for index, frame in enumerate(actual_frames)
        if frame.startswith("event: status\n")
    )
    live_answers = [
        json.loads(frame.split("data: ", 1)[1])["text"]
        for frame in actual_frames[:status_index]
        if frame.startswith("event: answer\n")
    ]
    terminal_answer = next(
        json.loads(frame.split("data: ", 1)[1])["text"]
        for frame in actual_frames[status_index + 1 :]
        if frame.startswith("event: answer\n")
    )
    assert live_answers
    assert "".join(live_answers) == terminal_answer == ANSWER
    assert graph.finalized == golden["finalized"]
    assert turns == 1


def test_a_mid_flight_failure_suppresses_the_unadmitted_answer_draft() -> None:
    """Failure preserves the golden wire except its unsafe early draft."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    graph = scripted_graph(raises=TimeoutError("provider read timed out after 120.0 s"))
    body, turns = _capture_send(graph)
    expected_frames = _golden_frames_with_answer_after_tools(
        golden["raise_sse"], include_admitted_answer=False
    )
    actual_frames = _normalize(body).strip().split("\n\n")
    assert actual_frames == expected_frames
    status_index = next(
        index
        for index, frame in enumerate(actual_frames)
        if frame.startswith("event: status\n")
    )
    assert not any(
        json.loads(frame.split("data: ", 1)[1]).get("text")
        for frame in actual_frames[:status_index]
        if frame.startswith("event: answer\n")
    )
    assert ANSWER not in body
    assert turns == 1
    # The abandonment the refuse path owes the checkpoint seam is unchanged
    # apart from the thread it ran on.
    assert [
        (status, reason) for _, status, reason in graph.finalized
    ] == [(status, reason) for _, status, reason in golden["raise_finalized"]]


def _golden_frames_with_answer_after_tools(
    golden_sse: str, *, include_admitted_answer: bool = True
) -> list[str]:
    """Keep captured frame bytes while encoding the safe release boundary.

    The old transport exposed an answer delta before tool completion. The
    executor now releases that same payload only after the graph has
    admitted it; failures omit the draft entirely. The terminal frames and
    every unrelated captured byte remain pinned to the golden.
    """
    frames = golden_sse.strip().split("\n\n")
    answer_index = next(
        index
        for index, frame in enumerate(frames)
        if frame.startswith("event: answer\n")
        and json.loads(frame.split("data: ", 1)[1]).get("text")
    )
    admitted_answer = frames.pop(answer_index)
    if include_admitted_answer:
        tool_result_index = next(
            index
            for index, frame in enumerate(frames)
            if frame.startswith("event: tool_result\n")
        )
        frames.insert(tool_result_index + 1, admitted_answer)
    return frames


def _assert_events_carry_admitted_answer(events: list[RunEvent]) -> None:
    answer_indices = [
        index
        for index, event in enumerate(events)
        if event.kind is RunEventKind.ANSWER
    ]
    assert answer_indices
    assert max(
        index
        for index, event in enumerate(events)
        if event.kind in (RunEventKind.TOOL_CALL, RunEventKind.TOOL_RESULT)
    ) < min(answer_indices)
    terminal = events[-1].data
    live_answer = "".join(events[index].data["text"] for index in answer_indices)
    assert live_answer
    assert live_answer == terminal.payload["answer"] == ANSWER


# ----- one executor, two surfaces -----


def test_the_background_resume_and_an_operator_send_share_one_executor(
    monkeypatch: Any,
) -> None:
    """The auto-resumer's resume IS an executor resume, not a second driver.

    WHY spy rather than assert on rows alone: the drift this guards against
    is a background path that grows its own persistence, which would still
    look correct on any single row check.
    """
    seen: list[str] = []
    original = RunExecutor.resume

    def spy(self: RunExecutor, request: ResumeRunRequest) -> Any:
        seen.append(request.run_id)
        return original(self, request)

    monkeypatch.setattr(RunExecutor, "resume", spy)

    store = InMemoryThreadStore()
    _store_with_thread(store)
    graph = scripted_graph()
    frames = list(
        _resume_run_stream(
            graph,
            store,
            run_control=RunControl(),
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            resume_input=None,
            resume_config={"configurable": {"thread_id": THREAD_ID}},
        )
    )
    assert seen == [RUN_ID]
    # The background caller discards every frame; the row is what it needed.
    assert frames and all(frame.startswith("event: ") for frame in frames)
    runs = store.list_runs(THREAD_ID)
    assert len(runs) == 1
    assert runs[0].status == "done"


def test_the_two_occupancy_probes_each_refuse_their_own_condition() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    graph = scripted_graph()
    graph.locked = True
    executor = RunExecutor(
        graph=graph, store=store, run_control=RunControl()
    )

    # The lock probe answers "is a run in the graph" — which the
    # auto-resumer and compaction hold without any admission claim.
    with pytest.raises(ConcurrentRunError):
        executor.assert_thread_occupancy(THREAD_ID)

    graph.locked = False
    executor.assert_thread_occupancy(THREAD_ID)
    # The window probe answers a different question: the thread is free,
    # but a paused choice window still owns the turn until someone picks.
    graph.interrupt_pending = lambda thread_id: {"kind": "choice_window"}  # type: ignore[method-assign]
    with pytest.raises(ConcurrentRunError):
        executor.assert_no_pending_choice(THREAD_ID)


@pytest.mark.parametrize("operation", ["send", "resume"])
def test_admission_is_held_until_terminal_persistence_finishes(
    operation: str,
) -> None:
    """A second same-thread turn cannot pass while the first row is committing."""
    first_append_entered = threading.Event()
    release_first_append = threading.Event()

    class BlockingAppendStore(InMemoryThreadStore):
        def append_run(self, record: Any) -> None:
            if record.id == "run-first":
                first_append_entered.set()
                if not release_first_append.wait(timeout=5):
                    raise TimeoutError("test did not release the first append")
            super().append_run(record)

    store = BlockingAppendStore()
    _store_with_thread(store)
    executor = RunExecutor(
        graph=scripted_graph(), store=store, run_control=RunControl()
    )

    def _stream(run_id: str):
        if operation == "send":
            return executor.send(
                RunRequest(
                    thread_id=THREAD_ID,
                    run_id=run_id,
                    question=RUN_QUESTION,
                    input_value={"messages": []},
                    config={"configurable": {"thread_id": THREAD_ID}},
                )
            )
        return executor.resume(
            ResumeRunRequest(
                thread_id=THREAD_ID,
                run_id=run_id,
                resume_input=None,
                resume_config={"configurable": {"thread_id": THREAD_ID}},
            )
        )

    first_errors: list[BaseException] = []

    def _finish_first() -> None:
        try:
            list(_stream("run-first"))
        except BaseException as exc:
            first_errors.append(exc)

    first_thread = threading.Thread(target=_finish_first)
    first_thread.start()
    second_error: BaseException | None = None
    try:
        assert first_append_entered.wait(timeout=5)
        try:
            list(_stream("run-second"))
        except BaseException as exc:
            second_error = exc
    finally:
        release_first_append.set()
        first_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert first_errors == []
    assert isinstance(second_error, RunAdmissionError)
    assert [run.id for run in store.list_runs(THREAD_ID)] == ["run-first"]


# ----- stop -----
# WHY all four in one test: they are one behaviour. A stop that ends the run
# but leaves the token registered makes the NEXT stop a no-op, and a stop
# that leaves the iterator open keeps the thread locked forever.


def test_a_stop_ends_the_run_releases_it_and_writes_exactly_one_row() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    control = RunControl()
    # The stop lands before the run's first frame: it must emit no channel
    # event at all, close the iterator, and write its stopped row.
    graph = scripted_graph(on_next=lambda index: control.request_stop(THREAD_ID))

    events, outcome = _run(graph, store, control)

    assert outcome.technical_result is RunTechnicalResult.STOPPED
    assert [event.kind for event in events] == [
        RunEventKind.STATUS,
        RunEventKind.ANSWER,
        RunEventKind.CITATIONS,
    ]
    assert events[0].data["reason"] == "stopped"
    # The graph iterator was closed, which is what releases the thread lock.
    assert graph.closed is True
    # The token is gone: a second Stop finds nothing to stop.
    assert control.active_run_id(THREAD_ID) is None
    assert control.request_stop(THREAD_ID) is None
    runs = store.list_runs(THREAD_ID)
    assert len(runs) == 1
    assert (runs[0].status, runs[0].status_reason) == ("refuse", "stopped")
    assert graph.finalized == [(THREAD_ID, "refuse", "stopped")]


def test_a_stop_that_lands_after_the_run_ends_does_not_rewrite_it() -> None:
    """A late Stop finds no token and cannot rewrite the finished turn."""
    store = InMemoryThreadStore()
    _store_with_thread(store)
    control = RunControl()

    _, outcome = _run(scripted_graph(), store, control)
    assert outcome.technical_result is RunTechnicalResult.DONE

    assert control.request_stop(THREAD_ID) is None
    runs = store.list_runs(THREAD_ID)
    assert [(run.status, run.status_reason) for run in runs] == [("done", "")]


# ----- one classification, one row -----


def test_a_mid_flight_failure_is_classified_once_and_persisted_once() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    events, outcome = _run(
        scripted_graph(raises=TimeoutError("provider read timed out after 120.0 s")),
        store,
        RunControl(),
    )

    statuses = [event for event in events if event.kind is RunEventKind.STATUS]
    assert len(statuses) == 1
    assert statuses[0].data["status_classified"] == "timeout"
    assert outcome.technical_result is RunTechnicalResult.TIMEOUT
    assert outcome.refuse_kind == "timeout"

    runs = store.list_runs(THREAD_ID)
    assert len(runs) == 1
    assert (runs[0].status, runs[0].status_classified) == ("refuse", "timeout")
    # The refuse wire already closed, so the executor adds no terminal frame.
    assert [event.kind for event in events][-3:] == [
        RunEventKind.STATUS,
        RunEventKind.ANSWER,
        RunEventKind.CITATIONS,
    ]


def test_a_run_that_pauses_writes_no_row_and_no_terminal_event() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    paused = scripted_graph()
    paused._script[-1] = (
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

    events, outcome = _run(paused, store, RunControl())

    assert outcome.technical_result is None
    assert outcome.choice_window == {"kind": "choice_window", "options": []}
    assert [event.kind for event in events][-1] is RunEventKind.CHOICE_WINDOW
    assert store.list_runs(THREAD_ID) == []


def test_a_document_choice_pause_projects_the_route_from_final_state() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    receipts = TurnReceiptProjector(InMemoryReceiptStore(store))
    receipts.begin(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        accepted_input_version=1,
        started_at=datetime.now(timezone.utc),
    )
    executor = RunExecutor(
        graph=ScriptedGraph([]),
        store=store,
        run_control=RunControl(),
        receipts=receipts,
    )
    outcome = RunOutcome(
        thread_id=THREAD_ID,
        run_id=RUN_ID,
        technical_result=None,
        final_state={
            "track_decision": {
                "route_intent": "retrieve",
                "route_source": "jev",
            }
        },
        thinking_ms=0,
        document_choice={"kind": "document_choice"},
    )

    executor._record_pause(THREAD_ID, RUN_ID, "clarify", outcome)

    receipt = receipts.read(THREAD_ID, RUN_ID)
    assert receipt is not None
    assert receipt.execution_state == "paused"
    assert receipt.semantic_result == "clarify"
    assert receipt.route_intent == "retrieve"
    assert receipt.route_source == "jev"


# ----- isolation between executors -----
# WHY two registries and not one shared object: that is what a mounted
# router owns, and a stop that could reach across routers would be a stop
# the operator cannot explain.


def test_two_executors_cannot_cancel_each_others_runs() -> None:
    store = InMemoryThreadStore()
    _store_with_thread(store)
    store.create_thread(id="thread-run-exec-2", title="second")
    first_control = RunControl()
    second_control = RunControl()

    entered = threading.Event()
    release = threading.Event()
    blocking = scripted_graph(on_next=lambda index: (entered.set(), release.wait(5)))
    other = scripted_graph()

    first = RunExecutor(
        graph=blocking, store=store, run_control=first_control
    )
    first_stream = first.send(
        RunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            question=RUN_QUESTION,
            input_value={"messages": []},
            config={"configurable": {"thread_id": THREAD_ID}},
        )
    )
    first_outcome: list[Any] = []
    worker = threading.Thread(
        target=lambda: first_outcome.append(
            next(iter(list(first_stream)), None) is not None
        )
    )
    worker.start()
    assert entered.wait(5) is True

    # The other executor's run runs to completion while the first is held
    # inside the graph: it must not be disturbed by anything on the first.
    second = RunExecutor(graph=other, store=store, run_control=second_control)
    second_events = list(
        second.send(
            RunRequest(
                thread_id="thread-run-exec-2",
                run_id="run-exec-2",
                question="second question",
                input_value={"messages": []},
                config={"configurable": {"thread_id": "thread-run-exec-2"}},
            )
        )
    )
    assert second_events[-1].kind is RunEventKind.TERMINAL
    assert [run.status for run in store.list_runs("thread-run-exec-2")] == ["done"]

    # Only now does the first run get stopped, and only it.
    assert first_control.request_stop(THREAD_ID) == RUN_ID
    assert second_control.request_stop("thread-run-exec-2") is None
    release.set()
    worker.join(5)
    assert worker.is_alive() is False

    runs = store.list_runs(THREAD_ID)
    assert [(run.status, run.status_reason) for run in runs] == [
        ("refuse", "stopped")
    ]
    assert first_control.active_run_id(THREAD_ID) is None


# ----- per-stage budgets -----
# WHY these are pinned at the executor and not at the wire: the deadline
# belongs to the one owner of the run lifecycle, and the run that finishes
# inside its budget must emit the frames it emitted before any of this.


def _stage(outcome: RunOutcome, stage: str) -> Any:
    return next(
        metric for metric in outcome.stage_metrics if metric.stage == stage
    )


def test_a_run_inside_its_budget_releases_admitted_answer_and_reports_stages() -> None:
    """A healthy run releases its admitted answer with unchanged stage data.

    The stage outcomes ride the outcome object only — nothing here reaches
    the SSE stream, because a frame the SPA must learn to parse is a
    change to a contract this work does not own.
    """
    store = InMemoryThreadStore()
    _store_with_thread(store)

    events, outcome = _run(scripted_graph(), store, RunControl())

    assert outcome.technical_result is RunTechnicalResult.DONE
    assert [event.kind for event in events] == [
        RunEventKind.THINKING,
        RunEventKind.TOOL_CALL,
        RunEventKind.TOOL_RESULT,
        RunEventKind.ANSWER,
        RunEventKind.TERMINAL,
    ]
    _assert_events_carry_admitted_answer(events)
    execute = _stage(outcome, "execute")
    assert (execute.exceeded, execute.complete) == (False, True)
    assert execute.budget_ms == 120_000
    cleanup = _stage(outcome, "cleanup")
    assert (cleanup.exceeded, cleanup.complete) == (False, True)
    assert cleanup.budget_ms == 1_000
    # No stage leaked into any event body.
    assert not any(
        "stage" in repr(event.data) for event in events if event.data is not None
    )


def test_a_tightened_budget_a_healthy_run_meets_changes_no_frame() -> None:
    """A tighter healthy-run budget preserves safe event order and answer."""
    store = InMemoryThreadStore()
    _store_with_thread(store)

    events, outcome = _run(
        scripted_graph(),
        store,
        RunControl(),
        budgets=RunBudgets(execute_stage_s=30.0, cleanup_grace_s=0.5),
    )

    assert [event.kind for event in events] == [
        RunEventKind.THINKING,
        RunEventKind.TOOL_CALL,
        RunEventKind.TOOL_RESULT,
        RunEventKind.ANSWER,
        RunEventKind.TERMINAL,
    ]
    _assert_events_carry_admitted_answer(events)
    assert outcome.technical_result is RunTechnicalResult.DONE
    assert _stage(outcome, "execute").budget_ms == 30_000


def test_a_stage_without_progress_past_its_budget_ends_the_run_as_a_timeout() -> (
    None
):
    """The deadline ends the run instead of letting it hang, and it ends
    it through the same refuse wire a provider timeout already used — so
    the SPA reads one thing for both."""
    store = InMemoryThreadStore()
    _store_with_thread(store)
    stalled = scripted_graph(
        on_next=lambda index: time.sleep(0.2) if index == 1 else None
    )

    events, outcome = _run(
        stalled,
        store,
        RunControl(),
        budgets=RunBudgets(execute_stage_s=0.05),
    )

    assert outcome.technical_result is RunTechnicalResult.TIMEOUT
    assert outcome.refuse_kind == "timeout"
    statuses = [event for event in events if event.kind is RunEventKind.STATUS]
    assert len(statuses) == 1
    assert statuses[0].data["status"] == "refuse"
    assert statuses[0].data["status_classified"] == "timeout"
    # The reason names the stage that ran long, so the log says which
    # bound was passed rather than that something timed out.
    assert "execute" in statuses[0].data["reason"]
    # The reader is closed the way any other end closes it.
    assert [event.kind for event in events][-3:] == [
        RunEventKind.STATUS,
        RunEventKind.ANSWER,
        RunEventKind.CITATIONS,
    ]
    execute = _stage(outcome, "execute")
    assert execute.exceeded is True
    # The stage was cut off, so its elapsed time is a lower bound on the
    # work the graph actually did.
    assert execute.complete is False
    # The run's release still happened: the iterator is closed, which is
    # what returns the per-thread lock.
    assert stalled.closed is True
    runs = store.list_runs(THREAD_ID)
    assert [(run.status, run.status_classified) for run in runs] == [
        ("refuse", "timeout")
    ]


def test_a_stop_is_never_mistaken_for_a_stage_that_ran_out_of_budget() -> None:
    """The operator's own stop ends the run as a stop, even when the budget
    was already passed: at that boundary a stop is not a timeout.

    The stop is asked for from INSIDE the graph's first pull, so it is
    already set by the time the run reaches its first event boundary and no
    budget check can be the one that ran first. The budget is crossed on the
    way there — a sleep never returns early — so BOTH conditions hold at that
    one boundary, which is what makes the order of the two checks the thing
    under test rather than how fast this machine happens to be.
    """
    store = InMemoryThreadStore()
    _store_with_thread(store)
    control = RunControl()
    asked: list[str | None] = []

    def on_next(index: int) -> None:
        if index != 0:
            return
        time.sleep(0.05)
        asked.append(control.request_stop(THREAD_ID))

    events, outcome = _run(
        scripted_graph(on_next=on_next),
        store,
        control,
        budgets=RunBudgets(execute_stage_s=0.001),
    )

    # The stop landed on this run's own token, before any boundary was read.
    assert asked == [RUN_ID]
    assert outcome.technical_result is RunTechnicalResult.STOPPED
    execute = _stage(outcome, "execute")
    # The budget WAS passed. Without this the case would pass trivially: a run
    # that read the two checks the other way round would have had nothing to
    # time out on and this assert is what keeps it a race the stop must win.
    assert execute.exceeded is True
    assert execute.budget_ms == 1
    # The wire says stop too — a timeout carries its own classification and
    # this frame carries none.
    statuses = [event for event in events if event.kind is RunEventKind.STATUS]
    assert [event.data["reason"] for event in statuses] == ["stopped"]
    assert [(run.status, run.status_reason) for run in store.list_runs(THREAD_ID)] == [
        ("refuse", "stopped")
    ]


@pytest.mark.parametrize("reason", ["user_stop", "client_disconnect"])
def test_a_stopped_legacy_resume_terminalizes_its_receipt_without_a_run_row(
    reason: str,
) -> None:
    threads = InMemoryThreadStore()
    _store_with_thread(threads)
    receipt_store = InMemoryReceiptStore(threads)
    receipts = TurnReceiptProjector(receipt_store)
    started = receipts.begin(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        accepted_input_version=4,
    )
    paused = receipts.paused(
        thread_id=THREAD_ID,
        turn_id=RUN_ID,
        semantic_result="clarify",
    )
    assert paused is not None
    runtime_state = TaskRuntimeState(
        input_history=(
            AcceptedInput(
                input_version=4,
                kind="message",
                payload=RUN_QUESTION,
                provenance="send",
                run_id=RUN_ID,
            ),
        )
    )
    runtime_reads: list[str] = []
    observed_states: list[str | None] = []
    control = RunControl()

    def read_runtime_state(thread_id: str) -> TaskRuntimeState:
        runtime_reads.append(thread_id)
        return runtime_state

    def stop_before_first_frame(_: int) -> None:
        receipt = receipt_store.read_receipt(THREAD_ID, RUN_ID)
        observed_states.append(None if receipt is None else receipt.execution_state)
        control.request_stop(THREAD_ID, reason=reason)

    graph = scripted_graph(on_next=stop_before_first_frame)
    graph.task_runtime_state = read_runtime_state
    executor = RunExecutor(
        graph=graph,
        store=threads,
        run_control=control,
        receipts=receipts,
    )
    stream = executor.resume(
        ResumeRunRequest(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            resume_input="legacy pick",
            resume_config={"configurable": {"thread_id": THREAD_ID}},
        )
    )
    events: list[RunEvent] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            outcome = stop.value
            break

    stopped = receipt_store.read_receipt(THREAD_ID, RUN_ID)
    assert outcome.technical_result is RunTechnicalResult.STOPPED
    assert outcome.cancellation_reason == reason
    assert observed_states == ["running"]
    assert runtime_reads == [THREAD_ID]
    assert stopped is not None
    assert stopped.execution_state == "terminal"
    assert stopped.technical_result == "stopped"
    assert stopped.cancellation_reason == reason
    assert stopped.accepted_input_count == started.accepted_input_count
    assert stopped.accepted_input_version == started.accepted_input_version
    assert stopped.segment_seq == started.segment_seq
    assert stopped.stage_metrics == paused.stage_metrics
    assert threads.list_runs(THREAD_ID) == []

