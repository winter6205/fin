"""Soft-Track T1: rules-primary ``decide_track`` unit contracts."""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from fin.host.track_decision import TrackDecision, decide_track


def test_greeting_decides_dialogue_rules_tier() -> None:
    decision = decide_track([HumanMessage(content="Hello!")])
    assert isinstance(decision, TrackDecision)
    assert decision.track == "dialogue"
    assert decision.decision_tier == "rules"
    assert decision.confidence >= 0.8
    assert decision.signals.get("greeting") is True


def test_off_domain_decides_off_topic_rules_tier() -> None:
    decision = decide_track(
        [HumanMessage(content="What's the weather in Seattle today?")]
    )
    assert decision.track == "off_topic"
    assert decision.decision_tier == "rules"
    assert decision.confidence >= 0.8
    assert decision.signals.get("off_domain") is True


def test_short_ambiguous_q_defaults_to_research() -> None:
    """CRITICAL: existing ScriptedChatModel bounce suites use HumanMessage('q')."""
    decision = decide_track([HumanMessage(content="q")])
    assert decision.track == "research"
    assert decision.decision_tier in ("rules", "conditional_policy")


def test_filing_ask_decides_research() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "research"


def test_decide_track_does_not_route_clarify_yet() -> None:
    """T2 owns clarify; T1 must not emit track=clarify."""
    decision = decide_track(
        [HumanMessage(content="What was revenue?")]  # underspecified filing cue
    )
    assert decision.track != "clarify"
