"""Tests for the streaming primitives in ``fin.host.stream_map``.

T3 introduces token-level streaming. The four pure functions under
test are the contract between the host graph, the SSE surface, and
the SPA:

* :func:`assemble_final_ai_message` joins ``AIMessageChunk``s into
  one ``AIMessage``, preserving ``tool_calls`` and
  ``additional_kwargs.reasoning_details`` so the next model call
  sees the prior turn's reasoning (ADR 0022 round-trip).
* :func:`strip_thinking_from_answer` removes any
  ``<thinking>...</thinking>`` block the proxy inlined into the
  answer prose (SC60 binding: thinking prose must NOT pass the
  citation gate).
* :func:`format_tool_summary` / sub-formatters produce the human
  one-liners the SPA renders in the tools region (spec line 32).

These tests are pure-Python (no graph, no FastAPI, no LLM). Each
test pins one contract; if any test breaks the surface breaks.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from fin.host.stream_map import (
    assemble_final_ai_message,
    format_tool_summary,
    strip_thinking_from_answer,
    summarize_list_files,
    summarize_read_file,
    summarize_retrieve,
)


# ----- assemble_final_ai_message -----


def test_assemble_concatenates_text_chunks_in_order() -> None:
    """Two text chunks must produce an ``AIMessage`` whose content
    concatenates left-to-right, the order the proxy streamed them."""
    chunks = [
        AIMessageChunk(content="Hello "),
        AIMessageChunk(content="world"),
    ]
    msg = assemble_final_ai_message(chunks)
    assert isinstance(msg, AIMessage)
    assert msg.content == "Hello world"


def test_assemble_empty_iterable_returns_empty_ai_message() -> None:
    """No chunks -> empty AIMessage (NOT ``None``). The checkpointer
    cannot accept a ``None`` message."""
    msg = assemble_final_ai_message([])
    assert isinstance(msg, AIMessage)
    assert msg.content == ""


def test_assemble_joins_tool_call_chunks_into_complete_tool_calls() -> None:
    """A streaming tool-call arrives as multiple ``tool_call_chunks``
    (name fragment, then args fragments, then final ``id``). The
    assembled ``AIMessage`` must expose the COMPLETE ``tool_calls``
    list so the next node (and the checkpoint) see a finished
    intent to call the tool."""
    chunks = [
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
    msg = assemble_final_ai_message(chunks)
    assert len(msg.tool_calls) == 1
    call = msg.tool_calls[0]
    assert call["name"] == "list_files"
    assert call["id"] == "c-1"
    # langchain-core parses the joined args fragments into a dict.
    assert call["args"] == {"company": "AAPL"}


def test_assemble_preserves_reasoning_details_for_replay() -> None:
    """``reasoning_details`` round-trips through chunk-add by
    concatenating the per-chunk lists. The next model call sees the
    prior turn's reasoning so the loop continues without dropping
    the model's scratchpad (ADR 0022)."""
    rd1 = [{"type": "reasoning.text", "text": "thinking one"}]
    rd2 = [{"type": "reasoning.text", "text": "thinking two"}]
    chunks = [
        AIMessageChunk(content="Hello ", additional_kwargs={"reasoning_details": rd1}),
        AIMessageChunk(content="world", additional_kwargs={"reasoning_details": rd2}),
    ]
    msg = assemble_final_ai_message(chunks)
    details = msg.additional_kwargs.get("reasoning_details")
    assert isinstance(details, list)
    # Both entries survive the merge (LangChain concat semantics on
    # list-valued additional_kwargs).
    assert any(d.get("text") == "thinking one" for d in details)
    assert any(d.get("text") == "thinking two" for d in details)


def test_assemble_single_chunk_returns_ai_message_with_same_shape() -> None:
    """A one-chunk stream still produces an ``AIMessage`` (not a
    chunk) so the checkpoint write is uniform."""
    chunk = AIMessageChunk(content="only")
    msg = assemble_final_ai_message([chunk])
    assert isinstance(msg, AIMessage)
    assert msg.content == "only"


# ----- strip_thinking_from_answer -----


