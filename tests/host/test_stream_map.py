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

from fin.corpus._private_fixtures import load_private_fixture

from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)

from fin.host.stream_map import (
    DeltaSplit,
    InlineFilterState,
    assemble_final_ai_message,
    format_tool_summary,
    reasoning_delta_from_chunk,
    reasoning_entries_from_delta,
    reasoning_entries_from_raw_chunk,
    reasoning_preserving_chat_openai,
    split_stream_delta,
    strip_thinking_from_answer,
    summarize_get_xbrl_fact,
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


def _minimax_reasoning_entry(text: str) -> dict[str, Any]:
    """One entry in the shape live MiniMax-M3 streams (2026-08-26).

    Every delta repeats the SAME ``id`` / ``format`` / ``index``
    identity and carries only its own slice of ``text``. Captured
    from ``POST https://api.minimaxi.com/v1/chat/completions`` with
    ``reasoning_split: true``.
    """
    return {
        "type": "reasoning.text",
        "id": "reasoning-text-1",
        "format": "MiniMax-response-v1",
        "index": 0,
        "text": text,
    }


def test_assemble_concatenates_only_the_prose_of_a_merged_entry() -> None:
    """Merging a streamed entry must grow ``text`` and nothing else.

    LangChain's ``merge_dicts`` concatenates every string field it
    has no special case for, so a provider that repeats an identity
    field on each delta (MiniMax repeats ``format``) ends up with
    that field duplicated once per chunk in the checkpointed
    message. The prose is the only field a delta actually splits.
    """
    chunks = [
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [_minimax_reasoning_entry(t)]
            },
        )
        for t in ("Let me ", "compute ", "17*23.")
    ]
    msg = assemble_final_ai_message(chunks)
    details = msg.additional_kwargs["reasoning_details"]
    assert len(details) == 1
    assert details[0]["text"] == "Let me compute 17*23."
    assert details[0]["format"] == "MiniMax-response-v1"
    assert details[0]["type"] == "reasoning.text"
    assert details[0]["id"] == "reasoning-text-1"
    assert details[0]["index"] == 0


def test_assemble_keeps_distinct_reasoning_entries_apart() -> None:
    """Two indexed entries stay two, each with its OWN identity.

    A provider that opens a second reasoning block mid-stream sends
    a new ``index``; repairing the first entry's metadata must not
    bleed onto the second.
    """
    first = _minimax_reasoning_entry("block one ")
    second = {
        **_minimax_reasoning_entry("block two "),
        "id": "reasoning-text-2",
        "format": "MiniMax-response-v2",
        "index": 1,
    }
    chunks = [
        AIMessageChunk(
            content="", additional_kwargs={"reasoning_details": [dict(first)]}
        ),
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [{**first, "text": "continued"}]
            },
        ),
        AIMessageChunk(
            content="", additional_kwargs={"reasoning_details": [dict(second)]}
        ),
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [{**second, "text": "also continued"}]
            },
        ),
    ]
    msg = assemble_final_ai_message(chunks)
    details = msg.additional_kwargs["reasoning_details"]
    assert [d["text"] for d in details] == [
        "block one continued",
        "block two also continued",
    ]
    assert [d["format"] for d in details] == [
        "MiniMax-response-v1",
        "MiniMax-response-v2",
    ]
    assert [d["id"] for d in details] == [
        "reasoning-text-1",
        "reasoning-text-2",
    ]


def test_assemble_concatenates_summary_prose_across_deltas() -> None:
    """``summary`` is prose too — a split summary must still join.

    ``reasoning.summary`` entries carry no ``text``; the repair must
    not mistake their prose for identity metadata and drop half of it.
    """
    chunks = [
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [
                    {
                        "type": "reasoning.summary",
                        "format": "openai-responses-v1",
                        "index": 0,
                        "summary": part,
                    }
                ]
            },
        )
        for part in ("weighed ", "both options")
    ]
    msg = assemble_final_ai_message(chunks)
    details = msg.additional_kwargs["reasoning_details"]
    assert len(details) == 1
    assert details[0]["summary"] == "weighed both options"
    assert details[0]["format"] == "openai-responses-v1"


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


def test_strip_removes_minimax_think_block() -> None:
    """MiniMax-M3 inlines ``<think>...</think>`` (not ``<thinking>``)
    when reasoning_split is missing. That scratchpad must not reach
    the citation gate as answer prose."""
    answer = "<think>The user asked about Apple net sales.</think>\nNet sales were $81.8 billion."
    out = strip_thinking_from_answer(answer)
    assert "<think" not in out.lower()
    assert "Apple net sales" not in out
    assert "Net sales were $81.8 billion." in out


def test_strip_keeps_markdown_indentation_and_blank_runs() -> None:
    """A thinking-free answer must survive the strip byte-for-byte.

    F3: the strip used to fold ``[ \\t]+\\n`` / ``\\n[ \\t]+`` /
    ``\\n{3,}`` over the WHOLE answer, which flattened nested
    markdown bullets and code-block indentation that the streamed
    deltas had already painted verbatim.
    """
    answer = (
        "Summary:\n\n"
        "- Point one\n"
        "    - nested detail\n\n\n"
        "```python\ndef f():\n    return 1\n```\n"
    )
    assert strip_thinking_from_answer(answer) == answer


# ----- streamed / persisted answer parity (F3) -----


