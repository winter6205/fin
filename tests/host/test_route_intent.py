"""T8 route-intent contracts: four wire values, three sources, no fifth.

``route intent`` is the product action selected for one input, distinct
from the graph ``track`` (spec ``qa-chain-grounded-experience`` "Routing
requirements"). This file pins the pure projection from a
``TrackDecision`` onto ``(RouteIntent, RouteSource)`` and the two seams
the graph wiring owns later: the Jev-failure fallback and the
policy-refusal refinement.

The policy-refusal path is deliberately narrow. A receipt must not
invent a refusal that no system/provider policy actually stated, so
these tests pin that ``retrieve``/``fallback`` survives a Jev outage and
that only an observed refusal refines the intent.
"""

from __future__ import annotations

import pytest

from fin.host.question.jev_choice import (
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_NO_JEV_CLIENT,
    JEV_CHOICE_EXIT_TRANSPORT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
)
from fin.host.grounded.route_intent import (
    BARE_CATALOG_VIEW_RE,
    RouteDecision,
    decide_route_intent,
    is_bare_catalog_view,
    normalize_route_intent,
    normalize_route_source,
)
from fin.host.grounded.track_decision import TrackDecision


def _decision(
    track: str,
    tier: str,
    signals: dict[str, object] | None = None,
) -> TrackDecision:
    return TrackDecision(
        track=track,  # type: ignore[arg-type]
        confidence=0.9,
        decision_tier=tier,  # type: ignore[arg-type]
        signals=dict(signals or {}),
    )


# ----- closed wire values -----

_ROUTE_INTENTS = ("retrieve", "overview", "chat", "policy_refuse")
_ROUTE_SOURCES = ("rule", "jev", "fallback")


@pytest.mark.parametrize("intent", _ROUTE_INTENTS)
@pytest.mark.parametrize("source", _ROUTE_SOURCES)
def test_normalizers_accept_the_full_closed_intent_source_matrix(
    intent: str, source: str
) -> None:
    """Contract: exactly four intents x three sources are valid wire
    values, so every pair is normalizable even when no tier produces it
    (``policy_refuse`` never comes from a track decision)."""
    assert normalize_route_intent(intent) == intent
    assert normalize_route_source(source) == source


@pytest.mark.parametrize("raw", ["", "   ", "nope", "evidence_refuse", "clarify", 1, None, b"chat", ["chat"]])
def test_normalizers_reject_unknown_blank_and_non_string_input(raw: object) -> None:
    """Contract: a malformed or unknown wire value normalizes to None
    rather than to a default intent, so an unread receipt cannot be
    mistaken for a decided one."""
    assert normalize_route_intent(raw) is None
    assert normalize_route_source(raw) is None


def test_normalizers_accept_case_and_padding_variants() -> None:
    """Contract: the accepted normal form is case-insensitive and
    whitespace-tolerant, because the value crosses a wire boundary."""
    assert normalize_route_intent("  Overview ") == "overview"
    assert normalize_route_intent("POLICY_REFUSE") == "policy_refuse"
    assert normalize_route_source(" Fallback ") == "fallback"


# ----- chat -----

def test_greeting_is_chat_from_rules() -> None:
    """Contract: a greeting is ``chat`` decided by ``rule`` — the track
    cascade's own high-confidence rules, not a judge."""
    route = decide_route_intent(_decision("dialogue", "rules", {"greeting": True}))

    assert route == RouteDecision(intent="chat", source="rule")


def test_off_topic_is_chat_and_not_a_policy_refusal() -> None:
    """Contract: off-topic is ``chat``. It is an out-of-domain answer,
    not a refusal under system/provider policy, so the receipt must not
    carry ``policy_refuse``."""
    route = decide_route_intent(
        _decision("off_topic", "rules", {"off_domain": True})
    )

    assert route == RouteDecision(intent="chat", source="rule")
    assert route.intent != "policy_refuse"


def test_empty_utterance_dialogue_residual_is_chat_from_rules() -> None:
    """Contract: the ``conditional_policy`` dialogue residual (empty
    text cannot be sent to the judge) is deterministic, so its source is
    ``rule`` — a residual is not a fallback."""
    route = decide_route_intent(
        _decision("dialogue", "conditional_policy", {"fallback": "dialogue"})
    )

    assert route == RouteDecision(intent="chat", source="rule")


