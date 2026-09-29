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

The module also owns the attachment half of the same seam: an attachment
hit or slice becomes evidence only while a trusted record answers for
its id and version, which is one rule for both legs and so lives in
:func:`fin.host.evidence_admission.attachment_evidence_from_ref`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import EvidenceItem, verify_citations
from fin.host.evidence_admission import (
    admit_retrieved_chunks,
    attachment_evidence_from_ref,
)
from fin.host.graph import _make_retrieve_tool
from fin.retrieve import Chunk
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    MAPPING_STAGE_EVIDENCE_ADMISSION,
    MappingFailureLedger,
)
from fin.retrieve.source_read import ATTACHMENT_KIND


RETRIEVAL_SIDE_ID = "iknow-row-000123"
DROPPED_TEXT = (
    "Total net sales for the quarter increased by nine percent "
    "compared with the prior year period."
)
KEPT_TEXT = (
    "Operating expenses for the quarter were flat compared with the "
    "prior year period."
)


def _locator(unit: int = 1) -> Locator:
    return Locator(
        document_id="DOC-2023Q1", version="v-" + "a" * 60, unit=unit
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
    assert [item.unit_text for item in evidence] == [KEPT_TEXT]
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


def test_retrieve_tool_envelope_names_a_hit_by_the_frozen_unit_only() -> None:
    """The chunk envelope the model receives carries exactly the company
    triple ``{document_id, version, unit}``: no generic whole-page
    ordinal survives anywhere on the citation path."""
    ledger = MappingFailureLedger()
    command = _invoke_retrieve_tool(
        _ScriptedPort([_mapped_chunk()]), ledger
    )

    payload = json.loads(command.update["messages"][0].content)
    assert set(payload["chunks"][0]["source_ref"]) == {
        "document_id",
        "version",
        "unit",
    }
    assert payload["chunks"][0]["source_ref"] == {
        "document_id": "DOC-2023Q1",
        "version": "v-" + "a" * 60,
        "unit": 1,
    }


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
        [EvidenceItem(locator=_locator(), unit_text=KEPT_TEXT)],
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
            EvidenceItem(locator=chunk.source_ref, unit_text=chunk.text)
            for chunk in admitted
        ],
        0,
        known_document_ids={"DOC-2023Q1"},
    )

    assert result.outcome == "bounce"
    assert getattr(result, "reason", None) is None


def test_admission_does_not_swallow_malformed_evidence_contract() -> None:
    """Admission filters on the mapping condition only. An empty
    ``unit_text`` is still the gate's malformed-evidence error, with the
    offending index — this seam does not widen or narrow that rule."""
    from fin.host.citation_gate import MalformedEvidenceError

    with pytest.raises(MalformedEvidenceError) as excinfo:
        verify_citations(
            "any answer",
            [
                EvidenceItem(locator=_locator(), unit_text=KEPT_TEXT),
                EvidenceItem(locator=_locator(unit=2), unit_text=""),
            ],
            0,
            known_document_ids={"DOC-2023Q1"},
        )

    assert excinfo.value.index == 1


# ----- the attachment identity grant -----

ATTACHMENT_REF: dict[str, Any] = {
    "kind": "attachment",
    "attachment_id": "ATT-1",
    "version": 7,
    "unit": 3,
}
ATTACHMENT_HIT_TEXT = (
    "The attached term sheet lists a purchase price of 12,000 thousand."
)


@dataclass(frozen=True)
class _Trusted:
    """The pair a thread's trusted record grants: name + presentation."""

    filename: str
    display: str


def _ref(**overrides: Any) -> dict[str, Any]:
    return {**ATTACHMENT_REF, **overrides}


def _grant(*, current_version: int | None = 7):
    """A grant that answers only for ``ATT-1`` at one version.

    The host builds this face from the run's thread-bound leg, so a
    foreign id, a voided row, and a re-prepared version all answer
    ``None`` here — which is exactly what the admission rule consumes.
    """

    def lookup(
        attachment_id: str, version_int: int | None
    ) -> tuple[str, str] | None:
        if attachment_id != "ATT-1" or version_int != current_version:
            return None
        return ("term-sheet.pdf", "page")

    return lookup


def test_an_attested_hit_becomes_attachment_evidence_carrying_its_identity() -> None:
    item = attachment_evidence_from_ref(
        _ref(), ATTACHMENT_HIT_TEXT, grant=_grant()
    )

    assert item is not None
    assert item.kind == ATTACHMENT_KIND
    assert item.attachment_id == "ATT-1"
    assert item.version_int == 7
    assert item.locator.document_id == ""
    assert item.locator.version == "7"
    assert item.locator.unit == 3
    # The display pair is the trusted record's, never the model's.
    assert item.filename == "term-sheet.pdf"
    assert item.display == "page"
    assert item.unit_text == ATTACHMENT_HIT_TEXT


def test_a_retrieved_hit_never_bridges_to_an_adjacent_slice() -> None:
    """A passage hit is not a unit-aligned offset, so it carries no
    cursor: ``_contiguous_runs`` only joins on ``next_position``, and a
    ``None`` there means this item stands alone."""
    item = attachment_evidence_from_ref(
        _ref(), ATTACHMENT_HIT_TEXT, grant=_grant()
    )

    assert item is not None
    assert (item.position, item.next_position) == (0, None)


def test_a_slice_keeps_the_cursor_the_read_returned() -> None:
    item = attachment_evidence_from_ref(
        _ref(),
        ATTACHMENT_HIT_TEXT,
        grant=_grant(),
        position=4_000,
        next_position=8_000,
    )

    assert item is not None
    assert (item.position, item.next_position) == (4_000, 8_000)


def test_a_record_that_grants_nothing_earns_no_evidence() -> None:
    """The text still reached the model; the gate just cannot attribute
    it, so no citation may rest on it."""
    assert (
        attachment_evidence_from_ref(
            _ref(attachment_id="OTHER-9"), ATTACHMENT_HIT_TEXT, grant=_grant()
        )
        is None
    )
    assert (
        attachment_evidence_from_ref(_ref(), ATTACHMENT_HIT_TEXT, grant=None)
        is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"unit": 0}, id="zero_unit"),
        pytest.param({"unit": -1}, id="negative_unit"),
        pytest.param({"unit": True}, id="bool_unit"),
        pytest.param({"unit": "3"}, id="string_unit"),
        pytest.param({"unit": None}, id="absent_unit"),
        pytest.param({"attachment_id": ""}, id="blank_attachment_id"),
        pytest.param({"version": ""}, id="blank_version"),
        pytest.param({"version": None}, id="absent_version"),
        pytest.param({"version": "v7"}, id="non_integer_version"),
    ],
)
def test_an_unattributable_shape_earns_no_evidence_rather_than_a_gate_error(
    overrides: dict[str, Any],
) -> None:
    """The gate validates its evidence eagerly and raises, so an item it
    must reject has to be stopped here: a malformed identity is no
    citation, never a failed run."""
    assert (
        attachment_evidence_from_ref(
            _ref(**overrides), ATTACHMENT_HIT_TEXT, grant=_grant()
        )
        is None
    )


def test_a_version_the_record_no_longer_answers_for_earns_no_evidence() -> None:
    assert (
        attachment_evidence_from_ref(
            _ref(version=8), ATTACHMENT_HIT_TEXT, grant=_grant(current_version=7)
        )
        is None
    )
