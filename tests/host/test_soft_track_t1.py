"""Soft-Track T1 graph contracts: dialogue / off_topic leave citation gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.agent_instructions import (
    DIALOGUE_INSTRUCTIONS_MARKER,
    HOST_INSTRUCTIONS_MARKER,
    OFF_TOPIC_INSTRUCTIONS_MARKER,
)
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.retrieve import BM25Adapter

AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Legacy tree-backed read adapter over the committed corpus. Only the
# scripted ``read_file`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
from fin.retrieve import ExtractedTreeReadPort

_TREE_READ_PORT = ExtractedTreeReadPort(
    Path("data/corpus/manifest.json"),
    Path("data/corpus/extracted"),
)


@pytest.fixture(scope="module")
def aapl_page1_text():
    tree = load_extracted_tree(
        Path("data/corpus/manifest.json"),
        Path("data/corpus/extracted"),
    )
    return tree[AAPL_2022Q3][0].text


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _checkpoint_messages(graph, thread_id: str) -> list:
    snapshot = graph._graph.get_state(_thread_config(thread_id))
    return list((snapshot.values or {}).get("messages") or [])


def test_greeting_dialogue_skips_citation_gate_and_does_not_checkpoint_prompt():
    """Greeting → track=dialogue, no tools, done, empty cites, no bounce,
    call-scoped dialogue SystemMessage not durable in checkpoint."""
    model = ScriptedChatModel(
        script=[AIMessage(content="Hello! How can I help with 10-Q research?")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="Hi!")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-greeting"),
    )
    assert out["track"] == "dialogue"
    assert out["status"] == "done"
    assert out.get("citations", ()) == () or out.get("citations") == []
    assert out["answer"]
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
    # No tool calls in history.
    for msg in out["messages"]:
        if isinstance(msg, AIMessage):
            assert not msg.tool_calls

    checkpointed = _checkpoint_messages(g, "t-soft-greeting")
    assert not any(
        isinstance(m, SystemMessage)
        and DIALOGUE_INSTRUCTIONS_MARKER in str(m.content)
        for m in checkpointed
    )
    assert not any(
        isinstance(m, SystemMessage) and HIDDEN_BOUNCE_MARKER in str(m.content)
        for m in checkpointed
    )


def test_off_domain_soft_done_not_refuse():
    """Off-domain → track=off_topic, soft done (not refuse), empty citations."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "I focus on SEC 10-Q filings in this workspace. "
                    "I cannot help with weather forecasts."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    out = g.invoke(
        {
            "messages": [
                HumanMessage(content="What's the weather in Seattle today?")
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-off-topic"),
    )
    assert out["track"] == "off_topic"
    assert out["status"] == "done"
    assert out["status"] != "refuse"
    assert out.get("citations", ()) == () or out.get("citations") == []
    assert out["answer"]
    checkpointed = _checkpoint_messages(g, "t-soft-off-topic")
    assert not any(
        isinstance(m, SystemMessage)
        and OFF_TOPIC_INSTRUCTIONS_MARKER in str(m.content)
        for m in checkpointed
    )
    assert not any(
        isinstance(m, SystemMessage) and HIDDEN_BOUNCE_MARKER in str(m.content)
        for m in checkpointed
    )


def test_research_fact_script_still_done_with_citation(aapl_page1_text):
    """Research tools+span path unchanged in kind: done with ≥1 citation."""
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
                        "id": "c-read-soft",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    out = g.invoke(
        {
            "messages": [HumanMessage(content="What was AAPL net sales in 2023 Q3?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-research"),
    )
    assert out["track"] == "research"
    assert out["status"] == "done"
    assert len(out["citations"]) >= 1


def test_compile_graph_identity_unchanged_under_soft_track():
    """Same checkpointer + model → same proxy (ADR 0013 / compile identity)."""
    checkpointer = InMemorySaver()
    model = ScriptedChatModel(script=[AIMessage(content="")])
    g1 = compile_graph(
        checkpointer=checkpointer,
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    g2 = compile_graph(
        checkpointer=checkpointer,
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    assert g1 is g2


def test_dialogue_model_sees_dialogue_prompt_not_research_prompt():
    """Call-scoped dialogue prompt is prepended at call time only."""
    from fin.host.scripted_model import ScriptedChatModel as SCM

    seen: list[list] = []

    class Recording(SCM):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = Recording(
        script=[AIMessage(content="Hi there.")],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        read_port=_TREE_READ_PORT,
    )
    g.invoke(
        {
            "messages": [HumanMessage(content="Hello")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-soft-dialogue-prompt"),
    )
    assert seen
    first = seen[0]
    assert isinstance(first[0], SystemMessage)
    assert DIALOGUE_INSTRUCTIONS_MARKER in str(first[0].content)
    assert HOST_INSTRUCTIONS_MARKER not in str(first[0].content)
