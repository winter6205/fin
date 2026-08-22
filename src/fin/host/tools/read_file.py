"""Host tool wrapper for ADR 0009's ``read_file`` tool.

Pure function: takes ``document_id`` + ``version`` (and optional
``page`` / ``page_limit``) and returns either a happy-path page-text
dict or ``{"error": "unknown_locator"}``. Reads only from the
committed extracted tree; never opens a PDF.

The happy-path shape is::

    {"document_id", "version", "page", "text", "page_count"}

where ``text`` is the joined page text of
``[page, page + page_limit - 1]`` (clamped to ``page_count``). The
model paginates by calling again with a higher ``page``.

``page_limit`` is silently clamped to 3 (ADR 0009 cap); ``page < 1``
or ``page > page_count`` returns the miss dict. ``IngestMissingError``
from ``load_extracted_tree`` is converted to the miss dict so the
model never sees a typed exception from this tool (T6 catches
``IngestMissingError`` separately at boot, ADR 0020).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import load_extracted_tree


_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"

_PAGE_LIMIT_CAP = 3
_UNKNOWN_LOCATOR: dict[str, str] = {"error": "unknown_locator"}


def read_file(
    document_id: str,
    version: str,
    page: int = 1,
    page_limit: int = 1,
    *,
    manifest_path: Path | None = None,
    extracted_root: Path | None = None,
) -> dict[str, Any]:
    """Read a page (or short page range) from a frozen 10-Q filing.

    Returns one of:

    * happy-path dict ``{"document_id", "version", "page", "text",
      "page_count"}``;
    * miss dict ``{"error": "unknown_locator"}`` for an unknown id,
      a version mismatch, a page out of range, a malformed extracted
      tree, or ``page_limit < 1``.

    Never raises.
    """
    if manifest_path is None:
        manifest_path = _DEFAULT_MANIFEST_PATH
    if extracted_root is None:
        extracted_root = _DEFAULT_EXTRACTED_ROOT

    # Lower-bound rejections happen before loading the tree; the upper
    # cap on ``page_limit`` clamps silently per ADR 0009.
    if page < 1 or page_limit < 1:
        return dict(_UNKNOWN_LOCATOR)
    if page_limit > _PAGE_LIMIT_CAP:
        page_limit = _PAGE_LIMIT_CAP

    try:
        pages_by_doc = load_extracted_tree(manifest_path, extracted_root)
    except IngestMissingError:
        # Typed error from a broken tree: convert to the model-facing
        # miss dict so the model can decide. T6 catches
        # ``IngestMissingError`` separately at graph boot for
        # diagnostics (ADR 0020).
        return dict(_UNKNOWN_LOCATOR)

    pages = pages_by_doc.get(document_id)
    if not pages:
        return dict(_UNKNOWN_LOCATOR)

    page_count = len(pages)
    if page > page_count:
        return dict(_UNKNOWN_LOCATOR)

    # The tree's per-page ``version`` is validated by
    # ``load_extracted_tree`` against the manifest sha256; comparing
    # the caller's ``version`` against the first page is enough to
    # reject any drift without re-reading the manifest.
    expected_version = pages[0].locator.version
    if version != expected_version:
        return dict(_UNKNOWN_LOCATOR)

    end_page = min(page + page_limit - 1, page_count)
    text = "\n\n".join(pages[i - 1].text for i in range(page, end_page + 1))

    return {
        "document_id": document_id,
        "version": version,
        "page": page,
        "text": text,
        "page_count": page_count,
    }


__all__ = ["read_file"]