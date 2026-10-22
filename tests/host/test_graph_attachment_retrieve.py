"""T3: the thread-bound attachment branch of ``retrieve``.

Spec ``specs/thread-attachment-source-read.md`` (decision 1 / decision 7,
lines 59 + 61) and plan item 8 (T3 acceptance), pinned through the frozen
``fin.host.attachment.attachment_retrieval`` contract:

* a ready attachment hit carries ``source_ref={kind:"attachment",
  attachment_id,version,unit}`` (``unit`` is the one-based frozen-unit
  ordinal) on the SAME ``retrieve`` envelope the company branch already
  produces;
* the thread scope is host-bound: the tool reads ``thread_id`` from the
  run's ``configurable`` and there is NO ``thread_id`` model argument, so
  a model cannot widen scope — a query run under another thread, and a
  query whose tool args name another thread, both answer for the ACTIVE
  thread only;
* naming attachments narrows the ATTACHMENT branch only — company
  filters and company ranking stay behind the existing port, and a
  comparison question gets both branches in one envelope;
* the branch's typed failures ride ``attachment_error`` and are never
  masked as an empty hit set, while company hits still return normally;
* a run suspended by ``ensure_attachment_ready`` resumes as the SAME run
  and its post-resume retrieve publishes locator-bearing
  ``attachment_chunks`` (plan item 8's gate/branch seam);
* a hit earns evidence under the attachment identity rule (T5): only
  while the run's thread-bound leg answers for that id at that version
  does its text ground a citation, and an unattested hit stays display
  text with nothing else changed; with no retriever wired the
  ``ToolMessage`` envelope is byte-identical to the pre-T3 shape.

Hermetic: a local fake ``AttachmentSearchPort`` + ``FakeRetrievePort`` +
``ScriptedChatModel``, wired through ``compile_graph`` exactly like
``tests/host/test_graph_attachment_gate.py`` drives the preparation gate.
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_retrieval import (
    AttachmentChunk,
    AttachmentIndexUnavailableError,
    AttachmentLocator,
    AttachmentNotReadyError,
    AttachmentRetrievalError,
    AttachmentUnknownLocatorError,
)
from fin.host.compile import compile_for_runtime
from fin.host.graph import _make_retrieve_tool
from fin.host.state import reset_input_state
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.source_read import ATTACHMENT_KIND
from attachment_fakes import FakeAttachmentService

KNOWN_IDS = {"AAPL-2023Q3"}

# Grounded by the track rules tier (mirrors the gate suite).
GROUNDED = "What was AAPL net sales in 2023 Q3?"

COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."
ATTACHMENT_TEXT = "The attached term sheet lists a purchase price of 12,000 thousand."

_THREAD_A = "t3-thread-a"
_THREAD_B = "t3-thread-b"

# The port's documented default window (``limit=None`` means 8), restated
# as a literal so the fake answers the contract independently of the
# implementation's constant.
_DEFAULT_WINDOW = 8


def _company_chunk() -> Chunk:
    return Chunk(
        text=COMPANY_TEXT,
        source_ref=Locator(document_id="AAPL-2023Q3", version="d" * 64, unit=3),
        score=1.5,
    )


def _attachment_chunk(
    attachment_id: str, *, version: int = 2, unit: int = 4, text: str = ATTACHMENT_TEXT
) -> AttachmentChunk:
    return AttachmentChunk(
        text=text,
        source_ref=AttachmentLocator(
            attachment_id=attachment_id, version=version, unit=unit
        ),
        score=0.75,
    )


class FakeAttachmentSearchPort:
    """Scripted thread-keyed attachment index, no attachment module import.

    ``by_thread`` maps a ``thread_id`` to that thread's searchable chunks;
    a thread with no entry has nothing to find. ``error`` (when set) is
    raised on every call, which is how each typed branch failure is
    scripted. Every call's kwargs are recorded so a test can prove the
    scope and the narrowing the host actually passed down.
    """

    def __init__(
        self,
        by_thread: dict[str, list[AttachmentChunk]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.by_thread = by_thread or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[AttachmentChunk]:
        window = _DEFAULT_WINDOW if limit is None else limit
        self.calls.append(
            {
                "thread_id": thread_id,
                "query": query,
                "attachment_ids": list(attachment_ids)
                if attachment_ids is not None
                else None,
                "limit": limit,
            }
        )
        if self.error is not None:
            raise self.error
        chunks = list(self.by_thread.get(thread_id, []))
        if attachment_ids is not None:
            chunks = [
                chunk
                for chunk in chunks
                if chunk.source_ref.attachment_id in set(attachment_ids)
            ]
        return chunks[:window]


class SearchCapableService(FakeAttachmentSearchPort):
    """A preparation service that ALSO answers ``search`` (T3's sibling leg).

    Stands in for the deployed attachment service: the composition root
    hands ``compile_graph`` ONE object that is both the preparation seam
    and the ``AttachmentSearchPort``.
    """


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _script(args: dict[str, Any]) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "retrieve", "args": args, "id": "c-retrieve", "type": "tool_call"}
            ],
        ),
        AIMessage(content="Answer."),
    ]


def _company_port(*, hits: bool) -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={GROUNDED: [_company_chunk()]} if hits else {},
        known_document_ids=set(KNOWN_IDS),
    )


def _compile(
    *,
    company_hits: bool = True,
    attachment_retriever: Any | None = None,
    attachment_reader_factory: Any | None = None,
    checkpointer: Any | None = None,
    model: Any | None = None,
) -> Any:
    model = model or ScriptedChatModel(script=_script({"query": GROUNDED}))
    graph = compile_graph(
        checkpointer=checkpointer or InMemorySaver(),
        model=model,
        adapter=_company_port(hits=company_hits),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=attachment_retriever,
        attachment_source_reader_factory=attachment_reader_factory,
    )
    graph.test_model = model
    return graph


def _run(graph: Any, thread_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    if args is not None:
        graph.test_model.script = _script(args)
    return graph.invoke(
        reset_input_state(GROUNDED),
        # A fresh run id keeps the process-local tool-correction registry
        # from carrying one test's correction state into another's run.
        config={
            "configurable": {
                "thread_id": thread_id,
                "run_id": f"test-run-{uuid.uuid4()}",
            }
        },
    )


def _envelope(out: dict[str, Any]) -> dict[str, Any]:
    messages = [
        m
        for m in out["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "retrieve"
    ]
    assert len(messages) == 1, f"expected one retrieve ToolMessage: {messages}"
    return json.loads(str(messages[0].content))


# ---------------------------------------------------------------------------
# the hit shape
# ---------------------------------------------------------------------------


def test_attachment_hit_carries_the_typed_locator() -> None:
    retriever = FakeAttachmentSearchPort(
        {_THREAD_A: [_attachment_chunk("att-1", version=7, unit=2)]}
    )
    graph = _compile(attachment_retriever=retriever)

    envelope = _envelope(_run(graph, _THREAD_A))

    assert envelope["attachment_chunks"] == [
        {
            "text": ATTACHMENT_TEXT,
            "source_ref": {
                "kind": "attachment",
                "attachment_id": "att-1",
                "version": 7,
                "unit": 2,
            },
            "score": 0.75,
        }
    ]
    # The scope and the query the host bound: the ORIGINAL query, verbatim.
    assert retriever.calls == [
        {
            "thread_id": _THREAD_A,
            "query": GROUNDED,
            "attachment_ids": None,
            "limit": 8,
        }
    ]


def test_an_attested_attachment_hit_enters_the_evidence_channel() -> None:
    """Retrieved attachment text is this-run evidence, exactly as a read
    slice is: the identity comes from the trusted record, the text from
    the hit."""
    retriever = FakeAttachmentSearchPort(
        {_THREAD_A: [_attachment_chunk("att-1"), _attachment_chunk("att-2")]}
    )
    graph = _compile(
        attachment_retriever=retriever,
        attachment_reader_factory=lambda thread_id: _HandoffReader(
            ATTACHMENT_TEXT, thread_id=thread_id, version=2
        ),
    )

    out = _run(graph, _THREAD_A)

    assert len(_envelope(out)["attachment_chunks"]) == 2
    attachments = [
        item for item in out["evidence"] if item.kind == ATTACHMENT_KIND
    ]
    assert [item.attachment_id for item in attachments] == ["att-1", "att-2"]
    assert [item.locator.unit for item in attachments] == [4, 4]
    assert all(
        (item.filename, item.display) == ("memo.txt", "unit")
        for item in attachments
    )
    # The company leg is untouched by the branch: one hit, one item.
    assert [
        (item.kind, item.locator.document_id)
        for item in out["evidence"]
        if item.kind != ATTACHMENT_KIND
    ] == [("company", "AAPL-2023Q3")]


def test_a_quote_from_a_retrieved_hit_alone_carries_an_attachment_citation() -> None:
    """The end-to-end clause: text the run only ever ``retrieve``d — never
    ``read_source``d — grounds a ``unit`` citation carrying the
    attachment's own identity, not a document id."""
    chunk = _attachment_chunk("MEMO-1", version=2, unit=4)
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": GROUNDED},
                        "id": "c-retrieve",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Verbatim: {ATTACHMENT_TEXT}"),
        ]
    )
    graph = _compile(
        attachment_retriever=FakeAttachmentSearchPort({_THREAD_A: [chunk]}),
        attachment_reader_factory=lambda thread_id: _HandoffReader(
            chunk.text, thread_id=thread_id, version=2
        ),
        model=model,
    )

    out = _run(graph, _THREAD_A)

    assert out["status"] == "done", out
    assert [citation.kind for citation in out["citations"]] == ["attachment"]
    citation = out["citations"][0]
    assert citation.attachment_id == "MEMO-1"
    assert citation.document_id == ""
    assert citation.unit == 4
    assert citation.display == "unit"
    assert citation.filename == "memo.txt"


