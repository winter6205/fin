"""P3 T5 — reconciling an uncertain tool effect before another execution.

The decision is read from the canonical ledger facts: a known failure
re-executes only under a DECLARED retry policy, an uncertain outcome stays
unknown until a reconciliation proves it, and a known success is a
duplicate delivery. No registered tool declares a reconciliation policy,
so the synthetic contract in this file is the only place a declared
policy exists — it is never registered in ``TOOL_CONTRACTS``.

The fenced write and the stale-owner case live against real PostgreSQL in
the ``runtime_pg`` case at the end of this file.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host import graph as graph_module
from fin.host.runtime import PostgresRuntimeStore, RuntimeCoordinator
from fin.host.runtime.schema import CheckpointOwnershipLost
from fin.host.state import reset_input_state
from fin.host.tools.contracts import (
    TOOL_CONTRACTS,
    PolicyRecord,
    ToolContract,
    contract_for,
)
from fin.host.tools.ledger import (
    PostgresToolOutcomeLedger,
    ToolOutcomeFact,
    ToolOutcomeObservation,
)
from fin.host.tools.reconciliation import (
    ReconciliationRefused,
    execution_decision,
    record_reconciliation,
    refusal_code,
)
from fin.host.tools.validation import CorrectionTracker
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileStore,
)
from fin.retrieve import Chunk, FakeRetrievePort

import runtime_pg_support

QUESTION = "What was AAPL net sales in 2023 Q3?"
QUERY = "net sales"
COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."

IDENTITY: dict[str, Any] = {
    "runtime_run_id": "run-1",
    "runtime_attempt_id": "attempt-1",
    "runtime_attempt_generation": 1,
    "thread_generation": 1,
}

CHILD_READBACK = """
import json, os
from fin.host.tools.ledger import PostgresToolOutcomeLedger

