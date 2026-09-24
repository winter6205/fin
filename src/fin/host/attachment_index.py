"""Derived search index over a thread attachment's frozen units.

An attachment's searchable index is *derived*: it is recomputed from the
parsed, versioned frozen-unit text and never authored on its own. That is
the spec's single-source rule — retrieval hits and ``read_source`` slices
address the same text, so the index may not carry a variant of it.

This module is the whole computation as pure functions: no file, no
database, no lock, no clock, standard library only. ``attachment_store``
persists what :func:`build_postings` returns (its ``put_index`` postings
shape is ``dict[str, Sequence[int]]``) and refuses ``ready`` for a version
that has no unit or posting rows of its own; :func:`is_usable` is the
stronger rule the preparation service applies before it reports a stored
``ready`` at all. Keeping the two apart means the durability layer and the
ranking rule stay independent and each is testable alone.

Determinism is a product requirement, not a nicety: a restart rebuilds
the index for the *same* parsed version, and any difference in term set
or hit order between the rebuild and the original is a difference the
operator would see across turns.

The tokenizer is the seam a real engine replaces. It is a Unicode
word-class segmentation, which keeps a CJK run together as one term
because fin has no segmenter of its own; see :func:`index_terms`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from fin.host.attachment_store import ParsedAttachmentUnit

__all__ = [
    "IndexHit",
    "build_postings",
    "index_terms",
    "is_usable",
    "query_terms",
    "search",
]


# Minimum term length. One-character tokens (articles, stray digits,
# single-letter list markers) match too broadly to earn a posting row.
_MIN_TERM_LEN: Final[int] = 2

# ``\w`` with the UNICODE flag is the Unicode word class: letters of any
# script (CJK ideographs are ``Lo``), digits, and connector punctuation.
# Emoji (``So``), variation selectors, combining marks, and every
# punctuation class — ASCII or ideographic — are boundaries, so an
# astral-plane code point can never split a Latin word in half the way a
# UTF-16-unit split would.
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\w+", re.UNICODE)


def _terms(text: str) -> tuple[str, ...]:
    """The one normalization pipeline: case-fold, split, drop noise,
    de-duplicate in first-seen order.

    Shared by the index side and the query side on purpose. Two
    near-identical implementations would drift on one character class,
    and the failure is silent: the attachment plainly contains the word,
    and the search still returns nothing.

    Case-folding (``str.casefold``) rather than ``lower`` because this is
    caseless matching, the exact use the method exists for.

    No Unicode *normalization* (NFC/NFKD) here: folding composed and
    decomposed spellings onto one form is the normalization rule's job,
    and ``fin.corpus`` already owns that authority. Re-deciding it in the
    index would give the same attachment text two identities.

    First-seen order is kept rather than sorted because it is the order
    the operator's question reads in, and it costs nothing.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _WORD_RE.finditer(text):
        term = match.group().casefold()
        if len(term) < _MIN_TERM_LEN or term in seen:
            continue
        seen.add(term)
        out.append(term)
    return tuple(out)


def index_terms(text: str) -> tuple[str, ...]:
    """The distinct terms of one frozen unit's text, in first-seen order.

    Empty text, and text with nothing but whitespace or punctuation,
    produce an empty tuple: no posting row, never a row for an empty
    term.

    Known limit of a word-class tokenizer on an unsegmented script: a
    Chinese phrase with no punctuation in it is one term, so a shorter
    query word inside it does not match. Widening this to a real CJK
    segmenter changes this function and nothing else —
    :func:`build_postings` and :func:`query_terms` both route through it,
    which is why they cannot disagree.
    """
    return _terms(text)


def query_terms(query: str) -> tuple[str, ...]:
    """The terms to look up for ``query``.

    Deliberately :func:`index_terms` under a second name rather than a
    second implementation — see the note there on why the two sides must
    normalize identically.
    """
    return _terms(query)


