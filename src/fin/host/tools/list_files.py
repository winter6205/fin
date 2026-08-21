"""Host tool wrapper for ADR 0009's ``list_files`` tool.

Pure function over the committed corpus manifest and extracted tree;
returns the contract shape documented in ADR 0009:

    {"filings": [{"document_id", "company", "period", "version",
                  "page_count"}, ...]}

or ``{"filings": []}`` when filters match nothing. No LangChain /
LangGraph imports on this path; T6 binds the callables to the model
via ``bind_tools`` when wiring the host graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fin.corpus.catalog import list_filings


def list_files(
    company: str | None = None,
    period: str | None = None,
    *,
    manifest_path: Path | None = None,
    extracted_root: Path | None = None,
) -> dict[str, Any]:
    """ADR 0009 ``list_files`` host tool.

    Optional ``company`` / ``period`` filters match the manifest's
    ``company`` / ``period`` fields exactly (case-sensitive). Unknown
    filters yield ``{"filings": []}`` (no exception). ``page_count``
    per filing comes from the committed extracted tree; if that tree
    is missing, ``IngestMissingError`` propagates so the host graph
    can fail fast at boot (T6, ADR 0020).
    """
    return list_filings(
        company=company,
        period=period,
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )


__all__ = ["list_files"]