# Answer shapes whose whitespace or tag boundaries previously
# diverged between the streamed deltas and the persisted row:
# nested indentation, trailing spaces, 3+ newline runs, and inline
# thinking blocks whose removal leaves a whitespace seam.
_PARITY_ANSWER_SHAPES: tuple[str, ...] = (
    "Summary:\n\n- Point one\n    - nested detail\n\nDone.",
    "Line one   \nLine two\n\n\nLine three",
    "<think>scratch</think>\n\nAnswer body.\n\n    indented tail\n",
    "A paragraph.\n\n<thinking>notes\nmore notes</thinking>\n\nB paragraph.",
    "<THINKING attr='x'>caps</THINKING >tail",
    "```python\ndef f():\n    return 1\n```\n",
)


def _streamed_answer(full: str, *, width: int) -> str:
    """Concatenate the ``answer`` pieces the delta split emits.

    Mirrors what ``_stream_run_frames`` puts on the wire: one
    ``split_stream_delta`` call per content chunk, then the
    ``final=True`` tail flush.
    """
    state = InlineFilterState()
    pieces: list[str] = []
    for start in range(0, len(full), width):
        split = split_stream_delta(state, full[start : start + width])
        state = split.state
        pieces.append(split.answer)
    pieces.append(split_stream_delta(state, "", final=True).answer)
    return "".join(pieces)


@pytest.mark.parametrize("full", _PARITY_ANSWER_SHAPES)
@pytest.mark.parametrize("width", [1, 3, 7, 4096])
def test_streamed_deltas_concatenate_to_the_stripped_answer(
    full: str, width: int
) -> None:
    """The two answer paths must agree byte-for-byte at any chunking.

    ``split_stream_delta`` produces the streamed answer;
    ``strip_thinking_from_answer`` produces the persisted one. The
    SPA prefers the streamed prose when the persisted status is
    ``done`` (``mergeReconciledTurn``), so any divergence is a
    visible repaint after the run ends.
    """
    assert _streamed_answer(full, width=width) == strip_thinking_from_answer(
        full
    )


def test_complete_tag_stem_at_a_delta_boundary_never_leaks(
    full: str = "before<thinking>scratch</thinking>after",
) -> None:
    """A delta that ends exactly ON a tag stem must hold it back.

    The hold-back used to keep at most ``len(stem) - 1`` characters,
    so a buffer that WAS the whole stem (``<thinking`` — the ``>``
    had not arrived yet) was released as answer prose and the tag
    never matched again: the scratchpad then streamed straight into
    the answer.
    """
    assert _streamed_answer(full, width=1) == "beforeafter"


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


def test_summarize_retrieve_skips_chunk_without_locator() -> None:
    """A chunk carrying no usable ``source_ref`` still counts as a hit
    but contributes no locator the operator cannot open."""
    chunks = [
        {"text": "no source_ref here"},
        {"source_ref": {"document_id": "AAPL-2023Q1", "page": 4}},
    ]
    out = summarize_retrieve(chunks=chunks, args={"query": "q"})
    assert "2 hits" in out
    assert out.endswith(" -> AAPL-2023Q1 p.4")


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


# ----- T8 / SC11: retrieve_unavailable is not a 0-hit miss -----


def test_summarize_retrieve_error_reads_as_backend_unavailable() -> None:
    """The typed EXIT gets prose, not a hit count.

    ``{"error": "retrieve_unavailable"}`` means retrieval never ran;
    rendering it as ``0 hits`` would tell the operator the corpus has
    nothing (``specs/s1-corpus-retrieve.md`` typed EXITs)."""
    out = summarize_retrieve(
        chunks=None, args={"query": "growth"}, error="retrieve_unavailable"
    )
    assert "unavailable" in out
    assert "0 hit" not in out
    assert "growth" in out


def test_summarize_retrieve_unknown_error_token_still_surfaces() -> None:
    """A token with no copy entry still renders readably (and never
    as a hit count) so a future typed EXIT is not silently blank."""
    out = summarize_retrieve(chunks=None, args={"query": "q"}, error="boom")
    assert "boom" in out
    assert "0 hit" not in out


def test_format_tool_summary_retrieve_unavailable_differs_from_zero_hits() -> (
    None
):
    """SC11: the two summaries must not be the same string."""
    unavailable = format_tool_summary(
        "retrieve",
        result={"error": "retrieve_unavailable"},
        args={"query": "growth"},
    )
    zero_hits = format_tool_summary(
        "retrieve", result={"chunks": []}, args={"query": "growth"}
    )
    assert unavailable != zero_hits
    assert "0 hits" in zero_hits
    assert "0 hits" not in unavailable
    assert "unavailable" in unavailable


# ----- T8 / SC11: get_xbrl_fact one-liner -----


