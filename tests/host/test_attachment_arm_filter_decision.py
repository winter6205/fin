"""The attachment arm under a generic metadata filter.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Trusted source scope and
retrieval arms": 「一个 attachment arm must apply the same metadata
predicates when it has equivalent metadata; if it cannot, it must fail
or be excluded with truthful degradation rather than return unfiltered
attachment evidence.」

fin's attachment rows carry no equivalent metadata: an
``AttachmentRecord`` holds ``filename`` and lifecycle fields only — no
``title`` / ``doc_type`` / ``company`` / ``period`` — and
``AttachmentSearchPort.search`` takes no metadata argument to bind one.
So the arm CANNOT apply the predicates, and the only contract-preserving
options are EXCLUDE WITH TRUTHFUL DEGRADATION or FAIL.

This module pins the exclude choice: the branch is left out, it searches
nothing, it publishes no attachment chunks and no attachment evidence,
and the envelope carries ``degraded: true`` with a named reason so the
partial result reads as incomplete rather than complete. It also pins
that the company branch still answers the filtered question, that an
absent filter leaves the arm exactly as it was, and that a company leg
which is ITSELF degraded keeps its own reasons beside the exclusion's —
two partial results are one degraded result naming both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import (
    ATTACHMENT_ARM_FILTER_EXCLUDED_REASON as EXCLUSION_REASON,
    MERGED_REASONS_OVERFLOW_REASON,
    _merged_degradation_reasons,
)
from fin.host.state import reset_input_state
from fin.retrieve import Chunk
from fin.retrieve.degradation import (
    DEGRADATION_STAGE_RETRIEVE,
    DEGRADED_FIELD,
    DEGRADED_REASONS_FIELD,
    DegradationLedger,
    read_degradation_signal,
    unsignalled_degradation_count,
)
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.source_read import ATTACHMENT_KIND


from test_graph_attachment_retrieve import (  # noqa: E402
    _THREAD_A,
    _THREAD_B,
    COMPANY_TEXT,
    FakeAttachmentSearchPort,
    _attachment_chunk,
)

FILTERED = "What was AAPL net sales in 2023 Q3?"
KNOWN_IDS = {"AAPL-2023Q3"}
ATTACHMENT_TEXT = "The attached term sheet lists a purchase price of 12,000 thousand."

# The generic filter this arm cannot honor: all four keys are in
# contract and every one of them is absent from an attachment row.
GENERIC_FILTER = {"doc_type": "10-Q", "company": "Apple"}


def _company_chunk() -> Chunk:
    return Chunk(
        text=COMPANY_TEXT,
        source_ref=Locator(document_id="AAPL-2023Q3", version="d" * 64, unit=3),
        score=1.5,
    )


def _script(args: dict[str, Any]) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": args,
                    "id": "c-retrieve",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Answer."),
    ]


def _compile(
    retriever: Any,
    *,
    ledger: DegradationLedger,
    hits_by_query: dict[str, list[Chunk]] | None = None,
    port: Any = None,
) -> Any:
    port = port if port is not None else FakeRetrievePort(
        hits_by_query=hits_by_query
        if hits_by_query is not None
        else {FILTERED: [_company_chunk()]},
        known_document_ids=set(KNOWN_IDS),
    )
    model = ScriptedChatModel(
        script=_script({"query": FILTERED, "filters": dict(GENERIC_FILTER)})
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever,
        degradation_ledger=ledger,
    )


def _envelope(out: dict[str, Any]) -> dict[str, Any]:
    messages = [
        m
        for m in out["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "retrieve"
    ]
    assert len(messages) == 1, f"expected one retrieve ToolMessage: {messages}"
    return json.loads(str(messages[0].content))


def _run(graph: Any) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(FILTERED),
        config={"configurable": {"thread_id": _THREAD_A}},
    )


# ----- the excluded arm searches nothing -----


def test_a_filtered_call_leaves_the_attachment_arm_out_entirely() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    _run(graph)

    assert retriever.calls == [], (
        "an arm that cannot apply the predicates must not search and "
        "return the result as if it had"
    )


def test_a_filtered_call_publishes_no_attachment_chunks() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    envelope = _envelope(_run(graph))

    assert "attachment_chunks" not in envelope


def test_a_filtered_call_earns_no_attachment_evidence() -> None:
    """No ATTACHMENT evidence may ground a citation; the filtered company
    hits keep their own."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    out = _run(graph)

    attachment_items = [
        item
        for item in out["evidence"]
        if getattr(item.locator, "kind", "") == ATTACHMENT_KIND
    ]
    assert attachment_items == [], "no attachment text may ground a citation"


