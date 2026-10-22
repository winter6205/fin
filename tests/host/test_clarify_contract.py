"""Contract tests for the terminal clarification and its reply chain.

The corrected contract has five observable halves:

1. A pending ``task_clarification`` is bound to the REAL current task,
   offers real candidate options plus exactly one trailing ``other``,
   carries no ``choice_details``, and never exposes the internal slot
   names (``company`` / ``period``) as options. With no real candidate
   at all the question is free-text only: ``choices == ["other"]``.
2. A company/period gap is RETRIEVAL FIRST: the host searches the
   corpus before asking. One scope answers directly and states that
   scope; only multiple candidates that change the answer clarify.
3. The pending question is committed to thread state and survives
   ``GET /api/threads/{id}``.
4. A reply naming the pending question consumes it, opens a NEW turn
   in the SAME thread, and answers with the original question as
   context. A missing/unknown ``question_id`` is an ordinary follow-up,
   never a silent drop. A free-text answer is ``choice_id="other"``
   plus non-blank ``free_text``.
5. The document-choice interrupt/``resume`` path is unchanged.

Hermetic: ScriptedChatModel + FakeRetrievePort + InMemorySaver + a
tmp_path catalog. No live model, network, or memory backend.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.corpus.catalog import record_filings
from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.question.document_choice_admission import (
    document_choice_resume_value,
    resolved_document_choice_pick,
)
from fin.host.task.task_runtime import open_pending_question, pending_question_payload
from fin.host.task.task_runtime_types import (
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    PURPOSE_TASK_CLARIFICATION,
    ChoiceDetail,
    ChoiceSourceRef,
    TaskRuntimeState,
    offer_document_choice,
)
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from jev_fake import FakeJevChoice

OVERVIEW = "有哪些资料"
GAP_FOLLOW_UP = "年假几天？"
FILING_GAP_FOLLOW_UP = "revenue?"
FREE_TEXT = "只看 ZEUS 2024 年"

ZEUS_DOC = "zeus-handbook"
ZEUS_POLICY_DOC = "zeus-policy-qa"
ACME_DOC = "acme-handbook"
ZEUS_ROW: dict[str, Any] = {
    "document_id": ZEUS_DOC,
    "title": "ZEUS handbook",
    "doc_type": "narrative",
    "company": "ZEUS",
    "period": "2024",
    "version": "v1",
    "page_count": 3,
}
ZEUS_POLICY_ROW: dict[str, Any] = {
    "document_id": ZEUS_POLICY_DOC,
    "title": "ZEUS policy Q&A",
    "doc_type": "narrative",
    "company": "ZEUS",
    "period": "2024",
    "version": "v1",
    "page_count": 2,
}
ACME_ROW: dict[str, Any] = {
    "document_id": ACME_DOC,
    "title": "ACME handbook",
    "doc_type": "narrative",
    "company": "ACME",
    "period": "2024",
    "version": "v1",
    "page_count": 4,
}
# A long-enough source span that the grounded citation gate can admit the
# answer, so "answered directly" is observable rather than masked by a
# citation repair refusal.
ZEUS_ANSWER = "ZEUS 员工手册规定：员工每年享有 15 天年假，未使用的年假最多可结转五天。"
ZEUS_POLICY_ANSWER = (
    "ZEUS 政策问答同样写明：员工每年享有 15 天年假，未使用的年假最多可结转五天。"
)
ACME_ANSWER = "ACME 员工手册规定：员工每年享有 10 天年假，未使用的年假最多可结转三天。"

INTERNAL_SLOT_NAMES = frozenset({"company", "period"})
# Real candidate identifiers the catalog and the retrieval hits carry. A
# clarification option must be traceable to one of these; an option the
# host invented is not an option.
REAL_CANDIDATE_MARKERS = frozenset(
    {"ZEUS", "ACME", "2024", ZEUS_DOC, ZEUS_POLICY_DOC, ACME_DOC}
)


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    """Fresh FastAPI app per test so dependency overrides do not leak."""
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


class _RecordingPort(FakeRetrievePort):
    """Return the scripted hits for EVERY query and record the calls.

    The contract says the host RETRIEVES across the corpus before it
    asks. The exact query text is an implementation choice, so the port
    must not depend on it; the recorded call count is what proves the
    retrieval happened first.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        known_document_ids: set[str],
    ) -> None:
        super().__init__(known_document_ids=known_document_ids)
        self._scripted_chunks = list(chunks)

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return {"chunks": list(self._scripted_chunks)}