def test_an_unattested_attachment_hit_stays_display_text_only() -> None:
    """No identity leg wired: the published text is unchanged, and the
    evidence channel holds the company hit alone — a quote the model
    copies from such a hit has nothing to be attributed to."""
    retriever = FakeAttachmentSearchPort(
        {_THREAD_A: [_attachment_chunk("att-1"), _attachment_chunk("att-2")]}
    )
    graph = _compile(attachment_retriever=retriever)

    out = _run(graph, _THREAD_A)

    assert len(_envelope(out)["attachment_chunks"]) == 2
    assert [item.kind for item in out["evidence"]] == ["company"]


def test_a_hit_the_record_has_been_reprepared_past_earns_no_evidence() -> None:
    """The version the hit carries is the version the citation would
    claim; a record that has moved on answers for nothing."""
    graph = _compile(
        attachment_retriever=FakeAttachmentSearchPort(
            {_THREAD_A: [_attachment_chunk("att-1", version=2)]}
        ),
        attachment_reader_factory=lambda thread_id: _HandoffReader(
            ATTACHMENT_TEXT, thread_id=thread_id, version=3
        ),
    )

    out = _run(graph, _THREAD_A)

    assert len(_envelope(out)["attachment_chunks"]) == 1
    assert [item.kind for item in out["evidence"]] == ["company"]


