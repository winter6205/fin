"""SC8: research ``done`` with a citation on the live ingested fixture.

The default ``uv run pytest -q`` never runs this file — it is marked
``mcp`` and ``tests/retrieve/conftest.py`` deselects that mark unless
``-m mcp`` names it, so the default suite stays hermetic (SC6). The
opt-in command documented in ``plans/first-closed-loop.md`` is::

    uv run pytest -q -m mcp tests/retrieve/test_live_research_cite_on_ingest.py

What the ticket covers, on the wire
-----------------------------------

T2 (``test_live_corpus_ingest.py``) proved the ingest half: a real repo
fixture goes through ``IngestEndpoint`` + ``MCPPushPort`` into the live
iknow server, and a sibling retrieve comes back with canonical,
byte-identical chunks. This file takes the same live session one hop
further and drives the **product research track** over it: a compiled
host graph (:func:`fin.host.compile_graph`) whose retrieve / ``read``
faces are the same ``MCPRetrieveAdapter`` the product boots, plus a
scripted model. Spec SC8 asks for one research send that ends ``done``
whose citation span is a substring of **this run's** retrieve text — so
the two assertions that carry the test are:

* the run's status is ``done`` and it carries >= 1 citation;
* every citation's ``quote`` is a substring of a chunk text THIS RUN
  received, and every citation's ``(document_id, version, page)`` is one
  of that payload's locators (ADR 0021: the locator comes from the
  evidence, never from the model's claim).

Why the model reads the payload instead of a pasted literal
-----------------------------------------------------------

The scripted model answers with a span it extracts from the retrieve
``ToolMessage`` of the SAME run. A literal pasted into the test would
keep passing after the store stopped returning that text — it would
prove the graph can match strings, not that the retrieval path
delivered them. With the span derived at runtime, an empty or changed
store makes the model produce an ungrounded answer, the citation gate
refuses, and the SC8 assertions fail with the run's own reason.

The negative half is the same run shape with an answer that is NOT
grounded in this-run text: evidence IS present, the model just does not
quote it. That run must refuse (``citation_repair_cap`` — the verifier's
own verdict, since the collection was not what failed) rather than
``done`` with an empty-or-borrowed citation. Without it, a gate that
passed everything would satisfy the positive assertions.

What this file deliberately does NOT re-test
--------------------------------------------

* SC2/SC3 (pre-ingest empty baseline, byte-identical chunks) — T2's pin.
* SC6 (attach != push) — ``tests/host/test_thread_attach.py``.
* The verifier's own span / refusals semantics —
  ``tests/host/test_citation_gate.py``.

Reach limitation (stated, not faked)
------------------------------------

The graph is built by :func:`compile_graph` with the live adapter
injected directly (``adapter=live.retrieve``), which is how this suite's
live tests reach the product port; it is NOT the ``fin.host.http`` boot
path (``compile_for_http`` -> ``compile_for_runtime``), which also
resolves the checkpoint URL, the operator env, and the knowledge-MCP
probe. The adapter instance under test IS the product class over a real
stdio session, and the retrieve request IS on the wire (asserted
below), so what the product boot adds on top is configuration, not the
retrieve/cite contract this ticket covers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.host import compile_graph
from fin.host.citation_gate import (
    _LOCATOR_REGEX,  # noqa: PLC2701 - the gate's own token shape is the SSOT
)
from fin.host.state import reset_input_state
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_push_port import MCPPushPort

from live_iknow_support import IKNOW_CORPUS_READY_ROOT, live_iknow


pytestmark = pytest.mark.mcp

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The same fixture, doc_type and query T2 measured against this
# worktree's build: `narrative` is the documented prose choice for the
# `table_dense` scene label, and `operating revenue` is the query the
# file's Note 2 – Revenue text ranks for. Reusing them keeps this test
# downstream of T2's proven ingest — and keeps the live store's content
# the same thing both tickets describe.
_FIXTURE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)
_DOC_TYPE = "narrative"
_RETRIEVE_QUERY = "operating revenue"
_RETRIEVE_LIMIT = 8

# ADR 0021's extractive minimum. The model's span is far longer; the
# constant is here so the assertion can name the contract it applies.
_MIN_SPAN_LEN = 32
# How much of the chosen chunk the model quotes. Long enough that a
# coincidental match is not what passes the gate.
_QUOTE_LEN = 96

# A prose answer with no >=32-char span in the fixture (measured against
# the whole document, a superset of every chunk text) and no
# locator-shaped token. It is the verifier's ordinary bounce path.
_UNGROUNDED = (
    "Management expects the pace of hiring to slow through the "
    "remainder of the fiscal year."
)
# The question marker that selects the negative answer. Routing is
# unaffected: both questions carry a ticker + period + metric, so
# ``decide_track`` sends them to the research track by its own rules.
_GROUNDED_MARKER = "grounded"
_UNGROUNDED_MARKER = "ungrounded"
_GROUNDED_QUESTION = (
    f"{_GROUNDED_MARKER}: what did AAPL report for operating revenue "
    "in 2022 Q3?"
)
_UNGROUNDED_QUESTION = (
    f"{_UNGROUNDED_MARKER}: what did AAPL report for operating revenue "
    "in 2022 Q3?"
)

_RETRIEVE_CALL_ID = "c-sc8-retrieve"


def _retrieved_payload(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """The chunk dicts the model received from this run's ``retrieve``.

    Reads the ``ToolMessage`` the retrieve tool wrote — the same text
    the citation gate admitted as evidence — so the assertions are
    about what the store returned in THIS run, not about a fixture on
    disk.
    """
    for message in messages:
        if (
            getattr(message, "name", None) == "retrieve"
            and isinstance(message.content, str)
        ):
            body = json.loads(message.content)
            chunks = body.get("chunks")
            if isinstance(chunks, list):
                return list(chunks)
    return []


def _quote_span(chunks: Sequence[dict[str, Any]]) -> str | None:
    """The span the model quotes: a real prefix of a real chunk.

    The first chunk long enough to quote and free of locator-shaped
    tokens the run cannot vouch for (the fixture's dataset header
    carries ``AAPL-2022Q3``, which is this ingest's *source* document,
    not its ``document_id`` — quoting it would refuse on
    ``unknown_locator`` for a reason that has nothing to do with SC8).
    ``None`` when no chunk qualifies; the caller then answers
    ungrounded, so a store that stopped returning quotable text fails
    the SC8 assertions instead of passing on a stale literal.
    """
    for chunk in chunks:
        text = str(chunk.get("text", ""))
        if len(text) < _MIN_SPAN_LEN:
            continue
        locator = chunk.get("source_ref") or {}
        known = {str(locator.get("document_id", ""))}
        if set(_LOCATOR_REGEX.findall(text)) - known:
            continue
        span = text.strip()[:_QUOTE_LEN].rstrip()
        if len(span) >= _MIN_SPAN_LEN:
            return span
    return None


class _CitedAnswerModel(BaseChatModel):
    """One retrieve call, then an answer built from ITS OWN payload.

    Routing is by transcript content, not by call index (mirrors
    ``_TwoRunScriptedModel`` in ``tests/host/test_named_failure_retrieve.py``):
    the first turn asks for ``retrieve``, and every later turn answers
    — quoted, unless the question carried the ungrounded marker.
    """

    @property
    def _llm_type(self) -> str:
        return "sc8-cited-answer"

    def bind_tools(
        self, tools: list[Any], **kwargs: Any
    ) -> "_CitedAnswerModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        chunks = _retrieved_payload(messages)
        if not chunks:
            return self._result(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {
                                "query": _RETRIEVE_QUERY,
                                "limit": _RETRIEVE_LIMIT,
                            },
                            "id": _RETRIEVE_CALL_ID,
                            "type": "tool_call",
                        }
                    ],
                )
            )
        transcript = " ".join(
            str(message.content) for message in messages
        )
        span = (
            None
            if _UNGROUNDED_MARKER in transcript
            else _quote_span(chunks)
        )
        if span is None:
            return self._result(AIMessage(content=_UNGROUNDED))
        return self._result(
            AIMessage(
                content=f"According to the ingested filing: {span}"
            )
        )

    @staticmethod
    def _result(message: AIMessage) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def ingested_live(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A live session with the fixture already in the store.

    Same guard and chain as ``test_live_corpus_ingest.py``: the
    corpus-ready worktree build is machine-local (the operator-console
    build's ``push`` predates the pushed-slice fix and raises
    ``PushProtocolError`` on this fixture), so an absent build skips
    cleanly instead of failing on an environment gap. The session is
    threaded into ``MCPPushPort`` by hand for the same reason T2
    documents: a self-spawned push process would own its own in-memory
    store and the graph's retrieve would see nothing.
    """
    entry = IKNOW_CORPUS_READY_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in "
            f"{IKNOW_CORPUS_READY_ROOT} to exercise the live SC8 path"
        )
    with live_iknow(monkeypatch, root=IKNOW_CORPUS_READY_ROOT) as live:
        endpoint = IngestEndpoint(
            push_port=MCPPushPort(session=live.session, timeout_seconds=20.0),
            invalidation_port=live.invalidation,
        )
        result = endpoint.ingest(
            _FIXTURE.read_bytes(),
            filename=_FIXTURE.name,
            document_id="AAPL-2022Q3",
            doc_type=_DOC_TYPE,
        )
        assert result.written >= 1, "the fixture pushed no chunks"
        yield live, result


