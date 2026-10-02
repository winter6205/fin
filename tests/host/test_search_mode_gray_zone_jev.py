"""检索档 gray zone: the Jev Choice REQUEST the 档 cascade sends.

Boundary of this file, as opposed to ``tests/host/test_search_mode_cascade.py``
(which owns the tier order and the fail-closed / EXIT family, including the
"Jev 档 tier" section there and the audit VALUES each answer records): the
request the judge gets, which turns must never consult it, the ``mode_``
namespace its answer is attributed under, the graph seam, and the guards that
the deleted MiniMax JSON classifier stays deleted.

The judge is the shared ``FakeJevChoice`` (dev/test injection, never a
product classifier); default pytest stays hermetic and the live 档
round-trip is its own named suite. The cascade under test: rules →
Jev 档 Choice → inherit peek → clarify / typed fail.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

import fin.host.search_mode_decision as smd
from fin.host import ScriptedChatModel, compile_graph
from fin.host.named_failures import (
    LASTING_RETRIEVE_TIERS,
    TIER_LABELS,
)
from fin.host.search_mode_decision import clarify_options, decide_search_mode
from fin.host.state import reset_input_state
from jev_fake import FakeJevChoice

pytestmark = pytest.mark.soft_track

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Grounded by the track rules tier, unnamed by the 档 rules tier, with no
# inheritable history: the gray zone the judge exists to decide.
AMBIGUOUS = "what was AAPL net sales in 2023 Q3?"


def _msgs(text: str) -> list[HumanMessage]:
    return [HumanMessage(content=text)]


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# the Choice request shape
# ---------------------------------------------------------------------------


def test_choice_request_names_the_closed_set_question_and_utterance() -> None:
    """The gray zone asks ONE closed-set question: the lasting 档 tokens as
    the options, the ``search_mode`` question id, and the utterance
    verbatim — nothing else, and exactly once."""
    fake = FakeJevChoice([("graph", 0.9)])
    decision = decide_search_mode(_msgs(AMBIGUOUS), jev_client=fake)

    (call,) = fake.calls
    # Derived, not re-spelled: which tokens (and in what order) the judge is
    # asked over is the SSOT tuple's contract, pinned literally once in
    # ``tests/host/test_soft_track_jev_route.py``.
    assert call["options"] == list(LASTING_RETRIEVE_TIERS)
    assert call["question_id"] == "search_mode"
    assert call["utterance"] == AMBIGUOUS
    assert decision.decision_tier == "jev"


def test_choice_instructions_teach_each_operator_label() -> None:
    """The tokens alone are not a choice an operator can reason about:
    every CONTEXT label has to be taught next to its own token, and the
    instruction text is built from the label SSOT, not re-spelled. The
    answer the instruction teaches has to be one the cascade accepts, so
    the scripted reply here is the bare 查找 token it was shown.

    The instruction prose stops at the names: each 档's discriminator is a
    per-option value on the wire, so a body pasted into the prompt would
    mean two copies of one rule that can drift apart. Both halves are
    pinned — every label present, no gloss body present.
    """
    fake = FakeJevChoice([("lexical", 0.9)])
    decision = decide_search_mode(_msgs(AMBIGUOUS), jev_client=fake)

    call = fake.calls[0]
    for mode in LASTING_RETRIEVE_TIERS:
        assert mode in call["instructions"]
        assert TIER_LABELS[mode] in call["instructions"]
    for gloss in smd._JEV_MODE_TIER_GLOSSES.values():
        assert gloss not in call["instructions"]
    assert decision.search_mode == "lexical"
    assert decision.decision_tier == "jev"
    assert decision.failure_code is None


# ---------------------------------------------------------------------------
# tiers that must reach the judge never see it
# ---------------------------------------------------------------------------


def test_rules_naming_skips_the_judge() -> None:
    """When the rules tier can name the 档, the judge never runs — that
    short-circuit holds for either rules outcome. 综述 is open (fin task
    4) and 真数据库 too since the tracked change T7 (commit 6e8ed97, which flipped the
    old sql pin from a rules typed_fail to a rules assignment): naming
    either assigns at the rules tier; BOTH paths skip the judge because
    the rules already read the utterance and returned a decided value."""
    fake: FakeJevChoice = FakeJevChoice([])  # any call would raise
    assigned = decide_search_mode(_msgs("综述档 说明一下"), jev_client=fake)
    assert fake.calls == []
    assert assigned.decision_tier == "rules"
    assert assigned.search_mode == "global"
    assert assigned.failure_code is None

    fake2: FakeJevChoice = FakeJevChoice([])
    named_sql = decide_search_mode(_msgs("sql档 查一下"), jev_client=fake2)
    assert fake2.calls == []
    assert named_sql.decision_tier == "rules"
    assert named_sql.search_mode == "sql"
    assert named_sql.failure_code is None


def test_choice_criteria_carry_one_gloss_per_lasting_tier() -> None:
    """Anti-drift pin for the next 档 rename. A token that arrives without
    its discriminator is the failure mode this prompt already shipped once
    (bare labels pulled ``sql 真数据库`` on any figure question), so the gloss
    table must be exactly the closed set and the cascade must hand that
    table over as its per-option criteria, value for value.

    A 档 added to ``LASTING_RETRIEVE_TIERS`` without its gloss stays LOUD
    here rather than becoming a silently undiscriminated option: the key
    set below is the closed set by equality, and the client validates the
    same pairing against the options before it sends, so the mismatch is a
    ``ValueError`` the cascade records as ``mode_jev_exit=unexpected_error``
    (pinned in ``tests/host/test_search_mode_cascade.py``) — never a guess.

    The label superset pin beside it is the other loud half: the instruction
    text emits a name only ``if mode in MODE_LABELS``, so a lasting 档 with
    a gloss but NO label would be dropped from what the judge is taught
    silently and could still be answered.
    """
    assert set(smd._JEV_MODE_TIER_GLOSSES) == set(LASTING_RETRIEVE_TIERS)
    assert set(smd.MODE_LABELS) >= set(LASTING_RETRIEVE_TIERS)
    for mode in LASTING_RETRIEVE_TIERS:
        assert smd._JEV_MODE_TIER_GLOSSES[mode].strip()

    fake = FakeJevChoice([("graph", 0.9)])
    decide_search_mode(_msgs(AMBIGUOUS), jev_client=fake)

    criteria = fake.calls[0]["criteria"]
    assert set(criteria) == set(LASTING_RETRIEVE_TIERS)
    for mode in LASTING_RETRIEVE_TIERS:
        assert criteria[mode] == smd._JEV_MODE_TIER_GLOSSES[mode]


def test_missing_client_names_the_judge_as_the_reason() -> None:
    """No key wired → no judge: the residual is clarify, never a silent
    综述 — and the audit row NAMES why, exactly as ``decide_track`` names
    ``jev_exit="no_jev_client"`` for its own gray zone. Without it a
    client-less 档 clarify turn is indistinguishable on the audit from one
    the judge was asked and abstained on, which is the silent branch this
    repo's 具名失败 doctrine forbids."""
    decision = decide_search_mode(_msgs(AMBIGUOUS), jev_client=None)

    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.signals["mode_jev_exit"] == "no_jev_client"
    # Nothing was asked, so nothing was answered: the EXIT is the whole
    # diagnostic, with no fabricated label or confidence beside it.
    assert "mode_jev_label" not in decision.signals
    assert "mode_jev_confidence" not in decision.signals
    assert decision.options == clarify_options()


