"""Idempotent ingest: frozen PDFs -> committed extracted tree.

Runs ``pdfplumber`` once per frozen PDF and writes two files per page
under ``data/corpus/extracted/<document_id>/``:

* ``page-NNN.json`` — the page text plus its locator
  (``document_id``, ``version``, ``page``).
* ``passages-NNN.json`` — the locator plus the passage chunks on that
  page. Each chunk carries ``char_start`` / ``char_end`` offsets into
  the page text.

A top-level ``_index.json`` lists every document and its expected page
count so the loader can fail fast on partial extractions.

Idempotency (ADR 0020): a second ``run_ingest()`` against the same
source tree must produce byte-identical artifacts. We achieve this by
computing the desired JSON payload first, hashing it with sha256, and
skipping any file whose on-disk content already matches. New or
drifted files are written atomically via ``os.replace`` over a temp
file. Iteration is in sorted ``document_id`` order so on-disk ordering
is reproducible across machines and Python versions.

Chunking rule
-------------

Fixed-size windows of 512 characters with a 64-character overlap. The
text returned by ``pdfplumber.Page.extract_text()`` does not expose
stable natural-sentence boundaries across runs (line wraps depend on
the PDF's text-extraction path and many 10-Q paragraphs use
abbreviations such as "Inc.", "Co.", "No." that a regex sentence
splitter would misread). A fixed-window rule is the simplest way to
guarantee deterministic artifacts (ADR 0020); T3 / T4 only need a
stable per-page passage list — they do not require natural-sentence
alignment. ADR 0006 specifies that ``{document_id, version, page}`` is
the locator; offsets in passages are page-local (not chunk IDs), so
this chunking rule does not change the locator shape.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

from fin.corpus.errors import ManifestShaMismatchError


_PASSAGE_SIZE = 512
_PASSAGE_OVERLAP = 64
_PAGE_NUM_WIDTH = 3


@dataclass(frozen=True)
class _RawPassage:
    """Internal: one passage on a page. Kept private to this module."""

    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class IngestResult:
    """Counters returned by ``run_ingest`` for tests and CLI output."""

    documents: int
    pages: int
    pages_written: int
    pages_skipped: int


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically via ``os.replace``.

    A short temp file in the same directory avoids cross-device rename
    surprises and keeps the partial write in the same filesystem.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _write_if_drifted(path: Path, payload: bytes) -> bool:
    """Write ``payload`` to ``path`` iff its sha256 differs from the
    existing file. Returns True iff a write occurred.
    """
    if path.exists():
        existing = path.read_bytes()
        if _sha256_bytes(existing) == _sha256_bytes(payload):
            return False
    _atomic_write(path, payload)
    return True


def _read_existing_index(extracted_root: Path) -> dict[str, dict[str, object]] | None:
    """Try to read the committed ``_index.json``.

    Returns the per-document records, or ``None`` if the index is
    missing / malformed (in which case the caller falls back to a full
    pdfplumber extraction). This is the fast-skip predicate: if the
    index says a document was extracted at the current expected sha
    and the page count matches the existing files, we can skip
    reopening the PDF.
    """
    index_path = extracted_root / "_index.json"
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    docs = payload.get("documents")
    if not isinstance(docs, dict):
        return None
    return docs


def _artifacts_present(doc_dir: Path, page_count: int) -> bool:
    """Cheap existence check: every expected page and passages file is
    on disk. Used as the fast-skip predicate before we trust the
    index and avoid reopening the PDF.
    """
    if not doc_dir.is_dir():
        return False
    for n in range(1, page_count + 1):
        if not (doc_dir / f"page-{n:03d}.json").is_file():
            return False
        if not (doc_dir / f"passages-{n:03d}.json").is_file():
            return False
    return True


def _chunk_page_text(text: str) -> list[_RawPassage]:
    """Fixed-size sliding window over the page text (see module docstring)."""
    if not text:
        return []
    chunks: list[_RawPassage] = []
    n = len(text)
    step = _PASSAGE_SIZE - _PASSAGE_OVERLAP
    i = 0
    while i < n:
        end = min(i + _PASSAGE_SIZE, n)
        chunks.append(_RawPassage(text=text[i:end], char_start=i, char_end=end))
        if end >= n:
            break
        i += step
    return chunks


def _page_payload(
    document_id: str, version: str, page: int, text: str
) -> bytes:
    return json.dumps(
        {
            "document_id": document_id,
            "version": version,
            "page": page,
            "text": text,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _passages_payload(
    document_id: str, version: str, page: int, passages: list[_RawPassage]
) -> bytes:
    return json.dumps(
        {
            "document_id": document_id,
            "version": version,
            "page": page,
            "passages": [
                {
                    "text": p.text,
                    "char_start": p.char_start,
                    "char_end": p.char_end,
                }
                for p in passages
            ],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def run_ingest(
    *,
    manifest_path: Path,
    pdf_dir: Path,
    extracted_root: Path,
) -> IngestResult:
    """Run the idempotent ingest command.

    Parameters
    ----------
    manifest_path:
        Path to ``data/corpus/manifest.json``.
    pdf_dir:
        Directory containing the frozen PDFs (each document's
        ``filename`` lives here).
    extracted_root:
        Output directory for the extracted tree. Will be created if
        missing.

    Raises
    ------
    ManifestShaMismatchError
        If the PDF file referenced by a manifest entry is missing, or
        if its sha256 does not match the manifest entry.

    IngestMissingError
        Not raised here — see ``fin.corpus.extracted.load_extracted_tree``
        for the read-side check.
    """
    manifest = json.loads(manifest_path.read_text())
    documents = manifest["documents"]
    extracted_root.mkdir(parents=True, exist_ok=True)

    # Fast-skip: if the previous run left a matching index and all
    # expected files exist, we skip the pdfplumber re-open and rely on
    # the byte-level idempotency of those files. This is what makes
    # repeated ``run_ingest`` calls cheap — without it, a second run
    # would still pay the full pdfplumber extraction cost on every
    # PDF (ADR 0020 requires that a second run not change file
    # checksums; it does NOT require that it re-run extraction).
    existing_index = _read_existing_index(extracted_root)

    pages_total = 0
    pages_written = 0
    pages_skipped = 0
    index: dict[str, dict[str, object]] = {}

    # Sort by document_id so on-disk ordering is deterministic across
    # runs and machines.
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        document_id: str = doc["document_id"]
        filename: str = doc["filename"]
        expected_sha: str = doc["sha256"]
        pdf_path = pdf_dir / filename

        if not pdf_path.exists():
            raise ManifestShaMismatchError(
                f"missing frozen PDF for {document_id}: {pdf_path}"
            )
        actual_sha = _sha256_file(pdf_path)
        if actual_sha != expected_sha:
            raise ManifestShaMismatchError(
                f"sha256 mismatch for {document_id}: "
                f"manifest={expected_sha} actual={actual_sha}"
            )

        doc_dir = extracted_root / document_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        existing_doc = (
            existing_index.get(document_id) if existing_index else None
        )
        if (
            existing_doc is not None
            and existing_doc.get("version") == expected_sha
            and isinstance(existing_doc.get("page_count"), int)
            and _artifacts_present(doc_dir, int(existing_doc["page_count"]))
        ):
            # Fast-skip: trust the index and the existence check. We
            # still account for the pages as "skipped" so the CLI
            # surface reflects the no-op nature of this run.
            page_count = int(existing_doc["page_count"])
            index[document_id] = {
                "version": expected_sha,
                "page_count": page_count,
            }
            pages_total += page_count
            pages_skipped += page_count
            continue

        pages_this_doc = 0
        with pdfplumber.open(str(pdf_path)) as pdf:
            for one_based_page_idx, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                page_bytes = _page_payload(
                    document_id=document_id,
                    version=expected_sha,
                    page=one_based_page_idx,
                    text=text,
                )
                passages_bytes = _passages_payload(
                    document_id=document_id,
                    version=expected_sha,
                    page=one_based_page_idx,
                    passages=_chunk_page_text(text),
                )
                page_filename = f"page-{one_based_page_idx:0{_PAGE_NUM_WIDTH}d}.json"
                passages_filename = (
                    f"passages-{one_based_page_idx:0{_PAGE_NUM_WIDTH}d}.json"
                )
                if _write_if_drifted(doc_dir / page_filename, page_bytes):
                    pages_written += 1
                else:
                    pages_skipped += 1
                # The passages file is written with the same predicate,
                # so its bytes match the page file's drift state — both
                # files are either present-and-correct or absent-and-new.
                _write_if_drifted(doc_dir / passages_filename, passages_bytes)
                pages_total += 1
                pages_this_doc += 1

        index[document_id] = {
            "version": expected_sha,
            "page_count": pages_this_doc,
        }

    # Rewrite the index deterministically (sorted by document_id).
    index_payload = json.dumps(
        {
            "schema_version": 1,
            "documents": dict(sorted(index.items())),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _write_if_drifted(extracted_root / "_index.json", index_payload)

    return IngestResult(
        documents=len(documents),
        pages=pages_total,
        pages_written=pages_written,
        pages_skipped=pages_skipped,
    )


def _default_paths() -> tuple[Path, Path, Path]:
    """Compute default (manifest, pdf_dir, extracted_root) for the CLI.

    The CLI is invoked as ``python -m fin.corpus.ingest`` from the
    worktree root, so the defaults resolve to ``data/corpus/...`` under
    that root. Programmatic callers should pass the paths explicitly.
    """
    project_root = Path(__file__).resolve().parents[3]
    corpus_dir = project_root / "data" / "corpus"
    return (
        corpus_dir / "manifest.json",
        corpus_dir / "pdf",
        corpus_dir / "extracted",
    )


def main(argv: list[str] | None = None) -> int:
    default_manifest, default_pdf_dir, default_extracted = _default_paths()
    parser = argparse.ArgumentParser(
        prog="python -m fin.corpus.ingest",
        description=(
            "fin v1 ingest: extract frozen PDFs into the committed "
            "data/corpus/extracted/ tree (ADR 0020)."
        ),
    )
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument("--pdf-dir", type=Path, default=default_pdf_dir)
    parser.add_argument(
        "--extracted-root", type=Path, default=default_extracted
    )
    args = parser.parse_args(argv)
    result = run_ingest(
        manifest_path=args.manifest,
        pdf_dir=args.pdf_dir,
        extracted_root=args.extracted_root,
    )
    print(
        f"ingest: documents={result.documents} "
        f"pages={result.pages} "
        f"written={result.pages_written} "
        f"skipped={result.pages_skipped}"
    )
    return 0


__all__ = ["IngestResult", "run_ingest", "main"]


if __name__ == "__main__":
    sys.exit(main())