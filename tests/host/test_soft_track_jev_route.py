"""ADR 0080 / specs/soft-track-judge-route.md: gray-zone Jev routing.

One fixture per Success Criterion. The judge here is the shared
``FakeJevChoice`` (dev/test injection, never the product classifier);
the product live item is the named live suite. Default pytest stays hermetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.jev_choice import JEV_CHOICE_EXIT_TRANSPORT, JevChoiceError
from fin.host.track_decision import decide_track
from fin.retrieve import ExtractedTreeReadPort
from jev_fake import FakeJevChoice

pytestmark = pytest.mark.soft_track

AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)


@pytest.fixture(scope="module")
def aapl_page1_text() -> str:
    from fin.corpus.extracted import load_extracted_tree

    tree = load_extracted_tree(
        Path("data/corpus/manifest.json"),
        Path("data/corpus/extracted"),
    )
    return tree[AAPL_2022Q3][0].text


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_sc_rules_already_dialogue_zero_judge_calls() -> None:
    """SC1: a rules-decided greeting never reaches the judge."""
    fake = FakeJevChoice([("grounded", 0.99)])
    decision = decide_track([HumanMessage(content="Hello!")], jev_client=fake)
    assert decision.track == "dialogue"
    assert decision.decision_tier == "rules"
    assert fake.calls == []


def test_sc_ambiguous_high_conf_grounded_binds_tools(
    aapl_page1_text: str,
) -> None:
    """SC2: ambiguous + judge ``grounded`` (high conf) → the research
    leaf runs WITH tools (a tool call is served, citation gate passes)."""
    span = aapl_page1_text[:80]
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "page": 1,
                            "page_limit": 1,
                        },
                        "id": "c-jev-grounded",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    fake = FakeJevChoice([("grounded", 0.9), ("graph", 0.9)])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=fake,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="dig into the numbers?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_cfg("jev-sc2"),
    )
    # One judge client, two closed sets: the track Choice first, then the
    # grounded turn's 检索档 Choice over ``A|graph|global|D``.
    assert [call["question_id"] for call in fake.calls] == [
        "track",
        "search_mode",
    ]
    assert fake.calls[1]["options"] == ["A", "graph", "global", "D"]
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "jev"
    assert out["search_mode_decision"]["search_mode"] == "graph"
    # The research leaf ran with tools: the served read_file produced a
    # citation and no bounce marker entered the transcript.
    assert len(out["citations"]) >= 1
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))


def test_sc_low_conf_any_label_clarifies_no_tools() -> None:
    """SC3: any label + low confidence → clarify, no tools."""
    fake = FakeJevChoice([("grounded", 0.3)])
    decision = decide_track(
        [HumanMessage(content="dig into the numbers?")], jev_client=fake
    )
    assert decision.track == "clarify"
    assert decision.signals.get("jev_exit") == "low_confidence"

    model = ScriptedChatModel(
        script=[AIMessage(content="Which company and period?")]
    )
    graph_fake = FakeJevChoice([("grounded", 0.3)])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=graph_fake,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="dig into the numbers?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_cfg("jev-sc3"),
    )
    assert out["track"] == "clarify"
    assert out["status"] == "done"
    for message in out["messages"]:
        if isinstance(message, AIMessage):
            assert not message.tool_calls


def test_sc_judge_throw_clarifies_not_grounded() -> None:
    """SC4: a judge throw fail-closes to clarify — never silent grounded."""
    fake = FakeJevChoice(
        [JevChoiceError("down", exit_code=JEV_CHOICE_EXIT_TRANSPORT)]
    )
    model = ScriptedChatModel(script=[AIMessage(content="Let me ask back.")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=fake,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="dig into the numbers?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_cfg("jev-sc4"),
    )
    assert out["track"] == "clarify"
    assert out["track_decision"]["signals"]["jev_exit"] == "transport"
    assert out["status"] == "done"


def test_gray_zone_ask_carries_leaf_semantics() -> None:
    """Live smoke evidence: generic default instructions misjudge off-domain
    asks; the cascade must send track-specific criteria with the question."""
    fake = FakeJevChoice([("off_topic", 0.8)])
    decide_track([HumanMessage(content="dig into the numbers?")], jev_client=fake)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert set(call["options"]) == {"dialogue", "clarify", "off_topic", "grounded"}
    assert call["instructions"]
    for leaf in ("dialogue", "clarify", "off_topic", "grounded"):
        assert leaf in call["instructions"]