def test_summarize_get_xbrl_fact_renders_concept_period_and_value() -> None:
    """A hit reads as ``concept period = value (unit, scale)`` plus
    the source locator — the fact row in one line (ADR 0031)."""
    out = summarize_get_xbrl_fact(
        result={
            "concept": "us-gaap:Revenues",
            "value": load_private_fixture("e8e6fa2502cf2441d4f1862bbbb7d147b7dbd57a3911be363417fe4342e69b94"),
            "period": "2023-Q3-YTD",
            "unit": "USD",
            "scale": "millions",
            "source_locator": {
                "document_id": "AAPL-2023Q3",
                "version": "v" * 64,
                "page": 4,
            },
        },
        args={"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
    )
    assert "us-gaap:Revenues" in out
    assert "2023-Q3-YTD" in out
    assert load_private_fixture("c18591bcf0070a92596e8fa66285e8cb7eafe482710c7f278506d2292ef3c207") in out
    assert "USD" in out
    assert "millions" in out
    assert "AAPL-2023Q3 p.4" in out


def test_summarize_get_xbrl_fact_renders_float_value() -> None:
    """A non-integral value keeps its decimals."""
    out = summarize_get_xbrl_fact(
        result={"concept": "c", "period": "p", "value": 1.25},
        args=None,
    )
    assert "1.25" in out


def test_summarize_get_xbrl_fact_appends_miss_token() -> None:
    """The typed miss (``fact_not_found``) surfaces its token the
    same way ``summarize_read_file`` surfaces ``unknown_locator``."""
    out = summarize_get_xbrl_fact(
        result={
            "error": "fact_not_found",
            "concept": "us-gaap:Revenues",
            "period": "2099-Q1",
            "document_id": "AAPL-2023Q3",
        },
        args={"concept": "us-gaap:Revenues", "period": "2099-Q1"},
    )
    assert "(fact_not_found)" in out
    assert "us-gaap:Revenues" in out


def test_summarize_get_xbrl_fact_falls_back_to_args_without_result() -> None:
    """No ``ToolMessage`` for the call (result ``None``): the
    arguments still describe what was looked up."""
    out = summarize_get_xbrl_fact(
        result=None,
        args={"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
    )
    assert out == "us-gaap:Revenues 2023-Q3-YTD"


def test_format_tool_summary_get_xbrl_fact_is_not_the_bare_name() -> None:
    """SC11: ``get_xbrl_fact`` no longer falls through to the bare
    tool name in the tools region."""
    out = format_tool_summary(
        "get_xbrl_fact",
        result={
            "concept": "us-gaap:Revenues",
            "value": load_private_fixture("e8e6fa2502cf2441d4f1862bbbb7d147b7dbd57a3911be363417fe4342e69b94"),
            "period": "2023-Q3-YTD",
            "unit": "USD",
            "scale": "millions",
            "source_locator": {
                "document_id": "AAPL-2023Q3",
                "version": "v" * 64,
                "page": 4,
            },
        },
        args={"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
    )
    assert out != "get_xbrl_fact"
    assert "us-gaap:Revenues" in out
    assert load_private_fixture("c18591bcf0070a92596e8fa66285e8cb7eafe482710c7f278506d2292ef3c207") in out


def test_format_tool_summary_get_xbrl_fact_survives_empty_call() -> None:
    """Defensive: neither result nor args (a malformed call) still
    yields a readable row instead of an empty cell."""
    out = format_tool_summary("get_xbrl_fact", result=None, args=None)
    assert out.strip()
    assert out != "get_xbrl_fact"


def test_summarize_get_xbrl_fact_head_prefers_result_over_args() -> None:
    """The tool echoes the concept it actually resolved; that wins over
    the alias the model asked for."""
    out = summarize_get_xbrl_fact(
        result={
            "concept": "us-gaap:Revenues",
            "period": "2023-Q3",
            "value": 1,
        },
        args={"concept": "revenue", "period": "Q3"},
    )
    assert out.startswith("us-gaap:Revenues 2023-Q3 =")


def test_summarize_get_xbrl_fact_head_falls_back_to_placeholder() -> None:
    """Neither side names the fact: a placeholder keeps the row
    readable instead of leaving a bare ``= value``."""
    out = summarize_get_xbrl_fact(result={"value": 7}, args={})
    assert out == "xbrl fact = 7"


def test_summarize_get_xbrl_fact_single_qualifier_no_comma() -> None:
    """A unit without a scale renders one qualifier, not ``(USD, )``."""
    out = summarize_get_xbrl_fact(
        result={"concept": "c", "period": "p", "value": 1, "unit": "USD"},
        args=None,
    )
    assert out == "c p = 1 (USD)"


def test_summarize_get_xbrl_fact_no_qualifiers_omits_parens() -> None:
    """Neither unit nor scale: no empty ``()`` after the value."""
    out = summarize_get_xbrl_fact(
        result={"concept": "c", "period": "p", "value": 1},
        args=None,
    )
    assert out == "c p = 1"


def test_summarize_get_xbrl_fact_locator_without_page_names_document() -> None:
    """A page-less locator still names the document it came from."""
    out = summarize_get_xbrl_fact(
        result={
            "concept": "c",
            "period": "p",
            "value": 1,
            "source_locator": {"document_id": "AAPL-2023Q3"},
        },
        args=None,
    )
    assert out.endswith(" -> AAPL-2023Q3")


def test_summarize_get_xbrl_fact_locator_without_doc_id_dropped() -> None:
    """A locator the operator cannot open renders as nothing rather
    than a dangling arrow."""
    out = summarize_get_xbrl_fact(
        result={
            "concept": "c",
            "period": "p",
            "value": 1,
            "source_locator": {"page": 4},
        },
        args=None,
    )
    assert out == "c p = 1"


def test_summarize_get_xbrl_fact_non_dict_locator_is_dropped() -> None:
    """A malformed ``source_locator`` (not a mapping) must not raise."""
    out = summarize_get_xbrl_fact(
        result={
            "concept": "c",
            "period": "p",
            "value": 1,
            "source_locator": "AAPL",
        },
        args=None,
    )
    assert out == "c p = 1"


# ----- T1 (#33): token-delta mapping (thinking / answer split) -----


def test_reasoning_delta_from_chunk_joins_reasoning_details_texts() -> None:
    """The ``reasoning_split=true`` channel (ADR 0022) lands each
    streamed thinking delta on ``additional_kwargs.reasoning_details``
    as one entry; the mapper concatenates the entries' ``text``
    fields for THIS chunk in order."""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={
            "reasoning_details": [{"text": "plan "}, {"text": "read"}]
        },
    )
    assert reasoning_delta_from_chunk(chunk) == "plan read"


def test_reasoning_delta_from_chunk_empty_when_no_reasoning() -> None:
    """Plain content chunks (no reasoning_details) yield an empty
    thinking delta so the caller skips the SSE frame."""
    assert reasoning_delta_from_chunk(AIMessageChunk(content="x")) == ""


def test_reasoning_delta_from_chunk_ignores_malformed_entries() -> None:
    """Defensive: non-dict entries and entries without a string
    ``text`` contribute nothing; a valid entry still passes."""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={
            "reasoning_details": [{"foo": 1}, "junk", {"text": "ok"}]
        },
    )
    assert reasoning_delta_from_chunk(chunk) == "ok"


def test_split_plain_delta_passes_through_as_answer() -> None:
    """No inline block -> the whole delta is answer prose."""
    split = split_stream_delta(InlineFilterState(), "hello")
    assert split.answer == "hello"
    assert split.thinking == ""
    assert split.state.buffer == "" and split.state.inside is False


def test_split_complete_inline_block_in_one_delta() -> None:
    """A whole ``<thinking>...</thinking>`` block inside one delta
    splits into thinking prose + visible answer in one pass."""
    split = split_stream_delta(
        InlineFilterState(), "<thinking>secret</thinking>visible"
    )
    assert split.thinking == "secret"
    assert split.answer == "visible"


def test_split_open_block_emits_answer_prefix_then_thinking_live() -> None:
    """Text before the opening tag is answer; thinking prose INSIDE
    an open block streams out immediately (only a potential partial
    close tag is held back)."""
    a = split_stream_delta(InlineFilterState(), "pre <thinking>par")
    assert a.answer == "pre "
    assert a.thinking == "par"
    assert a.state.inside is True
    b = split_stream_delta(a.state, "tial</thinking>post")
    assert b.thinking == "tial"
    assert b.answer == "post"
    assert b.state.inside is False and b.state.buffer == ""


def test_split_opening_tag_fragment_split_across_deltas() -> None:
    """``<thi`` + ``nking>x</thinking>tail``: the tag fragment is
    held back (NOT emitted as answer), then resolved on the next
    delta."""
    a = split_stream_delta(InlineFilterState(), "ans<thi")
    assert a.answer == "ans"
    assert a.state.buffer == "<thi"
    b = split_stream_delta(a.state, "nking>x</thinking>tail")
    assert b.thinking == "x"
    assert b.answer == "tail"


def test_split_closing_tag_fragment_split_across_deltas() -> None:
    """A close-tag fragment spanning deltas is held back as thinking
    (never leaked into answer) and resolved when the tag completes."""
    a = split_stream_delta(InlineFilterState(), "<thinking>think</thin")
    assert a.thinking == "think"
    assert a.state.inside is True
    assert a.state.buffer == "</thin"
    b = split_stream_delta(a.state, "king>out")
    assert b.answer == "out"
    assert b.thinking == ""


def test_split_is_case_insensitive_on_tags() -> None:
    """Proxy implementations disagree on tag casing; both open and
    close tags match case-insensitively."""
    split = split_stream_delta(
        InlineFilterState(), "<THINKING>x</ThInkIng >rest"
    )
    assert split.thinking == "x"
    assert split.answer == "rest"


def test_split_final_flush_releases_pending_fragments() -> None:
    """``final=True`` (stream end) releases any held buffer: a
    dangling fragment outside a block flushes as answer; a partial
    close tag inside a block flushes as thinking. Nothing is
    dropped."""
    held = split_stream_delta(InlineFilterState(), "tail<th")
    assert held.answer == "tail"
    flushed = split_stream_delta(held.state, "", final=True)
    assert flushed.answer == "<th"
    assert flushed.thinking == ""

    inside = split_stream_delta(InlineFilterState(), "<thinking>x</thin")
    assert inside.thinking == "x"
    closed = split_stream_delta(inside.state, "", final=True)
    assert closed.thinking == "</thin"
    assert closed.answer == ""


def test_split_multiple_blocks_in_one_delta() -> None:
    """Two complete blocks in a single delta resolve in one pass."""
    split = split_stream_delta(
        InlineFilterState(),
        "<thinking>a</thinking>mid<thinking>b</thinking>end",
    )
    assert split.thinking == "ab"
    assert split.answer == "midend"


def test_split_state_is_plain_data() -> None:
    """``DeltaSplit`` / ``InlineFilterState`` are frozen dataclasses
    so the route thread can carry them as local variables (no
    hidden mutable state across requests)."""
    split = split_stream_delta(InlineFilterState(), "x")
    assert isinstance(split, DeltaSplit)
    assert isinstance(split.state, InlineFilterState)


def test_split_partial_tag_only_delta_waits_without_looping() -> None:
    """A delta that is ENTIRELY a potential tag fragment (``"<thi"``)
    holds everything and emits nothing — and MUST terminate: the
    whole-buffer holdback case once spun the scan loop forever
    because no character was emittable and the buffer never shrank.
    Regression guard for the infinite loop."""
    a = split_stream_delta(InlineFilterState(), "<thi")
    assert a.answer == ""
    assert a.thinking == ""
    assert a.state.buffer == "<thi"
    # A subsequent empty delta still makes no progress — also must
    # terminate, holding the same fragment.
    b = split_stream_delta(a.state, "")
    assert b.answer == ""
    assert b.state.buffer == "<thi"
    # The next real delta resolves the tag.
    c = split_stream_delta(a.state, "nking>x</thinking>ok")
    assert c.thinking == "x"


def test_split_minimax_think_block_is_thinking_not_answer() -> None:
    """Live SSE must route MiniMax ``<think>`` deltas to thinking,
    otherwise the operator sees the scratchpad as the answer and
    the gate bounces on ungrounded prose."""
    split = split_stream_delta(
        InlineFilterState(), "<think>secret plan</think>visible span"
    )
    assert split.thinking == "secret plan"
    assert split.answer == "visible span"


def test_split_minimax_think_tag_can_span_chunks() -> None:
    """``<thi`` + ``nk>x</think>tail`` across deltas."""
    a = split_stream_delta(InlineFilterState(), "<thi")
    assert a.thinking == ""
    assert a.answer == ""
    b = split_stream_delta(a.state, "nk>x</think>tail")
    assert b.thinking == "x"
    assert b.answer == "tail"


# ----- T4 (#37): thinking duration clock -----


def test_thinking_clock_no_thinking_is_zero() -> None:
    from fin.host.stream_map import ThinkingClock

    clock = ThinkingClock(now_fn=lambda: 0.0)
    clock.tick_other()
    clock.close()
    assert clock.total_ms == 0


def test_thinking_clock_accumulates_one_segment() -> None:
    from fin.host.stream_map import ThinkingClock

    t = [0.0]
    clock = ThinkingClock(now_fn=lambda: t[0])
    clock.tick_thinking()
    t[0] = 1.5
    clock.tick_other()
    clock.close()
    assert clock.total_ms == 1500


def test_thinking_clock_accumulates_across_segments() -> None:
    from fin.host.stream_map import ThinkingClock

    t = [0.0]
    clock = ThinkingClock(now_fn=lambda: t[0])
    clock.tick_thinking()          # segment 1 starts
    t[0] = 0.5
    clock.tick_other()             # tool_call closes segment 1
    clock.tick_other()             # tool_result — no open segment
    t[0] = 1.0
    clock.tick_thinking()          # segment 2 starts
    t[0] = 1.25
    clock.close()                  # stream end closes segment 2
    assert clock.total_ms == 750


def test_thinking_clock_consecutive_thinking_ticks_do_not_restack() -> None:
    from fin.host.stream_map import ThinkingClock

    t = [0.0]
    clock = ThinkingClock(now_fn=lambda: t[0])
    clock.tick_thinking()
    t[0] = 2.0
    clock.tick_thinking()          # already open — no restart
    clock.close()
    assert clock.total_ms == 2000


# ----- T2: reasoning captured in the shape the provider emits -----


# The scratchpad prose every shape below carries. No ``answer``
# frame and no gate-visible prose may contain it (SC60).
SECRET = "INTERNAL the operator must never read as the answer"


def _raw_chunk(delta: dict[str, Any]) -> dict[str, Any]:
    """One OpenAI-compatible ``chat.completion.chunk`` payload.

    The shape ``ChatOpenAI`` receives off the wire before its own
    delta -> ``AIMessageChunk`` conversion runs.
    """
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "fin/MiniMax-M3",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def _map_chunks_to_frames(
    chunks: list[AIMessageChunk],
) -> list[tuple[str, str]]:
    """Fake chunk mapper: chunks -> the ``(event, text)`` SSE frames.

    Mirrors the send route's per-chunk mapping using ONLY the
    ``stream_map`` primitives, so a shape that regresses here
    regresses on the wire too — without booting FastAPI or a graph.
    """
    state = InlineFilterState()
    frames: list[tuple[str, str]] = []
    for chunk in chunks:
        reasoning = reasoning_delta_from_chunk(chunk)
        if reasoning:
            frames.append(("thinking", reasoning))
        content = chunk.content
        if isinstance(content, str) and content:
            split = split_stream_delta(state, content)
            state = split.state
            if split.thinking:
                frames.append(("thinking", split.thinking))
            if split.answer:
                frames.append(("answer", split.answer))
    tail = split_stream_delta(state, "", final=True)
    if tail.thinking:
        frames.append(("thinking", tail.thinking))
    if tail.answer:
        frames.append(("answer", tail.answer))
    return frames


def _text_of(frames: list[tuple[str, str]], event: str) -> str:
    return "".join(text for name, text in frames if name == event)


# --- raw provider delta -> normalized reasoning entries ---


def test_raw_chunk_minimax_reasoning_details_normalizes_to_entries() -> None:
    """The ``reasoning_split=true`` split channel: MiniMax puts the
    scratchpad on ``delta.reasoning_details`` (ADR 0022). The
    normalizer keeps the provider entry verbatim so replay-relevant
    fields (``type`` / ``format`` / ``index``) survive."""
    entries = reasoning_entries_from_raw_chunk(
        _raw_chunk(
            {
                "role": "assistant",
                "content": "",
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "text": SECRET,
                        "format": "unknown",
                        "index": 0,
                    }
                ],
            }
        )
    )
    assert entries == [
        {
            "type": "reasoning.text",
            "text": SECRET,
            "format": "unknown",
            "index": 0,
        }
    ]