# ---------------------------------------------------------------------------
# the scope is host-bound, never model-bound
# ---------------------------------------------------------------------------


def test_a_query_under_another_thread_finds_nothing_for_thread_a() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(attachment_retriever=retriever)

    envelope = _envelope(_run(graph, _THREAD_B))

    assert envelope["attachment_chunks"] == []
    assert "attachment_error" not in envelope
    assert retriever.calls[0]["thread_id"] == _THREAD_B


def test_a_model_supplied_thread_id_cannot_redirect_scope() -> None:
    """No ``thread_id`` model argument exists, and a forged key in the
    tool args cannot reach the port: the scope stays config-bound."""
    tool = _make_retrieve_tool(_company_port(hits=True))
    assert "thread_id" not in tool.args, tool.args
    assert "config" not in tool.args, tool.args

    retriever = FakeAttachmentSearchPort(
        {
            _THREAD_A: [_attachment_chunk("att-a", text="A only text")],
            _THREAD_B: [_attachment_chunk("att-b", text="B only text")],
        }
    )
    graph = _compile(attachment_retriever=retriever)

    out = _run(
        graph,
        _THREAD_A,
        args={"query": GROUNDED, "thread_id": _THREAD_B},
    )

    # P3 T2: the forged key is not a declared argument, so the canonical
    # schema refuses the call BEFORE the port is reached.
    messages = [
        message
        for message in out["messages"]
        if isinstance(message, ToolMessage)
        and getattr(message, "name", None) == "retrieve"
    ]
    assert len(messages) == 1, messages
    payload = json.loads(str(messages[0].content))
    assert payload["error"] == "invalid_tool_arguments"
    assert "thread_id" in payload["feedback"]
    assert _THREAD_B not in str(messages[0].content)
    assert retriever.calls == []


