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
from fin.host.grounded.citation_gate import HIDDEN_BOUNCE_MARKER
from jev_fake import FakeJevChoice

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _checkpoint_messages(graph, thread_id: str) -> list:
    snapshot = graph._graph.get_state(_thread_config(thread_id))
    return list((snapshot.values or {}).get("messages") or [])


def test_jev_clarify_ask_skips_tools_and_gate():
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
        jev_client=FakeJevChoice([("clarify", 0.99)]),
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


def test_jev_clarify_prompt_is_call_scoped_not_research():
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
        jev_client=FakeJevChoice([("clarify", 0.99)]),
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
    # T2 split: identity/soul/usage must NOT hardcode the frozen
    # ticker/quarter snapshot — asks ground options in the 知识库 via
    # list_files. The clarify leaf must still surface the missing-slot
    # structure (company and period fields) so the model knows which
    # catalog fields to ask for.
    content = str(first[0].content)
    for forbidden in ("AAPL", "MSFT", "AMZN", "2022 Q3", "2023 Q1",
                      "2023 Q2", "2023 Q3"):
        assert forbidden not in content, forbidden
    assert "company" in content
    assert "period" in content


def test_jev_clarify_company_ask_without_period():
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
        jev_client=FakeJevChoice([("clarify", 0.99)]),
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


def test_bare_q_follows_judge_to_dialogue_not_research():
    """Regression (ADR 0080 retarget): bare ``q`` is gray-zone — the
    Jev Choice decides it. A judged ``dialogue`` must land the dialogue
    leaf, NOT research (no single global default track). Without a
    client the same utterance falls back to retrieval (covered by
    ``test_track_decision`` gray-zone units)."""
    model = ScriptedChatModel(script=[AIMessage(content="plain reply")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=FakeJevChoice([("dialogue", 0.9)]),
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
    assert out["track_decision"]["decision_tier"] == "jev"


def test_jev_clarify_follow_up_with_filled_slots_enters_research():
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
        jev_client=FakeJevChoice([("clarify", 0.99)]),
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
    assert second["track"] == "grounded"