def test_strip_removes_inlined_thinking_block() -> None:
    """The proxy's inline ``<thinking>...</thinking>`` block must NOT
    survive the strip - SC60 binding."""
    answer = "<thinking>internal scratchpad notes</thinking>Real answer body."
    out = strip_thinking_from_answer(answer)
    assert "thinking" not in out.lower() or "<thinking" not in out.lower()
    assert "Real answer body." in out


def test_strip_is_no_op_when_no_thinking_block_present() -> None:
    """Common case: the proxy routed thinking through the dedicated
    ``reasoning_details`` channel; the answer prose is clean. The
    strip must be a no-op (no extra whitespace mutations)."""
    answer = "Plain prose answer with no inline thinking."
    assert strip_thinking_from_answer(answer) == answer


def test_strip_handles_multiline_thinking_blocks() -> None:
    """Multi-line thinking block collapses in one pass."""
    answer = (
        "<thinking>\nline one\nline two\nline three\n</thinking>\n\n"
        "Body answer."
    )
    out = strip_thinking_from_answer(answer)
    assert "<thinking" not in out
    assert "Body answer." in out


def test_strip_is_case_insensitive() -> None:
    """Proxy implementations differ on casing; the strip accepts both."""
    for tag in ("thinking", "THINKING", "Thinking"):
        answer = f"<{tag}>scratch</{tag}>body"
        out = strip_thinking_from_answer(answer)
        assert "<" not in out, (tag, out)


def test_strip_empty_string_is_empty_string() -> None:
    """Defensive: empty input -> empty output."""
    assert strip_thinking_from_answer("") == ""


def test_strip_does_not_eat_lone_literal_thinking_token() -> None:
    """A bare ``<thinking>`` token without a closing tag must NOT
    swallow the rest of the answer (no regex DoS on malformed
    proxies). The pattern requires the closing tag."""
    answer = "<thinking> unclosed token body"
    out = strip_thinking_from_answer(answer)
    # Without a closing tag the regex does not match; the answer
    # passes through unchanged.
    assert out == answer


# ----- summarize_list_files -----


def test_summarize_list_files_plural_when_many() -> None:
    """``n filings`` (plural) when n != 1."""
    out = summarize_list_files(
        filings=[{"document_id": "AAPL-2023Q1"}, {"document_id": "MSFT-2023Q1"}],
        args=None,
    )
    assert out == "2 filings"


def test_summarize_list_files_singular_when_one() -> None:
    """``1 filing`` (singular) when n == 1."""
    out = summarize_list_files(filings=[{"document_id": "AAPL-2023Q1"}], args=None)
    assert out == "1 filing"


def test_summarize_list_files_empty_returns_zero_filings() -> None:
    """Empty filings -> ``0 filings`` (so the operator sees the tool
    ran and returned nothing)."""
    out = summarize_list_files(filings=[], args=None)
    assert out == "0 filings"


def test_summarize_list_files_appends_filters_when_present() -> None:
    """Filter args append ``[company=AAPL]`` style suffix."""
    out = summarize_list_files(
        filings=[{"document_id": "AAPL-2023Q1"}],
        args={"company": "AAPL"},
    )
    assert out == "1 filing [company=AAPL]"


def test_summarize_list_files_combines_company_and_period_filters() -> None:
    """Both filters present -> both render."""
    out = summarize_list_files(
        filings=[],
        args={"company": "AAPL", "period": "2023Q1"},
    )
    assert "company=AAPL" in out
    assert "period=2023Q1" in out


# ----- summarize_retrieve -----


def test_summarize_retrieve_renders_query_and_hit_count() -> None:
    """Head line is ``"<query>': n hits"`` when a query exists."""
    out = summarize_retrieve(
        chunks=[{"source_ref": {"document_id": "AAPL-2023Q1", "page": 4}}],
        args={"query": "revenue growth"},
    )
    assert "revenue growth" in out
    assert "1 hit" in out


def test_summarize_retrieve_pluralises_hit() -> None:
    """``n hits`` (plural) when n != 1; ``1 hit`` singular."""
    chunks = [
        {"source_ref": {"document_id": "AAPL-2023Q1", "page": 1}},
        {"source_ref": {"document_id": "AAPL-2023Q1", "page": 2}},
    ]
    out = summarize_retrieve(chunks=chunks, args={"query": "q"})
    assert "2 hits" in out


