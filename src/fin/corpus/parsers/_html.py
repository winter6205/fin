"""HTML parser: heading structure blocks -> frozen units (T6
granularity).

Stdlib ``html.parser.HTMLParser`` only (no BeautifulSoup — the ACR
restraint rule and "优先纯 stdlib" both point at the stdlib here; HTML
text extraction does not justify a dependency).

Rule (parse rule version ``parse-2``):

* ``h1``–``h6`` start a new block; the heading text is the block's
  first line;
* block content is text in document order; ``script`` / ``style`` /
  ``head`` / ``template`` content is dropped; table cells (``td`` /
  ``th``) are joined with ``" "`` and table rows (``tr``) with
  ``"\n"`` so cell text stays a contiguous substring of the frozen
  unit text (SC7);
* consecutive blocks are merged under
  :data:`~fin.corpus.parsers._common.BLOCK_MERGE_CAP_CHARS` (the
  deterministic cap; a block larger than the cap is never split).

Heading tags are carried as STRUCTURAL SPANS, not re-guessed from the
frozen text: each produced line records whether it came from inside an
``h1``–``h6`` tag, and the block hands those line indices to
:func:`~fin.corpus.parsers._common.build_unit` as ``heading_lines``.
The tag itself never reaches the frozen text (SC7's contiguity rule),
and the window classifier stays ATX + UPPERCASE-only — a title-case
``<h2>Revenue</h2>`` resolves its title from the span, not from a
widened classifier (ADR 0053 / 0060).
"""

from __future__ import annotations

from html.parser import HTMLParser

from fin.corpus.errors import (
    MalformedSourceParseError,
    NoUnitsParseError,
)
from fin.corpus.parsers._common import (
    ParsedUnit,
    StructuredBlock,
    build_unit,
    merge_structured_blocks,
)

_HEADING_TAGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))
_DROP_TAGS = frozenset(("script", "style", "head", "template"))
_CELL_TAGS = frozenset(("td", "th"))
_ROW_TAGS = frozenset(("tr",))
_BLOCK_TAGS = frozenset(
    (
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "blockquote",
        "pre",
        "table",
        "figcaption",
        "header",
        "footer",
    )
)


class _HeadingBlockParser(HTMLParser):
    """Collect heading-scoped blocks from an HTML document.

    Structure: ``self._current`` holds the lines of the block being
    accumulated (a heading opens a new block and contributes its first
    line); ``self._blocks`` receives finished blocks in document
    order. Line discipline: newline markers are emitted at block-level
    element and table-row boundaries; within a line, cell boundaries
    emit ``" "``. The raw (non-normalized) block list is returned;
    freezing happens in ``build_unit``.

    Each block also records which of its lines came from inside a
    heading tag: ``self._in_heading`` is the open-tag state,
    ``self._heading_lines`` collects the indices. The two flags are
    kept strictly apart — a line index may only be appended inside
    :meth:`_flush_line`, and the block's list may only be captured and
    reset inside :meth:`_flush_block` — so a line index can never
    outlive the block it belongs to. An unclosed heading tag keeps
    marking its trailing lines (malformed input; deterministic all the
    same).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks: list[StructuredBlock] = []
        self._current: list[str] = []
        self._heading_lines: list[int] = []
        self._in_heading = False
        self._line: list[str] = []
        self._cell: list[str] = []
        self._in_cell = 0
        self._drop_depth = 0

    # -- state helpers -------------------------------------------------

    def _flush_line(self) -> None:
        text = "".join(self._line).strip()
        self._line = []
        if text:
            self._current.append(text)
            if self._in_heading:
                self._heading_lines.append(len(self._current) - 1)

    def _flush_block(self) -> None:
        self._flush_line()
        if self._current:
            self._blocks.append(
                StructuredBlock(
                    tuple(self._current), tuple(self._heading_lines)
                )
            )
            # Both reset together: an index left behind would mark a
            # line of the NEXT block.
            self._current = []
            self._heading_lines = []

    # -- tag handlers ---------------------------------------------------

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag in _HEADING_TAGS:
            # A heading starts a new block; its text becomes the
            # block's first line, and the lines it contributes are
            # marked as structural headings until the close tag.
            self._flush_block()
            self._in_heading = True
        elif tag in _CELL_TAGS:
            self._in_cell += 1
            self._cell = []
        elif tag in _ROW_TAGS or tag == "br" or tag in _BLOCK_TAGS:
            self._flush_line()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag in _CELL_TAGS:
            self._in_cell = max(0, self._in_cell - 1)
            self._line.append("".join(self._cell).strip())
            self._cell = []
        elif (
            tag in _ROW_TAGS
            or tag in _BLOCK_TAGS
            or tag in _HEADING_TAGS
            or tag in ("body", "html")
        ):
            self._flush_line()
            if tag in _HEADING_TAGS:
                self._in_heading = False

    # -- data handlers ---------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_cell:
            self._cell.append(data)
        else:
            self._line.append(data)

    def structured_blocks(self) -> tuple[StructuredBlock, ...]:
        self._flush_block()
        return tuple(self._blocks)


def _joined_line_index(lines: tuple[str, ...], index: int) -> int:
    """Map a ``StructuredBlock.lines`` index to its index in
    ``"\\n".join(lines)``.

    The two spaces are not the same one: ``_flush_line`` appends a
    line's whole text, and a ``<pre>`` block's text carries real
    newlines, so ONE parser line becomes SEVERAL frozen lines. An index
    counted over the block's lines must therefore be shifted by the
    extra newlines the lines before it contribute — otherwise the
    heading claim lands on a body line of the same block (measured on
    the committed fixtures: a ``<h2>`` after a ``<pre>`` block was
    mapped to the middle of that block's prose).
    """
    return sum(len(line.split("\n")) for line in lines[:index])


def parse_html(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as HTML into structure-block units."""
    parser = _HeadingBlockParser()
    try:
        raw = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedSourceParseError(
            f"html source for {document_id!r} is not valid UTF-8: {exc}"
        ) from exc
    parser.feed(raw)
    parser.close()
    units = [
        build_unit(
            fmt="html",
            source_sha256=source_sha256,
            document_id=document_id,
            version=version,
            unit=ordinal,
            text="\n".join(block.lines),
            heading_lines=tuple(
                _joined_line_index(block.lines, index)
                for index in block.heading_lines
            ),
        )
        for ordinal, block in enumerate(
            merge_structured_blocks(parser.structured_blocks()), start=1
        )
    ]
    if not units:
        raise NoUnitsParseError(
            f"html source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_html"]
