"""Boundary tests for the v1 host tools (``list_files``, ``read_source``).

Covers the five boundary classes from the defensive-contract-validator
axis: empty, negative, overflow, concurrent, exception. Together they
exercise the ADR 0009 contract that the host graph and the citation gate
rely on: tools return dicts (not exceptions), and the model can
distinguish a known-unknown ``unknown_locator`` from a tool that
crashed mid-run.

The committed catalog metadata table at ``data/corpus/catalog.json``
(ADR 0047) is the fixture for the ``list_files`` happy-path tests; the
committed extracted tree still backs the ``read_source`` tests, served
through the company leg the graph binds (a retrieve port carrying
``pages_by_doc``). No real LLM is invoked and no backend is contacted;
the synthetic three-unit tree is the fixture for the bounded-cursor
cases, which need a unit longer than the read bound.
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
from pathlib import Path

import pytest

from fin.corpus.catalog import record_filings
from fin.corpus.extracted import ExtractedPage, Locator, load_extracted_tree
from fin.host.tools import list_files
from fin.host.tools.read_source import read_source
from fin.retrieve import FakeRetrievePort
from fin.retrieve.source_read import (
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    serialized_result_bytes,
)


# ----- paths -----


def _project_data_dir() -> Path:
    """Project's committed ``data/corpus`` directory."""
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _manifest_path() -> Path:
    return _project_data_dir() / "manifest.json"


def _extracted_root() -> Path:
    return _project_data_dir() / "extracted"


def _catalog_path() -> Path:
    return _project_data_dir() / "catalog.json"


def _committed_pages() -> dict[str, list[ExtractedPage]]:
    """The committed extracted tree, keyed as the company leg wants it."""
    return {
        document_id: list(pages)
        for document_id, pages in load_extracted_tree(
            _manifest_path(), _extracted_root()
        ).items()
    }


def _company_reader(
    pages_by_doc: dict[str, list[ExtractedPage]] | None = None,
) -> FakeRetrievePort:
    """A company read leg over the committed (or injected) page table.

    The graph serves company reads from the bound adapter's own
    ``read_slice``, so the leg under test here is that same face, driven
    by the committed tree instead of an invented body.
    """
    return FakeRetrievePort(
        pages_by_doc=(
            _committed_pages() if pages_by_doc is None else pages_by_doc
        )
    )


# sha256 of the AAPL 2022-Q3 PDF (manifest SSOT)
AAPL_2022Q3_SHA = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
AAPL_2022Q3_PAGE_COUNT = 28


def _company_ref(document_id: str, version: str, unit: int) -> dict:
    """The typed company triple ``read_source`` routes to the company leg."""
    return {"document_id": document_id, "version": version, "unit": unit}


def _synthetic_pages(
    document_id: str, version: str, texts: list[str]
) -> list[ExtractedPage]:
    """Build one document's frozen unit table in memory.

    The read bound is per unit, so the cursor cases need a unit longer
    than the bound — far too large to commit as a corpus artifact —
    while staying the exact shape the loader hands the leg.
    """
    return [
        ExtractedPage(
            locator=Locator(document_id=document_id, version=version, unit=n),
            text=text,
            passages=(),
        )
        for n, text in enumerate(texts, start=1)
    ]


def _build_synthetic_3unit_tree(
    tmp_path: Path,
    document_id: str = "TEST-2023Q1",
) -> tuple[Path, Path, str]:
    """Build a minimal synthetic manifest + extracted tree for a single
    3-unit document. Returns ``(manifest_path, extracted_root, version)``.
    """
    version = "a" * 64
    extracted_root = tmp_path / "extracted"
    doc_dir = extracted_root / document_id
    doc_dir.mkdir(parents=True)

    for n in range(1, 4):
        page_text = f"PAGE {n} BODY"
        (doc_dir / f"page-{n:03d}.json").write_text(
            json.dumps(
                {
                    "document_id": document_id,
                    "version": version,
                    "page": n,
                    "text": page_text,
                }
            )
        )
        (doc_dir / f"passages-{n:03d}.json").write_text(
            json.dumps(
                {
                    "passages": [
                        {
                            "text": page_text,
                            "char_start": 0,
                            "char_end": len(page_text),
                        }
                    ]
                }
            )
        )

    (extracted_root / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    document_id: {
                        "page_count": 3,
                        "version": version,
                    }
                },
            }
        )
    )

    ticker = document_id.split("-")[0]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "upstream": {
                    "repo": "test",
                    "commit": "test",
                    "path": "test",
                    "license": "test",
                },
                "tickers": [ticker],
                "periods": ["2023 Q1"],
                "note": "synthetic test fixture",
                "documents": [
                    {
                        "document_id": document_id,
                        "source_id": ticker,
                        "company": ticker,
                        "period": "2023 Q1",
                        "doc_type": "10-Q",
                        "filename": "test.pdf",
                        "upstream_name": "test.pdf",
                        "bytes": 0,
                        "sha256": version,
                    }
                ],
            }
        )
    )
    return manifest_path, extracted_root, version