def test_jev_tier_dialogue_is_chat_from_jev() -> None:
    """Contract: a usable judge hint keeps its own attribution — a
    ``dialogue`` label the judge reached is ``chat`` from ``jev``."""
    route = decide_route_intent(
        _decision("dialogue", "jev", {"jev_label": "dialogue", "jev_confidence": 0.8})
    )

    assert route == RouteDecision(intent="chat", source="jev")


# ----- overview -----

def test_corpus_inventory_signal_is_overview_from_rules() -> None:
    """Contract: the existing Chinese catalog-discovery fast rule
    (``corpus_inventory``) enumerates the corpus, so its product intent
    is ``overview`` rather than a generic retrieve."""
    route = decide_route_intent(
        _decision("grounded", "rules", {"corpus_inventory": True})
    )

    assert route == RouteDecision(intent="overview", source="rule")


def test_delegated_scope_signal_is_overview_from_rules() -> None:
    """Contract: an operator-delegated scope hands the choice back to
    fin, which answers by enumerating what is available — the same
    catalog-discovery product action."""
    route = decide_route_intent(
        _decision("grounded", "rules", {"delegated_scope": True})
    )

    assert route == RouteDecision(intent="overview", source="rule")


def test_bare_catalog_view_signal_promotes_dialogue_to_overview() -> None:
    """Contract: a whole-utterance "show me what exists" ask reaches the
    dialogue residual today; the graph wiring sets ``bare_catalog_view``
    and this projection turns it into the catalog overview, still
    attributed to ``rule``."""
    route = decide_route_intent(
        _decision("dialogue", "conditional_policy", {"bare_catalog_view": True})
    )

    assert route == RouteDecision(intent="overview", source="rule")


# ----- retrieve -----

def test_ordinary_grounded_rules_route_is_retrieve_from_rules() -> None:
    """Contract: a grounded rules decision with no catalog signal is an
    ordinary knowledge question."""
    route = decide_route_intent(
        _decision("grounded", "rules", {"filing_ask": True})
    )

    assert route == RouteDecision(intent="retrieve", source="rule")


def test_inherit_tier_is_retrieve_from_rules() -> None:
    """Contract: an inherited short follow-up reuses the snapshot's
    grounded lane deterministically, so it is ``rule``-sourced."""
    route = decide_route_intent(
        _decision("grounded", "inherit", {"inherit": True})
    )

    assert route == RouteDecision(intent="retrieve", source="rule")


def test_jev_tier_grounded_is_retrieve_from_jev() -> None:
    """Contract: a usable judge hint that names the grounded leaf keeps
    the judge as the recorded source."""
    route = decide_route_intent(
        _decision("grounded", "jev", {"jev_label": "grounded", "jev_confidence": 0.9})
    )

    assert route == RouteDecision(intent="retrieve", source="jev")


def test_jev_tier_clarify_without_missing_slots_is_retrieve_from_jev() -> None:
    """Contract: a usable ``clarify`` hint that identifies no actionable
    missing user condition does not become a product intent — the
    underlying knowledge task retrieves."""
    route = decide_route_intent(
        _decision("clarify", "jev", {"jev_label": "clarify", "jev_confidence": 0.9})
    )

    assert route == RouteDecision(intent="retrieve", source="jev")


def test_jev_clarify_with_actionable_slots_keeps_retrieve_intent() -> None:
    """A Jev clarification is a task question within retrieve intent."""
    route = decide_route_intent(
        _decision(
            "clarify",
            "jev",
            {"filing_ask": True, "missing_slots": ["company", "period"]},
        )
    )

    assert route == RouteDecision(intent="retrieve", source="jev")
    assert route.intent != "policy_refuse"


def test_missing_slots_signal_stays_reachable_on_the_decision() -> None:
    """Contract: projecting to a route intent must not consume or rewrite
    the finer track signals — the canonical task contract reads
    ``missing_slots`` from the same decision."""
    decision = _decision(
        "clarify", "clarify", {"missing_slots": ["company"]}
    )

    decide_route_intent(decision)

    assert decision.signals["missing_slots"] == ["company"]


# ----- Jev failure fallback -----

