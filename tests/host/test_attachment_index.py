"""``fin.host.attachment.attachment_index`` — the thread-private derived index.

The module is a pure function set: no I/O, no store, no lock. These tests
are therefore written against the spec's behaviour, not against a
fixture-driven code path. Coverage follows the contract the attachment
store consumes:

* one normalization pipeline behind both ``index_terms`` and
  ``query_terms`` — indexed text and query text may never disagree on
  what a term is;
* postings keyed by the frozen unit's 1-based ordinal, which is the same
  ordinal ``source_ref.unit`` addresses in a read (spec: the retrieval
  text and the read text are one versioned frozen-unit source);
* a deterministic hit order, so a rebuilt index for the same parsed
  version reproduces the same byte sequence;
* ``is_usable`` as the ``ready`` gate: an empty or malformed derived
  index must never be reported as searchable.
"""

from __future__ import annotations

import dataclasses

import pytest

from fin.host.attachment.attachment_index import (
    IndexHit,
    build_postings,
    index_terms,
    is_usable,
    query_terms,
    search,
)
from fin.host.attachment.attachment_store import ParsedAttachmentUnit


def _units(*texts: tuple[int, str]) -> list[ParsedAttachmentUnit]:
    """Frozen units from ``(unit, text)`` pairs."""
    return [ParsedAttachmentUnit(unit=u, text=t) for u, t in texts]


# ----- the single normalization pipeline -----


def test_index_terms_lowercases_splits_and_dedups_in_first_seen_order() -> None:
    assert index_terms("Hello, WORLD! hello World") == ("hello", "world")


def test_index_terms_on_empty_text() -> None:
    assert index_terms("") == ()


def test_index_terms_on_whitespace_only_text() -> None:
    """Spaces, tabs, newlines and form feeds carry no term.

    A whitespace-only parse product must reach the index as no postings
    at all, so ``is_usable`` refuses to call it searchable rather than
    storing a phantom document.
    """
    assert index_terms(" \t\n\r\v\f  \n") == ()


def test_index_terms_drops_single_character_tokens() -> None:
    """Length < 2 is noise for a postings index: article letters, stray
    digits, list bullets rendered as one character."""
    assert index_terms("a I 3 x revenue 42") == ("revenue", "42")


def test_index_terms_splits_on_punctuation_not_only_on_spaces() -> None:
    assert index_terms("revenue;growth:margin,margin") == (
        "revenue",
        "growth",
        "margin",
    )


def test_index_terms_keeps_non_latin_scripts() -> None:
    """CJK runs are word characters, and ideographic punctuation is a
    boundary — the 10-Q material this index serves is Chinese."""
    assert index_terms("本季度 营收。测试，营收") == (
        "本季度",
        "营收",
        "测试",
    )


def test_index_terms_splits_supplementary_plane_emoji_as_boundary() -> None:
    """Astral-plane code points must not corrupt neighbouring words.

    Python strings are code-point sequences, so an emoji is one element;
    the risk is a tokenizer that treats it as a word character (it is
    ``So``, not alphanumeric) or that splits a Latin run around a
    surrogate half. Asserted with the literal code points because a
    UTF-16-based split would produce ``('re', 'venue')`` here only by
    accident and ``('', '')`` on a lone surrogate.
    """
    assert index_terms("re\U0001F3AFvenue") == ("re", "venue")
    assert index_terms("\U0001F600 \U0001F600") == ()
    # Heart with a variation selector: U+2764 is ``So`` and U+FE0F is a
    # combining mark, so neither side of the emoji survives as a term and
    # the Latin word around it stays whole.
    assert index_terms("margin❤️\ufe0fgrowth") == ("margin", "growth")


def test_query_terms_and_index_terms_agree_on_every_input() -> None:
    """One authority, no second implementation.

    Retrieval and reads share the frozen unit text; a query that
    normalized differently from the indexed text would silently return
    no hits for text that is plainly present.
    """
    for sample in (
        "Hello, WORLD! hello",
        "",
        "   ",
        "a I 3 revenue",
        "本季度 营收。测试",
        "re\U0001F3AFvenue",
        "Margin\u0007Growth",
    ):
        assert query_terms(sample) == index_terms(sample), sample


def test_query_terms_dedups_a_repeated_word() -> None:
    """A query repeating a term must not count it twice when ranking."""
    assert query_terms("margin margin growth") == ("margin", "growth")


