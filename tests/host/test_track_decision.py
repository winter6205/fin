"""Soft-Track T1: rules-primary ``decide_track`` unit contracts."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

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


def test_short_ambiguous_q_defaults_to_dialogue() -> None:
    """CRITICAL: no single global default track — the residual
    branch resolves bare ``q`` to dialogue, not research
    (specs/soft-track.md). ScriptedChatModel bounce suites that
    send HumanMessage('q') now exercise the dialogue leaf."""
    decision = decide_track([HumanMessage(content="q")])
    assert decision.track == "dialogue"
    assert decision.decision_tier == "conditional_policy"
    assert decision.signals.get("fallback") == "dialogue"


def test_filing_ask_decides_research() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "grounded"


def test_normalize_track_maps_research_alias() -> None:
    from fin.host.track_decision import normalize_track

    assert normalize_track("research") == "grounded"
    assert normalize_track("grounded") == "grounded"
    assert normalize_track("nope") is None


def test_underspecified_metric_ask_decides_clarify() -> None:
    """Filing-shaped ask missing company and/or period → clarify."""
    decision = decide_track([HumanMessage(content="What was revenue?")])
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("missing_slots")


class _StealClassify:
    """Classify stub whose invoke must exist as a method (not nested in __init__)."""

    def __init__(self, track: str, confidence: float = 0.99) -> None:
        self.calls = 0
        self._track = track
        self._confidence = confidence

    def invoke(self, messages, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return AIMessage(
            content=(
                f'{{"track": "{self._track}", '
                f'"confidence": {self._confidence}}}'
            )
        )


@pytest.mark.soft_track
def test_underspec_filing_ask_skips_llm_that_would_steal_research() -> None:
    """Production injects classify_model; MUST (2) still wins.

    LLM must not short-circuit inherit/clarify: a steal-to-research
    classifier on ``What was net sales?`` must land clarify with
    zero classify calls.
    """
    stub = _StealClassify("grounded")
    decision = decide_track(
        [HumanMessage(content="What was net sales?")],
        classify_model=stub,
    )
    assert stub.calls == 0
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("missing_slots")


@pytest.mark.soft_track
def test_underspec_followup_inherits_before_llm_steal() -> None:
    """Snapshot inherit MUST (5) also skips a steal-to-clarify LLM."""
    stub = _StealClassify("clarify")
    decision = decide_track(
        [HumanMessage(content="What was net sales?")],
        snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": ["AAPL-2023Q3"],
            }
        ],
        classify_model=stub,
    )
    assert stub.calls == 0
    assert decision.track == "grounded"
    assert decision.decision_tier == "inherit"


@pytest.mark.soft_track
def test_low_confidence_classify_routes_to_clarify() -> None:
    """An uncertain optional classifier must not select a track silently."""
    stub = _StealClassify("grounded", confidence=0.2)

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        classify_model=stub,
    )

    assert stub.calls == 1
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.confidence == 0.2
    assert decision.signals.get("classify_exit") == "low_confidence"
    assert decision.signals.get("llm_track") == "grounded"


def test_llm_research_alias_normalizes_to_grounded() -> None:
    stub = _StealClassify("research")
    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        classify_model=stub,
    )
    assert stub.calls == 1
    assert decision.track == "grounded"
    assert decision.signals.get("llm_track") == "grounded"


def test_metric_with_company_missing_period_decides_clarify() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales?")]
    )
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert "period" in (decision.signals.get("missing_slots") or [])


def test_metric_with_period_missing_company_decides_clarify() -> None:
    decision = decide_track(
        [HumanMessage(content="What was net sales in 2023 Q3?")]
    )
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert "company" in (decision.signals.get("missing_slots") or [])


def test_fully_specified_filing_ask_stays_research() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "grounded"


@pytest.mark.soft_track
@pytest.mark.parametrize("text", ["hmm", "ok thanks"])
def test_no_signal_residual_decides_dialogue_conditional_policy(text) -> None:
    """No single global default track (specs/soft-track.md): an
    utterance that matches no greeting, no off-domain, no filing ask,
    no research signal, and has no snapshot must resolve the residual
    conditional_policy branch to ``dialogue``, not ``research``."""
    decision = decide_track([HumanMessage(content=text)], classify_model=None)
    assert isinstance(decision, TrackDecision)
    assert decision.track == "dialogue"
    assert decision.decision_tier == "conditional_policy"
    assert decision.signals.get("fallback") == "dialogue"