# ---------------------------------------------------------------------------
# naming narrows the attachment branch only
# ---------------------------------------------------------------------------


def test_attachment_ids_is_a_model_facing_optional_argument() -> None:
    tool = _make_retrieve_tool(_company_port(hits=True))
    assert "attachment_ids" in tool.args, tool.args


def test_naming_one_attachment_excludes_the_other() -> None:
    retriever = FakeAttachmentSearchPort(
        {
            _THREAD_A: [
                _attachment_chunk("att-1", text="first attachment"),
                _attachment_chunk("att-2", text="second attachment"),
            ]
        }
    )
    graph = _compile(attachment_retriever=retriever)

    envelope = _envelope(
        _run(graph, _THREAD_A, args={"query": GROUNDED, "attachment_ids": ["att-1"]})
    )

    ids = [c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]]
    assert ids == ["att-1"]
    assert retriever.calls[0]["attachment_ids"] == ["att-1"]


def test_naming_an_attachment_leaves_the_company_filters_untouched() -> None:
    """``attachment_ids`` narrows the ATTACHMENT branch; the company port
    keeps its own filters and its own ranking (decision 7)."""
    port = _company_port(hits=True)
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    tool = _make_retrieve_tool(port, attachment_retriever=retriever)

    tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": GROUNDED, "ticker": "AAPL", "attachment_ids": ["att-9"]},
            "id": "c1",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": _THREAD_A}},
    )

    assert port.calls[0]["ticker"] == "AAPL"
    assert "attachment_ids" not in port.calls[0], port.calls[0]
    assert retriever.calls[0]["attachment_ids"] == ["att-9"]


def test_a_comparison_question_returns_both_branches_in_one_envelope() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(company_hits=True, attachment_retriever=retriever)

    envelope = _envelope(_run(graph, _THREAD_A))

    assert [c["source_ref"]["document_id"] for c in envelope["chunks"]] == [
        "AAPL-2023Q3"
    ]
    assert [c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]] == [
        "att-1"
    ]


# ---------------------------------------------------------------------------
# typed branch failures: named, never an empty success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AttachmentUnknownLocatorError(), "unknown_locator"),
        (AttachmentNotReadyError(), "attachment_not_ready"),
        (AttachmentIndexUnavailableError(), "attachment_index_unavailable"),
        # The base class with an instance-level named code (how a failed
        # row's stored reason arrives): the leg catches the typed BASE, so
        # the specific code rides the envelope unchanged.
        (
            AttachmentRetrievalError(code="attachment_empty_text"),
            "attachment_empty_text",
        ),
    ],
)
def test_a_typed_failure_names_itself_and_does_not_mask(
    error: Exception, code: str
) -> None:
    retriever = FakeAttachmentSearchPort(
        {_THREAD_A: [_attachment_chunk("att-1")]}, error=error
    )
    graph = _compile(attachment_retriever=retriever)

    envelope = _envelope(_run(graph, _THREAD_A))

    assert envelope["attachment_error"] == {"error": code}
    # Never an empty success riding alongside the failure.
    assert "attachment_chunks" not in envelope
    # The company branch still answers normally.
    assert [c["source_ref"]["document_id"] for c in envelope["chunks"]] == [
        "AAPL-2023Q3"
    ]


def test_an_empty_clean_result_publishes_an_empty_chunk_list() -> None:
    """A legal query with no attachment hits is a clean run of the branch:
    the pinned shape is ``attachment_chunks: []`` (present, not omitted)."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: []})
    graph = _compile(attachment_retriever=retriever)

    envelope = _envelope(_run(graph, _THREAD_A))

    assert envelope["attachment_chunks"] == []
    assert "attachment_error" not in envelope


def test_an_unbound_thread_scope_never_asks_the_port() -> None:
    """No host-bound thread_id means no scope: the branch is skipped rather
    than searched against an empty or guessed thread."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    tool = _make_retrieve_tool(_company_port(hits=True), attachment_retriever=retriever)

    command = tool.invoke(
        {"name": "retrieve", "args": {"query": GROUNDED}, "id": "c1", "type": "tool_call"},
        config={"configurable": {}},
    )

    assert retriever.calls == []
    envelope = json.loads(str(command.update["messages"][0].content))
    assert "attachment_chunks" not in envelope
    assert "attachment_error" not in envelope