ledger = PostgresToolOutcomeLedger(os.environ["FIN_TEST_DSN"])
facts = ledger.read_run(os.environ["FIN_RECONCILE_RUN"])
print(json.dumps({
    "pid": os.getpid(),
    "kinds": [fact.kind for fact in facts],
    "codes": [fact.outcome_code for fact in facts],
    "operations": [fact.business_operation_id for fact in facts],
}))
"""


# ----- helpers --------------------------------------------------------------


def _committed_fact(
    kind: str,
    *,
    seq: int = 1,
    logical_call_id: str = "save_memory#1",
    business_operation_id: str | None = "biz-1",
    outcome_code: str | None = None,
    tool_name: str = "save_memory",
    model_attempt: int = 1,
    execution_attempt: int | None = 1,
    provider_tool_call_id: str | None = "call-1",
) -> ToolOutcomeFact:
    """One committed ledger fact, as the ledger reads it back."""
    return ToolOutcomeFact(
        fact_seq=seq,
        fact_id=f"fact-{seq}",
        run_id="run-1",
        thread_id="thread-1",
        logical_call_id=logical_call_id,
        tool_name=tool_name,
        model_attempt=model_attempt,
        execution_attempt=execution_attempt,
        provider_tool_call_id=provider_tool_call_id,
        runtime_attempt_id="attempt-1",
        runtime_attempt_generation=1,
        thread_generation=1,
        business_operation_id=business_operation_id,
        kind=kind,
        outcome_code=outcome_code,
        field_paths=(),
        created_at=datetime.now(timezone.utc),
    )


@dataclass(frozen=True)
class _Ownership:
    """The Runtime identity a fenced ledger write commits under."""

    thread_id: str = "thread-1"
    run_id: str = "run-1"
    attempt_id: str = "attempt-1"
    attempt_generation: int = 1
    thread_generation: int = 1


class _Ledger:
    """The ledger port: canned facts, recorded observations."""

    def __init__(self, facts: Sequence[ToolOutcomeFact] = ()) -> None:
        self.facts = list(facts)
        self.recorded: list[ToolOutcomeObservation] = []

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
        self.recorded.extend(observations)
        return len(observations)

    def read_run(self, run_id: str) -> Sequence[ToolOutcomeFact]:
        return tuple(self.facts)


def _declared_contract() -> ToolContract:
    """A contract carrying the policies a reconciliation needs — TEST ONLY.

    It is never registered in ``TOOL_CONTRACTS``: the case proves the
    decision path, it does not declare a policy for a real tool.
    """
    return replace(
        contract_for("save_memory"),
        operation_identity="test-only: synthetic business operation",
        retry=PolicyRecord(
            "declared", "test-only declared retry", "tests/host"
        ),
        reconciliation=PolicyRecord(
            "declared", "test-only declared reconciliation", "tests/host"
        ),
        blocked_slices=(),
    )


def _chunk() -> Chunk:
    return Chunk(
        text=COMPANY_TEXT,
        source_ref=Locator(document_id="AAPL-2023Q3", version="d" * 64, unit=3),
        score=1.5,
    )


def _profile_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / "ws" / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _call(args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "retrieve", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _run(graph: Any) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": f"thread-{uuid.uuid4()}",
        "run_id": "run-1",
    }
    configurable.update(IDENTITY)
    return graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": configurable},
    )


def _tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [
        message
        for message in state.get("messages") or []
        if isinstance(message, ToolMessage)
    ]


# ----- the decision ---------------------------------------------------------


def test_a_known_failure_on_a_read_effect_tool_is_not_retried() -> None:
    """A read that failed does not get an automatic second attempt."""
    contract = contract_for("retrieve")
    facts = [
        _committed_fact(
            "valid_input_accepted",
            seq=1,
            logical_call_id="retrieve#1",
            business_operation_id=None,
            tool_name="retrieve",
            execution_attempt=None,
        ),
        _committed_fact(
            "execution_started",
            seq=2,
            logical_call_id="retrieve#1",
            business_operation_id=None,
            tool_name="retrieve",
        ),
        _committed_fact(
            "known_failed",
            seq=3,
            logical_call_id="retrieve#1",
            business_operation_id=None,
            tool_name="retrieve",
            outcome_code="tool_result_error",
        ),
    ]

    decision = execution_decision(contract, facts, logical_call_id="retrieve#1")

    # The contract declares no retry, so the failure is final for now.
    assert contract.retry.status != "declared"
    assert decision.kind == "retry_not_permitted"
    assert decision.observed_kind == "known_failed"
    assert refusal_code(decision) == "tool_retry_not_permitted"


def test_a_declared_retry_policy_is_the_only_path_from_a_known_failure() -> None:
    """With a declared policy the same operation may run again."""
    contract = _declared_contract()
    facts = [
        _committed_fact("known_failed", seq=1, outcome_code="tool_result_error")
    ]

    decision = execution_decision(contract, facts, business_operation_id="biz-1")

    assert contract.retry.status == "declared"
    assert decision.kind == "execute"
    assert refusal_code(decision) is None


def test_an_unknown_write_outcome_is_never_read_as_no_effect() -> None:
    """An uncertain effect stays uncertain; it is not a free retry."""
    contract = contract_for("save_memory")
    facts = [
        _committed_fact("execution_started", seq=1),
        _committed_fact(
            "outcome_unknown", seq=2, outcome_code="tool_result_error"
        ),
    ]

    decision = execution_decision(contract, facts, business_operation_id="biz-1")

    assert decision.kind != "execute"
    assert decision.kind == "reconciliation_policy_open"
    assert refusal_code(decision) == "tool_reconciliation_policy_open"


def test_a_declared_reconciliation_policy_reports_reconciliation_required() -> None:
    contract = _declared_contract()
    facts = [
        _committed_fact(
            "outcome_unknown", seq=1, outcome_code="tool_result_error"
        )
    ]

    decision = execution_decision(contract, facts, business_operation_id="biz-1")

    assert decision.kind == "reconciliation_required"
    assert refusal_code(decision) == "tool_outcome_unknown_reconciliation_required"


def test_a_duplicate_delivery_of_a_known_success_is_not_executed_again() -> None:
    contract = contract_for("save_memory")
    facts = [_committed_fact("succeeded", seq=1)]

    decision = execution_decision(contract, facts, business_operation_id="biz-1")

    assert decision.kind == "duplicate_delivery"
    assert refusal_code(decision) == "tool_duplicate_delivery"
    # Another business operation is untouched by that fact.
    assert (
        execution_decision(contract, facts, business_operation_id="biz-2").kind
        == "execute"
    )


def test_a_decision_needs_an_identity_to_scope_its_facts() -> None:
    with pytest.raises(ValueError):
        execution_decision(contract_for("retrieve"), [])


def test_a_trailing_execution_started_without_a_terminal_is_uncertain() -> None:
    """An in-flight fact with no later terminal is an unknown effect.

    A crash between ``execution_started`` and its terminal fact leaves the
    effect undecided; the next delivery must not read it as "no effect".
    """
    contract = contract_for("save_memory")
    facts = [_committed_fact("execution_started", seq=1)]

    decision = execution_decision(
        contract, facts, logical_call_id="save_memory#1"
    )

    assert decision.kind == "reconciliation_policy_open"
    assert decision.observed_kind == "execution_started"
    assert refusal_code(decision) == "tool_reconciliation_policy_open"


def test_the_last_deciding_fact_wins_over_an_earlier_in_flight_fact() -> None:
    contract = _declared_contract()
    facts = [
        _committed_fact("execution_started", seq=1),
        _committed_fact(
            "outcome_unknown", seq=2, outcome_code="tool_result_error"
        ),
        _committed_fact("reconciled", seq=3, outcome_code="proven_not_applied"),
    ]

    decision = execution_decision(contract, facts, business_operation_id="biz-1")

    assert decision.kind == "execute"
    assert decision.observed_kind == "reconciled"


# ----- writing the reconciliation -------------------------------------------


def test_a_reconciliation_is_refused_without_a_declared_policy() -> None:
    ledger = _Ledger()

    with pytest.raises(ReconciliationRefused) as refused:
        record_reconciliation(
            ledger,
            [_committed_fact("outcome_unknown")],
            contract=contract_for("save_memory"),
            business_operation_id="biz-1",
            resolution_code="proven_not_applied",
            ownership=_Ownership(),
        )

    assert refused.value.reason == "policy_not_declared"
    assert ledger.recorded == []


def test_a_reconciliation_is_refused_without_a_business_operation_id() -> None:
    ledger = _Ledger()

    with pytest.raises(ReconciliationRefused) as refused:
        record_reconciliation(
            ledger,
            [_committed_fact("outcome_unknown")],
            contract=_declared_contract(),
            business_operation_id=None,
            resolution_code="proven_not_applied",
            ownership=_Ownership(),
        )

    assert refused.value.reason == "business_operation_missing"
    assert ledger.recorded == []


def test_a_reconciled_unknown_operation_may_execute_again() -> None:
    """The positive transition: unknown -> reconciled -> execute."""
    contract = _declared_contract()
    ledger = _Ledger()
    unknown = [
        _committed_fact("execution_started", seq=1),
        _committed_fact(
            "outcome_unknown", seq=2, outcome_code="tool_result_error"
        ),
    ]
    assert (
        execution_decision(contract, unknown, business_operation_id="biz-1").kind
        == "reconciliation_required"
    )

    written = record_reconciliation(
        ledger,
        unknown,
        contract=contract,
        business_operation_id="biz-1",
        resolution_code="proven_not_applied",
        ownership=_Ownership(),
    )

    assert written == 1
    observation = ledger.recorded[0]
    assert observation.kind == "reconciled"
    assert observation.outcome_code == "proven_not_applied"
    assert observation.business_operation_id == "biz-1"
    assert observation.logical_call_id == "save_memory#1"
    # The proof carries no value: only the typed resolution.
    assert observation.field_paths == ()

    decision = execution_decision(
        contract, [*unknown, observation], business_operation_id="biz-1"
    )
    assert decision.kind == "execute"


def test_a_reconciliation_that_proves_the_effect_applied_is_not_replayed() -> None:
    contract = _declared_contract()
    ledger = _Ledger()
    unknown = [_committed_fact("outcome_unknown", seq=1)]

    record_reconciliation(
        ledger,
        unknown,
        contract=contract,
        business_operation_id="biz-1",
        resolution_code="proven_applied",
        ownership=_Ownership(),
    )

    decision = execution_decision(
        contract, [*unknown, ledger.recorded[0]], business_operation_id="biz-1"
    )
    assert decision.kind == "duplicate_delivery"


# ----- the graph guard ------------------------------------------------------


def test_the_graph_refuses_a_duplicate_delivery_before_dispatch(
    tmp_path: Path,
) -> None:
    """The same provider attempt arriving twice runs the tool once.

    The rule is reachable today and needs no business operation id: the
    run's committed facts already carry the ``succeeded`` outcome of this
    (logical call, provider attempt) pair.
    """
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "succeeded",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-dup",
            )
        ]
    )
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "call-dup"), AIMessage(content="Answer.")]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )

    out = _run(graph)

    # The tool never ran a second time.
    assert port.calls == []
    assert out.get("retrieve_attempted") is not True
    messages = _tool_messages(out)
    assert json.loads(messages[-1].content) == {"error": "tool_duplicate_delivery"}
    assert messages[-1].status == "error"
    # A refusal starts no execution, so it records no new fact.
    assert ledger.recorded == []


def test_a_fresh_provider_attempt_still_executes(tmp_path: Path) -> None:
    """A different provider attempt is a new delivery, not a duplicate."""
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "succeeded",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-old",
            )
        ]
    )
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "call-new"), AIMessage(content="Answer.")]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )

    out = _run(graph)

    assert out.get("retrieve_attempted") is True
    assert port.calls
    assert [observation.kind for observation in ledger.recorded] == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]


def test_a_call_without_a_business_operation_identity_still_dispatches(
    tmp_path: Path,
) -> None:
    """The current tools carry no identity, so their behavior is unchanged."""
    ledger = _Ledger()
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[_call({"query": QUERY}, "call-ok"), AIMessage(content="Answer.")]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )

    out = _run(graph)

    assert out.get("retrieve_attempted") is True
    assert port.calls
    assert [observation.kind for observation in ledger.recorded] == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]


def _declared_retrieve_contract() -> ToolContract:
    """A retrieve contract with a declared reconciliation policy — TEST ONLY."""
    return replace(
        contract_for("retrieve"),
        reconciliation=PolicyRecord(
            "declared", "test-only declared reconciliation", "tests/host"
        ),
    )


def _guarded_graph(
    tmp_path: Path,
    ledger: _Ledger,
    model: ScriptedChatModel,
    port: FakeRetrievePort,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )


def test_the_graph_refuses_a_redelivered_call_after_an_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tool is decided by the ledger, scoped to the logical call.

    No registered tool can derive a business operation id yet, so the guard
    must fall back to ``outcome.logical_call_id`` instead of dispatching a
    re-delivered call whose last effect fact is ``outcome_unknown``.
    """
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "outcome_unknown",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-1",
                outcome_code="tool_result_error",
            )
        ]
    )
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[
            _call({"query": QUERY}, "call-1"),
            AIMessage(content="Answer."),
        ]
    )
    graph = _guarded_graph(tmp_path, ledger, model, port)
    monkeypatch.setattr(
        graph_module,
        "_tool_contract_for",
        lambda name: _declared_retrieve_contract(),
    )

    out = _run(graph)

    # The port/body is not called again, and the refusal is the typed code.
    assert port.calls == []
    assert out.get("retrieve_attempted") is not True
    messages = _tool_messages(out)
    assert json.loads(messages[-1].content) == {
        "error": "tool_outcome_unknown_reconciliation_required"
    }
    assert messages[-1].status == "error"
    # A refusal starts no execution, so it records no new fact.
    assert ledger.recorded == []