def test_raw_chunk_reasoning_content_normalizes_to_entries() -> None:
    """The other shape OpenAI-compatible providers emit is a plain
    ``delta.reasoning_content`` string. It normalizes onto the same
    ``reasoning_details`` entry shape the rest of the host reads."""
    entries = reasoning_entries_from_raw_chunk(
        _raw_chunk({"content": "", "reasoning_content": "step one "})
    )
    assert entries == [{"type": "reasoning.text", "text": "step one "}]


def test_raw_chunk_reasoning_string_and_dict_both_normalize() -> None:
    """``delta.reasoning`` arrives as a bare string from some
    proxies and as an object from others; both carry the same
    prose."""
    as_string = reasoning_entries_from_raw_chunk(
        _raw_chunk({"content": "", "reasoning": "weighing the filing"})
    )
    as_object = reasoning_entries_from_raw_chunk(
        _raw_chunk(
            {"content": "", "reasoning": {"text": "weighing the filing"}}
        )
    )
    assert as_string == [
        {"type": "reasoning.text", "text": "weighing the filing"}
    ]
    assert as_object == as_string


def test_raw_chunk_without_reasoning_yields_no_entries() -> None:
    """A plain content delta carries no split channel — the caller
    must fall through to the inline strip, not synthesize an empty
    thinking frame."""
    plain = _raw_chunk({"content": "hi"})
    assert reasoning_entries_from_raw_chunk(plain) == []


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"choices": []},
        {"choices": [{"index": 0, "delta": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning_details": "junk"}}]},
        {"choices": "not-a-list"},
    ],
)
def test_raw_chunk_malformed_shapes_yield_no_entries(raw: Any) -> None:
    """Defensive (exception class): a truncated or mistyped provider
    frame must not raise inside the stream loop — a raise there kills
    the SSE body mid-run."""
    assert reasoning_entries_from_raw_chunk(raw) == []


def test_reasoning_entries_from_delta_prefers_the_split_list() -> None:
    """A provider that sends BOTH shapes in one delta must not have
    its scratchpad counted twice."""
    entries = reasoning_entries_from_delta(
        {
            "reasoning_details": [{"type": "reasoning.text", "text": SECRET}],
            "reasoning_content": SECRET,
        }
    )
    assert entries == [{"type": "reasoning.text", "text": SECRET}]


# --- chunk -> thinking delta (all provider shapes) ---


def test_reasoning_delta_from_chunk_reads_reasoning_content() -> None:
    """``reasoning_content`` on ``additional_kwargs`` is a thinking
    delta, same as the ``reasoning_details`` list."""
    chunk = AIMessageChunk(
        content="", additional_kwargs={"reasoning_content": "step one "}
    )
    assert reasoning_delta_from_chunk(chunk) == "step one "


def test_reasoning_delta_from_chunk_reads_reasoning_key() -> None:
    """``reasoning`` (string or object) is a thinking delta too."""
    as_string = AIMessageChunk(
        content="", additional_kwargs={"reasoning": "weighing"}
    )
    as_object = AIMessageChunk(
        content="", additional_kwargs={"reasoning": {"text": "weighing"}}
    )
    assert reasoning_delta_from_chunk(as_string) == "weighing"
    assert reasoning_delta_from_chunk(as_object) == "weighing"


def test_reasoning_delta_from_chunk_reads_summary_entries() -> None:
    """A ``reasoning.summary`` entry carries its prose under
    ``summary`` rather than ``text``; it is still thinking."""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": "read the filing"}
            ]
        },
    )
    assert reasoning_delta_from_chunk(chunk) == "read the filing"


