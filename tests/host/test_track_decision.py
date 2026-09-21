"""Soft-Track T1/T2: rules-primary ``decide_track`` unit contracts.

The gray-zone tier is a Jev closed-set Choice (ADR 0080): the MiniMax
JSON ``classify_model`` path is gone from the track cascade, so a
langchain-shaped classifier is no longer accepted by ``decide_track``
at all (signature-level proof).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_LABEL,
    JevChoiceError,
)
from fin.host.track_decision import TrackDecision, decide_track
from jev_fake import FakeJevChoice


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


def test_short_ambiguous_q_without_client_clarifies() -> None:
    """ADR 0080: a key-less boot fail-closes the gray zone to
    ``clarify`` — a missing ``FIN_JEV_API_KEY`` never silently picks
    ``dialogue`` or ``grounded`` for an ambiguous utterance. Only an
    utterance the judge cannot be asked about (empty text) still
    resolves to the ``conditional_policy`` dialogue residual."""
    decision = decide_track([HumanMessage(content="q")])
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("jev_exit") == "no_jev_client"


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


@pytest.mark.soft_track
def test_underspec_filing_ask_skips_judge_that_would_steal_research() -> None:
    """Production injects a Jev client; MUST (2) still wins.

    The judge must not short-circuit inherit/clarify: a
    steal-to-grounded Fake on ``What was net sales?`` must land clarify
    with zero judge calls.
    """
    fake = FakeJevChoice([("grounded", 0.99)])
    decision = decide_track(
        [HumanMessage(content="What was net sales?")],
        jev_client=fake,
    )
    assert fake.calls == []
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("missing_slots")


@pytest.mark.soft_track
def test_underspec_followup_inherits_before_judge_steal() -> None:
    """Snapshot inherit MUST (5) also skips a steal-to-clarify judge."""
    fake = FakeJevChoice([("clarify", 0.99)])
    decision = decide_track(
        [HumanMessage(content="What was net sales?")],
        snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": ["AAPL-2023Q3"],
            }
        ],
        jev_client=fake,
    )
    assert fake.calls == []
    assert decision.track == "grounded"
    assert decision.decision_tier == "inherit"


@pytest.mark.soft_track
def test_gray_zone_asks_the_closed_set_once() -> None:
    """Ambiguous utterance → one Choice over the four tracks."""
    fake = FakeJevChoice([("off_topic", 0.9)])
    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["utterance"] == "maybe compare these?"
    assert set(call["options"]) == {
        "dialogue",
        "clarify",
        "off_topic",
        "grounded",
    }
    assert call["question_id"] == "track"
    assert decision.track == "off_topic"
    assert decision.decision_tier == "jev"


@pytest.mark.soft_track
def test_low_confidence_judge_routes_to_clarify() -> None:
    """An uncertain judge must not select a track silently."""
    fake = FakeJevChoice([("grounded", 0.2)])

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )

    assert len(fake.calls) == 1
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.confidence == 0.2
    assert decision.signals.get("jev_exit") == "low_confidence"
    assert decision.signals.get("jev_label") == "grounded"


@pytest.mark.soft_track
def test_judge_invalid_label_exit_routes_to_clarify() -> None:
    """A compliant client never returns an open-set label; a raised
    ``invalid_label`` exit fail-closes to clarify (ADR 0080)."""
    fake = FakeJevChoice(
        [JevChoiceError("open set", exit_code=JEV_CHOICE_EXIT_INVALID_LABEL)]
    )
    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )
    assert decision.track == "clarify"
    assert decision.signals.get("jev_exit") == "invalid_label"


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
def test_no_signal_residual_without_client_clarifies(text) -> None:
    """ADR 0080 "missing key is clarify": an ambiguous utterance with
    no Jev client fail-closes to ``clarify`` — the MiniMax-era silent
    ``dialogue`` residual is gone for non-empty text."""
    decision = decide_track([HumanMessage(content=text)], jev_client=None)
    assert isinstance(decision, TrackDecision)
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("jev_exit") == "no_jev_client"


def test_empty_utterance_keeps_conditional_policy_dialogue() -> None:
    """The judge cannot be asked about empty material: only that case
    still resolves to the dialogue residual."""
    decision = decide_track([HumanMessage(content="   ")])
    assert decision.track == "dialogue"
    assert decision.decision_tier == "conditional_policy"
    assert decision.signals.get("fallback") == "dialogue"


def test_decide_track_rejects_langchain_shaped_classify_model() -> None:
    """Compile-time signature proof: no MiniMax JSON classify tier is
    reachable from ``decide_track`` any more (ADR 0080)."""
    with pytest.raises(TypeError):
        decide_track(
            [HumanMessage(content="q")],
            classify_model=object(),  # type: ignore[call-arg]
        )
