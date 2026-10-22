"""Deterministic chunk-parameter lookup table (T9 / SC13, 场景适配颗粒度).

Chunk parameters are decided by **one lookup** of the
(document type x section x content density) triad into a versioned
**data** table — never by code branches, heuristics that drift, or
model judgment. The same triad resolves to the same parameter group
on every call; the table version (:data:`TABLE_VERSION`) flows into
unit metadata so a table change forces a full in-place rebuild
(T13) and never leaves two table versions coexisting.

Domains
-------
* document type — reuses the T10 dataset vocabulary:
  ``narrative`` / ``table_dense`` / ``clause_list``;
* section (板块) — ``heading`` / ``body`` / ``table`` / ``list``;
* content density — integer levels :data:`DENSITY_PLAIN` (0) /
  :data:`DENSITY_MIXED` (1) / :data:`DENSITY_DENSE` (2), decided by
  :func:`classify_density` with **integer cross-multiplication only**
  (no float ratio, no float threshold).

Totality: unknown triads never raise. Resolution walks a fixed
fallback chain — exact triad row, then the document-type default row
(``section == "*"``), then the single global default row — so any
input lands on a deterministic row.

Boundary priority is an integer chain (``boundary_priority``): ranks
into :data:`BOUNDARY_KINDS`, tried in order by the T11 splitter. No
float, no model call.

Versioning: any change to any row or to the density thresholds must
bump :data:`TABLE_VERSION` (``params-1``). The golden snapshot in
``tests/corpus/test_params_table.py`` locks the row values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Table identity
# ---------------------------------------------------------------------------

# Version of the parameter table below. Bump on ANY row change; the
# splitter (T11) stamps it on block metadata alongside the parse rule
# version.
TABLE_VERSION: Final[str] = "params-1"

# Public alias — the name the parsers package stamps into metadata.
PARAMS_TABLE_VERSION: Final[str] = TABLE_VERSION

# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

# Document types (T10 dataset vocabulary).
#
# ``unknown`` (T6) is a MEMBER of the closed vocabulary, not a sentinel
# outside it: it is the honest answer for an upload whose text yields no
# reliable type, and a metadata-free document must still be able to name
# one. Because it is a member, ``lookup_chunk_params`` resolves it on the
# fixed global-default row rather than raising, and an explicit override
# of ``unknown`` is accepted by the same closed-vocabulary check as the
# other three — so the axis stays closed on both the ingest and the
# filter side.
DOC_TYPES: Final[tuple[str, ...]] = (
    "narrative",
    "table_dense",
    "clause_list",
    "unknown",
)

# Sections (板块) of a document a block can belong to.
SECTIONS: Final[tuple[str, ...]] = (
    "heading",
    "body",
    "table",
    "list",
)

# Content-density levels. Integers by contract: the classifier compares
# integer counts via cross-multiplication, never a float ratio.
DENSITY_PLAIN: Final[int] = 0
DENSITY_MIXED: Final[int] = 1
DENSITY_DENSE: Final[int] = 2

# Wildcard row keys (fallback chain terminators). Callers never need
# to pass them; they exist only as row keys.
_WILDCARD: Final[str] = "*"

# ---------------------------------------------------------------------------
# Boundary priority chain — integer ranks, resolved by name here only
# ---------------------------------------------------------------------------

BOUNDARY_HEADING: Final[int] = 1
BOUNDARY_TABLE_ROW: Final[int] = 2
BOUNDARY_CLAUSE_ITEM: Final[int] = 3
BOUNDARY_SENTENCE: Final[int] = 4
BOUNDARY_CHAR_CAP: Final[int] = 5

BOUNDARY_KINDS: Final[dict[int, str]] = {
    BOUNDARY_HEADING: "heading",
    BOUNDARY_TABLE_ROW: "table_row",
    BOUNDARY_CLAUSE_ITEM: "clause_item",
    BOUNDARY_SENTENCE: "sentence",
    BOUNDARY_CHAR_CAP: "char_cap",
}

# ---------------------------------------------------------------------------
# The table (data, not branches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkParams:
    """One parameter group produced by a single triad lookup.

    ``target_chars`` / ``min_chars`` / ``max_chars`` bound sub-block
    assembly (T11): a sub-block aims at ``target_chars``, is merged
    into a neighbour below ``min_chars``, and never exceeds
    ``max_chars``. ``boundary_priority`` is the integer boundary chain:
    ranks into :data:`BOUNDARY_KINDS`, tried in order.
    """

    target_chars: int
    min_chars: int
    max_chars: int
    boundary_priority: tuple[int, ...]


# Keyed by (doc_type, section, density); ``"*"`` is the in-domain
# wildcard resolved by the fixed fallback chain below. The third slot
# is therefore a density level *or* the wildcard: only the global
# default row uses the wildcard there, and a lookup that carries a
# real density can never reach it — the chain lands on it by name via
# ``_GLOBAL_DEFAULT_KEY`` below.
_TABLE: Final[dict[tuple[str, str, int | Literal["*"]], ChunkParams]] = {
    # narrative (财报叙述): prose blocks, sentence boundaries preferred.
    ("narrative", _WILDCARD, DENSITY_PLAIN): ChunkParams(
        target_chars=1200, min_chars=200, max_chars=2400,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
    ("narrative", _WILDCARD, DENSITY_MIXED): ChunkParams(
        target_chars=1000, min_chars=200, max_chars=2000,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
    ("narrative", _WILDCARD, DENSITY_DENSE): ChunkParams(
        target_chars=800, min_chars=150, max_chars=1600,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
    # A heading governs the block that follows it: larger ceilings.
    ("narrative", "heading", DENSITY_PLAIN): ChunkParams(
        target_chars=1500, min_chars=100, max_chars=3000,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
    ("narrative", "heading", DENSITY_MIXED): ChunkParams(
        target_chars=1200, min_chars=100, max_chars=2400,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
    ("narrative", "heading", DENSITY_DENSE): ChunkParams(
        target_chars=1000, min_chars=80, max_chars=2000,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
    # table_dense (表格密集): rows are the natural boundary, smaller caps.
    ("table_dense", _WILDCARD, DENSITY_PLAIN): ChunkParams(
        target_chars=1000, min_chars=150, max_chars=2000,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_TABLE_ROW, BOUNDARY_CHAR_CAP),
    ),
    ("table_dense", _WILDCARD, DENSITY_MIXED): ChunkParams(
        target_chars=800, min_chars=120, max_chars=1600,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_TABLE_ROW, BOUNDARY_CHAR_CAP),
    ),
    ("table_dense", _WILDCARD, DENSITY_DENSE): ChunkParams(
        target_chars=600, min_chars=100, max_chars=1200,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_TABLE_ROW, BOUNDARY_CHAR_CAP),
    ),
    # Inside a table region: tightest caps.
    ("table_dense", "table", DENSITY_PLAIN): ChunkParams(
        target_chars=700, min_chars=100, max_chars=1400,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_TABLE_ROW, BOUNDARY_CHAR_CAP),
    ),
    ("table_dense", "table", DENSITY_MIXED): ChunkParams(
        target_chars=600, min_chars=80, max_chars=1200,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_TABLE_ROW, BOUNDARY_CHAR_CAP),
    ),
    ("table_dense", "table", DENSITY_DENSE): ChunkParams(
        target_chars=500, min_chars=60, max_chars=1000,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_TABLE_ROW, BOUNDARY_CHAR_CAP),
    ),
    # clause_list (条款列表): clause items and sentences are boundaries.
    ("clause_list", _WILDCARD, DENSITY_PLAIN): ChunkParams(
        target_chars=1100, min_chars=180, max_chars=2200,
        boundary_priority=(
            BOUNDARY_HEADING,
            BOUNDARY_CLAUSE_ITEM,
            BOUNDARY_SENTENCE,
            BOUNDARY_CHAR_CAP,
        ),
    ),
    ("clause_list", _WILDCARD, DENSITY_MIXED): ChunkParams(
        target_chars=900, min_chars=150, max_chars=1800,
        boundary_priority=(
            BOUNDARY_HEADING,
            BOUNDARY_CLAUSE_ITEM,
            BOUNDARY_SENTENCE,
            BOUNDARY_CHAR_CAP,
        ),
    ),
    ("clause_list", _WILDCARD, DENSITY_DENSE): ChunkParams(
        target_chars=700, min_chars=120, max_chars=1400,
        boundary_priority=(
            BOUNDARY_HEADING,
            BOUNDARY_CLAUSE_ITEM,
            BOUNDARY_SENTENCE,
            BOUNDARY_CHAR_CAP,
        ),
    ),
    # Inside a list region: tighter caps, same boundary chain.
    ("clause_list", "list", DENSITY_PLAIN): ChunkParams(
        target_chars=900, min_chars=120, max_chars=1800,
        boundary_priority=(
            BOUNDARY_HEADING,
            BOUNDARY_CLAUSE_ITEM,
            BOUNDARY_SENTENCE,
            BOUNDARY_CHAR_CAP,
        ),
    ),
    ("clause_list", "list", DENSITY_MIXED): ChunkParams(
        target_chars=800, min_chars=100, max_chars=1600,
        boundary_priority=(
            BOUNDARY_HEADING,
            BOUNDARY_CLAUSE_ITEM,
            BOUNDARY_SENTENCE,
            BOUNDARY_CHAR_CAP,
        ),
    ),
    ("clause_list", "list", DENSITY_DENSE): ChunkParams(
        target_chars=600, min_chars=80, max_chars=1200,
        boundary_priority=(
            BOUNDARY_HEADING,
            BOUNDARY_CLAUSE_ITEM,
            BOUNDARY_SENTENCE,
            BOUNDARY_CHAR_CAP,
        ),
    ),
    # Global default row: the deterministic landing spot for any triad
    # the explicit rows do not cover. Lookup is total — never raises.
    (_WILDCARD, _WILDCARD, _WILDCARD): ChunkParams(
        target_chars=1000,
        min_chars=150,
        max_chars=2000,
        boundary_priority=(BOUNDARY_HEADING, BOUNDARY_SENTENCE, BOUNDARY_CHAR_CAP),
    ),
}

# The fixed fallback-chain terminator.
_GLOBAL_DEFAULT_KEY: Final[tuple[str, str, int | Literal["*"]]] = (
    _WILDCARD,
    _WILDCARD,
    _WILDCARD,
)


def lookup_chunk_params(
    doc_type: str, section: str, density: int
) -> ChunkParams:
    """Resolve one (document type x section x content density) lookup.

    Fixed fallback chain: exact triad row, then the document-type
    default row (``section == "*"``), then the global default row.
    Deterministic and total — unknown inputs land on a fixed row and
    never raise. This is the single decision point for chunk
    parameters (SC13): callers must not layer their own branches on
    top.
    """
    row = _TABLE.get((doc_type, section, density))
    if row is None:
        row = _TABLE.get((doc_type, _WILDCARD, density))
    if row is None:
        row = _TABLE[_GLOBAL_DEFAULT_KEY]
    return row


# ---------------------------------------------------------------------------
# Integer-only density classification
# ---------------------------------------------------------------------------

# Density thresholds are stated per 1000 characters but evaluated by
# integer cross-multiplication (count * 1000 >= chars * threshold) —
# no ratio is ever formed, so no float comparison can enter.
_DENSE_TABLE_CELLS_PER_MILLE: Final[int] = 60
_DENSE_LIST_ITEMS_PER_MILLE: Final[int] = 12
_MIXED_TABLE_CELLS_PER_MILLE: Final[int] = 20
_MIXED_LIST_ITEMS_PER_MILLE: Final[int] = 4

# Clause / list item starts: 中文条款编号, (一) style, 1. / 1.2. / 1) style.
_LIST_ITEM_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:第[〇零一二三四五六七八九十百千\d]+条"
    r"|[（(][〇零一二三四五六七八九十\d]+[）)]"
    r"|\d+(?:\.\d+)*[.、)．])"
)


def count_density_signals(text: str) -> tuple[int, int, int]:
    """Deterministic signal counts for one candidate block.

    Returns ``(chars, table_cell_count, list_item_count)``: the text
    length, the number of table-cell markers (``|`` and tab), and the
    number of lines starting like a clause / list item. Pure regex +
    counting — same input, same output, no model call.
    """
    table_cell_count = text.count("|") + text.count("\t")
    list_item_count = 0
    for line in text.split("\n"):
        if _LIST_ITEM_RE.match(line):
            list_item_count += 1
    return (len(text), table_cell_count, list_item_count)


def classify_density(
    *, chars: int, table_cell_count: int = 0, list_item_count: int = 0
) -> int:
    """Classify content density as one of the three integer levels.

    Integer comparison only: thresholds are "per 1000 chars" but
    evaluated as ``count * 1000 >= chars * threshold`` (exact integer
    cross-multiplication — the boundary itself, equality included, is
    exact). ``chars <= 0`` deterministically yields
    :data:`DENSITY_PLAIN` (no division, no raise). A dense signal wins
    over a mixed signal.
    """
    if chars <= 0:
        return DENSITY_PLAIN
    if (
        table_cell_count * 1000 >= chars * _DENSE_TABLE_CELLS_PER_MILLE
        or list_item_count * 1000 >= chars * _DENSE_LIST_ITEMS_PER_MILLE
    ):
        return DENSITY_DENSE
    if (
        table_cell_count * 1000 >= chars * _MIXED_TABLE_CELLS_PER_MILLE
        or list_item_count * 1000 >= chars * _MIXED_LIST_ITEMS_PER_MILLE
    ):
        return DENSITY_MIXED
    return DENSITY_PLAIN


def classify_text_density(text: str) -> int:
    """Convenience wrapper: count signals from raw text, then classify."""
    chars, cells, items = count_density_signals(text)
    return classify_density(
        chars=chars, table_cell_count=cells, list_item_count=items
    )


__all__ = [
    "BOUNDARY_CHAR_CAP",
    "BOUNDARY_CLAUSE_ITEM",
    "BOUNDARY_HEADING",
    "BOUNDARY_KINDS",
    "BOUNDARY_SENTENCE",
    "BOUNDARY_TABLE_ROW",
    "ChunkParams",
    "DENSITY_DENSE",
    "DENSITY_MIXED",
    "DENSITY_PLAIN",
    "DOC_TYPES",
    "PARAMS_TABLE_VERSION",
    "SECTIONS",
    "TABLE_VERSION",
    "classify_density",
    "classify_text_density",
    "count_density_signals",
    "lookup_chunk_params",
]