def test_reasoning_delta_from_chunk_does_not_double_count() -> None:
    """Both shapes on one chunk yield the prose ONCE."""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={
            "reasoning_details": [{"text": SECRET}],
            "reasoning_content": SECRET,
        },
    )
    assert reasoning_delta_from_chunk(chunk) == SECRET


# --- the converter that keeps the channel alive past langchain-openai ---


def test_stock_chat_openai_drops_the_provider_reasoning_channel() -> None:
    """Why the converter exists (peer finding, pinned as a test).

    ``langchain-openai`` targets the official OpenAI spec and its
    delta converter builds ``additional_kwargs`` from
    ``function_call`` only — ``reasoning_details`` /
    ``reasoning_content`` are dropped on the floor. If this test ever
    fails, upstream started preserving the channel and the subclass
    below can go away.
    """
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model="fin/MiniMax-M3",
        base_url="http://localhost:20128/v1",
        api_key="sk-no-key-required",
    )
    generated = model._convert_chunk_to_generation_chunk(
        _raw_chunk(
            {
                "role": "assistant",
                "content": "",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": SECRET, "index": 0}
                ],
            }
        ),
        AIMessageChunk,
        None,
    )
    assert generated is not None
    assert "reasoning_details" not in generated.message.additional_kwargs


@pytest.mark.parametrize(
    "delta",
    [
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {"type": "reasoning.text", "text": SECRET, "index": 0}
            ],
        },
        {"role": "assistant", "content": "", "reasoning_content": SECRET},
        {"role": "assistant", "content": "", "reasoning": SECRET},
    ],
)
def test_converter_preserves_provider_reasoning_onto_the_chunk(
    delta: dict[str, Any],
) -> None:
    """Every shape the configured provider may emit survives onto
    ``additional_kwargs.reasoning_details`` — the channel the SSE
    mapper and the checkpointed replay both read."""
    model = reasoning_preserving_chat_openai()(
        model="fin/MiniMax-M3",
        base_url="http://localhost:20128/v1",
        api_key="sk-no-key-required",
    )
    generated = model._convert_chunk_to_generation_chunk(
        _raw_chunk(delta), AIMessageChunk, None
    )
    assert generated is not None
    chunk = generated.message
    assert reasoning_delta_from_chunk(chunk) == SECRET
    assert SECRET not in chunk.content


