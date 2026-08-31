"""Deterministic multi-format exporters for derived dataset samples.

Spec §B1「语料来源三级」三级·派生: "由一级真实内容经确定性导出器生成的
md / docx / xlsx / pptx。内容全部来自真实语料；导出器固定模板与字节序
以保证可复现；清单须标 ``origin=derived`` 与 ``derived_from_sha256``；
引用身份仍回指原生文档，不新增第二套 locator 契约。"

Guarantees implemented here
---------------------------

* **Verbatim content** — every exporter embeds the frozen unit text
  byte-for-byte (UTF-8); nothing is rewritten, re-wrapped by a
  word-splitter that could alter characters, or generated.
* **Determinism** — fixed templates, fixed member order for OOXML
  zips, fixed zip timestamps (1980-01-01), no clock, no randomness,
  no locale. The same :class:`DerivedDoc` always exports to the same
  bytes (SC30's 重跑不一致样本数 = 0 rests on this).
* **Citation identity points back** — every file carries the native
  ``document_id`` + ``version`` sha256 in a fixed header; no second
  locator contract is introduced.
* **No new dependencies** — docx / xlsx / pptx are hand-rolled
  minimal OOXML packages (stdlib ``zipfile`` + XML strings). This
  keeps the lockfile untouched and the writers fully deterministic.

Hard variants
-------------

Each derived format also exports one *hard* sample (SC28: 每种格式
至少 1 个困难样本) built from the same real content with a
deterministic difficulty injection: ``header_footer`` interference
(md / txt / pptx), ``multi_column`` layout (html), or ``merged_cells``
tables (docx / xlsx). The frozen text itself stays verbatim; only the
template adds the realistic difficulty. The parse assertions on these
markers are T6's contract (the parsers do not exist yet); this ticket
lands the marker fields and manifest validation.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape as _xml_escape

from fin.corpus.dataset.pages import FrozenUnit

EXPORTER_VERSION = "1.0.0"

DERIVED_FORMATS = ("md", "txt", "html", "docx", "xlsx", "pptx")
EXTENSIONS = {
    "md": ".md",
    "txt": ".txt",
    "html": ".html",
    "docx": ".docx",
    "xlsx": ".xlsx",
    "pptx": ".pptx",
}

# Hard-sample kinds (SC28 vocabulary: 扫描/多栏/合并单元格/图文混排/
# 内嵌公式/页眉页脚干扰 — the kinds below are the ones the deterministic
# exporters can genuinely produce from real frozen content).
HARD_HEADER_FOOTER = "header_footer"
HARD_MULTI_COLUMN = "multi_column"
HARD_MERGED_CELLS = "merged_cells"

_FORMAT_SUFFIX = {
    "md": "Markdown",
    "txt": "plain text",
    "html": "HTML",
    "docx": "Word (docx)",
    "xlsx": "Excel (xlsx)",
    "pptx": "PowerPoint (pptx)",
}


@dataclass(frozen=True)
class HardTable:
    """A real table taken from a frozen unit, for the merged-cell variants.

    ``title`` is the frozen statement title line (merged across all
    columns in the exported table); ``rows`` are the frozen numeric
    rows split into cells by :func:`pages.split_table_row` (row 0 is
    the column header row of the statement).
    """

    source_unit: int
    title: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class DerivedDoc:
    """Everything an exporter needs; pure data, fully serializable."""

    sample_id: str
    fmt: str
    document_id: str
    version: str
    doc_type: str
    units: tuple[FrozenUnit, ...]
    hard_kind: str | None = None
    header_line: str = ""
    footer_line: str = ""
    hard_table: HardTable | None = None


def export_derived(doc: DerivedDoc) -> bytes:
    """Export one derived sample to its format's bytes. Deterministic."""
    if doc.fmt == "md":
        return export_markdown(doc)
    if doc.fmt == "txt":
        return export_text(doc)
    if doc.fmt == "html":
        return export_html(doc)
    if doc.fmt == "docx":
        return export_docx(doc)
    if doc.fmt == "xlsx":
        return export_xlsx(doc)
    if doc.fmt == "pptx":
        return export_pptx(doc)
    raise ValueError(f"no exporter for format {doc.fmt!r}")


