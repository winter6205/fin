"""Locator-bound table extraction from frozen page text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import Locator, load_extracted_tree


_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "corpus"
_DEFAULT_MANIFEST = _DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED = _DATA_DIR / "extracted"
_SEPARATOR = re.compile(r"^:?-{2,}:?$")


def _empty_payload(document_id: str, version: str, page: int) -> dict[str, Any]:
    return {
        "schema": "fin.table_extract.v1",
        "kind": "table",
        "locator": {
            "document_id": document_id,
            "version": version,
            "page": page,
        },
        "tables": [],
    }


def _split_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _is_separator(row: list[str]) -> bool:
    return bool(row) and all(_SEPARATOR.fullmatch(cell) for cell in row)


def _parse_tables(text: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    current: list[list[str]] = []
    for line in text.splitlines() + [""]:
        if "|" in line and line.strip().startswith("|"):
            current.append(_split_row(line))
            continue
        if len(current) >= 2:
            if _is_separator(current[1]):
                rows = current[2:]
                tables.append(
                    {
                        "index": len(tables) + 1,
                        "headers": current[0],
                        "rows": rows,
                    }
                )
        current = []
    return tables


def extract_tables(
    document_id: str,
    version: str,
    page: int = 1,
    *,
    manifest_path: Path | None = None,
    extracted_root: Path | None = None,
) -> dict[str, Any]:
    """Extract pipe-delimited tables from one frozen locator page.

    The returned artifact always carries the exact page locator. A
    page without a table is distinct from an unknown locator.
    """
    payload = _empty_payload(document_id, version, page)
    if page < 1:
        return {"error": "unknown_locator", "table_extract": payload}
    manifest = manifest_path or _DEFAULT_MANIFEST
    extracted = extracted_root or _DEFAULT_EXTRACTED
    try:
        tree = load_extracted_tree(manifest, extracted)
        pages = tree.get(document_id)
        if (
            not pages
            or version != pages[0].locator.version
            or page > len(pages)
        ):
            return {"error": "unknown_locator", "table_extract": payload}
        locator = Locator(
            document_id=document_id,
            version=version,
            page=page,
        )
        tables = _parse_tables(pages[page - 1].text)
        payload["locator"] = {
            "document_id": locator.document_id,
            "version": locator.version,
            "page": locator.page,
        }
        payload["tables"] = tables
        if not tables:
            return {"error": "table_not_found", "table_extract": payload}
        return {"table_extract": payload}
    except (IngestMissingError, OSError, KeyError, TypeError, ValueError):
        return {"error": "analysis_unavailable", "table_extract": payload}


__all__ = ["extract_tables"]