# ===== boundary 1: empty =====


def test_list_files_empty_returns_full_library_list() -> None:
    """``list_files()`` returns every committed filing with the contract
    shape."""
    result = list_files(
        catalog_path=_catalog_path(),
    )
    assert isinstance(result, dict)
    # Exactly one top-level key per ADR 0009.
    assert list(result.keys()) == ["filings"]
    assert len(result["filings"]) >= 21

    sample = result["filings"][0]
    # Contract keys: no ``filename``, ``bytes``, or ``sha256`` (only
    # ``version``). ``form`` joined the shape with spec SC3 of
    # ``specs/enterprise-kb-agent.md``; the rest of the EDGAR-shaped
    # submission header stays filter-only.
    assert set(sample.keys()) == {
        "document_id",
        "company",
        "period",
        "version",
        "page_count",
        "form",
    }
    assert "filename" not in sample
    assert "bytes" not in sample
    assert "sha256" not in sample
    for filter_only in ("cik", "accession", "filing_date", "ticker"):
        assert filter_only not in sample

    # Deterministic order: sorted by document_id.
    document_ids = [f["document_id"] for f in result["filings"]]
    assert document_ids == sorted(document_ids)

    # Every filing has a positive page_count from the committed tree.
    for filing in result["filings"]:
        assert filing["page_count"] > 0


def test_read_source_happy_path_returns_first_unit_slice() -> None:
    """``read_source`` over the committed corpus returns one complete
    bounded slice of the addressed unit and a terminal cursor."""
    result = read_source(
        _company_ref("AAPL-2022Q3", AAPL_2022Q3_SHA, 1),
        0,
        company_reader=_company_reader(),
    )
    assert set(result) == {"source_ref", "text", "next_position"}
    assert result["source_ref"] == _company_ref(
        "AAPL-2022Q3", AAPL_2022Q3_SHA, 1
    )
    tree = load_extracted_tree(_manifest_path(), _extracted_root())
    first_unit_text = tree["AAPL-2022Q3"][0].text
    # Expected text comes from the committed artifact, not from the read
    # seam: this unit fits the bound, so the slice is the whole unit and
    # the cursor is exhausted.
    assert result["text"] == first_unit_text
    assert result["next_position"] is None
    assert len(result["text"]) <= SOURCE_READ_MAX_TEXT_CODE_POINTS


# ===== boundary 2: negative =====


def test_list_files_unknown_company_returns_empty() -> None:
    """Unknown ``company`` filter returns ``{"filings": []}`` (not error)."""
    result = list_files(
        company="FOO",
        catalog_path=_catalog_path(),
    )
    assert result == {"filings": []}


def test_list_files_unknown_period_returns_empty() -> None:
    """Unknown ``period`` filter returns ``{"filings": []}``."""
    result = list_files(
        period="2099 Q4",
        catalog_path=_catalog_path(),
    )
    assert result == {"filings": []}


def test_read_source_unknown_document_id_returns_unknown_locator() -> None:
    """Unknown ``document_id`` returns the ``unknown_locator`` dict."""
    result = read_source(
        _company_ref("BOGUS-2099Q9", AAPL_2022Q3_SHA, 1),
        0,
        company_reader=_company_reader(),
    )
    assert result == {"error": "unknown_locator"}


def test_read_source_version_mismatch_returns_unknown_locator() -> None:
    """A 64-char ``version`` that is not the manifest sha256 returns the
    ``unknown_locator`` dict."""
    result = read_source(
        _company_ref("AAPL-2022Q3", "0" * 64, 1),
        0,
        company_reader=_company_reader(),
    )
    assert result == {"error": "unknown_locator"}


def test_read_source_empty_version_returns_invalid_locator() -> None:
    """Empty ``version`` is not a locator at all: the shape verdict is
    named before any corpus lookup, so it never looks like a miss."""
    result = read_source(
        _company_ref("AAPL-2022Q3", "", 1),
        0,
        company_reader=_company_reader(),
    )
    assert result["error"] == "invalid_locator"
    assert "text" not in result