def test_query_terms_on_an_empty_or_punctuation_only_query() -> None:
    assert query_terms("") == ()
    assert query_terms("  ...  ") == ()


# ----- postings -----


def test_build_postings_on_no_units() -> None:
    assert build_postings([]) == {}


def test_build_postings_keys_on_the_frozen_unit_ordinal_not_the_row_position() -> None:
    """``unit`` is the 1-based frozen-unit ordinal a locator addresses, so
    a postings entry must carry the record's own ordinal. Reading the
    sequence position instead would point ``read_source`` at a different
    unit than the one the term was found in — a silently wrong citation.
    """
    postings = build_postings(_units((5, "margin guidance"), (9, "revenue outlook")))

    assert postings["margin"] == (5,)
    assert postings["revenue"] == (9,)


def test_build_postings_lists_every_unit_of_a_repeated_term_ascending() -> None:
    """The same word in units 3, 1 and 20 is one posting of (1, 3, 20):
    ascending ordinal order, so the ranking tie-break below has a sorted
    posting to walk and a rebuild matches byte for byte.
    """
    postings = build_postings(
        _units((3, "margin"), (1, "Margin"), (20, "MARGIN and nothing else"))
    )

    assert postings["margin"] == (1, 3, 20)


def test_build_postings_records_a_unit_once_even_when_the_word_repeats_in_it() -> None:
    postings = build_postings(_units((1, "margin margin and Margin again")))

    assert postings["margin"] == (1,)


def test_build_postings_carries_no_row_for_a_unit_without_terms() -> None:
    """Empty or punctuation-only frozen text cannot be hit, so it must
    not appear anywhere: the store's ``ready`` gate reads that absence.
    """
    postings = build_postings(_units((1, ""), (2, "   \n  "), (3, "。！？;,")))

    assert postings == {}


def test_build_postings_is_a_term_to_ordinal_sequence_mapping() -> None:
    """The shape ``attachment_store.put_index`` accepts
    (``dict[str, Sequence[int]]``), pinned so a swap to a set or a float
    score fails here rather than at the store boundary.
    """
    postings = build_postings(_units((1, "revenue guidance"), (2, "revenue margin")))

    assert isinstance(postings, dict)
    assert all(isinstance(term, str) for term in postings)
    assert all(
        isinstance(units, tuple) and all(isinstance(u, int) for u in units)
        for units in postings.values()
    )


def test_build_postings_is_independent_of_the_unit_input_order() -> None:
    """A rebuild reads the units in the order the store returns them and
    a retry may hand over a re-parsed set in another order. Same text,
    same postings — including the key order, which is what makes the
    stored rows comparable.
    """
    forward = _units((1, "revenue margin"), (2, "margin guidance"), (3, "outlook"))
    shuffled = _units((3, "outlook"), (1, "revenue margin"), (2, "margin guidance"))

    rebuilt = build_postings(shuffled)
    assert rebuilt == build_postings(forward)
    assert list(rebuilt) == sorted(rebuilt)


def test_build_postings_spans_scripts_in_one_pass() -> None:
    postings = build_postings(_units((1, "本季度 营收。测试"), (2, "revenue 营收")))

    assert postings["营收"] == (1, 2)
    assert postings["本季度"] == (1,)
    assert postings["revenue"] == (2,)


# ----- search -----


def test_search_on_an_empty_query_returns_no_hits() -> None:
    postings = build_postings(_units((1, "margin guidance")))

    assert search(postings, "") == ()
    assert search(postings, "   ") == ()
    # A query with words but no indexable term is the same exit.
    assert search(postings, "a . !,") == ()


def test_search_on_terms_that_are_not_in_the_index() -> None:
    postings = build_postings(_units((1, "margin guidance"), (2, "revenue outlook")))

    assert search(postings, "divestiture") == ()
    assert search(postings, "margin divestiture") == (IndexHit(1, ("margin",)),)


def test_search_ranks_more_matched_terms_first() -> None:
    postings = build_postings(
        _units(
            (1, "guidance only here"),
            (2, "margin guidance"),
            (3, "margin guidance and revenue"),
        )
    )

    hits = search(postings, "margin guidance")

    assert [h.unit for h in hits] == [2, 3, 1]
    assert [h.matched for h in hits] == [
        ("margin", "guidance"),
        ("margin", "guidance"),
        ("guidance",),
    ]


