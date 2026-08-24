"""Soft-Track T4: classify-tier EXITs down the cascade."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.track_decision import decide_track

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class _ClassifyStub:
    """Minimal chat-model stand-in for ``decide_track`` LLM tier."""

    def __init__(self, *, content: str | None = None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls = 0

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content or "")


def test_invalid_track_enum_exits_llm_and_finishes_defined():
    """Invalid/missing track from classify → EXIT → cascade continues."""
    stub = _ClassifyStub(content='{"track": "supervisor"}')
    decision = decide_track(
        [HumanMessage(content="hmm maybe filings?")],
        classify_model=stub,
    )
    assert stub.calls == 1
    assert decision.track in ("dialogue", "clarify", "off_topic", "research")
    assert decision.signals.get("classify_exit") == "invalid_track"
    # Must have left the llm tier via EXIT (landed on a later tier).
    assert decision.decision_tier in (
        "inherit",
        "clarify",
        "conditional_policy",
    )


def test_schema_parse_fail_exits_llm_and_finishes_defined():
    stub = _ClassifyStub(content="not-json{{{")
    decision = decide_track(
        [HumanMessage(content="hmm maybe filings?")],
        classify_model=stub,
    )
    assert stub.calls == 1
    assert decision.track in ("dialogue", "clarify", "off_topic", "research")
    assert decision.signals.get("classify_exit") == "schema_parse"
    assert decision.decision_tier != "llm"


def test_classify_model_error_exits_and_finishes_defined():
    stub = _ClassifyStub(error=RuntimeError("classify down"))
    decision = decide_track(
        [HumanMessage(content="hmm maybe filings?")],
        classify_model=stub,
    )
    assert stub.calls == 1
    assert decision.track in ("dialogue", "clarify", "off_topic", "research")
    assert decision.signals.get("classify_exit") == "model_error"
    assert decision.decision_tier != "llm"


def test_high_confidence_rules_skip_llm():
    """Greeting/off-domain/full research must not call classify_model."""
    stub = _ClassifyStub(content='{"track": "research"}')
    greeting = decide_track(
        [HumanMessage(content="Hello!")],
        classify_model=stub,
    )
    assert greeting.track == "dialogue"
    assert greeting.decision_tier == "rules"
    assert stub.calls == 0

    off = decide_track(
        [HumanMessage(content="What's the weather in Seattle today?")],
        classify_model=stub,
    )
    assert off.track == "off_topic"
    assert stub.calls == 0


def test_llm_valid_track_used_when_ambiguous():
    stub = _ClassifyStub(content='{"track": "dialogue", "confidence": 0.7}')
    decision = decide_track(
        [HumanMessage(content="hmm maybe filings?")],
        classify_model=stub,
    )
    assert stub.calls == 1
    assert decision.track == "dialogue"
    assert decision.decision_tier == "llm"


def test_graph_classify_exit_does_not_crash_or_enter_gate():
    """Injected classify_model error → defined track; non-research skips gate."""
    stub = _ClassifyStub(error=RuntimeError("boom"))
    # Ambiguous utterance + LLM exit → cascade; may land dialogue/clarify/
    # research/off_topic. Use snapshot-less ambiguous text.
    model = ScriptedChatModel(
        script=[AIMessage(content="Soft reply after classify EXIT.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        classify_model=stub,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="hmm maybe filings?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-t4-exit"),
    )
    assert out["track"] in ("dialogue", "clarify", "off_topic", "research")
    assert out["track_decision"]["signals"].get("classify_exit") == "model_error"
    if out["track"] != "research":
        assert out["status"] == "done"
        assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
        assert out.get("citations", ()) == () or out.get("citations") == []


def test_scripted_research_q_still_rules_path_without_classify_model():
    """Default compile (no classify_model): bare q stays conditional_policy."""
    decision = decide_track([HumanMessage(content="q")])
    assert decision.track == "research"
    assert decision.decision_tier == "conditional_policy"
