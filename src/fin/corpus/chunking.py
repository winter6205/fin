"""Parent/child chunking over frozen unit text (plan T11, SC11 / SC16).

The splitter consumes the T6 parse products (``ParsedUnit``) and
produces the two-level block shape the citation gate's hard constraint
is stated over (SC11 / CONTEXT「父子分块」):

* **parent blocks** — the title-scoped **抽图窗** (ADR 0053 / 0060):
  the rule lives in :mod:`fin.corpus.window`, the parent level is its
  output verbatim (heading + body, or the overflow slices of one
  oversized body), and every parent text is a contiguous substring of
  its frozen unit text;
* **child blocks** — the only retrievable units (the hit set); every
  child text is a contiguous substring of its parent text;
* **push payload** — one payload per child, carrying its parent's text
  (``parent_text``) plus the structured metadata the retrieval side
  consumes as filter conditions (SC20 fields; the push adapter itself
  is T14's deliverable).

Contiguity by construction
--------------------------
Blocks are never re-assembled from pieces and never re-normalized
(SC17 extension): a block is exactly ``unit_text[start:end]`` — one
Python slice of the frozen unit text. Parents are spans of the unit
text; children are spans inside their parent's span, so both levels of
"连续子串" hold by construction and the eval assertion
(``tests/eval/test_chunking_contract.py``) verifies them on the full
dataset.

Determinism (SC12/SC13)
-----------------------
Every decision is an integer table lookup or an integer comparison:

* ``section`` — a deterministic integer-share classifier (no float);
* ``density`` — ``fin.corpus.params.classify_text_density`` (integer
  cross-multiplication, T9);
* parameters — exactly one ``fin.corpus.params.lookup_chunk_params``
  triad lookup; unknown doc types (e.g. the dataset's ``mixed``) land
  on the global default row, never on a code branch;
* boundaries — the row's ``boundary_priority`` integer chain, tried in
  ascending rank order (1 = heading … 5 = char_cap) for the CHILD
  level, where ``char_cap`` is the last-resort hard cut. The PARENT
  level is :func:`fin.corpus.window.window_spans` — the params row
  never shrinks or grows a window (ADR 0060: density sizes in-window
  children only). No float, no model judgment.

Deterministic block identity: ``block_id`` is the sha256 of a canonical
``\\x1f``-joined string mixing kind, locator, ordinal, block text and
the three rule versions (normalization / parse / params). Same input,
same id — T12's byte comparison consumes it and T14's idempotency key
builds on it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from fin.corpus.normalization import NORMALIZATION_RULE_VERSION
from fin.corpus.params import (
    BOUNDARY_CHAR_CAP,
    BOUNDARY_CLAUSE_ITEM,
    BOUNDARY_HEADING,
    BOUNDARY_SENTENCE,
    BOUNDARY_TABLE_ROW,
    ChunkParams,
    classify_text_density,
    lookup_chunk_params,
)
# The clause/list-item pattern is params' single authority (used there
# for integer density counting); the splitter reuses it so the two
# integer decisions can never drift apart.
from fin.corpus.params import _LIST_ITEM_RE
from fin.corpus.parsers._common import ParseResult, ParsedUnit, UnitLocator
from fin.corpus.window import (
    ExtractWindow,
    is_heading_line,
    line_offsets,
    window_spans,
)

KIND_PARENT: Final[str] = "parent"
KIND_CHILD: Final[str] = "child"

# Sentence-ending punctuation; a '.' between digits (3.14) is not a
# sentence end. CJK and ASCII punctuation alike.
_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(r"[。！？；;!?]|\.(?!\d)")

# Whitespace consumed into the left block after a sentence end.
_TRAILING_WS: Final[str] = " \t\r\n\v\f"

_ID_SEPARATOR: Final[str] = "\x1f"


# ---------------------------------------------------------------------------
# Deterministic section classification (integer shares only)
# ---------------------------------------------------------------------------


def classify_section(text: str) -> str:
    """Classify one frozen unit's 板块 as ``heading`` / ``table`` /
    ``list`` / ``body`` (the T9 ``SECTIONS`` vocabulary).

    Integer-share rule: a unit is ``table`` when at least half its
    lines carry table-cell markers (``|`` or tab), ``list`` when at
    least half its lines start like a clause/list item, ``heading``
    when it opens with a heading line, and ``body`` otherwise. Same
    input, same output.
    """
    lines = text.split("\n")
    n = len(lines)
    table_lines = sum(1 for ln in lines if "|" in ln or "\t" in ln)
    item_lines = sum(1 for ln in lines if _LIST_ITEM_RE.match(ln))
    if table_lines > 0 and table_lines * 2 >= n:
        return "table"
    if item_lines > 0 and item_lines * 2 >= n:
        return "list"
    if lines and is_heading_line(lines[0]):
        return "heading"
    return "body"


# ---------------------------------------------------------------------------
# Boundary cut computation (integer offsets, one rank at a time)
# ---------------------------------------------------------------------------


def _cuts_of_rank(
    text: str, start: int, end: int, rank: int
) -> list[tuple[int, int]]:
    """Candidate cuts of one boundary kind inside ``[start, end)``.

    A cut ``(c, n)`` splits ``text[start:end]`` into ``text[start:c]``
    and ``text[n:end]`` — both contiguous slices, so every block
    produced from these offsets stays a verbatim substring of the
    frozen unit text. ``n > c`` skips the separator (newline / blank)
    so that neither side carries leading or trailing whitespace: block
    text must remain a fixed point of the normalization outlet (SC17).
    """
    cuts: list[tuple[int, int]] = []
    if rank == BOUNDARY_HEADING:
        for ls, le in line_offsets(text, start, end):
            if ls > start and is_heading_line(text[ls:le]):
                cuts.append((ls - 1, ls))  # cut before the newline, next at line start
    elif rank == BOUNDARY_CLAUSE_ITEM:
        for ls, le in line_offsets(text, start, end):
            if ls > start and _LIST_ITEM_RE.match(text[ls:le]):
                cuts.append((ls - 1, ls))
    elif rank == BOUNDARY_TABLE_ROW:
        for ls, le in line_offsets(text, start, end):
            if le + 1 < end and ("|" in text[ls:le] or "\t" in text[ls:le]):
                cuts.append((le, le + 1))  # cut after the row, newline skipped
    elif rank == BOUNDARY_SENTENCE:
        for m in _SENTENCE_END_RE.finditer(text, start, end):
            n = m.end()
            while n < end and text[n] in _TRAILING_WS:
                n += 1
            if start < m.end() < end and n < end:
                cuts.append((m.end(), n))
    return sorted(set(cuts))


def _hard_cut(text: str, start: int, end: int, cap: int) -> tuple[int, int]:
    """One char_cap hard cut from ``start``: the piece is at most
    ``cap`` chars and neither side keeps whitespace at the seam."""
    p = min(start + cap, end)
    while p > start and text[p - 1] in _TRAILING_WS:
        p -= 1
    if p == start:  # defensive: an all-whitespace window cuts hard
        p = min(start + cap, end)
    n = p
    while n < end and text[n] in _TRAILING_WS:
        n += 1
    return p, n


def _parent_windows(text: str, chain: tuple[int, ...]) -> tuple[ExtractWindow, ...]:
    """The parent level: the title-scoped **抽图窗** of the unit.

    The window rule is :func:`fin.corpus.window.window_spans` — the
    single authority (ADR 0053 / 0060) — and the params row must not
    retarget it: density sizes the in-window children only. The row's
    boundary chain still tells the window rule which line shapes its
    own classifier counts as headings (the ``heading`` rank is in every
    row today, but an out-of-row chain is the caller's statement about
    its own corpus, so the spaces are handed in verbatim).
    """
    if BOUNDARY_HEADING not in chain:
        return window_spans(text, heading_lines=())
    return window_spans(text)


def _merge_small_spans(
    spans: list[tuple[int, int]], *, min_chars: int, max_chars: int
) -> list[tuple[int, int]]:
    """Merge children below ``min_chars`` into a neighbour (same parent
    — a child must stay inside its parent), keeping every merged span
    within ``max_chars``. Merging uses the spanning slice, which is
    still a contiguous substring of the parent.
    """
    i = 0
    while i < len(spans):
        s, e = spans[i]
        if e - s >= min_chars or len(spans) == 1:
            i += 1
            continue
        merged = False
        if i > 0:
            prev_s, _ = spans[i - 1]
            if e - prev_s <= max_chars:
                spans[i - 1 : i + 1] = [(prev_s, e)]
                merged = True
        if not merged and i + 1 < len(spans):
            _, next_e = spans[i + 1]
            if next_e - s <= max_chars:
                spans[i : i + 2] = [(s, next_e)]
                merged = True
        if not merged:
            i += 1
    return spans


def _boundary_cuts(
    text: str, start: int, end: int, params: ChunkParams
) -> list[tuple[int, int]]:
    """Every boundary cut of the row's chain inside ``[start, end)``,
    in ascending rank order, deduplicated (``char_cap`` is the implicit
    hard fallback, so it contributes no candidate here).
    """
    cuts: list[tuple[int, int]] = []
    for rank in sorted(set(params.boundary_priority)):
        if rank != BOUNDARY_CHAR_CAP:
            cuts.extend(_cuts_of_rank(text, start, end, rank))
    return sorted(set(cuts))


def _child_spans(
    text: str,
    start: int,
    end: int,
    params: ChunkParams,
    *,
    floor: int | None = None,
) -> list[tuple[int, int]]:
    """Child spans inside one parent: pack towards ``target_chars``,
    never above ``max_chars``, cutting at the best available boundary
    (any non-char_cap rank in the chain; char_cap is the implicit hard
    fallback), then merge children below ``min_chars`` into a
    neighbour. Every span is a slice of ``text[start:end]``.

    ``floor`` is the monotone lower bound the packing must start at —
    normally the cursor the PREVIOUS window's children left on the unit
    text. Adjacent windows overlap by ``WINDOW_OVERLAP_PERCENT``, so
    packing each window from its own start would re-emit that overlap
    as child-on-child overlap across the seam — the identity ADR 0063
    reserves for windows. The floor is clamped to ``[start, end)``, so
    every span still lies inside this window (SC5) while the unit-wide
    child sequence stays monotone (the wire's freeze invariant).
    """
    target = params.target_chars
    max_chars = params.max_chars
    cuts = _boundary_cuts(text, start, end, params)

    spans: list[tuple[int, int]] = []
    cur = start if floor is None else max(start, floor)
    # The floor can land on whitespace the previous window's packing
    # skipped: a child text must stay a fixed point of the normalization
    # outlet (SC17), so the first child starts on the first content
    # character at or after the floor.
    while cur < end and text[cur] in _TRAILING_WS:
        cur += 1
    while end - cur > target:
        limit = cur + max_chars
        usable = [c for c in cuts if cur < c[0] <= limit]
        reached = [c for c in usable if c[0] >= cur + target]
        if reached:
            chosen = min(reached)  # target reached → cut at the next boundary
        elif usable:
            chosen = max(usable)  # best boundary below the target+cap window
        else:
            chosen = None  # no boundary at all
        if chosen is None:
            if end - cur <= max_chars:
                break  # remainder below the cap: it becomes the final child
            p, n = _hard_cut(text, cur, end, max_chars)
            spans.append((cur, p))
            cur = n
        else:
            spans.append((cur, chosen[0]))
            cur = chosen[1]
    if cur < end:
        spans.append((cur, end))

    return _merge_small_spans(
        spans, min_chars=params.min_chars, max_chars=max_chars
    )


# ---------------------------------------------------------------------------
# Block / payload shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkBlock:
    """One block of the two-level shape (SC11 / SC16).

    ``kind`` is the structural hit-set discriminator: only
    ``kind == "child"`` blocks are retrievable (``is_retrievable``);
    parent blocks exist to ride along as context and never enter the
    hit set. ``ordinal`` is the 1-based position of the block within
    its unit, counted per kind.

    ``title`` is the governing heading text of the parent window
    (``None`` on the OCR / textless shape and on every child — the
    parent carries the window's heading, children are in-window keys).
    It rides the block shape but not the identity hash: ``block_id``
    mixes kind / locator / ordinal / text / versions (see
    ``_block_id``), so a title change cannot silently re-key a block
    whose text is unchanged.
    """

    kind: str  # KIND_PARENT or KIND_CHILD
    block_id: str  # deterministic sha256 (see _block_id)
    text: str
    char_count: int
    ordinal: int
    parent_ordinal: int | None  # None for parents
    parent_block_id: str | None  # None for parents
    title: str | None = None  # governing heading; parents only

    @property
    def is_retrievable(self) -> bool:
        """SC16: the hit set contains only child blocks."""
        return self.kind == KIND_CHILD


@dataclass(frozen=True)
class ChunkPayload:
    """One child block's push payload (SC16 / SC20 shape).

    The payload rides the child block plus its parent's text — the
    parent itself is NOT an independent payload. T14 owns the push
    adapter; this dataclass fixes the in-payload structure:
    ``parent_text`` travels with every child, and the structured
    metadata fields the retrieval side filters on are present.
    """

    # the hittable child block
    block_id: str
    text: str
    # the parent context riding along
    parent_block_id: str
    parent_text: str
    # frozen-unit identity + structured metadata (SC20 filter fields)
    locator: UnitLocator
    fmt: str
    doc_type: str
    section: str
    density: int
    source_sha256: str
    parse_rule_version: str
    params_table_version: str
    normalization_rule_version: str

    def to_dict(self) -> dict[str, object]:
        """Deterministic dict shape (T12 byte comparison / T14 wire)."""
        return {
            "block_id": self.block_id,
            "text": self.text,
            "parent_block_id": self.parent_block_id,
            "parent_text": self.parent_text,
            "locator": self.locator.to_dict(),
            "fmt": self.fmt,
            "doc_type": self.doc_type,
            "section": self.section,
            "density": self.density,
            "source_sha256": self.source_sha256,
            "parse_rule_version": self.parse_rule_version,
            "params_table_version": self.params_table_version,
            "normalization_rule_version": self.normalization_rule_version,
        }


@dataclass(frozen=True)
class UnitChunking:
    """The two-level chunking of one frozen unit."""

    locator: UnitLocator
    unit_text: str
    fmt: str
    doc_type: str
    section: str
    density: int
    params: ChunkParams
    parents: tuple[ChunkBlock, ...]
    children: tuple[ChunkBlock, ...]
    source_sha256: str
    parse_rule_version: str
    params_table_version: str

    def hittable_blocks(self) -> tuple[ChunkBlock, ...]:
        """SC16: the hit set — children only, never parents."""
        return self.children

    def payloads(self) -> tuple[ChunkPayload, ...]:
        """One push payload per child, parent text riding along."""
        parents_by_id = {p.block_id: p for p in self.parents}
        return tuple(
            ChunkPayload(
                block_id=child.block_id,
                text=child.text,
                parent_block_id=child.parent_block_id,
                parent_text=parents_by_id[child.parent_block_id].text,
                locator=self.locator,
                fmt=self.fmt,
                doc_type=self.doc_type,
                section=self.section,
                density=self.density,
                source_sha256=self.source_sha256,
                parse_rule_version=self.parse_rule_version,
                params_table_version=self.params_table_version,
                normalization_rule_version=NORMALIZATION_RULE_VERSION,
            )
            for child in self.children
        )


@dataclass(frozen=True)
class DocumentChunking:
    """The chunking of a whole parsed document, unit by unit."""

    document_id: str
    version: str
    doc_type: str
    units: tuple[UnitChunking, ...]

    def hittable_blocks(self) -> tuple[ChunkBlock, ...]:
        """SC16 hit set over the whole document: children only."""
        return tuple(child for unit in self.units for child in unit.children)

    def payloads(self) -> tuple[ChunkPayload, ...]:
        return tuple(p for unit in self.units for p in unit.payloads())


def _block_id(
    *, kind: str, unit: ParsedUnit, ordinal: int, text: str
) -> str:
    """Deterministic block identity: sha256 over a canonical
    ``\\x1f``-joined string of kind, locator, per-kind ordinal, block
    text and the three rule versions. Same input → same id; distinct
    blocks → distinct ids (kind and ordinal disambiguate repeated
    text). T12's byte comparison and T14's idempotency key consume it.
    """
    locator = unit.locator
    parts = (
        kind,
        locator.document_id,
        locator.version,
        str(locator.unit),
        str(ordinal),
        text,
        NORMALIZATION_RULE_VERSION,
        unit.parse_rule_version,
        unit.params_table_version,
    )
    return hashlib.sha256(_ID_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _parent_block(unit: ParsedUnit, text: str, window: ExtractWindow) -> ChunkBlock:
    """One window as a parent block: span text plus its governing title."""
    parent_text = window.text_of(text)
    return ChunkBlock(
        kind=KIND_PARENT,
        block_id=_block_id(
            kind=KIND_PARENT, unit=unit, ordinal=window.ordinal, text=parent_text
        ),
        text=parent_text,
        char_count=window.end - window.start,
        ordinal=window.ordinal,
        parent_ordinal=None,
        parent_block_id=None,
        title=window.title,
    )


def _child_blocks(
    unit: ParsedUnit,
    text: str,
    parents: list[ChunkBlock],
    windows: tuple[ExtractWindow, ...],
    params: ChunkParams,
) -> list[ChunkBlock]:
    """Pack every window's children: contiguous substrings of the
    window, ordinaled across the unit (SC16 hit set).

    The windows of one body overlap by ``WINDOW_OVERLAP_PERCENT``, so
    the child packing carries a unit-wide cursor: window k+1 cuts its
    first child from the floor its predecessor's children left (its own
    span start while that floor is behind it). Every child stays inside
    its own window, but the children of the unit read end-to-end over
    the unit text — the dense monotone ``chunks[]`` the receiving
    service's freeze invariant walks (ADR 0063: the 8% is a window
    property, never an ES child property).
    """
    blocks: list[ChunkBlock] = []
    cursor = 0
    for parent, window in zip(parents, windows):
        for cs, ce in _child_spans(
            text, window.start, window.end, params, floor=cursor
        ):
            child_text = text[cs:ce]
            ordinal = len(blocks) + 1
            blocks.append(
                ChunkBlock(
                    kind=KIND_CHILD,
                    block_id=_block_id(
                        kind=KIND_CHILD, unit=unit, ordinal=ordinal, text=child_text
                    ),
                    text=child_text,
                    char_count=ce - cs,
                    ordinal=ordinal,
                    parent_ordinal=parent.ordinal,
                    parent_block_id=parent.block_id,
                )
            )
            cursor = ce
    return blocks


def split_unit(unit: ParsedUnit, *, doc_type: str) -> UnitChunking:
    """Split one frozen unit into parent + child blocks.

    ``doc_type`` is the T10 dataset vocabulary (``narrative`` /
    ``table_dense`` / ``clause_list``; T10 primary samples carry
    ``mixed``, which deterministically lands on the params table's
    global default row). Everything else — section, density, parameter
    row, boundary chain — is decided by the integer machinery above.
    """
    text = unit.text
    section = classify_section(text)
    density = classify_text_density(text)
    params = lookup_chunk_params(doc_type, section, density)

    windows = _parent_windows(text, params.boundary_priority)
    parent_blocks = [_parent_block(unit, text, window) for window in windows]

    return UnitChunking(
        locator=unit.locator,
        unit_text=text,
        fmt=unit.fmt,
        doc_type=doc_type,
        section=section,
        density=density,
        params=params,
        parents=tuple(parent_blocks),
        children=tuple(_child_blocks(unit, text, parent_blocks, windows, params)),
        source_sha256=unit.source_sha256,
        parse_rule_version=unit.parse_rule_version,
        params_table_version=unit.params_table_version,
    )


def split_document(result: ParseResult, *, doc_type: str) -> DocumentChunking:
    """Split every frozen unit of a parsed document (T6 product) into
    the two-level block shape."""
    return DocumentChunking(
        document_id=result.document_id,
        version=result.version,
        doc_type=doc_type,
        units=tuple(split_unit(unit, doc_type=doc_type) for unit in result.units),
    )


__all__ = [
    "KIND_CHILD",
    "KIND_PARENT",
    "ChunkBlock",
    "ChunkPayload",
    "DocumentChunking",
    "UnitChunking",
    "classify_section",
    "split_document",
    "split_unit",
]
