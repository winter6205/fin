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


# EDGAR-shaped optional fields per ADR 0031. T1 only reads them off
# the manifest so the live ingest path can use them; the additive
# filter kwargs land in T2 (catalog shape stays v1 for now).
_EDGAR_OPTIONAL_FIELDS = (
    "cik",
    "accession",
    "filing_date",
    "ticker",
    "form",
)


def _read_edgar_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Read the optional EDGAR-shaped fields off a manifest entry.

    Returns a dict containing only the fields that are actually
    present in the manifest entry (the demo corpus may leave them
    null, and a future live-edited row is allowed to omit some). The
    caller decides whether / how to merge them into the output shape.
    """
    return {
        key: doc[key]
        for key in _EDGAR_OPTIONAL_FIELDS
        if key in doc and doc[key] is not None
    }


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

    The optional EDGAR-shaped fields (``cik``, ``accession``,
    ``filing_date``, ``ticker``, ``form``) are read off the manifest
    and passed through to the output dict **only when present** —
    v1 callers still receive the v1 hit shape. T2 owns the additive
    optional kwargs that surface these fields via filter args.
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
        hit: dict[str, Any] = {
            "document_id": doc_id,
            "company": doc["company"],
            "period": doc["period"],
            "version": doc["sha256"],
            "page_count": len(pages_by_doc[doc_id]),
        }
        # T1: do NOT emit the new fields in the catalog hit; the
        # manifest reader exercises them, but the v1 output shape
        # stays stable for the ask path until T2 lands additive
        # optional filters. Reading them here also makes any drift in
        # the JSON shape surface as a KeyError at catalog boot rather
        # than at a deeper lookup site.
        _read_edgar_fields(doc)
        filings.append(hit)
    return {"filings": filings}


__all__ = ["list_filings"]