# ----- the exclusion is reported truthfully -----


def test_a_filtered_call_reports_degradation_with_a_named_reason() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    envelope = _envelope(_run(graph))

    assert envelope["degraded"] is True
    reasons = envelope["degraded_reasons"]
    assert reasons, "an excluded arm must name why"
    for reason in reasons:
        assert isinstance(reason, str) and reason.strip()


def test_the_degradation_reason_names_the_excluded_attachment_arm() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    envelope = _envelope(_run(graph))

    joined = " ".join(envelope["degraded_reasons"])
    assert "attachment" in joined


def test_the_exclusion_is_recorded_on_the_ledger_verbatim() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    ledger = DegradationLedger()
    graph = _compile(retriever, ledger=ledger)

    _run(graph)

    assert ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE)
    assert unsignalled_degradation_count(ledger) == 0


def test_the_user_facing_copy_names_no_reason() -> None:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    envelope = _envelope(_run(graph))

    notice = envelope.get("degradation_notice", "")
    assert notice
    for reason in envelope["degraded_reasons"]:
        assert reason not in notice


# ----- both arms degraded: the reasons must ACCUMULATE -----

COMPANY_REASON = "company_arm:sql_unavailable"


class _CompanyDegradedPort(FakeRetrievePort):
    """A port whose answer is BOTH usable hits and degraded.

    §B2 第 1 条: a degraded response with results travels with its flag,
    so the company leg can leave the attachment arm out AND report its
    own backend loss in the same envelope. That is the shape the merge
    has to get right, and it is why an either/or assertion cannot see
    the defect.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.degraded_reasons = [COMPANY_REASON]

    def _ordinary_hits(
        self,
        query: str,
        filters: Any,
        limit: int,
    ) -> dict[str, Any]:
        answer = super()._ordinary_hits(query, filters, limit)
        if "error" in answer:
            return answer
        answer["degraded"] = True
        answer["degraded_reasons"] = list(self.degraded_reasons)
        return answer


def _both_arms_graph() -> Any:
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    return _compile(retriever, ledger=DegradationLedger(), port=_CompanyDegradedPort(
        hits_by_query={FILTERED: [_company_chunk()]},
        known_document_ids=set(KNOWN_IDS),
    ))


def test_both_arms_degraded_names_both_reasons() -> None:
    """Two partial results are ONE degraded result naming both causes.

    The company leg lost an arm of its own (``company_arm:...``) and the
    attachment leg was excluded for want of equivalent metadata. Both
    facts are true, both are reported, and neither may erase the other:
    a result that names only the exclusion reads as complete with respect
    to the corpus when it is not. This is the case an
    ``in`` -on-one-side assertion cannot catch — it passed while the
    merge replaced the company reason wholesale.
    """
    envelope = _envelope(_run(_both_arms_graph()))

    reasons = envelope["degraded_reasons"]
    assert COMPANY_REASON in reasons, (
        "the company leg's own degradation reason was erased by the "
        f"excluded-arm merge: {reasons}"
    )
    assert any("attachment" in reason for reason in reasons), (
        f"the excluded attachment arm stopped being reported: {reasons}"
    )


def test_the_merged_reasons_keep_the_company_order_and_add_the_exclusion() -> None:
    """Union, not replacement: the company's reasons come first and the
    exclusion is appended, so the list reads as the legs ran."""
    envelope = _envelope(_run(_both_arms_graph()))

    reasons = envelope["degraded_reasons"]
    assert reasons[0] == COMPANY_REASON, (
        f"the company's own reason must stay at the head of the list: {reasons}"
    )
    assert len(reasons) == 2, (
        f"two causes, two reasons — no duplicate and no dropped entry: {reasons}"
    )


def test_the_merge_does_not_exceed_the_envelope_reason_bound() -> None:
    """The union must stay inside §B3's one-to-sixteen bound, or the
    envelope it writes stops being readable as a degradation signal."""
    envelope = _envelope(_run(_both_arms_graph()))

    reasons = envelope["degraded_reasons"]
    assert 1 <= len(reasons) <= 16, (
        f"a merged envelope must stay within the wire bound: {reasons}"
    )
    # And the host's own strict reader must still accept what it wrote —
    # the merge cannot produce a shape ``read_degradation_signal`` refuses.
    assert read_degradation_signal(envelope).degraded is True


def test_a_full_company_reason_list_is_summarized_not_truncated() -> None:
    """§B3's bound is sixteen, and the host is a producer as well as a
    reader: writing seventeen would be an envelope its OWN strict reader
    rejects as malformed. The arithmetic says the excluded arm's single
    reason can tip a full list over exactly that edge, so the overflow is
    pinned here directly — a named summary replaces the seventeenth
    entry, never a silent clip, because a clipped report is
    indistinguishable from a complete one at the reading end.
    """
    full = tuple(f"company_arm:{index}" for index in range(16))

    merged = _merged_degradation_reasons(full, (EXCLUSION_REASON,))

    assert len(merged) == 16
    assert merged[-1] == MERGED_REASONS_OVERFLOW_REASON
    assert read_degradation_signal(
        {DEGRADED_FIELD: True, DEGRADED_REASONS_FIELD: list(merged)}
    ).degraded_reasons == merged


def test_a_merge_below_the_bound_is_a_plain_union() -> None:
    """The bound guard is not a general-purpose cap: an ordinary merge
    passes through with every reason, in order, deduplicated."""
    merged = _merged_degradation_reasons(
        ("company_arm:a", "company_arm:b"), (EXCLUSION_REASON, "company_arm:a")
    )

    assert merged == ("company_arm:a", "company_arm:b", EXCLUSION_REASON)


# ----- the company branch still answers the filtered question -----


def test_the_filtered_company_hits_still_answer() -> None:
    """The excluded arm narrows the ATTACHMENT branch only; the company
    leg owns the filter and keeps answering."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    graph = _compile(retriever, ledger=DegradationLedger())

    envelope = _envelope(_run(graph))

    assert envelope["chunks"], "the filtered company branch must still answer"
    assert envelope["chunks"][0]["text"] == COMPANY_TEXT


