"""T6 analysis-depth contracts: redline, tables, and statements.

The fixtures are local extracted pages and fact sidecars. No SEC or
LLM call is involved. These tests intentionally pin the structured
envelopes instead of accepting a prose-only answer.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, write_facts
from fin.host.tools.redline import redline
from fin.host.tools.statements import three_year_statement
from fin.host.tools.table_extract import extract_tables


def _write_extracted_fixture(tmp_path: Path) -> dict[str, str | Path]:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    docs = {
        "ACME-2022Q3": (
            "Revenue increased steadily.\n\n"
            "Operating margin was 10%.\n\n"
            "Cash flow was positive.",
            "a" * 64,
        ),
        "ACME-2023Q3": (
            "Revenue increased rapidly.\n\n"
            "Operating margin was 12%.\n\n"
            "Cash flow was positive.\n\n"
            "New disclosure paragraph.",
            "b" * 64,
        ),
        "ACME-TABLE-2023Q3": (
            "CONSOLIDATED RESULTS\n\n"
            "| Metric | 2023 | 2022 |\n"
            "| --- | ---: | ---: |\n"
            "| Revenue | 100 | 90 |\n"
            "| Margin | 20% | 18% |\n",
            "c" * 64,
        ),
    }
    for document_id, (text, version) in docs.items():
        doc_dir = extracted / document_id
        doc_dir.mkdir()
        (doc_dir / "page-001.json").write_text(
            json.dumps(
                {
                    "document_id": document_id,
                    "version": version,
                    "page": 1,
                    "text": text,
                }
            )
        )
        (doc_dir / "passages-001.json").write_text(
            json.dumps(
                {
                    "passages": [
                        {
                            "text": text,
                            "char_start": 0,
                            "char_end": len(text),
                        }
                    ]
                }
            )
        )
    (extracted / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    document_id: {"page_count": 1, "version": version}
                    for document_id, (_, version) in docs.items()
                },
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": document_id,
                        "sha256": version,
                        "company": "ACME",
                        "period": "2023 Q3",
                    }
                    for document_id, (_, version) in docs.items()
                ]
            }
        )
    )
    return {
        "manifest": manifest,
        "extracted": extracted,
        "before_version": docs["ACME-2022Q3"][1],
        "after_version": docs["ACME-2023Q3"][1],
        "table_version": docs["ACME-TABLE-2023Q3"][1],
    }


def _write_statement_facts(tmp_path: Path) -> Path:
    root = tmp_path / "facts"
    rows = (
        ("ACME-2021FY", "2021-FY", 100),
        ("ACME-2022FY", "2022-FY", 120),
        ("ACME-2023FY", "2023-FY", 150),
    )
    for document_id, period, revenue in rows:
        write_facts(
            document_id=document_id,
            facts=[
                Fact(
                    concept="us-gaap:Revenues",
                    value=revenue,
                    period=period,
                    unit="USD",
                    scale="millions",
                    source_locator=Locator(
                        document_id=document_id, version="d" * 64, unit=4
                    ),
                ),
                Fact(
                    concept="us-gaap:OperatingIncomeLoss",
                    value=revenue // 5,
                    period=period,
                    unit="USD",
                    scale="millions",
                    source_locator=Locator(
                        document_id=document_id, version="d" * 64, unit=4
                    ),
                ),
            ],
            facts_root=root,
        )
    return root


def test_redline_returns_paragraph_changes_with_source_locators(tmp_path: Path) -> None:
    fixture = _write_extracted_fixture(tmp_path)

    result = redline(
        before_document_id="ACME-2022Q3",
        before_version=fixture["before_version"],
        after_document_id="ACME-2023Q3",
        after_version=fixture["after_version"],
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )

    payload = result["redline"]
    assert payload["schema"] == "fin.redline.v1"
    assert [change["status"] for change in payload["changes"]] == [
        "changed",
        "changed",
        "unchanged",
        "added",
    ]
    assert payload["changes"][0]["before"] == "Revenue increased steadily."
    assert payload["changes"][0]["after"] == "Revenue increased rapidly."
    assert payload["changes"][0]["before_locator"] == {
        "document_id": "ACME-2022Q3",
        "version": "a" * 64,
        "unit": 1,
    }
    assert payload["changes"][-1]["after_locator"]["document_id"] == "ACME-2023Q3"
    assert json.loads(json.dumps(result)) == result


def test_redline_unknown_locator_is_typed_and_never_prose(tmp_path: Path) -> None:
    fixture = _write_extracted_fixture(tmp_path)

    result = redline(
        before_document_id="ACME-2022Q3",
        before_version="0" * 64,
        after_document_id="ACME-2023Q3",
        after_version=fixture["after_version"],
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )

    assert result["error"] == "unknown_locator"
    assert result["redline"]["changes"] == []


def test_redline_malformed_tree_is_typed_exception_exit(tmp_path: Path) -> None:
    fixture = _write_extracted_fixture(tmp_path)
    (fixture["extracted"] / "ACME-2023Q3" / "page-001.json").write_text("{")

    result = redline(
        before_document_id="ACME-2022Q3",
        before_version=fixture["before_version"],
        after_document_id="ACME-2023Q3",
        after_version=fixture["after_version"],
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )

    assert result["error"] == "analysis_unavailable"
    assert result["redline"]["changes"] == []


def test_table_extract_is_bound_to_fixture_unit_locator(tmp_path: Path) -> None:
    fixture = _write_extracted_fixture(tmp_path)

    result = extract_tables(
        document_id="ACME-TABLE-2023Q3",
        version=fixture["table_version"],
        unit=1,
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )

    payload = result["table_extract"]
    assert payload["schema"] == "fin.table_extract.v1"
    assert payload["locator"] == {
        "document_id": "ACME-TABLE-2023Q3",
        "version": "c" * 64,
        "unit": 1,
    }
    assert payload["tables"][0]["headers"] == ["Metric", "2023", "2022"]
    assert payload["tables"][0]["rows"] == [
        ["Revenue", "100", "90"],
        ["Margin", "20%", "18%"],
    ]


def test_table_extract_missing_table_and_bad_locator_are_typed(tmp_path: Path) -> None:
    fixture = _write_extracted_fixture(tmp_path)

    no_table = extract_tables(
        document_id="ACME-2022Q3",
        version=fixture["before_version"],
        unit=1,
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )
    bad_locator = extract_tables(
        document_id="ACME-TABLE-2023Q3",
        version="0" * 64,
        unit=1,
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )
    zero_unit = extract_tables(
        document_id="ACME-TABLE-2023Q3",
        version=fixture["table_version"],
        unit=0,
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )
    unit_past_tree = extract_tables(
        document_id="ACME-TABLE-2023Q3",
        version=fixture["table_version"],
        unit=2,
        manifest_path=fixture["manifest"],
        extracted_root=fixture["extracted"],
    )

    assert no_table["error"] == "table_not_found"
    assert bad_locator["error"] == "unknown_locator"
    assert bad_locator["table_extract"]["tables"] == []
    # A zero ordinal and one past the frozen tree are locator misses,
    # never a table miss: an out-of-range read must not look like a
    # unit that exists but has no table.
    assert zero_unit["error"] == "unknown_locator"
    assert unit_past_tree["error"] == "unknown_locator"
    assert unit_past_tree["table_extract"]["locator"]["unit"] == 2


def test_table_extract_concurrent_reads_are_identical(tmp_path: Path) -> None:
    fixture = _write_extracted_fixture(tmp_path)

    def run() -> str:
        return json.dumps(
            extract_tables(
                document_id="ACME-TABLE-2023Q3",
                version=fixture["table_version"],
                unit=1,
                manifest_path=fixture["manifest"],
                extracted_root=fixture["extracted"],
            ),
            sort_keys=True,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(lambda _: run(), range(8)))
    assert len(set(payloads)) == 1


def test_three_year_statement_renders_fact_values_and_locators(tmp_path: Path) -> None:
    facts_root = _write_statement_facts(tmp_path)

    result = three_year_statement(
        issuer="ACME",
        periods=["2021-FY", "2022-FY", "2023-FY"],
        concepts=["us-gaap:Revenues", "us-gaap:OperatingIncomeLoss"],
        facts_root=facts_root,
    )

    payload = result["statement"]
    assert payload["schema"] == "fin.statement.v1"
    assert payload["kind"] == "three_year"
    assert [cell["value"] for cell in payload["rows"][0]["cells"]] == [
        100,
        120,
        150,
    ]
    assert payload["rows"][0]["cells"][2]["locator"]["document_id"] == "ACME-2023FY"
    assert payload["rows"][0]["cells"][2]["locator"]["unit"] == 4
    assert "deltas" not in payload
    assert json.loads(json.dumps(result)) == result


def test_three_year_statement_requires_exactly_three_periods(tmp_path: Path) -> None:
    facts_root = _write_statement_facts(tmp_path)

    result = three_year_statement(
        issuer="ACME",
        periods=["2022-FY", "2023-FY"],
        concepts=["us-gaap:Revenues"],
        facts_root=facts_root,
    )

    assert result["error"] == "statement_requires_three_years"
    assert result["statement"]["rows"] == []


def test_three_year_statement_unknown_issuer_keeps_missing_rows(tmp_path: Path) -> None:
    facts_root = _write_statement_facts(tmp_path)

    result = three_year_statement(
        issuer="UNKNOWN",
        periods=["2021-FY", "2022-FY", "2023-FY"],
        concepts=["us-gaap:Revenues"],
        facts_root=facts_root,
    )

    row = result["statement"]["rows"][0]
    assert [cell["status"] for cell in row["cells"]] == ["missing"] * 3
    assert result["statement"]["missing"][0]["reason"] == "fact_not_found"


def test_three_year_statement_overflow_is_typed(tmp_path: Path) -> None:
    facts_root = _write_statement_facts(tmp_path)

    result = three_year_statement(
        issuer="ACME",
        periods=["2021-FY", "2022-FY", "2023-FY"],
        concepts=[f"us-gaap:Concept{n}" for n in range(30)],
        facts_root=facts_root,
    )

    assert result["error"] == "statement_overflow"
    assert result["requested"] == 90
    assert result["statement"]["rows"] == []


def test_three_year_statement_malformed_fact_file_degrades_to_missing(
    tmp_path: Path,
) -> None:
    facts_root = _write_statement_facts(tmp_path)
    (facts_root / "ACME-2022FY.json").write_text("{")

    result = three_year_statement(
        issuer="ACME",
        periods=["2021-FY", "2022-FY", "2023-FY"],
        concepts=["us-gaap:Revenues"],
        facts_root=facts_root,
    )

    cells = result["statement"]["rows"][0]["cells"]
    assert [cell["status"] for cell in cells] == ["hit", "missing", "hit"]
