"""The parser-carried heading span contract (SC7, the HTML half).

SC7 says HTML heading **tags** are the window title source, while the
frozen unit keeps the heading text only — the tag never enters the
frozen text. The title source therefore travels beside the text:
``_html.py`` records which of a block's lines came from inside an
``h1``-``h6`` tag, ``build_unit`` maps those raw line indices onto
spans of the frozen text (``ParsedUnit.heading_spans``), and the
splitter unions them with the classifier's own set
(``chunking.effective_heading_lines``) before
``fin.corpus.window.window_spans`` scopes the titles.

What this module locks, on the three layers:

* the mapping (``_frozen_line_spans``) — raw line index → frozen span,
  including the index shift a dropped blank line introduces; the three
  malformed-claim shapes (out of range / repeated / unsorted) fail
  named with ``heading_span_invalid``; an empty heading contributes no
  span and no error;
* the merge (``merge_structured_blocks``) — the second block's heading
  index shifts by the predecessor's line count, and the plain-string
  door (``merge_blocks``) still reproduces its old byte-for-byte
  output;
* the union (``effective_heading_lines``) — classifier ∪ spans,
  sorted, deduplicated, and gated by the ``heading`` rank; a unit with
  no spans yields exactly the classifier's own set, so no format
  without heading tags can move.

The classifier itself is deliberately NOT widened (ATX + UPPERCASE
only). ``tests/corpus/test_window_extract_contract.py`` locks that
negative from the product path; this module never asserts a Title Case
line is a classifier heading.
"""

from __future__ import annotations

import pytest

from fin.corpus.chunking import effective_heading_lines, split_unit
from fin.corpus.errors import ParseHeadingSpanError
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.params import BOUNDARY_HEADING
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import (
    BLOCK_MERGE_CAP_CHARS,
    StructuredBlock,
    build_unit,
    merge_blocks,
    merge_structured_blocks,
)
from fin.corpus.window import (
    classifier_heading_lines,
    is_heading_line,
    window_spans,
)

DOCUMENT_ID = "HEADING-SPAN"
VERSION = "a" * 64

# A representative boundary chain: every params row carries the
# ``heading`` rank today, so a missing-rank chain is the explicit
# "this corpus has no headings" statement this module also locks.
_CHAIN_WITH_HEADING = (BOUNDARY_HEADING, 5)
_CHAIN_WITHOUT_HEADING = (4, 5)


def _sentences(n: int, start: int = 0) -> str:
    """n deterministic ~100-char sentences, each ending in a period."""
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _unit(text: str, *, heading_lines: tuple[int, ...] = (), unit: int = 1):
    """One frozen unit through the single construction point."""
    return build_unit(
        fmt="html",
        source_sha256=VERSION,
        document_id=DOCUMENT_ID,
        version=VERSION,
        unit=unit,
        text=text,
        heading_lines=heading_lines,
    )


# ---------------------------------------------------------------------------
# The mapping: raw line index -> frozen span
# ---------------------------------------------------------------------------


def test_default_heading_lines_yields_no_spans_and_no_window_move() -> None:
    # The defaulted field is the compatibility guarantee: a parser that
    # knows no structural headings produces ``heading_spans == ()`` and
    # its window tree is exactly what the classifier alone produced
    # before the channel existed.
    text = "# Alpha\nbody one\n# Beta\nbody two"
    unit = _unit(text)
    assert unit.heading_spans == ()
    assert unit.text == normalize_frozen_text(text)
    assert split_unit(unit, doc_type="narrative").parents == split_unit(
        _unit(text), doc_type="narrative"
    ).parents
    assert [w.title for w in window_spans(unit.text)] == [
        w.title
        for w in window_spans(
            unit.text, heading_lines=classifier_heading_lines(unit.text)
        )
    ]


def test_heading_index_maps_to_its_frozen_span_when_a_blank_shifts_it():
    # The main correctness risk of the whole channel: the freeze drops
    # blank lines, so the caller's index numbering is the OUTLET's.
    # With the leading blank line the raw line list is
    # ``["", "Revenue", "body"]`` — the heading is index 1 raw, and the
    # frozen text is ``"Revenue\nbody"`` (no leading empty line), so its
    # span starts at 0.
    raw = "\nRevenue\nbody text here"
    unit = _unit(raw, heading_lines=(1,))
    assert unit.text == "Revenue\nbody text here"
    assert unit.heading_spans == ((0, len("Revenue")),)
    assert unit.text[unit.heading_spans[0][0] : unit.heading_spans[0][1]] == (
        "Revenue"
    )
    assert is_heading_line("Revenue") is False, "the classifier stays narrow"


