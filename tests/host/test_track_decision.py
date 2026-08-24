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


def test_underspecified_metric_ask_decides_clarify() -> None:
    """Filing-shaped ask missing company and/or period → clarify (T2)."""
    decision = decide_track([HumanMessage(content="What was revenue?")])
    assert decision.track == "clarify"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("missing_slots")


def test_metric_with_company_missing_period_decides_clarify() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales?")]
    )
    assert decision.track == "clarify"
    assert "period" in (decision.signals.get("missing_slots") or [])


def test_metric_with_period_missing_company_decides_clarify() -> None:
    decision = decide_track(
        [HumanMessage(content="What was net sales in 2023 Q3?")]
    )
    assert decision.track == "clarify"
    assert "company" in (decision.signals.get("missing_slots") or [])


def test_fully_specified_filing_ask_stays_research() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "research"
