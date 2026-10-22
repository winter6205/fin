"""P3 T4 — the immutable tool outcome ledger (hermetic cases).

The ledger stores typed observations only: fact identity is deterministic
over the run / logical call / kind / provider tool-call id / execution
attempt, a payload never carries a submitted value, and an error result is
a KNOWN failure only for a read-effect contract. A graph with no ledger
writes nothing, and a graph WITH a ledger but no Runtime attempt identity
(the CLI's direct invoke) also writes nothing rather than committing
outside the fence.

Real PostgreSQL behavior (dedup, readback, trigger, stale worker) lives in
``test_tool_ledger_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import _ValidatedToolCall, _tool_outcome_observations
from fin.host.state import reset_input_state
from fin.host.tools.ledger import (
    PostgresToolOutcomeLedger,
    ToolOutcomeLedgerError,
    ToolOutcomeObservation,
    fact_id,
)
from fin.host.tools.validation import CorrectionTracker
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileStore,
)
from fin.retrieve import Chunk, FakeRetrievePort

QUESTION = "What was AAPL net sales in 2023 Q3?"
QUERY = "net sales"
COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."
SECRET = "SUBMITTED-SECRET-9f3a"

IDENTITY: dict[str, Any] = {
    "runtime_run_id": "run-1",
    "runtime_attempt_id": "attempt-1",
    "runtime_attempt_generation": 1,
    "thread_generation": 1,
}


# ----- fixtures / helpers ---------------------------------------------------


def _chunk() -> Chunk:
    return Chunk(
        text=COMPANY_TEXT,
        source_ref=Locator(document_id="AAPL-2023Q3", version="d" * 64, unit=3),
        score=1.5,
    )


def _port() -> FakeRetrievePort:
    return FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})


def _profile_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / "ws" / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _graph(
    tmp_path: Path,
    model: ScriptedChatModel,
    port: FakeRetrievePort,
    ledger: Any | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )


def _run(
    graph: Any,
    *,
    identity: bool = True,
    thread_id: str | None = None,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": thread_id or f"thread-{uuid.uuid4()}",
        "run_id": f"run-{uuid.uuid4()}",
    }
    if identity:
        configurable.update(IDENTITY)
    return graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": configurable},
    )


def _call(args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "retrieve", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


class _RecordingLedger:
    """The ledger port, captured in memory for the hermetic graph cases."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(
        self,
        observations: Sequence[ToolOutcomeObservation],
        *,
        thread_id: str,
        run_id: str,
        attempt_id: str,
        attempt_generation: int,
        thread_generation: int,
    ) -> int:
        self.calls.append(
            {
                "observations": list(observations),
                "thread_id": thread_id,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "attempt_generation": attempt_generation,
                "thread_generation": thread_generation,
            }
        )
        return len(observations)

    def read_run(self, run_id: str) -> Sequence[Any]:
        """This fake commits nothing, so a run has no fact to read back."""
        return ()


def _accepted(
    tracker: CorrectionTracker,
    name: str,
    args: dict[str, Any],
    call_id: str,
) -> _ValidatedToolCall:
    tracker.begin_model_attempt([{"name": name, "args": args, "id": call_id}])
    outcome = tracker.record_validation(name, args, tool_call_id=call_id)
    return _ValidatedToolCall(
        name=name,
        args=args,
        tool_call_id=call_id,
        outcome=outcome,
        execution_attempt=tracker.next_execution_attempt(
            outcome.logical_call_id
        ),
    )


def _tool_result(call_id: str, *, status: str = "success") -> ToolMessage:
    return ToolMessage(
        content="{}",
        name="retrieve",
        tool_call_id=call_id,
        status=status,
    )


