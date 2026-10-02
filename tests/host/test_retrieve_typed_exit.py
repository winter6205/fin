"""The ``retrieve_unavailable`` typed EXIT reaches the model (T3).

Spec ``specs/s1-corpus-retrieve.md`` (Boundaries → Typed EXITs,
success criteria item "Product retrieve with scripted MCP-down"):

    **retrieve_unavailable**: product MCP transport/process down or
    protocol fail → retrieve tool returns ``{error:
    "retrieve_unavailable"}``; does not return ``{chunks:[]}`` as if
    a miss; does not bind archived BM25 (ADR 0030).

The port-level half of that contract is covered by
``tests/retrieve/test_mcp_adapter.py``. This module covers the HOST
half: the ``ToolMessage`` the agent loop appends must carry the
error dict verbatim, so the model can distinguish "retrieval is
down, retry or say so" from "the corpus has no such passage".
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import _make_retrieve_tool
from fin.retrieve import Chunk


class _UnavailablePort:
    """Retrieve port whose every call reports the typed EXIT.

    Mirrors ``MCPRetrieveAdapter.retrieve`` after the transport
    failed: the sync seam translates ``RetrieveUnavailableError``
    into the structured error dict.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return {"error": "retrieve_unavailable"}


class _HitPort:
    """Retrieve port returning one locatable chunk."""

    def __init__(self, chunk: Chunk) -> None:
        self._chunk = chunk
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return {"chunks": [self._chunk]}


def _locator() -> Locator:
    return Locator(
        document_id="DOC-2023Q1", version="v-" + "a" * 60, unit=1
    )


def _invoke_retrieve_tool(adapter: Any, **kwargs: Any):
    """Run the host ``retrieve`` tool once and return its ``Command``."""
    tool = _make_retrieve_tool(adapter)
    payload = {"query": "what was revenue", **kwargs}
    return tool.invoke(
        {
            "name": "retrieve",
            "args": payload,
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


# ----- tool-level surface -----


def test_retrieve_tool_message_carries_error_dict_not_empty_chunks() -> None:
    """MCP-down must NOT be serialized as ``{"chunks": []}``.

    ``{"chunks": []}`` is the MISS shape (unknown document_id,
    unknown filter, no hits, mapper-drop; an empty query is invalid
    input and gets its own named code per T21). Reusing it for a
    transport failure
    tells the model "this corpus has nothing" when the truth is
    "retrieval never ran".
    """
    command = _invoke_retrieve_tool(_UnavailablePort())
    messages = command.update["messages"]
    assert len(messages) == 1
    tool_message = messages[0]
    assert isinstance(tool_message, ToolMessage)
    payload = json.loads(tool_message.content)
    assert payload == {"error": "retrieve_unavailable"}
    assert "chunks" not in payload


def test_retrieve_tool_error_contributes_no_evidence() -> None:
    """A typed EXIT contributes no ``EvidenceItem`` — there is no
    unit text to cite. The citation gate must not see phantom
    evidence from a failed transport."""
    command = _invoke_retrieve_tool(_UnavailablePort())
    assert not command.update.get("evidence")


def test_retrieve_tool_hit_still_serializes_chunks() -> None:
    """The happy path is unchanged: chunks serialize to the
    contract dict and each chunk contributes one evidence item."""
    chunk = Chunk(text="Revenue rose.", source_ref=_locator(), score=2.5)
    command = _invoke_retrieve_tool(_HitPort(chunk))
    payload = json.loads(command.update["messages"][0].content)
    assert payload["chunks"] == [
        {
            "text": "Revenue rose.",
            "source_ref": {
                "document_id": "DOC-2023Q1",
                "version": "v-" + "a" * 60,
                "unit": 1,
            },
            "score": 2.5,
        }
    ]
    evidence = command.update["evidence"]
    assert len(evidence) == 1
    assert evidence[0].locator == _locator()


def test_retrieve_tool_miss_still_serializes_empty_chunks() -> None:
    """A genuine miss keeps the ``{"chunks": []}`` shape — the
    error branch must not swallow the miss contract."""

    class _MissPort:
        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            return {"chunks": []}

    command = _invoke_retrieve_tool(_MissPort())
    payload = json.loads(command.update["messages"][0].content)
    assert payload == {"chunks": []}


def test_retrieve_tool_forwards_edgar_filters_to_the_port() -> None:
    """ADR 0031 additive filters (``cik`` / ``form`` / ``ticker`` /
    exact ``filing_date``) are part of the port contract; the host
    tool must forward them instead of dropping them on the floor."""
    port = _HitPort(Chunk(text="t", source_ref=_locator(), score=1.0))
    _invoke_retrieve_tool(
        port,
        document_id="DOC-2023Q1",
        cik="0000320193",
        form="10-Q",
        ticker="AAPL",
        filing_date="2023-08-04",
    )
    assert port.calls == [
        {
            "query": "what was revenue",
            "document_id": "DOC-2023Q1",
            "cik": "0000320193",
            "form": "10-Q",
            "ticker": "AAPL",
            "filing_date": "2023-08-04",
            "limit": 8,
            # The host tool assigns no 档 (that is the host cascade's
            # job, another spec): the port receives an explicit
            # ``None`` = "do not send", iknow's default B.
            "search_mode": None,
            # Record-shape half since #322 T7 (6e8ed97): the sql 档's
            # window pair is recorded on EVERY call (the keys are the
            # port's contract shape; values stay ``None`` here because
            # no 档 — let alone sql — was assigned).
            "sql_window_start": None,
            "sql_window_end": None,
        }
    ]


# ----- host-graph surface -----


def test_host_graph_surfaces_retrieve_unavailable_to_the_model() -> None:
    """End-to-end through the compiled graph: the ``ToolMessage``
    appended to the run's message list says ``retrieve_unavailable``.

    This is the surface the next ``model_node`` call reads, so it is
    the one the spec's "surface the error to the model" clause is
    about.
    """
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "what was revenue"},
                        "id": "c-retrieve",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Retrieval is unavailable right now."),
        ],
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_UnavailablePort(),
        known_document_ids={"DOC-2023Q1"},
    )
    out = graph.invoke(
        {
            # Names a company AND a period so Soft-Track routes the
            # turn to the research leaf (a bare "what was revenue?"
            # is a clarify ask and never reaches the tools).
            "messages": [HumanMessage("What was AAPL revenue in 2023 Q1?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config={"configurable": {"thread_id": "t-retrieve-unavailable"}},
    )
    tool_messages = [
        m for m in out["messages"] if isinstance(m, ToolMessage)
    ]
    assert tool_messages, "the run appended no ToolMessage"
    payloads = [json.loads(m.content) for m in tool_messages]
    assert {"error": "retrieve_unavailable"} in payloads
    assert not out.get("evidence")