def test_the_graph_refuses_a_call_whose_execution_started_has_no_terminal(
    tmp_path: Path,
) -> None:
    """A trailing in-flight fact with no later terminal is uncertain."""
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "execution_started",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-1",
            )
        ]
    )
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[
            _call({"query": QUERY}, "call-1"),
            AIMessage(content="Answer."),
        ]
    )
    graph = _guarded_graph(tmp_path, ledger, model, port)

    out = _run(graph)

    assert port.calls == []
    messages = _tool_messages(out)
    # ``retrieve`` declares no reconciliation policy, so the open policy
    # decides; the call is still refused.
    assert json.loads(messages[-1].content) == {
        "error": "tool_reconciliation_policy_open"
    }
    assert messages[-1].status == "error"


def test_the_graph_runs_a_call_after_a_proven_not_applied_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "outcome_unknown",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-1",
                outcome_code="tool_result_error",
            ),
            _committed_fact(
                "reconciled",
                seq=2,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-1",
                outcome_code="proven_not_applied",
            ),
        ]
    )
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[
            _call({"query": QUERY}, "call-1"),
            AIMessage(content="Answer."),
        ]
    )
    graph = _guarded_graph(tmp_path, ledger, model, port)
    monkeypatch.setattr(
        graph_module,
        "_tool_contract_for",
        lambda name: _declared_retrieve_contract(),
    )

    out = _run(graph)

    assert port.calls
    assert out.get("retrieve_attempted") is True
    messages = _tool_messages(out)
    assert "chunks" in json.loads(messages[-1].content)


