"""Shared extracted ``_index.json`` parse / validate helpers.

Ingest and live-fetch both must fail closed on a present-but-corrupt
index. Keeping the schema checks in one place prevents the two writers
from drifting.
"""

from __future__ import annotations

import json
from pathlib import Path

from fin.corpus.errors import MalformedExtractedIndexError


def _validate_index_document_entry(
    document_id: object,
    entry: object,
    *,
    index_path: Path,
) -> None:
    """Raise when one document record fails the extracted-index schema."""
    if not isinstance(document_id, str) or not isinstance(entry, dict):
        raise MalformedExtractedIndexError(
            f"extracted index has an invalid document record: {index_path}"
        )
    version = entry.get("version")
    page_count = entry.get("page_count")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 0
    ):
        raise MalformedExtractedIndexError(
            f"extracted index has an invalid document record for "
            f"{document_id!r}: {index_path}"
        )


def parse_extracted_index_documents(
    index_path: Path,
) -> dict[str, dict[str, object]]:
    """Parse a present ``_index.json`` into document records.

    The path must already exist as a regular file. Callers that treat
    a missing index as initialization should probe existence first.
    """
    if not index_path.is_file():
        raise MalformedExtractedIndexError(
            f"extracted index is not a regular file: {index_path}"
        )
    try:
        payload = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise MalformedExtractedIndexError(
            f"malformed extracted index: {index_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise MalformedExtractedIndexError(
            f"extracted index must be a JSON object: {index_path}"
        )
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise MalformedExtractedIndexError(
            f"extracted index has no 'documents' object: {index_path}"
        )
    for document_id, entry in documents.items():
        _validate_index_document_entry(
            document_id, entry, index_path=index_path
        )
    return documents


def read_existing_extracted_index(
    extracted_root: Path,
) -> dict[str, dict[str, object]] | None:
    """Return documents from ``extracted_root/_index.json``, or ``None``.

    A missing index is an initialization case (``None``). A present
    unreadable or structurally invalid index is corruption.
    """
    index_path = extracted_root / "_index.json"
    if not index_path.exists():
        return None
    return parse_extracted_index_documents(index_path)


__all__ = [
    "parse_extracted_index_documents",
    "read_existing_extracted_index",
]
