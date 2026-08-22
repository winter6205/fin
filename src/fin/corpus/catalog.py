"""Read-side catalog for the v1 agent's ``list_files`` tool.

Pure function over the committed corpus manifest and the extracted
tree. No LangChain / LangGraph imports on this path. Returns the
ADR 0009 contract shape (``{"filings": [...]}`` or
``{"filings": []}``).

The ``page_count`` value per filing comes from the committed extracted
tree via ``load_extracted_tree``; if that tree is missing or
incomplete, this function raises the typed ``IngestMissingError`` so
the host graph (T6) can fail fast at boot (ADR 0020).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fin.corpus.extracted import load_extracted_tree


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"


def list_filings(
    company: str | None = None,
    period: str | None = None,
    *,
    manifest_path: Path | None = None,
    extracted_root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the ADR 0009 catalog shape for the frozen 10-Qs.

    Each hit is ``{document_id, company, period, version, page_count}``.
    Filings are sorted by ``document_id`` for deterministic output.
    Unknown filters yield ``{"filings": []}``.
    """
    if manifest_path is None:
        manifest_path = _DEFAULT_MANIFEST_PATH
    if extracted_root is None:
        extracted_root = _DEFAULT_EXTRACTED_ROOT

    payload = json.loads(manifest_path.read_text())
    documents = payload["documents"]

    pages_by_doc = load_extracted_tree(manifest_path, extracted_root)

    filings: list[dict[str, Any]] = []
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        if company is not None and doc["company"] != company:
            continue
        if period is not None and doc["period"] != period:
            continue
        doc_id = doc["document_id"]
        filings.append(
            {
                "document_id": doc_id,
                "company": doc["company"],
                "period": doc["period"],
                "version": doc["sha256"],
                "page_count": len(pages_by_doc[doc_id]),
            }
        )
    return {"filings": filings}


__all__ = ["list_filings"]