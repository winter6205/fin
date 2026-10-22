"""Document-reply admission at the send door.

The shared contract puts validation of a document reply BEFORE the reply
is accepted: "Admission validates the current question/task conditions,
originating thread/run/interrupt, candidate membership and current source
identity/version/permission before accepting a document reply", and "an
invalid/foreign/expired reply makes no input or scope write".

The graph's ``document_choice_interrupt_node`` already runs that
validate-then-reopen loop, and it stays: it is the backstop and the reopen
path. What it cannot be is an EARLY rejection — a send door that accepts
first and refuses at reconciliation has already advanced
``input_version``, and that accepted input is exactly the write the
contract forbids.

These tests pin the send-door pre-check:

* a reply that cannot possibly be valid is refused BEFORE
  :func:`accept_input`, leaving ``input_version`` and the whole envelope
  byte-identical;
* a legitimate duplicate pick is still recognized in ACCEPTED HISTORY, so
  a second click returns the FIRST outcome instead of advancing again;
* the pre-check consults the graph's own primitives rather than growing a
  second copy of the rules;
* the controls that are NOT user input — a bodyless crash resume, a
  display reload, an ordinary message, a search-mode-only control — stay
  outside it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.grounded.grounded_decision import CANDIDATE_TERMINATOR
from fin.host.named_failures import TASK_INPUT_INVALID, TASK_REPLY_EXPIRED
from fin.host.task.task_runtime_types import INPUT_MESSAGE, INPUT_REPLY
from fin.host.workspace_http import SendMessageRequest, _accept_at_send_door
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort

GROUNDED = "What was AAPL net sales in 2023 Q3?"
DOC_A = "AAPL-2023Q3"
DOC_B = "AAPL-2022Q3"
KNOWN_IDS = {DOC_A, DOC_B, "MSFT-2023Q1"}

DOC_A_TEXT = (
    "Section 1. Apple reported net sales of 89,493 million dollars for the "
    "fiscal 2023 fourth quarter."
)
DOC_B_TEXT = (
    "Section 1. Apple reported net sales of 82,959 million dollars for the "
    "fiscal 2022 fourth quarter."
)


def _chunk(document_id: str, text: str, unit: int) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(document_id=document_id, version="v" * 8, unit=unit),
        score=1.0,
    )


def _choice_output(*document_ids: str) -> str:
    """One model decision output that explicitly asks for a choice."""
    import json

    payload = {"candidates": [{"document_id": doc} for doc in document_ids]}
    return f"这个问题有两种理解。{CANDIDATE_TERMINATOR}{json.dumps(payload)}"


def _compile(answer: str) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": GROUNDED},
                            "id": "c-retrieve-admission",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content=answer),
            ]
        ),
        adapter=FakeRetrievePort(
            hits_by_query={
                GROUNDED: [
                    _chunk(DOC_A, DOC_A_TEXT, 1),
                    _chunk(DOC_B, DOC_B_TEXT, 1),
                ]
            },
            known_document_ids=set(KNOWN_IDS),
        ),
        known_document_ids=set(KNOWN_IDS),
    )


def _open_choice(thread_id: str) -> tuple[Any, str, int]:
    """Run a thread to its paused document choice.

    Returns the graph, the first candidate's id, and the ``input_version``
    the pause left behind — the value every refusal below must not move.
    """
    from fin.host.state import reset_input_state
    from fin.host.task.task_runtime import accept_input
    from fin.host.task.task_runtime_types import TaskRuntimeState

    g = _compile(_choice_output(DOC_A, DOC_B))
    envelope, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload=GROUNDED,
        provenance="workspace_send",
        run_id=f"run-{thread_id}",
    )
    g.invoke(
        {
            **reset_input_state(GROUNDED),
            "task_runtime": envelope.to_state_dict(),
        },
        config={
            "configurable": {
                "thread_id": thread_id,
                "run_id": f"run-{thread_id}",
                "accepted_task_input": accepted.to_state_dict(),
            }
        },
    )
    runtime = g.task_runtime_state(thread_id)
    pick = runtime.pending_question.choice_details[0].choice_id
    return g, pick, runtime.input_version


def _door(
    g: Any,
    thread_id: str,
    body: SendMessageRequest,
    *,
    run_id: str,
    kind: str = INPUT_REPLY,
) -> Any:
    """Call the admission door exactly as ``send_message`` does."""
    from fin.host.workspace_http import _accepted_task_runtime_state

    return _accept_at_send_door(
        _accepted_task_runtime_state(g, thread_id),
        body,
        kind=kind,
        run_id=run_id,
        graph=g,
        thread_id=thread_id,
    )


# ----- the refusal: no input, no scope write -----


def test_an_invalid_document_reply_is_refused_before_any_input_is_written() -> None:
    """A pick naming no candidate never reaches ``accept_input``.

    This is the whole point of the pre-check: the send door refuses, so
    ``input_version`` is untouched, the envelope is byte-identical, and
    the graph is never invoked — there is nothing for a scope write to
    attach to.
    """
    thread_id = "admission-invalid"
    g, _picked, before_version = _open_choice(thread_id)
    before = g.task_runtime_state(thread_id)
    before_dict = before.to_state_dict()

    with pytest.raises(HTTPException) as caught:
        _door(
            g,
            thread_id,
            SendMessageRequest(
                question="", question_id=before.pending_question.question_id,
                choice_id="not-a-candidate",
            ),
            run_id=f"run-{thread_id}",
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {"error": TASK_INPUT_INVALID}
    after = g.task_runtime_state(thread_id)
    assert after.input_version == before_version
    assert after.to_state_dict() == before_dict


def test_a_document_reply_naming_a_superseded_question_is_refused() -> None:
    """The question id is the first ownership check.

    A reply that names a question this thread no longer has open cannot
    answer whatever happens to be pending now.
    """
    thread_id = "admission-superseded"
    g, picked, before_version = _open_choice(thread_id)
    before = g.task_runtime_state(thread_id)

    with pytest.raises(HTTPException) as caught:
        _door(
            g,
            thread_id,
            SendMessageRequest(question="", question_id="doc-choice-other", choice_id=picked),
            run_id=f"run-{thread_id}",
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"error": TASK_REPLY_EXPIRED}
    after = g.task_runtime_state(thread_id)
    assert after.input_version == before_version
    assert after.to_state_dict() == before.to_state_dict()


def test_a_document_reply_from_a_foreign_run_is_refused() -> None:
    """``origin_run_id`` binds the question to the run that paused.

    The door reads the run id off the CHECKPOINT (the resume route reuses
    it too); a checkpoint whose run id is not the question's origin cannot
    produce a valid reply.
    """

    class _ForeignRunGraph:
        """A stand-in that reports a different canonical run id."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def checkpoint_run_id(self, thread_id: str) -> str | None:
            return "some-other-run"

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    thread_id = "admission-foreign"
    g, picked, before_version = _open_choice(thread_id)
    before = g.task_runtime_state(thread_id)

    with pytest.raises(HTTPException) as caught:
        _door(
            _ForeignRunGraph(g),
            thread_id,
            SendMessageRequest(
                question="",
                question_id=before.pending_question.question_id,
                choice_id=picked,
            ),
            run_id=f"run-{thread_id}",
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {"error": TASK_INPUT_INVALID}
    after = g.task_runtime_state(thread_id)
    assert after.input_version == before_version
    assert after.to_state_dict() == before.to_state_dict()


def test_a_graph_without_the_checkpoint_run_id_seam_refuses_nothing() -> None:
    """No seam means no run identity to judge — the door stays out of it.

    A stand-in graph (a test double, a partially wired host) exposes no
    ``checkpoint_run_id``. Refusing there would 400 every legitimate reply
    on an unwired graph, which is the opposite of what this pre-check is
    for; the graph-side loop remains the backstop.
    """

    class _NoSeamGraph:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            if name == "checkpoint_run_id":
                raise AttributeError(name)
            return getattr(self._inner, name)

    thread_id = "admission-noseam"
    g, picked, before_version = _open_choice(thread_id)
    before = g.task_runtime_state(thread_id)

    _next_state, accepted = _door(
        _NoSeamGraph(g),
        thread_id,
        SendMessageRequest(
            question="",
            question_id=before.pending_question.question_id,
            choice_id=picked,
        ),
        run_id=f"run-{thread_id}",
    )

    assert accepted.input_version == before_version + 1
    assert accepted.kind == INPUT_REPLY


# ----- idempotency survives the pre-check -----


def test_a_duplicate_pick_returns_the_first_outcome_without_advancing() -> None:
    """A double click is recognized in accepted history at the DOOR.

    The pick the graph already accepted is still in ``input_history`` with
    its composite key, so the door hands back that FIRST accepted record.
    ``input_version`` therefore cannot move a second time — and because
    the returned record names the already-reconciled version, the runtime
    node no-ops on it instead of reconciling the same reply again.
    """
    thread_id = "admission-duplicate"
    g, picked, _before_version = _open_choice(thread_id)
    before = g.task_runtime_state(thread_id)
    question_id = before.pending_question.question_id

    _state, accepted = _door(
        g,
        thread_id,
        SendMessageRequest(question="", question_id=question_id, choice_id=picked),
        run_id=f"run-{thread_id}",
    )

    _state_again, repeat = _door(
        g,
        thread_id,
        SendMessageRequest(question="", question_id=question_id, choice_id=picked),
        run_id=f"run-{thread_id}",
    )

    assert repeat.input_version == accepted.input_version
    assert repeat.choice_id == accepted.choice_id
    assert g.task_runtime_state(thread_id).input_version == before.input_version


# ----- the pass-through and the non-inputs -----


def test_a_valid_reply_passes_the_door_unchanged() -> None:
    """The door stays the door: a valid reply advances exactly one input
    and carries the question's own choice id verbatim."""
    thread_id = "admission-valid"
    g, picked, before_version = _open_choice(thread_id)
    before = g.task_runtime_state(thread_id)

    next_state, accepted = _door(
        g,
        thread_id,
        SendMessageRequest(
            question="",
            question_id=before.pending_question.question_id,
            choice_id=picked,
        ),
        run_id=f"run-{thread_id}",
    )

    assert accepted.input_version == before_version + 1
    assert accepted.kind == INPUT_REPLY
    assert accepted.choice_id == picked
    assert next_state.input_version == before_version + 1
    assert len(next_state.input_history) == len(before.input_history) + 1


def test_an_ordinary_message_never_enters_the_precheck() -> None:
    """A plain send is one accepted input and nothing else.

    The pre-check is scoped to replies that NAME a question, so the
    composer turn that opens the very next question is untouched.
    """
    thread_id = "admission-message"
    g, _picked, before_version = _open_choice(thread_id)

    _state, accepted = _door(
        g,
        thread_id,
        SendMessageRequest(question="那利润呢？"),
        kind=INPUT_MESSAGE,
        run_id=f"run-{thread_id}",
    )

    assert accepted.input_version == before_version + 1
    assert accepted.kind == INPUT_MESSAGE
    assert accepted.question_id is None


# ----- over the wire -----


@pytest.fixture
def http_app() -> Any:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: Any) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(http_app)