def _run_question(live: Any, result: Any, question: str, thread_id: str) -> Any:
    """One research send through a graph on the live product port."""
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_CitedAnswerModel(),
        adapter=live.retrieve,
        known_document_ids={result.document_id},
    )
    return graph.invoke(
        reset_input_state(question),
        config={"configurable": {"thread_id": thread_id}},
    )


def test_research_done_cites_this_runs_retrieved_text(
    ingested_live: Any,
) -> None:
    """SC8: one send ends ``done`` with a citation from this-run text."""

    live, result = ingested_live

    # The adapter under test is the product class, and it is wired to
    # the retrieve AND read faces (``compile_graph`` defaults
    # ``read_port`` to the adapter's own ``read``) — the product-config
    # port, not a test double.
    assert isinstance(live.retrieve, MCPRetrieveAdapter)

    out = _run_question(
        live, result, _GROUNDED_QUESTION, "sc8-grounded"
    )

    # The ticket's surface is the RESEARCH track: a run that landed on
    # the dialogue leaf could also report ``done``, and asserting only
    # the status would not tell the two apart.
    assert out["track"] == "grounded", (
        f"SC8 is a research-track contract; this send was routed to "
        f"{out['track']!r}"
    )
    assert out["status"] == "done", (
        f"SC8: the grounded send did not reach done: "
        f"status={out['status']!r} reason={out.get('status_reason')!r}"
    )
    citations = list(out["citations"])
    assert citations, "SC8: a done run must carry >= 1 citation"

    # This run's retrieve payload — the text the gate admitted as
    # evidence, read back from the graph's own state.
    chunks = _retrieved_payload(out["messages"])
    assert chunks, "the run recorded no retrieve payload"
    for chunk in chunks:
        source_ref = chunk["source_ref"]
        assert source_ref["document_id"] == result.document_id
        assert source_ref["version"] == result.version

    retrieved_texts = [str(chunk["text"]) for chunk in chunks]
    retrieved_locators = [
        (
            chunk["source_ref"]["document_id"],
            chunk["source_ref"]["version"],
            chunk["source_ref"]["page"],
        )
        for chunk in chunks
    ]

    for citation in citations:
        assert len(citation.quote) >= _MIN_SPAN_LEN, (
            f"citation quote is shorter than the 32-char extractive "
            f"minimum: {citation.quote!r}"
        )
        # SC8's load-bearing half: the span occurs in text THIS RUN
        # received from retrieve. A citation built from anything else
        # (model memory, the prompt, another run) fails here.
        assert any(citation.quote in text for text in retrieved_texts), (
            "citation quote is not a substring of this-run retrieve "
            f"text: {citation.quote!r}"
        )
        # ADR 0021: the locator is the evidence's, never the model's.
        assert (
            citation.document_id,
            citation.version,
            citation.page,
        ) in retrieved_locators, (
            f"citation locator {citation.document_id!r} / "
            f"{citation.version[:12]!r} / {citation.page!r} is not one of "
            "this run's retrieved chunk locators"
        )

    # The path is only "live" if the request actually crossed the wire:
    # the compiled graph's retrieve tool called the real MCP server, not
    # an in-process fallback.
    retrieve_calls = [
        request
        for request in live.session.requests
        if request["params"]["name"] == "retrieve"
    ]
    assert retrieve_calls, "no retrieve tools/call reached the live server"
    assert retrieve_calls[0]["params"]["arguments"]["query"] == (
        _RETRIEVE_QUERY
    )


def test_ungrounded_answer_over_the_same_live_evidence_is_not_done(
    ingested_live: Any,
) -> None:
    """The negative half: evidence present, no span quoted -> refuse.

    Same store, same retrieve, same live session: only the answer
    changes. If this run came back ``done``, the positive test above
    would prove nothing about the verifier — a gate that passes
    everything would satisfy it. The reason is the verifier's own
    ``citation_repair_cap`` (not a retrieve-side name): retrieve did
    return chunks, so the collection was not what failed.
    """

    live, result = ingested_live

    out = _run_question(
        live, result, _UNGROUNDED_QUESTION, "sc8-ungrounded"
    )

    assert out["track"] == "grounded"
    assert out["status"] == "refuse", (
        f"an answer with no this-run span must not be done: "
        f"status={out['status']!r}"
    )
    assert out["status_reason"] == "citation_repair_cap"
    assert list(out["citations"]) == []

    # ...and it refused WITH evidence on hand: the same live retrieve
    # ran and returned chunks, so the refusal is the verifier's, not an
    # empty collection's.
    chunks = _retrieved_payload(out["messages"])
    assert chunks, (
        "the negative run retrieved nothing; the refusal would then be "
        "about the collection, not about the ungrounded answer"
    )