def test_converter_leaves_a_plain_content_chunk_alone() -> None:
    """No split channel in the delta -> no synthesized reasoning; the
    answer text passes through untouched so the inline-strip
    fallback stays the only other thinking source."""
    model = reasoning_preserving_chat_openai()(
        model="fin/MiniMax-M3",
        base_url="http://localhost:20128/v1",
        api_key="sk-no-key-required",
    )
    generated = model._convert_chunk_to_generation_chunk(
        _raw_chunk({"role": "assistant", "content": "Net sales rose."}),
        AIMessageChunk,
        None,
    )
    assert generated is not None
    assert generated.message.content == "Net sales rose."
    assert reasoning_delta_from_chunk(generated.message) == ""


def test_converter_passes_through_a_none_generation_chunk() -> None:
    """``_convert_chunk_to_generation_chunk`` returns ``None`` for
    frames the base class skips; the override must not dereference
    it."""
    model = reasoning_preserving_chat_openai()(
        model="fin/MiniMax-M3",
        base_url="http://localhost:20128/v1",
        api_key="sk-no-key-required",
    )
    assert (
        model._convert_chunk_to_generation_chunk(
            {"type": "content.delta"}, AIMessageChunk, None
        )
        is None
    )


# --- outbound: the checkpointed reasoning rides the next request ---


