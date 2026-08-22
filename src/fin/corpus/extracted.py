"""Read-side loader for the committed extracted tree.

This module is the **only** module that the ask path reads to obtain
page text and page-passage chunks. It must NOT import ``pdfplumber``;
the ingest side does that work once and commits JSON artifacts to
``data/corpus/extracted/`` (ADR 0020). If the tree is missing or
incomplete, ``load_extracted_tree`` raises ``IngestMissingError`` so
the caller can fail fast instead of silently opening a PDF.

The loader uses the corpus manifest as the SSOT for which document_ids
and per-document page counts should exist on disk. A page file
``page-NNN.json`` and a passages file ``passages-NNN.json`` are
expected for every 1-based page of every document in the manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fin.corpus.errors import IngestMissingError


@dataclass(frozen=True)
class Locator:
    """v1 locator: ``{document_id, version, page}`` (ADR 0006).

    Frozen so an extracted artifact cannot drift its identity after
    ingest; ``version`` is the manifest sha256 of the source PDF, NOT
    a content hash.
    """

    document_id: str
    version: str
    page: int


@dataclass(frozen=True)
class Passage:
    """One passage chunk on a page. ``char_start`` / ``char_end`` are
    half-open offsets into the page text.
    """

    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ExtractedPage:
    """One page of one document: page text plus the passages on that page."""

    locator: Locator
    text: str
    passages: tuple[Passage, ...]


def _load_manifest(manifest_path: Path) -> list[dict]:
    payload = json.loads(manifest_path.read_text())
    documents = payload["documents"]
    if not isinstance(documents, list):
        raise IngestMissingError(
            f"manifest at {manifest_path} has no list under 'documents'"
        )
    return documents


def _read_expected_index(
    extracted_root: Path,
) -> dict[str, dict[str, object]] | None:
    """Read the ``_index.json`` that ingest writes. Returns the
    per-document ``{version, page_count}`` records, or ``None`` if the
    index is missing / malformed. The loader uses this to know how
    many pages to expect per document; without it, a mid-range
    deletion would silently shrink the tree.
    """
    index_path = extracted_root / "_index.json"
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text())
    except json.JSONDecodeError as exc:
        raise IngestMissingError(
            f"malformed _index.json: {index_path} "
            f"({exc.msg} at line {exc.lineno})"
        ) from exc
    docs = payload.get("documents")
    if not isinstance(docs, dict):
        return None
    return docs


def _read_page_payload(page_path: Path) -> tuple[str, str, int, str]:
    """Read a ``page-NNN.json`` and return ``(document_id, version, page, text)``.

    Raises ``IngestMissingError`` with the offending path on malformed
    JSON, so the caller sees the typed error rather than a generic
    ``JSONDecodeError``.
    """
    try:
        payload = json.loads(page_path.read_text())
    except json.JSONDecodeError as exc:
        raise IngestMissingError(
            f"malformed page artifact: {page_path} ({exc.msg} at line {exc.lineno})"
        ) from exc
    return (
        payload["document_id"],
        payload["version"],
        int(payload["page"]),
        payload["text"],
    )


def _read_passages_payload(
    passages_path: Path,
) -> list[Passage]:
    """Read a ``passages-NNN.json`` and return the ``Passage`` list.

    Raises ``IngestMissingError`` on malformed JSON.
    """
    try:
        payload = json.loads(passages_path.read_text())
    except json.JSONDecodeError as exc:
        raise IngestMissingError(
            f"malformed passages artifact: {passages_path} "
            f"({exc.msg} at line {exc.lineno})"
        ) from exc
    out: list[Passage] = []
    for entry in payload.get("passages", []):
        out.append(
            Passage(
                text=entry["text"],
                char_start=int(entry["char_start"]),
                char_end=int(entry["char_end"]),
            )
        )
    return out


def load_extracted_tree(
    manifest_path: Path,
    extracted_root: Path,
) -> dict[str, tuple[ExtractedPage, ...]]:
    """Load the committed extracted tree under ``extracted_root``.

    The manifest (``data/corpus/manifest.json``) is the SSOT for which
    documents and how many pages per document should exist on disk.
    Documents and pages are returned in sorted order so callers can
    rely on deterministic iteration (T4's BM25 index, the citation
    gate's evidence lookup).

    Raises
    ------
    IngestMissingError
        If ``extracted_root`` does not exist, if any document in the
        manifest has no directory under ``extracted_root``, or if any
        expected ``page-NNN.json`` / ``passages-NNN.json`` is missing
        or malformed. The loader does NOT silently skip or fall back
        to opening the source PDF.
    """
    documents = _load_manifest(manifest_path)

    if not extracted_root.exists():
        raise IngestMissingError(
            f"extracted tree missing: {extracted_root} "
            "— run `python -m fin.corpus.ingest` first"
        )

    expected_index = _read_expected_index(extracted_root)

    out: dict[str, tuple[ExtractedPage, ...]] = {}
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        document_id: str = doc["document_id"]
        expected_version: str = doc["sha256"]
        doc_dir = extracted_root / document_id
        if not doc_dir.is_dir():
            raise IngestMissingError(
                f"extracted tree missing document dir: {doc_dir}"
            )

        expected_page_count: int | None = None
        if expected_index is not None:
            entry = expected_index.get(document_id)
            if isinstance(entry, dict):
                pc = entry.get("page_count")
                if isinstance(pc, int):
                    expected_page_count = pc

        if expected_page_count is None:
            # No index to validate against: fall back to scanning the
            # highest page number present. This still catches an empty
            # document dir but cannot catch a missing mid-range page.
            page_files = sorted(doc_dir.glob("page-*.json"))
            if not page_files:
                raise IngestMissingError(
                    f"no page files under {doc_dir}"
                )
        else:
            # Strict check: every expected page must have BOTH a page
            # file and a passages file. This is the "incomplete tree"
            # detection that the host graph depends on.
            for n in range(1, expected_page_count + 1):
                page_path = doc_dir / f"page-{n:03d}.json"
                if not page_path.is_file():
                    raise IngestMissingError(
                        f"missing page file for {document_id} page {n}: "
                        f"{page_path}"
                    )
                passages_path = doc_dir / f"passages-{n:03d}.json"
                if not passages_path.is_file():
                    raise IngestMissingError(
                        f"missing passages file for {document_id} page {n}: "
                        f"{passages_path}"
                    )
            page_files = sorted(doc_dir.glob("page-*.json"))

        pages: list[ExtractedPage] = []
        for page_path in page_files:
            doc_id, version, page_num, text = _read_page_payload(page_path)
            if doc_id != document_id:
                raise IngestMissingError(
                    f"{page_path} document_id={doc_id!r} does not "
                    f"match manifest document_id={document_id!r}"
                )
            if version != expected_version:
                raise IngestMissingError(
                    f"{page_path} version={version!r} does not "
                    f"match manifest sha256={expected_version!r}"
                )
            # 3-digit zero-padded index, matching ingest's naming rule.
            expected_name = f"page-{page_num:03d}.json"
            if page_path.name != expected_name:
                raise IngestMissingError(
                    f"{page_path} does not match expected name "
                    f"{expected_name!r} for page={page_num}"
                )

            passages_path = doc_dir / f"passages-{page_num:03d}.json"
            if not passages_path.is_file():
                raise IngestMissingError(
                    f"missing passages file for {document_id} page {page_num}: "
                    f"{passages_path}"
                )
            passages = tuple(_read_passages_payload(passages_path))

            pages.append(
                ExtractedPage(
                    locator=Locator(
                        document_id=document_id,
                        version=expected_version,
                        page=page_num,
                    ),
                    text=text,
                    passages=passages,
                )
            )

        out[document_id] = tuple(pages)

    return out


__all__ = ["ExtractedPage", "Locator", "Passage", "load_extracted_tree"]