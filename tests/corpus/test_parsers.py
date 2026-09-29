"""T6 / SC6 / SC7 / SC10: seven-format parsing into frozen-unit text +
structured metadata.

Each format produces >= 1 non-empty frozen unit; table-class samples
(Excel worksheet, HTML table region, PDF table region) yield cell text
that passes the >= 32-char citation-gate contiguous-substring check;
every unit carries the eight metadata fields (format, source byte
hash, locator triple, unit ordinal, char count, language, parse rule
version, params-table version) and a parse missing any one of them
terminates with a named failure — never a "field missing but
successful" unit.

Default-suite discipline: only small samples are opened. The PDF
sample is capped at the first few physical pages via ``max_units``;
no large-corpus tree is walked, no service is started, no network is
touched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fin.corpus.errors import (
    EmptySourceParseError,
    MalformedSourceParseError,
    NoUnitsParseError,
    ParseError,
    ParseMetadataIncompleteError,
    UnsupportedFormatParseError,
)
from fin.corpus.extracted import Locator
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.parsers import (
    PARAMS_TABLE_VERSION,
    PARSE_RULE_VERSION,
    SUPPORTED_FORMATS,
    parse_document,
)
from fin.host.citation_gate import DoneResult, EvidenceItem, verify_citations

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PDF_SAMPLE = (
    _PROJECT_ROOT / "data" / "corpus" / "pdf" / "2023_Q3_AAPL.pdf"
)

# Default-suite pdf work is bounded: the first few physical pages
# already include the income-statement table region (page 4).
_PDF_MAX_UNITS = 6

# Small derived samples (tier 3, a few hundred KB each at most).
_DOCX_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/docx/AAPL-2022Q3__table_dense__docx__docx.docx"
)
_MD_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)
_TXT_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/txt/AAPL-2022Q3__narrative__txt__txt.txt"
)
_HTML_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/html/AAPL-2022Q3__table_dense__html__html.html"
)
_XLSX_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/xlsx/AAPL-2022Q3__table_dense__xlsx__xlsx.xlsx"
)
_PPTX_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/pptx/AAPL-2023Q1__table_dense__pptx__pptx.pptx"
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_sample(
    path: Path,
    *,
    fmt: str,
    document_id: str,
    max_units: int | None = None,
):
    return parse_document(
        path,
        fmt=fmt,
        document_id=document_id,
        version=_sha256_file(path),
        max_units=max_units,
    )


# ----- SC6: seven formats, each >= 1 non-empty frozen unit -------------


@pytest.mark.parametrize(
    "fmt",
    ["pdf", "docx", "xlsx", "pptx", "html", "md", "txt"],
)
def test_each_format_produces_nonempty_frozen_unit(fmt: str) -> None:
    paths = {
        "pdf": _PDF_SAMPLE,
        "docx": _DOCX_SAMPLE,
        "xlsx": _XLSX_SAMPLE,
        "pptx": _PPTX_SAMPLE,
        "html": _HTML_SAMPLE,
        "md": _MD_SAMPLE,
        "txt": _TXT_SAMPLE,
    }
    result = _parse_sample(paths[fmt], fmt=fmt, document_id="T6-DEMO")
    assert result.fmt == fmt
    assert len(result.units) >= 1
    for unit in result.units:
        assert unit.text != ""
        # Frozen text is a fixed point of the single normalization
        # outlet (T5): the parser only calls it, never re-implements.
        assert normalize_frozen_text(unit.text) == unit.text
    assert any(unit.text.strip() for unit in result.units)


# ----- SC10: the eight metadata fields ---------------------------------


@pytest.mark.parametrize(
    "fmt",
    ["pdf", "docx", "xlsx", "pptx", "html", "md", "txt"],
)
def test_metadata_eight_fields_present(fmt: str) -> None:
    paths = {
        "pdf": _PDF_SAMPLE,
        "docx": _DOCX_SAMPLE,
        "xlsx": _XLSX_SAMPLE,
        "pptx": _PPTX_SAMPLE,
        "html": _HTML_SAMPLE,
        "md": _MD_SAMPLE,
        "txt": _TXT_SAMPLE,
    }
    path = paths[fmt]
    result = _parse_sample(path, fmt=fmt, document_id="T6-DEMO")
    for unit in result.units:
        # 1. format
        assert unit.fmt in SUPPORTED_FORMATS
        # 2. source byte hash
        assert unit.source_sha256 == _sha256_file(path)
        # 3. locator triple {document_id, version, unit}
        assert unit.locator.document_id == "T6-DEMO"
        assert unit.locator.version == _sha256_file(path)
        # 4. unit ordinal (1-based, document-unique)
        assert unit.locator.unit >= 1
        # 5. char count
        assert unit.char_count == len(unit.text) > 0
        # 6. language
        assert unit.language != ""
        # 7. parse rule version
        assert unit.parse_rule_version == PARSE_RULE_VERSION
        # 8. params-table version (placeholder until T9 takes over)
        assert unit.params_table_version == PARAMS_TABLE_VERSION
    # Unit ordinals are unique and strictly increasing.
    ordinals = [unit.locator.unit for unit in result.units]
    assert ordinals == sorted(set(ordinals))


# ----- SC7: table-class cell text passes the citation gate -------------


def _gate_span(unit_text: str, anchor: str) -> str:
    """Build a >= 32-char contiguous span of ``unit_text`` around the
    table cell text ``anchor`` (a contiguous substring of the frozen
    unit text by construction)."""
    idx = unit_text.index(anchor)
    start = max(0, idx - 20)
    end = min(len(unit_text), idx + len(anchor) + 20)
    span = unit_text[start:end]
    assert len(span) >= 32
    assert anchor in span
    return span


def _gate_passes(span: str, unit) -> None:
    result = verify_citations(
        span,
        [
            EvidenceItem(
                locator=Locator(
                    document_id=unit.locator.document_id,
                    version=unit.locator.version,
                    unit=unit.locator.unit,
                ),
                unit_text=unit.text,
            )
        ],
        repair_count=0,
        known_document_ids={unit.locator.document_id},
    )
    assert isinstance(result, DoneResult)
    assert result.citations[0].quote == span


def test_xlsx_cell_text_passes_citation_gate() -> None:
    result = _parse_sample(_XLSX_SAMPLE, fmt="xlsx", document_id="T6-DEMO")
    cell = "SECURITIES AND EXCHANGE COMMISSION"
    unit = next(u for u in result.units if cell in u.text)
    span = _gate_span(unit.text, cell)
    _gate_passes(span, unit)


def test_html_table_cell_text_passes_citation_gate() -> None:
    result = _parse_sample(_HTML_SAMPLE, fmt="html", document_id="T6-DEMO")
    anchor = "Total net sales 82,959 81,434 304,182 282,457"
    unit = next(u for u in result.units if anchor in u.text)
    span = _gate_span(unit.text, anchor)
    _gate_passes(span, unit)


def test_pdf_table_region_cell_text_passes_citation_gate() -> None:
    result = _parse_sample(
        _PDF_SAMPLE,
        fmt="pdf",
        document_id="AAPL-2023Q3",
        max_units=_PDF_MAX_UNITS,
    )
    anchor = "Total net sales 81,797 82,959 293,787 304,182"
    unit = next(u for u in result.units if anchor in u.text)
    span = _gate_span(unit.text, anchor)
    _gate_passes(span, unit)


# ----- determinism under one parse-rule version (SC12 function level) --


@pytest.mark.parametrize(
    "fmt",
    ["pdf", "docx", "xlsx", "pptx", "html", "md", "txt"],
)
def test_same_input_parses_identically_twice(fmt: str) -> None:
    paths = {
        "pdf": (_PDF_SAMPLE, _PDF_MAX_UNITS),
        "docx": (_DOCX_SAMPLE, None),
        "xlsx": (_XLSX_SAMPLE, None),
        "pptx": (_PPTX_SAMPLE, None),
        "html": (_HTML_SAMPLE, None),
        "md": (_MD_SAMPLE, None),
        "txt": (_TXT_SAMPLE, None),
    }
    path, max_units = paths[fmt]
    first = _parse_sample(
        path, fmt=fmt, document_id="T6-DEMO", max_units=max_units
    )
    second = _parse_sample(
        path, fmt=fmt, document_id="T6-DEMO", max_units=max_units
    )
    assert first == second


# ----- granularity direction (decided in plan T6) ----------------------


def test_pdf_units_are_physical_pages() -> None:
    result = _parse_sample(
        _PDF_SAMPLE,
        fmt="pdf",
        document_id="AAPL-2023Q3",
        max_units=_PDF_MAX_UNITS,
    )
    assert [u.locator.unit for u in result.units] == list(
        range(1, _PDF_MAX_UNITS + 1)
    )


def test_pptx_units_are_physical_slides() -> None:
    result = _parse_sample(_PPTX_SAMPLE, fmt="pptx", document_id="T6-DEMO")
    # Expected ordinals = the deck's physical slide count, derived
    # from the archive itself (one unit per slide, in slide order).
    import re
    import zipfile

    with zipfile.ZipFile(_PPTX_SAMPLE) as zf:
        slide_count = len(
            [
                n
                for n in zf.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
            ]
        )
    assert [u.locator.unit for u in result.units] == list(
        range(1, slide_count + 1)
    )


def test_xlsx_units_are_consecutive_row_blocks_within_a_sheet() -> None:
    result = _parse_sample(_XLSX_SAMPLE, fmt="xlsx", document_id="T6-DEMO")
    assert len(result.units) >= 1
    for unit in result.units:
        # Every unit is a row block: no unit carries more rows than the
        # fixed block size (64 consecutive rows per unit, per sheet).
        assert len(unit.text.split("\n")) <= 64


def test_md_units_are_heading_structure_blocks() -> None:
    result = _parse_sample(_MD_SAMPLE, fmt="md", document_id="T6-DEMO")
    assert len(result.units) > 1
    headings = [
        line
        for line in _MD_SAMPLE.read_text().splitlines()
        if line.startswith("## Unit")
    ]
    body = "\n".join(u.text for u in result.units)
    for heading in headings:
        assert heading in body


def test_html_units_are_heading_structure_blocks() -> None:
    result = _parse_sample(_HTML_SAMPLE, fmt="html", document_id="T6-DEMO")
    assert len(result.units) > 1
    headings = [
        line
        for line in _HTML_SAMPLE.read_text().splitlines()
        if line.startswith("<h2>")
    ]
    body = "\n".join(u.text for u in result.units)
    for heading in headings:
        heading_text = heading.replace("<h2>", "").replace("</h2>", "")
        assert heading_text in body


def test_txt_units_are_blank_line_paragraphs(tmp_path: Path) -> None:
    doc = tmp_path / "sample.txt"
    doc.write_text(
        "First paragraph line one\ncontinues here.\n"
        "\n"
        "Second paragraph.\n"
        "\n\n"
        "Third paragraph.\n",
        encoding="utf-8",
    )
    result = parse_document(
        doc,
        fmt="txt",
        document_id="T6-TXT",
        version=_sha256_file(doc),
    )
    texts = [u.text for u in result.units]
    assert texts == [
        "First paragraph line one\ncontinues here.",
        "Second paragraph.",
        "Third paragraph.",
    ]


def test_html_small_sections_merge_up_to_the_deterministic_cap(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "sample.html"
    doc.write_text(
        "<html><body>"
        "<h2>Alpha</h2><p>" + "x" * 50 + "</p>"
        "<h2>Beta</h2><p>" + "y" * 50 + "</p>"
        "</body></html>",
        encoding="utf-8",
    )
    result = parse_document(
        doc,
        fmt="html",
        document_id="T6-HTML",
        version=_sha256_file(doc),
    )
    # Both tiny sections merge into one deterministic unit.
    assert len(result.units) == 1
    assert "Alpha" in result.units[0].text
    assert "Beta" in result.units[0].text


# ----- named failures ---------------------------------------------------


def test_zero_byte_source_is_a_named_failure(tmp_path: Path) -> None:
    doc = tmp_path / "empty.txt"
    doc.write_bytes(b"")
    with pytest.raises(EmptySourceParseError):
        parse_document(
            doc, fmt="txt", document_id="T6-EMPTY", version="v0"
        )


def test_whitespace_only_source_yields_no_units_named_failure(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "blank.txt"
    doc.write_text("  \n\n   \n", encoding="utf-8")
    with pytest.raises(NoUnitsParseError):
        parse_document(
            doc, fmt="txt", document_id="T6-BLANK", version="v0"
        )


def test_unsupported_format_is_a_named_failure(tmp_path: Path) -> None:
    doc = tmp_path / "sample.txt"
    doc.write_text("hello world", encoding="utf-8")
    with pytest.raises(UnsupportedFormatParseError):
        parse_document(
            doc, fmt="rtf", document_id="T6-RTF", version="v0"
        )


def test_corrupt_docx_is_a_named_failure(tmp_path: Path) -> None:
    doc = tmp_path / "corrupt.docx"
    doc.write_bytes(b"PK\x03\x04 definitely not a real zip")
    with pytest.raises(MalformedSourceParseError):
        parse_document(
            doc, fmt="docx", document_id="T6-CORRUPT", version="v0"
        )


def test_missing_metadata_field_is_a_named_failure(tmp_path: Path) -> None:
    doc = tmp_path / "sample.txt"
    doc.write_text("some content", encoding="utf-8")
    with pytest.raises(ParseMetadataIncompleteError):
        parse_document(
            doc, fmt="txt", document_id="", version="v0"
        )
    with pytest.raises(ParseMetadataIncompleteError):
        parse_document(
            doc, fmt="txt", document_id="T6-NOVER", version=""
        )


def test_named_failures_share_the_parse_error_family() -> None:
    for exc_type in (
        EmptySourceParseError,
        NoUnitsParseError,
        UnsupportedFormatParseError,
        MalformedSourceParseError,
        ParseMetadataIncompleteError,
    ):
        assert issubclass(exc_type, ParseError)
        assert issubclass(exc_type, RuntimeError)