def test_judge_naming_the_real_db_tier_is_now_assigned_at_the_jev_tier() -> None:
    """Flipped by the tracked change T7 (commit 6e8ed97): the old pin asserted the
    judge naming sql exited 「真库未接」 (``real_db_not_wired``) with no
    assignment — a 「档未开」-distinct named failure. The 档 is open now,
    so a judge answer of ``sql`` assigns exactly like ``global`` does
    (mirror of ``test_jev_naming_the_global_tier_is_assigned_at_the_jev_tier``
    in the cascade suite): the judge's label rides the audit, no failure
    code, and fin never invents a refusal for a 档 it now runs."""
    fake = FakeJevChoice([("sql", 0.95)])
    decision = decide_search_mode(_msgs(AMBIGUOUS), jev_client=fake)

    assert decision.search_mode == "sql"
    assert decision.decision_tier == "jev"
    assert decision.failure_code is None
    assert decision.signals["mode_jev_label"] == "sql"


# ---------------------------------------------------------------------------
# the audit attribution NAMESPACE (the recorded VALUES live in
# ``test_search_mode_cascade.py``)
# ---------------------------------------------------------------------------


def test_judge_keys_carry_the_mode_prefix() -> None:
    """The 档 judge namespaces its own signals with ``mode_`` so a merged
    log reads WHICH judge wrote them. The two ``signals`` dicts land in
    separate audit rows and are never merged, so this is attribution, not
    collision-avoidance — the track judge's bare ``jev_*`` names simply do
    not belong on this row."""
    fake = FakeJevChoice([("lexical", 0.9)])
    decision = decide_search_mode(_msgs(AMBIGUOUS), jev_client=fake)

    judge_keys = {
        key
        for key in decision.signals
        if key.startswith("mode_")
    }
    assert judge_keys == {"mode_jev_label", "mode_jev_confidence"}
    for key in ("jev_exit", "jev_label", "jev_confidence"):
        assert key not in decision.signals