def test_the_search_runs_once_even_when_the_company_leg_rewrites() -> None:
    """The empty-hit rewrite retry stays company-port-only."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    port = _company_port(hits=False)
    tool = _make_retrieve_tool(port, attachment_retriever=retriever)

    tool.invoke(
        {"name": "retrieve", "args": {"query": GROUNDED}, "id": "c1", "type": "tool_call"},
        config={"configurable": {"thread_id": _THREAD_A}},
    )

    assert len(port.calls) == 2, "the company leg retried once, as before"
    assert [c["query"] for c in port.calls] != [GROUNDED, GROUNDED]
    assert [c["query"] for c in retriever.calls] == [GROUNDED]


# ---------------------------------------------------------------------------
# unwired retriever: no attachment keys reach the envelope
# ---------------------------------------------------------------------------


def test_retriever_absent_keeps_the_envelope_byte_identical() -> None:
    graph = _compile(company_hits=True)

    envelope = _envelope(_run(graph, _THREAD_A))

    assert envelope == {
        "chunks": [
            {
                "text": COMPANY_TEXT,
                "source_ref": {
                    "document_id": "AAPL-2023Q3",
                    "version": "d" * 64,
                    "unit": 3,
                },
                "score": 1.5,
            }
        ],
        "distribution": "hit",
    }


def test_retriever_absent_keeps_the_miss_envelope_byte_identical() -> None:
    graph = _compile(company_hits=False)

    assert _envelope(_run(graph, _THREAD_A)) == {"chunks": [], "distribution": "zero"}


def test_attachment_ids_without_a_retriever_changes_nothing() -> None:
    """A model naming attachments on a graph with no attachment source
    still gets today's envelope, not an error and not an empty list."""
    graph = _compile(company_hits=False)

    envelope = _envelope(
        _run(graph, _THREAD_A, args={"query": GROUNDED, "attachment_ids": ["att-1"]})
    )

    assert envelope == {"chunks": [], "distribution": "zero"}


# ---------------------------------------------------------------------------
# the gate seam: a suspended run resumes INTO this branch
# ---------------------------------------------------------------------------


def test_a_run_suspended_by_the_gate_resumes_into_locator_publishing() -> None:
    """Plan item 8: an eligible ``uploaded``/``parsing`` attachment suspends
    the dependent run, that SAME run resumes automatically, and its
    subsequent retrieve publishes the locator-bearing ``attachment_chunks``.

    This pins the seam between the two T3 halves (gate wait, branch read)
    that neither suite covered alone.
    """
    service = FakeAttachmentService({"att-1": ["pending", "ready"]})
    retriever = FakeAttachmentSearchPort(
        {_THREAD_A: [_attachment_chunk("att-1", version=3, unit=5)]}
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=_script({"query": GROUNDED})),
        adapter=_company_port(hits=True),
        known_document_ids=set(KNOWN_IDS),
        attachment_service=service,
        attachment_retriever=retriever,
    )
    config = {"configurable": {"thread_id": _THREAD_A}}
    out = graph.invoke(
        reset_input_state(
            GROUNDED,
            attachments=[
                {
                    "attachment_id": "att-1",
                    "filename": "term-sheet.pdf",
                    "text": "The attached term sheet lists a purchase price.",
                }
            ],
        ),
        config=config,
    )
    interrupts = out.get("__interrupt__") or ()
    assert interrupts, "an eligible in-flight attachment suspends the run"
    assert interrupts[0].value == {
        "kind": "attachment_preparation",
        "attachment_ids": ["att-1"],
    }
    # The pause is before the grounded leaf: nothing reached the branch.
    assert retriever.calls == []

    # The resume needs no operator resend, and the run's own retrieve
    # answers with the typed locator (unit is the one-based ordinal).
    done = graph.invoke(Command(resume="job-done"), config=config)
    assert not done.get("__interrupt__")
    envelope = _envelope(done)
    assert envelope["attachment_chunks"] == [
        {
            "text": ATTACHMENT_TEXT,
            "source_ref": {
                "kind": "attachment",
                "attachment_id": "att-1",
                "version": 3,
                "unit": 5,
            },
            "score": 0.75,
        }
    ]
    assert envelope["attachment_chunks"][0]["source_ref"]["unit"] >= 1
    # Replay shape: the run paused on this row's job and the resumed run
    # re-checked it without any operator resend. The GATE loop re-consults
    # on every LLM entry, so the call COUNT is topology; the identity of
    # what was waited on (and never re-sent as a new turn) is the pin.
    assert service.prep_calls[0] == (_THREAD_A, "att-1")
    assert set(service.prep_calls) == {(_THREAD_A, "att-1")}
    assert len(retriever.calls) == 1