def test_heading_index_after_a_dropped_blank_finds_the_right_line() -> None:
    # The same shift with a line in front of the dropped blank: the raw
    # list is ``["intro line", "", "Revenue", "body"]`` (the blank is
    # index 1), and the frozen text drops it, so the heading is frozen
    # line 1 while its raw index is 2. Walking only the claimed line
    # would resolve the span to ``intro line``.
    raw = "intro line\n\nRevenue\nbody text"
    unit = _unit(raw, heading_lines=(2,))
    assert unit.text == "intro line\nRevenue\nbody text"
    start, end = unit.heading_spans[0]
    assert unit.text[start:end] == "Revenue"
    # and the classifier, run over the frozen text, does NOT see it
    assert classifier_heading_lines(unit.text) == ()


def test_multiple_heading_indices_keep_text_order() -> None:
    # Two claimed lines: the spans come back in text order, each over
    # its own line (the intervening body line is walked, not emitted).
    raw = "Revenue\nbody one\nCosts\nbody two"
    unit = _unit(raw, heading_lines=(0, 2))
    assert [unit.text[s:e] for s, e in unit.heading_spans] == [
        "Revenue",
        "Costs",
    ]
    # the same two lines handed in out of order are a named failure
    with pytest.raises(ParseHeadingSpanError) as excinfo:
        _unit(raw, heading_lines=(2, 0))
    assert excinfo.value.code == "heading_span_invalid"


def test_heading_span_survives_the_freeze_rewriting_the_line() -> None:
    # The span is computed by walking the outlet's own output, so a
    # line the outlet rewrites still maps to the frozen characters:
    # this helper never re-implements a replacement rule, it counts the
    # outlet's pieces. The zero-width space and the soft hyphen below
    # are removed and the full-width digit folds to ASCII, so the span
    # length is the FROZEN length, not the raw line's.
    raw = "\u200bRev\uff11enue\u00ad"
    unit = _unit(raw, heading_lines=(0,))
    assert unit.text == normalize_frozen_text(raw) == "Rev1enue"
    assert unit.heading_spans == ((0, len("Rev1enue")),)
    assert len(raw) != len(unit.text), "precondition: the outlet rewrote"


# ---------------------------------------------------------------------------
# Malformed claims fail named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", [3, 99, -1])
def test_out_of_range_heading_index_is_a_named_failure(index: int) -> None:
    with pytest.raises(ParseHeadingSpanError) as excinfo:
        _unit("Alpha\nBeta\nGamma", heading_lines=(index,))
    assert excinfo.value.code == "heading_span_invalid"


def test_repeated_heading_index_is_a_named_failure() -> None:
    with pytest.raises(ParseHeadingSpanError) as excinfo:
        _unit("Alpha\nBeta\nGamma", heading_lines=(1, 1))
    assert excinfo.value.code == "heading_span_invalid"


def test_unsorted_heading_indices_are_a_named_failure() -> None:
    with pytest.raises(ParseHeadingSpanError) as excinfo:
        _unit("Alpha\nBeta\nGamma", heading_lines=(2, 0))
    assert excinfo.value.code == "heading_span_invalid"


def test_empty_heading_line_yields_no_span_and_no_error() -> None:
    # An ``<h2></h2>`` contributes no line at all (the parser's
    # ``_flush_line`` appends only non-empty text), so the index points
    # at a whitespace-only raw line the outlet drops: no span, no
    # failure — the malformed-document shape stays deterministic.
    raw = "Alpha\n   \nGamma"
    unit = _unit(raw, heading_lines=(1,))
    assert unit.heading_spans == ()
    assert unit.text == "Alpha\nGamma"


# ---------------------------------------------------------------------------
# The merge rule: one authority, two doors
# ---------------------------------------------------------------------------


