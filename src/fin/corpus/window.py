"""Title-scoped extract window (抽图窗): the corpus window rule (ADR 0053).

One **抽图窗** is the current heading plus a body span, capped at
``EXTRACT_WINDOW_TOKENS`` extract-model tokens. When the heading body
fits the cap the whole body is one window and overlap is zero; when it
does not, the same heading is kept and the **body only** is split into
slices of at most the cap, adjacent slices overlapping by about
``WINDOW_OVERLAP_PERCENT`` — the overflow branch of the same rule, not
a second splitter (ADR 0053 / 0060 / 0063). The windows of one heading
all share its ``title``; the heading text is not re-emitted inside the
later windows and is not a second body cutter.

Cap semantics
-------------
The cap governs the window's **body** span, never the heading: a
region whose body is at most :data:`EXTRACT_WINDOW_CHARS` yields
exactly one window whose span covers heading + body with overlap 0
(SC2), and only an oversized body splits (SC8 keeps the cap out of
the density table's reach — density sizes in-window children, never
this window). The heading region is bounded by the *next* heading
line, so a heading whose body is exactly the cap is still one window.

Whitespace at a boundary is never part of a window: a seam trims it
on both sides (the same rule ``chunking._hard_cut`` applies), which
keeps every window text a fixed point of the normalization outlet
(SC17). No content is dropped — only whitespace.

Determinism (SC6 / SC12)
------------------------
The whole rule is pure integer arithmetic over the frozen-unit text —
no float, no dict/set iteration order, no clock, no model, no OCR, no
randomness. Same frozen bytes + same rule version → identical windows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

# Extract-window token cap: the extract-model token budget one window
# body may consume (ADR 0053).
EXTRACT_WINDOW_TOKENS: Final[int] = 1200

# Character-per-token estimate, v1 deterministic: ``len(text) // 4``.
# The same 4-chars-per-token convention as
# ``fin.host.session_compaction.CHARS_PER_TOKEN`` (this corpus module is
# the authority for the切分 layer and deliberately does not import the
# host module to share the number — an import would point the corpus
# dependency outward).
WINDOW_CHARS_PER_TOKEN: Final[int] = 4

EXTRACT_WINDOW_CHARS: Final[int] = EXTRACT_WINDOW_TOKENS * WINDOW_CHARS_PER_TOKEN

# Overflow overlap between adjacent windows of the same heading body:
# ~8% of the *preceding* window's actual length, integer arithmetic
# (ADR 0053 / 0063 — this percent belongs to windows only, never to
# ES/lexical children).
WINDOW_OVERLAP_PERCENT: Final[int] = 8

# Percent band the SC3 test asserts: overlap / preceding window length
# must land inside [6, 10] percent. Tests only.
WINDOW_OVERLAP_TOLERANCE: Final[tuple[int, int]] = (6, 10)

# Markdown ATX headings are headings verbatim (moved verbatim from
# ``chunking._is_heading_line`` — this module is the single authority).
_MD_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^#{1,6}\s")

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

_LEADING_HASHES_RE: Final[re.Pattern[str]] = re.compile(r"^#+\s*")

# Whitespace trimmed at an overflow seam (same rule as
# ``chunking._TRAILING_WS``: neither side of a seam keeps whitespace).
_TRAILING_WS: Final[str] = " \t\r\n\v\f"


def is_heading_line(line: str) -> bool:
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


def count_window_tokens(text: str) -> int:
    """Deterministic token estimate of one window text: ``len // 4``."""
    return len(text) // WINDOW_CHARS_PER_TOKEN


def window_title(line: str) -> str:
    """The governing heading text of one heading line: the leading ATX
    ``#`` run and surrounding whitespace stripped.
    """
    return _LEADING_HASHES_RE.sub("", line).strip()


@dataclass(frozen=True)
class ExtractWindow:
    """One title-scoped extract window (抽图窗) of one frozen unit.

    ``start`` / ``end`` are the window's contiguous span in the frozen
    unit text (``text[start:end]``; the first window of a heading
    region includes the heading line). ``body_start`` / ``body_end``
    delimit the body portion — the span the
    :data:`EXTRACT_WINDOW_CHARS` cap governs — and ``title`` is the
    governing heading text, or ``None`` when the region has no heading
    (the OCR / textless shape).
    """

    ordinal: int
    start: int
    end: int
    body_start: int
    body_end: int
    title: str | None

    def text_of(self, unit_text: str) -> str:
        """The window's contiguous span of the frozen unit text."""
        return unit_text[self.start : self.end]

    def body_of(self, unit_text: str) -> str:
        """The window's body portion (the part the token cap governs)."""
        return unit_text[self.body_start : self.body_end]