def test_the_graph_refuses_a_call_after_a_proven_applied_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "outcome_unknown",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-1",
                outcome_code="tool_result_error",
            ),
            _committed_fact(
                "reconciled",
                seq=2,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-1",
                outcome_code="proven_applied",
            ),
        ]
    )
    port = FakeRetrievePort(hits_by_query={QUERY: [_chunk()]})
    model = ScriptedChatModel(
        script=[
            _call({"query": QUERY}, "call-1"),
            AIMessage(content="Answer."),
        ]
    )
    graph = _guarded_graph(tmp_path, ledger, model, port)
    monkeypatch.setattr(
        graph_module,
        "_tool_contract_for",
        lambda name: _declared_retrieve_contract(),
    )

    out = _run(graph)

    assert port.calls == []
    messages = _tool_messages(out)
    assert json.loads(messages[-1].content) == {"error": "tool_duplicate_delivery"}
    assert messages[-1].status == "error"


def test_two_distinct_calls_in_one_turn_are_not_conflated(
    tmp_path: Path,
) -> None:
    """A re-delivered call and a fresh sibling have distinct logical calls."""
    ledger = _Ledger(
        facts=[
            _committed_fact(
                "succeeded",
                seq=1,
                logical_call_id="retrieve#1",
                business_operation_id=None,
                tool_name="retrieve",
                provider_tool_call_id="call-dup",
            )
        ]
    )
    port = FakeRetrievePort(
        hits_by_query={QUERY: [_chunk()], "revenue": [_chunk()]}
    )
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": QUERY},
                        "id": "call-dup",
                        "type": "tool_call",
                    },
                    {
                        "name": "retrieve",
                        "args": {"query": "revenue"},
                        "id": "call-new",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="Answer."),
        ]
    )
    graph = _guarded_graph(tmp_path, ledger, model, port)

    out = _run(graph)

    # The duplicate is refused; the sibling still runs exactly once.
    assert [call["query"] for call in port.calls] == ["revenue"]
    messages = _tool_messages(out)
    assert json.loads(messages[0].content) == {"error": "tool_duplicate_delivery"}
    assert "chunks" in json.loads(messages[1].content)