def test_merge_structured_blocks_shifts_the_second_blocks_indices() -> None:
    first = StructuredBlock(("Alpha heading", "alpha body"), (0,))
    second = StructuredBlock(("Beta heading", "beta body"), (0,))
    merged = merge_structured_blocks([first, second])
    assert len(merged) == 1
    assert merged[0].lines == (
        "Alpha heading",
        "alpha body",
        "Beta heading",
        "beta body",
    )
    # the second block's index 0 moved past the first block's 2 lines
    assert merged[0].heading_lines == (0, 2)


def test_merge_structured_blocks_keeps_blocks_separate_above_the_cap() -> None:
    big = "x" * BLOCK_MERGE_CAP_CHARS
    blocks = [
        StructuredBlock((big,), (0,)),
        StructuredBlock(("Beta heading",), (0,)),
    ]
    merged = merge_structured_blocks(blocks)
    assert len(merged) == 2
    assert merged[1].heading_lines == (0,)


def test_merge_blocks_reproduces_the_plain_string_merge_bytes() -> None:
    # The string door must stay byte-for-byte what it was: compare it
    # against an independent re-implementation of the documented rule
    # (the original test was ``len(prev) + len(block) + 1 <= cap``).
    def reference(blocks: list[str], cap: int) -> list[str]:
        out: list[str] = []
        for block in blocks:
            if out and len(out[-1]) + len(block) + 1 <= cap:
                out[-1] = out[-1] + "\n" + block
            else:
                out.append(block)
        return out

    for blocks in (
        ["Alpha", "Beta", "Gamma"],
        ["x" * (BLOCK_MERGE_CAP_CHARS - 5), "small"],
        ["x" * BLOCK_MERGE_CAP_CHARS, "small", "tail"],
        ["a\nb", "c\nd"],
    ):
        assert merge_blocks(list(blocks)) == reference(
            list(blocks), BLOCK_MERGE_CAP_CHARS
        )


def test_merge_blocks_agrees_with_the_structured_door() -> None:
    # The two doors answer the same merge question: joining the
    # structured output back into strings equals the string door.
    blocks = ["Alpha heading", "alpha body", "Beta heading", "beta body"]
    structured = merge_structured_blocks(
        [StructuredBlock(tuple(block.split("\n"))) for block in blocks]
    )
    assert [
        "\n".join(block.lines) for block in structured
    ] == merge_blocks(blocks)


# ---------------------------------------------------------------------------
# The union the splitter hands the window rule
# ---------------------------------------------------------------------------


def test_effective_heading_lines_is_the_classifier_set_with_no_spans() -> None:
    text = "# Alpha\nbody one\nbody two"
    assert effective_heading_lines(text, (), _CHAIN_WITH_HEADING) == (
        classifier_heading_lines(text)
    )


def test_effective_heading_lines_unions_the_two_sources() -> None:
    # ``# Alpha`` is the classifier's; the span at the ``Beta`` line is
    # the parser's (the line itself is Title Case, invisible to the
    # classifier). Both must survive: the union is additive only.
    text = "# Alpha\nbody one\nBeta\nbody two"
    beta_start = text.index("Beta")
    spans = ((beta_start, beta_start + len("Beta")),)
    assert effective_heading_lines(text, spans, _CHAIN_WITH_HEADING) == (
        (0, len("# Alpha")),
        spans[0],
    )


def test_effective_heading_lines_dedupes_and_sorts() -> None:
    # A span the classifier also found is not duplicated, and an
    # out-of-order hand-in still leaves the result sorted (the window
    # rule reads the spans in order; unsorted input silently yields a
    # wrong tree).
    text = "# Alpha\nbody one\nBeta\nbody two"
    alpha = (0, len("# Alpha"))
    beta = (text.index("Beta"), text.index("Beta") + len("Beta"))
    result = effective_heading_lines(text, (beta, alpha), _CHAIN_WITH_HEADING)
    assert result == (alpha, beta)
    assert len(result) == len(set(result))


def test_effective_heading_lines_empty_without_the_heading_rank() -> None:
    text = "# Alpha\nbody one"
    spans = classifier_heading_lines(text)
    assert effective_heading_lines(text, spans, _CHAIN_WITHOUT_HEADING) == ()