# ---------------------------------------------------------------------------
# Index shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexHit:
    """One frozen unit the query reached, and the terms that reached it.

    ``unit`` is the 1-based frozen-unit ordinal, verbatim the value a
    ready hit is expected to carry in
    ``source_ref={kind:"attachment", attachment_id, version, unit}``;
    this module is version- and attachment-agnostic because the caller
    already knows which attachment it indexed.

    Frozen because the hit is on its way to becoming a locator and a
    citation record: a mutated ordinal would attribute real returned
    text to the wrong unit, which is the one citation error that cannot
    be caught downstream.
    """

    unit: int
    matched: tuple[str, ...]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_postings(
    units: Sequence[ParsedAttachmentUnit],
) -> dict[str, tuple[int, ...]]:
    """The derived postings list for one version's frozen units.

    Term → the 1-based ordinals of the units carrying it, ascending. The
    ordinal is taken from :attr:`ParsedAttachmentUnit.unit`, never from
    the row's position in ``units``: the ordinal is what a locator
    addresses and what ``read_source`` resolves, so an index keyed by
    anything else would point a citation at different text than the hit
    promised.

    Both output orders are canonical — terms sorted, ordinals sorted —
    so the same unit texts always produce the same mapping regardless of
    the order they arrive in. A restart rebuilds an index for a version
    that is already on the wire, and a rebuild that reshuffled rows would
    be observably different across the restart the spec requires to be
    transparent.

    A unit with no term contributes no row anywhere, which is what lets
    :func:`is_usable` distinguish "indexed an empty parse" from
    "indexed nothing yet".
    """
    postings: dict[str, set[int]] = {}
    for parsed in units:
        for term in index_terms(parsed.text):
            postings.setdefault(term, set()).add(parsed.unit)
    return {term: tuple(sorted(postings[term])) for term in sorted(postings)}


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def search(
    postings: Mapping[str, Sequence[int]], query: str
) -> tuple[IndexHit, ...]:
    """Rank one query against a postings map.

    Order: number of distinct query terms matched, descending, then the
    unit ordinal ascending. Counting distinct terms rather than
    occurrences keeps a unit's rank from rising because the operator
    repeated a word, and the ordinal tie-break makes the first hit of a
    follow-up turn the same unit it was on the first turn.

    An empty query — including one whose every token was dropped by the
    length floor — returns no hits rather than every unit: a query with
    nothing to match has not selected anything.

    This ranks whatever it is handed; it does not second-guess the
    ordinals it was stored. Validating the index is :func:`is_usable`'s
    single job, and the caller gates on it before searching, so a broken
    index surfaces as ``attachment_index_unavailable`` instead of quietly
    producing locators no read can resolve.
    """
    terms = query_terms(query)
    if not terms:
        return ()

    # ``matched[unit]`` accumulates in query order, so a hit's term tuple
    # is stable and independent of the postings' internal order.
    matched: dict[int, list[str]] = {}
    for term in terms:
        # ``fromkeys`` credits a term once per unit even if a hand-built
        # mapping lists that ordinal twice: the score counts distinct
        # terms, not postings rows.
        for unit in dict.fromkeys(postings.get(term, ())):
            matched.setdefault(unit, []).append(term)

    hits = [
        IndexHit(unit=unit, matched=tuple(hit_terms))
        for unit, hit_terms in matched.items()
    ]
    hits.sort(key=lambda hit: (-len(hit.matched), hit.unit))
    return tuple(hits)


def is_usable(postings: Mapping[str, Sequence[int]]) -> bool:
    """Whether a derived index may back a ``ready`` claim.

    The preparation service asks this before it reports a stored ``ready``,
    and the two rules are the spec's own:

    * a derived index with no rows, or with a term that reaches no unit,
      cannot serve a hit — publishing ``ready`` over it would present a
      failed index as an attachment that simply has nothing to find;
    * every ordinal must be a valid 1-based frozen-unit ordinal, because
      a hit becomes a locator the model reads back, and unit 0 or a
      negative unit is a read that is guaranteed to fail.
    """
    if not postings:
        return False
    return all(
        units and all(unit >= 1 for unit in units)
        for units in postings.values()
    )
