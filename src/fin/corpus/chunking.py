"""Parent/child chunking over frozen unit text (plan T11, SC11 / SC16).

The splitter consumes the T6 parse products (``ParsedUnit``) and
produces the two-level block shape the citation gate's hard constraint
is stated over (SC11 / CONTEXT「父子分块」):

* **parent blocks** — bounded context for the model; every parent text
  is a contiguous substring of its frozen unit text;
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
  ascending rank order (1 = heading … 5 = char_cap): parent blocks are
  split at heading boundaries first, oversized pieces fall through the
  remaining ranks, and ``char_cap`` is the last-resort hard cut. No
  float, no model judgment.

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

KIND_PARENT: Final[str] = "parent"
KIND_CHILD: Final[str] = "child"

# Markdown ATX headings are headings verbatim.
_MD_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^#{1,6}\s")

# Sentence-ending punctuation; a '.' between digits (3.14) is not a
# sentence end. CJK and ASCII punctuation alike.
_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(r"[。！？；;!?]|\.(?!\d)")

# Whitespace consumed into the left block after a sentence end.
_TRAILING_WS: Final[str] = " \t\r\n\v\f"

# Punctuation that disqualifies an uppercase line from being a heading.
_NOT_HEADING_TAIL: Final[tuple[str, ...]] = (
    ".",
    ",",
    ";",
    ":",
    "!",
    "?",
    "。",
    "，",
    "；",
    "：",
    "！",
    "？",
)

_ID_SEPARATOR: Final[str] = "\x1f"


# ---------------------------------------------------------------------------
# Deterministic section classification (integer shares only)
# ---------------------------------------------------------------------------


def _is_heading_line(line: str) -> bool:
    """Deterministic heading test: markdown ATX headings, or a short
    uppercase-only line with no sentence punctuation and at least four
    letters. Pure character classing — no float, no model.
    """
    if _MD_HEADING_RE.match(line):
        return True
    stripped = line.strip()
    if not 4 <= len(stripped) <= 80:
        return False
    if stripped.endswith(_NOT_HEADING_TAIL):
        return False
    letters = 0
    for ch in stripped:
        if ch.isalpha():
            if ch.islower():
                return False
            letters += 1
    return letters >= 4


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
    if lines and _is_heading_line(lines[0]):
        return "heading"
    return "body"


# ---------------------------------------------------------------------------
# Boundary cut computation (integer offsets, one rank at a time)
# ---------------------------------------------------------------------------


def _line_offsets(text: str, start: int, end: int):
    """Yield ``(line_start, line_end)`` for every line in ``[start, end)``."""
    pos = start
    while pos < end:
        nl = text.find("\n", pos, end)
        if nl == -1:
            yield pos, end
            return
        yield pos, nl
        pos = nl + 1


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
        for ls, le in _line_offsets(text, start, end):
            if ls > start and _is_heading_line(text[ls:le]):
                cuts.append((ls - 1, ls))  # cut before the newline, next at line start
    elif rank == BOUNDARY_CLAUSE_ITEM:
        for ls, le in _line_offsets(text, start, end):
            if ls > start and _LIST_ITEM_RE.match(text[ls:le]):
                cuts.append((ls - 1, ls))
    elif rank == BOUNDARY_TABLE_ROW:
        for ls, le in _line_offsets(text, start, end):
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


def _split_span_at_all(
    text: str, start: int, end: int, cuts: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Split ``[start, end)`` at EVERY given cut (used for the rank-1
    heading pass: each heading starts a new parent)."""
    pieces: list[tuple[int, int]] = []
    seg = start
    for cut, nxt in sorted(set(cuts)):
        if not start < cut < end:
            continue
        if seg < cut:
            pieces.append((seg, cut))
        seg = max(seg, nxt)
    if seg < end:
        pieces.append((seg, end))
    return pieces


def _split_span_greedy(
    text: str, start: int, end: int, cuts: list[tuple[int, int]], cap: int
) -> list[tuple[int, int]]:
    """Cut ``[start, end)`` into the largest possible pieces under
    ``cap`` using the given cuts. A remainder with no usable cut stays
    oversized — the next rank in the chain takes it.
    """
    cuts = sorted({c for c in cuts if start < c[0] < end})
    if not cuts:
        return [(start, end)]
    pieces: list[tuple[int, int]] = []
    seg = start
    while end - seg > cap:
        usable = [c for c in cuts if seg < c[0] <= seg + cap]
        if not usable:
            break
        cut, nxt = max(usable)
        pieces.append((seg, cut))
        seg = nxt
    if seg < end:
        pieces.append((seg, end))
    return pieces


def _hard_cap_span(
    text: str, start: int, end: int, cap: int
) -> list[tuple[int, int]]:
    """The char_cap rank: deterministic hard cuts every ``cap`` chars."""
    pieces: list[tuple[int, int]] = []
    cur = start
    while end - cur > cap:
        p, n = _hard_cut(text, cur, end, cap)
        pieces.append((cur, p))
        cur = n
    if cur < end:
        pieces.append((cur, end))
    return pieces