def _kinds_by_call(
    observations: Sequence[ToolOutcomeObservation],
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for observation in observations:
        grouped.setdefault(observation.provider_tool_call_id or "", []).append(
            observation.kind
        )
    return grouped


# ----- fact identity --------------------------------------------------------


def test_fact_id_is_deterministic_and_covers_every_identity_part() -> None:
    base: dict[str, Any] = {
        "run_id": "run-1",
        "logical_call_id": "retrieve#1",
        "kind": "succeeded",
        "provider_tool_call_id": "call-a",
        "execution_attempt": 1,
    }
    identity = fact_id(**base)

    assert identity == fact_id(**base)
    assert len(identity) == 64
    for patch in (
        {"run_id": "run-2"},
        {"logical_call_id": "retrieve#2"},
        {"kind": "known_failed"},
        {"provider_tool_call_id": "call-b"},
        {"execution_attempt": 2},
    ):
        assert fact_id(**{**base, **patch}) != identity, patch


def test_the_fact_identity_is_pinned() -> None:
    """The identity payload is part of the ledger's contract."""
    assert (
        fact_id(
            run_id="run-1",
            logical_call_id="retrieve#1",
            kind="succeeded",
            provider_tool_call_id="call-a",
            execution_attempt=1,
        )
        == "5024fa967f4191838edb90259ece8cf531564c9670db719e2a5e409569de9e1a"
    )
    assert (
        fact_id(
            run_id="run-1",
            logical_call_id="retrieve#1",
            kind="succeeded",
            provider_tool_call_id=None,
            execution_attempt=None,
        )
        == "db7e3a27faf98d02e1612414957fbf99a9699c47e066e03575714e4e6552db42"
    )


def test_an_unstorable_identity_is_a_typed_error_not_an_assertion() -> None:
    with pytest.raises(ToolOutcomeLedgerError):
        fact_id(
            run_id="",
            logical_call_id="retrieve#1",
            kind="succeeded",
            provider_tool_call_id=None,
            execution_attempt=None,
        )
    with pytest.raises(ToolOutcomeLedgerError):
        fact_id(
            run_id="run-1",
            logical_call_id="retrieve#1",
            kind="not_a_kind",  # type: ignore[arg-type]
            provider_tool_call_id=None,
            execution_attempt=None,
        )
    with pytest.raises(ToolOutcomeLedgerError):
        fact_id(
            run_id="run-1",
            logical_call_id="retrieve#1",
            kind="succeeded",
            provider_tool_call_id="  ",
            execution_attempt=None,
        )


def test_an_unstorable_observation_is_a_typed_error_not_an_assertion() -> None:
    with pytest.raises(ToolOutcomeLedgerError):
        ToolOutcomeObservation(
            logical_call_id="  ",
            tool_name="retrieve",
            model_attempt=1,
            kind="succeeded",
        )
    with pytest.raises(ToolOutcomeLedgerError):
        ToolOutcomeObservation(
            logical_call_id="retrieve#1",
            tool_name="",
            model_attempt=1,
            kind="succeeded",
        )
    with pytest.raises(ToolOutcomeLedgerError):
        ToolOutcomeObservation(
            logical_call_id="retrieve#1",
            tool_name="retrieve",
            model_attempt=1,
            kind="not_a_kind",  # type: ignore[arg-type]
        )
    with pytest.raises(ToolOutcomeLedgerError):
        ToolOutcomeObservation(
            logical_call_id="retrieve#1",
            tool_name="retrieve",
            model_attempt=1,
            kind="known_failed",
            outcome_code="a free-text reason",  # type: ignore[arg-type]
        )
    with pytest.raises(ToolOutcomeLedgerError):
        ToolOutcomeObservation(
            logical_call_id="retrieve#1",
            tool_name="retrieve",
            model_attempt=1,
            kind="schema_rejected",
            field_paths=("",),
        )


def test_recording_nothing_opens_no_connection() -> None:
    """The no-op path writes no row and needs no database."""
    ledger = PostgresToolOutcomeLedger(
        "postgresql://nobody@127.0.0.1:1/fin_tool_outcome_ledger_none"
    )

    assert (
        ledger.record(
            [],
            thread_id="thread-1",
            run_id="run-1",
            attempt_id="attempt-1",
            attempt_generation=1,
            thread_generation=1,
        )
        == 0
    )


# ----- value-free payloads --------------------------------------------------


def test_observations_carry_only_declared_field_paths_and_codes() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt(
        [{"name": "retrieve", "args": {"query": SECRET}, "id": "call-1"}]
    )
    call = _ValidatedToolCall(
        name="retrieve",
        args={"query": SECRET, "limit": "many"},
        tool_call_id="call-1",
        outcome=tracker.record_validation(
            "retrieve",
            {"query": SECRET, "limit": "many"},
            tool_call_id="call-1",
        ),
    )
    observations = _tool_outcome_observations([call], [])

    assert call.outcome is not None
    assert call.outcome.model_attempt == 1

    assert [observation.kind for observation in observations] == [
        "schema_rejected",
        "correction_offered",
    ]
    payload = repr([asdict(observation) for observation in observations])
    assert SECRET not in payload
    for observation in observations:
        assert observation.field_paths == ("limit",)
        assert observation.outcome_code == "invalid_tool_arguments"
        assert observation.execution_attempt is None
        # The provider id is an attempt identity, never the logical call.
        assert observation.logical_call_id != observation.provider_tool_call_id


def test_an_unknown_tool_produces_no_facts() -> None:
    call = _ValidatedToolCall(
        name="not_a_tool",
        args={},
        tool_call_id="call-unknown",
        outcome=None,
        unknown_tool=True,
    )
    assert _tool_outcome_observations([call], []) == []


# ----- effect-based classification ------------------------------------------


def test_error_results_classify_by_the_contract_effect() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    read_call = _accepted(tracker, "retrieve", {"query": QUERY}, "call-read")
    write_call = _accepted(
        tracker, "save_memory", {"title": "t", "body": "b"}, "call-write"
    )
    visual_call = _accepted(
        tracker,
        "read_visual_detail",
        {
            "attachment_id": "att-1",
            "page": 1,
            "chart": "Revenue",
            "series": "2023",
            "category": "Q3",
        },
        "call-visual",
    )
    missing_call = _accepted(tracker, "list_files", {}, "call-missing")

    results: list[BaseMessage] = [
        _tool_result("call-read", status="error"),
        _tool_result("call-write", status="error"),
        _tool_result("call-visual", status="error"),
    ]
    observations = _tool_outcome_observations(
        [read_call, write_call, visual_call, missing_call], results
    )

    kinds = _kinds_by_call(observations)
    assert kinds["call-read"] == [
        "valid_input_accepted",
        "execution_started",
        "known_failed",
    ]
    # A persistent write and an external effect may already have happened:
    # their error is an UNKNOWN outcome, never a known failure.
    assert kinds["call-write"] == [
        "valid_input_accepted",
        "execution_started",
        "outcome_unknown",
    ]
    assert kinds["call-visual"] == [
        "valid_input_accepted",
        "execution_started",
        "outcome_unknown",
    ]
    # An accepted call with no result is unknown, not a success.
    assert kinds["call-missing"] == [
        "valid_input_accepted",
        "execution_started",
        "outcome_unknown",
    ]

    codes = {
        (observation.provider_tool_call_id, observation.kind): (
            observation.outcome_code
        )
        for observation in observations
    }
    assert codes[("call-read", "known_failed")] == "tool_result_error"
    assert codes[("call-write", "outcome_unknown")] == "tool_result_error"
    assert codes[("call-missing", "outcome_unknown")] == "result_missing"


def test_a_successful_result_is_success_not_unknown() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    call = _accepted(tracker, "retrieve", {"query": QUERY}, "call-ok")
    observations = _tool_outcome_observations([call], [_tool_result("call-ok")])

    assert [observation.kind for observation in observations] == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]
    assert all(
        observation.outcome_code is None
        for observation in observations
        if observation.kind == "succeeded"
    )
    assert observations[1].execution_attempt == observations[0].model_attempt


