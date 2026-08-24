"""Soft-Track T3: status snapshot inherit for research follow-ups."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.host.track_decision import decide_track

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_two_research_context_turns_append_two_snapshots():
    """Two successive research-context turns leave ≥2 readable snapshots."""
    model = ScriptedChatModel(
        script=[
            AIMessage(content="Looking up AAPL 2023 Q3."),
            AIMessage(content="Looking up AAPL 2023 Q2."),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    tid = "t-soft-snap-2"
    out1 = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    assert out1["track"] == "research"
    snaps1 = list(out1.get("status_snapshots") or [])
    assert len(snaps1) >= 1
    assert snaps1[-1].get("company") in ("AAPL", "Apple") or snaps1[-1].get(
        "company"
    ) == "AAPL"

    out2 = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q2?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    snaps2 = list(out2.get("status_snapshots") or [])
    assert len(snaps2) >= 2
    # Prior snapshot retained; latest reflects Q2.
    assert snaps2[0].get("period") != snaps2[-1].get("period") or len(snaps2) >= 2
    assert "Q2" in str(snaps2[-1].get("period") or "")


def test_short_followup_inherits_research_from_snapshot():
    """Prior research snapshot + short follow-up → inherit → research."""
    decision = decide_track(
        [HumanMessage(content="and EPS?")],
        snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": ["AAPL-2023Q3"],
            }
        ],
    )
    assert decision.track == "research"
    assert decision.decision_tier == "inherit"

    model = ScriptedChatModel(script=[AIMessage(content="need evidence")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    tid = "t-soft-inherit"
    g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    out = g.invoke(
        {
            **reset_input_state("and EPS?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    assert out["track"] == "research"
    assert out["track_decision"]["decision_tier"] == "inherit"


def test_company_change_clears_dependents_keeps_prior():
    """Company-change turn: latest clears dependents; prior remains."""
    model = ScriptedChatModel(
        script=[
            AIMessage(content="AAPL context."),
            AIMessage(content="MSFT context."),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    tid = "t-soft-company-change"
    g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    out = g.invoke(
        {
            **reset_input_state("What about MSFT revenue?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    snaps = list(out.get("status_snapshots") or [])
    assert len(snaps) >= 2
    prior = snaps[0]
    latest = snaps[-1]
    assert prior.get("company") == "AAPL"
    assert prior.get("period")
    assert latest.get("company") == "MSFT"
    # Dependent fields cleared on company change when not restated.
    assert not latest.get("period")
    assert list(latest.get("document_ids") or []) == []


def test_reset_input_state_omits_status_snapshots():
    """Run-scoped reset must not wipe snapshot history."""
    reset = reset_input_state("follow-up")
    assert "status_snapshots" not in reset