def test_the_filter_reaches_the_port_verbatim() -> None:
    """The filter is the port seam's contract: excluding the attachment
    arm never drops the filter the company leg applied."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    port = FakeRetrievePort(
        hits_by_query={FILTERED: [_company_chunk()]},
        known_document_ids=set(KNOWN_IDS),
    )
    model = ScriptedChatModel(
        script=_script({"query": FILTERED, "filters": dict(GENERIC_FILTER)})
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever,
        degradation_ledger=DegradationLedger(),
    )

    _run(graph)

    assert port.calls, "the port must have been called"
    # The fake records the wire-mapped form the public keys ride as.
    assert port.calls[0]["filters"] == {
        "metadata.doc_type": "10-Q",
        "metadata.company": "Apple",
    }


# ----- an absent filter leaves the arm alone -----


def test_an_unfiltered_call_still_searches_attachments() -> None:
    """The decision binds only a call that actually carries a filter;
    without one the arm runs exactly as before."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    port = FakeRetrievePort(
        hits_by_query={FILTERED: [_company_chunk()]},
        known_document_ids=set(KNOWN_IDS),
    )
    model = ScriptedChatModel(script=_script({"query": FILTERED}))
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever,
        degradation_ledger=DegradationLedger(),
    )

    envelope = _envelope(_run(graph))

    assert retriever.calls, "an unfiltered call must still search the arm"
    assert envelope["attachment_chunks"]
    assert "degraded" not in envelope


@pytest.mark.parametrize("empty", [{}, {"title": "   "}])
def test_a_whole_scope_filter_leaves_the_arm_alone(empty: dict[str, str]) -> None:
    """A filter that normalizes to nothing is a whole-scope search, not a
    narrowed one, so the arm is eligible exactly as before."""
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    port = FakeRetrievePort(
        hits_by_query={FILTERED: [_company_chunk()]},
        known_document_ids=set(KNOWN_IDS),
    )
    model = ScriptedChatModel(script=_script({"query": FILTERED, "filters": empty}))
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever,
        degradation_ledger=DegradationLedger(),
    )

    envelope = _envelope(_run(graph))

    assert retriever.calls
    assert envelope["attachment_chunks"]