# ---------------------------------------------------------------------------
# wiring: the compile seam and the graph cache identity
# ---------------------------------------------------------------------------


def test_two_retrievers_are_two_graphs_and_one_retriever_is_one_graph() -> None:
    checkpointer = InMemorySaver()
    model = ScriptedChatModel(script=_script({"query": GROUNDED}))
    retriever_1 = FakeAttachmentSearchPort()
    retriever_2 = FakeAttachmentSearchPort()

    first = compile_graph(
        checkpointer=checkpointer,
        model=model,
        adapter=_company_port(hits=True),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever_1,
    )
    same = compile_graph(
        checkpointer=checkpointer,
        model=model,
        adapter=_company_port(hits=True),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever_1,
    )
    other = compile_graph(
        checkpointer=checkpointer,
        model=model,
        adapter=_company_port(hits=True),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever_2,
    )
    unwired = compile_graph(
        checkpointer=checkpointer,
        model=model,
        adapter=_company_port(hits=True),
        known_document_ids=set(KNOWN_IDS),
    )

    assert same is first
    assert other is not first
    assert unwired is not first


class _FakeChatOpenAI:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def bind_tools(self, _tools: object) -> "_FakeChatOpenAI":
        return self

    def bind(self, **_kwargs: object) -> "_FakeChatOpenAI":
        return self


class _FakePostgresSaver:
    def setup(self) -> None:
        return None


class _FakePostgresCM:
    def __enter__(self) -> _FakePostgresSaver:
        return _FakePostgresSaver()

    def __exit__(self, *exc: object) -> None:
        return None


class _FakePostgresSaverFactory:
    @classmethod
    def from_conn_string(cls, _url: str, **_kwargs: Any):
        return _FakePostgresCM()


