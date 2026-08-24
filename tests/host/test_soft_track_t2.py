"""Soft-Track T2 graph contracts: clarify path before tools."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.agent_instructions import (
    CLARIFY_INSTRUCTIONS_MARKER,
    HOST_INSTRUCTIONS_MARKER,
)
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _checkpoint_messages(graph, thread_id: str) -> list:
    snapshot = graph._graph.get_state(_thread_config(thread_id))
    return list((snapshot.values or {}).get("messages") or [])


def test_underspecified_filing_ask_clarify_skips_tools_and_gate():
    """Metric without company/period → clarify, no tools, done, empty cites."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Which company and period? Options include AAPL, MSFT, "
                    "AMZN and periods 2022 Q3, 2023 Q1–Q3."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="What was revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-clarify"),
    )
    assert out["track"] == "clarify"
    assert out["status"] == "done"
    assert out.get("citations", ()) == () or out.get("citations") == []
    assert out["answer"]
    assert "AAPL" in out["answer"] or "MSFT" in out["answer"]
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
    for msg in out["messages"]:
        if isinstance(msg, AIMessage):
            assert not msg.tool_calls

    checkpointed = _checkpoint_messages(g, "t-soft-clarify")
    assert not any(
        isinstance(m, SystemMessage)
        and CLARIFY_INSTRUCTIONS_MARKER in str(m.content)
        for m in checkpointed
    )
    assert not any(
        isinstance(m, SystemMessage) and HIDDEN_BOUNCE_MARKER in str(m.content)
        for m in checkpointed
    )


def test_clarify_prompt_is_call_scoped_not_research():
    """Clarify leaf prepends CLARIFY prompt at call time only."""
    from fin.host.scripted_model import ScriptedChatModel as SCM

    seen: list[list] = []

    class Recording(SCM):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = Recording(
        script=[
            AIMessage(
                content="Please pick a company (AAPL, MSFT, AMZN) and period."
            )
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    g.invoke(
        {
            "messages": [
                HumanMessage(content="What was net sales for the quarter?")
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-clarify-prompt"),
    )
    assert seen
    first = seen[0]
    assert isinstance(first[0], SystemMessage)
    assert CLARIFY_INSTRUCTIONS_MARKER in str(first[0].content)
    assert HOST_INSTRUCTIONS_MARKER not in str(first[0].content)
    assert "AAPL" in str(first[0].content)
    assert "2022 Q3" in str(first[0].content) or "2023" in str(first[0].content)


def test_company_without_period_metric_ask_is_clarify():
    """Filing metric with company but missing period → clarify."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="Which period for AAPL? Options: 2022 Q3, 2023 Q1–Q3."
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="What was AAPL net sales?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-clarify-period"),
    )
    assert out["track"] == "clarify"
    assert out["status"] == "done"


def test_bare_q_dialogue_not_clarify():
    """Regression: HumanMessage('q') stays dialogue (no single global
    default track — bare ``q`` falls past rules / inherit /
    clarify, so the residual branch lands on dialogue, NOT
    research and NOT clarify). Specs/soft-track.md."""
    model = ScriptedChatModel(script=[AIMessage(content="need tools")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="q")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-clarify-q"),
    )
    assert out["track"] == "dialogue"


def test_filled_slots_follow_up_after_clarify_enters_research():
    """T2: next run with company+period filled can enter research."""
    from fin.host.state import reset_input_state

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Which company and period? Options include AAPL, MSFT, "
                    "AMZN and periods 2022 Q3, 2023 Q1–Q3."
                )
            ),
            AIMessage(content="need evidence"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    tid = "t-soft-clarify-then-research"
    first = g.invoke(
        {
            "messages": [HumanMessage(content="What was revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config(tid),
    )
    assert first["track"] == "clarify"
    second = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_thread_config(tid),
    )
    assert second["track"] == "research"