def test_summarize_retrieve_appends_locators_up_to_three() -> None:
    """Up to 3 locators render so the operator can scan which pages
    were touched."""
    chunks = [
        {"source_ref": {"document_id": f"D-{i}", "page": i}} for i in range(1, 6)
    ]
    out = summarize_retrieve(chunks=chunks, args={"query": "q"})
    assert "D-1 p.1" in out
    assert "D-3 p.3" in out
    # 4th and 5th are out of the cap.
    assert "D-4 p.4" not in out
    assert "D-5 p.5" not in out


def test_summarize_retrieve_document_id_filter_appended() -> None:
    """When the model filtered retrieve on a document_id, it shows
    in parentheses next to the query so the operator sees the scope."""
    out = summarize_retrieve(
        chunks=[],
        args={"query": "q", "document_id": "AAPL-2023Q1"},
    )
    assert "(AAPL-2023Q1)" in out


def test_summarize_retrieve_accepts_locator_objects() -> None:
    """Real adapter returns ``Locator`` objects on chunks; the
    formatter reads attributes, not dict keys."""
    from fin.corpus.extracted import Locator

    chunks = [
        type("C", (), {"source_ref": Locator("AAPL-2023Q1", "v1", 4)})(),
    ]
    out = summarize_retrieve(chunks=chunks, args={"query": "q"})
    assert "AAPL-2023Q1 p.4" in out


# ----- summarize_read_file -----


def test_summarize_read_file_renders_document_and_page() -> None:
    """Spec line 32: ``document_id p.N``."""
    out = summarize_read_file(
        result=None,
        args={"document_id": "AAPL-2023Q1", "page": 4, "page_limit": 1},
    )
    assert out == "AAPL-2023Q1 p.4"


def test_summarize_read_file_renders_page_range_when_limit_gt_one() -> None:
    """``page_limit > 1`` produces ``p.start-end``."""
    out = summarize_read_file(
        result=None,
        args={"document_id": "AAPL-2023Q1", "page": 4, "page_limit": 3},
    )
    assert out == "AAPL-2023Q1 p.4-6"


def test_summarize_read_file_appends_unknown_locator_marker() -> None:
    """A miss (the tool returned ``{"error": "unknown_locator"}``)
    surfaces that error token so the operator can see why evidence
    is empty."""
    out = summarize_read_file(
        result={"error": "unknown_locator"},
        args={"document_id": "AAPL-2023Q1", "page": 4, "page_limit": 1},
    )
    assert "(unknown_locator)" in out


def test_summarize_read_file_falls_back_to_result_when_args_missing() -> None:
    """If only ``result`` is available (test path), use it."""
    out = summarize_read_file(
        result={"document_id": "AAPL-2023Q1", "page": 7, "page_limit": 1},
        args=None,
    )
    assert out == "AAPL-2023Q1 p.7"


# ----- format_tool_summary (dispatch) -----


def test_format_tool_summary_dispatches_list_files() -> None:
    """Dispatch on ``name='list_files'`` calls the list formatter."""
    out = format_tool_summary(
        "list_files",
        result={"filings": [{"document_id": "X"}]},
        args={},
    )
    assert "1 filing" in out


def test_format_tool_summary_dispatches_retrieve() -> None:
    """Dispatch on ``name='retrieve'`` calls the retrieve formatter."""
    out = format_tool_summary(
        "retrieve",
        result={"chunks": []},
        args={"query": "growth"},
    )
    assert "growth" in out


def test_format_tool_summary_dispatches_read_file() -> None:
    """Dispatch on ``name='read_file'`` calls the read_file formatter."""
    out = format_tool_summary(
        "read_file",
        result=None,
        args={"document_id": "X", "page": 3, "page_limit": 1},
    )
    assert out == "X p.3"


def test_format_tool_summary_unknown_tool_returns_bare_name() -> None:
    """A future tool name not in the dispatch table falls back to
    the bare name so the tools region still shows a row."""
    out = format_tool_summary("future_tool", result=None, args=None)
    assert out == "future_tool"


def test_format_tool_summary_handles_non_dict_result() -> None:
    """Defensive: a non-dict ``result`` does not crash the
    formatter (tests sometimes pass raw ``list`` or ``str``)."""
    out = format_tool_summary("list_files", result=["raw list"], args=None)
    assert "0 filings" in out
