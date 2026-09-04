"""Corpus fixtures shared by the corpus and host test packages.

``multi_form_corpus`` is the "test corpus includes those forms" half of
spec SC3. The committed corpus is 10-Q only — that is what makes the
SC3 negative case (``form=10-K`` / ``form=8-K`` -> ``{"filings": []}``)
meaningful — so the positive case needs its own catalog.

The fixture builds a catalog metadata table through
``fin.corpus.catalog.record_filings`` — the same seam the ingest
endpoint (T3) uses — so tests exercise the real row contract
``{document_id, company, period, version, page_count}`` rather than a
hand-rolled manifest.

It yields ``{"catalog_path": ...}`` so callers can splat it straight
into ``list_files`` / ``list_filings``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fin.corpus.catalog import record_filings


# One row per form the operator should be able to see in the library.
_MULTI_FORM_ROWS: tuple[tuple[str, str, str], ...] = (
    ("ZEUS-2023-10K", "10-K", "2023 FY"),
    ("ZEUS-2023-8K", "8-K", "2023-05-04"),
    ("ZEUS-2023Q1", "10-Q", "2023 Q1"),
)


@pytest.fixture
def multi_form_corpus(tmp_path: Path) -> dict[str, Path]:
    """A catalog of one 10-K, one 8-K and one 10-Q for the same issuer."""
    catalog_path = tmp_path / "catalog.json"
    rows = [
        {
            "document_id": document_id,
            "company": "ZEUS",
            "period": period,
            "version": str(index + 1) * 64,
            "page_count": 1,
            "form": form,
            "cik": "0000000042",
            "ticker": "ZEUS",
            "filing_date": "2023-05-04",
        }
        for index, (document_id, form, period) in enumerate(_MULTI_FORM_ROWS)
    ]
    record_filings(rows, catalog_path=catalog_path)

    return {"catalog_path": catalog_path}
