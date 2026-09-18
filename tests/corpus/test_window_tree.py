"""The title-scoped extract window rule (抽图窗) — spec 1 of 4.

Locks the window layer of ``specs/extract-window-tree.md`` (SC2 / SC3
on the rule itself, plus the boundaries the spec's negative / empty /
overflow / concurrent test classes name):

* SC2 — a heading body at or below the cap is exactly one window with
  zero overlap;
* SC3 — an oversized body splits BODY ONLY into windows sharing the
  same title, adjacent windows overlapping ~8% of the preceding
  window; the heading is not a second body cutter;
* the cap governs the body span, never the heading line;
* empty / heading-only / no-heading (OCR-like) / exactly-at-cap
  boundaries;
* determinism: the same frozen text yields byte-identical windows
  (SC6; ``test_chunking.py`` locks the block-level identity, and
  ``test_window_extract_contract.py`` locks the concurrent /
  version-stamped half).
"""

from __future__ import annotations

import pytest

from fin.corpus import chunking, window
from fin.corpus.window import (
    EXTRACT_WINDOW_CHARS,
    EXTRACT_WINDOW_TOKENS,
    WINDOW_CHARS_PER_TOKEN,
    WINDOW_OVERLAP_PERCENT,
    WINDOW_OVERLAP_TOLERANCE,
    count_window_tokens,
    is_heading_line,
    window_spans,
)


def _sentences(n: int, start: int = 0) -> str:
    """n deterministic ~100-char sentences, each ending in a period."""
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _overflow_text(heading: str = "# Alpha") -> str:
    """A heading body comfortably past the cap (the SC3 fixture)."""
    return f"{heading}\n" + _sentences(60)


def _overlap_pairs(text: str, windows):
    """``(overlap_len, preceding_window_len, preceding_body_len)`` per
    adjacent pair of the same title. The SC3 band is measured on the
    window span (the spec's "overlap length / window length"); the
    exact-arithmetic check uses the BODY length — the quantity the
    rule's integer formula actually scales (`window._next_slice`
    computes ``overlap`` from the body slice, so tying the constant to
    the total span breaks for any heading whose length is not a
    multiple that divides cleanly).
    """
    pairs = []
    for prev, nxt in zip(windows, windows[1:]):
        if prev.title != nxt.title:
            continue
        overlap = prev.end - nxt.start
        if overlap > 0:
            pairs.append(
                (
                    overlap,
                    prev.end - prev.start,
                    prev.body_end - prev.body_start,
                )
            )
    return pairs


# ---------------------------------------------------------------------------
# SC2: a body at or below the cap is one window, overlap 0
# ---------------------------------------------------------------------------


def test_sc2_body_within_cap_is_exactly_one_window_with_zero_overlap() -> None:
    text = "# Alpha\n" + _sentences(30)
    windows = window_spans(text)
    assert len(windows) == 1, "a <=1200-token body is one window (SC2)"
    only = windows[0]
    assert only.title == "Alpha"
    # The span covers heading + body; its tail whitespace is trimmed
    # like every seam, so block text stays a normalization fixed point
    # (SC17) — the body content itself is the whole body.
    assert only.start == 0 and only.end == len(text.rstrip())
    assert only.body_of(text) == text[text.index("\n") + 1 :].rstrip()
    assert count_window_tokens(only.body_of(text)) <= EXTRACT_WINDOW_TOKENS
    assert _overlap_pairs(text, windows) == []


def test_sc2_body_exactly_at_the_cap_is_one_window() -> None:
    # The cap is inclusive: body chars == EXTRACT_WINDOW_CHARS still
    # yields a single window (SC2 / SC8 boundary).
    body = "x" * EXTRACT_WINDOW_CHARS
    text = "# Alpha\n" + body
    windows = window_spans(text)
    assert len(windows) == 1
    assert windows[0].body_of(text) == body
    assert len(windows[0].body_of(text)) == EXTRACT_WINDOW_CHARS


def test_body_one_char_over_the_cap_overlows_into_two_windows() -> None:
    body = "x" * (EXTRACT_WINDOW_CHARS + 1)
    text = "# Alpha\n" + body
    windows = window_spans(text)
    assert len(windows) == 2
    assert windows[0].body_of(text) == body[:EXTRACT_WINDOW_CHARS]
    assert windows[1].body_of(text)[-1] == "x"
    for w in windows:
        assert len(w.body_of(text)) <= EXTRACT_WINDOW_CHARS


# ---------------------------------------------------------------------------
# SC3: overflow keeps one heading and overlaps adjacent windows ~8%
# ---------------------------------------------------------------------------


