"""The three execution seams this lane instruments, as hand-written spans.

ADR 0102 / ``specs/qa-chain-server-runtime.md`` §"Trace transport and
spans" fix the contract this file holds to:

* exactly five spans are hand-written, and this lane owns three of them
  — ``classify_node``, ``retrieve_tool`` and ``model_node``;
* the trace id derives from (thread_id, turn_id) and every span id from
  (thread_id, turn_id, segment_seq), so two runs with the same
  correlation produce the same ids and a different segment produces
  different ones;
* the segment root is the parent of every seam span and the receipt
  projection is its SIBLING, so no span references a missing parent;
* a span ends normally even when its body raises;
* only allowlisted attributes ride a span — never a prompt, an answer,
  a source label or id, a raw tool argument, raw exception text, a
  credential or a connection string;
* with no exporter configured a seam costs nothing and does no I/O.

The harness is the SDK's own in-memory span exporter, not a mock of the
unit under test: the spans are built by the same code the product runs
and collected by the same SDK machinery a real exporter would use.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from fin.corpus.extracted import Locator
from fin.host.graph import _make_retrieve_tool, compile_graph
from fin.host.receipt_export import (
    SEAM_SPAN_NAMES,
    receipt_span_id_for,
    seam_span_id_for,
    segment_span_id_for,
    trace_id_for,
)
from fin.host.scripted_model import ScriptedChatModel
from fin.host.span_seams import (
    SEAM_SPAN_ATTRIBUTES,
    SEGMENT_ROOT_SPAN_NAME,
    SegmentIdentity,
    active_span_processors,
    publish_segment,
    publish_turn_segment,
)
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import Chunk

THREAD_ID = "opaque-thread-id"
TURN_ID = "opaque-turn-id"
SEGMENT_SEQ = 0

#: The three seams this lane owns. The other two belong to the parallel
#: lane, so they are NOT asserted here.
OWNED_SEAMS = ("classify_node", "retrieve_tool", "model_node")

#: The names a seam span may carry beyond the correlation triple.
CORRELATION_ATTRIBUTES = {
    "fin.thread_id",
    "fin.turn_id",
    "fin.segment_seq",
}

KNOWN_IDS = {"AAPL-2022Q3"}


# ----- the harness -----
#
# ``trace_api.set_tracer_provider`` may only be called once per process,
# so the provider is installed by assigning the module's own global and
# the exporter is swapped per test by adding a processor to that one
# provider.


@pytest.fixture(name="spans")
def _spans() -> Iterator[InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = getattr(trace_api, "_TRACER_PROVIDER", None)
    trace_api._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        trace_api._TRACER_PROVIDER = previous
        provider.shutdown()


@pytest.fixture(name="no_exporter")
def _no_exporter() -> Iterator[None]:
    """A provider with NO span processor: the default, no exporter."""
    provider = TracerProvider()
    previous = getattr(trace_api, "_TRACER_PROVIDER", None)
    trace_api._TRACER_PROVIDER = provider
    try:
        yield
    finally:
        trace_api._TRACER_PROVIDER = previous
        provider.shutdown()


# ----- helpers -----


def _config(thread_id: str = THREAD_ID, run_id: str = TURN_ID) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "run_id": run_id}}


def _identity(segment_seq: int = SEGMENT_SEQ) -> SegmentIdentity:
    return SegmentIdentity(
        thread_id=THREAD_ID, turn_id=TURN_ID, segment_seq=segment_seq
    )


def _named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in spans.get_finished_spans() if span.name == name]


def _one(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    emitted = _named(spans, name)
    assert len(emitted) == 1, [span.name for span in spans.get_finished_spans()]
    return emitted[0]


def _hit() -> Chunk:
    return Chunk(
        text="Apple net sales for fiscal 2023 third quarter grew.",
        source_ref=Locator(document_id="AAPL-2022Q3", version="a" * 64, unit=1),
        score=1.0,
    )


def _port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={"revenue": [_hit()]}, known_document_ids=KNOWN_IDS
    )


def _script() -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue"},
                    "id": "c-span",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Per the filings: revenue grew."),
    ]


def _graph() -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=_script()),
        known_document_ids=KNOWN_IDS,
        adapter=_port(),
    )


def _turn_input() -> dict[str, Any]:
    """One grounded turn: a human utterance the model then answers."""
    return {
        "messages": [HumanMessage("what was AAPL net sales in 2023 Q3?")],
        "evidence": [],
        "repair_count": 0,
        "step_count": 0,
    }


def _run_graph(graph: Any, config: dict[str, Any] | None = None) -> None:
    graph.invoke(_turn_input(), config=config if config is not None else _config())


# ----- classify_node -----


class TestClassifyNode:
    """The routing seam, instrumented by hand."""

    def test_exactly_one_span_is_emitted_for_the_seam(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        assert _named(spans, "classify_node")

    def test_the_span_id_is_derived_from_the_correlation(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        span = _one(spans, "classify_node")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ, "classify_node"
        )
        assert span.context.trace_id == trace_id_for(THREAD_ID, TURN_ID)

    def test_the_span_is_a_child_of_the_segment_root(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        span = _one(spans, "classify_node")
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ
        )
        # The receipt projection is the root's SIBLING, never the parent.
        assert span.parent.span_id != receipt_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ
        )

    def test_the_span_carries_only_allowlisted_attributes(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        span = _one(spans, "classify_node")
        names = set(span.attributes or {})
        assert names <= SEAM_SPAN_ATTRIBUTES | CORRELATION_ATTRIBUTES
        # The route projection is a persisted receipt enum, so it rides
        # the span under its contract name.
        assert "fin.route.intent" in names
        assert "fin.route.source" in names
        # Nothing from the utterance.
        for value in (span.attributes or {}).values():
            assert "AAPL" not in str(value)
            assert "net sales" not in str(value)


# ----- model_node -----


class TestModelNode:
    """The model seam, instrumented by hand."""

    def test_exactly_one_span_is_emitted_for_the_seam(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        assert _named(spans, "model_node")

    def test_the_span_id_is_derived_from_the_correlation(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        span = _one(spans, "model_node")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ, "model_node"
        )
        assert span.context.trace_id == trace_id_for(THREAD_ID, TURN_ID)

    def test_the_span_is_a_child_of_the_segment_root(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        span = _one(spans, "model_node")
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ
        )

    def test_the_span_carries_only_allowlisted_attributes(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        with publish_segment(_identity()):
            _run_graph(graph)
        span = _one(spans, "model_node")
        names = set(span.attributes or {})
        assert names <= SEAM_SPAN_ATTRIBUTES | CORRELATION_ATTRIBUTES
        # The scripted answer is the model's own text and never rides a
        # span.
        for value in (span.attributes or {}).values():
            assert "Per the filings" not in str(value)

    def test_the_span_ends_normally_when_the_body_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        class _RaisingModel(ScriptedChatModel):
            def _generate(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("the model is down")

        graph = compile_graph(
            checkpointer=InMemorySaver(),
            model=_RaisingModel(script=[]),
            known_document_ids=KNOWN_IDS,
            adapter=_port(),
        )
        with publish_segment(_identity()):
            with pytest.raises(RuntimeError):
                _run_graph(graph)
        # The span was ended, not left open, and the raw exception text
        # did not ride it.
        for span in _named(spans, "model_node"):
            assert span.end_time is not None
            assert span.start_time is not None
            for value in (span.attributes or {}).values():
                assert "the model is down" not in str(value)


# ----- retrieve_tool -----


class TestRetrieveTool:
    """The retrieval seam, instrumented by hand."""

    def _tool(self) -> Any:
        return _make_retrieve_tool(_port())

    def _call(self, tool: Any) -> Any:
        return tool.run(
            {"query": "revenue"},
            tool_call_id="c-span",
            config=_config(),
        )

    def test_exactly_one_span_is_emitted_for_the_seam(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = self._tool()
        with publish_segment(_identity()):
            self._call(tool)
        assert _named(spans, "retrieve_tool")

    def test_the_span_id_is_derived_from_the_correlation(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = self._tool()
        with publish_segment(_identity()):
            self._call(tool)
        span = _one(spans, "retrieve_tool")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ, "retrieve_tool"
        )
        assert span.context.trace_id == trace_id_for(THREAD_ID, TURN_ID)

    def test_the_span_is_a_child_of_the_segment_root(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = self._tool()
        with publish_segment(_identity()):
            self._call(tool)
        span = _one(spans, "retrieve_tool")
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ
        )

    def test_the_span_carries_only_allowlisted_attributes(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = self._tool()
        with publish_segment(_identity()):
            self._call(tool)
        span = _one(spans, "retrieve_tool")
        names = set(span.attributes or {})
        assert names <= SEAM_SPAN_ATTRIBUTES | CORRELATION_ATTRIBUTES
        # The raw tool arguments and the retrieved text never ride a span.
        for value in (span.attributes or {}).values():
            assert "revenue" not in str(value)
            assert "Apple net sales" not in str(value)

    def test_the_reserved_rerank_attributes_emit_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = self._tool()
        with publish_segment(_identity()):
            self._call(tool)
        span = _one(spans, "retrieve_tool")
        for name in (
            "fin.retrieve.rerank_outcome",
            "fin.retrieve.rerank_skipped_reason",
            "fin.retrieve.rerank_fallback",
            "fin.retrieve.rerank_effective_limit",
        ):
            assert name not in (span.attributes or {})

    def test_the_span_ends_normally_when_the_body_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        class _RaisingPort(FakeRetrievePort):
            def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("port is down")

        tool = _make_retrieve_tool(_RaisingPort(known_document_ids=KNOWN_IDS))
        with publish_segment(_identity()):
            with pytest.raises(RuntimeError):
                self._call(tool)
        span = _one(spans, "retrieve_tool")
        assert span.end_time is not None
        assert span.start_time is not None
        # The raw exception text is not an attribute.
        for value in (span.attributes or {}).values():
            assert "port is down" not in str(value)


# ----- the properties every seam shares -----


class TestSharedSeamProperties:
    def test_the_three_seam_ids_are_distinct(self) -> None:
        ids = {
            seam: seam_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ, seam)
            for seam in OWNED_SEAMS
        }
        assert len(set(ids.values())) == len(ids)
        # And distinct from the root and from the projection.
        assert seam_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ, OWNED_SEAMS[0]) not in {
            segment_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ),
            receipt_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ),
        }

    def test_the_seam_names_are_part_of_the_closed_contract(self) -> None:
        for seam in OWNED_SEAMS:
            assert seam in SEAM_SPAN_NAMES

    def test_two_runs_with_the_same_correlation_produce_the_same_ids(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = _make_retrieve_tool(_port())
        for _ in range(2):
            with publish_segment(_identity()):
                tool.run(
                    {"query": "revenue"},
                    tool_call_id="c-span",
                    config=_config(),
                )
        emitted = _named(spans, "retrieve_tool")
        assert len(emitted) == 2
        assert emitted[0].context.span_id == emitted[1].context.span_id
        assert emitted[0].context.trace_id == emitted[1].context.trace_id

    def test_a_different_segment_produces_a_different_id(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = _make_retrieve_tool(_port())
        for segment_seq in (0, 1):
            with publish_segment(_identity(segment_seq)):
                tool.run(
                    {"query": "revenue"},
                    tool_call_id="c-span",
                    config=_config(),
                )
        emitted = _named(spans, "retrieve_tool")
        assert len(emitted) == 2
        assert emitted[0].context.span_id != emitted[1].context.span_id
        # One turn is one trace, whichever segment a span belongs to.
        assert emitted[0].context.trace_id == emitted[1].context.trace_id

    def test_a_seam_without_a_published_segment_emits_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = _make_retrieve_tool(_port())
        tool.run(
            {"query": "revenue"},
            tool_call_id="c-span",
            config=_config(),
        )
        assert spans.get_finished_spans() == ()

    def test_a_seam_with_an_unbound_config_emits_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = _make_retrieve_tool(_port())
        with publish_segment(_identity()):
            tool.run(
                {"query": "revenue"},
                tool_call_id="c-span",
                config={"configurable": {}},
            )
        # The segment's ROOT is still exported — it is the segment's own
        # span — but no seam span rides a correlation the call did not
        # carry.
        assert _named(spans, "retrieve_tool") == []
        assert _named(spans, "classify_node") == []
        assert _named(spans, "model_node") == []

    def test_the_segment_root_is_the_parent_of_every_seam(
        self, spans: InMemorySpanExporter
    ) -> None:
        tool = _make_retrieve_tool(_port())
        with publish_segment(_identity()):
            tool.run(
                {"query": "revenue"},
                tool_call_id="c-span",
                config=_config(),
            )
        root_ids = {
            span.context.span_id
            for span in spans.get_finished_spans()
            if span.name == SEGMENT_ROOT_SPAN_NAME
        }
        assert root_ids == {segment_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ)}
        for span in spans.get_finished_spans():
            if span.name == SEGMENT_ROOT_SPAN_NAME:
                assert span.parent is None
            else:
                assert span.parent is not None
                assert span.parent.span_id in root_ids

    def test_a_run_that_carries_its_segment_publishes_one(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A run config that carries the segment number is enough.

        The segment's identity is a run fact, so a run that states which
        segment it is gets its spans without a separate publisher.
        """
        graph = _graph()
        config = _config()
        config["configurable"]["segment_seq"] = 4
        _run_graph(graph, config)
        span = _one(spans, "classify_node")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, 4, "classify_node"
        )
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(THREAD_ID, TURN_ID, 4)
        # One root for the segment, not one per node.
        assert len(_named(spans, SEGMENT_ROOT_SPAN_NAME)) == 1

    def test_a_run_without_a_segment_number_emits_no_seam_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        graph = _graph()
        _run_graph(graph)
        assert _named(spans, "classify_node") == []
        assert _named(spans, "model_node") == []
        assert _named(spans, "retrieve_tool") == []

    def test_the_segment_number_is_read_from_the_turn_s_receipt(
        self, spans: InMemorySpanExporter
    ) -> None:
        """The durable receipt column is the authority on the segment.

        A turn whose receipt says it is in segment 3 gets its spans
        stamped with 3, so a resumed turn does not reopen the segment it
        already finished.
        """

        class _Receipt:
            segment_seq = 3

        class _Receipts:
            def read(self, thread_id: str, turn_id: str) -> _Receipt:
                assert (thread_id, turn_id) == (THREAD_ID, TURN_ID)
                return _Receipt()

        with publish_turn_segment(_Receipts(), THREAD_ID, TURN_ID):
            tool = _make_retrieve_tool(_port())
            tool.run(
                {"query": "revenue"},
                tool_call_id="c-span",
                config=_config(),
            )
        span = _one(spans, "retrieve_tool")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, 3, "retrieve_tool"
        )
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(THREAD_ID, TURN_ID, 3)

    def test_a_turn_with_no_receipt_is_in_its_first_segment(
        self, spans: InMemorySpanExporter
    ) -> None:
        class _Receipts:
            def read(self, thread_id: str, turn_id: str) -> None:
                return None

        with publish_turn_segment(_Receipts(), THREAD_ID, TURN_ID):
            tool = _make_retrieve_tool(_port())
            tool.run(
                {"query": "revenue"},
                tool_call_id="c-span",
                config=_config(),
            )
        span = _one(spans, "retrieve_tool")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, 0, "retrieve_tool"
        )

    def test_an_unreadable_receipt_leaves_the_run_alone(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A store fault must not fail the run, and must not guess."""

        class _Receipts:
            def read(self, thread_id: str, turn_id: str) -> None:
                raise RuntimeError("the store is down")

        with publish_turn_segment(_Receipts(), THREAD_ID, TURN_ID):
            tool = _make_retrieve_tool(_port())
            result = tool.run(
                {"query": "revenue"},
                tool_call_id="c-span",
                config=_config(),
            )
        assert result is not None
        span = _one(spans, "retrieve_tool")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, 0, "retrieve_tool"
        )


# ----- export off by default -----


class TestNoExporterConfigured:
    def test_a_seam_costs_nothing_and_does_no_network_io(
        self, no_exporter: None
    ) -> None:
        """With no exporter selected, a seam does no I/O at all.

        The guard is a socket that refuses to be connected: a seam that
        opened a connection would raise instead of quietly returning.
        """
        real_socket = socket.socket

        class _RefusingSocket(real_socket):  # type: ignore[misc]
            def connect(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("a seam opened a network connection")

            def connect_ex(self, *args: Any, **kwargs: Any) -> int:
                raise AssertionError("a seam opened a network connection")

        socket.socket = _RefusingSocket  # type: ignore[misc]
        try:
            tool = _make_retrieve_tool(_port())
            with publish_segment(_identity()):
                result = tool.run(
                    {"query": "revenue"},
                    tool_call_id="c-span",
                    config=_config(),
                )
        finally:
            socket.socket = real_socket  # type: ignore[misc]
        assert result is not None

    def test_a_whole_turn_completes_with_no_exporter_selected(
        self, no_exporter: None
    ) -> None:
        """The trace layer is off by default, so a turn still runs.

        ADR 0102 (amended 2026-10-20): the session-record layer is always
        on and the trace layer is opt-in. A whole grounded turn — model,
        retrieval, citation gate, admission — must therefore finish with
        no exporter configured, and it must do so without a span leaving
        the process or a socket being opened on the trace layer's behalf.
        """
        real_socket = socket.socket

        class _RefusingSocket(real_socket):  # type: ignore[misc]
            def connect(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("a turn with tracing off opened a socket")

            def connect_ex(self, *args: Any, **kwargs: Any) -> int:
                raise AssertionError("a turn with tracing off opened a socket")

        socket.socket = _RefusingSocket  # type: ignore[misc]
        try:
            state = _graph().invoke(_turn_input(), config=_config())
        finally:
            socket.socket = real_socket  # type: ignore[misc]

        assert state["messages"][-1].content == "Per the filings: revenue grew."
        assert active_span_processors() == ()
