"""Soft-Track gray-zone exits (ADR 0080): gray-zone Jev tier exits and tier order."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.grounded.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.question.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_LABEL,
    JEV_CHOICE_EXIT_SCHEMA,
    JEV_CHOICE_EXIT_TRANSPORT,
    JevChoiceError,
)
from fin.host.grounded.track_decision import decide_track
from jev_fake import FakeJevChoice

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


@pytest.mark.parametrize(
    "exit_code",
    [
        JEV_CHOICE_EXIT_INVALID_LABEL,
        JEV_CHOICE_EXIT_SCHEMA,
        JEV_CHOICE_EXIT_TRANSPORT,
    ],
)
def test_jev_exit_codes_all_fall_back_to_retrieval(exit_code: str):
    """GE-1: any typed judge exit leaves the turn on the retrieval lane
    — never a pre-retrieval clarify, never a fall-through to a MiniMax
    JSON classify or a silent default track. The receipt still records
    WHY the judge decided nothing."""
    fake = FakeJevChoice([JevChoiceError("judge down", exit_code=exit_code)])
    decision = decide_track(
        [HumanMessage(content="hmm maybe this?")],
        jev_client=fake,
    )
    assert len(fake.calls) == 1
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals.get("jev_exit") == exit_code


def test_high_confidence_rules_skip_judge():
    """Greeting/off-domain/full research must not call the judge."""
    fake = FakeJevChoice([("grounded", 0.99)])
    greeting = decide_track(
        [HumanMessage(content="Hello!")],
        jev_client=fake,
    )
    assert greeting.track == "dialogue"
    assert greeting.decision_tier == "rules"

    off = decide_track(
        [HumanMessage(content="What's the weather in Seattle today?")],
        jev_client=fake,
    )
    assert off.track == "off_topic"
    assert fake.calls == []


def test_jev_valid_label_used_when_ambiguous():
    fake = FakeJevChoice([("dialogue", 0.7)])
    decision = decide_track(
        [HumanMessage(content="hmm maybe this?")],
        jev_client=fake,
    )
    assert len(fake.calls) == 1
    assert decision.track == "dialogue"
    assert decision.decision_tier == "jev"


def test_graph_jev_error_does_not_crash_and_reaches_grounded():
    """GE-1: an injected judge throw is contained at the cascade boundary
    — the turn reaches the grounded leaf (never a crash, never a
    pre-retrieval clarify). With no retrieval, the leaf's admission
    falls through to the citation gate unchanged."""
    fake = FakeJevChoice(
        [JevChoiceError("boom", exit_code=JEV_CHOICE_EXIT_TRANSPORT)]
    )
    model = ScriptedChatModel(
        script=[AIMessage(content="Soft reply after judge EXIT.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=fake,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="hmm maybe this?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-t4-exit"),
    )
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "jev_fallback"
    assert (
        out["track_decision"]["signals"].get("jev_exit") == "transport"
    )
    assert out["status"] == "refuse"
    # The gate DID run now (the turn is grounded, not a pre-retrieval
    # clarify): the answer cites nothing, so the gate bounces it.
    assert HIDDEN_BOUNCE_MARKER in str(out.get("messages", []))
    assert out.get("citations", ()) == () or out.get("citations") == []


def test_scripted_research_q_rules_path_without_client():
    """Default compile (no client): a fully-slotted ask stays rules."""
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
