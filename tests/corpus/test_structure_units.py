"""Tests for structure-aware citable units on the existing corpus path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fin.corpus.structure_units import (
    ParsedSection,
    StructureLocator,
    StructureUnitLimitError,
    parse_structure_units,
)
from fin.corpus.structure_units_errors import StructureParseError
from fin.host.tools import list_files


class FakeParser:
    def __init__(self, sections: list[ParsedSection]) -> None:
        self.sections = sections

    def parse(self, source: Path) -> list[ParsedSection]:
        return self.sections


class FailingParser:
    def parse(self, source: Path) -> list[ParsedSection]:
        raise OSError("parser backend unavailable")


def test_flat_sections_have_stable_citable_locators(tmp_path: Path) -> None:
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"multi-section fixture")
    version = hashlib.sha256(source.read_bytes()).hexdigest()

    units = parse_structure_units(
        document_id="TEST-2026",
        version=version,
        source=source,
        parser=FakeParser(
            [
                ParsedSection(title="Overview", text="Alpha facts."),
                ParsedSection(title="Risk Factors", text="Beta risks."),
            ]
        ),
    )

    assert [unit.locator.unit for unit in units] == [
        "section-001",
        "section-002",
    ]
    assert units[0].locator == StructureLocator(
        document_id="TEST-2026",
        version=version,
        unit="section-001",
    )
    assert units[0].text == "Overview\nAlpha facts."
    assert units[1].text == "Risk Factors\nBeta risks."


def test_nested_sections_preserve_subset_invariants(tmp_path: Path) -> None:
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"nested fixture")

    units = parse_structure_units(
        document_id="TEST-2026",
        version="v1",
        source=source,
        parser=FakeParser(
            [
                ParsedSection(
                    title="Management Discussion",
                    text="Top-level context.",
                    children=(
                        ParsedSection(
                            title="Liquidity",
                            text="Child discussion.",
                        ),
                    ),
                )
            ]
        ),
    )

    parent, child = units
    assert [unit.locator.unit for unit in units] == [
        "section-001",
        "section-001/section-001",
    ]
    assert child.parent_unit == parent.locator.unit
    assert child.text in parent.text
    assert "Liquidity\nChild discussion." in parent.text
    assert parent.text in (
        "Management Discussion\nTop-level context.\n"
        "Liquidity\nChild discussion."
    )


def test_pdf_parser_uses_existing_pdf_extraction_for_sections(
    tmp_path: Path,
) -> None:
    source = tmp_path / "multi-section.pdf"
    content = b"BT /F1 14 Tf 72 720 Td (1. Overview) Tj 0 -24 Td (Revenue increased.) Tj 0 -24 Td (2. Risk Factors) Tj 0 -24 Td (Supply chain risk.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    source.write_bytes(output)

    units = parse_structure_units(
        document_id="PDF-2026",
        version=hashlib.sha256(output).hexdigest(),
        source=source,
    )

    assert [unit.title for unit in units] == [
        "1. Overview",
        "2. Risk Factors",
    ]
    assert "Revenue increased" in units[0].text
    assert "Supply chain risk" in units[1].text


def test_parser_failure_and_unit_overflow_are_typed(tmp_path: Path) -> None:
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"fixture")

    with pytest.raises(StructureParseError, match="parser backend unavailable"):
        parse_structure_units(
            document_id="TEST-2026",
            version="v1",
            source=source,
            parser=FailingParser(),
        )

    with pytest.raises(StructureUnitLimitError, match="maximum"):
        parse_structure_units(
            document_id="TEST-2026",
            version="v1",
            source=source,
            parser=FakeParser(
                [
                    ParsedSection(title="One", text="one"),
                    ParsedSection(title="Two", text="two"),
                ]
            ),
            max_units=1,
        )


def test_unknown_form_filter_remains_empty_catalog_result(
    tmp_path: Path,
) -> None:
    document_id = "TEST-2026"
    version = "a" * 64
    extracted_root = tmp_path / "extracted" / document_id
    extracted_root.mkdir(parents=True)
    (extracted_root / "page-001.json").write_text(
        json.dumps(
            {
                "document_id": document_id,
                "version": version,
                "page": 1,
                "text": "fixture",
            }
        )
    )
    (extracted_root / "passages-001.json").write_text(
        json.dumps({"passages": []})
    )
    (extracted_root.parent / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    document_id: {"version": version, "page_count": 1}
                },
            }
        )
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": document_id,
                        "company": "TEST",
                        "period": "2026 Q1",
                        "sha256": version,
                        "form": "10-Q",
                    }
                ]
            }
        )
    )

    assert list_files(
        form="8-K",
        manifest_path=manifest_path,
        extracted_root=extracted_root.parent,
    ) == {"filings": []}