@pytest.mark.parametrize(
    "exit_code",
    [
        JEV_CHOICE_EXIT_NO_JEV_CLIENT,
        JEV_CHOICE_EXIT_TRANSPORT,
        JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
        JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    ],
)
def test_every_jev_failure_exit_falls_back_to_retrieve(exit_code: str) -> None:
    """Contract: Jev absent, a typed exit, an unexpected throw and a
    sub-threshold hint all mean the judge decided nothing, so the turn
    falls back to authorized retrieval — never to a refusal and never to
    an evidence refusal before retrieval happens."""
    route = decide_route_intent(
        _decision("clarify", "clarify", {"jev_exit": exit_code})
    )

    assert route == RouteDecision(intent="retrieve", source="fallback")
    assert route.intent not in {"policy_refuse", "evidence_refuse"}


@pytest.mark.parametrize(
    "exit_code",
    [
        JEV_CHOICE_EXIT_NO_JEV_CLIENT,
        JEV_CHOICE_EXIT_TRANSPORT,
        JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
        JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    ],
)
def test_jev_failure_refusal_refinement_does_not_fire_without_an_observed_refusal(
    exit_code: str,
) -> None:
    """Contract: a Jev outage is not a policy refusal. With no observed
    system/provider refusal the refine seam returns None and the fallback
    route stands."""
    route = decide_route_intent(
        _decision("clarify", "clarify", {"jev_exit": exit_code})
    )

    assert route.refine_for_policy_refusal(False) is None
    assert route == RouteDecision(intent="retrieve", source="fallback")


def test_jev_failure_with_absent_text_still_falls_back_to_retrieve() -> None:
    """Contract: the fallback does not depend on the utterance being
    readable — an empty/absent text with a Jev exit still retrieves."""
    route = decide_route_intent(
        _decision("clarify", "clarify", {"utterance_len": 0, "jev_exit": JEV_CHOICE_EXIT_NO_JEV_CLIENT})
    )

    assert route == RouteDecision(intent="retrieve", source="fallback")


# ----- policy refusal -----

@pytest.mark.parametrize("source", _ROUTE_SOURCES)
def test_observed_policy_refusal_refines_intent_and_keeps_source(source: str) -> None:
    """Contract: only an observed system/model-policy refusal establishes
    ``policy_refuse``, and it refines the intent without rewriting how the
    turn was routed — so the receipt still records rule/jev/fallback."""
    prior = RouteDecision(intent="retrieve", source=source)  # type: ignore[arg-type]

    refined = prior.refine_for_policy_refusal(True)

    assert refined == RouteDecision(intent="policy_refuse", source=source)


# ----- bare catalog view predicate -----

@pytest.mark.parametrize(
    "text",
    [
        "看看",
        "看一下",
        "看一下资料",
        "瞧瞧",
        "瞅瞅",
        "浏览一下",
        "先看看",
        "帮我看看",
        "有什么资料",
        "有哪些资料",
        "看看。",
        "看看?",
        "  看看！  ",
    ],
)
def test_bare_catalog_view_matches_a_whole_utterance_look(text: str) -> None:
    """Contract: the acceptance asks "看看" show the overview, so the
    predicate matches a whole utterance that only asks to look at what
    exists, trailing punctuation included."""
    assert is_bare_catalog_view(text)
    assert BARE_CATALOG_VIEW_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "看看第三季度的收入",
        "今年年假几天",
        "看看 AAPL 2023 Q3 的 net sales",
        "帮我看看判官公司的收入",
        "",
        "有什么风险",
    ],
)
def test_bare_catalog_view_rejects_an_utterance_with_other_content(text: str) -> None:
    """Contract: the predicate is anchored to the whole utterance, so an
    ask that also names a subject stays an ordinary question and is not
    swept into the catalog overview."""
    assert not is_bare_catalog_view(text)


# ----- closed value space -----

@pytest.mark.parametrize(
    "track",
    ["dialogue", "clarify", "off_topic", "grounded", "research", "nope", ""],
)
def test_no_track_string_produces_a_fifth_intent(track: str) -> None:
    """Contract: the projection is total over any track string and its
    image stays inside the four declared intents."""
    route = decide_route_intent(_decision(track, "rules"))

    assert route.intent in _ROUTE_INTENTS
    assert route.source in _ROUTE_SOURCES
