"""Soft-Track T1/T2: rules-primary ``decide_track`` unit contracts.

The gray-zone tier is a Jev closed-set Choice (ADR 0080): the MiniMax
JSON ``classify_model`` path is gone from the track cascade, so a
langchain-shaped classifier is no longer accepted by ``decide_track``
at all (signature-level proof).

This file also carries the track lane's mis-wired-criteria containment
pin at the DEFAULT gate (no ``soft_track`` mark), so a criteria wiring
break fails a plain ``uv run pytest -q`` run rather than hiding behind
the opt-in marks of ``test_track_decision_t7.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_LABEL,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoiceClient,
    JevChoiceError,
)
from fin.host.track_decision import (
    _JEV_TRACK_LEAF_CRITERIA,
    _JEV_TRACK_OPTIONS,
    TrackDecision,
    decide_track,
)
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
def test_gray_zone_instructions_teach_every_leaf() -> None:
    """The track mirror of ``test_choice_instructions_teach_each_operator_label``
    in ``tests/host/test_search_mode_gray_zone_jev.py``: every leaf of the
    closed set — read from ``_JEV_TRACK_OPTIONS``, not re-spelled here —
    must appear in the instruction text the cascade ACTUALLY sends, and no
    leaf body may: a body pasted into the prose would be a second copy of
    the rule the criteria slot owns (the drift the ADR 0081 slot A/B
    retired). Fake-recorded, so this pins the request, not a constant."""
    fake = FakeJevChoice([("off_topic", 0.9)])
    decide_track(
        [HumanMessage(content="maybe compare these?")], jev_client=fake
    )
    call = fake.calls[0]
    for leaf in _JEV_TRACK_OPTIONS:
        assert leaf in call["instructions"]
    for leaf, body in _JEV_TRACK_LEAF_CRITERIA.items():
        assert call["criteria"][leaf] == body
        assert body not in call["instructions"]


def test_miswired_leaf_criteria_is_contained_as_an_exit(
    monkeypatch: pytest.MonkeyPatch,
    jev_never_sent_client: tuple[JevChoiceClient, list[dict[str, Any]]],
) -> None:
    """The track cascade's own landing point for a criteria table that
    stopped covering the closed set — what a leaf added to
    ``_JEV_TRACK_OPTIONS`` without its body leaves behind. The real client
    refuses to send a malformed question (``ValueError``), so the cascade
    still fail-closes to clarify and the audit names the throw as a wiring
    bug, never as a judge fact. The never-sent proof is the explicit
    ``sent == []`` count on the shared fixture transport.

    This pin sits at the DEFAULT gate on purpose (see this file's
    docstring); the 档 lane's twin is
    ``test_miswired_criteria_is_contained_as_an_exit`` in
    ``tests/host/test_search_mode_cascade.py``.
    """
    client, sent = jev_never_sent_client
    monkeypatch.delitem(_JEV_TRACK_LEAF_CRITERIA, "grounded")

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=client,
    )

    assert sent == []  # explicit count: nothing reached the transport
    assert decision.track == "clarify"
    assert decision.decision_tier == "clarify"
    assert decision.signals["jev_exit"] == JEV_CHOICE_EXIT_UNEXPECTED_ERROR
    assert decision.signals["jev_error_type"] == "ValueError"


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
