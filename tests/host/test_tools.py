"""Boundary tests for the v1 host tools (``list_files``, ``read_file``).

Covers the five boundary classes from the defensive-contract-validator
axis: empty, negative, overflow, concurrent, exception. Together they
exercise the ADR 0009 contract that T6's host graph and T5's citation
gate rely on: tools return dicts (not exceptions), and the model can
distinguish a known-unknown ``unknown_locator`` from a tool that
crashed mid-run.

The committed extracted tree at ``data/corpus/extracted/`` (T2) is the
fixture for every happy-path test. No real LLM is invoked; no corpus
is mocked. The one exception test monkey-patches
``load_extracted_tree`` to verify the typed-error translation rule.
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
from pathlib import Path

import pytest

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import load_extracted_tree
from fin.host.tools import list_files, read_file

# The read_file module is imported via importlib because the package's
# ``__init__`` rebinds ``fin.host.tools.read_file`` to the callable,
# shadowing the module attribute lookup that ``monkeypatch`` needs.
import importlib
read_file_mod = importlib.import_module("fin.host.tools.read_file")


# ----- paths -----


def _project_data_dir() -> Path:
    """Project's committed ``data/corpus`` directory."""
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _manifest_path() -> Path:
    return _project_data_dir() / "manifest.json"


def _extracted_root() -> Path:
    return _project_data_dir() / "extracted"


# sha256 of the AAPL 2022-Q3 PDF (manifest SSOT)
AAPL_2022Q3_SHA = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
AAPL_2022Q3_PAGE_COUNT = 28


# ----- synthetic 3-page fixture builder -----