def line_offsets(
    text: str, start: int = 0, end: int | None = None
) -> tuple[tuple[int, int], ...]:
    """``(line_start, line_end)`` for every line in ``[start, end)``.

    Public, and the ONE copy: the splitter (``fin.corpus.chunking``)
    imports this rather than carrying a twin, because the rule layer
    sits below its consumer and a second walk would let the two line
    rules drift (ADR 0060 one window tree).
    """
    limit = len(text) if end is None else end
    offsets: list[tuple[int, int]] = []
    pos = start
    while pos < limit:
        nl = text.find("\n", pos, limit)
        if nl == -1:
            offsets.append((pos, limit))
            break
        offsets.append((pos, nl))
        pos = nl + 1
    return tuple(offsets)


def _heading_lines(text: str) -> tuple[tuple[int, int], ...]:
    """Spans of the unit's heading lines, in text order."""
    return tuple(
        (ls, le) for ls, le in line_offsets(text) if is_heading_line(text[ls:le])
    )


def _region_starts(
    text: str, heading_lines: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, str | None], ...]:
    """``(start, title)`` for every title-scoped region, in text order.

    A heading line starts a region that runs to the next heading line;
    the leading run before the first heading is a region with no title
    (the OCR case). Text with no heading line at all is one title-less
    region.
    """
    if not heading_lines:
        return ((0, None),) if text else ()
    starts: list[tuple[int, str | None]] = []
    if heading_lines[0][0] > 0:
        starts.append((0, None))
    for ls, le in heading_lines:
        starts.append((ls, window_title(text[ls:le])))
    return tuple(starts)


def _body_bounds(
    text: str, region_start: int, region_end: int, heading_end: int | None
) -> tuple[int, int]:
    """The body span of one region: the line after the heading line to
    the region end. A heading whose line is the region's last line has
    an empty body; a region with no heading line (OCR / textless) is
    body from end to end.
    """
    if heading_end is None:
        return region_start, region_end
    if heading_end >= region_end:
        return region_end, region_end
    start = heading_end + 1
    if text[heading_end] != "\n" or start >= region_end:
        return region_end, region_end
    return start, region_end


def _trim_seam_end(text: str, pos: int, floor: int) -> int:
    """Trim trailing whitespace off ``pos`` for a seam (same rule as
    ``chunking._hard_cut``: no side of a seam keeps whitespace). Never
    trims past ``floor``, so the slice stays non-empty.
    """
    while pos > floor and text[pos - 1] in _TRAILING_WS:
        pos -= 1
    return pos


def _skip_seam_whitespace(text: str, pos: int, ceiling: int) -> int:
    """Skip whitespace on the far side of a seam."""
    while pos < ceiling and text[pos] in _TRAILING_WS:
        pos += 1
    return pos


def _next_slice(text: str, start: int, end: int, cap: int) -> tuple[int, int]:
    """The next overflow slice of ``text[start:end]``: at most ``cap``
    chars with no whitespace at the seam's inner edges.

    Returns ``(piece_end, next_start)``; ``next_start >= end`` means
    the piece was the last one. The next slice's ceiling stays ``end``
    — the body region end — so a window never reaches past its region.
    """
    piece_end = _trim_seam_end(text, min(start + cap, end), start)
    next_start = _skip_seam_whitespace(text, piece_end, end)
    if next_start >= end:
        return piece_end, end
    overlap = (piece_end - start) * WINDOW_OVERLAP_PERCENT // 100
    next_start = max(start + 1, piece_end - overlap)
    # The piece end is already trimmed, so the only untrimmed edge at
    # the next slice's start is the overlap reach itself: a slice
    # boundary landing on whitespace is stepped back off it. The step
    # stays inside the overlap reach, so it can only shorten the
    # overlap, never push the next slice past the piece end.
    while next_start < piece_end and text[next_start] in _TRAILING_WS:
        next_start += 1
    return piece_end, next_start


def _window(
    ordinal: int, start: int, end: int, body_start: int, body_end: int, title: str | None
) -> ExtractWindow:
    return ExtractWindow(
        ordinal=ordinal,
        start=start,
        end=end,
        body_start=body_start,
        body_end=body_end,
        title=title,
    )