# ----- the reconciliation half of the guard --------------------------------


def _guarded_call(
    tracker: CorrectionTracker,
    *,
    name: str = "save_memory",
    args: dict[str, Any] | None = None,
    call_id: str = "call-1",
) -> graph_module._ValidatedToolCall:
    """One accepted call, validated through the real tracker."""
    submitted = args or {"title": "t", "body": "b"}
    tracker.begin_model_attempt([{"name": name, "args": submitted, "id": call_id}])
    outcome = tracker.record_validation(name, submitted, tool_call_id=call_id)
    assert outcome.accepted, outcome
    return graph_module._ValidatedToolCall(
        name=name,
        args=submitted,
        tool_call_id=call_id,
        outcome=outcome,
        execution_attempt=tracker.next_execution_attempt(
            outcome.logical_call_id
        ),
    )


def test_the_operation_identity_path_is_inactive_for_every_registered_tool() -> None:
    """No registered contract declares a DERIVABLE operation identity."""
    for name in TOOL_CONTRACTS:
        outcome = graph_module._business_operation_id_for_call(
            graph_module._ValidatedToolCall(
                name=name,
                args={},
                tool_call_id="call-1",
                outcome=None,
            )
        )
        assert outcome is None, name


def test_blocked_slices_do_not_override_the_declared_reconciliation_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared policies decide; ``blocked_slices`` is not a shortcut.

    ``execution_decision`` already answers ``reconciliation_policy_open``
    and ``retry_not_permitted`` from an UNDECLARED policy, so a blocked
    slice must not turn a DECLARED policy into an open one.
    """
    contract = replace(_declared_contract(), blocked_slices=("reconciliation",))
    monkeypatch.setattr(
        graph_module,
        "_tool_contract_for",
        lambda name: contract,
    )
    monkeypatch.setattr(
        graph_module, "_business_operation_id_for_call", lambda call: "biz-1"
    )
    facts = [
        _committed_fact("outcome_unknown", seq=1, outcome_code="tool_result_error")
    ]
    call = _guarded_call(CorrectionTracker("thread-1", "run-1"))

    refusal = graph_module._execution_refusal(facts, call)

    assert refusal is not None
    assert json.loads(str(refusal.content)) == {
        "error": "tool_outcome_unknown_reconciliation_required"
    }


def test_the_guard_scopes_to_the_logical_call_without_an_operation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no business operation id, the logical call id scopes the facts."""
    contract = replace(
        contract_for("save_memory"),
        reconciliation=PolicyRecord(
            "declared", "test-only declared reconciliation", "tests/host"
        ),
    )
    monkeypatch.setattr(graph_module, "_tool_contract_for", lambda name: contract)
    facts = [
        _committed_fact("outcome_unknown", seq=1, outcome_code="tool_result_error")
    ]
    call = _guarded_call(CorrectionTracker("thread-1", "run-1"))

    # Every registered tool still derives no business operation id; the
    # guard must not read that as "no decision".
    assert graph_module._business_operation_id_for_call(call) is None
    refusal = graph_module._execution_refusal(facts, call)

    assert refusal is not None
    assert json.loads(str(refusal.content)) == {
        "error": "tool_outcome_unknown_reconciliation_required"
    }


