"""Tests for the streaming-enabled ``ScriptedChatModel`` (T3).

T3 extends the scripted chat model so the test surface can drive
``model_node.stream()`` (the streaming path the workspace send
route uses) without ever touching a live LLM. The two extensions:

* ``_stream(messages, ...)`` yields ``AIMessageChunk``s so a test
  can script a thinking-then-answer stream. The shape mirrors what
  the production ChatOpenAI emits against the MiniMax proxy
  (ADR 0022: ``reasoning_details`` on ``additional_kwargs``).
* The existing ``script`` list (``AIMessage``) still drives sync
  ``_generate``; tests that need streaming populate
  ``stream_script`` (a list of ``AIMessageChunk``) instead. Both
  fields fall back gracefully: empty stream_script delegates to the
  sync script.

The step-cap loop behaviour (returning the LAST chunk forever so a
test can force the cap without raising) is mirrored from the
existing sync ``_generate`` contract. T4 (crash resume) relies on
this.

Why this is a real test (not just ``assertTrue`` smoke): every
assertion pins a wire contract the host graph or the streaming
proxy depends on. If any test breaks the test surface lies and
production breaks too.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from fin.host.scripted_model import ScriptedChatModel


# ----- stream_script basics -----


def test_stream_yields_each_chunk_in_order_then_sentinel() -> None:
    """A two-chunk stream script yields each chunk in order. The
    LangChain base class appends a ``chunk_position='last'`` sentinel
    so the runnable caller can detect end-of-stream; the test
    asserts that the scripted chunks themselves arrive in order."""
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(content="Hello "),
            AIMessageChunk(content="world"),
        ]
    )
    chunks = list(model.stream([]))
    # The scripted chunks come through; the LangChain base may add a
    # final sentinel - filter to the scripted ones by content.
    real = [c for c in chunks if c.content]
    contents = [c.content for c in real]
    assert contents == ["Hello ", "world"]


def test_stream_assembled_message_concatenates_text() -> None:
    """The same chunks assembled via LangChain's chunk-add produce
    a single ``AIMessage`` whose content is the joined prose."""
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(content="Hello "),
            AIMessageChunk(content="world"),
        ]
    )
    chunks = list(model.stream([]))
    real = [c for c in chunks if c.content]
    acc = real[0]
    for c in real[1:]:
        acc = acc + c
    assert acc.content == "Hello world"


def test_stream_preserves_reasoning_details_across_chunks() -> None:
    """The proxy streams ``reasoning_details`` as a per-chunk
    ``additional_kwargs`` list; the test must mirror that shape so
    the assembly path (TDD for ``stream_map.assemble_final_ai_message``)
    round-trips the list entries."""
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_details": [{"text": "thought one"}]},
            ),
            AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_details": [{"text": "thought two"}]},
            ),
            AIMessageChunk(content="Hello"),
        ]
    )
    chunks = list(model.stream([]))
    real = [c for c in chunks if c.content or c.additional_kwargs]
    acc = real[0]
    for c in real[1:]:
        acc = acc + c
    details = acc.additional_kwargs.get("reasoning_details")
    assert isinstance(details, list)
    texts = [d.get("text") for d in details]
    assert "thought one" in texts
    assert "thought two" in texts


def test_stream_joins_tool_call_chunks_into_complete_tool_calls() -> None:
    """A streaming tool call arrives as multiple ``tool_call_chunks``;
    the assembled ``tool_calls`` list must contain one fully-formed
    entry (spec line 37: ``model_node`` complete => full
    ``AIMessage`` including complete ``tool_calls``)."""
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "list_files", "args": "", "id": "c-1", "index": 0}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": None, "args": '{"company"', "id": None, "index": 0}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": None, "args": ':"AAPL"}', "id": None, "index": 0}
                ],
            ),
        ]
    )
    chunks = list(model.stream([]))
    real = [c for c in chunks if c.tool_calls or c.tool_call_chunks]
    acc = real[0]
    for c in real[1:]:
        acc = acc + c
    assert len(acc.tool_calls) == 1
    call = acc.tool_calls[0]
    assert call["name"] == "list_files"
    assert call["id"] == "c-1"
    assert call["args"] == {"company": "AAPL"}


# ----- step-cap loop behavior -----


def test_stream_exhausted_script_replays_full_response_forever() -> None:
    """Once the streaming script is exhausted, EVERY subsequent
    ``stream()`` call replays the ENTIRE final response (all chunks,
    in order) so the assembled ``AIMessage`` is identical across
    calls — mirroring the sync ``_generate`` contract (``script[-1]``
    replays forever as a complete message).

    Why not replay just the LAST chunk: a split tool call's final
    chunk is a continuation fragment (``name=None``, ``id=None``);
    assembled alone it yields a malformed ``tool_call`` entry whose
    ``tool_call_id`` is ``None`` and downstream ``ToolNode`` raises
    ``ValidationError`` constructing the error ``ToolMessage``.
    Replaying the full response keeps every tool call fully-formed
    so step-cap loops land on a stable shape (spec line 37)."""
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(content="once"),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "list_files", "args": "", "id": "c-loop", "index": 0}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": None, "args": '{"company": "AAPL"}', "id": None, "index": 0}
                ],
            ),
        ]
    )

    def assemble(call_chunks: list[AIMessageChunk]):
        acc = call_chunks[0]
        for c in call_chunks[1:]:
            acc = acc + c
        return acc

    first_raw = list(model.stream([]))
    first = [c for c in first_raw if c.content or c.tool_call_chunks]
    assert len(first) == 3  # every scripted chunk, exactly once
    first_msg = assemble(first)
    assert first_msg.tool_calls == [
        {"name": "list_files", "args": {"company": "AAPL"}, "id": "c-loop", "type": "tool_call"}
    ]

    # Second AND third calls: identical full-response replay.
    for _ in range(2):
        replay_raw = list(model.stream([]))
        replay = [c for c in replay_raw if c.content or c.tool_call_chunks]
        assert len(replay) == 3
        replay_msg = assemble(replay)
        assert replay_msg.content == first_msg.content
        assert replay_msg.tool_calls == first_msg.tool_calls


def test_sync_generate_still_works_when_stream_script_set() -> None:
    """A test that wires only ``stream_script`` still gets
    meaningful sync ``invoke`` behaviour: the sync path delegates to
    ``script`` (the AIMessage list). When ``script`` is empty,
    ``_generate`` returns the fallback empty message."""
    model = ScriptedChatModel(
        stream_script=[AIMessageChunk(content="stream-only")]
    )
    # Sync invoke path: ``_generate`` reads ``self.script`` which
    # is empty -> fallback ``AIMessage(content="")``.
    from langchain_core.messages import HumanMessage

    chat_result = model._generate([HumanMessage(content="hi")])
    msg = chat_result.generations[0].message
    assert isinstance(msg, AIMessage)
    assert msg.content == ""


def test_stream_with_empty_script_falls_back_to_sync_invoke() -> None:
    """No streaming script -> ``stream()`` yields the result of the
    sync ``invoke`` wrapped in a single ``AIMessageChunk``. The
    LangChain default ``BaseChatModel.stream`` already implements
    this delegation; the test pins the contract that an empty
    ``stream_script`` does not raise."""
    model = ScriptedChatModel(script=[AIMessage(content="fallback")])
    chunks = list(model.stream([]))
    # The base default wraps the invoke output as one chunk; assert
    # we got SOMETHING (the LangChain base may add a sentinel).
    real = [c for c in chunks if c.content]
    assert any(c.content == "fallback" for c in real)


# ----- stream_responses: one full response per model call (T1) -----


def test_stream_responses_yields_one_full_response_per_stream_call() -> None:
    """``stream_responses`` scripts MULTIPLE model calls: each
    ``stream()`` invocation consumes ONE response (a list of
    chunks) in order. The T1 wire tests need call 1 to stream a
    tool-call response and call 2 to stream thinking + answer
    deltas — ``stream_script`` alone cannot express two distinct
    streamed responses."""
    model = ScriptedChatModel(
        stream_responses=[
            [AIMessageChunk(content="a"), AIMessageChunk(content="b")],
            [AIMessageChunk(content="c")],
        ]
    )
    first = list(model.stream([HumanMessage(content="q")]))
    second = list(model.stream([HumanMessage(content="q")]))
    third = list(model.stream([HumanMessage(content="q")]))
    assert [c.content for c in first if c.content] == ["a", "b"]
    assert [c.content for c in second if c.content] == ["c"]
    # Exhausted: the LAST response replays in full (mirrors the
    # sync ``script[-1]`` contract).
    assert [c.content for c in third if c.content] == ["c"]


def test_stream_responses_preserves_chunk_fields_verbatim() -> None:
    """Chunks replay with ``additional_kwargs`` (reasoning_details)
    and ``tool_call_chunks`` intact so the delta mapper sees the
    exact shapes the proxy emits."""
    model = ScriptedChatModel(
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_details": [{"text": "plan"}]
                    },
                    tool_call_chunks=[
                        {"name": "list_files", "args": "", "id": "c1", "index": 0}
                    ],
                )
            ]
        ]
    )
    chunks = list(model.stream([HumanMessage(content="q")]))
    chunk = next(c for c in chunks if c.additional_kwargs)
    assert chunk.additional_kwargs["reasoning_details"] == [
        {"text": "plan"}
    ]
    assert chunk.tool_call_chunks[0]["name"] == "list_files"


def test_stream_responses_takes_precedence_over_stream_script() -> None:
    """When both are set, ``stream_responses`` drives the streamed
    path; ``stream_script`` (single-response legacy shape) stays for
    existing tests."""
    model = ScriptedChatModel(
        stream_responses=[[AIMessageChunk(content="resp")]],
        stream_script=[AIMessageChunk(content="legacy")],
    )
    chunks = list(model.stream([HumanMessage(content="q")]))
    assert [c.content for c in chunks if c.content] == ["resp"]
