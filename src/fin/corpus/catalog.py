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

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import load_extracted_tree


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"


def list_filings(
    company: str | None = None,
    period: str | None = None,
    *,
    cik: str | None = None,
    form: str | None = None,
    ticker: str | None = None,
    filing_date: str | None = None,
    manifest_path: Path | None = None,
    extracted_root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the ADR 0009 catalog shape for the ingested filings.

    Each hit is ``{document_id, company, period, version, page_count,
    form}``. Filings are sorted by ``document_id`` for deterministic
    output. Unknown filters yield ``{"filings": []}``.

    Additive EDGAR-shaped optional filters per ADR 0031: ``cik``,
    ``form``, ``ticker``, exact ``filing_date``. Filters are
    AND-combined. Empty filter values (``cik=""`` etc.) behave as
    empty hits — NOT "match anything" and NOT an exception, per
    spec "empty hits not exceptions".

    ``form`` is the one EDGAR-shaped field that is also emitted, per
    spec SC3: the operator reads the Corpus 首页 card to see what kind
    of filing a row is, and the model needs the same answer from the
    catalog tool. ``cik`` / ``accession`` / ``filing_date`` / ``ticker``
    stay filter inputs only.
    """
    if manifest_path is None:
        manifest_path = _DEFAULT_MANIFEST_PATH
    if extracted_root is None:
        extracted_root = _DEFAULT_EXTRACTED_ROOT

    # Missing / unreadable manifest is the same family of "the
    # committed corpus is in a known-bad state" failure as a missing
    # extracted tree (ADR 0020). Translate the low-level I/O error
    # to the typed ``IngestMissingError`` so the host graph can
    # fail fast at boot rather than see ``FileNotFoundError``.
    try:
        manifest_payload = json.loads(manifest_path.read_text())
    except FileNotFoundError as exc:
        raise IngestMissingError(
            f"manifest not found at {manifest_path}"
        ) from exc
    documents = manifest_payload["documents"]

    pages_by_doc = load_extracted_tree(manifest_path, extracted_root)

    filings: list[dict[str, Any]] = []
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        if company is not None and doc["company"] != company:
            continue
        if period is not None and doc["period"] != period:
            continue
        # EDGAR-shaped optional filters (ADR 0031). Empty-string
        # filter values behave as empty hits — short-circuit before
        # any row passes the row loop.
        if cik is not None:
            if cik == "" or doc.get("cik") != cik:
                continue
        if form is not None:
            if form == "" or doc.get("form") != form:
                continue
        if ticker is not None:
            if ticker == "" or doc.get("ticker") != ticker:
                continue
        if filing_date is not None:
            if filing_date == "" or doc.get("filing_date") != filing_date:
                continue
        doc_id = doc["document_id"]
        hit: dict[str, Any] = {
            "document_id": doc_id,
            "company": doc["company"],
            "period": doc["period"],
            "version": doc["sha256"],
            "page_count": len(pages_by_doc[doc_id]),
            # Always present, ``None`` when the manifest row was never
            # given one: a dropped key would read on the card as a
            # rendering bug rather than as an unlabelled corpus row.
            "form": doc.get("form"),
        }
        filings.append(hit)
    return {"filings": filings}


__all__ = ["list_filings"]