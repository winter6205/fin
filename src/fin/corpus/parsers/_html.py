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
"""

from __future__ import annotations

from html.parser import HTMLParser

from fin.corpus.errors import (
    MalformedSourceParseError,
    NoUnitsParseError,
)
from fin.corpus.parsers._common import (
    ParsedUnit,
    build_unit,
    merge_blocks,
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
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks: list[str] = []
        self._current: list[str] = []
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

    def _flush_block(self) -> None:
        self._flush_line()
        if self._current:
            self._blocks.append("\n".join(self._current))
            self._current = []

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
            # block's first line.
            self._flush_block()
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

    # -- data handlers ---------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_cell:
            self._cell.append(data)
        else:
            self._line.append(data)

    def blocks(self) -> list[str]:
        self._flush_block()
        return self._blocks


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
            text=block,
        )
        for ordinal, block in enumerate(
            merge_blocks(parser.blocks()), start=1
        )
    ]
    if not units:
        raise NoUnitsParseError(
            f"html source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_html"]