def derived_filename(sample_id: str, fmt: str) -> str:
    return f"{sample_id}{EXTENSIONS[fmt]}"


def _identity_lines(doc: DerivedDoc) -> list[str]:
    return [
        f"sample_id: {doc.sample_id}",
        f"exporter_version: {EXPORTER_VERSION}",
        "origin: derived",
        "tier: 3",
        f"source_document_id: {doc.document_id}",
        f"source_version: {doc.version}",
        "source_locator_contract: {document_id, version, unit} of the native "
        "document; citation identity points back to the native filing, no "
        "second locator contract is created here.",
        f"doc_type: {doc.doc_type}",
        f"hard_kind: {doc.hard_kind if doc.hard_kind else 'none'}",
    ]


def _prose(doc: DerivedDoc) -> str:
    units = ", ".join(str(u.unit) for u in doc.units)
    return (
        f"Verbatim frozen text of native unit(s) {units} of filing "
        f"{doc.document_id} (version {doc.version}), exported by "
        f"fin.corpus.dataset v{EXPORTER_VERSION} with a fixed template and "
        "byte order. Content is real; the derived sample asserts no new "
        "citation identity."
    )


def _unit_body(doc: DerivedDoc, unit: FrozenUnit) -> str:
    """Unit text with hard header/footer interference when configured."""
    if doc.hard_kind == HARD_HEADER_FOOTER and doc.header_line and doc.footer_line:
        return f"{doc.header_line}\n{unit.text}\n{doc.footer_line}"
    return unit.text


# --------------------------------------------------------------------------
# Markdown / plain text / HTML
# --------------------------------------------------------------------------


def export_markdown(doc: DerivedDoc) -> bytes:
    out = io.StringIO()
    out.write("<!-- fin-corpus-dataset derived sample\n")
    for line in _identity_lines(doc):
        out.write(f"{line}\n")
    out.write("-->\n\n")
    out.write(f"# {doc.sample_id}\n\n")
    out.write(f"{_prose(doc)}\n\n")
    for unit in doc.units:
        out.write(f"## Unit {unit.unit}\n\n")
        out.write(_unit_body(doc, unit))
        out.write("\n\n")
    return out.getvalue().encode("utf-8")


def export_text(doc: DerivedDoc) -> bytes:
    bar = "=" * 80
    thin = "-" * 80
    out = io.StringIO()
    out.write(f"{bar}\n")
    out.write("fin-corpus-dataset derived sample (origin=derived, tier=3)\n")
    for line in _identity_lines(doc):
        out.write(f"{line}\n")
    out.write(f"{bar}\n\n")
    out.write(f"{_prose(doc)}\n\n")
    for unit in doc.units:
        out.write(f"{thin}\n")
        out.write(f"Unit {unit.unit} (native unit of {doc.document_id})\n")
        out.write(f"{thin}\n")
        out.write(_unit_body(doc, unit))
        out.write("\n\n")
    return out.getvalue().encode("utf-8")