@pytest.mark.parametrize("bad_unit", [0, -1])
def test_read_source_unit_below_one_returns_invalid_locator(bad_unit: int) -> None:
    """``unit < 1`` is a malformed ordinal, not a coordinate into the
    tree: refused as ``invalid_locator`` whatever the corpus holds."""
    result = read_source(
        _company_ref("AAPL-2022Q3", AAPL_2022Q3_SHA, bad_unit),
        0,
        company_reader=_company_reader(),
    )
    assert result["error"] == "invalid_locator"


def test_read_source_legacy_page_key_is_not_a_locator() -> None:
    """A ref still carrying the physical ``page`` field is a malformed
    locator: the triple has exactly one spelling, so a second ordinal
    name cannot quietly address a unit."""
    result = read_source(
        {
            "document_id": "AAPL-2022Q3",
            "version": AAPL_2022Q3_SHA,
            "page": 1,
        },
        0,
        company_reader=_company_reader(),
    )
    assert result["error"] == "invalid_locator"


def test_read_source_unit_beyond_returns_unknown_locator() -> None:
    """``unit`` past the last unit of the document returns the
    ``unknown_locator`` dict."""
    for bad_unit in (AAPL_2022Q3_PAGE_COUNT + 1, AAPL_2022Q3_PAGE_COUNT + 10):
        result = read_source(
            _company_ref("AAPL-2022Q3", AAPL_2022Q3_SHA, bad_unit),
            0,
            company_reader=_company_reader(),
        )
        assert result == {"error": "unknown_locator"}


# ===== boundary 3: overflow =====


def test_read_source_one_call_never_exceeds_the_bound(tmp_path: Path) -> None:
    """The model owns no window parameter: whatever the unit length, one
    call answers at most the bound in code points and the serialized
    result stays inside the byte cap.
    """
    document_id = "TEST-2023Q1"
    version = "a" * 64
    long_unit = "x" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 500)
    reader = _company_reader({document_id: _synthetic_pages(document_id, version, [long_unit])})

    result = read_source(
        _company_ref(document_id, version, 1),
        0,
        company_reader=reader,
    )
    assert "error" not in result
    assert len(result["text"]) <= SOURCE_READ_MAX_TEXT_CODE_POINTS
    assert serialized_result_bytes(result) <= SOURCE_READ_MAX_RESULT_BYTES
    # A truncated body must hand back a cursor, never a silent cut.
    assert result["next_position"] == len(result["text"])
    assert result["next_position"] is not None


def test_read_source_cursor_reaches_the_whole_unit(tmp_path: Path) -> None:
    """Bounded slices are not a lossy view: following ``next_position``
    until it is ``None`` reconstructs the unit exactly, with strict
    advance at every step.
    """
    document_id = "TEST-2023Q1"
    version = "a" * 64
    long_unit = "".join(
        f"UNIT1 SEGMENT {i:04d} " for i in range(1200)
    )  # > SOURCE_READ_MAX_TEXT_CODE_POINTS, so several reads are needed
    assert len(long_unit) > SOURCE_READ_MAX_TEXT_CODE_POINTS
    reader = _company_reader({document_id: _synthetic_pages(document_id, version, [long_unit])})

    collected: list[str] = []
    position = 0
    for _ in range(10):
        result = read_source(
            _company_ref(document_id, version, 1),
            position,
            company_reader=reader,
        )
        assert "error" not in result
        collected.append(result["text"])
        if result["next_position"] is None:
            break
        # Strict advance: the cursor can never stall and loop forever.
        assert result["next_position"] > position
        position = result["next_position"]
    else:
        pytest.fail("the read cursor did not terminate")

    assert "".join(collected) == long_unit


def test_read_source_resolves_exactly_one_unit(tmp_path: Path) -> None:
    """One call reads one unit: a read never bridges into the next
    unit's text, so a quote can only ever vouch for the unit it names.
    """
    manifest_path, extracted_root, version = _build_synthetic_3unit_tree(
        tmp_path
    )
    reader = _company_reader(
        {
            "TEST-2023Q1": list(
                load_extracted_tree(manifest_path, extracted_root)[
                    "TEST-2023Q1"
                ]
            )
        }
    )

    first = read_source(
        _company_ref("TEST-2023Q1", version, 1), 0, company_reader=reader
    )
    second = read_source(
        _company_ref("TEST-2023Q1", version, 2), 0, company_reader=reader
    )

    assert first["text"] == "PAGE 1 BODY"
    assert "PAGE 2" not in first["text"]
    assert first["next_position"] is None
    assert second["text"] == "PAGE 2 BODY"
    assert "PAGE 1" not in second["text"]
    assert second["source_ref"]["unit"] == 2


