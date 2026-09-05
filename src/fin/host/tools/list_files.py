"""Host tool wrapper for ADR 0009's ``list_files`` tool.

Pure function over the catalog metadata table (``data/corpus/
catalog.json``, ADR 0047 — ingested/pushed filings, not a serve-time
extracted-tree SSOT); returns the contract shape documented in ADR
0009, extended with ``form`` per spec SC3 of
``specs/enterprise-kb-agent.md``:

    {"filings": [{"document_id", "company", "period", "version",
                  "page_count", "form"}, ...]}

or ``{"filings": []}`` when the table is empty/missing or filters
match nothing. No LangChain / LangGraph imports on this path; T6
binds the callables to the model via ``bind_tools`` when wiring the
host graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fin.corpus.catalog import list_filings


def list_files(
    company: str | None = None,
    period: str | None = None,
    *,
    cik: str | None = None,
    form: str | None = None,
    ticker: str | None = None,
    filing_date: str | None = None,
    catalog_path: Path | str | None = None,
) -> dict[str, Any]:
    """ADR 0009 ``list_files`` host tool.

    Optional ``company`` / ``period`` filters match the catalog
    row's ``company`` / ``period`` fields exactly (case-sensitive).
    The additive EDGAR-shaped optional filters (``cik``, ``form``,
    ``ticker``, exact ``filing_date``) per ADR 0031 are forwarded
    through to the catalog. Empty filter values, unknown values,
    and conflicting filters all yield ``{"filings": []}`` (no
    exception). Each hit reports its ``form`` (``None`` on a corpus
    row that was never given one). ``page_count`` per filing comes
    from the catalog metadata table row; a missing table file is the
    empty catalog, not a failure (ADR 0047).
    """
    return list_filings(
        company=company,
        period=period,
        cik=cik,
        form=form,
        ticker=ticker,
        filing_date=filing_date,
        catalog_path=catalog_path,
    )


__all__ = ["list_files"]