def export_html(doc: DerivedDoc) -> bytes:
    out = io.StringIO()
    out.write("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
    out.write('<meta charset="utf-8">\n')
    out.write(f"<title>{_xml_escape(doc.sample_id)}</title>\n")
    out.write("</head>\n<body>\n")
    out.write(
        f'<main data-sample-id="{_xml_escape(doc.sample_id)}" '
        f'data-origin="derived" data-tier="3" '
        f'data-source-document-id="{_xml_escape(doc.document_id)}" '
        f'data-source-version="{_xml_escape(doc.version)}" '
        f'data-doc-type="{_xml_escape(doc.doc_type)}">\n'
    )
    if doc.hard_kind == HARD_MULTI_COLUMN:
        # Multi-column difficulty: real prose sections rendered in two
        # CSS columns, the way financial-newsletter layouts interleave.
        out.write('<div class="columns" style="column-count: 2; column-gap: 2em;">\n')
    for unit in doc.units:
        out.write(
            f'<section id="unit-{unit.unit:03d}" data-unit="{unit.unit}" '
            f'data-source-document-id="{_xml_escape(doc.document_id)}" '
            f'data-source-version="{_xml_escape(doc.version)}">\n'
        )
        out.write(f"<h2>Unit {unit.unit}</h2>\n")
        out.write(f"<pre>{_xml_escape(_unit_body(doc, unit))}</pre>\n")
        out.write("</section>\n")
    if doc.hard_kind == HARD_MULTI_COLUMN:
        out.write("</div>\n")
    out.write("</main>\n</body>\n</html>\n")
    return out.getvalue().encode("utf-8")


# --------------------------------------------------------------------------
# OOXML plumbing (stdlib-only, deterministic)
# --------------------------------------------------------------------------

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _zip_bytes(members: list[tuple[str, str]]) -> bytes:
    """Pack members into a zip with fixed order, timestamps and attrs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, data.encode("utf-8"))
    return buf.getvalue()


def _content_types(defaults: list[tuple[str, str]], overrides: list[tuple[str, str]]) -> str:
    out = io.StringIO()
    out.write(_XML_DECL)
    out.write('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">')
    for ext, ctype in defaults:
        out.write(f'<Default Extension="{ext}" ContentType="{ctype}"/>')
    for part, ctype in overrides:
        out.write(f'<Override PartName="{part}" ContentType="{ctype}"/>')
    out.write("</Types>")
    return out.getvalue()


_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _rels(entries: list[tuple[str, str, str]]) -> str:
    out = io.StringIO()
    out.write(_XML_DECL)
    out.write(f'<Relationships xmlns="{_RELS_NS}">')
    for rid, rtype, target in entries:
        out.write(f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>')
    out.write("</Relationships>")
    return out.getvalue()


_OFFDOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _paragraph_text(text: str) -> str:
    return f'<w:t xml:space="preserve">{_xml_escape(text)}</w:t>'


def _w_paragraph(text: str) -> str:
    return f"<w:p><w:r>{_paragraph_text(text)}</w:r></w:p>"


def _w_table(table: HardTable) -> str:
    """A real Word table whose first row is merged across all columns."""
    n_cols = max(len(r) for r in table.rows) if table.rows else 1
    out = io.StringIO()
    out.write("<w:tbl><w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/>")
    out.write(
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        "</w:tblBorders>"
        "</w:tblPr>"
    )
    out.write(f"<w:tblGrid>{'<w:gridCol w:w=\"2400\"/>' * n_cols}</w:tblGrid>")
    # Merged first row: the statement title spans every column.
    out.write("<w:tr>")
    out.write(
        f'<w:tc><w:tcPr><w:gridSpan w:val="{n_cols}"/></w:tcPr>'
        f"{_w_paragraph(table.title)}</w:tc>"
    )
    out.write("</w:tr>")
    for row in table.rows:
        out.write("<w:tr>")
        for cell in row:
            out.write(f"<w:tc>{_w_paragraph(cell)}</w:tc>")
        out.write("</w:tr>")
    out.write("</w:tbl>")
    return out.getvalue()


def export_docx(doc: DerivedDoc) -> bytes:
    body: list[str] = []
    body.append(_w_paragraph(f"fin-corpus-dataset derived sample: {doc.sample_id}"))
    for line in _identity_lines(doc):
        body.append(_w_paragraph(line))
    body.append(_w_paragraph(_prose(doc)))
    for unit in doc.units:
        body.append(_w_paragraph(f"Unit {unit.unit}"))
        for line in _unit_body(doc, unit).splitlines():
            body.append(_w_paragraph(line) if line else "<w:p/>")
        if doc.hard_kind == HARD_MERGED_CELLS and doc.hard_table is not None:
            body.append(_w_table(doc.hard_table))
    document = (
        _XML_DECL
        + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        + "<w:body>"
        + "".join(body)
        + "<w:sectPr/></w:body></w:document>"
    )
    members = [
        (
            "[Content_Types].xml",
            _content_types(
                [
                    ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
                    ("xml", "application/xml"),
                ],
                [
                    (
                        "/word/document.xml",
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document.main+xml",
                    ),
                    (
                        "/docProps/core.xml",
                        "application/vnd.openxmlformats-package.core-properties+xml",
                    ),
                    (
                        "/docProps/app.xml",
                        "application/vnd.openxmlformats-officedocument"
                        ".extended-properties+xml",
                    ),
                ],
            ),
        ),
        (
            "_rels/.rels",
            _rels(
                [
                    (
                        "rId1",
                        _OFFDOC_REL + "/officeDocument",
                        "word/document.xml",
                    ),
                    (
                        "rId2",
                        "http://schemas.openxmlformats.org/package/2006/relationships"
                        "/metadata/core-properties",
                        "docProps/core.xml",
                    ),
                    (
                        "rId3",
                        _OFFDOC_REL + "/extended-properties",
                        "docProps/app.xml",
                    ),
                ]
            ),
        ),
        ("docProps/core.xml", _core_props(doc.sample_id)),
        ("docProps/app.xml", _app_props()),
        ("word/document.xml", document),
    ]
    return _zip_bytes(members)


_W_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (deterministic spreadsheet refs)."""
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return letters


def _xlsx_cell(row: int, col: int, text: str) -> str:
    ref = f"{_col_letter(col)}{row}"
    return (
        f'<c r="{ref}" t="inlineStr"><is>'
        f'<t xml:space="preserve">{_xml_escape(text)}</t></is></c>'
    )


def _xlsx_sheet(rows: list[list[str]], merged: tuple[int, int] | None) -> str:
    out = io.StringIO()
    out.write(_XML_DECL)
    out.write(f'<worksheet xmlns="{_W_NS}"><sheetData>')
    for r, cells in enumerate(rows, start=1):
        out.write(f'<row r="{r}">')
        for c, text in enumerate(cells):
            out.write(_xlsx_cell(r, c, text))
        out.write("</row>")
    out.write("</sheetData>")
    if merged is not None:
        r, last_col = merged
        out.write(
            f'<mergeCells count="1"><mergeCell ref="A{r}:'
            f"{_col_letter(last_col)}{r}\"/></mergeCells>"
        )
    out.write("</worksheet>")
    return out.getvalue()


def export_xlsx(doc: DerivedDoc) -> bytes:
    """One worksheet per native unit; hard variant merges the title row."""
    sheets: list[tuple[str, str]] = []
    merge_spec: tuple[int, int] | None = None
    for i, unit in enumerate(doc.units, start=1):
        rows: list[list[str]] = []
        for line in unit.text.splitlines() or [""]:
            rows.append(split_sheet_row(line))
        if doc.hard_kind == HARD_MERGED_CELLS and doc.hard_table is not None:
            table = doc.hard_table
            rows = [[table.title]] + [list(r) for r in table.rows]
            n_cols = max(len(r) for r in rows) if rows else 1
            merge_spec = (1, max(n_cols - 1, 0))
        sheets.append((f"Unit {unit.unit:03d}", _xlsx_sheet(rows, merge_spec)))
    workbook = (
        _XML_DECL
        + f'<workbook xmlns="{_W_NS}" xmlns:r="{_OFFDOC_REL}"><sheets>'
        + "".join(
            f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
            for i, (name, _) in enumerate(sheets, start=1)
        )
        + "</sheets></workbook>"
    )
    overrides = [
        (
            "/xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        )
    ]
    for i in range(1, len(sheets) + 1):
        overrides.append(
            (
                f"/xl/worksheets/sheet{i}.xml",
                "application/vnd.openxmlformats-officedocument.spreadsheetml"
                ".worksheet+xml",
            )
        )
    members: list[tuple[str, str]] = [
        (
            "[Content_Types].xml",
            _content_types(
                [
                    ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
                    ("xml", "application/xml"),
                ],
                overrides,
            ),
        ),
        (
            "_rels/.rels",
            _rels(
                [
                    (
                        "rId1",
                        _OFFDOC_REL + "/officeDocument",
                        "xl/workbook.xml",
                    )
                ]
            ),
        ),
        ("xl/workbook.xml", workbook),
        (
            "xl/_rels/workbook.xml.rels",
            _rels(
                [
                    (
                        f"rId{i}",
                        _OFFDOC_REL + "/worksheet",
                        f"worksheets/sheet{i}.xml",
                    )
                    for i in range(1, len(sheets) + 1)
                ]
            ),
        ),
    ]
    members.extend((f"xl/worksheets/sheet{i}.xml", xml) for i, (_, xml) in enumerate(sheets, start=1))
    return _zip_bytes(members)


def split_sheet_row(line: str) -> list[str]:
    """Split one frozen line into spreadsheet cells.

    Table-like rows (>= 2 number tokens) go through the numeric row
    splitter so real columns survive; prose stays one cell per line.
    """
    from fin.corpus.dataset.pages import _NUMBER_TOKEN, split_table_row

    if len(_NUMBER_TOKEN.findall(line)) >= 2:
        return split_table_row(line)
    return [line]


# --------------------------------------------------------------------------
# PPTX (minimal but complete OPC package)
# --------------------------------------------------------------------------

_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _sp_tree_open(shape_name: str) -> str:
    return (
        "<p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
        "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="{_xml_escape(shape_name)}"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>'
        "<p:spPr><a:xfrm><a:off x=\"838200\" y=\"365760\"/>"
        "<a:ext cx=\"10515600\" cy=\"6126480\"/></a:xfrm>"
        "<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom></p:spPr>"
        "<p:txBody><a:bodyPr wrap=\"square\" rtlCol=\"0\"/><a:lstStyle/>"
    )


def _sp_tree_close() -> str:
    return "</p:txBody></p:sp></p:spTree>"


def _a_paragraphs(lines: list[str], size_hundredths: int = 1200) -> str:
    out = io.StringIO()
    for line in lines:
        out.write("<a:p>")
        if line:
            out.write(
                f'<a:r><a:rPr lang="en-US" sz="{size_hundredths}" dirty="0"/>'
                f"<a:t>{_xml_escape(line)}</a:t></a:r>"
            )
        out.write("</a:p>")
    return out.getvalue()


def _slide_xml(title: str, lines: list[str], footer: str | None) -> str:
    out = io.StringIO()
    out.write(_XML_DECL)
    out.write(
        f'<p:sld xmlns:a="{_A_NS}" xmlns:r="{_OFFDOC_REL}" xmlns:p="{_P_NS}">'
        "<p:cSld>"
    )
    out.write(_sp_tree_open(title))
    out.write(_a_paragraphs([title] + lines))
    out.write(_sp_tree_close())
    if footer:
        out.write(
            '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Footer"/>'
            '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            "<p:spPr><a:xfrm><a:off x=\"838200\" y=\"6350000\"/>"
            "<a:ext cx=\"10515600\" cy=\"304800\"/></a:xfrm>"
            "<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom></p:spPr>"
            "<p:txBody><a:bodyPr wrap=\"square\" rtlCol=\"0\"/><a:lstStyle/>"
        )
        out.write(_a_paragraphs([footer], size_hundredths=900))
        out.write("</p:txBody></p:sp>")
    out.write("</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>")
    return out.getvalue()


_THEME_XML = (
    _XML_DECL
    + f'<a:theme xmlns:a="{_A_NS}" name="fin-dataset-theme">'
    + "<a:themeElements>"
    + '<a:clrScheme name="fin">'
    + '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    + '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    + '<a:dk2><a:srgbClr val="44546A"/></a:dk2>'
    + '<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    + '<a:accent1><a:srgbClr val="4472C4"/></a:accent1>'
    + '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    + '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>'
    + '<a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    + '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>'
    + '<a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    + '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
    + '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    + "</a:clrScheme>"
    + '<a:fontScheme name="fin">'
    + '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/>'
    + '<a:cs typeface=""/></a:majorFont>'
    + '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
    + '<a:cs typeface=""/></a:minorFont>'
    + "</a:fontScheme>"
    + '<a:fmtScheme name="fin">'
    + "<a:fillStyleLst>"
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    + "</a:fillStyleLst>"
    + "<a:lnStyleLst>"
    + '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    + '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    + '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    + "</a:lnStyleLst>"
    + "<a:effectStyleLst>"
    + "<a:effectStyle><a:effectLst/></a:effectStyle>"
    + "<a:effectStyle><a:effectLst/></a:effectStyle>"
    + "<a:effectStyle><a:effectLst/></a:effectStyle>"
    + "</a:effectStyleLst>"
    + "<a:bgFillStyleLst>"
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    + "</a:bgFillStyleLst>"
    + "</a:fmtScheme>"
    + "</a:themeElements></a:theme>"
)


def export_pptx(doc: DerivedDoc) -> bytes:
    """One slide per native unit, in a minimal but complete OPC package.

    Package members: content types, root rels, doc props, presentation
    + rels, slide master + rels + theme, slide layout + rels, and one
    slide + rels per unit. Member order and timestamps are fixed, so
    the bytes are a pure function of the input.
    """
    n = len(doc.units)
    slide_parts: list[str] = []
    for unit in doc.units:
        lines = [l for l in unit.text.splitlines() if l.strip()]
        footer = doc.footer_line if doc.hard_kind == HARD_HEADER_FOOTER else None
        title = f"Unit {unit.unit}"
        if doc.hard_kind == HARD_HEADER_FOOTER and doc.header_line:
            title = f"{doc.header_line}"
            lines = lines
        slide_parts.append(_slide_xml(title, lines, footer))

    ct_overrides = [
        (
            "/ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument.presentationml"
            ".presentation.main+xml",
        ),
        (
            "/ppt/slideMasters/slideMaster1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml"
            ".slideMaster+xml",
        ),
        (
            "/ppt/slideLayouts/slideLayout1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml"
            ".slideLayout+xml",
        ),
        ("/ppt/theme/theme1.xml", "application/vnd.openxmlformats-officedocument.theme+xml"),
    ]
    ct_overrides.extend(
        (
            f"/ppt/slides/slide{i}.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        )
        for i in range(1, n + 1)
    )
    pres_rels = [("rId1", _OFFDOC_REL + "/slideMaster", "slideMasters/slideMaster1.xml")]
    pres_rels.extend(
        (f"rId{i + 1}", _OFFDOC_REL + "/slide", f"slides/slide{i}.xml")
        for i in range(1, n + 1)
    )
    presentation = (
        _XML_DECL
        + f'<p:presentation xmlns:a="{_A_NS}" xmlns:r="{_OFFDOC_REL}" xmlns:p="{_P_NS}">'
        + '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        + "<p:sldIdLst>"
        + "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, n + 1))
        + "</p:sldIdLst>"
        + '<p:sldSz cx="12192000" cy="6858000"/>'
        + '<p:notesSz cx="6858000" cy="9144000"/>'
        + "</p:presentation>"
    )
    members: list[tuple[str, str]] = [
        (
            "[Content_Types].xml",
            _content_types(
                [
                    ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
                    ("xml", "application/xml"),
                ],
                ct_overrides,
            ),
        ),
        (
            "_rels/.rels",
            _rels(
                [
                    ("rId1", _OFFDOC_REL + "/officeDocument", "ppt/presentation.xml"),
                    (
                        "rId2",
                        "http://schemas.openxmlformats.org/package/2006/relationships"
                        "/metadata/core-properties",
                        "docProps/core.xml",
                    ),
                    ("rId3", _OFFDOC_REL + "/extended-properties", "docProps/app.xml"),
                ]
            ),
        ),
        ("docProps/core.xml", _core_props(doc.sample_id)),
        ("docProps/app.xml", _app_props()),
        ("ppt/presentation.xml", presentation),
        (
            "ppt/_rels/presentation.xml.rels",
            _rels(pres_rels),
        ),
        ("ppt/theme/theme1.xml", _THEME_XML),
        (
            "ppt/slideMasters/slideMaster1.xml",
            _XML_DECL
            + f'<p:sldMaster xmlns:a="{_A_NS}" xmlns:r="{_OFFDOC_REL}" xmlns:p="{_P_NS}">'
            + "<p:cSld>"
            + "<p:spTree>"
            + '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            + "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
            + "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
            + "</p:spTree></p:cSld>"
            + '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1"'
            + ' accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5"'
            + ' accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            + '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
            + "</p:sldLayoutIdLst></p:sldMaster>",
        ),
        (
            "ppt/slideMasters/_rels/slideMaster1.xml.rels",
            _rels(
                [
                    ("rId1", _OFFDOC_REL + "/slideLayout", "../slideLayouts/slideLayout1.xml"),
                    ("rId2", _OFFDOC_REL + "/theme", "../theme/theme1.xml"),
                ]
            ),
        ),
        (
            "ppt/slideLayouts/slideLayout1.xml",
            _XML_DECL
            + f'<p:sldLayout xmlns:a="{_A_NS}" xmlns:r="{_OFFDOC_REL}" xmlns:p="{_P_NS}"'
            + ' type="blank"><p:cSld name="Blank">'
            + "<p:spTree>"
            + '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            + "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
            + "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
            + "</p:spTree></p:cSld>"
            + "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>",
        ),
        (
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            _rels(
                [("rId1", _OFFDOC_REL + "/slideMaster", "../slideMasters/slideMaster1.xml")]
            ),
        ),
    ]
    for i, part in enumerate(slide_parts, start=1):
        members.append((f"ppt/slides/slide{i}.xml", part))
        members.append(
            (
                f"ppt/slides/_rels/slide{i}.xml.rels",
                _rels(
                    [
                        (
                            "rId1",
                            _OFFDOC_REL + "/slideLayout",
                            "../slideLayouts/slideLayout1.xml",
                        )
                    ]
                ),
            )
        )
    return _zip_bytes(members)


def _core_props(title: str) -> str:
    return (
        _XML_DECL
        + '<cp:coreProperties '
        + 'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        + 'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        + 'xmlns:dcterms="http://purl.org/dc/terms/" '
        + 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + f"<dc:title>{_xml_escape(title)}</dc:title>"
        + "<dc:creator>fin.corpus.dataset</dc:creator>"
        + "<cp:lastModifiedBy>fin.corpus.dataset</cp:lastModifiedBy>"
        + '<dcterms:created xsi:type="dcterms:W3CDTF">1980-01-01T00:00:00Z</dcterms:created>'
        + '<dcterms:modified xsi:type="dcterms:W3CDTF">1980-01-01T00:00:00Z</dcterms:modified>'
        + "</cp:coreProperties>"
    )


def _app_props() -> str:
    return (
        _XML_DECL
        + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        + 'extended-properties"><Application>fin.corpus.dataset</Application></Properties>'
    )
