"""The two decision seams this lane instruments, as hand-written spans.

ADR 0102 / ``specs/qa-chain-server-runtime.md`` §"Trace transport and
spans" fix the contract this file holds to, for the ``citation_gate_node``
and ``admit_decision`` seams (the other three are held in
``test_span_seams.py``):

* exactly one span per seam per segment, its id derived from
  (thread_id, turn_id, segment_seq) and the seam's own name, distinct
  from every other seam, the segment root and the receipt projection;
* the segment root is the parent, so no span references a missing
  parent;
* a span ends normally even when the seam body raises;
* only allowlisted attributes ride a span — and on ``admit_decision``
  the answer text and the evidence text (the two payloads this seam
  holds) never appear in any attribute value;
* with no exporter configured a seam costs nothing and does no I/O.

The harness is the SDK's own in-memory span exporter, not a mock: the
spans are built by the same code the product runs and collected by the
same SDK machinery a real exporter would use.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from fin.corpus.extracted import Locator
from fin.host import EvidenceItem, ScriptedChatModel, compile_graph
from fin.host.grounded.citation_gate import Citation, DoneResult
from fin.host.grounded.grounded_decision import (
    CANDIDATE_TERMINATOR,
    GroundedDecision,
    admit_decision,
    parse_decision_output,
)
from fin.host.receipt_export import (
    SEAM_SPAN_NAMES,
    receipt_span_id_for,
    seam_span_id_for,
    segment_span_id_for,
    trace_id_for,
)
from fin.host.span_seams import (
    SEAM_SPAN_ATTRIBUTES,
    SEGMENT_ROOT_SPAN_NAME,
    SegmentIdentity,
    publish_segment,
)
from fin.host.state import reset_input_state

THREAD_ID = "opaque-thread-id"
TURN_ID = "opaque-turn-id"
SEGMENT_SEQ = 0

#: The two seams this lane owns. The other three belong to the parallel
#: lane, so they are NOT asserted here.
OWNED_SEAMS = ("citation_gate_node", "admit_decision")

#: The names a seam span may carry beyond the correlation triple.
CORRELATION_ATTRIBUTES = {
    "fin.thread_id",
    "fin.turn_id",
    "fin.segment_seq",
}

_SHA = "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"


# ----- the harness -----


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


def _identity(segment_seq: int = SEGMENT_SEQ) -> SegmentIdentity:
    return SegmentIdentity(
        thread_id=THREAD_ID, turn_id=TURN_ID, segment_seq=segment_seq
    )


def _graph_config() -> dict[str, Any]:
    return {"configurable": {"thread_id": THREAD_ID, "run_id": TURN_ID}}


def _named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in spans.get_finished_spans() if span.name == name]


def _one(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    emitted = _named(spans, name)
    assert len(emitted) == 1, [span.name for span in spans.get_finished_spans()]
    return emitted[0]


def _citation() -> Citation:
    return Citation(
        kind="company",
        document_id="MSFT-2023Q1",
        attachment_id="",
        version=_SHA,
        unit=1,
        display="page",
        filename="",
        quote="quoted span",
    )


def _evidence(unit_text: str) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            locator=Locator(document_id="MSFT-2023Q1", version="e" * 64, unit=1),
            unit_text=unit_text,
        )
    ]


def _verifier(answer_echo: str | None = None, refuse: bool = False) -> Any:
    """The injected R3 verifier: done on the text it was given."""
    if refuse:
        return lambda answer: __import__(
            "fin.host.grounded.citation_gate", fromlist=["RefuseResult"]
        ).RefuseResult(outcome="refuse", reason="unknown_locator")
    return lambda answer: DoneResult(
        outcome="done",
        citations=(_citation(),),
        answer=answer_echo if answer_echo is not None else answer,
    )


def _admit(
    text: str,
    *,
    evidence: list[EvidenceItem] | None = None,
    verify: Any | None = None,
    retrieval_attempted: bool = True,
) -> GroundedDecision:
    return admit_decision(
        answer_text=text,
        parsed=parse_decision_output(text),
        evidence=tuple(evidence or ()),
        retrieval_attempted=retrieval_attempted,
        verify=verify if verify is not None else _verifier(),
    )


# ----- the graph (citation_gate_node) -----


EVIDENCE_TEXT = (
    "Microsoft Corporation reported total revenue of 52,857 million dollars."
)
QUOTED_ANSWER = f"Per the filing: {EVIDENCE_TEXT[:40]}"

GROUNDED_QUESTION = "What was AAPL net sales in 2023 Q3?"


def _gate_graph() -> Any:
    """A grounded graph whose single model hop clears the citation gate."""
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content=QUOTED_ANSWER)]),
        known_document_ids={"MSFT-2023Q1"},
    )


def _gate_input() -> dict[str, Any]:
    """One grounded turn seeded with the evidence its answer quotes."""
    state = reset_input_state(GROUNDED_QUESTION)
    state["evidence"] = _evidence(EVIDENCE_TEXT)
    return state


# ----- citation_gate_node -----


class TestCitationGateNode:
    """The citation gate seam, instrumented by hand."""

    def test_exactly_one_span_is_emitted_for_the_seam(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _gate_graph().invoke(_gate_input(), config=_graph_config())
        assert len(_named(spans, "citation_gate_node")) == 1

    def test_the_span_id_is_derived_from_the_correlation(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _gate_graph().invoke(_gate_input(), config=_graph_config())
        span = _one(spans, "citation_gate_node")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ, "citation_gate_node"
        )
        assert span.context.trace_id == trace_id_for(THREAD_ID, TURN_ID)

    def test_the_span_is_a_child_of_the_segment_root(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _gate_graph().invoke(_gate_input(), config=_graph_config())
        span = _one(spans, "citation_gate_node")
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ
        )

    def test_a_different_segment_produces_a_different_id(
        self, spans: InMemorySpanExporter
    ) -> None:
        for segment_seq in (0, 1):
            with publish_segment(_identity(segment_seq)):
                _gate_graph().invoke(_gate_input(), config=_graph_config())
        emitted = _named(spans, "citation_gate_node")
        assert len(emitted) == 2
        assert emitted[0].context.span_id != emitted[1].context.span_id
        # One turn is one trace, whichever segment a span belongs to.
        assert emitted[0].context.trace_id == emitted[1].context.trace_id

    def test_the_span_is_distinct_from_the_other_seams_root_and_projection(
        self,
    ) -> None:
        ids = {
            seam: seam_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ, seam)
            for seam in SEAM_SPAN_NAMES
        }
        assert len(set(ids.values())) == len(ids)
        gate = ids["citation_gate_node"]
        assert gate not in {
            segment_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ),
            receipt_span_id_for(THREAD_ID, TURN_ID, SEGMENT_SEQ),
        }

    def test_the_span_ends_normally_when_the_body_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A verifier fault is the run's failure, never a missing span.

        The gate is the run's SECOND verifier call on this route (the
        admission's one-call budget is the first), so the fault is
        armed for that call only and the raise surfaces from the gate
        node itself. The span still lands with the segment, and no raw
        exception text rides it.
        """
        import fin.host.graph as graph_module
        import fin.host.grounded.nodes as gate_module

        real_verify = graph_module.verify_citations
        calls = {"n": 0}

        def _raise_on_gate(**kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("verifier is down")
            return real_verify(**kwargs)

        with pytest.MonkeyPatch.context() as patch:
            # The admission calls the verifier from the composition root
            # and the gate calls it from the capability that owns it;
            # both call sites must see the armed fault.
            patch.setattr(graph_module, "verify_citations", _raise_on_gate)
            patch.setattr(gate_module, "verify_citations", _raise_on_gate)
            with publish_segment(_identity()):
                with pytest.raises(RuntimeError):
                    _gate_graph().invoke(_gate_input(), config=_graph_config())
        assert calls["n"] >= 2
        span = _one(spans, "citation_gate_node")
        assert span.start_time is not None
        assert span.end_time is not None
        for value in (span.attributes or {}).values():
            assert "verifier is down" not in str(value)

    def test_the_span_carries_only_allowlisted_attributes(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _gate_graph().invoke(_gate_input(), config=_graph_config())
        span = _one(spans, "citation_gate_node")
        names = set(span.attributes or {})
        assert names <= SEAM_SPAN_ATTRIBUTES | CORRELATION_ATTRIBUTES
        # The answer text and the evidence text never ride a span.
        for value in (span.attributes or {}).values():
            assert QUOTED_ANSWER[:40] not in str(value)
            assert "52,857" not in str(value)


# ----- admit_decision -----


class TestAdmitDecision:
    """The admission seam, instrumented by hand.

    ``admit_decision`` receives no run config: its correlation comes
    from the ambient segment the run's owner published, which is why
    these tests call it inside ``publish_segment`` directly.
    """

    def test_exactly_one_span_is_emitted_for_the_seam(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _admit("公司本季度的收入主要来自硬件销售业务。")
        assert len(_named(spans, "admit_decision")) == 1

    def test_the_span_id_is_derived_from_the_correlation(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _admit("公司本季度的收入主要来自硬件销售业务。")
        span = _one(spans, "admit_decision")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ, "admit_decision"
        )
        assert span.context.trace_id == trace_id_for(THREAD_ID, TURN_ID)

    def test_the_span_is_a_child_of_the_segment_root(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _admit("公司本季度的收入主要来自硬件销售业务。")
        span = _one(spans, "admit_decision")
        assert span.parent is not None
        assert span.parent.span_id == segment_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ
        )

    def test_a_different_segment_produces_a_different_id(
        self, spans: InMemorySpanExporter
    ) -> None:
        for segment_seq in (0, 1):
            with publish_segment(_identity(segment_seq)):
                _admit("公司本季度的收入主要来自硬件销售业务。")
        emitted = _named(spans, "admit_decision")
        assert len(emitted) == 2
        assert emitted[0].context.span_id != emitted[1].context.span_id
        assert emitted[0].context.trace_id == emitted[1].context.trace_id

    def test_a_call_outside_any_segment_emits_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        _admit("公司本季度的收入主要来自硬件销售业务。")
        assert spans.get_finished_spans() == ()

    def test_the_choice_branch_lands_on_the_same_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        """Every branch of the seam is the one span of this segment.

        The choice branch carries the answer text and the evidence text
        in its arguments, so it is the branch where a leak would hide.
        """
        evidence = _evidence("第一季度的经营说明在这里。")
        text = (
            "这个问题有两种理解。"
            f"{CANDIDATE_TERMINATOR}\n"
            '{{"candidates": [{"document_id": "MSFT-2023Q1"}]}}'
        )
        with publish_segment(_identity()):
            _admit(text, evidence=evidence)
        span = _one(spans, "admit_decision")
        assert span.context.span_id == seam_span_id_for(
            THREAD_ID, TURN_ID, SEGMENT_SEQ, "admit_decision"
        )

    def test_the_span_ends_normally_when_the_body_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A verifier fault is the run's failure, never a missing span."""

        def _raising_verify(_answer: str) -> Any:
            raise RuntimeError("verifier is down")

        with publish_segment(_identity()):
            with pytest.raises(RuntimeError):
                _admit(
                    "公司本季度的收入主要来自硬件销售业务。",
                    verify=_raising_verify,
                )
        span = _one(spans, "admit_decision")
        assert span.start_time is not None
        assert span.end_time is not None
        for value in (span.attributes or {}).values():
            assert "verifier is down" not in str(value)

    def test_the_answer_text_and_evidence_content_never_ride_the_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        """The redaction boundary: this seam HOLDS the answer text and
        the evidence set. Their content is user and source text, so no
        attribute name and no attribute VALUE may carry it — on any
        branch, including the fallback whose answer bytes survive
        admission and the refusal that holds no answer at all.

        Checked over the serialized attribute set, not just the names:
        a value could survive a name-set assertion and still leak.
        """
        answer = "公司本季度的收入主要来自硬件销售业务。"
        evidence_text = "第 1 页的正文内容足够长以便形成一句完整的描述。"
        branches: list[tuple[str, str, list[EvidenceItem], Any]] = [
            ("merge", answer, [], _verifier()),
            (
                "fallback_merge",
                answer + "\n" + CANDIDATE_TERMINATOR + "not json at all",
                [],
                _verifier(),
            ),
            (
                "fallback_refuse",
                "无法核实的一句话。" + CANDIDATE_TERMINATOR + "{{{bad",
                [],
                _verifier(refuse=True),
            ),
            ("choice", "这个问题有两种理解。", [], None),
        ]
        # The choice branch's own text is built from the same pieces so
        # one sweep covers every branch's arguments and result.
        choice_text = (
            "这个问题有两种理解。"
            f"{CANDIDATE_TERMINATOR}\n"
            '{{"candidates": [{"document_id": "MSFT-2023Q1"}]}}'
        )
        branches[3] = (
            "choice",
            choice_text,
            _evidence(evidence_text),
            None,
        )

        for _name, text, evidence, verify in branches:
            with publish_segment(_identity()):
                _admit(text, evidence=evidence, verify=verify)
        # ONE span per seam per segment: the four branches above are four
        # segments, so four spans accumulate and EVERY one is swept.
        emitted = _named(spans, "admit_decision")
        assert len(emitted) == len(branches)
        for span in emitted:
            for value in (span.attributes or {}).values():
                rendered = str(value)
                assert answer not in rendered
                assert evidence_text not in rendered
                assert "无法核实的一句话" not in rendered
                assert "这个问题有两种理解" not in rendered
                assert "MSFT-2023Q1" not in rendered
                assert "not json at all" not in rendered

    def test_the_span_carries_only_allowlisted_attributes(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _admit("公司本季度的收入主要来自硬件销售业务。")
        span = _one(spans, "admit_decision")
        names = set(span.attributes or {})
        assert names <= SEAM_SPAN_ATTRIBUTES | CORRELATION_ATTRIBUTES


# ----- the properties every seam shares -----


class TestSharedSeamProperties:
    def test_the_seam_names_are_part_of_the_closed_contract(self) -> None:
        for seam in OWNED_SEAMS:
            assert seam in SEAM_SPAN_NAMES

    def test_a_seam_without_a_published_segment_emits_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        """No ambient segment, no span: a guessed segment number would
        stamp a span into the wrong segment forever, which is worse
        than absent evidence."""
        _gate_graph().invoke(_gate_input(), config=_graph_config())
        _admit("公司本季度的收入主要来自硬件销售业务。")
        assert _named(spans, "citation_gate_node") == []
        assert _named(spans, "admit_decision") == []

    def test_a_seam_outside_its_own_segment_emits_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A graph call whose config names a DIFFERENT turn must not
        stamp its seam span into the published segment's identity."""
        other = SegmentIdentity(thread_id="other-thread", turn_id="other-turn")
        with publish_segment(other):
            _gate_graph().invoke(_gate_input(), config=_graph_config())
        assert _named(spans, "citation_gate_node") == []

    def test_admit_trusts_the_published_segment_identity(
        self, spans: InMemorySpanExporter
    ) -> None:
        """``admit_decision`` reads only the ambient segment, so the
        only mismatch it can hit is a segment published by another
        owner mid-call — the seam records into the scope it matches,
        which is the published one's identity, and nothing here."""
        with publish_segment(
            SegmentIdentity(thread_id=THREAD_ID, turn_id="other-turn")
        ):
            _admit("公司本季度的收入主要来自硬件销售业务。")
        # The seam span is recorded under the scope it matched — the
        # published segment — because the ambient identity IS the
        # correlation this seam was given. That is the contract: the
        # publisher owns the identity, the seam trusts it.
        assert _one(spans, "admit_decision").context.span_id == seam_span_id_for(
            THREAD_ID, "other-turn", 0, "admit_decision"
        )

    def test_the_reserved_rerank_attributes_emit_nothing(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _admit("公司本季度的收入主要来自硬件销售业务。")
            _gate_graph().invoke(_gate_input(), config=_graph_config())
        for seam in OWNED_SEAMS:
            span = _one(spans, seam)
            for name in (
                "fin.retrieve.rerank_outcome",
                "fin.retrieve.rerank_skipped_reason",
                "fin.retrieve.rerank_fallback",
                "fin.retrieve.rerank_effective_limit",
            ):
                assert name not in (span.attributes or {})

    def test_no_seam_span_references_a_missing_parent(
        self, spans: InMemorySpanExporter
    ) -> None:
        with publish_segment(_identity()):
            _gate_graph().invoke(_gate_input(), config=_graph_config())
            _admit("公司本季度的收入主要来自硬件销售业务。")
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
            with publish_segment(_identity()):
                _gate_graph().invoke(_gate_input(), config=_graph_config())
                _admit("公司本季度的收入主要来自硬件销售业务。")
        finally:
            socket.socket = real_socket  # type: ignore[misc]
