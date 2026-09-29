"""``compare_facts`` inside the host graph (T3 / spec SC2 + SC11).

Three contracts live here:

1. The comparison the tool produced lands on ``AgentState`` as
   structured data (``comparisons``), so the workspace turn row can
   render a table instead of re-parsing prose.
2. The numeric gate treats every comparison COLUMN as an implicit
   ``(concept, period)`` claim, so an answer that quotes numbers the
   comparison never reported cannot reach ``done`` (SC2's
   "a turn that quotes only unverified numbers does not ``done``").
3. The citation verifier is unchanged: a comparison contributes no
   ``EvidenceItem``, so prose still needs a verified span, and
   ``refuse`` / ``step_cap`` still carry empty citations (SC11).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.host.tools.compare_facts import COMPARISON_SCHEMA
from fin.retrieve import FakeRetrievePort


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "AAPL-2023Q3", "MSFT-2023Q3"}

# The ``read_source`` company leg over the committed corpus units. Only
# the scripted ``read_source`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
_READ_ADAPTER = FakeRetrievePort(
    pages_by_doc={
        AAPL_2022Q3: load_extracted_tree(
            Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
        )[AAPL_2022Q3]
    }
)

# A verbatim slice of the committed AAPL-2022Q3 page 1. The citation
# gate needs a >=32-char extractive span before any prose answer can
# pass; without it we would be pinning the bounce path, not the
# numeric one. Digit-free on purpose: a number inside the span would
# itself be a claim the numeric gate has to match.
PAGE_SPAN = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _compare_then_answer_script(
    *, answer: str, periods: list[str], issuers: list[str] | None = None
) -> list[AIMessage]:
    """compare_facts -> read_source (evidence) -> prose answer."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "compare_facts",
                    "args": {
                        "concept": "us-gaap:Revenues",
                        "periods": periods,
                        "issuers": issuers or ["AAPL"],
                    },
                    "id": "c-compare",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "unit": 1,
                        },
                        "position": 0,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


# Both slots (company + period) are present so Soft-Track's rules
# tier lands on ``research``; a slot-less "compare AAPL revenue"
# routes to the clarify leaf and never reaches a tool.
COMPARE_QUESTION = "Compare AAPL revenue for Q3 2023 versus Q3 2022."


def _run(script: list[AIMessage], *, facts_root: Path, thread_id: str) -> dict:
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        facts_root=facts_root,
    )
    return graph.invoke(
        {
            "messages": [HumanMessage(content=COMPARE_QUESTION)],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config(thread_id),
    )


# ----- wiring -----


def test_compile_graph_binds_compare_facts_after_get_xbrl_fact(
    compare_seed,
) -> None:
    """Additive binding: ``compare_facts`` is registered LAST so the
    existing tools keep their positions (LangChain's tool binding is
    order-sensitive)."""
    model = ScriptedChatModel(script=[])
    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        facts_root=compare_seed.root,
    )
    names = [getattr(t, "name", repr(t)) for t in model.bound_tools]
    assert "compare_facts" in names
    assert names.index("compare_facts") > names.index("get_xbrl_fact")


def test_reset_input_state_clears_comparisons() -> None:
    """A follow-up run must not inherit the previous run's table."""
    assert reset_input_state("q")["comparisons"] == []


# ----- structured comparison on state -----


def test_comparison_lands_on_state_as_structured_data(compare_seed) -> None:
    out = _run(
        _compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue grew year over year.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-state",
    )
    comparisons = out["comparisons"]
    assert len(comparisons) == 1
    entry = comparisons[0]
    assert entry["schema"] == COMPARISON_SCHEMA
    assert entry["tool_call_id"] == "c-compare"
    assert [cell["value"] for cell in entry["rows"][0]["cells"]] == [
        compare_seed.aapl_2022_value,
        compare_seed.aapl_2023_value,
    ]
    assert entry["deltas"][0]["direction"] == "up"


def test_comparison_contributes_no_citation_evidence(compare_seed) -> None:
    """A fact row is not page text: every citation still comes from a
    read / retrieve span (SC11 — the verifier only fills verified
    spans)."""
    out = _run(
        _compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue grew year over year.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-evidence",
    )
    assert out["status"] == "done"
    assert len(out["citations"]) >= 1
    assert {c.document_id for c in out["citations"]} == {AAPL_2022Q3}


def test_comparison_tool_summary_is_human_readable(compare_seed) -> None:
    out = _run(
        _compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue grew year over year.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-summary",
    )
    summaries = {s["name"]: s["summary"] for s in out["tool_summaries"]}
    assert "us-gaap:Revenues" in summaries["compare_facts"]
    assert "2/2" in summaries["compare_facts"]
    # Raw JSON never reaches a summary string (spec line 32).
    assert "{" not in summaries["compare_facts"]


# ----- numeric gate binding (SC2) -----


def test_answer_quoting_the_compared_numbers_reaches_done(
    compare_seed,
) -> None:
    answer = (
        f"{PAGE_SPAN} Revenue rose from $194,140 million to "
        f"$293,787 million."
    )
    out = _run(
        _compare_then_answer_script(
            answer=answer,
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-done",
    )
    assert out["status"] == "done"
    assert out["status_reason"] == "done"


def test_fabricated_numbers_after_compare_do_not_reach_done(
    compare_seed,
) -> None:
    """SC2: a turn quoting numbers the comparison never reported must
    not ``done`` — even though the prose carries a verified span."""
    answer = (
        f"{PAGE_SPAN} Revenue rose from $500,000 million to "
        f"$900,000 million."
    )
    out = _run(
        _compare_then_answer_script(
            answer=answer,
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-fabricated",
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "numeric_gate_fail"
    assert tuple(out.get("citations") or ()) == ()


def test_all_missing_comparison_refuses_a_numeric_answer(
    compare_seed,
) -> None:
    """Every cell missed, so no number in the answer is grounded."""
    answer = f"{PAGE_SPAN} Revenue was $123,456 million."
    out = _run(
        _compare_then_answer_script(
            answer=answer, periods=["1999-Q1-YTD"]
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-allmissing",
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "numeric_gate_fail"


def test_typed_empty_comparison_claims_nothing(compare_seed) -> None:
    """An ``empty_comparison`` EXIT has no columns, so it makes no
    numeric claim and the gate stays skipped — the run is judged by
    the citation verifier alone."""
    out = _run(
        _compare_then_answer_script(answer=f"{PAGE_SPAN} No data.", periods=[]),
        facts_root=compare_seed.root,
        thread_id="t-compare-empty",
    )
    assert out["status_reason"] != "numeric_gate_fail"
    assert out["comparisons"] == []


def test_qualitative_answer_after_compare_is_not_refused(
    compare_seed,
) -> None:
    """No parsed numbers in the answer -> vacuous pass for each claim
    (the citation verifier remains the grounding check)."""
    out = _run(
        _compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue grew year over year.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-qualitative",
    )
    assert out["status"] == "done"


def test_peer_comparison_numbers_are_gated_too(compare_seed) -> None:
    answer = (
        f"{PAGE_SPAN} AAPL reported $293,787 million and MSFT "
        f"$211,915 million."
    )
    out = _run(
        _compare_then_answer_script(
            answer=answer,
            periods=[compare_seed.period_2023],
            issuers=["AAPL", "MSFT"],
        ),
        facts_root=compare_seed.root,
        thread_id="t-compare-peer",
    )
    assert out["status"] == "done"
    comparison = out["comparisons"][0]
    assert comparison["axis"] == "peer"
    assert [row["key"] for row in comparison["rows"]] == ["AAPL", "MSFT"]
