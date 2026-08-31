"""Citation-evidence admission drops mapping-failed hits (plan T17, SC22).

SC22 has two halves. The retrieve half (counting the adapter's
mapper-drop) is ``tests/retrieve/test_hit_mapping.py``. This module
covers the host half: 「该命中不进入引用证据，检索侧标识永不作为引用证据
传给用户」.

The injection is a retrieve port that hands the host a chunk whose
``source_ref`` is still the retrieval service's own identifier — the
exact shape a stale ``locator_map`` or a future adapter without one
would produce. Admission drops it and counts it; the citation gate's own
verdict rules are NOT consulted differently and NOT relaxed (the last
three tests pin the gate's existing semantics).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import EvidenceItem, verify_citations
from fin.host.evidence_admission import admit_retrieved_chunks
from fin.host.graph import _make_retrieve_tool
from fin.retrieve import Chunk
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    MAPPING_STAGE_EVIDENCE_ADMISSION,
    MappingFailureLedger,
)


RETRIEVAL_SIDE_ID = "iknow-row-000123"
DROPPED_TEXT = (
    "Total net sales for the quarter increased by nine percent "
    "compared with the prior year period."
)
KEPT_TEXT = (
    "Operating expenses for the quarter were flat compared with the "
    "prior year period."
)


def _locator(page: int = 1) -> Locator:
    return Locator(
        document_id="DOC-2023Q1", version="v-" + "a" * 60, page=page
    )


def _mapping_failed_chunk() -> Chunk:
    """A hit whose ``source_ref`` never mapped back to a fin locator."""
    return Chunk(
        text=DROPPED_TEXT,
        source_ref=RETRIEVAL_SIDE_ID,  # type: ignore[arg-type]
        score=9.0,
    )


def _mapped_chunk() -> Chunk:
    return Chunk(text=KEPT_TEXT, source_ref=_locator(), score=1.0)


class _ScriptedPort:
    """Retrieve port returning a fixed chunk list."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"chunks": list(self._chunks)}


def _invoke_retrieve_tool(adapter: Any, ledger: MappingFailureLedger):
    tool = _make_retrieve_tool(adapter, mapping_ledger=ledger)
    return tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "what was revenue"},
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


# ----- the admission seam -----


def test_admission_drops_a_chunk_whose_source_ref_is_not_a_locator() -> None:
    ledger = MappingFailureLedger()
    admitted = admit_retrieved_chunks(
        [_mapping_failed_chunk(), _mapped_chunk()], ledger=ledger
    )

    assert [chunk.text for chunk in admitted] == [KEPT_TEXT]
    assert ledger.count() == 1
    failure = ledger.failures()[0]
    assert failure.code == MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF
    assert failure.stage == MAPPING_STAGE_EVIDENCE_ADMISSION
    assert failure.retrieval_ref == RETRIEVAL_SIDE_ID


def test_admission_counts_nothing_when_every_hit_mapped() -> None:
    ledger = MappingFailureLedger()
    admitted = admit_retrieved_chunks([_mapped_chunk()], ledger=ledger)

    assert len(admitted) == 1
    assert ledger.count() == 0


# ----- the host retrieve tool -----


def test_retrieve_tool_contributes_no_evidence_for_a_mapping_failed_hit() -> None:
    """The dropped hit must not become an ``EvidenceItem``: the gate
    reads ``locator.document_id`` and would otherwise see a retrieval
    service row id."""
    ledger = MappingFailureLedger()
    command = _invoke_retrieve_tool(
        _ScriptedPort([_mapping_failed_chunk(), _mapped_chunk()]),
        ledger,
    )

    evidence = command.update["evidence"]
    assert [item.locator for item in evidence] == [_locator()]
    assert ledger.count() == 1


def test_retrieve_tool_message_omits_the_retrieval_side_identifier() -> None:
    """The dropped hit is absent from the serialized chunk envelope too,
    so the model cannot quote a passage fin cannot attribute."""
    ledger = MappingFailureLedger()
    command = _invoke_retrieve_tool(
        _ScriptedPort([_mapping_failed_chunk(), _mapped_chunk()]),
        ledger,
    )

    content = command.update["messages"][0].content
    assert RETRIEVAL_SIDE_ID not in content
    assert DROPPED_TEXT not in content
    payload = json.loads(content)
    assert [chunk["text"] for chunk in payload["chunks"]] == [KEPT_TEXT]


def test_retrieve_tool_counts_the_drop_when_every_hit_fails_to_map() -> None:
    """All-dropped is the miss shape on the wire — but with a non-zero
    counter, which is what stops it from looking like a healthy empty
    corpus."""
    ledger = MappingFailureLedger()
    command = _invoke_retrieve_tool(
        _ScriptedPort([_mapping_failed_chunk()]), ledger
    )

    assert json.loads(command.update["messages"][0].content) == {"chunks": []}
    assert not command.update.get("evidence")
    assert ledger.count() > 0