# The entry shape official MiniMax Chat Completions emits under
# ``reasoning_details`` and reads back off the assistant message.
# Verified live against ``MiniMax-M3`` at
# ``https://api.minimaxi.com/v1``: an instruction planted only in
# this field changes the next turn's answer (3/3), while the same
# thread without the field does not (0/3).
MINIMAX_ENTRY: dict[str, Any] = {
    "type": "reasoning.text",
    "id": "reasoning-text-1",
    "format": "MiniMax-response-v1",
    "index": 0,
    "text": "Read the 10-K cover page before quoting net sales.",
}


def _replay_thread(additional_kwargs: dict[str, Any]) -> list[Any]:
    """A thread whose ONE prior assistant turn carries ``kwargs``.

    Index 1 is that turn — the message whose reasoning the provider
    needs back to keep the chain of thought unbroken across the
    follow-up question at index 2.
    """
    return [
        HumanMessage(content="What were net sales?"),
        AIMessage(
            content="Net sales rose 8%.",
            additional_kwargs=additional_kwargs,
        ),
        HumanMessage(content="And operating margin?"),
    ]


def _configured_provider_model(cls: Any) -> Any:
    """Build ``cls`` against the configured provider's coordinates."""
    return cls(
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        api_key="sk-no-key-required",
    )


def test_stock_chat_openai_drops_prior_reasoning_from_the_request() -> None:
    """Why the outbound override exists (pinned as a test).

    ``_convert_message_to_dict`` copies only ``name`` / ``tool_calls``
    / ``function_call`` / ``audio`` off ``additional_kwargs``, so the
    reasoning captured on the inbound side never reaches the provider
    on the next call. If this test ever fails, upstream started
    forwarding the channel and the override below can go away.
    """
    from langchain_openai import ChatOpenAI

    model = _configured_provider_model(ChatOpenAI)
    payload = model._get_request_payload(
        _replay_thread({"reasoning_details": [MINIMAX_ENTRY]})
    )
    assert "reasoning_details" not in payload["messages"][1]


def test_outbound_request_replays_the_prior_turn_reasoning() -> None:
    """The checkpointed ``AIMessage``'s reasoning rides the next
    request on the key the configured provider reads."""
    model = _configured_provider_model(reasoning_preserving_chat_openai())
    payload = model._get_request_payload(
        _replay_thread({"reasoning_details": [MINIMAX_ENTRY]})
    )
    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_details"] == [MINIMAX_ENTRY]


def test_outbound_request_keeps_reasoning_entry_identity_verbatim() -> None:
    """Every replay field survives, not just the prose: the provider
    correlates entries by ``id`` / ``index`` and rejects a rewritten
    ``format``, so entries go out exactly as they were captured."""
    second = {
        "type": "reasoning.text",
        "id": "reasoning-text-2",
        "format": "MiniMax-response-v1",
        "index": 1,
        "text": "Cross-check the margin line.",
    }
    model = _configured_provider_model(reasoning_preserving_chat_openai())
    payload = model._get_request_payload(
        _replay_thread({"reasoning_details": [MINIMAX_ENTRY, second]})
    )
    assert payload["messages"][1]["reasoning_details"] == [
        MINIMAX_ENTRY,
        second,
    ]


def test_outbound_request_normalizes_reasoning_content_onto_the_key() -> None:
    """A turn checkpointed by a provider that only spelled the channel
    ``reasoning_content`` still replays as ``reasoning_details`` —
    the one key the configured provider accepts."""
    model = _configured_provider_model(reasoning_preserving_chat_openai())
    payload = model._get_request_payload(
        _replay_thread({"reasoning_content": "Read the cover page."})
    )
    assert payload["messages"][1]["reasoning_details"] == [
        {"type": "reasoning.text", "text": "Read the cover page."}
    ]


def test_outbound_request_is_byte_identical_without_prior_reasoning() -> None:
    """A thread whose turns carry no reasoning produces exactly the
    stock request — the override adds a key, it never rewrites one."""
    from langchain_openai import ChatOpenAI

    thread = _replay_thread({})
    stock = _configured_provider_model(ChatOpenAI)._get_request_payload(thread)
    preserving = _configured_provider_model(
        reasoning_preserving_chat_openai()
    )._get_request_payload(thread)
    assert preserving == stock
    assert all("reasoning_details" not in m for m in preserving["messages"])


def test_outbound_request_leaves_non_assistant_messages_alone() -> None:
    """``reasoning_details`` is an assistant-message field. A human or
    tool turn that happens to carry the key in its
    ``additional_kwargs`` must not grow one in the request."""
    model = _configured_provider_model(reasoning_preserving_chat_openai())
    payload = model._get_request_payload(
        [
            HumanMessage(
                content="What were net sales?",
                additional_kwargs={"reasoning_details": [MINIMAX_ENTRY]},
            ),
            ToolMessage(
                content='{"units": 12}',
                tool_call_id="call_1",
                additional_kwargs={"reasoning_details": [MINIMAX_ENTRY]},
            ),
        ]
    )
    assert all("reasoning_details" not in m for m in payload["messages"])