def _build_synthetic_3page_tree(
    tmp_path: Path,
    document_id: str = "TEST-2023Q1",
) -> tuple[Path, Path, str]:
    """Build a minimal synthetic manifest + extracted tree for a single
    3-page document. Returns ``(manifest_path, extracted_root, version)``.
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


def test_list_files_empty_returns_full_12_doc_list() -> None:
    """``list_files()`` returns every committed filing with the contract
    shape. Plan C expanded the tier-1 corpus via 8 EDGAR iXBRL primary
    docs, so the floor is now >= 20 (12 frozen private fixture source PDFs + 8
    live-fetched iXBRL HTMLs), not exactly 12."""
    result = list_files(
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert isinstance(result, dict)
    # Exactly one top-level key per ADR 0009.
    assert list(result.keys()) == ["filings"]
    assert len(result["filings"]) >= 20

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


def test_read_file_happy_path_returns_first_page() -> None:
    """``read_file("AAPL-2022Q3", sha, page=1, page_limit=1)`` returns page 1."""
    result = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=1,
        page_limit=1,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert "error" not in result
    assert result["document_id"] == "AAPL-2022Q3"
    assert result["version"] == AAPL_2022Q3_SHA
    assert result["page"] == 1
    assert result["page_count"] == AAPL_2022Q3_PAGE_COUNT
    assert result["text"]  # non-empty


# ===== boundary 2: negative =====


def test_list_files_unknown_company_returns_empty() -> None:
    """Unknown ``company`` filter returns ``{"filings": []}`` (not error)."""
    result = list_files(
        company="FOO",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}


def test_list_files_unknown_period_returns_empty() -> None:
    """Unknown ``period`` filter returns ``{"filings": []}``."""
    result = list_files(
        period="2099 Q4",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}


def test_read_file_unknown_document_id_returns_unknown_locator() -> None:
    """Unknown ``document_id`` returns the ``unknown_locator`` dict."""
    result = read_file(
        "BOGUS-2099Q9",
        AAPL_2022Q3_SHA,
        page=1,
        page_limit=1,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"error": "unknown_locator"}


def test_read_file_version_mismatch_returns_unknown_locator() -> None:
    """Wrong-length / wrong-content ``version`` returns the ``unknown_locator`` dict."""
    # Wrong-length version (still 64 hex chars but not the manifest sha256).
    result = read_file(
        "AAPL-2022Q3",
        "0" * 64,
        page=1,
        page_limit=1,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"error": "unknown_locator"}


def test_read_file_empty_version_returns_unknown_locator() -> None:
    """Empty ``version`` is not a sha256; returns the ``unknown_locator`` dict."""
    result = read_file(
        "AAPL-2022Q3",
        "",
        page=1,
        page_limit=1,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"error": "unknown_locator"}


@pytest.mark.parametrize("bad_page", [0, -1])
def test_read_file_page_below_one_returns_unknown_locator(bad_page: int) -> None:
    """``page < 1`` returns the ``unknown_locator`` dict."""
    result = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=bad_page,
        page_limit=1,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"error": "unknown_locator"}


def test_read_file_page_beyond_returns_unknown_locator() -> None:
    """``page`` past the last page returns the ``unknown_locator`` dict."""
    for bad_page in (AAPL_2022Q3_PAGE_COUNT + 1, AAPL_2022Q3_PAGE_COUNT + 10):
        result = read_file(
            "AAPL-2022Q3",
            AAPL_2022Q3_SHA,
            page=bad_page,
            page_limit=1,
            manifest_path=_manifest_path(),
            extracted_root=_extracted_root(),
        )
        assert result == {"error": "unknown_locator"}


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_read_file_page_limit_below_one_returns_unknown_locator(
    bad_limit: int,
) -> None:
    """``page_limit < 1`` returns the ``unknown_locator`` dict."""
    result = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=1,
        page_limit=bad_limit,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"error": "unknown_locator"}


def test_read_file_page_limit_above_cap_clamps_to_three() -> None:
    """``page_limit > 3`` is silently clamped to 3 (ADR 0009 cap)."""
    result_limit_10 = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=1,
        page_limit=10,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    result_limit_3 = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=1,
        page_limit=3,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    # Same text as a clean ``page_limit=3`` call.
    assert result_limit_10["text"] == result_limit_3["text"]
    assert "error" not in result_limit_10
    assert result_limit_10["page_count"] == AAPL_2022Q3_PAGE_COUNT


# ===== boundary 3: overflow =====


def test_read_file_three_page_doc_with_page_limit_3_returns_all_three(
    tmp_path: Path,
) -> None:
    """Synthetic 3-page doc + ``page_limit=3`` returns all three pages."""
    manifest_path, extracted_root, version = _build_synthetic_3page_tree(tmp_path)

    result = read_file(
        "TEST-2023Q1",
        version,
        page=1,
        page_limit=3,
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    assert "error" not in result
    assert result["page_count"] == 3
    assert result["text"] == "PAGE 1 BODY\n\nPAGE 2 BODY\n\nPAGE 3 BODY"


def test_read_file_page_beyond_with_page_limit_3_clamps_to_existing_pages() -> None:
    """``page + page_limit - 1 > page_count`` clamps to existing pages."""
    page = 27  # 27 + 3 - 1 = 29 > 28
    result = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=page,
        page_limit=3,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert "error" not in result
    assert result["page"] == page
    assert result["page_count"] == AAPL_2022Q3_PAGE_COUNT

    # Must join pages 27 and 28 only (clamped).
    tree = load_extracted_tree(_manifest_path(), _extracted_root())
    expected_text = "\n\n".join(
        tree["AAPL-2022Q3"][i - 1].text for i in (27, 28)
    )
    assert result["text"] == expected_text


def test_read_file_page2_with_page_limit_2_joins_pages_2_and_3() -> None:
    """``page=2, page_limit=2`` joins pages 2 and 3."""
    result = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=2,
        page_limit=2,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert "error" not in result
    assert result["page"] == 2

    tree = load_extracted_tree(_manifest_path(), _extracted_root())
    expected_text = "\n\n".join(
        tree["AAPL-2022Q3"][i - 1].text for i in (2, 3)
    )
    assert result["text"] == expected_text


# ===== boundary 4: concurrent =====


def test_concurrent_read_file_returns_identical_text() -> None:
    """10 parallel ``read_file`` calls against the same document return
    byte-identical text. Read-only safety under threading.
    """

    def _call() -> str:
        result = read_file(
            "AAPL-2022Q3",
            AAPL_2022Q3_SHA,
            page=1,
            page_limit=1,
            manifest_path=_manifest_path(),
            extracted_root=_extracted_root(),
        )
        return result["text"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        texts = list(pool.map(lambda _: _call(), range(10)))

    # All 10 calls must produce byte-identical text.
    assert len(set(texts)) == 1


# ===== boundary 5: exception =====


def test_read_file_ingest_missing_returns_unknown_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_file`` converts ``IngestMissingError`` from
    ``load_extracted_tree`` into the ``unknown_locator`` dict instead of
    propagating the typed error to the model (ADR 0009 + ADR 0020).
    """

    def _raise_ingest_missing(*args, **kwargs):
        raise IngestMissingError("simulated missing tree")

    monkeypatch.setattr(
        read_file_mod,
        "load_extracted_tree",
        _raise_ingest_missing,
    )

    result = read_file(
        "AAPL-2022Q3",
        AAPL_2022Q3_SHA,
        page=1,
        page_limit=1,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"error": "unknown_locator"}


# ===== filter sanity (happy-path filter coverage) =====


def test_list_files_with_aapl_filter_returns_4_documents() -> None:
    """``list_files(company='AAPL')`` returns every AAPL filing.

    Plan C added 3 EDGAR iXBRL AAPL primary docs (CIK 0000320193)
    on top of the 4 frozen private fixture source PDFs, so AAPL now has 7 filings
    rather than 4.
    """
    result = list_files(
        company="AAPL",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 7
    for filing in result["filings"]:
        assert filing["company"] == "AAPL"


def test_list_files_with_period_filter_returns_3_documents() -> None:
    """``list_files(period='2023 Q1')`` returns 3 cross-company docs."""
    result = list_files(
        period="2023 Q1",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 3
    for filing in result["filings"]:
        assert filing["period"] == "2023 Q1"


def test_list_files_with_combined_filter_returns_1_document() -> None:
    """``list_files(company='AAPL', period='2023 Q1')`` returns the 1 AAPL Q1 doc."""
    result = list_files(
        company="AAPL",
        period="2023 Q1",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 1
    assert result["filings"][0]["document_id"] == "AAPL-2023Q1"


def test_list_files_with_synthetic_3page_tree_reports_page_count(
    tmp_path: Path,
) -> None:
    """``list_files`` over a synthetic 3-page tree reports ``page_count=3``."""
    manifest_path, extracted_root, _ = _build_synthetic_3page_tree(tmp_path)

    result = list_files(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
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
    for sub in ("list_files.py", "read_file.py"):
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