class _RecordingScriptedModel(ScriptedChatModel):
    """Scripted model that keeps every prompt it saw.

    ``_generate`` is the one seam both ``invoke`` and the scripted
    ``stream`` path pass through, so recording there catches a node that
    calls either surface.
    """

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.seen.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


def _chunk(document_id: str, text: str) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(document_id=document_id, version="v1", unit=1),
        score=1.0,
    )


def _grounded_script(answer: str) -> list[AIMessage]:
    """One retrieve hop plus the grounded answer quoting the hit."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "年假"},
                    "id": "c-scope-retrieve",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _install_graph(
    http_app: FastAPI,
    tmp_path: Any,
    *,
    rows: list[dict[str, Any]],
    chunks: list[Chunk],
    jev_results: list[tuple[str, float]],
    script: list[AIMessage],
) -> tuple[Any, _RecordingPort, _RecordingScriptedModel]:
    """Bind one hermetic graph to the app, with the catalog and hits."""
    from fin.host.http import get_graph

    catalog_path = tmp_path / "catalog.json"
    record_filings(rows, catalog_path=catalog_path)
    known_ids = {str(row["document_id"]) for row in rows}
    port = _RecordingPort(chunks, known_document_ids=known_ids)
    model = _RecordingScriptedModel(script=script)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        catalog_path=catalog_path,
        known_document_ids=known_ids,
        jev_client=FakeJevChoice(jev_results),
    )
    http_app.dependency_overrides[get_graph] = lambda: graph
    return graph, port, model


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "研究"}).json()["id"]


def _send(client: TestClient, thread_id: str, question: str, **door: Any) -> Any:
    return client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": question, **door},
    )


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                events.append((current["event"], json.loads(current["data"])))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:") :].strip()
    if "event" in current and "data" in current:
        events.append((current["event"], json.loads(current["data"])))
    return events


def _status(body: str) -> dict[str, Any]:
    for name, data in _parse_sse_events(body):
        if name == "status":
            return data
    raise AssertionError(f"no status frame in: {body[:400]}")


def _answers(body: str) -> list[str]:
    return [
        str(data.get("text") or "")
        for name, data in _parse_sse_events(body)
        if name == "answer"
    ]


def _open_thread(client: TestClient) -> str:
    tid = _new_thread(client)
    opened = _send(client, tid, OVERVIEW)
    assert opened.status_code == 200, opened.text
    return tid


def _open_clarification(
    client: TestClient,
    graph: Any,
    thread_id: str,
    *,
    question: str = GAP_FOLLOW_UP,
) -> dict[str, Any]:
    """Drive one multi-candidate gap follow-up and return the wire question."""
    sent = _send(client, thread_id, question)
    assert sent.status_code == 200, sent.text
    runtime = graph.task_runtime_state(thread_id)
    assert runtime.pending_question is not None, (
        "a multi-candidate gap follow-up did not open a clarification; "
        f"turns={client.get(f'/api/threads/{thread_id}').json()['turns']!r}"
    )
    wire = client.get(f"/api/threads/{thread_id}").json()
    payload = wire["task_runtime"]["pending_question"]
    assert payload is not None
    return payload


def _assert_no_internal_slot_names(text: str) -> None:
    folded = text.casefold()
    for slot in INTERNAL_SLOT_NAMES:
        assert slot not in folded, (
            f"internal slot name {slot!r} leaked into user-facing text: {text!r}"
        )


# ---------------------------------------------------------------------------
# 2. Retrieval first: one scope answers, multiple candidate scopes clarify
# ---------------------------------------------------------------------------


def test_single_scope_follow_up_retrieves_and_answers_without_clarifying(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """Only ZEUS can answer, so the host answers and states that scope.

    Asking a company/period question would make the operator resolve an
    ambiguity the corpus does not have.
    """
    graph, port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW],
        chunks=[_chunk(ZEUS_DOC, ZEUS_ANSWER)],
        jev_results=[("grounded", 0.99), ("clarify", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _open_thread(client)
    calls_before = len(port.calls)

    sent = _send(client, tid, GAP_FOLLOW_UP)

    assert sent.status_code == 200, sent.text
    assert len(port.calls) > calls_before, (
        "the gap follow-up never reached the retrieve port; it clarified "
        "before searching the corpus"
    )
    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is None, (
        "a single answerable scope still produced a clarification: "
        f"{runtime.pending_question!r}"
    )
    status = _status(sent.text)
    answer = status.get("assistant_message") or "".join(_answers(sent.text))
    assert answer.strip(), f"the single-scope follow-up produced no answer: {sent.text[:400]}"
    assert "ZEUS" in answer, (
        f"the answer never states the scope it used: {answer!r}"
    )
    assert client.get(f"/api/threads/{tid}").json()["task_runtime"][
        "pending_question"
    ] is None


def test_multiple_chunks_from_one_document_still_answer_directly(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """One candidate DOCUMENT is one scope, however many hits it has.

    Candidates are catalog documents, so several chunks from the same
    document are one option, not several. Retrieval proves that single
    candidate, the host answers directly, and no question is opened.
    """
    graph, port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ZEUS_DOC, ZEUS_POLICY_ANSWER),
        ],
        jev_results=[("grounded", 0.99), ("clarify", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _open_thread(client)
    calls_before = len(port.calls)

    sent = _send(client, tid, GAP_FOLLOW_UP)

    assert sent.status_code == 200, sent.text
    assert len(port.calls) > calls_before, (
        "the same-scope follow-up never reached the retrieve port"
    )
    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is None, (
        "two consistent documents in ONE scope produced a clarification: "
        f"{runtime.pending_question!r}"
    )
    status = _status(sent.text)
    answer = status.get("assistant_message") or "".join(_answers(sent.text))
    assert "ZEUS" in answer, (
        f"the answer never states the scope it used: {answer!r}"
    )


def test_multiple_candidate_follow_up_opens_a_persisted_terminal_clarification(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """Two candidate scopes change the answer, so the host clarifies.

    The question must be task-bound, candidate-backed and persisted; the
    internal slot vocabulary is not a user-facing option.
    """
    graph, port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[("grounded", 0.99), ("clarify", 0.99)],
        script=[AIMessage(content="Which company should I use?")],
    )
    tid = _open_thread(client)
    calls_before = len(port.calls)

    payload = _open_clarification(client, graph, tid)

    assert len(port.calls) > calls_before, (
        "the clarification was published before any retrieval across the "
        "corpus"
    )
    runtime = graph.task_runtime_state(tid)
    assert runtime.current_task is not None
    assert runtime.pending_question is not None
    question = runtime.pending_question
    assert question.purpose == PURPOSE_TASK_CLARIFICATION
    assert question.choice_details == ()
    assert question.task_id.strip(), "the pending question has no real task id"
    assert question.task_id == runtime.current_task.task_id

    # The GET projection is the persisted envelope, not a second copy.
    assert payload == question.to_state_dict()
    assert payload["purpose"] == PURPOSE_TASK_CLARIFICATION
    assert payload["task_id"] == runtime.current_task.task_id
    assert payload["choice_details"] == []

    choices = payload["choices"]
    assert choices, "a terminal clarification must offer at least Other"
    assert choices[-1] == OTHER_CHOICE_ID
    assert choices.count(OTHER_CHOICE_ID) == 1
    listed = [choice for choice in choices if choice != OTHER_CHOICE_ID]
    assert len(listed) >= 2, (
        "two candidate scopes must offer two real options; "
        f"got {choices!r}"
    )
    for option in listed:
        _assert_no_internal_slot_names(option)
        assert any(
            marker.casefold() in option.casefold()
            for marker in REAL_CANDIDATE_MARKERS
        ), f"invented option with no catalog/retrieval backing: {option!r}"
    _assert_no_internal_slot_names(payload["prompt"])


def test_filing_gap_follow_up_with_one_scope_retrieves_and_answers(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """A metric ask that names no company/period is still a gap.

    The contract does not carve out "filing" questions: if one scope can
    answer, retrieval proves it and the host answers without asking the
    operator to fill a slot the corpus already fixes.
    """
    graph, port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW],
        chunks=[_chunk(ZEUS_DOC, ZEUS_ANSWER)],
        jev_results=[("grounded", 0.99), ("clarify", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _open_thread(client)
    calls_before = len(port.calls)

    sent = _send(client, tid, FILING_GAP_FOLLOW_UP)

    assert sent.status_code == 200, sent.text
    assert len(port.calls) > calls_before, (
        "the filing-gap follow-up never reached the retrieve port"
    )
    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is None, (
        "one answerable filing scope still produced a clarification: "
        f"{runtime.pending_question!r}"
    )
    answer = _status(sent.text).get("assistant_message") or "".join(
        _answers(sent.text)
    )
    assert "ZEUS" in answer, (
        f"the answer never states the scope it used: {answer!r}"
    )


def test_filing_gap_follow_up_with_multiple_scopes_clarifies_with_real_options(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """Two scopes change the answer, so even a filing gap clarifies.

    The options still come from the catalog; the internal slot vocabulary
    is never the question.
    """
    graph, _port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[("grounded", 0.99), ("clarify", 0.99)],
        script=[AIMessage(content="Which company should I use?")],
    )
    tid = _open_thread(client)

    payload = _open_clarification(
        client, graph, tid, question=FILING_GAP_FOLLOW_UP
    )

    assert payload["purpose"] == PURPOSE_TASK_CLARIFICATION
    assert payload["task_id"].strip()
    assert payload["choice_details"] == []
    choices = payload["choices"]
    assert choices[-1] == OTHER_CHOICE_ID
    assert choices.count(OTHER_CHOICE_ID) == 1
    listed = [choice for choice in choices if choice != OTHER_CHOICE_ID]
    assert len(listed) >= 2, choices
    for option in listed:
        _assert_no_internal_slot_names(option)
        assert any(
            marker.casefold() in option.casefold()
            for marker in REAL_CANDIDATE_MARKERS
        ), f"invented option with no catalog/retrieval backing: {option!r}"


def test_terminal_clarification_without_real_candidates_offers_only_other(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """No candidate can be named, so the question is answerable by text.

    The contract is explicit: with no real candidates the published
    choices are exactly ``["other"]`` and the question stays answerable
    by free text.
    """
    graph, _port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[],
        chunks=[],
        jev_results=[("clarify", 0.99), ("grounded", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _new_thread(client)
    sent = _send(client, tid, GAP_FOLLOW_UP)
    assert sent.status_code == 200, sent.text

    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is not None, (
        "with no real candidate the host dropped the question instead of "
        "asking for free text"
    )
    payload = client.get(f"/api/threads/{tid}").json()["task_runtime"][
        "pending_question"
    ]
    assert payload is not None
    assert payload["purpose"] == PURPOSE_TASK_CLARIFICATION
    assert payload["choices"] == [OTHER_CHOICE_ID], payload["choices"]
    assert payload["choice_details"] == []
    assert payload["prompt"].strip()
    _assert_no_internal_slot_names(payload["prompt"])

    # The free-text-only question is answerable: Other plus non-blank text
    # consumes it and opens a new turn.
    turns_before = len(client.get(f"/api/threads/{tid}").json()["turns"])
    replied = _send(
        client,
        tid,
        FREE_TEXT,
        question_id=payload["question_id"],
        choice_id=OTHER_CHOICE_ID,
        free_text=FREE_TEXT,
    )
    assert replied.status_code == 200, replied.text
    after = graph.task_runtime_state(tid)
    assert after.pending_question is None
    assert after.input_history[-1].kind == "reply"
    assert after.input_history[-1].choice_id == OTHER_CHOICE_ID
    assert after.input_history[-1].payload == FREE_TEXT
    assert len(client.get(f"/api/threads/{tid}").json()["turns"]) == (
        turns_before + 1
    )


# ---------------------------------------------------------------------------
# 4. Reply chain: consume the question, open a new turn, answer with context
# ---------------------------------------------------------------------------


def test_free_text_reply_consumes_the_question_and_opens_a_new_turn(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    graph, _port, model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[("clarify", 0.99), ("grounded", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _new_thread(client)
    opened = _send(client, tid, GAP_FOLLOW_UP)
    assert opened.status_code == 200, opened.text
    payload = client.get(f"/api/threads/{tid}").json()["task_runtime"][
        "pending_question"
    ]
    assert payload is not None, "the opening question did not clarify"
    question_id = payload["question_id"]
    turns_before = client.get(f"/api/threads/{tid}").json()["turns"]
    clarification_run_id = turns_before[-1]["run_id"]

    model.seen.clear()
    replied = _send(
        client,
        tid,
        FREE_TEXT,
        question_id=question_id,
        choice_id=OTHER_CHOICE_ID,
        free_text=FREE_TEXT,
    )

    assert replied.status_code == 200, replied.text
    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is None, (
        "the matching reply left the question pending"
    )
    last_input = runtime.input_history[-1]
    assert last_input.kind == "reply"
    assert last_input.question_id == question_id
    assert last_input.choice_id == OTHER_CHOICE_ID
    assert last_input.payload == FREE_TEXT

    turns_after = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns_after) == len(turns_before) + 1, (
        "the clarification reply did not open a NEW turn"
    )
    assert turns_after[-1]["run_id"] != clarification_run_id, (
        "the reply reused the terminal clarification's run instead of "
        "opening a new turn"
    )
    assert turns_after[-1]["answer"].strip() or turns_after[-1][
        "assistant_message"
    ].strip()

    # ONE composed value is authoritative: the original task question
    # beside the operator's answer, identical on the persisted history
    # row and on the checkpoint's turn message.
    composed = f"原问题：{runtime.current_task.goal}\n补充回答：{FREE_TEXT}"
    assert turns_after[-1]["question"] == composed, (
        "the persisted reply-turn question diverged from the checkpoint's "
        f"composed message: {turns_after[-1]['question']!r}"
    )
    checkpoint_messages = graph._graph.get_state(
        {"configurable": {"thread_id": tid}}
    ).values["messages"]
    human_texts = [
        str(message.content)
        for message in checkpoint_messages
        if isinstance(message, HumanMessage)
    ]
    assert human_texts[-1] == composed, (
        "the checkpoint's reply-turn message is not the persisted question"
    )

    # The reply turn is answered with the ORIGINAL question as context,
    # not as a context-free answer to the free text alone.
    human_texts = [
        str(message.content)
        for call in model.seen
        for message in call
        if isinstance(message, HumanMessage)
    ]
    joined = "\n".join(human_texts)
    assert GAP_FOLLOW_UP in joined, (
        "the reply turn never saw the original question it answers"
    )
    assert FREE_TEXT in joined, "the reply turn never saw the operator's text"


def test_blank_free_text_reply_is_refused_and_leaves_the_question_answerable(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """The association is right but there is no answer value.

    Accepting it would open a turn whose "补充回答" is empty and whose
    accepted input has no original. The typed refusal keeps the question
    pending so the operator can answer it.
    """
    graph, _port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[("clarify", 0.99), ("grounded", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _new_thread(client)
    _send(client, tid, GAP_FOLLOW_UP)
    payload = client.get(f"/api/threads/{tid}").json()["task_runtime"][
        "pending_question"
    ]
    assert payload is not None
    turns_before = len(client.get(f"/api/threads/{tid}").json()["turns"])

    for blank in ("", "   ", "\n\t "):
        refused = _send(
            client,
            tid,
            "",
            question_id=payload["question_id"],
            free_text=blank,
        )
        assert refused.status_code == 400, (
            f"a blank free-text reply ({blank!r}) was not refused: "
            f"{refused.status_code} {refused.text}"
        )
        assert refused.json()["detail"]["error"] == "task_input_invalid"

    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is not None, (
        "a blank reply consumed the question it could not answer"
    )
    assert runtime.pending_question.question_id == payload["question_id"]
    assert len(client.get(f"/api/threads/{tid}").json()["turns"]) == turns_before


def test_a_superseded_free_text_reply_is_refused_with_the_typed_vocabulary(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """Free text without a choice is still an ANSWER attempt.

    A stale association must not become an ordinary new turn just because
    the free-text-only card sends no ``choice_id``.
    """
    graph, _port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[("clarify", 0.99)],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _new_thread(client)
    _send(client, tid, GAP_FOLLOW_UP)
    payload = client.get(f"/api/threads/{tid}").json()["task_runtime"][
        "pending_question"
    ]
    assert payload is not None
    turns_before = len(client.get(f"/api/threads/{tid}").json()["turns"])

    stale = _send(
        client,
        tid,
        "",
        question_id="q-no-longer-pending",
        free_text="ZEUS 2024 年",
    )

    assert stale.status_code == 409, (
        "a stale free-text reply was admitted as a new turn: "
        f"{stale.status_code} {stale.text}"
    )
    assert stale.json()["detail"]["error"] == "task_reply_expired"
    runtime = graph.task_runtime_state(tid)
    assert runtime.pending_question is not None
    assert len(client.get(f"/api/threads/{tid}").json()["turns"]) == turns_before


def test_a_duplicate_free_text_reply_replays_instead_of_opening_a_second_turn(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """A repeat delivery is idempotent even with no ``choice_id``.

    The first delivery is accepted once; the identical second delivery
    returns the first run's outcome and writes no second input or turn.
    """
    graph, _port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[("clarify", 0.99), ("grounded", 0.99)],
        script=_grounded_script(f"ZEUS 的年假政策是：{ZEUS_ANSWER}"),
    )
    tid = _new_thread(client)
    _send(client, tid, GAP_FOLLOW_UP)
    payload = client.get(f"/api/threads/{tid}").json()["task_runtime"][
        "pending_question"
    ]
    assert payload is not None
    # The free-text-only delivery carries no ``choice_id``: this is the
    # shape the review found turning an exact repeat into a second turn.
    body = {
        "question_id": payload["question_id"],
        "free_text": "ZEUS 2024 年",
    }
    first = _send(client, tid, "", **body)
    assert first.status_code == 200, first.text
    version_after_first = graph.task_runtime_state(tid).input_version
    turns_after_first = len(client.get(f"/api/threads/{tid}").json()["turns"])

    replay = _send(client, tid, "", **body)

    assert replay.status_code == 200, replay.text
    assert _status(replay.text)["status"] == _status(first.text)["status"]
    runtime = graph.task_runtime_state(tid)
    assert runtime.input_version == version_after_first, (
        "the duplicate delivery advanced input history a second time"
    )
    last_input = runtime.input_history[-1]
    assert last_input.question_id == payload["question_id"]
    assert last_input.choice_id is None
    assert (
        len(client.get(f"/api/threads/{tid}").json()["turns"])
        == turns_after_first
    ), "the duplicate delivery wrote a second turn"


def _thread_with_pending_clarification(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> tuple[Any, str, int]:
    """A thread whose content clarification is pending, plus its turn count."""
    graph, _port, _model = _install_graph(
        http_app,
        tmp_path,
        rows=[ZEUS_ROW, ACME_ROW],
        chunks=[
            _chunk(ZEUS_DOC, ZEUS_ANSWER),
            _chunk(ACME_DOC, ACME_ANSWER),
        ],
        jev_results=[
            ("grounded", 0.99),
            ("clarify", 0.99),
            ("grounded", 0.99),
            ("grounded", 0.99),
        ],
        script=_grounded_script(ZEUS_ANSWER),
    )
    tid = _open_thread(client)
    _open_clarification(client, graph, tid)
    turns_before = len(client.get(f"/api/threads/{tid}").json()["turns"])
    return graph, tid, turns_before


def test_missing_question_id_is_a_normal_follow_up(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """A message without an association is ordinary input, not a drop."""
    _graph, tid, turns_before = _thread_with_pending_clarification(
        client, http_app, tmp_path
    )

    missing = _send(client, tid, "再补充一句：只看 2024 年")

    assert missing.status_code == 200, missing.text
    turns_after = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns_after) == turns_before + 1
    assert turns_after[-1]["question"] == "再补充一句：只看 2024 年"


def test_unknown_question_id_is_a_normal_follow_up(
    client: TestClient, http_app: FastAPI, tmp_path: Any
) -> None:
    """A stale association must not swallow the operator's message.

    The id names no pending or accepted question, so the send is an
    ordinary follow-up; refusing it as an expired reply would lose the
    input entirely.
    """
    _graph, tid, turns_before = _thread_with_pending_clarification(
        client, http_app, tmp_path
    )

    unknown = _send(client, tid, "那 2024 年呢？", question_id="q-no-longer-here")

    assert unknown.status_code == 200, (
        "an unknown question_id was refused instead of treated as a "
        f"follow-up: {unknown.status_code} {unknown.text}"
    )
    turns_after = client.get(f"/api/threads/{tid}").json()["turns"]
    assert len(turns_after) == turns_before + 1
    assert turns_after[-1]["question"] == "那 2024 年呢？"


# ---------------------------------------------------------------------------
# 5. The document-choice interrupt/resume path is unchanged
# ---------------------------------------------------------------------------


def test_document_choice_with_empty_task_id_round_trips_unchanged() -> None:
    """A document choice is run-bound, not task-bound.

    Its empty ``task_id`` is part of the frozen shape and must stay
    accepted: the run association (``origin_run_id``) is what a pick
    resolves against, and the trailing Other is a real member.
    """
    detail = ChoiceDetail(
        choice_id="doc-a",
        label="ZEUS 2024 年报",
        summary="一份可读的 ZEUS 年报。",
        source_ref=ChoiceSourceRef(
            source_kind="corpus",
            source_id=ZEUS_DOC,
            version="v1",
            section=None,
        ),
    )
    question = offer_document_choice(
        question_id="q-doc",
        task_id="",
        task_version=1,
        prompt="要用哪一份资料对比？",
        choice_details=(detail,),
        origin_run_id="run-7",
        input_version=0,
    )
    # No current task exists yet: the question is published with its
    # contractually empty task id.
    state = open_pending_question(TaskRuntimeState(), question)
    payload = pending_question_payload(state)
    assert payload is not None
    assert payload["task_id"] == ""
    assert payload["purpose"] == PURPOSE_DOCUMENT_CHOICE
    assert payload["choices"] == ["doc-a", OTHER_CHOICE_ID]
    assert payload["choice_details"][0]["source_ref"]["source_id"] == ZEUS_DOC

    restored = TaskRuntimeState.from_state_dict(state.to_state_dict())
    assert restored.pending_question == state.pending_question
    # A web-style pick still resolves against the original run.
    picked = resolved_document_choice_pick(
        restored, origin_run_id="run-7", raw_pick="doc-a"
    )
    assert picked is not None
    assert picked.question_id == "q-doc"
    assert document_choice_resume_value(OTHER_CHOICE_ID, "  换个问法  ") == {
        "choice_id": OTHER_CHOICE_ID,
        "free_text": "  换个问法  ",
    }
