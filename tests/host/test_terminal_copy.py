"""Every terminal refusal renders ordinary-person copy (T8 / SC10).

``specs/workspace.md`` forbids a blank terminal card: when a run
ends without an ``answer``, the SPA renders
``RunResponsePayload.assistant_message`` in the answer-bubble slot,
so an empty string there IS the blank crash card.

``numeric_gate_fail`` — the research-track refusal the numeric gate
emits when a claimed number has no evidenced fact row (T5 / ADR
0031) — was the one refusal reason with no entry in
``ORDINARY_PERSON_COPY``, so it fell through
``ordinary_person_copy``'s ``.get(reason, "")`` to ``""``.

The tests below pin the copy at three levels:

* the mapping helper (``ordinary_person_copy``);
* the wire payload both the SSE ``status`` frame and the persisted
  turn row are built from (``_run_to_response_payload`` — the SSOT
  shared by ``/ask`` and the workspace send path);
* an end-to-end scripted run that actually ends
  ``numeric_gate_fail``, mapped through that payload.

Reason coverage (rather than one hard-coded string) is what keeps a
future refusal reason from re-opening the same gap.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.run_payload import (
    ORDINARY_PERSON_COPY,
    _run_to_response_payload,
    ordinary_person_copy,
)


# Every ``status_reason`` a refusing terminal state can carry:
# ``unknown_locator`` / ``citation_repair_cap`` from the citation
# gate (``fin.host.citation_gate``) and ``numeric_gate_fail`` from
# the numeric gate (``fin.host.graph.citation_gate_node``).
REFUSE_REASONS = (
    "unknown_locator",
    "citation_repair_cap",
    "numeric_gate_fail",
)


def _facts_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "corpus" / "facts"


# ----- mapping helper -----


def test_numeric_gate_fail_has_ordinary_person_copy() -> None:
    """SC10: the numeric-gate refusal is not blank."""
    copy = ordinary_person_copy("refuse", "numeric_gate_fail")
    assert copy.strip()


def test_numeric_gate_fail_copy_is_ordinary_prose() -> None:
    """The operator sees prose, not the internal reason token."""
    copy = ORDINARY_PERSON_COPY["numeric_gate_fail"]
    assert "numeric_gate_fail" not in copy
    assert "_" not in copy


def test_numeric_gate_fail_copy_differs_from_other_refusals() -> None:
    """A distinct failure mode reads distinctly — otherwise the
    copy cannot tell the operator what to do next."""
    copies = {reason: ordinary_person_copy("refuse", reason) for reason in REFUSE_REASONS}
    assert len(set(copies.values())) == len(REFUSE_REASONS)


def test_every_refuse_reason_has_non_blank_copy() -> None:
    """No refusal reason may fall through to the empty string."""
    blank = [r for r in REFUSE_REASONS if not ordinary_person_copy("refuse", r).strip()]
    assert blank == []


# ----- wire payload (SSE status frame + persisted turn row) -----


def test_payload_numeric_gate_fail_carries_assistant_message() -> None:
    """The shape the SSE ``status`` frame and the ``RunRecord`` are
    built from: empty ``answer``, non-empty ordinary-person copy."""
    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": "numeric_gate_fail",
            "answer": "Revenue was $999 in 2023 Q3.",
            "citations": (),
        }
    )
    assert payload["status"] == "refuse"
    assert payload["answer"] == ""
    assert payload["reason"] == "numeric_gate_fail"
    assert payload["assistant_message"].strip()
    assert payload["assistant_message"] == ORDINARY_PERSON_COPY["numeric_gate_fail"]


def test_payload_done_still_has_no_assistant_message() -> None:
    """Regression guard: ``done`` runs answer for themselves."""
    payload = _run_to_response_payload(
        {"status": "done", "status_reason": "done", "answer": "A.", "citations": ()}
    )
    assert payload["assistant_message"] == ""


# ----- end-to-end: a run that really ends numeric_gate_fail -----


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Legacy tree-backed read adapter over the committed corpus. Only the
# scripted ``read_file`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
from fin.retrieve import ExtractedTreeReadPort

_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)


def _numeric_gate_fail_run() -> dict:
    """Scripted research run whose answer quotes a wrong number.

    Mirrors ``tests/host/test_research_done_with_fact.py``: the
    answer embeds a >=32-char span from the read page so the
    citation gate passes and the NUMERIC gate is the refusing one.
    """
    span = (
        "UNITED STATES SECURITIES AND EXCHANGE COMMISSION "
        "Washington, D.C. 20549 FORM 10-Q"
    )
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_xbrl_fact",
                        "args": {
                            "concept": "us-gaap:Revenues",
                            "period": "2023-Q3-YTD",
                            "document_id": "AAPL-2023Q3",
                        },
                        "id": "c-fact",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "page": 1,
                            "page_limit": 1,
                        },
                        "id": "c-read",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {span} Revenue was $999 in 2023 Q3."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
        facts_root=_facts_root(),
    )
    return graph.invoke(
        {
            "messages": [HumanMessage(content="What was AAPL Q3 2023 revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config={"configurable": {"thread_id": "t-numeric-gate-copy"}},
    )


def test_numeric_gate_fail_run_renders_non_blank_terminal_card() -> None:
    """SC10 end to end: the run really refuses with
    ``numeric_gate_fail`` and the payload the turn row / SSE
    terminal frame is built from carries non-empty copy."""
    out = _numeric_gate_fail_run()
    assert out["status"] == "refuse"
    assert out["status_reason"] == "numeric_gate_fail"

    payload = _run_to_response_payload(out)
    assert payload["answer"] == ""
    assert payload["assistant_message"].strip()
    assert payload["citations"] == []


def test_numeric_gate_fail_run_summarizes_the_fact_lookup() -> None:
    """The tools region of that same refused turn names the fact
    the model looked up (T8's ``get_xbrl_fact`` one-liner), so the
    operator can see WHICH number failed verification."""
    payload = _run_to_response_payload(_numeric_gate_fail_run())
    fact_rows = [s for s in payload["tool_summaries"] if s["name"] == "get_xbrl_fact"]
    assert fact_rows, "the run recorded no get_xbrl_fact summary"
    summary = fact_rows[0]["summary"]
    assert summary != "get_xbrl_fact"
    assert "us-gaap:Revenues" in summary