# ----- through the compiled graph -----


def _graph_with(chunks: list[Chunk], ledger: MappingFailureLedger, answer: str):
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
            AIMessage(content=answer),
        ],
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_ScriptedPort(chunks),
        known_document_ids={"DOC-2023Q1"},
        mapping_failure_ledger=ledger,
    )


def test_dropped_hit_cannot_be_cited_and_its_id_never_reaches_the_user(
    tmp_path,
) -> None:
    """End to end: the model quotes the dropped passage verbatim, and the
    run still produces no citation for it. The retrieval-side id appears
    nowhere in the run's user-visible payload — and the failure count is
    on disk afterwards."""
    from fin.host.run_payload import _run_to_response_payload

    path = tmp_path / "mapping-failures.jsonl"
    graph = _graph_with(
        [_mapping_failed_chunk()],
        MappingFailureLedger(path=path),
        f"Per the filing: {DROPPED_TEXT}",
    )

    out = graph.invoke(
        {
            "messages": [HumanMessage("What was AAPL revenue in 2023 Q1?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config={"configurable": {"thread_id": "t-mapping-drop"}},
    )

    assert not out.get("evidence")
    payload = _run_to_response_payload(out)
    assert payload["citations"] == []
    assert payload["evidence_locators"] == []
    assert RETRIEVAL_SIDE_ID not in json.dumps(payload, default=str)

    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tool_messages
    for message in tool_messages:
        assert RETRIEVAL_SIDE_ID not in message.content

    assert MappingFailureLedger(path=path).count() > 0


def test_mapped_hit_on_the_same_run_is_still_citable() -> None:
    """The drop is per hit: a locatable chunk beside a dropped one still
    grounds the answer, so admission is not a blanket refusal."""
    from fin.host.run_payload import _run_to_response_payload

    ledger = MappingFailureLedger()
    graph = _graph_with(
        [_mapping_failed_chunk(), _mapped_chunk()],
        ledger,
        f"Per the filing: {KEPT_TEXT}",
    )

    out = graph.invoke(
        {
            "messages": [HumanMessage("What was AAPL revenue in 2023 Q1?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config={"configurable": {"thread_id": "t-mapping-drop-mixed"}},
    )

    payload = _run_to_response_payload(out)
    assert payload["citations"], payload
    assert all(
        citation["document_id"] == "DOC-2023Q1"
        for citation in payload["citations"]
    )
    assert RETRIEVAL_SIDE_ID not in json.dumps(payload, default=str)
    assert ledger.count() == 1


# ----- the citation gate's own rules are untouched -----


def test_unknown_locator_refusal_is_unchanged_by_admission() -> None:
    """SC43's ``unknown_locator`` keeps its existing semantics: a
    document_id claim outside the corpus still refuses immediately, and
    admission neither pre-empts nor suppresses that verdict."""
    result = verify_citations(
        "Per FAKE-9999Q1 the number moved.",
        [EvidenceItem(locator=_locator(), page_text=KEPT_TEXT)],
        0,
        known_document_ids={"DOC-2023Q1"},
    )

    assert result.outcome == "refuse"
    assert result.reason == "unknown_locator"


def test_dropped_hit_does_not_turn_into_an_unknown_locator_refusal() -> None:
    """A mapping failure is a drop, not a rewrite of the gate's refusal
    vocabulary: with no citable span the run bounces on the ordinary
    path, never on ``unknown_locator``."""
    admitted = admit_retrieved_chunks(
        [_mapping_failed_chunk()], ledger=MappingFailureLedger()
    )
    result = verify_citations(
        f"Per the filing: {DROPPED_TEXT}",
        [
            EvidenceItem(locator=chunk.source_ref, page_text=chunk.text)
            for chunk in admitted
        ],
        0,
        known_document_ids={"DOC-2023Q1"},
    )

    assert result.outcome == "bounce"
    assert getattr(result, "reason", None) is None


def test_admission_does_not_swallow_malformed_evidence_contract() -> None:
    """Admission filters on the mapping condition only. An empty
    ``page_text`` is still the gate's malformed-evidence error — this
    ticket does not widen or narrow that rule."""
    from fin.host.citation_gate import MalformedEvidenceError

    with pytest.raises(MalformedEvidenceError):
        verify_citations(
            "any answer",
            [EvidenceItem(locator=_locator(), page_text="")],
            0,
            known_document_ids={"DOC-2023Q1"},
        )
