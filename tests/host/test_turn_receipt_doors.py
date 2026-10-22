"""Every door reports onto the receipt of the turn it continues.

The executor already ends a run on its turn's receipt; what these cases
pin is the wiring in front of it. A send is the one door that may bring
a receipt into existence, because it is the one door that accepts a new
logical turn; the clarification door counts its accepted reply onto the
paused run's own receipt; the crash-resume door and the background
auto-resumer close that same receipt rather than reporting a turn of
their own.

Hermetic, like the rest of the host suite: ScriptedChatModel +
InMemorySaver + the committed corpus tree, no live model and no
database.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import accept_input, decide
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    PendingQuestion,
    ReconciliationProposal,
)
from fin.host.turn_receipt_store import (
    STAGE_METRIC_FIELDS,
    STAGE_METRIC_KEYS,
    InMemoryReceiptStore,
    TurnReceipt,
)
from fin.host.turn_receipts import TurnReceiptProjector
from fin.host.workspace_http import _resume_run_stream

KNOWN_IDS = {"AAPL-2022Q3"}
RESEARCH = "What was AAPL net sales in 2023 Q3?"
QUESTION_ID = "q-seeded"
CHOICES = ("公司", "期间")
PAUSED_RUN_ID = "run-receipt-paused"


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def store(http_app: FastAPI) -> Any:
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    memory: Any = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: memory
    http_app.dependency_overrides[get_graph] = lambda: _graph()
    return memory


def _graph(*, model: Any = None) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model
        or ScriptedChatModel(
            script=[AIMessage(content="Net sales grew year over year.")]
        ),
        known_document_ids=KNOWN_IDS,
    )


def _receipts(store: Any) -> TurnReceiptProjector:
    """The projection the mounted app derives from this store.

    Built the way the router's own dependency builds it, so the receipt
    these cases read is the one the doors wrote — not a second store
    that happens to agree.
    """
    from fin.host.turn_receipts import default_receipts

    return default_receipts(store)


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "研究"}).json()["id"]


def _send(client: TestClient, thread_id: str, question: str, **door: Any) -> Any:
    return client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": question, **door},
    )


def _cfg(thread_id: str, **configurable: Any) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def _pause_run(g: Any, tid: str, *, run_id: str) -> str:
    cfg = _cfg(tid, run_id=run_id)
    stream = g.stream(
        reset_input_state(RESEARCH), config=cfg, stream_mode="values"
    )
    for _ in stream:
        break
    stream.close()
    assert g.pending_nodes(tid), "the run did not pause"
    return run_id


def _seed_pending_question(g: Any, tid: str, *, run_id: str) -> None:
    """Commit a clarification the way the reconcile node does."""
    state = g.task_runtime_state(tid)
    state, accepted = accept_input(
        state,
        kind=INPUT_MESSAGE,
        payload=RESEARCH,
        provenance="workspace_send",
    )
    committed = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_MESSAGE,
            goal=RESEARCH,
            question=PendingQuestion(
                question_id=QUESTION_ID,
                task_id="",
                task_version=1,
                prompt="请补充：公司",
                choices=CHOICES,
                input_version=0,
            ),
        ),
    )
    g._graph.update_state(
        _cfg(tid, run_id=run_id), {"task_runtime": committed.to_state_dict()}
    )


def _paused_thread(client: TestClient, store: Any) -> tuple[str, Any]:
    """A thread paused mid-run, holding the receipt a send would have left.

    The pause is a real mid-run checkpoint on the real graph, and the
    receipt is created exactly the way the send door creates one — at
    acceptance, under the run's own id. Seeding it here stands in for
    the send that got the thread into this state; the doors under test
    are the ones that follow.
    """
    from fin.host.http import get_graph

    g = _graph()
    client.app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)
    _pause_run(g, tid, run_id=PAUSED_RUN_ID)
    _seed_pending_question(g, tid, run_id=PAUSED_RUN_ID)
    assert g.checkpoint_run_id(tid) == PAUSED_RUN_ID
    _receipts(store).begin(
        thread_id=tid,
        turn_id=PAUSED_RUN_ID,
        accepted_input_version=g.task_runtime_state(tid).input_version,
    )
    _receipts(store).paused(
        thread_id=tid,
        turn_id=PAUSED_RUN_ID,
        semantic_result="clarify",
    )
    return tid, g


def _one_receipt(projector: TurnReceiptProjector, thread_id: str) -> TurnReceipt:
    """The one receipt of this turn, and proof the thread holds only it.

    Reading by key cannot see a second receipt: a store that wrote one
    under a different ``turn_id`` would answer this read exactly the same
    way. The count is what makes the identity claim checkable, so it is
    asserted on every case rather than in one place that a later case
    could skip.
    """
    receipts = projector.store.list_receipts(thread_id)
    assert len(receipts) == 1, (
        f"expected exactly one receipt for the thread, got "
        f"{[r.turn_id for r in receipts]}"
    )
    receipt = receipts[0]
    assert receipt.turn_id == PAUSED_RUN_ID
    return receipt


# ----- the send door is the only place a receipt is created -----


def test_a_send_ends_on_the_receipt_of_the_turn_it_started(
    client: TestClient, store: Any
) -> None:
    tid = _new_thread(client)

    body = _send(client, tid, RESEARCH).text

    assert "event: status" in body
    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns) == 1
    receipt = _receipts(store).read(tid, turns[0]["run_id"])
    assert receipt is not None
    assert receipt.turn_id == turns[0]["run_id"]
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "done"
    assert receipt.terminal_at is not None
    # Sets, not order: a jsonb round-trip through the durable store
    # reorders its keys, so an order assertion here would pin the
    # in-memory twin's dict layout and refuse the row the database
    # legitimately returns.
    assert set(receipt.stage_metrics) == set(STAGE_METRIC_KEYS)
    for entry in receipt.stage_metrics.values():
        assert tuple(entry) == STAGE_METRIC_FIELDS


def test_a_research_turn_reports_the_model_calls_it_actually_made(
    client: TestClient, store: Any
) -> None:
    """The real graph, the real boundary, and a total that adds up.

    This is the case the executor-level cases cannot reach: a research
    turn that calls the model, asks it for tools, calls it again to
    answer, and is bounced by the citation gate until it cites. Every one
    of those calls is a real invocation, and the receipt's inclusive
    total is exactly their number.
    """
    from fin.host.http import get_graph

    tool_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "retrieve", "args": {"query": "net sales"}, "id": "c1"}
        ],
    )
    graph = _graph(
        model=ScriptedChatModel(
            script=[
                tool_call,
                AIMessage(content="Net sales grew year over year."),
                AIMessage(content="Net sales grew year over year. [1]"),
            ]
        )
    )
    client.app.dependency_overrides[get_graph] = lambda: graph

    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    receipt = _receipts(store).read(tid, turns[0]["run_id"])
    assert receipt is not None

    parts = (
        "runtime_reconciliation",
        "grounded_decision",
        "tool_planning",
        "other_model",
    )
    metrics = receipt.stage_metrics
    assert metrics["tool_planning"]["calls"] >= 1, (
        "the run asked the model for tools and no call was counted as such"
    )
    assert metrics["grounded_decision"]["calls"] >= 1
    assert metrics["model"]["calls"] == sum(metrics[key]["calls"] for key in parts)
    # The script authors two calls and the citation gate bounces for
    # more, so the exact total is the graph's business; what this pins is
    # that every one of those invocations is inside the total and none of
    # them is counted twice.
    assert metrics["model"]["calls"] >= 3
    # The reconciliation node made no model call, and the count says so
    # rather than assuming one per accepted input.
    assert metrics["runtime_reconciliation"]["calls"] == 0


# ----- the clarification door counts onto the paused turn -----


def test_a_reply_counts_once_onto_the_receipt_it_continues(
    client: TestClient, store: Any
) -> None:
    tid, g = _paused_thread(client, store)
    before = g.task_runtime_state(tid)

    _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])

    after = g.task_runtime_state(tid)
    receipt = _one_receipt(_receipts(store), tid)
    assert receipt.accepted_input_count == 2
    assert receipt.accounting_complete is False
    # The receipt records the version Task Runtime's acceptance reached,
    # not a count it keeps itself.
    assert receipt.accepted_input_version == after.input_version
    assert after.input_version == before.input_version + 1
    # The continued turn is the paused one: one receipt, one run id.
    assert receipt.turn_id == PAUSED_RUN_ID
    assert receipt.execution_state == "terminal"
    turn_ids = [
        turn["run_id"] for turn in client.get(f"/api/threads/{tid}").json()["turns"]
    ]
    assert turn_ids == [PAUSED_RUN_ID]


def test_an_identical_retry_counts_no_second_input(
    client: TestClient, store: Any
) -> None:
    tid, _g = _paused_thread(client, store)
    _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])
    counted = _one_receipt(_receipts(store), tid)

    _send(client, tid, "", question_id=QUESTION_ID, choice_id=CHOICES[0])

    retried = _one_receipt(_receipts(store), tid)
    assert retried.accepted_input_count == counted.accepted_input_count


# ----- the crash-resume door closes the receipt it reopened -----


def test_a_bodyless_resume_closes_the_turn_it_reopened(
    client: TestClient, store: Any
) -> None:
    tid, _g = _paused_thread(client, store)

    response = client.post(f"/api/threads/{tid}/resume")

    assert response.status_code == 200
    receipt = _one_receipt(_receipts(store), tid)
    assert receipt.execution_state == "terminal"
    assert receipt.technical_result == "done"
    assert receipt.accepted_input_count == 1
    turns = client.get(f"/api/threads/{tid}").json()["turns"]
    assert [turn["run_id"] for turn in turns] == [PAUSED_RUN_ID]


# ----- the background auto-resumer is the same seam -----


def test_the_background_auto_resume_closes_the_same_receipt(
    client: TestClient, store: Any
) -> None:
    from fin.host.run_control import RunControl

    tid, g = _paused_thread(client, store)
    projector = _receipts(store)
    before = _one_receipt(projector, tid)

    for _frame in _resume_run_stream(
        g,
        store,
        run_control=RunControl(),
        thread_id=tid,
        run_id=PAUSED_RUN_ID,
        resume_input=None,
        resume_config=_cfg(tid, run_id=PAUSED_RUN_ID),
        receipts=projector,
    ):
        pass

    resumed = _one_receipt(projector, tid)
    assert resumed.turn_id == before.turn_id
    assert resumed.started_at == before.started_at
    assert resumed.execution_state == "terminal"
    assert [turn.id for turn in store.list_runs(tid)] == [PAUSED_RUN_ID]


def test_a_background_resume_without_a_projection_still_lands_its_row(
    client: TestClient, store: Any
) -> None:
    from fin.host.run_control import RunControl

    tid, g = _paused_thread(client, store)
    _receipts(store).read(tid, PAUSED_RUN_ID)

    for _frame in _resume_run_stream(
        g,
        store,
        run_control=RunControl(),
        thread_id=tid,
        run_id=PAUSED_RUN_ID,
        resume_input=None,
        resume_config=_cfg(tid, run_id=PAUSED_RUN_ID),
    ):
        pass

    assert [turn.id for turn in store.list_runs(tid)] == [PAUSED_RUN_ID]


def test_the_receipt_store_follows_the_store_the_route_writes_to() -> None:
    from fin.host.thread_store import InMemoryThreadStore
    from fin.host.turn_receipts import default_receipts

    memory = InMemoryThreadStore()
    first = default_receipts(memory)
    second = default_receipts(memory)

    # Two projections over one store would each answer for half the
    # turns, and a background resume could not see what the door wrote.
    assert first is second
    assert isinstance(first.store, InMemoryReceiptStore)
