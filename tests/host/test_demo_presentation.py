"""Presentation-only HTTP fields stay derived and outside RunRecord rows."""

import json

from fin.corpus.extracted import Locator
from fin.host.grounded.citation_gate import Citation, EvidenceItem
from fin.host.named_failures import CORPUS_NOT_READY, INSUFFICIENT_EVIDENCE
from fin.host.grounded.readable_projection import EVIDENCE_REFUSAL_COPY, INDEX_WORK_COPY
from fin.host.run_payload import _run_to_response_payload
from fin.host.workspace_http import _terminal_sse_frames


def test_terminal_status_carries_graph_owned_search_mode_decision() -> None:
    decision = {
        "search_mode": "global",
        "confidence": 0.91,
        "decision_tier": "jev",
        "signals": {"utterance_len": 47},
        "options": [],
        "failure_code": None,
    }
    payload = _run_to_response_payload(
        {"status": "done", "answer": "A grounded answer.", "search_mode_decision": decision}
    )

    frames = list(_terminal_sse_frames(run_id="run-1", payload=payload))
    status_frame = json.loads(frames[0].split("data: ", 1)[1])

    assert frames[0].startswith("event: status\n")
    assert status_frame["search_mode_decision"] == decision


def test_ask_citation_schema_accepts_optional_display_section() -> None:
    from fin.host.http import CitationSchema

    row = {
        "kind": "company",
        "document_id": "ZEUS-2024Q3",
        "attachment_id": "",
        "version": "v1",
        "unit": 2,
        "display": "page",
        "filename": "",
        "quote": "A sufficiently long verified passage from this source.",
        "section": "Section 2",
    }

    assert CitationSchema(**row).model_dump(exclude_none=True) == row


def test_terminal_citation_adds_section_without_mutating_run_payload() -> None:
    quote = "The installation guide states that the gateway uses two power supplies."
    citation = Citation(
        kind="company",
        document_id="ZEUS-2024Q3",
        attachment_id="",
        version="v1",
        unit=2,
        display="page",
        filename="",
        quote=quote,
    )
    evidence = EvidenceItem(
        locator=Locator(document_id="ZEUS-2024Q3", version="v1", unit=2),
        unit_text=f"ZEUS Installation Guide · Page 2\nSection 4: Power\n{quote}",
    )
    payload = _run_to_response_payload(
        {"status": "done", "answer": quote, "citations": [citation]}
    )

    assert "section" not in payload["citations"][0]
    frames = list(
        _terminal_sse_frames(
            run_id="run-2", payload=payload, evidence=[evidence]
        )
    )
    citation_rows = json.loads(frames[-1].split("data: ", 1)[1])

    assert citation_rows[0]["section"] == "Section 4"


def test_unsupported_material_outcome_shows_the_exact_no_evidence_copy() -> None:
    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": INSUFFICIENT_EVIDENCE,
            "answer": "A model-generated unsupported claim.",
            "citations": [],
        }
    )

    assert payload["reason"] == INSUFFICIENT_EVIDENCE
    assert payload["answer"] == ""
    assert payload["citations"] == []
    # GE-5 / T10: the evidence-refusal sentence names the insufficiency and
    # carries the two supported rephrasings, in ordinary-person Chinese.
    assert payload["assistant_message"] == EVIDENCE_REFUSAL_COPY


def test_empty_retrieval_outcome_shows_the_exact_no_evidence_copy() -> None:
    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": CORPUS_NOT_READY,
            "answer": "",
            "citations": [],
        }
    )

    assert payload["reason"] == CORPUS_NOT_READY
    # GE-5 / T10: index-work copy is DISTINCT from the refusal sentence;
    # ``corpus_not_ready`` is the reason that carries it.
    assert payload["assistant_message"] == INDEX_WORK_COPY