def test_a_tool_reported_error_envelope_is_a_known_failure() -> None:
    """A success-status result whose payload carries ``error`` is a failure.

    The tool itself reported a known failure (an unavailable retriever or
    a refused filter). Recording it as ``succeeded`` would hide a real
    failure from the replay guard.
    """
    tracker = CorrectionTracker("thread-1", "run-1")
    call = _accepted(tracker, "retrieve", {"query": QUERY}, "call-error")
    envelope = ToolMessage(
        content=json.dumps({"error": "retrieve_unavailable"}),
        name="retrieve",
        tool_call_id="call-error",
        status="success",
    )

    observations = _tool_outcome_observations([call], [envelope])

    assert [observation.kind for observation in observations] == [
        "valid_input_accepted",
        "execution_started",
        "known_failed",
    ]
    assert observations[-1].outcome_code == "tool_result_error"
    assert observations[-1].execution_attempt == 1


def test_a_mapping_payload_with_an_error_key_is_a_known_failure() -> None:
    """A mapping content is parsed directly, not only a JSON string."""
    tracker = CorrectionTracker("thread-1", "run-1")
    call = _accepted(tracker, "retrieve", {"query": QUERY}, "call-mapping")
    result = _tool_result("call-mapping")
    result.content = {"error": "invalid_filter"}

    observations = _tool_outcome_observations([call], [result])

    assert [observation.kind for observation in observations] == [
        "valid_input_accepted",
        "execution_started",
        "known_failed",
    ]
    assert observations[-1].outcome_code == "tool_result_error"