def test_sc3_overflow_windows_share_the_title_and_overlap_about_eight_percent() -> None:
    text = _overflow_text()
    windows = window_spans(text)
    assert len(windows) >= 2, "precondition: the body exceeds the cap"
    assert {w.title for w in windows} == {"Alpha"}
    for w in windows:
        assert len(w.body_of(text)) <= EXTRACT_WINDOW_CHARS
    low, high = WINDOW_OVERLAP_TOLERANCE
    pairs = _overlap_pairs(text, windows)
    assert len(pairs) == len(windows) - 1, "every adjacent pair must overlap"
    for overlap, prev_len, prev_body in pairs:
        percent = overlap * 100 / prev_len
        assert low <= percent <= high, (overlap, prev_len, percent)
    # The rule is integer arithmetic over the BODY slice (what
    # ``window._next_slice`` scales): the percent is the constant, not
    # a coincidence of one window size. Total-span ratios drift by the
    # heading's length; the body ratio does not.
    for overlap, _, prev_body in pairs:
        assert overlap == prev_body * WINDOW_OVERLAP_PERCENT // 100


def test_sc3_heading_is_not_a_second_body_cutter() -> None:
    text = _overflow_text()
    heading = "# Alpha"
    windows = window_spans(text)
    assert len(windows) >= 2
    assert windows[0].text_of(text).startswith(heading)
    body_first = text.index("\n") + 1
    assert windows[0].body_start == body_first, "body starts after the heading line"
    for w in windows[1:]:
        assert not w.text_of(text).startswith(heading), (
            "later windows must not re-emit the heading"
        )
        assert w.start > windows[0].start


def _non_whitespace_covered(text: str, windows) -> set[int]:
    covered: set[int] = set()
    for w in windows:
        covered.update(range(w.body_start, w.body_end))
    return {i for i in covered if not text[i].isspace()}


def test_sc3_windows_are_ordered_and_cover_their_body() -> None:
    text = _overflow_text()
    windows = window_spans(text)
    body_start = text.index("\n") + 1
    assert windows[0].body_start == body_start
    assert windows[-1].body_end >= len(text.rstrip())
    for prev, nxt in zip(windows, windows[1:]):
        assert nxt.start < prev.end, "adjacent windows overlap (not a gap)"
        assert nxt.body_start < nxt.body_end
        # The overlap is the only shared span: the next window's body
        # starts exactly where the previous window's body would have
        # continued past its own end.
        assert nxt.body_start == prev.body_end - (prev.end - nxt.start)
    # every non-whitespace body char of the region is inside a window
    # (the seams trim whitespace only — no content is ever skipped)
    assert _non_whitespace_covered(text, windows) == {
        i for i in range(body_start, len(text)) if not text[i].isspace()
    }


def test_sc3_overflow_counts_scale_with_body_length() -> None:
    # 2 windows at ~1.25x cap, more as the body grows — the overflow
    # branch, not a second splitter with its own thresholds.
    smaller = window_spans(_overflow_text())
    bigger = window_spans(_overflow_text() + _sentences(60))
    assert len(bigger) > len(smaller)


def test_non_adjacent_windows_are_not_required_to_overlap() -> None:
    # SC3 states adjacency only; this pins the reading that the rule
    # never reaches past the immediately preceding window.
    text = "# Alpha\n" + _sentences(200)
    windows = window_spans(text)
    assert len(windows) >= 3
    for first, second, third in zip(windows, windows[1:], windows[2:]):
        assert third.start >= first.end - (first.end - second.start)


# ---------------------------------------------------------------------------
# Title scoping
# ---------------------------------------------------------------------------


def test_each_heading_starts_its_own_region() -> None:
    text = "# Alpha\nbody one\n# Beta\nbody two\n# Gamma\nbody three"
    windows = window_spans(text)
    assert [w.title for w in windows] == ["Alpha", "Beta", "Gamma"]
    assert windows[1].body_of(text) == "body two"
    assert windows[1].start == text.index("# Beta")
    assert windows[2].start == text.index("# Gamma")
    assert windows[2].body_of(text) == "body three"


def test_region_before_the_first_heading_is_titleless() -> None:
    text = "plain preamble\n# Alpha\nbody one"
    windows = window_spans(text)
    assert windows[0].title is None
    assert windows[0].text_of(text) == "plain preamble"
    assert windows[1].title == "Alpha"
    assert windows[1].body_start == windows[1].start + len("# Alpha\n")


def test_no_heading_at_all_is_one_titleless_window() -> None:
    # The OCR / textless shape: no heading tags, so title is None and
    # the whole frozen text is the body (ADR 0054).
    text = _sentences(10).rstrip()
    windows = window_spans(text)
    assert len(windows) == 1
    assert windows[0].title is None
    assert windows[0].body_start == 0 and windows[0].body_end == len(text)


def test_heading_title_strips_the_hash_run() -> None:
    windows = window_spans("### Quarterly Results\nbody")
    assert windows[0].title == "Quarterly Results"


def test_uppercase_heading_line_is_a_region_start() -> None:
    text = "QUARTERLY RESULTS\nbody one\nANOTHER SECTION\nbody two"
    assert [w.title for w in window_spans(text)] == [
        "QUARTERLY RESULTS",
        "ANOTHER SECTION",
    ]


# ---------------------------------------------------------------------------
# Cap semantics: the body span, not the heading
# ---------------------------------------------------------------------------