def test_search_breaks_a_tie_on_the_lower_unit() -> None:
    """Two units matching the same number of terms are ordered by unit
    ordinal, so a follow-up turn on an unchanged index presents the same
    first hit.
    """
    postings = build_postings(_units((7, "margin"), (2, "MARGIN"), (5, "margin")))

    assert [h.unit for h in search(postings, "margin")] == [2, 5, 7]


def test_search_counts_a_repeated_query_word_once() -> None:
    """``margin margin revenue`` is a two-term query: the repetition is
    emphasis, not evidence that the unit is more relevant.
    """
    postings = build_postings(_units((1, "margin revenue"), (2, "margin guidance")))

    hits = search(postings, "margin margin revenue revenue")

    assert hits == (IndexHit(1, ("margin", "revenue")), IndexHit(2, ("margin",)))


def test_search_reports_the_terms_that_actually_hit_in_query_order() -> None:
    postings = build_postings(_units((1, "margin guidance only")))

    (hit,) = search(postings, "guidance margin absent-term")

    assert hit == IndexHit(unit=1, matched=("guidance", "margin"))


def test_search_is_repeatable_for_the_same_input() -> None:
    postings = build_postings(
        _units((1, "a"), (2, "margin"), (3, "margin guidance"), (4, "guidance margin"))
    )

    assert search(postings, "margin guidance") == search(postings, "margin guidance")


def test_search_over_a_cjk_index_finds_the_unit_that_holds_the_phrase() -> None:
    """The path the operator takes: ask in Chinese, hit the frozen unit,
    then read that unit. Posting ordinals must round-trip to units the
    store can hand back.
    """
    units = _units((1, "本季度 营收 增长"), (2, "毛利率 下降"), (3, "指引 不变"))
    postings = build_postings(units)

    hits = search(postings, "毛利率 下降")

    assert [h.unit for h in hits] == [2]
    assert [u.text for u in units if u.unit == hits[0].unit] == ["毛利率 下降"]


def test_an_unpunctuated_cjk_run_stays_one_term() -> None:
    """The pinned tokenizer limit, asserted so it is a known boundary
    rather than a surprise: this index has no CJK segmenter, so a query
    word inside a longer run misses. Swapping in a real engine changes
    :func:`fin.host.attachment.attachment_index.index_terms` only, and this test is
    the one that moves with it.
    """
    postings = build_postings(_units((1, "本季度营收增长")))

    assert search(postings, "营收") == ()
    assert search(postings, "本季度营收增长") == (
        IndexHit(1, ("本季度营收增长",)),
    )


def test_index_hit_is_frozen() -> None:
    """A hit becomes a ``source_ref`` and a citation record; a mutated
    one would attribute text to the wrong unit.
    """
    hit = IndexHit(unit=1, matched=("margin",))

    assert hit == IndexHit(1, ("margin",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.unit = 2  # type: ignore[misc]


# ----- the readiness gate -----


def test_is_usable_rejects_an_empty_index() -> None:
    """``ready`` must never be reported for an attachment with no
    postings — that is the spec's "a failed index is not an empty
    successful retrieval".
    """
    assert is_usable({}) is False


def test_is_usable_rejects_a_posting_without_units() -> None:
    assert is_usable({"margin": ()}) is False
    assert is_usable({"margin": (1,), "revenue": ()}) is False


def test_is_usable_rejects_ordinals_that_are_not_one_based() -> None:
    """Zero, negative, and a zero mixed into otherwise valid postings:
    ``read_source`` cannot resolve unit 0, so such an index would hand
    the model a locator that always fails.
    """
    assert is_usable({"margin": (0,)}) is False
    assert is_usable({"margin": (-1,)}) is False
    assert is_usable({"margin": (1, 2)}) is True
    assert is_usable({"margin": (1,), "revenue": (0,)}) is False


def test_is_usable_accepts_a_minimal_index() -> None:
    assert is_usable({"margin": (1, 3)}) is True


def test_is_usable_agrees_with_what_the_builder_produces() -> None:
    """The two halves of the contract meet: text with a term yields an
    index the store may publish ``ready``; text without one does not.
    """
    assert is_usable(build_postings(_units((1, "margin guidance")))) is True
    assert is_usable(build_postings(_units((1, "a b"), (2, "。")))) is False
    assert is_usable(build_postings(_units((1, "revenue")))) is True