# ===== boundary 4: concurrent =====


def test_concurrent_read_source_returns_identical_text() -> None:
    """10 parallel ``read_source`` calls against the same unit return
    byte-identical results. Read-only safety under threading.
    """

    def _call() -> str:
        return json.dumps(
            read_source(
                _company_ref("AAPL-2022Q3", AAPL_2022Q3_SHA, 1),
                0,
                company_reader=_company_reader(),
            ),
            sort_keys=True,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        payloads = list(pool.map(lambda _: _call(), range(10)))

    # All 10 calls must produce byte-identical results.
    assert len(set(payloads)) == 1


# ===== boundary 5: exception =====


def test_read_source_with_no_bodies_is_a_typed_miss_never_a_raise() -> None:
    """A body source that holds nothing converts to the ``unknown_locator``
    dict instead of propagating a typed error to the model (ADR 0009 +
    ADR 0020): the leg is present and wired, the corpus behind it is
    empty, and the answer is still a dict with no text."""

    result = read_source(
        _company_ref("AAPL-2022Q3", AAPL_2022Q3_SHA, 1),
        0,
        company_reader=FakeRetrievePort(pages_by_doc={}),
    )
    assert result == {"error": "unknown_locator"}


# ===== filter sanity (happy-path filter coverage) =====


def test_list_files_with_aapl_filter_returns_7_documents() -> None:
    """``list_files(company='AAPL')`` returns every AAPL filing.

    Plan C added 3 EDGAR iXBRL AAPL primary docs (CIK 0000320193)
    on top of the 4 frozen private fixture source PDFs, so AAPL now has 7 filings
    rather than 4.
    """
    result = list_files(
        company="AAPL",
        catalog_path=_catalog_path(),
    )
    assert len(result["filings"]) == 7
    for filing in result["filings"]:
        assert filing["company"] == "AAPL"


def test_list_files_with_period_filter_returns_3_documents() -> None:
    """``list_files(period='2023 Q1')`` returns 3 cross-company docs."""
    result = list_files(
        period="2023 Q1",
        catalog_path=_catalog_path(),
    )
    assert len(result["filings"]) == 3
    for filing in result["filings"]:
        assert filing["period"] == "2023 Q1"


def test_list_files_with_combined_filter_returns_1_document() -> None:
    """``list_files(company='AAPL', period='2023 Q1')`` returns the 1 AAPL Q1 doc."""
    result = list_files(
        company="AAPL",
        period="2023 Q1",
        catalog_path=_catalog_path(),
    )
    assert len(result["filings"]) == 1
    assert result["filings"][0]["document_id"] == "AAPL-2023Q1"


def test_list_files_with_synthetic_3page_row_reports_page_count(
    tmp_path: Path,
) -> None:
    """``list_files`` over a synthetic catalog row reports ``page_count=3``.

    The page_count comes from the catalog metadata table row (ADR
    0047) — the extracted tree is not consulted on the catalog path.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "TEST-2023Q1",
                "title": "Test filing",
                "doc_type": "narrative",
                "company": "TEST",
                "period": "2023 Q1",
                "version": "a" * 64,
                "page_count": 3,
            }
        ],
        catalog_path=catalog_path,
    )

    result = list_files(catalog_path=catalog_path)
    assert len(result["filings"]) == 1
    assert result["filings"][0]["page_count"] == 3
    assert result["filings"][0]["document_id"] == "TEST-2023Q1"


# ===== pdfplumber absence =====


def _assert_no_pdfplumber(source_path: Path) -> None:
    """AST scan to ensure ``source_path`` does not import ``pdfplumber``."""
    tree = ast.parse(source_path.read_text())
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "pdfplumber":
                    offending.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "pdfplumber":
                offending.append(
                    f"line {node.lineno}: from {node.module} import ..."
                )
    assert offending == [], (
        f"{source_path.name} must not import pdfplumber (ADR 0020); "
        f"offending: {offending}"
    )


def test_host_tools_do_not_import_pdfplumber() -> None:
    """The host tools must not import ``pdfplumber`` (ADR 0020)."""
    tools_dir = (
        Path(__file__).resolve().parents[2] / "src" / "fin" / "host" / "tools"
    )
    for sub in ("list_files.py", "read_source.py"):
        _assert_no_pdfplumber(tools_dir / sub)


def test_corpus_catalog_does_not_import_pdfplumber() -> None:
    """The corpus catalog must not import ``pdfplumber`` (ADR 0020)."""
    catalog = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "fin"
        / "corpus"
        / "catalog.py"
    )
    _assert_no_pdfplumber(catalog)