def _bounded_spans(
    text: str, start: int, end: int, cap: int, chain: tuple[int, ...]
) -> list[tuple[int, int]]:
    """Bound ``[start, end)`` into pieces of at most ``cap`` chars by
    trying the boundary chain in ascending rank order (SC13: the
    integer chain is tried in order — heading first, char_cap last).
    """
    pieces = [(start, end)]
    for rank in sorted(set(chain)):
        if all(e - s <= cap for s, e in pieces):
            break
        if rank == BOUNDARY_CHAR_CAP:
            hard: list[tuple[int, int]] = []
            for s, e in pieces:
                hard.extend(_hard_cap_span(text, s, e, cap))
            pieces = hard
            break
        next_pieces: list[tuple[int, int]] = []
        for s, e in pieces:
            if e - s <= cap:
                next_pieces.append((s, e))
                continue
            cuts = _cuts_of_rank(text, s, e, rank)
            if cuts:
                next_pieces.extend(_split_span_greedy(text, s, e, cuts, cap))
            else:
                next_pieces.append((s, e))
        pieces = next_pieces
    return pieces


def _parent_spans(
    text: str, chain: tuple[int, ...], cap: int
) -> list[tuple[int, int]]:
    """Parent spans: heading-delimited regions first (a heading governs
    the block that follows it), then bounded through the chain."""
    pieces = [(0, len(text))]
    if BOUNDARY_HEADING in chain:
        heading_cuts = _cuts_of_rank(text, 0, len(text), BOUNDARY_HEADING)
        if heading_cuts:
            pieces = _split_span_at_all(text, 0, len(text), heading_cuts)
    spans: list[tuple[int, int]] = []
    for s, e in pieces:
        spans.extend(_bounded_spans(text, s, e, cap, chain))
    return spans


def _child_spans(
    text: str, start: int, end: int, params: ChunkParams
) -> list[tuple[int, int]]:
    """Child spans inside one parent: pack towards ``target_chars``,
    never above ``max_chars``, cutting at the best available boundary
    (any non-char_cap rank in the chain; char_cap is the implicit hard
    fallback), then merge children below ``min_chars`` into a
    neighbour. Every span is a slice of ``text[start:end]``.
    """
    target = params.target_chars
    min_chars = params.min_chars
    max_chars = params.max_chars
    cuts: list[tuple[int, int]] = []
    for rank in sorted(set(params.boundary_priority)):
        if rank != BOUNDARY_CHAR_CAP:
            cuts.extend(_cuts_of_rank(text, start, end, rank))
    cuts = sorted(set(cuts))

    spans: list[tuple[int, int]] = []
    cur = start
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

    # Merge children below min_chars into a neighbour (same parent — a
    # child must stay inside its parent), keeping every merged span
    # within max_chars. Merging uses the spanning slice, which is still
    # a contiguous substring of the parent.
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
    """

    kind: str  # KIND_PARENT or KIND_CHILD
    block_id: str  # deterministic sha256 (see _block_id)
    text: str
    char_count: int
    ordinal: int
    parent_ordinal: int | None  # None for parents
    parent_block_id: str | None  # None for parents

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

    parent_blocks: list[ChunkBlock] = []
    parent_spans = _parent_spans(text, params.boundary_priority, params.max_chars)
    for ordinal, (ps, pe) in enumerate(parent_spans, start=1):
        parent_text = text[ps:pe]
        parent_blocks.append(
            ChunkBlock(
                kind=KIND_PARENT,
                block_id=_block_id(
                    kind=KIND_PARENT, unit=unit, ordinal=ordinal, text=parent_text
                ),
                text=parent_text,
                char_count=pe - ps,
                ordinal=ordinal,
                parent_ordinal=None,
                parent_block_id=None,
            )
        )

    child_blocks: list[ChunkBlock] = []
    child_ordinal = 0
    for parent, (ps, pe) in zip(parent_blocks, parent_spans):
        for cs, ce in _child_spans(text, ps, pe, params):
            child_ordinal += 1
            child_text = text[cs:ce]
            child_blocks.append(
                ChunkBlock(
                    kind=KIND_CHILD,
                    block_id=_block_id(
                        kind=KIND_CHILD,
                        unit=unit,
                        ordinal=child_ordinal,
                        text=child_text,
                    ),
                    text=child_text,
                    char_count=ce - cs,
                    ordinal=child_ordinal,
                    parent_ordinal=parent.ordinal,
                    parent_block_id=parent.block_id,
                )
            )

    return UnitChunking(
        locator=unit.locator,
        unit_text=text,
        fmt=unit.fmt,
        doc_type=doc_type,
        section=section,
        density=density,
        params=params,
        parents=tuple(parent_blocks),
        children=tuple(child_blocks),
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
