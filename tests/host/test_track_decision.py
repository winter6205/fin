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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host.question.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_LABEL,
    JEV_CHOICE_EXIT_NO_JEV_CLIENT,
    JEV_CHOICE_EXIT_TRANSPORT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoiceClient,
    JevChoiceError,
)
from fin.corpus.catalog import record_filings
from fin.host import ScriptedChatModel, compile_graph
from fin.host.grounded.track_decision import (
    _JEV_TRACK_LEAF_CRITERIA,
    _JEV_TRACK_OPTIONS,
    TrackDecision,
    decide_track,
)
from fin.host.state import reset_input_state
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


def test_short_ambiguous_q_without_client_retrieves() -> None:
    """GE-1: a key-less boot sends an ambiguous utterance down the
    RETRIEVAL lane, not to ``clarify``. Judge-down is not a missing user
    slot, and GE-2 counts a refusal only after ≥1 actual retrieval. Only
    an utterance the judge cannot be asked about (empty text) still
    resolves to the ``conditional_policy`` dialogue residual."""
    decision = decide_track([HumanMessage(content="q")])
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals.get("jev_exit") == "no_jev_client"


def test_filing_ask_decides_research() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "grounded"


# ----- GE-1: a Jev exit reaches retrieval, never a pre-retrieval clarify -----

_ORDINARY_QUESTION = "年假几天"


def test_ordinary_question_without_jev_client_reaches_retrieval() -> None:
    """GE-1 (specs/qa-chain-grounded-experience.md:93): "an ordinary
    question with Jev absent/error retrieves and cites". A missing
    client must leave the turn on the retrieval lane — it is not a
    missing user slot, and GE-2 counts a refusal only AFTER ≥1 actual
    retrieval, which a pre-retrieval clarify forecloses."""
    decision = decide_track([HumanMessage(content=_ORDINARY_QUESTION)])

    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    # GE-1: the receipt still records WHY the judge did not decide.
    assert decision.signals.get("jev_exit") == JEV_CHOICE_EXIT_NO_JEV_CLIENT


@pytest.mark.soft_track
def test_every_jev_exit_reaches_retrieval_rather_than_clarify() -> None:
    """Missing-slot financial asks still retrieve when Jev cannot decide."""
    for fake in (
        FakeJevChoice([JevChoiceError("down", exit_code=JEV_CHOICE_EXIT_TRANSPORT)]),
        _RaisingJevChoice(),
        FakeJevChoice([("grounded", 0.2)]),
    ):
        decision = decide_track(
            [
                HumanMessage(
                    content="What was Quasar Beacon Systems quarterly revenue?"
                )
            ],
            jev_client=fake,
        )
        assert decision.track == "grounded", decision.signals
        assert decision.decision_tier == "jev_fallback", decision.signals
        assert decision.signals.get("jev_exit"), decision.signals
        assert "missing_slots" not in decision.signals


