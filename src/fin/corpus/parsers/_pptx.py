"""PowerPoint (.pptx) parser: physical slides -> frozen units (T6
granularity).

One unit per physical slide (plan T6 "已决"); the unit ordinal equals
the slide's physical position (numeric sort of ``ppt/slides/slideN.xml``
— never lexical). Within a slide, ``a:p`` paragraph runs (``a:t``) are
joined with ``""`` and paragraphs with ``"\n"``.

Stdlib only (``zipfile`` + ElementTree) — see ``_ooxml.py``.
"""

from __future__ import annotations

from xml.etree import ElementTree

from fin.corpus.errors import NoUnitsParseError
from fin.corpus.parsers._common import ParsedUnit, build_unit
from fin.corpus.parsers._ooxml import open_zip, part_number, parse_xml

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDES_DIR = "ppt/slides/"


def _slide_text(slide: ElementTree.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in slide.iter(_A_NS + "p"):
        run = "".join(t.text or "" for t in paragraph.iter(_A_NS + "t"))
        if run.strip():
            paragraphs.append(run.strip())
    return "\n".join(paragraphs)


def parse_pptx(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as .pptx into per-slide frozen units."""
    units: list[ParsedUnit] = []
    zip_file = open_zip(source_bytes, document_id=document_id)
    with zip_file:
        slide_names = sorted(
            (
                name
                for name in zip_file.namelist()
                if name.startswith(_SLIDES_DIR)
                and name.endswith(".xml")
                and "/" not in name[len(_SLIDES_DIR) :]
            ),
            key=part_number,
        )
        for slide_name in slide_names:
            slide = parse_xml(
                zip_file, slide_name, document_id=document_id
            )
            text = _slide_text(slide)
            if not text:
                continue
            units.append(
                build_unit(
                    fmt="pptx",
                    source_sha256=source_sha256,
                    document_id=document_id,
                    version=version,
                    unit=part_number(slide_name) or len(units) + 1,
                    text=text,
                )
            )
    if not units:
        raise NoUnitsParseError(
            f"pptx source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_pptx"]
