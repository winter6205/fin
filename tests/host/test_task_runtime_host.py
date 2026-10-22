"""Host integration boundary tests for the question and task runtime.

Covers the host seams around the pure decision logic (already pinned by
``test_task_runtime_contract.py`` / ``_lifecycle.py`` / ``_outcome.py``):

* plans/question-task-runtime.md **T2** — the send door durably accepts an
  ordinary input BEFORE the graph runs and the one ``reconcile_task`` node
  commits it once (SC-1). The node is idempotent and never manufactures an
  accepted input on a resume/replay.
* **T3/T4** — a taskless greeting advances input history but commits no task
  and no task outcome (SC-3); the thread-scoped envelope survives a
  run-scoped reset and compaction (SC-5).
* **T5** — ADR 0104 retired the internal retrieval-mode picker: a routing
  residual is assigned host-side (``graph`` / ``decision_tier="fallback"``)
  and opens NO window, while the windows that are still valid (the confirmed
  Fast extraction proposal) still open, pause and resume under the
  ``extract_method_choice -> reconcile_task -> compaction_gate`` topology
  (SC-1/SC-4).
* The wire projection, stand-in tolerance, task/turn independence, and the
  empty / concurrent / legacy-checkpoint boundary classes (SC-5, SC-8).

Hermetic: ScriptedChatModel + InMemorySaver + the committed corpus tree only.
No live model, network, or iknow-memory backend.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import ExtractedPage, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.session.session_compaction import session_token_budget
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import accept_input, decide
from fin.host.task.task_runtime_types import (
    DISPOSITION_SWITCH,
    INPUT_MESSAGE,
    AcceptedInput,
    PendingQuestion,
    ReconciliationProposal,
    TaskRuntimeState,
)
from fin.retrieve import FakeRetrievePort


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Grounded, unnamed by the retrieval-tier rules -> the ordinary research
# leaf. The same utterance the choice-window suite uses.
RESEARCH = "What was AAPL net sales in 2023 Q3?"
GREETING = "Hi!"

MANIFEST_PATH = Path("data/corpus/manifest.json")
EXTRACTED_ROOT = Path("data/corpus/extracted")

# The wire fields of ``TaskRuntimeView`` — the frozen projection contract.
TASK_RUNTIME_WIRE_FIELDS = frozenset(
    {
        "current_task",
        "archived_tasks",
        "pending_question",
        "reconciliation",
        "task_outcome",
        "input_version",
        "input_history",
    }
)
EMPTY_TASK_RUNTIME_VIEW = {
    "current_task": None,
    "archived_tasks": [],
    "pending_question": None,
    "reconciliation": None,
    "task_outcome": None,
    "input_version": 0,
    "input_history": [],
}

_READ_MANIFEST: dict[str, tuple[ExtractedPage, ...]] | None = None


def _read_manifest() -> dict[str, tuple[ExtractedPage, ...]]:
    """The committed extracted tree as a ``{document_id: pages}`` read
    manifest (mirrors ``test_workspace_http.py``)."""
    global _READ_MANIFEST
    if _READ_MANIFEST is None:
        _READ_MANIFEST = load_extracted_tree(MANIFEST_PATH, EXTRACTED_ROOT)
    return _READ_MANIFEST


def _tree_read_adapter() -> FakeRetrievePort:
    return FakeRetrievePort(pages_by_doc=_read_manifest())


# ----- fixtures (same shape as test_workspace_http.py; tests are not a package) -----


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    return _read_manifest()[AAPL_2022Q3][0].text


@pytest.fixture
def http_app() -> FastAPI:
    """Fresh FastAPI app per test so dependency overrides do not leak."""
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    """In-memory thread store + a default scripted graph bound to the app."""
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, ThreadStore

    store: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store

    default_graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: default_graph
    return store


def _install_graph(
    http_app: FastAPI,
    script: list[AIMessage],
    *,
    adapter: Any = None,
    summarizer: Any = None,
    enable_choice_window: bool = False,
) -> Any:
    """Build a scripted graph and bind it to the app's ``get_graph``."""
    from fin.host.http import get_graph

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_IDS,
        adapter=adapter,
        summarizer=summarizer,
        enable_choice_window=enable_choice_window,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    """Same wire parser as ``test_workspace_http._parse_sse_events``."""
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:") :].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "研究"}).json()["id"]


def _send(
    client: TestClient,
    thread_id: str,
    question: str,
    **door: Any,
) -> Any:
    """POST one send. The ``door`` kwargs are the reply / restore fields
    the runtime contract added; ordinary sends pass none."""
    return client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": question, **door},
    )


def _task_runtime(client: TestClient, thread_id: str) -> dict[str, Any]:
    return client.get(f"/api/threads/{thread_id}").json()["task_runtime"]


def _cfg(thread_id: str, **configurable: Any) -> dict:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def _accepted_config(thread_id: str, accepted: AcceptedInput) -> dict:
    """The run config the send door hands the graph (T2 seam)."""
    return _cfg(thread_id, accepted_task_input=accepted.to_state_dict())