def test_the_composition_root_threads_the_service_as_the_retriever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compile_for_runtime`` hands its resolved attachment service to
    ``compile_graph`` as ``attachment_retriever`` when the service is
    there, and hands nothing when it is not."""
    chat_module = types.ModuleType("langchain_openai")
    chat_module.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", chat_module)
    pg_module = types.ModuleType("langgraph.checkpoint.postgres")
    pg_module.PostgresSaver = _FakePostgresSaverFactory
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", pg_module)
    for key in ("FIN_RETRIEVE_ADAPTER", "FIN_MODEL", "FIN_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FIN_MODEL", "synthetic-attachment-test-model")

    calls: list[dict[str, Any]] = []

    def _fake_compile_graph(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "compiled"

    monkeypatch.setattr("fin.host.compile.compile_graph", _fake_compile_graph)

    service = SearchCapableService()
    compile_for_runtime(checkpointer_url="postgresql://x", attachment_service=service)
    assert calls[-1]["attachment_retriever"] is service

    compile_for_runtime(checkpointer_url="postgresql://x")
    assert calls[-1]["attachment_retriever"] is None


# ---------------------------------------------------------------------------
# T3 -> T4 handoff (integration gate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HandoffRecord:
    """The trusted record a handoff read was admitted under."""

    thread_id: str
    filename: str
    version: int


class _HandoffReader:
    """Attachment read leg that bounds real text through the T4 contract.

    Carries the two identity faces the host asks of the run's own leg: the
    record behind an ``attachment_id`` (safe filename + version) and
    whether that identity is still current for the run's thread. A reader
    without them returns text the citation gate cannot attribute.
    """

    def __init__(self, text: str, *, thread_id: str, version: int = 2) -> None:
        self._text = text
        self._record = _HandoffRecord(
            thread_id=thread_id, filename="memo.txt", version=version
        )

    def read_slice(self, source_ref: Any, position: int = 0) -> dict[str, Any]:
        from fin.retrieve.source_read import bounded_slice

        return bounded_slice(dict(source_ref), self._text, position)

    def owned_record(self, attachment_id: str) -> _HandoffRecord:
        return self._record

    def identity_is_current(
        self, attachment_id: str, version_int: int | None
    ) -> bool:
        return attachment_id == "MEMO-1" and version_int == self._record.version


class _StaleHandoffReader(_HandoffReader):
    """A leg whose record still grants the display pair while its parsed
    content has moved to another version — the race the citation
    re-check exists to close.
    """

    def identity_is_current(
        self, attachment_id: str, version_int: int | None
    ) -> bool:
        return False


class _FaultingHandoffReader(_HandoffReader):
    """A leg whose store cannot answer the identity question at all."""

    def identity_is_current(
        self, attachment_id: str, version_int: int | None
    ) -> bool:
        raise OSError("store unreachable")


class _ReadonlyHandoffReader(_HandoffReader):
    """A leg with no identity face: it can return text and nothing else."""

    identity_is_current = None  # type: ignore[assignment]


def _run_attachment_read_handoff(
    reader_factory: Any,
) -> dict[str, Any]:
    """One graph, one thread: retrieve, read the published locator, then
    answer with the attachment text verbatim.

    The answer is word-for-word grounded, so whether it can carry a
    citation depends only on the identity the host can attest.
    """
    chunk = _attachment_chunk("MEMO-1", version=2, unit=4)
    handoff_ref = chunk.source_ref.as_source_ref()
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": GROUNDED},
                        "id": "c-retrieve",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {"source_ref": handoff_ref, "position": 0},
                        "id": "c-read",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Verbatim: {ATTACHMENT_TEXT}"),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_company_port(hits=True),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=FakeAttachmentSearchPort({_THREAD_A: [chunk]}),
        attachment_source_reader_factory=reader_factory,
    )
    return graph.invoke(
        reset_input_state(GROUNDED),
        config={"configurable": {"thread_id": _THREAD_A}},
    )


def test_same_thread_retrieve_then_read_source_handoff() -> None:
    """The locator T3 publishes is the locator T4 reads, in one run.

    One graph wiring BOTH seams (search port + source-reader factory),
    one thread: ``retrieve`` answers with the typed ``source_ref``, the
    model copies that exact dict into ``read_source``, the slice comes
    back bounded, and the citation carries the attachment identity the
    host attested — not anything the answer claimed.
    """
    chunk = _attachment_chunk("MEMO-1", version=2, unit=4)
    handoff_ref = chunk.source_ref.as_source_ref()
    built: list[str] = []

    def factory(thread_id: str) -> Any:
        built.append(thread_id)
        return _HandoffReader(chunk.text, thread_id=thread_id, version=2)

    out = _run_attachment_read_handoff(factory)

    assert out["status"] == "done"
    assert built and set(built) == {_THREAD_A}
    envelope = _envelope(out)
    assert envelope["attachment_chunks"][0]["source_ref"] == handoff_ref
    citation = out["citations"][0]
    # The attachment identity is the attachment id, not a document id: the
    # company slot stays empty, and the presentation comes from the trusted
    # record (a ``.txt`` unit is never labeled a page).
    assert citation.kind == "attachment"
    assert citation.attachment_id == "MEMO-1"
    assert citation.document_id == ""
    assert citation.filename == "memo.txt"
    assert citation.display == "unit"
    assert citation.unit == 4


@pytest.mark.parametrize(
    "reader_class",
    [_FaultingHandoffReader, _ReadonlyHandoffReader, _StaleHandoffReader],
    ids=["store_fault", "no_identity_face", "stale_or_foreign_identity"],
)
def test_an_unattested_attachment_identity_leaves_the_quote_uncitable(
    reader_class: Any,
) -> None:
    """A verbatim attachment quote still needs a current identity.

    A store that cannot answer, a leg that never promised to, and a
    record whose parsed version moved on all fail closed: the run ends
    refused with no citation, and no foreign or stale identity reaches
    the wire.
    """
    out = _run_attachment_read_handoff(
        lambda thread_id: reader_class(
            ATTACHMENT_TEXT, thread_id=thread_id, version=2
        )
    )

    assert out["status"] == "refuse"
    assert out["citations"] == []