def _overflow_windows(
    text: str,
    *,
    ordinal: int,
    region_start: int,
    body_start: int,
    body_end: int,
    title: str | None,
) -> tuple[list[ExtractWindow], int]:
    """Split an oversized body into windows sharing one heading.

    Window 1's span reaches back to the region start (the heading
    rides along); later windows are their own body slices. The title
    is carried by every window — the heading text lives in the frozen
    text once, in window 1 only.
    """
    windows: list[ExtractWindow] = []
    start = body_start
    body_end = max(body_end, start + 1)  # never an empty slice
    accepted: int | None = None
    while True:
        piece_end, next_start = _next_slice(
            text, start, body_end, EXTRACT_WINDOW_CHARS
        )
        span_start = region_start if start == body_start else start
        # Windows never share a slice start: when the reach step-back
        # lands the next slice on its predecessor's trimmed seam, that
        # seam's window already reaches the piece end, so the piece
        # would be a duplicate span — dropping it loses no text (the
        # seam-adjacent whitespace excepted) and duplicate spans would
        # produce duplicate block texts / ids.
        if accepted is None or start > accepted:
            windows.append(
                _window(ordinal, span_start, piece_end, start, piece_end, title)
            )
            ordinal += 1
            accepted = start
        if next_start >= body_end:
            break
        start = next_start
    return windows, ordinal


def _region_windows(
    text: str,
    *,
    ordinal: int,
    region_start: int,
    region_end: int,
    title: str | None,
    heading_end: int | None,
) -> tuple[list[ExtractWindow], int]:
    """The windows of one title-scoped region: exactly one when the
    body fits the cap (SC2), the overflow slices otherwise."""
    body_start, body_end = _body_bounds(text, region_start, region_end, heading_end)
    if body_end - body_start > EXTRACT_WINDOW_CHARS:
        return _overflow_windows(
            text,
            ordinal=ordinal,
            region_start=region_start,
            body_start=body_start,
            body_end=body_end,
            title=title,
        )
    return [
        _window(ordinal, region_start, region_end, body_start, body_end, title)
    ], ordinal + 1


def window_spans(
    text: str, *, heading_lines: Sequence[tuple[int, int]] | None = None
) -> tuple[ExtractWindow, ...]:
    """The extract windows of one frozen unit text, in text order.

    Title scoping first: each heading line opens a region that ends
    where the next heading line starts; the text before the first
    heading (or all of it, when there is no heading) is one region
    with ``title is None``. Within a region the cap governs the body
    only — a body of at most :data:`EXTRACT_WINDOW_CHARS` is one
    window (heading + body, overlap 0), a larger body splits into
    slices of at most that many chars overlapping the preceding slice
    by :data:`WINDOW_OVERLAP_PERCENT`.

    ``heading_lines`` overrides the unit's own ``is_heading_line``
    scan (callers that already classified the lines pass their spans in
    — the classifier then stays the single authority). Pure integer
    arithmetic; empty text yields an empty tuple.
    """
    if not text:
        return ()
    headings = (
        _heading_lines(text) if heading_lines is None else tuple(heading_lines)
    )
    starts = _region_starts(text, headings)
    heading_end_of = {ls: le for ls, le in headings}
    windows: list[ExtractWindow] = []
    ordinal = 1
    for index, (region_start, title) in enumerate(starts):
        region_end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        # A region's tail reaches up to the next heading line, so it
        # carries that line's leading newline; window text is a fixed
        # point of the normalization outlet (SC17), so the span is
        # trimmed off the trailing whitespace here like every seam.
        region_end = _trim_seam_end(text, region_end, region_start)
        if region_end <= region_start:
            continue
        region_windows, ordinal = _region_windows(
            text,
            ordinal=ordinal,
            region_start=region_start,
            region_end=region_end,
            title=title,
            heading_end=heading_end_of.get(region_start),
        )
        windows.extend(region_windows)
    return tuple(windows)


__all__ = [
    "EXTRACT_WINDOW_CHARS",
    "EXTRACT_WINDOW_TOKENS",
    "WINDOW_CHARS_PER_TOKEN",
    "WINDOW_OVERLAP_PERCENT",
    "WINDOW_OVERLAP_TOLERANCE",
    "ExtractWindow",
    "count_window_tokens",
    "is_heading_line",
    "line_offsets",
    "window_spans",
    "window_title",
]