def _door_input(payload: str) -> dict[str, Any]:
    """What the send door hands the graph: the run-scoped reset plus the
    accepted-input envelope it committed for THIS run."""
    runtime, _accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload=payload,
        provenance="workspace_send",
    )
    state = reset_input_state(payload)
    state["task_runtime"] = runtime.to_state_dict()
    return state


def _accepted(payload: str, version: int = 1) -> AcceptedInput:
    return AcceptedInput(
        input_version=version,
        kind=INPUT_MESSAGE,
        payload=payload,
        provenance="workspace_send",
    )


def _fat_history() -> list:
    """~9_200 estimated tokens — far above the 90% trigger at 1_600."""
    tool_payload = "p" * 12_000
    messages: list = []
    for _ in range(4):
        messages.append(
            AIMessage(
                content="calling read_source",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {"source_ref": {"document_id": AAPL_2022Q3, "unit": 1}},
                        "id": "c-seed",
                        "type": "tool_call",
                    }
                ],
            )
        )
        messages.append(ToolMessage(content=tool_payload, tool_call_id="c-seed"))
    messages.append(HumanMessage(content="Break down AAPL 2023 Q3 revenue"))
    return messages


def _read_source_call(call_id: str = "c-read") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": AAPL_2022Q3,
                        "version": AAPL_2022Q3_VERSION,
                        "unit": 1,
                    },
                    "position": 0,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


# ===========================================================================
# SC-1 — the send door durably accepts an ordinary input and the reconcile
# node commits it once
# ===========================================================================


def test_send_accepts_input_and_commits_a_current_task_with_reconciliation(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """First send -> input history advanced once, a current task committed,
    and the one reconciliation marker recorded (SC-1)."""
    _install_graph(http_app, [AIMessage(content="Net sales grew.")])
    tid = _new_thread(client)

    assert _send(client, tid, RESEARCH).status_code == 200
    task_runtime = _task_runtime(client, tid)

    assert task_runtime["input_version"] == 1
    assert task_runtime["current_task"] is not None
    assert task_runtime["reconciliation"] is not None
    assert task_runtime["reconciliation"]["reconciled_input_version"] == 1
    assert task_runtime["reconciliation"]["disposition"] == "start"


def test_second_send_advances_input_version_by_exactly_one(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Two sends -> exactly two accepted inputs; the version never skips."""
    _install_graph(
        http_app,
        [AIMessage(content="Net sales grew."), AIMessage(content="Still growing.")],
    )
    tid = _new_thread(client)

    _send(client, tid, RESEARCH)
    _send(client, tid, "And the margin?")

    task_runtime = _task_runtime(client, tid)
    assert task_runtime["input_version"] == 2
    assert task_runtime["reconciliation"]["reconciled_input_version"] == 2


def test_input_history_keeps_the_original_payload_with_its_provenance(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The input history stores the ORIGINAL submission, never the goal or a
    summary derived from it (SC-1 original-input provenance)."""
    g = _install_graph(http_app, [AIMessage(content="Net sales grew.")])
    tid = _new_thread(client)
    original = "  Compare AAPL 2023 Q3 net sales  "

    _send(client, tid, original)

    history = g.task_runtime_state(tid).input_history
    assert history[-1].payload == original
    assert history[-1].provenance == "workspace_send"


def test_taskless_greeting_advances_input_but_manufactures_no_task(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A greeting is reconciled (input history advances) yet stays taskless
    with no task outcome (SC-3)."""
    _install_graph(http_app, [AIMessage(content="Hi there.")])
    tid = _new_thread(client)

    assert _send(client, tid, GREETING).status_code == 200
    task_runtime = _task_runtime(client, tid)

    assert task_runtime["input_version"] == 1
    assert task_runtime["current_task"] is None
    assert task_runtime["task_outcome"] is None
    assert task_runtime["reconciliation"]["disposition"] == "none"


# ===========================================================================
# SC-5 — thread-scoped task history survives a run-scoped reset and compaction
# ===========================================================================


def test_two_consecutive_sends_preserve_the_first_task(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The run-scoped reset on the second send does not erase the task the
    first send committed (SC-5)."""
    _install_graph(
        http_app,
        [AIMessage(content="Net sales grew."), AIMessage(content="Still growing.")],
    )
    tid = _new_thread(client)

    _send(client, tid, RESEARCH)
    first_task_id = _task_runtime(client, tid)["current_task"]["task_id"]

    _send(client, tid, "And the margin?")

    second = _task_runtime(client, tid)
    assert second["current_task"] is not None
    assert second["current_task"]["task_id"] == first_task_id


def test_reset_input_state_omits_the_thread_scoped_envelope() -> None:
    """The run-scoped reset omits ``task_runtime`` by contract — the one
    reason a reset, a resume, and a compaction preserve task history."""
    reset = reset_input_state("hello")
    assert "task_runtime" not in reset
    # Same thread-scoped omission as the durable status snapshots.
    assert "status_snapshots" not in reset


def test_compaction_preserves_the_thread_scoped_task(fresh_store) -> None:
    """A real compaction run (force, evicting) leaves the task envelope
    untouched: the gate writes only ``messages`` (SC-5)."""
    from fin.host.session.compaction import _SUMMARY_MARKER

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
        summarizer=ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")]),
    )
    tid = "compact-preserves-task"
    # A committed envelope (a real current task), not a bare acceptance.
    accepted_state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload=RESEARCH,
        provenance="workspace_send",
    )
    committed = decide(
        accepted_state,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal=RESEARCH),
    )
    g._graph.update_state(
        _cfg(tid),
        {"messages": _fat_history(), "task_runtime": committed.to_state_dict()},
        as_node="model",
    )

    with session_token_budget(1_600):
        receipt = g.compact_thread(tid)

    assert receipt["outcome"] == "compacted"
    messages = g._graph.get_state(_cfg(tid)).values["messages"]
    assert _SUMMARY_MARKER in messages[0].content
    # The task survived the compaction write-back unchanged.
    after = g.task_runtime_state(tid)
    assert after.input_version == 1
    assert after.current_task is not None
    assert after.current_task.goal == RESEARCH


