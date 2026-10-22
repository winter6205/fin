"""P3 T2/T3 — validation and bounded correction at the graph boundary.

Every model-emitted tool call is validated against its canonical contract
before the prebuilt ``ToolNode`` can execute it. A first invalid response
gets exactly one safe correction; a second invalid response is the typed
terminal outcome. Nothing invalid reaches a tool body, and valid siblings
in the same model turn still run. Feedback carries no submitted value,
raw kwargs, exception text, stack trace, or source content.

Hermetic: scripted model, fake retrieve port, in-memory checkpointer.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host import graph as graph_module
from fin.host.state import reset_input_state
from fin.host.tools.contracts import PolicyRecord, contract_for
from fin.host.tools.validation import CorrectionTracker
from fin.host.user_profile import GLOBAL_FILENAME, WORKSPACE_FILENAME, UserProfileStore
from fin.retrieve import Chunk, FakeRetrievePort


# Grounded by the track rules tier (mirrors the gate suite).
QUESTION = "What was AAPL net sales in 2023 Q3?"
QUERY = "net sales"
COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."
SECRET = "SUBMITTED-SECRET-9f3a"

_FORBIDDEN_FRAGMENTS = (
    SECRET,
    "Traceback",
    "ValidationError",
    "kwargs",
)


def _chunk() -> Chunk:
    return Chunk(
        text=COMPANY_TEXT,
        source_ref=Locator(document_id="AAPL-2023Q3", version="d" * 64, unit=3),
        score=1.5,
    )


def _port(*, hits: bool = True) -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={QUERY: [_chunk()]} if hits else {},
    )


def _call(args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "retrieve", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _profile_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / "ws" / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _graph(tmp_path: Path, model: ScriptedChatModel, port: FakeRetrievePort) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
    )


def _run(
    graph: Any,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    tracker: CorrectionTracker | None = None,
    question: str = QUESTION,
    identity: bool = False,
) -> dict[str, Any]:
    # A fresh identity per invocation keeps the process-local registry from
    # leaking correction state between test cases.
    configurable: dict[str, Any] = {
        "thread_id": thread_id or f"thread-{uuid.uuid4()}",
        "run_id": run_id or f"run-{uuid.uuid4()}",
    }
    if identity:
        configurable.update(
            {
                "runtime_run_id": "run-1",
                "runtime_attempt_id": "attempt-1",
                "runtime_attempt_generation": 1,
                "thread_generation": 1,
            }
        )
    if tracker is not None:
        configurable["tool_correction_tracker"] = tracker
    return graph.invoke(
        reset_input_state(question),
        config={"configurable": configurable},
    )


def _tool_messages(out: dict[str, Any]) -> list[ToolMessage]:
    return [
        message
        for message in out["messages"]
        if isinstance(message, ToolMessage)
        and getattr(message, "name", None) == "retrieve"
    ]


def _payload(message: ToolMessage) -> dict[str, Any]:
    return json.loads(str(message.content))


# ----- bounded correction ---------------------------------------------------


def test_first_invalid_call_gets_one_safe_correction_and_is_not_executed(
    tmp_path: Path,
) -> None:
    port = _port()
    model = ScriptedChatModel(
        script=[
            # Wrong type: ``limit`` is not the failure; ``query`` is.
            _call({"query": 424242}, "attempt-1"),
            _call({"query": QUERY}, "attempt-2"),
            AIMessage(content="Answer."),
        ]
    )
    out = _run(_graph(tmp_path, model, port))

    messages = _tool_messages(out)
    assert len(messages) >= 2, messages
    first, second = messages[0], messages[1]

    # The first attempt was rejected BEFORE execution and answered with a
    # safe correction naming the field.
    assert first.tool_call_id == "attempt-1"
    assert first.status == "error"
    rejected = _payload(first)
    assert rejected["error"] == "invalid_tool_arguments"
    assert "query" in rejected["feedback"]
    assert "424242" not in str(first.content)

    # The corrected attempt carries a NEW provider id, correlates to the same
    # logical call, and executes exactly once.
    assert second.tool_call_id == "attempt-2"
    assert "chunks" in _payload(second)
    assert len(port.calls) == 1, port.calls

    # Everything the wrapper already folded survives the validation gate:
    # the Command-shaped retrieve update, its outcome, evidence, and the
    # summary of the call that actually ran.
    assert out["retrieve_attempted"] is True
    assert out["retrieve_outcomes"] == [{"chunks": 1, "mapping_failures": 0}]
    assert out["evidence"], out["evidence"]
    assert [row["tool_call_id"] for row in out["tool_summaries"]] == [
        "attempt-2"
    ]
    assert out["comparisons"] == []
    assert out["analysis_results"] == []


def test_second_invalid_response_is_terminal_and_executes_nothing(
    tmp_path: Path,
) -> None:
    port = _port()
    model = ScriptedChatModel(
        script=[
            _call({"query": 111111}, "attempt-1"),
            _call({"query": 222222}, "attempt-2"),
            AIMessage(content="Answer."),
        ]
    )
    out = _run(_graph(tmp_path, model, port))

    messages = _tool_messages(out)
    assert [_payload(message)["error"] for message in messages] == [
        "invalid_tool_arguments",
        "tool_validation_exhausted",
    ]
    assert messages[1].status == "error"
    assert messages[1].tool_call_id == "attempt-2"
    assert "222222" not in str(messages[1].content)
    # No third correction attempt and no execution.
    assert len(messages) == 2, messages
    assert port.calls == []


def test_a_valid_sibling_still_runs_in_a_mixed_batch(tmp_path: Path) -> None:
    port = _port()
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"limit": "many"},
                        "id": "sibling-bad",
                        "type": "tool_call",
                    },
                    {
                        "name": "retrieve",
                        "args": {"query": QUERY},
                        "id": "sibling-good",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="Answer."),
        ]
    )
    out = _run(_graph(tmp_path, model, port))

    messages = _tool_messages(out)
    assert [message.tool_call_id for message in messages] == [
        "sibling-bad",
        "sibling-good",
    ], messages
    # Original call order is preserved: the refusal, then the real result.
    assert _payload(messages[0])["error"] == "invalid_tool_arguments"
    assert "chunks" in _payload(messages[1])
    assert len(port.calls) == 1, port.calls
    # Only the call that actually ran contributes a summary.
    assert [row["tool_call_id"] for row in out["tool_summaries"]] == [
        "sibling-good"
    ], out["tool_summaries"]


def test_feedback_is_value_free(tmp_path: Path) -> None:
    port = _port()
    model = ScriptedChatModel(
        script=[
            _call({"query": 987654, "leak": SECRET}, "attempt-1"),
            _call({"query": QUERY}, "attempt-2"),
            AIMessage(content="Answer."),
        ]
    )
    out = _run(_graph(tmp_path, model, port))

    first = _tool_messages(out)[0]
    feedback = _payload(first)["feedback"]
    # Field paths and declared-shape hints are present.
    assert "query" in feedback
    assert "leak" in feedback
    # No submitted value, exception text, traceback, or raw kwargs.
    assert "987654" not in feedback
    for fragment in _FORBIDDEN_FRAGMENTS:
        assert fragment not in feedback, (fragment, feedback)
        assert fragment not in str(first.content), (fragment, first.content)
    assert COMPANY_TEXT not in str(first.content)


# ----- run isolation --------------------------------------------------------


def test_two_runs_do_not_share_correction_state(tmp_path: Path) -> None:
    # Same thread id, different run ids: the tracker is per run, so the
    # second run's FIRST invalid call still earns a correction.
    port = _port()
    first_model = ScriptedChatModel(
        script=[
            _call({"query": 333333}, "run-a-attempt"),
            AIMessage(content="Answer."),
        ]
    )
    second_model = ScriptedChatModel(
        script=[
            _call({"query": 444444}, "run-b-attempt"),
            AIMessage(content="Answer."),
        ]
    )
    suffix = uuid.uuid4()
    first = _run(
        _graph(tmp_path / "a", first_model, port),
        thread_id="shared-thread",
        run_id=f"run-a-{suffix}",
    )
    second = _run(
        _graph(tmp_path / "b", second_model, port),
        thread_id="shared-thread",
        run_id=f"run-b-{suffix}",
    )

    assert _payload(_tool_messages(first)[0])["error"] == "invalid_tool_arguments"
    assert _payload(_tool_messages(second)[0])["error"] == "invalid_tool_arguments"
    assert port.calls == []


# ----- the clarify lane dispatches the same canonical contract ------------


def _run_clarify_leaf(tmp_path: Path, script: list[Any]) -> dict[str, Any]:
    """Run one turn straight into the clarify leaf.

    The clarify lane is reached normally from ``reconcile_task``; driving
    the checkpoint's track directly keeps the case about the LEAF's
    dispatch instead of the track classifier.
    """
    graph = _graph(
        tmp_path, ScriptedChatModel(script=script), _port()
    )
    thread_id = f"thread-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    graph._graph.update_state(
        config,
        {
            "messages": [HumanMessage(content="What was revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
    )
    # A published clarification leaves the clarify lane through
    # ``clarify_scope`` with the track already decided.
    graph._graph.update_state(
        config, {"track": "clarify", "status": "running"}, as_node="clarify_scope"
    )
    return graph.invoke(None, config=config)


def test_clarify_rejected_call_carries_no_value_and_no_raw_kwargs(
    tmp_path: Path,
) -> None:
    """A schema failure is the safe payload, never LangGraph's template.

    The clarify lane used to hand the raw model call to its dispatcher,
    so a schema failure rendered LangGraph's ``Error invoking tool ... with
    kwargs {...}`` text — the submitted value and the raw kwargs — into
    the model-visible message.
    """
    out = _run_clarify_leaf(
        tmp_path,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_files",
                        "args": {"company": 424242, "leak": SECRET},
                        "id": "clarify-attempt-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Which company did you mean?"),
        ],
    )

    tool_messages = [
        message
        for message in out["messages"]
        if isinstance(message, ToolMessage) and message.name == "list_files"
    ]
    assert len(tool_messages) == 1, tool_messages
    message = tool_messages[0]
    payload = _payload(message)
    assert payload["error"] == "invalid_tool_arguments"
    assert "company" in payload["feedback"]
    # No submitted value, no raw kwargs rendering, no exception text.
    assert "424242" not in str(message.content)
    assert SECRET not in str(message.content)
    for fragment in (*_FORBIDDEN_FRAGMENTS, "with kwargs", "ToolInvocationError"):
        assert fragment not in str(message.content), (fragment, message.content)
    assert message.status == "error"
    # The leaf still finishes the turn instead of crashing on the refusal.
    assert out["track"] == "clarify"
    assert out["status"] == "done"


def test_an_injected_tracker_is_used(tmp_path: Path) -> None:
    tracker = CorrectionTracker("injected-thread", "injected-run")
    port = _port()
    model = ScriptedChatModel(
        script=[
            _call({"query": 555555}, "injected-attempt"),
            AIMessage(content="Answer."),
        ]
    )
    out = _run(_graph(tmp_path, model, port), tracker=tracker)

    assert len(_tool_messages(out)) == 1
    assert tracker.logical_call_for("injected-attempt") is not None


# ----- the correction loop over real runs and turns -------------------------


def test_a_new_invalid_call_after_exhaustion_gets_a_fresh_correction(
    tmp_path: Path,
) -> None:
    """An exhausted call is terminal; the NEXT attempt is a new call.

    The second rejected attempt exhausts its call. A third, more wrong
    attempt of the same tool is a NEW logical call: it earns its own
    bounded correction instead of the terminal refusal the first one
    already used up.
    """
    port = _port()
    model = ScriptedChatModel(
        script=[
            _call({"query": 111111}, "attempt-1"),
            _call({"query": 222222}, "attempt-2"),
            _call({"query": 333333}, "attempt-3"),
            AIMessage(content="Answer."),
        ]
    )
    out = _run(_graph(tmp_path, model, port))

    errors = [_payload(message)["error"] for message in _tool_messages(out)]
    assert errors == [
        "invalid_tool_arguments",
        "tool_validation_exhausted",
        # NOT "tool_validation_exhausted": the exhausted call never
        # absorbs a later attempt.
        "invalid_tool_arguments",
    ]
    assert port.calls == []


def test_a_second_question_does_not_inherit_turn_one_correction(
    tmp_path: Path,
) -> None:
    """A second question on the SAME thread starts a fresh budget.

    The first question ends while its logical call is still OPEN and has
    already spent its correction (the model never came back to it). The
    tracker entry survives the question, so only turn scoping stops the
    second question's first invalid call from being absorbed by the first
    question's call — which would answer it terminally with no feedback.
    """
    port = _port()
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    first_graph = _graph(
        tmp_path / "a",
        ScriptedChatModel(
            script=[
                _call({"query": 111111}, "turn-1-attempt-1"),
                AIMessage(content="Answer."),
            ]
        ),
        port,
    )
    first = _run(first_graph, thread_id=thread_id, run_id=run_id)

    second_graph = _graph(
        tmp_path / "b",
        ScriptedChatModel(
            script=[
                _call({"query": 444444}, "turn-2-attempt-1"),
                AIMessage(content="Answer."),
            ]
        ),
        port,
    )
    second = _run(
        second_graph,
        thread_id=thread_id,
        run_id=run_id,
        question="And what about MSFT in 2023 Q1?",
    )

    assert [
        _payload(message)["error"] for message in _tool_messages(first)
    ] == ["invalid_tool_arguments"]
    # The second question's FIRST invalid call is corrected, not absorbed
    # by the first question's spent correction.
    second_messages = _tool_messages(second)[-1:]
    assert [
        _payload(message)["error"] for message in second_messages
    ] == ["invalid_tool_arguments"]
    assert port.calls == []


# ----- the tracker is rebuilt from the run's committed facts -----------------


class _Fact:
    """One committed ledger fact, in the shape the tracker rebuilds from."""

    def __init__(
        self,
        logical_call_id: str,
        tool_name: str,
        provider_tool_call_id: str | None,
        kind: str,
        model_attempt: int,
        execution_attempt: int | None = None,
        business_operation_id: str | None = None,
        outcome_code: str | None = None,
    ) -> None:
        self.logical_call_id = logical_call_id
        self.tool_name = tool_name
        self.provider_tool_call_id = provider_tool_call_id
        self.kind = kind
        self.model_attempt = model_attempt
        self.execution_attempt = execution_attempt
        self.business_operation_id = business_operation_id
        self.outcome_code = outcome_code


class _Ledger:
    """The ledger port: committed facts, recorded observations."""

    def __init__(self, facts: Sequence[_Fact] = ()) -> None:
        self.facts = list(facts)
        self.recorded: list[Any] = []

    def record(
        self,
        observations: Sequence[Any],
        *,
        thread_id: str,
        run_id: str,
        attempt_id: str,
        attempt_generation: int,
        thread_generation: int,
    ) -> int:
        self.recorded.extend(observations)
        return len(observations)

    def read_run(self, run_id: str) -> Sequence[_Fact]:
        return tuple(self.facts)


def test_a_takeover_resumes_the_spent_correction_from_the_ledger(
    tmp_path: Path,
) -> None:
    """A rebuilt tracker keeps the one-correction bound.

    A takeover (or an LRU eviction) starts a tracker that remembers
    nothing. The run's committed facts carry the correction that was
    already offered, so the first invalid call after the handover is the
    terminal one instead of a SECOND correction.
    """
    ledger = _Ledger(
        facts=[
            _Fact("retrieve#1", "retrieve", "prior-attempt", "schema_rejected", 1),
            _Fact(
                "retrieve#1",
                "retrieve",
                "prior-attempt",
                "correction_offered",
                1,
            ),
        ]
    )
    port = _port()
    model = ScriptedChatModel(
        script=[
            _call({"query": 555555}, "after-takeover"),
            AIMessage(content="Answer."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )
    thread_id = f"thread-{uuid.uuid4()}"
    out = graph.invoke(
        reset_input_state(QUESTION),
        config={
            "configurable": {
                "thread_id": thread_id,
                "run_id": f"run-{uuid.uuid4()}",
                "runtime_run_id": f"run-{uuid.uuid4()}",
                "runtime_attempt_id": "attempt-1",
                "runtime_attempt_generation": 1,
                "thread_generation": 1,
            }
        },
    )

    errors = [_payload(message)["error"] for message in _tool_messages(out)]
    # The correction was already spent before the takeover, so this call
    # is terminal: it is NOT answered with a second correction.
    assert errors == ["tool_validation_exhausted"]
    assert port.calls == []


# ----- P3 T5: a re-delivered call is decided from the ledger -----------------


def test_a_redelivered_call_after_an_unknown_outcome_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same provider call delivered after ``outcome_unknown`` never runs.

    The decision is scoped to the LOGICAL call id because no registered
    tool can derive a business operation id yet. The synthetic declared
    reconciliation policy is test-only.
    """
    ledger = _Ledger(
        facts=[
            _Fact(
                "retrieve#1",
                "retrieve",
                "call-1",
                "outcome_unknown",
                1,
                execution_attempt=1,
                outcome_code="tool_result_error",
            )
        ]
    )
    port = _port()
    model = ScriptedChatModel(
        script=[
            _call({"query": QUERY}, "call-1"),
            AIMessage(content="Answer."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )
    contract = replace(
        contract_for("retrieve"),
        reconciliation=PolicyRecord(
            "declared", "test-only declared reconciliation", "tests/host"
        ),
    )
    monkeypatch.setattr(graph_module, "_tool_contract_for", lambda name: contract)

    out = _run(graph, identity=True)

    # The tool body never ran a second time.
    assert port.calls == []
    assert out.get("retrieve_attempted") is not True
    messages = _tool_messages(out)
    assert [_payload(message) for message in messages] == [
        {"error": "tool_outcome_unknown_reconciliation_required"}
    ]
    assert messages[0].status == "error"
    # A refusal starts no execution, so it commits no new fact.
    assert ledger.recorded == []