# ---------------------------------------------------------------------------
# the graph seam
# ---------------------------------------------------------------------------


def _compile(script: list[AIMessage], *, jev_client: Any | None = None) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        jev_client=jev_client,
        known_document_ids=KNOWN_IDS,
    )


def test_dialogue_turn_makes_zero_mode_judge_calls() -> None:
    """A non-grounded track never reaches the 档 cascade, so an empty
    script proves the judge was not consulted at all."""
    fake: FakeJevChoice = FakeJevChoice([])
    g = _compile([AIMessage(content="Hi there.")], jev_client=fake)
    out = g.invoke(
        {**reset_input_state("Hi!"), "evidence": []}, config=_cfg("smj-greeting")
    )

    assert out["track"] == "dialogue"
    assert fake.calls == []
    assert not out.get("search_mode_decision")


def test_mode_decision_is_recorded_before_retrieve() -> None:
    fake = FakeJevChoice([("graph", 0.9)])
    g = _compile(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "AAPL net sales"},
                        "id": "c-retrieve-smj",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Per the filings: net sales grew."),
        ],
        jev_client=fake,
    )
    out = g.invoke(
        {**reset_input_state(AMBIGUOUS), "evidence": []},
        config=_cfg("smj-before-retrieve"),
    )

    assert out["track"] == "grounded"
    assert [(call["question_id"], call["options"]) for call in fake.calls] == [
        ("search_mode", ["lexical", "graph", "global", "sql"])
    ]
    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["decision_tier"] == "jev"
    summaries = out.get("tool_summaries") or []
    assert any(entry.get("name") == "retrieve" for entry in summaries)


# ---------------------------------------------------------------------------
# the removed structured-classify 档 path stays removed
# ---------------------------------------------------------------------------


class _ModelShapedWithoutChoose:
    """The injection shape the deleted classifier took: ``invoke`` and no
    ``choose``. Reaching the cascade with it must be inert."""

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the deleted structured-classify path ran")


def test_json_classify_model_path_is_gone() -> None:
    for name in ("_try_classify", "_CLASSIFY_SYSTEM", "_message_text"):
        assert not hasattr(smd, name), name


def test_entry_points_take_the_judge_not_a_model() -> None:
    for fn in (smd.decide_search_mode, smd.search_mode_update_for_turn):
        params = inspect.signature(fn).parameters
        assert "jev_client" in params
        assert "classify_model" not in params
        assert "pinned_mode" in params


def test_a_model_shaped_injection_cannot_drive_the_cascade() -> None:
    """Behavioral guard, not a text search: an object with ``invoke`` but
    no ``choose`` can neither crash the run nor name a 档. It gets the
    same residual an absent judge gives, and the audit says why — a
    contained ``AttributeError``, never a model call."""
    absent = decide_search_mode(_msgs(AMBIGUOUS))
    decision = decide_search_mode(
        _msgs(AMBIGUOUS), jev_client=_ModelShapedWithoutChoose()
    )

    assert decision.decision_tier == "clarify" == absent.decision_tier
    assert decision.search_mode is None
    assert decision.options == absent.options
    assert decision.signals["mode_jev_exit"] == "unexpected_error"
    assert decision.signals["mode_jev_error_type"] == "AttributeError"