# ===========================================================================
# SC-1 — the reconcile node is idempotent and never manufactures an input
# ===========================================================================


def test_reconcile_node_is_idempotent_for_an_already_reconciled_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Replaying the SAME accepted input leaves the committed envelope
    unchanged: the reconciliation marker short-circuits the node (which
    returns ``{}``) rather than re-running the transition."""
    import logging

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[AIMessage(content="Net sales grew."), AIMessage(content="Again.")]
        ),
        known_document_ids=KNOWN_IDS,
    )
    tid = "reconcile-idempotent"
    accepted = _accepted(RESEARCH, version=1)

    first = g.invoke(_door_input(RESEARCH), config=_accepted_config(tid, accepted))
    committed = TaskRuntimeState.from_state_dict(first["task_runtime"])
    assert committed.reconciliation is not None
    assert committed.reconciliation.reconciled_input_version == 1

    # The replay carries no fresh envelope: the checkpoint's committed one
    # stands, and the node must recognise the input as already decided.
    with caplog.at_level(logging.WARNING, logger="fin.host.graph"):
        second = g.invoke(
            {"messages": [HumanMessage(RESEARCH)]},
            config=_accepted_config(tid, accepted),
        )

    replayed = TaskRuntimeState.from_state_dict(second["task_runtime"])
    assert replayed.input_version == 1
    assert replayed.reconciliation is not None
    assert replayed.reconciliation.reconciled_input_version == 1
    assert replayed.current_task == committed.current_task
    # The short-circuit (not the guard fallback) handled it: the guard path
    # would have logged a refusal, leaving state unchanged but the log dirty.
    assert "task reconciliation refused" not in caplog.text


def test_reconcile_node_without_an_accepted_input_manufactures_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume/replay passes no ``accepted_task_input``: the node returns
    ``{}`` silently and no input or task is invented (the resumed-execution
    contract, T2)."""
    import logging

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="Net sales grew.")]),
        known_document_ids=KNOWN_IDS,
    )
    tid = "reconcile-resume"

    with caplog.at_level(logging.WARNING, logger="fin.host.graph"):
        out = g.invoke(reset_input_state(RESEARCH), config=_cfg(tid))

    env = TaskRuntimeState.from_state_dict(out.get("task_runtime"))
    assert env == TaskRuntimeState()
    assert env.input_version == 0
    assert env.current_task is None
    assert "task reconciliation refused" not in caplog.text


# ===========================================================================
# SC-1 / SC-4 — the retired mode picker and the windows that are still valid
# ===========================================================================


