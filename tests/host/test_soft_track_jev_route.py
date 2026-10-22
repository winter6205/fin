"""ADR 0080 / specs/soft-track-judge-route.md: gray-zone Jev routing.

One fixture per Success Criterion. The judge here is the shared
``FakeJevChoice`` (dev/test injection, never the product classifier);
the product live item is the named live suite. Default pytest stays hermetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import ExtractedPage, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.grounded.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.question.jev_choice import JEV_CHOICE_EXIT_TRANSPORT, JevChoiceError
from fin.host.grounded.track_decision import decide_track
from fin.retrieve import FakeRetrievePort
from jev_fake import FakeJevChoice

pytestmark = pytest.mark.soft_track

AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

MANIFEST_PATH = Path("data/corpus/manifest.json")
EXTRACTED_ROOT = Path("data/corpus/extracted")

_READ_MANIFEST: dict[str, tuple[ExtractedPage, ...]] | None = None


def _read_manifest() -> dict[str, tuple[ExtractedPage, ...]]:
    """The committed extracted tree as a ``{document_id: pages}`` read
    manifest.

    The graph never reads the tree (ADR 0046 / 0047), so a scripted
    ``read_source`` transcript hands its bodies in through the bound
    adapter's ``pages_by_doc``. Cached per module:
    ``ExtractedPage`` is frozen and ``FakeRetrievePort`` copies what it
    is handed, so no graph build disturbs another case's bodies.
    """
    global _READ_MANIFEST
    if _READ_MANIFEST is None:
        _READ_MANIFEST = load_extracted_tree(MANIFEST_PATH, EXTRACTED_ROOT)
    return _READ_MANIFEST


def _tree_read_adapter() -> FakeRetrievePort:
    """A fake retrieve port whose company read leg serves the committed
    corpus, so scripted ``read_source`` calls land on real filing text."""
    return FakeRetrievePort(pages_by_doc=_read_manifest())


def _read_source_call(unit: int, call_id: str) -> AIMessage:
    """One ``read_source`` hop for one frozen unit, read from its start."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": AAPL_2022Q3,
                        "version": AAPL_2022Q3_VERSION,
                        "unit": unit,
                    },
                    "position": 0,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _tool_message_names(messages: list[Any]) -> set[str]:
    return {
        name
        for name in (getattr(message, "name", None) for message in messages)
        if name
    }


@pytest.fixture(scope="module")
def aapl_page1_text() -> str:
    return _read_manifest()[AAPL_2022Q3][0].text


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
            _read_source_call(1, "c-jev-grounded"),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    fake = FakeJevChoice([("grounded", 0.9), ("graph", 0.9)])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=fake,
        known_document_ids=KNOWN_IDS,
        adapter=_tree_read_adapter(),
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
    # grounded turn's 检索档 Choice over the four word forms.
    assert [call["question_id"] for call in fake.calls] == [
        "track",
        "search_mode",
    ]
    # Spelled literally, not compared to ``LASTING_RETRIEVE_TIERS``: what this
    # seam owes the judge is the operator-facing ORDER of the 档 list, and an
    # equality against the tuple's own production could not name a reordering.
    assert fake.calls[1]["options"] == ["lexical", "graph", "global", "sql"]
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "jev"
    assert out["search_mode_decision"]["search_mode"] == "graph"
    # The research leaf ran with tools: the served ``read_source`` slice
    # produced a citation and no bounce marker entered the transcript.
    assert len(out["citations"]) >= 1
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
    # The citation is the unit this run read, not a page ordinal the
    # model named: ``read_source`` replaced the whole-page ``read_file``
    # seam with no compatibility path (spec thread-attachment-source-read
    # decision 5 / SC11).
    citation = out["citations"][0]
    assert citation.kind == "company"
    assert citation.document_id == AAPL_2022Q3
    assert citation.version == AAPL_2022Q3_VERSION
    assert citation.unit == 1
    assert citation.quote == span
    tool_names = _tool_message_names(out["messages"])
    assert "read_source" in tool_names
    assert "read_file" not in tool_names


def test_sc_low_conf_any_label_reaches_grounded_no_tools() -> None:
    """SC3 (GE-1): any label + sub-threshold confidence means the judge
    decided nothing — the turn reaches the grounded leaf rather than a
    pre-retrieval clarify. The scripted model makes no tool call, so the
    leaf runs no tools; with no retrieval the gate bounces the
    uncited answer."""
    fake = FakeJevChoice([("grounded", 0.3)])
    decision = decide_track(
        [HumanMessage(content="dig into the numbers?")], jev_client=fake
    )
    assert decision.track == "grounded"
    assert decision.decision_tier == "jev_fallback"
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
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "jev_fallback"
    assert out["track_decision"]["signals"].get("jev_exit") == "low_confidence"
    assert out["status"] == "refuse"
    for message in out["messages"]:
        if isinstance(message, AIMessage):
            assert not message.tool_calls


def test_sc_judge_throw_reaches_grounded_not_clarify() -> None:
    """SC4 (GE-1): a judge throw reaches the grounded leaf — never a
    pre-retrieval clarify and never a crash. The throw is contained at
    the cascade boundary; the receipt records the typed exit. With no
    retrieval the gate bounces the uncited answer."""
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
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "jev_fallback"
    assert out["track_decision"]["signals"]["jev_exit"] == "transport"
    assert out["status"] == "refuse"


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