def test_a_declared_reconciliation_policy_requires_a_proof_before_a_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _declared_contract()
    monkeypatch.setattr(graph_module, "_tool_contract_for", lambda name: contract)
    monkeypatch.setattr(
        graph_module, "_business_operation_id_for_call", lambda call: "biz-1"
    )
    facts = [
        _committed_fact("outcome_unknown", seq=1, outcome_code="tool_result_error")
    ]
    call = _guarded_call(CorrectionTracker("thread-1", "run-1"))

    refusal = graph_module._execution_refusal(facts, call)

    assert refusal is not None
    assert json.loads(str(refusal.content)) == {
        "error": "tool_outcome_unknown_reconciliation_required"
    }


def test_a_reconciled_unknown_operation_dispatches_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _declared_contract()
    monkeypatch.setattr(graph_module, "_tool_contract_for", lambda name: contract)
    monkeypatch.setattr(
        graph_module, "_business_operation_id_for_call", lambda call: "biz-1"
    )
    facts = [
        _committed_fact("outcome_unknown", seq=1, outcome_code="tool_result_error"),
        _committed_fact(
            "reconciled", seq=2, outcome_code="proven_not_applied"
        ),
    ]
    call = _guarded_call(CorrectionTracker("thread-1", "run-1"))

    assert graph_module._execution_refusal(facts, call) is None


# ----- the fenced write against real PostgreSQL -----------------------------


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_tool_reconcile")
    try:
        yield runtime_pg_support.scoped_url(dsn, schema)
    finally:
        runtime_pg_support.drop_schema(dsn, schema)


@pytest.fixture(scope="module")
def store(runtime_url: str) -> PostgresRuntimeStore:
    runtime_store = PostgresRuntimeStore(runtime_url)
    runtime_store.setup()
    return runtime_store