@pytest.mark.soft_track
def test_the_retired_mode_window_opens_no_picker_while_a_valid_window_resumes(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """ADR 0104: the internal retrieval-mode picker is retired; the windows
    that are still valid keep their pause/resume contract.

    Part 1 — the MODE lane: with the HITL lane ON, a grounded turn the
    routing cascade cannot assign opens NO window. The residual is the
    host's own recorded decision — ``graph`` at
    ``decision_tier="fallback"`` with confidence 0.0 and no options — and
    the run continues to its terminal frames instead of waiting for an
    operator who would have to understand internal routing.

    Part 2 — a STILL-VALID window (the Fast extraction proposal): the run
    pauses on the propose window BEFORE ``reconcile_task`` and before any
    model call; the send door has already accepted the input (SC-1), and the
    resume carries the SAME run through the reconcile topology into the
    grounded leaf.
    """
    g = _install_graph(
        http_app,
        [AIMessage(content="Net sales grew year over year.")],
        enable_choice_window=True,
    )

    # ----- part 1: the mode lane opens no picker -----
    tid = _new_thread(client)
    sent = _send(client, tid, RESEARCH)
    assert sent.status_code == 200
    names = [name for name, _ in _parse_sse_events(sent.text)]
    # No retrieval-mode picker opens for a routing residual.
    assert "choice_window" not in names
    # The run was never paused: it reached its terminal frames.
    assert "status" in names
    assert names[-1] == "citations"
    assert g.interrupt_pending(tid) is None

    audit = g._graph.get_state(_cfg(tid)).values["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["decision_tier"] == "fallback"
    assert audit["confidence"] == 0.0
    # No mode-selection options: nothing a client could paint a picker from.
    assert audit["options"] == []
    # The fallback keeps the ORIGINAL judge signals instead of inventing a
    # judge result (spec search-mode-host contract item 6).
    assert audit["signals"]["mode_jev_exit"] == "no_jev_client"

    # The send door accepted the input and the run reconciled it.
    runtime = _task_runtime(client, tid)
    assert runtime["input_version"] == 1
    assert runtime["reconciliation"]["reconciled_input_version"] == 1
    assert runtime["current_task"] is not None

    # ----- part 2: a still-valid window opens, pauses and resumes -----
    window_tid = _new_thread(client)
    paused = _send(client, window_tid, RESEARCH, propose_extract_method="fast")
    assert paused.status_code == 200
    events = _parse_sse_events(paused.text)
    names = [name for name, _ in events]
    assert names[-1] == "choice_window"
    assert events[-1][1]["kind"] == "choice_window"
    # The open window is the EXTRACT-METHOD one (standard / fast), not a
    # retrieval-mode picker: the mode is the host's own decision, never an
    # operator's pick.
    assert sorted(str(option.get("id")) for option in events[-1][1]["options"]) == [
        "fast",
        "standard",
    ]
    # The paused run emitted no terminal frame (it is not over).
    assert "status" not in names
    assert "citations" not in names
    # The window paused BEFORE any model call: the scripted model was never
    # reached.
    paused_messages = (
        g._graph.get_state(_cfg(window_tid)).values.get("messages") or []
    )
    assert not any(isinstance(message, AIMessage) for message in paused_messages)
    # The send door accepted the input even though the graph paused.
    assert _task_runtime(client, window_tid)["input_version"] == 1

    resumed = client.post(
        f"/api/threads/{window_tid}/resume", json={"pick": "standard"}
    )
    assert resumed.status_code == 200
    resumed_names = [name for name, _ in _parse_sse_events(resumed.text)]
    assert "choice_window" not in resumed_names
    assert resumed_names[-1] == "citations"

    proposal = g._graph.get_state(_cfg(window_tid)).values["extract_method_proposal"]
    assert proposal["proposed"] == "fast"
    assert proposal["picked"] == "standard"
    # The method pick never speaks for the retrieval mode: the routing
    # decision is the same host-side fallback.
    resumed_audit = g._graph.get_state(_cfg(window_tid)).values[
        "search_mode_decision"
    ]
    assert resumed_audit["search_mode"] == "graph"
    assert resumed_audit["decision_tier"] == "fallback"


# ===========================================================================
# SC-5 — the wire projection, stand-in tolerance, and task/turn independence
# ===========================================================================


def test_get_thread_task_runtime_declares_every_wire_field(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """FastAPI must not filter any declared field off the response
    (SC-5 wire projection). ``input_history`` is here because the
    decision spec requires readers to obtain bounded ORIGINAL input
    history, not only the current task."""
    _install_graph(http_app, [AIMessage(content="Net sales grew.")])
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)

    task_runtime = _task_runtime(client, tid)
    assert set(task_runtime.keys()) == TASK_RUNTIME_WIRE_FIELDS
    assert isinstance(task_runtime["archived_tasks"], list)
    assert isinstance(task_runtime["input_version"], int)
    # The accepted original is readable back, bounded, newest last.
    history = task_runtime["input_history"]
    assert [row["input_version"] for row in history] == [1]
    assert history[0]["payload"] == RESEARCH


def test_get_thread_with_a_standin_graph_without_the_seam_is_an_empty_view(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A graph stand-in without ``task_runtime_state`` serves the empty view
    (taskless) rather than 500-ing or crashing the route (SC-5)."""
    from fin.host.http import get_graph

    class _StandInNoTaskSeam:
        def is_locked(self, thread_id: str) -> bool:
            return False

        def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
            return ()

        def interrupt_pending(self, thread_id: str) -> dict[str, Any] | None:
            return None

    http_app.dependency_overrides[get_graph] = lambda: _StandInNoTaskSeam()
    tid = _new_thread(client)

    response = client.get(f"/api/threads/{tid}")
    assert response.status_code == 200
    assert response.json()["task_runtime"] == EMPTY_TASK_RUNTIME_VIEW


def test_task_runtime_is_independent_from_turn_status(
    client: TestClient, fresh_store, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """A turn that ends ``done`` does NOT imply its task is complete: the
    task stays active while the turn's row and the task envelope are read
    from separate records (T6, SC-7)."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_graph(
        http_app,
        [_read_source_call(), AIMessage(content=answer)],
        adapter=_tree_read_adapter(),
    )
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)

    detail = client.get(f"/api/threads/{tid}").json()
    assert detail["turns"][0]["status"] == "done"

    task_runtime = detail["task_runtime"]
    assert task_runtime["current_task"] is not None
    assert task_runtime["current_task"]["outcome"] == "active"
    # The turn finishing did not establish task completion. With no
    # grounding assessment the outcome boundary writes nothing onto
    # task_outcome, so the field stays unknown. The task itself remains
    # active on current_task.
    assert task_runtime["task_outcome"] is None


# ===========================================================================
# Boundary classes required by the repo test rules: empty, concurrent, legacy
# ===========================================================================


def test_empty_question_send_is_204_and_does_not_advance_input_version(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """An empty/whitespace submit keeps the existing 204 no-op semantics and
    accepts nothing — the send door's acceptance sits AFTER the guard."""
    _install_graph(http_app, [AIMessage(content="never reached")])
    tid = _new_thread(client)

    response = _send(client, tid, "   ")
    assert response.status_code == 204
    assert response.content == b""

    task_runtime = _task_runtime(client, tid)
    assert task_runtime["input_version"] == 0
    assert task_runtime["current_task"] is None


def test_an_explicit_restore_is_not_swallowed_by_the_empty_question_guard(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A restore names an archive, not message text.

    The empty-submit 204 guard used to run first and drop the request
    whole, so the operator's restore looked like it had done nothing —
    the false impression W5 forbids. A door that names SOMETHING must
    reach the task door and be answered there; only a request naming
    nothing at all is still an empty submit.
    """
    _install_graph(http_app, [AIMessage(content="restore attempted")])
    tid = _new_thread(client)

    response = _send(client, tid, "", restore_archive_id="archive-task-1-v1")

    # Not 204: the request was routed, not discarded as an empty submit.
    assert response.status_code != 204
    # The archive does not exist on this fresh thread, so the task door
    # refuses it as ``task_restore_unknown`` — a refusal the surface can
    # name, which is the whole difference from a silent 204.
    assert "task_restore_unknown" in response.text
    # Acceptance precedes semantic work (contract §Representation): the
    # restore is durably recorded as input 1 and THEN refused, so the
    # original request is recoverable even though it could not be adopted.
    assert _task_runtime(client, tid)["input_version"] == 1


def test_an_explicit_reply_is_not_swallowed_by_the_empty_question_guard(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A clarification reply names a question, not message text.

    Same exemption as the restore: an Other reply carries its text in
    ``free_text``, but a LISTED choice has none at all, so requiring
    ``question`` would make every listed-choice reply a silent 204.
    """
    _install_graph(http_app, [AIMessage(content="reply attempted")])
    tid = _new_thread(client)

    response = _send(client, tid, "", question_id="q1", choice_id="2023")

    assert response.status_code != 204


def test_concurrent_send_returns_409_and_leaves_task_state_unchanged(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A same-thread send while a run holds the per-thread lock is the
    existing 409 ``concurrent_run`` and accepts nothing; the first send's
    task state stands."""
    entered = threading.Event()
    release = threading.Event()

    from fin.host.http import get_graph
    from fin.host.scripted_model import ScriptedChatModel as SCM

    class BlockingModel(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=BlockingModel(script=[AIMessage(content="answer")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    tid = _new_thread(client)

    results: dict[str, Any] = {}

    def hit(label: str) -> None:
        results[label] = _send(client, tid, RESEARCH)

    with ThreadPoolExecutor(max_workers=2) as ex:
        first = ex.submit(hit, "first")
        assert entered.wait(timeout=5.0), "first request never entered the graph"
        second = ex.submit(hit, "second")
        time.sleep(0.2)
        release.set()
        first.result(timeout=10.0)
        second.result(timeout=10.0)

    assert sorted(r.status_code for r in results.values()) == [200, 409]
    rejected = next(r for r in results.values() if r.status_code == 409)
    assert rejected.json() == {"error": "concurrent_run"}

    task_runtime = _task_runtime(client, tid)
    assert task_runtime["input_version"] == 1
    assert task_runtime["current_task"] is not None


def test_task_runtime_state_reads_empty_on_a_legacy_checkpoint(
    fresh_store,
) -> None:
    """A checkpoint written before the envelope existed (no ``task_runtime``
    key) reads as the empty state instead of raising — no backfill."""
    from langchain_core.messages import HumanMessage as HM

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    tid = "legacy-thread"
    g._graph.update_state(
        _cfg(tid),
        {"messages": [HM("an old turn from before the envelope existed")]},
        as_node="model",
    )

    assert g.task_runtime_state(tid) == TaskRuntimeState()
    # A thread with no checkpoint at all reads the same way.
    assert g.task_runtime_state("never-seen") == TaskRuntimeState()


@pytest.mark.soft_track
def test_window_pause_then_resume_still_reconciles_the_accepted_input(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """SC-1: a choice-window pause must not let dependent work start on an
    UNRECONCILED accepted input.

    The send door accepts the input before the graph runs, then the window
    pauses the run BEFORE ``reconcile_task``. The resume carries no run
    config, so the node recovers the accepted input from the committed
    envelope instead of reaching the grounded leaf with none.

    The window is the Fast extraction proposal — one of the two windows that
    are still valid (ADR 0104 retired the internal retrieval-mode picker;
    the content clarification and the confirmed Fast proposal remain). It
    pauses on the same seam the retired mode window used: between
    ``classify`` and ``reconcile_task``.
    """
    g = _install_graph(
        http_app,
        [AIMessage(content="Net sales grew year over year.")],
        enable_choice_window=True,
    )
    tid = _new_thread(client)
    sent = _send(client, tid, RESEARCH, propose_extract_method="fast")
    assert sent.status_code == 200
    names = [name for name, _ in _parse_sse_events(sent.text)]
    assert names[-1] == "choice_window"
    # The paused run emitted no terminal frame (it is not over).
    assert "status" not in names
    assert "citations" not in names

    # The send door accepted the input and NOTHING reconciled it yet: the
    # window pauses BEFORE ``reconcile_task``, so no dependent work started
    # on an unreconciled accepted input.
    paused = g.task_runtime_state(tid)
    assert paused.input_version == 1
    assert paused.reconciliation is None, (
        "the pause let dependent work start on an unreconciled accepted input"
    )
    assert paused.current_task is None

    resumed = client.post(f"/api/threads/{tid}/resume", json={"pick": "standard"})
    assert resumed.status_code == 200

    runtime = g.task_runtime_state(tid)
    assert runtime.input_version == 1
    assert runtime.reconciliation is not None, (
        "the paused input reached dependent work with no committed decision"
    )
    assert runtime.reconciliation.reconciled_input_version == 1
    assert runtime.current_task is not None
    # The reconciled input is the ORIGINAL one the door accepted — the node
    # recovered it from the committed envelope, it did not invent one.
    assert runtime.input_history[-1].payload == RESEARCH
    assert runtime.input_history[-1].provenance == "workspace_send"


# ===========================================================================
# Repair evidence for the code-review High findings
# ===========================================================================


def test_reply_door_clears_the_pending_question(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """T3/SC-4: a valid reply resolves the question it names and advances
    input history exactly once.

    Before the repair the reply door did not exist, so a committed
    question was a permanent latch — every answer went through the
    message branch, which deliberately preserves it.
    """
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)

    # The scripted lane does not open a clarification, so the committed
    # question is seeded through the same decision the graph commits. The
    # reply that follows is a real HTTP send through the real door.
    seeded = _seed_pending_question(g, tid)
    assert seeded.pending_question is not None
    version_before = seeded.input_version

    replied = _send(
        client,
        tid,
        RESEARCH,
        question_id=seeded.pending_question.question_id,
        choice_id=seeded.pending_question.choices[0],
    )
    assert replied.status_code == 200

    after = g.task_runtime_state(tid)
    assert after.pending_question is None, "a valid reply left the question pending"
    assert after.input_version == version_before + 1
    assert after.input_history[-1].kind == "reply"
    assert after.input_history[-1].choice_id == seeded.pending_question.choices[0]


def _seed_pending_question(
    g: Any, tid: str, *, question_id: str = "q-seeded"
) -> TaskRuntimeState:
    """Commit a clarification the way the reconcile node does.

    Written back through the checkpointer, because the send door reads the
    COMMITTED envelope — a question built only in memory would not be
    there to answer.
    """
    state = g.task_runtime_state(tid)
    state, accepted = accept_input(
        state, kind=INPUT_MESSAGE, payload=RESEARCH, provenance="workspace_send"
    )
    committed = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_MESSAGE,
            goal=RESEARCH,
            question=PendingQuestion(
                question_id=question_id,
                task_id="",
                task_version=1,
                prompt="请补充：公司",
                choices=("公司", "期间"),
                input_version=0,
            ),
        ),
    )
    g._graph.update_state(
        _cfg(tid), {"task_runtime": committed.to_state_dict()}
    )
    return g.task_runtime_state(tid)


def test_reply_with_no_pending_question_is_refused(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """SC-4/SC-8: a reply naming no pending question is refused at the
    door, and writes nothing at all.

    Admission validates the reply BEFORE accepting it, so there is no
    committed input for the reconciliation boundary to reject later: an
    invalid reply makes no input or scope write. Accepting first and
    refusing mid-run was the older shape, and it cost a durable
    ``input_version`` advance plus a turn row for a draft to a question
    that never existed — a new input manufactured by a stale delivery.

    The refusal is ``task_reply_unknown`` — nothing is pending at all —
    rather than ``task_reply_expired``, which is reserved for a reply to
    a SUPERSEDED question. A surface branches on the token to decide
    whether to re-read the question list or to tell the operator the
    question it asked has been replaced.
    """
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    before = g.task_runtime_state(tid)

    refused = _send(
        client,
        tid,
        RESEARCH,
        question_id="no-such-question",
        choice_id="a",
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "task_reply_unknown"
    after = g.task_runtime_state(tid)
    # Nothing the reply could have changed moved, and no accepted input
    # was written either: SC-4's counter advances once per accepted
    # input, and this delivery never became one.
    assert after.input_version == before.input_version
    assert after.input_history == before.input_history
    assert after.current_task == before.current_task
    assert after.pending_question == before.pending_question
    assert after.archived_tasks == before.archived_tasks
    assert after.task_outcome == before.task_outcome


def test_reply_to_a_superseded_question_is_refused_as_expired(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A reply naming a question the thread HAS had, but no longer has
    pending, is ``task_reply_expired`` — and is refused before it writes.

    The question was real and the operator answered it late, which is the
    case ``task_reply_unknown`` cannot describe: the distinction is what
    lets a surface say "that question was replaced" instead of "this
    thread asked you nothing".
    """
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    _seed_pending_question(g, tid, question_id="q-first")
    pending = _seed_pending_question(g, tid, question_id="q-second")
    assert pending.pending_question is not None
    assert pending.pending_question.question_id == "q-second"
    before = g.task_runtime_state(tid)

    refused = _send(client, tid, RESEARCH, question_id="q-first", choice_id="公司")

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "task_reply_expired"
    # The stale reply resolves nothing: the LIVE question is still the
    # one this thread is waiting on, and no input was accepted.
    after = g.task_runtime_state(tid)
    assert after == before
    assert after.pending_question is not None
    assert after.pending_question.question_id == "q-second"


def test_the_two_reply_refusal_tokens_stay_distinct(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The same delivery is UNKNOWN or EXPIRED purely on what the thread
    holds — the token reports the state, not the delivery.

    Collapsing them would make one of the two answers unreachable from
    the wire, and a surface would have to guess which clarification the
    operator meant to answer.
    """
    g = _install_graph(http_app, [AIMessage(content="ok"), AIMessage(content="ok")])
    never_asked = _new_thread(client)
    _send(client, never_asked, RESEARCH)

    superseded = _new_thread(client)
    _send(client, superseded, RESEARCH)
    _seed_pending_question(g, superseded, question_id="q-first")
    _seed_pending_question(g, superseded, question_id="q-second")

    unknown = _send(
        client, never_asked, RESEARCH, question_id="q-first", choice_id="公司"
    )
    expired = _send(client, superseded, RESEARCH, question_id="q-first", choice_id="公司")

    assert (unknown.status_code, expired.status_code) == (409, 409)
    assert unknown.json()["detail"]["error"] == "task_reply_unknown"
    assert expired.json()["detail"]["error"] == "task_reply_expired"


def test_restore_without_a_recheck_is_refused_not_adopted(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """T4/SC-5: a restore is explicit and must not adopt archived results
    until the current rechecks pass. With no source-authority seam wired,
    the refusal is the truthful answer and the current task stands."""
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    before = g.task_runtime_state(tid)

    refused = _send(client, tid, RESEARCH, restore_archive_id="archive-does-not-exist")
    assert refused.status_code == 200
    after = g.task_runtime_state(tid)
    assert after.current_task == before.current_task
    assert after.archived_tasks == before.archived_tasks


def test_refused_reconciliation_never_reaches_dependent_work(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """SC-1/SC-8: a reply naming no pending question is refused BEFORE any
    run, so no dependent work is ever reachable from it.

    Before the repair the node swallowed the refusal and returned an empty
    update, so the lane continued into the model with no committed
    reconciliation at all. Admission now decides first: the refusal is a
    4xx with no stream, no turn row and no question put to the user.
    """
    g = _install_graph(
        http_app,
        [_read_source_call(), AIMessage(content="cited")],
        adapter=_tree_read_adapter(),
    )
    tid = _new_thread(client)
    before = g.task_runtime_state(tid)

    refused = _send(
        client,
        tid,
        RESEARCH,
        question_id="never-opened",
        choice_id="a",
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "task_reply_unknown"
    # The refusal is a system outcome: no question is put to the user,
    # and the model is never reached to ask one.
    assert "请补充" not in refused.text
    assert g.task_runtime_state(tid).task_outcome != "complete"
    assert g.task_runtime_state(tid) == before
    assert client.get(f"/api/threads/{tid}").json()["turns"] == []


# ===========================================================================
# T3/SC-4: the company-change signal archives the outgoing task.
# ===========================================================================


def test_company_change_archives_the_outgoing_task(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A message naming a different company is a SWITCH, not progress.

    Regression: ``_is_task_switch`` read ``state["current_task"]``, a key
    that does not exist on ``AgentState`` — the task lives INSIDE the
    ``task_runtime`` envelope. The lookup was therefore always ``None``
    and the archive branch was unreachable through the real seam, while
    the pure-logic tests still passed because they pass ``switch=True``
    in directly.
    """
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)

    first = _send(client, tid, "What was MSFT net sales in 2023 Q1?")
    assert first.status_code == 200
    started = g.task_runtime_state(tid)
    assert started.current_task is not None, "the first message opened no task"

    _send(client, tid, "What was AAPL net sales in 2022 Q3?")

    switched = g.task_runtime_state(tid)
    assert switched.reconciliation.disposition == DISPOSITION_SWITCH, (
        "a company change did not reconcile as a switch; "
        f"got {switched.reconciliation.disposition!r}"
    )
    assert len(switched.archived_tasks) == 1, "the outgoing task was not archived"
    assert switched.archived_tasks[0].task.task_id == started.current_task.task_id
    assert switched.current_task is not None
    assert switched.current_task.task_id != started.current_task.task_id

def test_a_peer_verdict_survives_the_checkpoint_and_does_not_outlive_its_run(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """``grounding_assessment`` is a real channel, cleared every run.

    Regression: the key was read by the outcome boundary but never
    declared on ``AgentState``, so the checkpointer dropped every peer
    verdict and the grounded-answer module could never be heard at all.

    Completion below is asserted through the node that reads the
    channel, so it can only pass if the key survived the schema.
    """
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)
    _send(client, tid, RESEARCH)
    assert g.task_runtime_state(tid).current_task is not None

    verdict = {
        "supported": True,
        "material_gaps": [],
        "turn_outcome": "answered",
        "source_scope_version": 1,
    }
    g._graph.update_state(_cfg(tid), {"grounding_assessment": verdict})

    stored = g._graph.get_state(_cfg(tid)).values.get("grounding_assessment")
    assert stored == verdict, (
        "the peer verdict was dropped by the checkpoint; the "
        f"grounded-answer seam is unreachable. stored={stored!r}"
    )

    # Run-scoped: a fresh send clears it, so a verdict recorded for one
    # question can never complete the next task.
    reset = reset_input_state(RESEARCH)
    assert reset["grounding_assessment"] == {}
    assert "task_runtime" not in reset, "thread-scoped history must survive a reset"

    _send(client, tid, "And what about the prior quarter?")
    assert g._graph.get_state(_cfg(tid)).values.get("grounding_assessment") == {}


def test_a_pinned_search_mode_still_detects_the_switch(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A search-mode tier pin must not hide a company change.

    Regression: the switch test was inferred from the last snapshot row,
    but a grounded turn with a pinned tier appends an extra snapshot
    patch row that copies the company forward. "Drop the last row" then
    left this turn's OWN row in the baseline, so the utterance was
    compared against itself and the switch was missed — the AAPL request
    was reconciled under the stale MSFT task.
    """
    g = _install_graph(http_app, [AIMessage(content="ok")])
    tid = _new_thread(client)

    assert _send(client, tid, "What was MSFT net sales in 2023 Q1?").status_code == 200
    started = g.task_runtime_state(tid)
    assert started.current_task is not None

    pinned = _send(
        client, tid, "What was AAPL net sales in 2022 Q3?", tiers=["lexical"]
    )
    assert pinned.status_code == 200

    switched = g.task_runtime_state(tid)
    assert switched.reconciliation.disposition == DISPOSITION_SWITCH, (
        "the pinned tier hid the company change; "
        f"got {switched.reconciliation.disposition!r}"
    )
    assert len(switched.archived_tasks) == 1
    assert switched.archived_tasks[0].task.task_id == started.current_task.task_id


def test_repeating_the_same_request_is_progress_not_a_switch(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """Asking the same thing twice keeps the task and its work.

    Regression: the same baseline inference produced the opposite error
    too. A repeated utterance appends no snapshot row (the delta
    dedupes), so the row "dropped" was a PRIOR turn's, and the repeat
    reconciled as a switch — archiving the task the user was still
    working on.
    """
    g = _install_graph(http_app, [AIMessage(content="ok"), AIMessage(content="ok")])
    tid = _new_thread(client)

    _send(client, tid, "What was MSFT net sales in 2023 Q1?")
    started = g.task_runtime_state(tid)
    assert started.current_task is not None

    _send(client, tid, "What was AAPL net sales in 2022 Q3?")
    _send(client, tid, "What was AAPL net sales in 2022 Q3?")

    after = g.task_runtime_state(tid)
    assert after.reconciliation.disposition != DISPOSITION_SWITCH, (
        "a repeated request archived the task the user was still working on"
    )
    assert after.current_task is not None
    assert after.current_task.goal == "What was AAPL net sales in 2022 Q3?"
