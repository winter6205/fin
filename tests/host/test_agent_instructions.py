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


def test_messages_for_model_defaults_to_dialogue_prompt() -> None:
    """No single global default track: an omitted ``track=`` resolves
    to the dialogue prompt, not the research/host prompt."""
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(msgs)
    assert isinstance(out[0], SystemMessage)
    assert DIALOGUE_INSTRUCTIONS_MARKER in str(out[0].content)
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert HOST_INSTRUCTIONS_MARKER not in str(out[0].content)
    assert out[1:] == msgs


def test_messages_for_model_unknown_track_falls_back_to_dialogue_prompt():
    """Unknown track values must not silently select the host/research
    prompt — dialogue is the defined fallback (specs/soft-track.md)."""
    msgs = [HumanMessage(content="?")]
    out = messages_for_model(msgs, track="supervisor")  # type: ignore[arg-type]
    assert isinstance(out[0], SystemMessage)
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert HOST_INSTRUCTIONS_MARKER not in str(out[0].content)


def test_messages_for_model_research_still_selects_host_prompt() -> None:
    """Explicit ``track="research"`` keeps the citation / tool contract."""
    msgs = [HumanMessage(content="What was AAPL net sales in 2023 Q3?")]
    out = messages_for_model(msgs, track="research")
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


def test_host_prompt_names_every_research_tool() -> None:
    """The research track can call four tools; the prompt must name
    all four, ``get_xbrl_fact`` included (spec SC12)."""
    for tool_name in ("list_files", "retrieve", "read_file", "get_xbrl_fact"):
        assert tool_name in HOST_SYSTEM_PROMPT


def test_host_prompt_drops_stale_bm25_mention() -> None:
    """BM25 is archived (ADR 0032) — the prompt must not advertise it
    as the retrieve implementation (spec SC12)."""
    assert "BM25" not in HOST_SYSTEM_PROMPT


def test_host_prompt_describes_retrieve_as_the_retrieve_port() -> None:
    """The seam wording stays ``retrieve port`` (ADR 0032)."""
    assert "retrieve port" in HOST_SYSTEM_PROMPT