def test_cap_governs_the_body_not_the_heading() -> None:
    # A body of exactly the cap plus a long heading still yields one
    # window: the heading does not consume the body budget (SC2 + SC8).
    heading = "# " + "L" * 400
    text = heading + "\n" + "x" * EXTRACT_WINDOW_CHARS
    windows = window_spans(text)
    assert len(windows) == 1
    assert len(windows[0].text_of(text)) > EXTRACT_WINDOW_CHARS


def test_count_window_tokens_is_the_documented_estimate() -> None:
    assert count_window_tokens("x" * EXTRACT_WINDOW_CHARS) == EXTRACT_WINDOW_TOKENS
    assert count_window_tokens("x" * 3) == 0
    assert WINDOW_CHARS_PER_TOKEN == 4
    assert EXTRACT_WINDOW_CHARS == EXTRACT_WINDOW_TOKENS * WINDOW_CHARS_PER_TOKEN


def test_window_bodies_are_verbatim_slices() -> None:
    text = _overflow_text()
    for w in window_spans(text):
        assert w.text_of(text) == text[w.start : w.end]
        assert w.body_of(text) == text[w.body_start : w.body_end]


def test_seams_carry_no_leading_or_trailing_whitespace() -> None:
    text = "# Alpha\n" + " \n".join(_sentences(i) for i in (40, 40))
    for w in window_spans(text):
        body = w.body_of(text)
        if body:
            assert body[0] not in " \t\n"
            assert body[-1] not in " \t\n"
        assert w.text_of(text)[0] not in " \t\n"


def test_every_body_char_is_reachable_from_a_window() -> None:
    text = "# Alpha\n" + "".join(f"row {i} | {i * 7}\n" for i in range(900))
    windows = window_spans(text)
    assert len(windows) >= 2
    assert _non_whitespace_covered(text, windows) == {
        i
        for i in range(windows[0].body_start, len(text))
        if not text[i].isspace()
    }


# ---------------------------------------------------------------------------
# Empty / negative boundaries
# ---------------------------------------------------------------------------


def test_empty_text_yields_no_window() -> None:
    assert window_spans("") == ()


def test_heading_with_empty_body_is_one_heading_window() -> None:
    windows = window_spans("# Alpha")
    assert len(windows) == 1
    assert windows[0].title == "Alpha"
    assert windows[0].text_of("# Alpha") == "# Alpha"
    assert windows[0].body_start == windows[0].body_end == len("# Alpha")


def test_heading_followed_by_next_heading_has_an_empty_body() -> None:
    text = "# Alpha\n# Beta\nbody"
    windows = window_spans(text)
    assert windows[0].body_start == windows[0].body_end
    assert windows[1].title == "Beta"


# ---------------------------------------------------------------------------
# Single authority + determinism
# ---------------------------------------------------------------------------


def test_heading_classifier_has_one_authority() -> None:
    # The splitter must consume this module's classifier, not keep a
    # private copy: the two layers can then never disagree about where
    # a heading starts (ADR 0053 / 0060 one window tree).
    assert chunking.is_heading_line is window.is_heading_line
    assert not hasattr(chunking, "_is_heading_line")
    assert is_heading_line("# Alpha")
    assert is_heading_line("QUARTERLY RESULTS")
    assert not is_heading_line("lowercase heading")
    assert not is_heading_line("QUARTERLY RESULTS.")
    assert not is_heading_line("AB")


def test_window_spans_is_deterministic() -> None:
    text = _overflow_text() + "".join(
        f"| row {i} | value {i} |\n" for i in range(200)
    )
    first, second = window_spans(text), window_spans(text)
    assert first == second
    # SC6 names the fields, not only the object: ids/text and the span
    # offsets must match explicitly, so equality of the frozen
    # dataclass cannot hide a field the rule stopped setting.
    assert [w.ordinal for w in first] == [w.ordinal for w in second]
    assert [w.text_of(text) for w in first] == [w.text_of(text) for w in second]
    assert [
        (w.start, w.end, w.body_start, w.body_end) for w in first
    ] == [(w.start, w.end, w.body_start, w.body_end) for w in second]
    assert [w.title for w in first] == [w.title for w in second]


def test_heading_lines_override_is_honoured() -> None:
    # Callers that classified their own lines can hand the spans in;
    # an empty sequence means "this text has no headings".
    text = "# Alpha\nbody one\n# Beta\nbody two"
    assert len(window_spans(text, heading_lines=())) == 1
    assert window_spans(text, heading_lines=((0, 7),))[0].title == "Alpha"


@pytest.mark.parametrize(
    "text",
    [
        "QUARTERLY RESULTS\n# Alpha\n" + _sentences(60),
        "# Alpha\n" + _sentences(60) + "\n# Beta\n" + _sentences(60),
        _sentences(120),
    ],
)
def test_overflow_is_never_a_gap(text: str) -> None:
    windows = window_spans(text)
    assert windows
    for prev, nxt in zip(windows, windows[1:]):
        if prev.title == nxt.title:
            assert nxt.start < prev.end
        else:
            assert nxt.start >= prev.end