class _RaisingJevChoice:
    """A client that throws a non-typed error out of ``choose``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def choose(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        raise RuntimeError("classifier unavailable")


def test_a_jev_exit_is_never_chat_or_off_topic() -> None:
    """Boundary (GE-1): retrieval is the only residual. Falling back to
    ``dialogue``/``off_topic`` would answer an ordinary question with no
    retrieval attempt at all — the silent-default the judge-down case
    must not produce."""
    decision = decide_track([HumanMessage(content=_ORDINARY_QUESTION)])

    assert decision.track not in ("dialogue", "off_topic", "clarify")


def test_normalize_track_maps_research_alias() -> None:
    from fin.host.grounded.track_decision import normalize_track

    assert normalize_track("research") == "grounded"
    assert normalize_track("grounded") == "grounded"
    assert normalize_track("nope") is None


def test_unscoped_financial_question_without_jev_reaches_retrieval() -> None:
    """A missing company/year must not block ordinary knowledge retrieval."""
    question = (
        "What was Quasar Beacon Systems' quarterly subscription revenue? "
        'Search the exact phrase "Quasar Beacon Systems quarterly '
        'subscription revenue".'
    )
    decision = decide_track([HumanMessage(content=question)])

    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals.get("jev_exit") == JEV_CHOICE_EXIT_NO_JEV_CLIENT
    assert "missing_slots" not in decision.signals


def test_underspecified_metric_ask_without_jev_uses_retrieval() -> None:
    """A metric without company or period still receives retrieval."""
    decision = decide_track([HumanMessage(content="What was revenue?")])

    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals.get("jev_exit") == JEV_CHOICE_EXIT_NO_JEV_CLIENT
    assert "missing_slots" not in decision.signals


@pytest.mark.soft_track
def test_underspec_filing_ask_reaches_jev_before_route() -> None:
    """Missing financial slots cannot bypass the optional Jev decision."""
    fake = FakeJevChoice([("grounded", 0.99)])
    decision = decide_track(
        [HumanMessage(content="What was net sales?")],
        jev_client=fake,
    )

    assert len(fake.calls) == 1
    assert fake.calls[0]["utterance"] == "What was net sales?"
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev"
    assert "missing_slots" not in decision.signals


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


def test_jev_explicit_clarify_hint_reaches_canonical_track() -> None:
    fake = FakeJevChoice([("clarify", 0.99)])
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales?")],
        jev_client=fake,
    )

    assert len(fake.calls) == 1
    assert decision.track == "clarify"
    assert decision.decision_tier == "jev"
    assert decision.signals.get("missing_slots") == ["period"]


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
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals["jev_exit"] == JEV_CHOICE_EXIT_UNEXPECTED_ERROR
    assert decision.signals["jev_error_type"] == "ValueError"


@pytest.mark.soft_track
def test_low_confidence_judge_falls_back_to_retrieval() -> None:
    """An uncertain judge must not select a track silently — it falls
    back to retrieval (GE-1), keeping its hint and confidence so the
    receipt shows what the judge was leaning towards."""
    fake = FakeJevChoice([("grounded", 0.2)])

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )

    assert len(fake.calls) == 1
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.confidence == 0.2
    assert decision.signals.get("jev_exit") == "low_confidence"
    assert decision.signals.get("jev_label") == "grounded"


@pytest.mark.soft_track
def test_judge_invalid_label_exit_falls_back_to_retrieval() -> None:
    """A compliant client never returns an open-set label; a raised
    ``invalid_label`` exit falls back to retrieval (GE-1) rather than
    asking the user a question the judge failed to decide."""
    fake = FakeJevChoice(
        [JevChoiceError("open set", exit_code=JEV_CHOICE_EXIT_INVALID_LABEL)]
    )
    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )
    assert decision.track == "grounded"
    assert decision.signals.get("jev_exit") == "invalid_label"


def test_metric_with_company_missing_period_retrieves_without_jev() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales?")]
    )

    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals.get("jev_exit") == JEV_CHOICE_EXIT_NO_JEV_CLIENT
    assert "missing_slots" not in decision.signals


def test_metric_with_period_missing_company_retrieves_without_jev() -> None:
    decision = decide_track(
        [HumanMessage(content="What was net sales in 2023 Q3?")]
    )

    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
    assert decision.signals.get("jev_exit") == JEV_CHOICE_EXIT_NO_JEV_CLIENT
    assert "missing_slots" not in decision.signals


def test_fully_specified_filing_ask_stays_research() -> None:
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    )
    assert decision.track == "grounded"


@pytest.mark.soft_track
@pytest.mark.parametrize("text", ["hmm", "ok thanks"])
def test_no_signal_residual_without_client_retrieves(text) -> None:
    """GE-1: an ambiguous utterance with no Jev client falls back to
    RETRIEVAL — the judge-down residual is the retrieval lane, and the
    MiniMax-era silent ``dialogue`` residual stays gone for non-empty
    text."""
    decision = decide_track([HumanMessage(content=text)], jev_client=None)
    assert isinstance(decision, TrackDecision)
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
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


# ----- E2: enterprise-material routing rule (step E2) -----

_EXPENSE_PRESENT = (
    "According to the corporate travel expense policy, what is the"
    " maximum reimbursable amount for a working business dinner per"
    " person?"
)
_EXPENSE_ABSENT = (
    "According to the corporate travel expense policy, what is the"
    " nightly lodging cap for a family member who accompanies an"
    " employee on a business trip?"
)
_EXPENSE_AMOUNT = (
    "Under the corporate travel expense policy, may an employee"
    " expense a $95 business dinner, or is the per-person cap"
    " exceeded?"
)


@pytest.mark.parametrize(
    "question", [_EXPENSE_PRESENT, _EXPENSE_ABSENT, _EXPENSE_AMOUNT]
)
def test_enterprise_material_question_is_grounded_rules_tier(
    question: str,
) -> None:
    """A question anchored to an authorized enterprise document is a
    knowledge-QA ask: it routes grounded at the rules tier and never
    reaches the fail-closed gray-zone clarify (spec
    enterprise-evidence-contract E2; ADR 0090)."""
    decision = decide_track([HumanMessage(content=question)])
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("enterprise_material") is True
    assert "jev_exit" not in decision.signals


@pytest.mark.soft_track
def test_enterprise_material_rule_beats_steal_to_clarify_judge() -> None:
    """The rules tier owns the enterprise-material ask: a Jev client
    voting ``clarify`` cannot demote it (precedence, ADR 0080 tier 1
    over tier 2)."""
    fake = FakeJevChoice([("clarify", 0.99)])
    decision = decide_track(
        [HumanMessage(content=_EXPENSE_ABSENT)], jev_client=fake
    )
    assert fake.calls == []
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"


def test_off_domain_question_with_expense_policy_word_keeps_off_topic() -> None:
    """Precedence inside the rules tier: a weather ask that merely
    contains the phrase is not an enterprise-material request; the
    earlier off-domain rule still wins."""
    decision = decide_track(
        [HumanMessage(content="weather in Seattle and the corporate travel expense policy")]
    )
    assert decision.track == "off_topic"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("off_domain") is True


def test_filing_ask_routing_untouched_by_enterprise_rule() -> None:
    """Retention: the financial lane routes exactly as before — the
    enterprise rule is additive and never fires on a filing ask."""
    decision = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2022 Q3?")]
    )
    assert decision.track == "grounded"
    assert decision.signals.get("enterprise_material") is None
    assert decision.signals.get("filing_ask") is True


@pytest.mark.parametrize("text", ["", "   ", "policy", "the policy?"])
def test_enterprise_rule_needs_a_real_utterance_and_a_named_modifier(
    text: str,
) -> None:
    """Empty / whitespace input keeps the conditional-policy residual;
    a bare "policy" with no enterprise-material modifier (corporate /
    company / travel / expense / employee) keeps the gray zone — with
    no client it falls back to retrieval, and the enterprise signal
    itself must stay absent.
    """
    decision = decide_track([HumanMessage(content=text)], jev_client=None)
    if not text.strip():
        assert decision.track == "dialogue"
        assert decision.decision_tier == "conditional_policy"
    else:
        assert decision.track == "grounded"
        assert decision.decision_tier == "jev_fallback"
        assert decision.signals.get("jev_exit") == "no_jev_client"
        assert decision.signals.get("enterprise_material") is None


# ----- Attachment-as-source: upload frame, not the bare noun 附件 -----

_ATTACHMENT_ASK_FRAME = "根据这份费用政策，工作午餐的报销上限是多少美元？晚餐呢？"
_ATTACHMENT_ASK_NAMED = "根据这份附件，报告里规定的差旅住宿上限是多少美元？"
_ATTACHMENT_ASK_BARE = "这份费用政策规定工作午餐的报销上限是多少美元？请引用原文。"
_ATTACHMENT_ASK_EN = (
    "According to the attached file, what is the nightly lodging cap for "
    "a family member accompanying an employee?"
)
_ATTACHMENT_ASK_UPLOAD = "请看上传的附件，工作午餐的报销上限是多少美元？"


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(_ATTACHMENT_ASK_FRAME, id="source-frame-demonstrative"),
        pytest.param(_ATTACHMENT_ASK_NAMED, id="named-attachment"),
        pytest.param(_ATTACHMENT_ASK_BARE, id="bare-demonstrative"),
        pytest.param(_ATTACHMENT_ASK_EN, id="english-attached-file"),
        pytest.param(_ATTACHMENT_ASK_UPLOAD, id="upload-frame"),
    ],
)
def test_an_attachment_as_source_ask_is_grounded_rules_tier(question: str) -> None:
    """An ask whose answer comes from material uploaded in this thread
    is grounded at the rules tier. A bare document word is not enough;
    the upload frame, a source frame, or a demonstrative reference is.
    """
    decision = decide_track([HumanMessage(content=question)])
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("attachment_source") is True
    assert "jev_exit" not in decision.signals


_DRAFT_A_REPORT = "请帮我起草一份季度经营分析报告的大纲，并列出每章要点。"
_MENTION_AN_ATTACHMENT_A = "请帮我起草一封致客户的通知，作为邮件附件发送。"
_MENTION_AN_ATTACHMENT_B = "帮我写个邮件，附件里附上季度数据。"
_ATTACHMENT_ASK_TICKERED = (
    "根据这份附件，Apple 的 Trade Receivables 中哪类客户占了最大比例？"
    "具体占比是多少？"
)


@pytest.mark.parametrize(
    ("label", "confidence"),
    [
        pytest.param("clarify", 0.25, id="real-judge-turn-1"),
        pytest.param("clarify", 0.30, id="real-judge-turn-2"),
        pytest.param("off_topic", 0.37, id="f3-recorded-turn"),
    ],
)
def test_the_attachment_rule_beats_the_judge_that_fail_closed_it(
    label: str, confidence: float
) -> None:
    """The rules tier owns the attachment ask, so a judge voting the
    other way is never called."""
    fake = FakeJevChoice([(label, confidence)])
    decision = decide_track(
        [HumanMessage(content=_ATTACHMENT_ASK_FRAME)], jev_client=fake
    )
    assert fake.calls == []
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("attachment_source") is True


def test_a_non_attachment_document_ask_is_untouched_by_the_attachment_rule() -> None:
    """起草 a report names a document as the object of the turn, not
    as uploaded source material."""
    keyless = decide_track([HumanMessage(content=_DRAFT_A_REPORT)], jev_client=None)
    assert keyless.track == "grounded"
    assert keyless.decision_tier == "jev_fallback"
    assert keyless.signals.get("jev_exit") == "no_jev_client"
    assert "attachment_source" not in keyless.signals

    judged = decide_track(
        [HumanMessage(content=_DRAFT_A_REPORT)],
        jev_client=FakeJevChoice([("dialogue", 0.81)]),
    )
    assert judged.track == "dialogue"
    assert judged.decision_tier == "jev"
    assert judged.confidence == 0.81


@pytest.mark.parametrize(
    "question",
    [
        pytest.param(_MENTION_AN_ATTACHMENT_A, id="as-the-email-attachment"),
        pytest.param(_MENTION_AN_ATTACHMENT_B, id="put-it-in-the-attachment"),
    ],
)
def test_an_ask_that_only_mentions_an_attachment_is_not_an_attachment_source(
    question: str,
) -> None:
    """Naming 附件 as the output of a 起草/写 ask is not a claim that
    the answer comes from material uploaded in this thread."""
    keyless = decide_track([HumanMessage(content=question)], jev_client=None)
    assert keyless.track == "grounded"
    assert keyless.decision_tier == "jev_fallback"
    assert keyless.signals.get("jev_exit") == "no_jev_client"
    assert "attachment_source" not in keyless.signals

    judged = decide_track(
        [HumanMessage(content=question)],
        jev_client=FakeJevChoice([("dialogue", 0.81)]),
    )
    assert judged.track == "dialogue"
    assert judged.decision_tier == "jev"
    assert judged.confidence == 0.81
    assert "attachment_source" not in judged.signals


def test_a_tickered_attachment_ask_still_routes_grounded() -> None:
    """An attachment ask that also names a ticker stays grounded on the
    research-signal arm. The attachment signal is not required."""
    decision = decide_track(
        [HumanMessage(content=_ATTACHMENT_ASK_TICKERED)], jev_client=None
    )
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert "jev_exit" not in decision.signals
    assert decision.signals.get("attachment_source") is not True


# ----- R8: route from bound authorized materials -----

_CHINESE_ENTERPRISE_QUESTION = "差旅报销制度里，工作晚餐每人最多可以报销多少？"
_LONG_CHINESE_QUESTION = _CHINESE_ENTERPRISE_QUESTION * 40


def test_jev_leaf_criteria_describe_authorized_materials() -> None:
    """Leaf copy names dialogue, a missing slot, off-topic, and an
    answer from authorized materials. It does not describe the product
    as US SEC filings."""
    criteria = _JEV_TRACK_LEAF_CRITERIA
    assert set(criteria) == set(_JEV_TRACK_OPTIONS)
    grounded = criteria["grounded"].casefold()
    off_topic = criteria["off_topic"].casefold()
    clarify = criteria["clarify"].casefold()
    dialogue = criteria["dialogue"].casefold()
    assert "sec" not in grounded
    assert "us-filing" not in off_topic
    assert "us filing" not in off_topic
    assert "authorized material" in grounded
    assert "slot" in clarify
    assert "off-topic" in off_topic or "off topic" in off_topic
    assert "dialogue" in dialogue or "conversational" in dialogue


def test_chinese_question_with_retrievable_material_is_grounded() -> None:
    """A question with no company token is grounded once a bound
    material is already retrievable. English policy words are not
    required, and the judge is not asked."""
    fake = FakeJevChoice([("off_topic", 0.99)])
    facts = {"retrievable_count": 1}
    decision = decide_track(
        [HumanMessage(content=_CHINESE_ENTERPRISE_QUESTION)],
        jev_client=fake,
        bound_materials=facts,
    )
    assert fake.calls == []
    assert decision.track == "grounded"
    assert decision.track not in ("clarify", "off_topic")
    assert decision.decision_tier == "rules"
    assert decision.signals.get("retrievable_materials") == 1
    assert "jev_exit" not in decision.signals


def test_chinese_question_with_zero_materials_is_not_forced_grounded() -> None:
    """The same question does not become grounded when the count is zero."""
    fake = FakeJevChoice([("off_topic", 0.99)])
    decision = decide_track(
        [HumanMessage(content=_CHINESE_ENTERPRISE_QUESTION)],
        jev_client=fake,
        bound_materials={"retrievable_count": 0},
    )
    assert decision.track == "off_topic"
    assert decision.decision_tier == "jev"
    assert decision.track != "grounded"


def test_omitted_material_facts_do_not_raise_or_ground() -> None:
    """No facts argument means no bound-material signal: the turn is
    NOT grounded by the RULES tier — the judge-down fallback does not
    impersonate that rule."""
    decision = decide_track(
        [HumanMessage(content=_CHINESE_ENTERPRISE_QUESTION)],
        bound_materials=None,
    )
    assert decision.decision_tier != "rules"
    assert decision.signals.get("retrievable_materials") is None
    assert decision.signals.get("jev_exit") == "no_jev_client"


def test_empty_utterance_with_retrievable_materials_is_grounded() -> None:
    """Empty text is not a greeting, so a positive count still grounds it."""
    decision = decide_track(
        [HumanMessage(content="   ")],
        bound_materials={"retrievable_count": 2},
    )
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"


def test_empty_utterance_with_zero_materials_stays_dialogue() -> None:
    decision = decide_track(
        [HumanMessage(content="")],
        bound_materials={"retrievable_count": 0},
    )
    assert decision.track == "dialogue"
    assert decision.decision_tier == "conditional_policy"


def test_long_utterance_with_retrievable_materials_is_grounded() -> None:
    """Length does not push a bound-material question into the judge."""
    assert len(_LONG_CHINESE_QUESTION) > 500
    fake = FakeJevChoice([("clarify", 0.99)])
    decision = decide_track(
        [HumanMessage(content=_LONG_CHINESE_QUESTION)],
        jev_client=fake,
        bound_materials={"retrievable_count": 1},
    )
    assert fake.calls == []
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"


def test_long_utterance_with_zero_materials_is_not_forced_grounded() -> None:
    fake = FakeJevChoice([("off_topic", 0.99)])
    decision = decide_track(
        [HumanMessage(content=_LONG_CHINESE_QUESTION)],
        jev_client=fake,
        bound_materials={"retrievable_count": 0},
    )
    assert decision.track == "off_topic"
    assert decision.decision_tier == "jev"


def test_greeting_stays_dialogue_when_materials_are_retrievable() -> None:
    decision = decide_track(
        [HumanMessage(content="Hello!")],
        bound_materials={"retrievable_count": 3},
    )
    assert decision.track == "dialogue"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("greeting") is True


def test_bound_material_facts_are_not_mutated() -> None:
    """The classifier reads the facts object and does not write it."""

    class _ToolSpy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def retrieve(self, *_args: object, **_kwargs: object) -> None:
            self.calls.append("retrieve")

        def list_files(self, *_args: object, **_kwargs: object) -> None:
            self.calls.append("list_files")

        def read(self, *_args: object, **_kwargs: object) -> None:
            self.calls.append("read")

    spy = _ToolSpy()
    facts: dict[str, Any] = {"retrievable_count": 1, "tools": spy}
    before = {"retrievable_count": 1, "tools": spy}
    decision = decide_track(
        [HumanMessage(content=_CHINESE_ENTERPRISE_QUESTION)],
        bound_materials=facts,
    )
    assert facts == before
    assert spy.calls == []
    assert decision.track == "grounded"


def test_non_numeric_material_count_does_not_raise() -> None:
    decision = decide_track(
        [HumanMessage(content=_CHINESE_ENTERPRISE_QUESTION)],
        bound_materials={"retrievable_count": "1"},
    )
    # A string count is no signal, so the RULES tier never grounded this
    # ask — only the judge-down fallback lane can carry it now.
    assert decision.decision_tier != "rules"
    assert decision.signals.get("retrievable_materials") is None


def test_filing_tie_break_retrieves_incomplete_ask_without_materials() -> None:
    """Rules keep complete filing asks; missing slots fall through to retrieval."""
    grounded = decide_track(
        [HumanMessage(content="What was AAPL net sales in 2023 Q3?")],
        bound_materials={"retrievable_count": 0},
    )
    assert grounded.track == "grounded"
    assert grounded.signals.get("filing_ask") is True

    incomplete = decide_track(
        [HumanMessage(content="What was revenue?")],
        bound_materials={"retrievable_count": 0},
    )
    assert incomplete.track == "grounded"
    assert incomplete.decision_tier == "jev_fallback"
    assert incomplete.signals.get("jev_exit") == JEV_CHOICE_EXIT_NO_JEV_CLIENT
    assert "missing_slots" not in incomplete.signals

    off_topic = decide_track(
        [HumanMessage(content="What's the weather in Seattle today?")],
        bound_materials={"retrievable_count": 0},
    )
    assert off_topic.track == "off_topic"
    assert off_topic.signals.get("off_domain") is True


def test_missing_slots_are_not_required_when_materials_are_retrievable() -> None:
    decision = decide_track(
        [HumanMessage(content="What was revenue?")],
        bound_materials={"retrievable_count": 1},
    )
    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"


def test_chinese_corpus_inventory_question_enters_research_track() -> None:
    decision = decide_track([HumanMessage(content="仓库有数据吗")])

    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("corpus_inventory") is True


def test_chinese_company_lookup_searches_before_asking_for_period() -> None:
    """The operator names a company AND delegates the rest ("帮我查判官
    公司的"). Asking again for the period is the loop this fix removes."""
    decision = decide_track([HumanMessage(content="帮我查判官公司的")])

    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("delegated_scope") is True
    # The route no longer asks for a period the operator delegated.
    assert "missing_slots" not in decision.signals


def test_delegated_choice_after_a_scope_question_uses_research_track() -> None:
    decision = decide_track(
        [
            HumanMessage(content="仓库有数据吗"),
            AIMessage(content="请提供公司名和报告期间。"),
            HumanMessage(content="随便"),
        ]
    )

    assert decision.track == "grounded"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("delegated_scope") is True


def test_corpus_inventory_question_renders_catalog_overview(tmp_path) -> None:
    catalog_path = tmp_path / "catalog.json"
    # A NEW physical row carries the T6 display fields: the spec's row
    # contract (qa-chain-corpus-retrieval §"Catalog fields and
    # compatibility projection") requires a non-empty ``title`` and
    # ``doc_type`` on every write.
    record_filings(
        [
            {
                "document_id": "APPLE-2023Q1",
                "title": "Apple 2023 Q1 10-Q",
                "doc_type": "10-Q",
                "company": "Apple Incorporated",
                "period": "2023 Q1",
                "version": "a" * 64,
                "page_count": 10,
            },
            {
                "document_id": "MICROSOFT-2023Q2",
                "title": "Microsoft 2023 Q2 10-Q",
                "doc_type": "10-Q",
                "company": "Microsoft Corporation",
                "period": "2023 Q2",
                "version": "b" * 64,
                "page_count": 8,
            },
        ],
        catalog_path=catalog_path,
    )
    model = ScriptedChatModel(script=[AIMessage(content="model output is not used")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        catalog_path=catalog_path,
    )

    out = graph.invoke(
        {**reset_input_state("仓库有数据吗"), "evidence": []},
        config={"configurable": {"thread_id": "track-corpus-inventory"}},
    )

    answer = out["answer"]
    assert out["track"] == "grounded"
    assert out["route_intent"] == "overview"
    assert out["status"] == "done"
    assert "2 份资料" in answer
    assert "Apple Incorporated" in answer
    assert "Microsoft Corporation" in answer
    assert "2023 Q1" in answer
    assert "2023 Q2" in answer
    assert out["citations"] == ()
    assert model._index == 0


def test_a_delegated_filing_ask_without_a_scope_marker_does_not_claim_delegation() -> None:
    """Boundary for the delegated-scope arm: a filing ask that uses a
    retrieval verb but does NOT delegate the scope must not claim the
    operator delegated it. Without this guard 「帮我查一下revenue」 would
    read as a delegation and would lose its genuine ambiguity.

    The turn itself falls back to retrieval with no Jev client (GE-1),
    so the observable contract pinned here is the ABSENT delegation
    signal — a delegated ask would resolve without asking the user,
    and that distinction is what this boundary exists to protect.
    """
    decision = decide_track([HumanMessage(content="帮我查一下revenue")])

    assert decision.signals.get("delegated_scope") is None
    assert "missing_slots" not in decision.signals
    assert decision.signals.get("jev_exit") == "no_jev_client"
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"


def test_catalog_inventory_does_not_sweep_up_ordinary_question_prose() -> None:
    """Boundary for the catalog arm: 「有哪些」 only counts when it names
    a catalog noun, so an ordinary question about risks or rules is not
    routed into a catalog enumeration. With no Jev client the turn still
    falls back to retrieval (GE-1), so the catalog arm's absence is the
    observable: it must not stamp ``corpus_inventory`` — that signal is
    what would make the receipt claim an overview instead of a search."""
    for text in ("有哪些风险", "有哪些规定"):
        decision = decide_track([HumanMessage(content=text)])
        assert decision.signals.get("corpus_inventory") is None, text
        assert decision.signals.get("jev_exit") == "no_jev_client", text


def test_clarify_second_model_call_pairs_every_tool_result(
    tmp_path,
) -> None:
    """The second clarify model call must carry the AIMessage that
    PRODUCED the tool result, immediately before that ToolMessage.

    The grounded lane gets this ordering from LangGraph node
    orchestration (model node → tools node → back to model node); the
    clarify leaf assembles its second call by hand, so it owns the
    ordering itself. Sending a ToolMessage with no preceding
    tool_calls AIMessage is an orphan tool result, which the provider
    rejects with ``tool result's tool id ... not found (2013)`` and the
    turn surfaces as a 500.
    """
    seen: list[list] = []

    class Recording(ScriptedChatModel):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = Recording(
        script=[
            _list_files_call("c-orphan-check"),
            AIMessage(content="The 知识库 holds ZEUS 2024 material."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=FakeJevChoice([("clarify", 0.99)]),
        known_document_ids=set(),
        catalog_path=_catalog_with_zeus(tmp_path),
    )

    graph.invoke(
        {**reset_input_state("What was revenue?"), "evidence": []},
        config={"configurable": {"thread_id": "clarify-tool-pairing"}},
    )

    assert len(seen) == 2, f"expected two model calls, got {len(seen)}"
    second = seen[1]
    # Every ToolMessage is immediately preceded by the AIMessage whose
    # tool_calls it answers.
    for index, message in enumerate(second):
        if getattr(message, "type", None) != "tool":
            continue
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        assert index > 0, "a ToolMessage must have a predecessor"
        producer = second[index - 1]
        assert isinstance(producer, AIMessage), (
            f"ToolMessage {tool_call_id} has no producing AIMessage "
            f"immediately before it"
        )
        assert tool_call_id in {
            str(call.get("id") or "") for call in producer.tool_calls
        }, f"ToolMessage {tool_call_id} is not answered by the preceding call"


def test_clarify_second_call_does_not_duplicate_the_producing_message(
    tmp_path,
) -> None:
    """The fix must ADD the producing AIMessage, not append a second
    copy: the same ``tool_call`` appearing twice makes the provider
    reject the pair the same way."""
    seen: list[list] = []

    class Recording(ScriptedChatModel):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = Recording(
        script=[
            _list_files_call("c-dup-check"),
            AIMessage(content="The 知识库 holds ZEUS 2024 material."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=FakeJevChoice([("clarify", 0.99)]),
        known_document_ids=set(),
        catalog_path=_catalog_with_zeus(tmp_path),
    )

    graph.invoke(
        {**reset_input_state("What was revenue?"), "evidence": []},
        config={"configurable": {"thread_id": "clarify-tool-no-dup"}},
    )

    second = seen[1]
    producing = [
        message
        for message in second
        if isinstance(message, AIMessage) and message.tool_calls
    ]
    assert len(producing) == 1, (
        f"expected exactly one producing AIMessage, got {len(producing)}"
    )
    # The track prompt is still exactly one head SystemMessage — the
    # hand-assembled second call must not stack a second one.
    heads = [
        message
        for message in second
        if isinstance(message, SystemMessage)
    ]
    assert len(heads) == 1, f"expected one SystemMessage head, got {len(heads)}"


# ----- the clarify leaf can reach the catalog it is asked about -----


def _catalog_with_zeus(tmp_path: Any) -> Any:
    """A catalog holding the ZEUS demo rows (specs/demo-seed.md §4)."""
    from fin.corpus.catalog import record_filings

    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "zeus-handbook",
                "title": "ZEUS handbook",
                "doc_type": "narrative",
                "company": "ZEUS",
                "period": "2024",
                "version": "v1",
                "page_count": 3,
            },
            {
                "document_id": "zeus-product-guide",
                "title": "ZEUS product guide",
                "doc_type": "narrative",
                "company": "ZEUS",
                "period": "2024",
                "version": "v1",
                "page_count": 2,
            },
        ],
        catalog_path=catalog_path,
    )
    return catalog_path


def _list_files_call(call_id: str = "c-clarify-catalog") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "list_files",
                "args": {},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_clarify_leaf_grounds_its_ask_in_the_real_catalog(tmp_path) -> None:
    """The clarify prompt tells the model to ground the options in the
    知识库 via ``list_files``. With the tool bound the leaf can ACTUALLY
    do that: the catalog rows reach the model and its answer is built
    from them, instead of the generic company/period restatement.
    """
    model = ScriptedChatModel(
        script=[
            _list_files_call(),
            AIMessage(content="The 知识库 holds ZEUS 2024 material."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=FakeJevChoice([("clarify", 0.99)]),
        known_document_ids=set(),
        catalog_path=_catalog_with_zeus(tmp_path),
    )

    out = graph.invoke(
        {**reset_input_state("What was revenue?"), "evidence": []},
        config={"configurable": {"thread_id": "clarify-catalog-hop"}},
    )

    assert out["track"] == "clarify"
    assert out["status"] == "done"
    catalog_tool_messages = [
        message
        for message in out["messages"]
        if getattr(message, "name", None) == "list_files"
    ]
    assert catalog_tool_messages, "clarify leaf must reach list_files"
    # The catalog really came back, so the ask was groundable.
    assert "zeus-handbook" in str(catalog_tool_messages[0].content)
    assert "ZEUS" in out["answer"]


def test_clarify_leaf_stays_citation_free_and_tool_free_for_a_plain_ask(
    tmp_path,
) -> None:
    """A clarify turn that asks without a tool call keeps the OTHER two
    halves of the leaf contract: done, EMPTY citations, and NO catalog
    hop the operator did not ask for. Catalog metadata is not evidence,
    so ``evidence`` must stay empty even on the tool path.
    """
    model = ScriptedChatModel(script=[AIMessage(content="Which company?")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=FakeJevChoice([("clarify", 0.99)]),
        known_document_ids=set(),
        catalog_path=_catalog_with_zeus(tmp_path),
    )

    out = graph.invoke(
        {**reset_input_state("What was revenue?"), "evidence": []},
        config={"configurable": {"thread_id": "clarify-no-tool"}},
    )

    assert out["track"] == "clarify"
    assert out["status"] == "done"
    assert list(out.get("citations") or ()) == []
    assert not any(
        getattr(message, "name", None) == "list_files"
        for message in out["messages"]
    )
    assert list(out.get("evidence") or ()) == []
