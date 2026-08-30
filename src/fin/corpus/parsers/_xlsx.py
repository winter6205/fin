"""Excel (.xlsx) parser: consecutive row blocks per worksheet -> frozen
units (T6 granularity).

One unit per :data:`~fin.corpus.parsers._common.XLSX_ROWS_PER_UNIT`
consecutive non-empty rows within a worksheet; worksheets are visited
in workbook order and unit ordinals are sequential across the whole
document. Cells are joined with ``" "`` within a row and rows with
``"\n"`` so any cell text stays a contiguous substring of the frozen
unit text (the citation-gate compatibility surface — SC7).

Stdlib only (``zipfile`` + ElementTree) — see ``_ooxml.py``.
"""

from __future__ import annotations

from xml.etree import ElementTree

from fin.corpus.errors import MalformedSourceParseError, NoUnitsParseError
from fin.corpus.parsers._common import (
    ParsedUnit,
    XLSX_ROWS_PER_UNIT,
    build_unit,
)
from fin.corpus.parsers._ooxml import open_zip, parse_xml

_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _load_shared_strings(
    zip_file, *, document_id: str
) -> list[str]:
    """Read ``xl/sharedStrings.xml`` (absent in some writers → [])."""
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []
    root = parse_xml(
        zip_file, "xl/sharedStrings.xml", document_id=document_id
    )
    return [
        "".join(t.text or "" for t in item.iter(_MAIN_NS + "t"))
        for item in root.iter(_MAIN_NS + "si")
    ]


def _worksheet_targets(
    zip_file, *, document_id: str
) -> list[str]:
    """Resolve workbook sheet order to worksheet part targets via the
    workbook's relationship part. Order is the workbook's own sheet
    sequence — deterministic and stable for a frozen source."""
    workbook = parse_xml(
        zip_file, "xl/workbook.xml", document_id=document_id
    )
    rels_root = parse_xml(
        zip_file,
        "xl/_rels/workbook.xml.rels",
        document_id=document_id,
    )
    rel_targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in rels_root.iter(_PKG_REL_NS + "Relationship")
    }
    targets: list[str] = []
    for sheet in workbook.iter(_MAIN_NS + "sheet"):
        rid = sheet.get(_REL_NS + "id")
        target = rel_targets.get(rid or "", "")
        if not target:
            raise MalformedSourceParseError(
                f"xlsx workbook of {document_id!r} has a sheet without a "
                "resolvable relationship target"
            )
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        targets.append(target)
    if not targets:
        raise MalformedSourceParseError(
            f"xlsx workbook of {document_id!r} declares no worksheets"
        )
    return targets


def _cell_text(
    cell: ElementTree.Element, shared_strings: list[str]
) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(_MAIN_NS + "is")
        if inline is not None:
            return "".join(t.text or "" for t in inline.iter(_MAIN_NS + "t"))
        return ""
    value = cell.find(_MAIN_NS + "v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        return shared_strings[int(value.text)]
    return value.text


def _sheet_rows(
    sheet: ElementTree.Element, shared_strings: list[str]
) -> list[str]:
    rows: list[str] = []
    for row in sheet.iter(_MAIN_NS + "row"):
        cells = [
            _cell_text(cell, shared_strings)
            for cell in row.iter(_MAIN_NS + "c")
        ]
        line = " ".join(cell for cell in cells if cell).strip()
        if line:
            rows.append(line)
    return rows


def parse_xlsx(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as .xlsx into per-sheet row-block units."""
    units: list[ParsedUnit] = []
    zip_file = open_zip(source_bytes, document_id=document_id)
    with zip_file:
        shared_strings = _load_shared_strings(
            zip_file, document_id=document_id
        )
        for target in _worksheet_targets(
            zip_file, document_id=document_id
        ):
            sheet = parse_xml(zip_file, target, document_id=document_id)
            rows = _sheet_rows(sheet, shared_strings)
            for block_start in range(0, len(rows), XLSX_ROWS_PER_UNIT):
                block = rows[block_start : block_start + XLSX_ROWS_PER_UNIT]
                units.append(
                    build_unit(
                        fmt="xlsx",
                        source_sha256=source_sha256,
                        document_id=document_id,
                        version=version,
                        unit=len(units) + 1,
                        text="\n".join(block),
                    )
                )
    if not units:
        raise NoUnitsParseError(
            f"xlsx source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_xlsx"]