def test_outbound_request_replays_reasoning_beside_tool_calls() -> None:
    """The interleaved-thinking case: the assistant turn that opened a
    tool call carries BOTH its ``tool_calls`` and the reasoning that
    produced them into the next request."""
    model = _configured_provider_model(reasoning_preserving_chat_openai())
    payload = model._get_request_payload(
        [
            HumanMessage(content="How many units of QX-9?"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_inventory",
                        "args": {"sku": "QX-9"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
                additional_kwargs={"reasoning_details": [MINIMAX_ENTRY]},
            ),
            ToolMessage(content='{"units": 12}', tool_call_id="call_1"),
        ]
    )
    assistant = payload["messages"][1]
    assert assistant["reasoning_details"] == [MINIMAX_ENTRY]
    assert assistant["tool_calls"][0]["function"]["name"] == "get_inventory"


# --- assembly: the checkpointed message replays the reasoning ---


def test_assemble_normalizes_reasoning_content_for_replay() -> None:
    """A provider that only speaks ``reasoning_content`` still lands
    a replayable ``reasoning_details`` list on the checkpointed
    ``AIMessage`` — that is the field ``graph._extract_thinking_text``
    reads for the turn row and the next model call."""
    chunks = [
        AIMessageChunk(
            content="", additional_kwargs={"reasoning_content": "step one "}
        ),
        AIMessageChunk(
            content="", additional_kwargs={"reasoning_content": "step two"}
        ),
        AIMessageChunk(content="Net sales rose."),
    ]
    msg = assemble_final_ai_message(chunks)
    details = msg.additional_kwargs.get("reasoning_details")
    assert isinstance(details, list)
    assert "".join(d["text"] for d in details) == "step one step two"
    assert msg.content == "Net sales rose."


def test_assemble_does_not_duplicate_an_existing_details_list() -> None:
    """The canonical shape is already replayable; normalization must
    leave it exactly as the provider streamed it."""
    chunks = [
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [{"type": "reasoning.text", "text": "a"}]
            },
        ),
        AIMessageChunk(content="body"),
    ]
    msg = assemble_final_ai_message(chunks)
    assert msg.additional_kwargs["reasoning_details"] == [
        {"type": "reasoning.text", "text": "a"}
    ]


# --- fallback: split channel absent, inline blocks present ---


def test_inline_fallback_streams_thinking_when_split_channel_absent() -> None:
    """Error contract: when the provider drops or renames the split
    channel and inlines ``<think>`` instead, the strip path still
    routes the scratchpad to ``thinking`` — reasoning is surfaced,
    never swallowed into the answer."""
    chunks = [
        AIMessageChunk(content="<thi"),
        AIMessageChunk(content=f"nk>{SECRET}</think>Net sales "),
        AIMessageChunk(content="rose 8%."),
    ]
    frames = _map_chunks_to_frames(chunks)
    assert _text_of(frames, "thinking") == SECRET
    assert _text_of(frames, "answer") == "Net sales rose 8%."


def test_inline_fallback_survives_assembly_and_the_citation_gate() -> None:
    """The same absent-split-channel run: the assembled message keeps
    the inline block for the turn row, and the gate-visible prose
    (``strip_thinking_from_answer``) is thinking-free."""
    from fin.host.stream_map import inline_thinking_segments

    chunks = [
        AIMessageChunk(content=f"<think>{SECRET}</think>"),
        AIMessageChunk(content="Net sales rose 8%."),
    ]
    msg = assemble_final_ai_message(chunks)
    assert inline_thinking_segments(str(msg.content)) == [SECRET]
    gate_prose = strip_thinking_from_answer(str(msg.content))
    assert SECRET not in gate_prose
    assert "Net sales rose 8%." in gate_prose


# --- SC60 across every shape: no thinking prose in any answer frame ---


@pytest.mark.parametrize(
    "reasoning_kwargs",
    [
        {"reasoning_details": [{"type": "reasoning.text", "text": SECRET}]},
        {"reasoning_content": SECRET},
        {"reasoning": SECRET},
        {"reasoning": {"text": SECRET}},
        {
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": SECRET}
            ]
        },
    ],
)
def test_every_split_shape_streams_thinking_and_never_the_answer(
    reasoning_kwargs: dict[str, Any],
) -> None:
    """SC60 across the shapes the provider may emit: the scratchpad
    reaches ``thinking`` frames and NO ``answer`` frame contains it."""
    chunks = [
        AIMessageChunk(content="", additional_kwargs=reasoning_kwargs),
        AIMessageChunk(content="Net sales "),
        AIMessageChunk(content="rose 8%."),
    ]
    frames = _map_chunks_to_frames(chunks)
    assert _text_of(frames, "thinking") == SECRET
    answer = _text_of(frames, "answer")
    assert answer == "Net sales rose 8%."
    assert SECRET not in answer
    assert all(SECRET not in text for name, text in frames if name == "answer")


def test_inline_block_inside_a_split_channel_run_still_never_leaks() -> None:
    """Belt and braces: a provider that uses the split channel AND
    inlines a block in the same run keeps both out of the answer."""
    chunks = [
        AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_details": [{"text": "planning. "}]},
        ),
        AIMessageChunk(content=f"<think>{SECRET}</think>Net sales rose 8%."),
    ]
    frames = _map_chunks_to_frames(chunks)
    assert _text_of(frames, "thinking") == f"planning. {SECRET}"
    assert _text_of(frames, "answer") == "Net sales rose 8%."