def _install_graph(http_app: Any, answer: str) -> Any:
    from fin.host.http import get_graph

    g = _compile(answer)
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    import json

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
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def test_a_duplicate_reply_posted_to_the_wire_advances_nothing(
    client: Any, http_app: Any
) -> None:
    """The idempotency rule holds on the real send door — and launches NO work.

    The pick is accepted once through the resume path (which writes the
    composite key the graph uses), and a second click on the SAME reply is
    recognized: the thread's ``input_version`` and its accepted source
    scope are exactly what the first pick left behind.

    ``specs/question-task-runtime-contract.md:92`` is explicit that an
    already-reconciled identical retry must "return the existing outcome
    WITHOUT LAUNCHING WORK". So the duplicate must also leave the TURN
    TABLE exactly as the first pick left it: a door that re-ran the graph
    on the duplicate would append a second logical answer turn for one
    user action, which is the defect this pins.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = client.post("/api/threads", json={"first_message": "研究"}).json()["id"]
    assert (
        client.post(
            f"/api/threads/{thread_id}/messages", json={"question": GROUNDED}
        ).status_code
        == 200
    )

    detail = client.get(f"/api/threads/{thread_id}").json()
    pending = detail["task_runtime"]["pending_question"]
    pick = pending["choice_details"][0]["choice_id"]
    question_id = pending["question_id"]
    assert pending["purpose"] == "document_choice"

    resumed = client.post(f"/api/threads/{thread_id}/resume", json={"pick": pick})
    assert resumed.status_code == 200
    settled = client.get(f"/api/threads/{thread_id}").json()
    after_first = settled["task_runtime"]["input_version"]
    scope_first = settled["source_scope"]
    turns_first = settled["turns"]
    assert settled["task_runtime"]["pending_question"] is None
    assert scope_first is not None
    history_first = settled["task_runtime"]["input_history"]

    repeat = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "", "question_id": question_id, "choice_id": pick},
    )
    assert repeat.status_code == 200
    repeat_events = dict(_parse_sse_events(repeat.text))
    names = list(repeat_events)

    final = client.get(f"/api/threads/{thread_id}").json()
    assert final["task_runtime"]["input_version"] == after_first
    assert final["source_scope"] == scope_first
    assert final["task_runtime"]["input_history"] == history_first
    # Contract line 92: an already-reconciled retry "return[s] the existing
    # outcome without launching work". A fresh graph run on the duplicate
    # is exactly the launched work, and it shows up as an appended turn.
    assert final["turns"] == turns_first
    # The stream publishes the STORED outcome: same run id, same answer
    # text, and only the terminal frame set (no live tool / thinking
    # frames, which a fresh run would have streamed again).
    assert names == ["status", "answer", "citations"]
    settled_answer = turns_first[-1]["answer"]
    assert repeat_events["status"]["run_id"] == turns_first[-1]["run_id"]
    assert repeat_events["answer"]["text"] == settled_answer
    # The stream re-pauses on nothing: no second ``document_choice`` frame,
    # so the question stays answered rather than re-opened by the retry.
    assert "document_choice" not in names
    from fin.host.http import get_graph

    assert http_app.dependency_overrides[get_graph]() is g


def test_the_send_route_refuses_a_pick_naming_no_candidate(
    client: Any, http_app: Any
) -> None:
    """The pre-check sits on the ROUTE, not only on the helper.

    A 400 with no SSE body, no turn and no graph invoke — the paused
    question, its ``input_version`` and its (absent) source scope are
    exactly what they were, and a valid pick still resolves against the
    same question afterwards.
    """
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = client.post("/api/threads", json={"first_message": "研究"}).json()["id"]
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})

    paused = client.get(f"/api/threads/{thread_id}").json()
    pending = paused["task_runtime"]["pending_question"]
    question_id = pending["question_id"]

    refused = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "", "question_id": question_id, "choice_id": "not-a-candidate"},
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == {"error": TASK_INPUT_INVALID}

    unchanged = client.get(f"/api/threads/{thread_id}").json()
    assert unchanged["task_runtime"]["input_version"] == paused["task_runtime"]["input_version"]
    assert unchanged["source_scope"] is None
    assert unchanged["task_runtime"]["pending_question"] == pending

    settled = client.post(
        f"/api/threads/{thread_id}/resume",
        json={"pick": pending["choice_details"][0]["choice_id"]},
    )
    assert settled.status_code == 200
    assert client.get(f"/api/threads/{thread_id}").json()["source_scope"] is not None


def test_a_bodyless_crash_resume_and_a_reload_create_no_user_input(
    client: Any, http_app: Any
) -> None:
    """Transport controls stay transport.

    A bodyless resume redraws the paused question and a detail reload
    re-reads the projection; neither is a user input, so neither moves
    ``input_version`` and neither is refused by the pre-check.
    """
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = client.post("/api/threads", json={"first_message": "研究"}).json()["id"]
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})

    paused = client.get(f"/api/threads/{thread_id}").json()
    paused_version = paused["task_runtime"]["input_version"]
    assert paused["task_runtime"]["pending_question"] is not None

    redrawn = client.post(f"/api/threads/{thread_id}/resume")
    assert redrawn.status_code == 200
    assert client.get(f"/api/threads/{thread_id}").json()["task_runtime"][
        "input_version"
    ] == paused_version
