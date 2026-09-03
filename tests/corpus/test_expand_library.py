"""Acceptance tests for the SC6 SEC-shaped library expansion.

The committed 10-Q catalog remains intact while one small, fixture-only
10-K from another issuer proves that catalog consumers do not assume one
form forever.
"""

from __future__ import annotations

from pathlib import Path

from fin.corpus.catalog import list_filings


_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "corpus"
_MANIFEST = _DATA_DIR / "manifest.json"
_EXTRACTED = _DATA_DIR / "extracted"


def test_catalog_exposes_extra_issuer_with_truthful_form_and_period() -> None:
    """The extra fixture is a GOOG 2023 FY 10-K, not another 10-Q."""
    result = list_filings(
        ticker="GOOG",
        form="10-K",
        manifest_path=_MANIFEST,
        extracted_root=_EXTRACTED,
    )

    assert result["filings"] == [
        {
            "document_id": "GOOG-2023FY",
            "company": "GOOG",
            "period": "2023 FY",
            "version": "6bbfa3c053cdd837d905cabbdc2d42d8ea367d2e5d0a5214f5d9ca236f4d706e",
            "page_count": 1,
            "form": "10-K",
        }
    ]


def test_catalog_expansion_keeps_existing_10q_rows() -> None:
    """SC6 is additive: the existing 10-Q rows plus one 10-K."""
    ten_q = list_filings(
        form="10-Q", manifest_path=_MANIFEST, extracted_root=_EXTRACTED
    )
    all_filings = list_filings(
        manifest_path=_MANIFEST, extracted_root=_EXTRACTED
    )

    assert len(ten_q["filings"]) == 20
    assert len(all_filings["filings"]) == 21
    assert {filing["form"] for filing in all_filings["filings"]} == {
        "10-Q",
        "10-K",
    }