def test_unit_without_spans_windows_exactly_as_the_classifier_did() -> None:
    # The no-move guarantee stated over the splitter: for a unit whose
    # parser knows no heading tags, the parent spans and titles are
    # identical to the classifier-only derivation.
    text = "# Alpha\n" + _sentences(60)
    unit = _unit(text)
    assert unit.heading_spans == ()
    chunked = split_unit(unit, doc_type="narrative")
    expected = window_spans(
        unit.text, heading_lines=classifier_heading_lines(unit.text)
    )
    assert [p.text for p in chunked.parents] == [
        w.text_of(unit.text) for w in expected
    ]
    assert [p.title for p in chunked.parents] == [w.title for w in expected]


def test_parser_carried_span_titles_a_title_case_html_heading() -> None:
    # The end-to-end product path, minimal shape: the ``<h2>`` tag
    # (not the line) makes the title, and the tag never enters the
    # frozen text.
    source = (
        "<html><body><main><h2>Revenue</h2>"
        "<p>Total net sales increased during the quarter.</p>"
        "</main></body></html>"
    ).encode("utf-8")
    result = parse_document(
        source, fmt="html", document_id=DOCUMENT_ID, version=VERSION
    )
    unit = result.units[0]
    assert unit.text == (
        "Revenue\nTotal net sales increased during the quarter."
    )
    assert unit.heading_spans == ((0, len("Revenue")),)
    assert [
        p.title for p in split_unit(unit, doc_type="narrative").parents
    ] == ["Revenue"]


# ---------------------------------------------------------------------------
# Inline markup inside the heading tag
# ---------------------------------------------------------------------------


def test_nested_inline_heading_still_marks_its_line() -> None:
    # ``<h2><em>Rev</em></h2>`` — the heading's text arrives through
    # nested tags, so the flag must be tag-independent: it is set by
    # the ``h2`` open tag and cleared by the ``h2`` close tag, not by
    # the inline element in between.
    source = (
        "<html><body><main><h2><em>Rev</em>enue</h2>"
        "<p>Total net sales increased.</p>"
        "</main></body></html>"
    ).encode("utf-8")
    result = parse_document(
        source, fmt="html", document_id=DOCUMENT_ID, version=VERSION
    )
    unit = result.units[0]
    assert unit.text == "Revenue\nTotal net sales increased."
    assert unit.heading_spans == ((0, len("Revenue")),)
    assert [
        p.title for p in split_unit(unit, doc_type="narrative").parents
    ] == ["Revenue"]


def test_heading_after_a_pre_block_maps_past_its_embedded_newlines() -> None:
    # ``<pre>`` text carries real newlines, so ONE parser line becomes
    # several frozen lines and ``StructuredBlock.heading_lines`` (which
    # indexes the block's lines) is a different index space than
    # ``build_unit``'s ``text.split("\n")``. Without the shift the
    # second heading resolves onto a body line of the ``<pre>`` block —
    # measured on the committed fixtures before the shift was added.
    source = (
        "<html><body><main><h2>Unit 2</h2>"
        "<pre>body line one\nbody line two\nbody line three</pre>"
        "<h2>Unit 25</h2><pre>tail</pre>"
        "</main></body></html>"
    ).encode("utf-8")
    result = parse_document(
        source, fmt="html", document_id=DOCUMENT_ID, version=VERSION
    )
    unit = result.units[0]
    assert unit.text == (
        "Unit 2\nbody line one\nbody line two\nbody line three\n"
        "Unit 25\ntail"
    )
    assert [unit.text[s:e] for s, e in unit.heading_spans] == [
        "Unit 2",
        "Unit 25",
    ]
    assert [p.title for p in split_unit(unit, doc_type="narrative").parents] == [
        "Unit 2",
        "Unit 25",
    ]


def test_uppercase_html_heading_keeps_its_classifier_title() -> None:
    # The union's other half, on the product path: an UPPERCASE HTML
    # heading was already titled before the channel existed and must
    # stay titled — the parser span and the classifier agree on the
    # same line, and the dedup keeps one region, not two.
    source = (
        "<html><body><main><h2>REVENUE</h2>"
        "<p>Total net sales increased.</p>"
        "</main></body></html>"
    ).encode("utf-8")
    result = parse_document(
        source, fmt="html", document_id=DOCUMENT_ID, version=VERSION
    )
    unit = result.units[0]
    assert unit.heading_spans == ((0, len("REVENUE")),)
    assert is_heading_line("REVENUE")
    assert [
        p.title for p in split_unit(unit, doc_type="narrative").parents
    ] == ["REVENUE"]
