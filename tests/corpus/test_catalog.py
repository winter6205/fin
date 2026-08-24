"""Boundary tests for the catalog's EDGAR-shaped optional filters (T2 / ADR 0031).

The catalog (``fin.corpus.catalog.list_filings``) gained additive
optional kwargs (``cik`` / ``form`` / ``ticker`` / ``filing_date``)
alongside the v1 ``company`` / ``period`` filters. These tests cover
the five boundary classes from the defensive-contract-validator axis:
empty, negative, overflow, concurrent, exception. Together they lock
the contract that the host graph's ``list_files`` tool and the
retrieve port share.

Filter semantics under test (per ADR 0031 / spec):

* Empty filter values (e.g. ``cik=""``) MUST behave as empty hits —
  NOT "match anything" and NOT an exception. The spec calls this out
  explicitly under the "empty hits not exceptions" rule.
* Unknown ``cik`` / ``ticker`` / ``form`` / ``filing_date`` MUST
  return ``{"filings": []}``.
* Conflicting filters (``ticker="AAPL"`` + ``cik="0000789019"``
  which is MSFT's CIK) MUST return ``{"filings": []}`` — the
  filters are AND-combined, so a row that passes one but fails the
  other is dropped.
* The output hit shape stays ``{document_id, company, period,
  version, page_count}`` — the new EDGAR-shaped fields are
  filter inputs only, NOT emitted in the output until a later
  ticket extends the output keys.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from fin.corpus.errors import IngestMissingError
from fin.host.tools.list_files import list_files


# ---------- paths ----------


def _project_data_dir() -> Path:
    """Project's committed ``data/corpus`` directory."""
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _manifest_path() -> Path:
    return _project_data_dir() / "manifest.json"


def _extracted_root() -> Path:
    return _project_data_dir() / "extracted"


# Real EDGAR-shaped values from the committed manifest. Anchored here
# so a manifest edit that breaks the assumption surfaces as a
# well-named fixture failure, not a misleading test result.
AAPL_CIK = "0000320193"
MSFT_CIK = "0000789019"
AMZN_CIK = "0001018724"


# ===== boundary 1: empty =====


@pytest.mark.parametrize(
    "field",
    ["cik", "form", "ticker", "filing_date"],
)
def test_empty_filter_value_returns_empty_hits(field: str) -> None:
    """Empty string filter (e.g. ``cik=""``) MUST return ``{"filings": []}``.

    Per ADR 0031 / spec: "Empty hits not exceptions". An empty filter
    value is NOT "match anything" and NOT an exception — it is a
    well-formed call that simply has no row matching it.
    """
    result = list_files(
        **{field: ""},
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}, (
        f"empty {field}='' must yield empty hits (per ADR 0031); "
        f"got {result!r}"
    )


# ===== boundary 2: negative =====


def test_unknown_cik_returns_empty_hits() -> None:
    """Unknown ``cik`` returns ``{"filings": []}`` — NOT an error."""
    result = list_files(
        cik="9999999999",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}


def test_unknown_ticker_returns_empty_hits() -> None:
    """Unknown ``ticker`` returns ``{"filings": []}``."""
    result = list_files(
        ticker="ZZZZ",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}


def test_unknown_form_returns_empty_hits() -> None:
    """Unknown ``form`` returns ``{"filings": []}``.

    Per spec acceptance: ``form`` is NOT hard-coded to demo 10-Q when
    other form rows exist. In the demo corpus every row is 10-Q, so
    a non-10-Q form (e.g. ``10-K``) returns the empty contract.
    """
    result = list_files(
        form="10-K",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}


def test_unknown_filing_date_returns_empty_hits() -> None:
    """Unknown exact ``filing_date`` returns ``{"filings": []}``."""
    result = list_files(
        filing_date="2099-12-31",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}


