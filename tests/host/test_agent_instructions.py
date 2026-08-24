"""Soft-Track T1: per-track call-scoped prompts."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from fin.host.agent_instructions import (
    DIALOGUE_INSTRUCTIONS_MARKER,
    DIALOGUE_SYSTEM_PROMPT,
    HOST_INSTRUCTIONS_MARKER,
    HOST_SYSTEM_PROMPT,
    OFF_TOPIC_INSTRUCTIONS_MARKER,
    OFF_TOPIC_SYSTEM_PROMPT,
    messages_for_model,
)


def test_messages_for_model_defaults_to_research_prompt() -> None:
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(msgs)
    assert isinstance(out[0], SystemMessage)
    assert HOST_INSTRUCTIONS_MARKER in str(out[0].content)
    assert out[0].content == HOST_SYSTEM_PROMPT
    assert out[1:] == msgs


def test_messages_for_model_dialogue_selects_dialogue_prompt() -> None:
    msgs = [HumanMessage(content="Hi")]
    out = messages_for_model(msgs, track="dialogue")
    assert DIALOGUE_INSTRUCTIONS_MARKER in str(out[0].content)
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert HOST_INSTRUCTIONS_MARKER not in str(out[0].content)


def test_messages_for_model_off_topic_selects_off_topic_prompt() -> None:
    msgs = [HumanMessage(content="weather?")]
    out = messages_for_model(msgs, track="off_topic")
    assert OFF_TOPIC_INSTRUCTIONS_MARKER in str(out[0].content)
    assert out[0].content == OFF_TOPIC_SYSTEM_PROMPT
