"""Word (.docx) parser: structure blocks -> frozen units (T6).

Word granularity follows the same structure-block direction as HTML /
Markdown (plan T6 "已决"): heading paragraphs (``w:pStyle`` values
starting with ``Heading``/``Title``, case-insensitive) start a new
block; consecutive blocks are merged under the deterministic cap
(:data:`~fin.corpus.parsers._common.BLOCK_MERGE_CAP_CHARS`). Tables
(``w:tbl``) are emitted as one block with ``" "``-joined cells and
newline-joined rows so table cell text stays a contiguous substring of
the frozen unit text (the citation-gate compatibility surface).

The derived dataset samples carry no heading styles; there the block
rule degenerates deterministically to cap-merge over the paragraph /
table sequence — same code path, no special case.
"""

from __future__ import annotations

from xml.etree import ElementTree

from fin.corpus.errors import MalformedSourceParseError, NoUnitsParseError
from fin.corpus.parsers._common import (
    ParsedUnit,
    build_unit,
    merge_blocks,
)
from fin.corpus.parsers._ooxml import open_zip, parse_xml

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_HEADING_STYLE_MARKERS = ("heading", "title")


def _is_heading(paragraph: ElementTree.Element) -> bool:
    p_style = paragraph.find(_W_NS + "pPr/" + _W_NS + "pStyle")
    if p_style is None:
        return False
    value = (p_style.get(_W_NS + "val") or "").lower()
    return any(value.startswith(marker) for marker in _HEADING_STYLE_MARKERS)


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    return "".join(t.text or "" for t in paragraph.iter(_W_NS + "t"))


def _table_block(table: ElementTree.Element) -> str:
    rows: list[str] = []
    for row in table.iter(_W_NS + "tr"):
        cells = [
            " ".join(
                _paragraph_text(paragraph)
                for paragraph in cell.iter(_W_NS + "p")
            ).strip()
            for cell in row.iter(_W_NS + "tc")
        ]
        rows.append(" ".join(cell for cell in cells if cell))
    return "\n".join(row for row in rows if row)


def _segment_blocks(root: ElementTree.Element) -> list[str]:
    """Walk the document body in order, emitting one block per
    heading-scoped section: a heading paragraph starts a new block and
    carries through until the next heading; tables are standalone
    blocks."""
    blocks: list[str] = []
    current: list[str] = []
    body = root.find(_W_NS + "body")
    if body is None:
        raise MalformedSourceParseError(
            "docx part word/document.xml has no w:body"
        )
    for child in body:
        if child.tag == _W_NS + "p":
            text = _paragraph_text(child)
            if _is_heading(child):
                if current:
                    blocks.append("\n".join(current))
                    current = []
                if text:
                    blocks.append(text)
            elif text:
                current.append(text)
        elif child.tag == _W_NS + "tbl":
            block = _table_block(child)
            if block:
                current.append(block)
    if current:
        blocks.append("\n".join(current))
    return blocks


def parse_docx(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as .docx into structure-block units."""
    zip_file = open_zip(source_bytes, document_id=document_id)
    with zip_file:
        root = parse_xml(
            zip_file, "word/document.xml", document_id=document_id
        )
        blocks = merge_blocks(_segment_blocks(root))
    units = [
        build_unit(
            fmt="docx",
            source_sha256=source_sha256,
            document_id=document_id,
            version=version,
            unit=ordinal,
            text=block,
        )
        for ordinal, block in enumerate(blocks, start=1)
    ]
    if not units:
        raise NoUnitsParseError(
            f"docx source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_docx"]