@pytest.mark.parametrize(
    "content",
    [
        "retrieve_unavailable",
        "[1, 2]",
        '{"chunks": 3}',
        "not json at all",
    ],
)
def test_a_result_that_is_not_an_error_object_stays_succeeded(
    content: str,
) -> None:
    """Only an object carrying ``error`` changes the classification."""
    tracker = CorrectionTracker("thread-1", "run-1")
    call = _accepted(tracker, "retrieve", {"query": QUERY}, "call-plain")
    result = ToolMessage(
        content=content,
        name="retrieve",
        tool_call_id="call-plain",
        status="success",
    )

    observations = _tool_outcome_observations([call], [result])

    assert [observation.kind for observation in observations] == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]


def test_a_terminal_invalid_attempt_reports_correction_exhausted() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt(
        [{"name": "retrieve", "args": {"limit": "many"}, "id": "first"}]
    )
    tracker.record_validation(
        "retrieve", {"limit": "many"}, tool_call_id="first"
    )
    second = _accepted(tracker, "retrieve", {"limit": "many"}, "second")

    observations = _tool_outcome_observations([second], [])
    assert [observation.kind for observation in observations] == [
        "schema_rejected",
        "correction_exhausted",
    ]
    assert observations[-1].outcome_code == "tool_validation_exhausted"


# ----- graph wiring ---------------------------------------------------------


def test_graph_records_one_fenced_step_with_every_fact(
    tmp_path: Path,
) -> None:
    ledger = _RecordingLedger()
    model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "call-ok"), AIMessage(content="Answer.")]
    )
    out = _run(_graph(tmp_path, model, _port(), ledger))

    assert out["retrieve_attempted"] is True
    # Every fact of the step is recorded, in call order. How the caller
    # splits its writes is the caller's business; the ledger's contract is
    # that no fact is lost and every fact carries the fencing identity.
    assert ledger.calls, "a wired graph records its step"
    for call in ledger.calls:
        assert call["run_id"] == IDENTITY["runtime_run_id"]
        assert call["attempt_id"] == IDENTITY["runtime_attempt_id"]
        assert call["attempt_generation"] == IDENTITY["runtime_attempt_generation"]
        assert call["thread_generation"] == IDENTITY["thread_generation"]
    kinds = [
        observation.kind
        for call in ledger.calls
        for observation in call["observations"]
    ]
    assert kinds == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]
    assert {
        observation.logical_call_id
        for call in ledger.calls
        for observation in call["observations"]
    } == {"retrieve#1"}


def test_graph_records_a_rejected_call_without_execution_facts(
    tmp_path: Path,
) -> None:
    ledger = _RecordingLedger()
    model = ScriptedChatModel(
        script=[
            _call({"query": 987654, "leak": SECRET}, "call-bad"),
            AIMessage(content="Answer."),
        ]
    )
    _run(_graph(tmp_path, model, _port(), ledger))

    observations = ledger.calls[0]["observations"]
    assert [observation.kind for observation in observations] == [
        "schema_rejected",
        "correction_offered",
    ]
    assert all(
        observation.execution_attempt is None for observation in observations
    )
    assert SECRET not in repr([asdict(observation) for observation in observations])


def test_a_graph_without_a_ledger_writes_nothing(tmp_path: Path) -> None:
    ledger = _RecordingLedger()
    wired_model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "wired"), AIMessage(content="Answer.")]
    )
    _run(_graph(tmp_path / "wired", wired_model, _port(), ledger))
    assert ledger.calls
    calls_before = len(ledger.calls)

    unwired_model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "unwired"), AIMessage(content="Answer.")]
    )
    unwired = _graph(tmp_path / "unwired", unwired_model, _port())
    assert unwired.tool_ledger is None
    out = _run(unwired)
    assert out["retrieve_attempted"] is True
    assert len(ledger.calls) == calls_before


def test_a_wired_ledger_without_runtime_identity_writes_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The CLI's direct invoke has no attempt to fence a fact under."""
    ledger = _RecordingLedger()
    model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "call-ok"), AIMessage(content="Answer.")]
    )

    state = _run(_graph(tmp_path, model, _port(), ledger), identity=False)

    assert state["messages"]
    assert ledger.calls == []
    # The drop is OBSERVABLE: a silent gap in the measurement source
    # would otherwise look like a run that never called a tool. The
    # warning names the reason and carries no submitted value.
    dropped = [
        record
        for record in caplog.records
        if "tool outcome facts dropped" in record.getMessage()
    ]
    assert dropped, caplog.records
    assert "no Runtime worker attempt" in dropped[0].getMessage()
    assert QUERY not in dropped[0].getMessage()