def test_a_filtered_call_does_not_leak_its_exclusion_into_the_next_call() -> None:
    """The excluded-arm report is per-CALL state, not per-GRAPH state.

    ``compile_graph`` builds the retrieve tool once and the graph is
    cached across runs, so the closure's ``excluded_arm_signal`` lives as
    long as the process. Without a per-invocation reset, the filtered
    call's signal would still be set when the NEXT call runs — and that
    next call, which has no filter and whose arm really searched, would
    answer with a degradation and a reason its own shape never earned.
    The truthful channel owes the absence.
    """
    retriever = FakeAttachmentSearchPort(
        {
            _THREAD_A: [_attachment_chunk("att-1")],
            _THREAD_B: [_attachment_chunk("att-2")],
        }
    )
    port = FakeRetrievePort(
        hits_by_query={FILTERED: [_company_chunk()]},
        known_document_ids=set(KNOWN_IDS),
    )
    model = ScriptedChatModel(
        script=_script({"query": FILTERED, "filters": dict(GENERIC_FILTER)})
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever,
        degradation_ledger=DegradationLedger(),
    )

    model.script = _script({"query": FILTERED, "filters": dict(GENERIC_FILTER)})
    model._index = 0
    graph.invoke(
        reset_input_state(FILTERED),
        config={"configurable": {"thread_id": _THREAD_A}},
    )
    model.script = _script({"query": FILTERED})
    model._index = 0
    second = _envelope(
        graph.invoke(
            reset_input_state(FILTERED),
            config={"configurable": {"thread_id": _THREAD_B}},
        )
    )

    assert second["attachment_chunks"], "the unfiltered call's arm must have run"
    assert "degraded" not in second, (
        "the previous call's excluded-arm signal leaked into this envelope"
    )
    assert "degraded_reasons" not in second


# ----- an out-of-contract filter map excludes the arm too -----


def _refusing_port() -> FakeRetrievePort:
    """A port that refuses, the way the real seam refuses an
    out-of-contract map with ``invalid_filter``."""
    return FakeRetrievePort(hits_by_query={})


@pytest.mark.parametrize(
    "off_contract",
    [
        pytest.param({"cik": "0000320193"}, id="legacy-name-inside-the-map"),
        pytest.param({"issuer": "Acme"}, id="unknown-key"),
        pytest.param({"company": ["Acme"]}, id="non-string-value"),
    ],
)
def test_an_off_contract_filter_excludes_the_attachment_arm(
    off_contract: dict[str, Any],
) -> None:
    """A map the port seam refuses is not 「no narrowing」.

    :func:`normalized_metadata_filters` raises for every out-of-contract
    state, and the previous reading mapped that raise to ``False`` —
    「the arm runs normally」. That let an unfiltered attachment answer
    ride the same envelope as an ``invalid_filter`` refusal: the model
    saw a refusal for the corpus AND attachment chunks the refusal had
    no scope to exclude. An un-askable call must draw nothing from any
    arm.
    """
    retriever = FakeAttachmentSearchPort({_THREAD_A: [_attachment_chunk("att-1")]})
    model = ScriptedChatModel(script=_script({"query": FILTERED, "filters": off_contract}))
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_refusing_port(),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=retriever,
        degradation_ledger=DegradationLedger(),
    )

    envelope = _envelope(_run(graph))

    assert retriever.calls == [], (
        "a call whose filter the port refuses must not search the "
        "attachment arm and answer with unscoped evidence"
    )
    assert "attachment_chunks" not in envelope


# ----- the decision is grounded in the attachment contract -----


def test_the_attachment_row_carries_no_generic_metadata_to_bind() -> None:
    """The WHY of the exclusion, pinned: a row with no ``title`` /
    ``doc_type`` / ``company`` / ``period`` field is why the arm cannot
    apply the predicates at all."""
    import dataclasses

    from fin.host.attachment.attachment_store import AttachmentRecord

    fields = {f.name for f in dataclasses.fields(AttachmentRecord)}
    for key in ("title", "doc_type", "company", "period"):
        assert key not in fields, (
            f"{key} now exists on an attachment row: the exclusion may no "
            "longer be the only contract-preserving option, and this test "
            "must be revisited with the arm that binds it"
        )


def test_the_attachment_search_port_takes_no_metadata_argument() -> None:
    """The other half: even given a row field there is nowhere to pass the
    predicate — ``search`` has no metadata parameter."""
    import inspect

    from fin.host.attachment.attachment_retrieval import ThreadAttachmentRetriever

    signature = inspect.signature(ThreadAttachmentRetriever.search)
    assert "metadata" not in signature.parameters
    assert "filters" not in signature.parameters


def test_the_excluded_arm_is_documented_in_the_tool_contract() -> None:
    """A model that supplies a filter must be able to see that the
    attachment arm will not answer, or it cannot reason about the
    partial result."""
    from fin.host.graph import _make_retrieve_tool

    doc = (_make_retrieve_tool.__doc__ or "") + " ".join(
        getattr(
            _make_retrieve_tool,
            "__wrapped__",
            _make_retrieve_tool,
        ).__doc__
        or ""
        for _ in (0,)
    )
    # The docstring lives on the inner tool function, which the factory
    # closes over; assert against the module source instead, which is
    # the same contract the model reads.
    source = Path(
        _make_retrieve_tool.__code__.co_filename
    ).read_text(encoding="utf-8")
    assert "attachment" in source