@pytest.fixture(scope="module")
def ledger(runtime_url: str) -> PostgresToolOutcomeLedger:
    outcome_ledger = PostgresToolOutcomeLedger(runtime_url)
    outcome_ledger.setup()
    return outcome_ledger


@pytest.fixture()
def coordinator(store: PostgresRuntimeStore) -> RuntimeCoordinator:
    return RuntimeCoordinator(store, queue_capacity=8, lease_seconds=30.0)


def _claim(coordinator: RuntimeCoordinator) -> Any:
    thread = f"thread-{uuid.uuid4()}"
    coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "reconcile me"},
    )
    outcome = coordinator.claim(thread_id=thread, worker_id="worker-a")
    assert outcome.claimed is True, outcome.reason
    assert outcome.lease is not None
    return outcome.lease


def _expire(url: str, attempt_id: str) -> None:
    """Expire one lease by an explicit row update, never by sleeping."""
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(
            "UPDATE fin_runtime_worker_attempt SET "
            "lease_expires_at = now() - interval '1 second' "
            "WHERE attempt_id = %s",
            (attempt_id,),
        )


def _unknown_observations() -> list[ToolOutcomeObservation]:
    return [
        ToolOutcomeObservation(
            logical_call_id="save_memory#1",
            tool_name="save_memory",
            model_attempt=1,
            kind="execution_started",
            provider_tool_call_id="call-1",
            execution_attempt=1,
            business_operation_id="biz-1",
        ),
        ToolOutcomeObservation(
            logical_call_id="save_memory#1",
            tool_name="save_memory",
            model_attempt=1,
            kind="outcome_unknown",
            provider_tool_call_id="call-1",
            execution_attempt=1,
            business_operation_id="biz-1",
            outcome_code="tool_result_error",
        ),
    ]


@pytest.mark.runtime_pg
def test_a_stale_worker_cannot_reconcile_an_unknown_outcome(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    """A late worker cannot resolve an outcome it no longer owns."""
    lease = _claim(coordinator)
    observations = _unknown_observations()
    assert (
        ledger.record(
            observations,
            thread_id=lease.thread_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            attempt_generation=lease.attempt_generation,
            thread_generation=lease.thread_generation,
        )
        == len(observations)
    )

    # Explicit ordering: the old lease is expired by a row update, then the
    # takeover claim mints the next generation. No sleep infers the race.
    _expire(ledger.connection_url, lease.attempt_id)
    takeover = coordinator.claim(
        thread_id=lease.thread_id, worker_id="worker-b", run_id=lease.run_id
    )
    assert takeover.claimed is True, takeover.reason
    assert takeover.lease is not None
    assert takeover.lease.attempt_generation == lease.attempt_generation + 1

    facts = ledger.read_run(lease.run_id)
    contract = _declared_contract()
    with pytest.raises(CheckpointOwnershipLost):
        record_reconciliation(
            ledger,
            facts,
            contract=contract,
            business_operation_id="biz-1",
            resolution_code="proven_not_applied",
            ownership=lease,
        )

    # The fenced write committed nothing.
    assert [fact.kind for fact in ledger.read_run(lease.run_id)] == [
        "execution_started",
        "outcome_unknown",
    ]

    # The current owner can reconcile, and a fresh process reads it back.
    assert (
        record_reconciliation(
            ledger,
            facts,
            contract=contract,
            business_operation_id="biz-1",
            resolution_code="proven_not_applied",
            ownership=takeover.lease,
        )
        == 1
    )
    committed = ledger.read_run(lease.run_id)
    assert [fact.kind for fact in committed] == [
        "execution_started",
        "outcome_unknown",
        "reconciled",
    ]
    assert committed[-1].outcome_code == "proven_not_applied"
    assert committed[-1].business_operation_id == "biz-1"

    child = runtime_pg_support.run_child(
        CHILD_READBACK,
        dsn=ledger.connection_url,
        env={"FIN_RECONCILE_RUN": lease.run_id},
    )
    assert child["pid"] != os.getpid()
    assert child["kinds"] == [
        "execution_started",
        "outcome_unknown",
        "reconciled",
    ]
    assert child["codes"][-1] == "proven_not_applied"
    assert child["operations"] == ["biz-1", "biz-1", "biz-1"]