def test_conflicting_ticker_and_cik_returns_empty_hits() -> None:
    """Conflicting ``ticker``+``cik`` returns ``{"filings": []}``.

    AAPL's ticker (``AAPL``) paired with MSFT's CIK
    (``0000789019``) — no row satisfies both. Filters are
    AND-combined, so a row that passes one and fails the other is
    dropped; the empty hits path is the contract, NOT an exception.
    """
    result = list_files(
        ticker="AAPL",
        cik=MSFT_CIK,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result == {"filings": []}, (
        f"conflicting ticker=MSFT cik={AAPL_CIK}? filters must yield "
        f"empty hits; got {result!r}"
    )


def test_matching_ticker_and_cik_returns_hits() -> None:
    """Matching ``ticker``+``cik`` returns the AND-combined hits.

    AAPL's ticker (``AAPL``) paired with AAPL's CIK
    (``0000320193``) — exactly 4 rows in the demo corpus
    (one per period). The AND of two filters that agree on the
    same ticker must equal the single-filter result.
    """
    result = list_files(
        ticker="AAPL",
        cik=AAPL_CIK,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 4
    for filing in result["filings"]:
        assert filing["company"] == "AAPL"


def test_cik_filter_alone_returns_one_issuer_hits() -> None:
    """``cik`` alone narrows to the issuer's filings (4 rows)."""
    result = list_files(
        cik=AAPL_CIK,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 4
    for filing in result["filings"]:
        assert filing["company"] == "AAPL"


def test_form_filter_alone_returns_all_10Q_hits() -> None:
    """``form="10-Q"`` returns all 12 demo filings (every row is 10-Q)."""
    result = list_files(
        form="10-Q",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 12


def test_filing_date_filter_alone_returns_single_hit() -> None:
    """Exact ``filing_date`` returns exactly the one matching row."""
    # AAPL-2022Q3's filing_date is 2022-08-05 per the manifest.
    result = list_files(
        filing_date="2022-08-05",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 1
    assert result["filings"][0]["document_id"] == "AAPL-2022Q3"


def test_company_and_cik_filters_are_and_combined() -> None:
    """``company`` + ``cik`` agree on AAPL → 4 hits; cross-issuer → empty."""
    matching = list_files(
        company="AAPL",
        cik=AAPL_CIK,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(matching["filings"]) == 4

    cross = list_files(
        company="AAPL",
        cik=MSFT_CIK,
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert cross == {"filings": []}


# ===== boundary 3: overflow (large manifest round-trip) =====


def test_default_returns_all_12_filings() -> None:
    """No filters → all 12 demo filings, in deterministic document_id order.

    Boundary "overflow" stand-in for catalog: the catalog has no
    internal cap, but a large manifest must still round-trip without
    dropping rows.
    """
    result = list_files(
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 12
    document_ids = [f["document_id"] for f in result["filings"]]
    assert document_ids == sorted(document_ids)


# ===== boundary 4: concurrent (parallel catalog reads) =====


def test_concurrent_list_files_calls_are_safe() -> None:
    """Two threads call ``list_files`` concurrently with overlapping
    filters; both return the same correct shape, no deadlock, no
    exception.

    The catalog is a read-only pure function over the committed
    manifest and extracted tree, so concurrent calls must be safe
    by construction. We verify that the safety stays safe.
    """
    def _call() -> dict:
        return list_files(
            ticker="AAPL",
            cik=AAPL_CIK,
            manifest_path=_manifest_path(),
            extracted_root=_extracted_root(),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _call(), range(8)))

    for i, result in enumerate(results):
        assert result == {"filings": results[0]["filings"]}, (
            f"call #{i} returned {result!r}, expected the same shape "
            f"as call #0 {results[0]!r}"
        )


# ===== boundary 5: exception =====


def test_missing_manifest_raises_typed_error() -> None:
    """Non-existent ``manifest_path`` raises ``IngestMissingError``."""
    bogus = Path("/nonexistent/manifest.json")
    with pytest.raises(IngestMissingError):
        list_files(
            manifest_path=bogus,
            extracted_root=_extracted_root(),
        )


def test_missing_extracted_root_raises_typed_error() -> None:
    """Non-existent ``extracted_root`` raises ``IngestMissingError``."""
    bogus = Path("/nonexistent/extracted-root")
    with pytest.raises(IngestMissingError):
        list_files(
            manifest_path=_manifest_path(),
            extracted_root=bogus,
        )


# ===== v1-shape preservation =====


_V1_HIT_KEYS = {
    "document_id",
    "company",
    "period",
    "version",
    "page_count",
}


def test_list_files_aapl_preserves_v1_shape() -> None:
    """``list_files(company="AAPL")`` returns the v1 hit shape only.

    The new EDGAR-shaped fields (``cik``, ``accession``,
    ``filing_date``, ``ticker``, ``form``) MUST NOT appear in the
    output dict until a later ticket explicitly extends the output
    keys. They are filter inputs only in T2.
    """
    result = list_files(
        company="AAPL",
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert result["filings"], "expected at least one AAPL hit"
    for filing in result["filings"]:
        assert set(filing.keys()) == _V1_HIT_KEYS, (
            f"output hit must stay v1-shaped; got {set(filing.keys())}. "
            "T2 is filter-only, not output-shape extension."
        )
        # And explicitly forbid the EDGAR-shaped fields.
        for forbidden in ("cik", "accession", "filing_date", "ticker", "form"):
            assert forbidden not in filing, (
                f"{forbidden!r} leaked into the v1 hit shape; T2 is "
                "filter-only, output-shape change belongs to a later "
                "ticket"
            )


def test_list_files_default_returns_all_12_with_v1_shape() -> None:
    """``list_files()`` returns 12 filings each with the v1 hit shape."""
    result = list_files(
        manifest_path=_manifest_path(),
        extracted_root=_extracted_root(),
    )
    assert len(result["filings"]) == 12
    for filing in result["filings"]:
        assert set(filing.keys()) == _V1_HIT_KEYS


# ===== synthetic manifest: form rows other than 10-Q =====


def test_list_files_form_filter_accepts_non_demo_form(tmp_path: Path) -> None:
    """A synthetic manifest with 10-K rows: ``form='10-K'`` returns
    only 10-K rows, ``form='10-Q'`` returns only 10-Q rows.

    Per spec acceptance: ``form`` is NOT hard-coded to the demo
    10-Q. We assert the filter accepts other values when rows for
    those values exist.
    """
    version = "a" * 64
    documents: list[dict] = []
    for doc_id, form in (
        ("TEST-10K-2023Q1", "10-K"),
        ("TEST-10Q-2023Q1", "10-Q"),
    ):
        documents.append(
            {
                "document_id": doc_id,
                "source_id": "TEST",
                "company": "TEST",
                "ticker": "TEST",
                "cik": "0000000001",
                "accession": "0000000001-23-000001",
                "filing_date": "2023-02-01",
                "form": form,
                "period": "2023 Q1",
                "doc_type": form,
                "filename": f"{doc_id}.pdf",
                "upstream_name": f"{doc_id}.pdf",
                "bytes": 0,
                "sha256": version,
            }
        )

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "upstream": {"repo": "test", "commit": "test", "path": "test", "license": "test"},
                "tickers": ["TEST"],
                "periods": ["2023 Q1"],
                "note": "synthetic test fixture with mixed forms",
                "documents": documents,
            }
        )
    )

    extracted_root = tmp_path / "extracted"
    extracted_root.mkdir()
    for doc in documents:
        doc_dir = extracted_root / doc["document_id"]
        doc_dir.mkdir()
        text = f"{doc['document_id']} page 1 body"
        (doc_dir / "page-001.json").write_text(
            json.dumps(
                {
                    "document_id": doc["document_id"],
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
                        {"text": text, "char_start": 0, "char_end": len(text)}
                    ]
                }
            )
        )
    (extracted_root / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    doc["document_id"]: {"page_count": 1, "version": version}
                    for doc in documents
                },
            }
        )
    )

    ten_k = list_files(
        form="10-K",
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    ten_q = list_files(
        form="10-Q",
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    assert len(ten_k["filings"]) == 1
    assert ten_k["filings"][0]["document_id"] == "TEST-10K-2023Q1"
    assert len(ten_q["filings"]) == 1
    assert ten_q["filings"][0]["document_id"] == "TEST-10Q-2023Q1"