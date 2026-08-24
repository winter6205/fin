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
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

from fin.corpus.errors import ManifestShaMismatchError
from fin.corpus.ocr import (
    OCREngine,
    OCRError,
    OcrRequiredUnavailableError,
    detect_ocr_required,
)


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
    document_id: str,
    version: str,
    page: int,
    text: str,
    *,
    ocr_applied: bool = False,
) -> bytes:
    return json.dumps(
        {
            "document_id": document_id,
            "version": version,
            "page": page,
            "text": text,
            "ocr_applied": ocr_applied,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _run_ocr_engine(
    *,
    engine: OCREngine,
    pdf_path: Path,
    page_num: int,
    document_id: str,
) -> str:
    """Invoke ``engine.recognize`` and coerce every failure shape into
    the typed ``OcrRequiredUnavailableError``.

    EXIT: empty OCR text or ``OCRError`` from the engine -> typed EXIT.
    """
    try:
        text = engine.recognize(source=pdf_path, page=page_num)
    except OcrRequiredUnavailableError:
        # Re-raise verbatim — must not be re-wrapped.
        raise
    except OCRError as exc:
        raise OcrRequiredUnavailableError(
            f"OCR failed for {document_id} page {page_num}: {exc}"
        ) from exc
    if not text or not text.strip():
        # Spec: OCR-required units must not succeed with empty
        # frozen text.
        raise OcrRequiredUnavailableError(
            f"OCR returned empty text for {document_id} page {page_num}"
        )
    return text


def _process_page_with_ocr(
    *,
    page: object,
    page_num: int,
    document_id: str,
    pdf_path: Path,
    ocr_engine: OCREngine | None,
    ocr_required_marker: str | None,
) -> tuple[str, bool]:
    """Run one page through pdfplumber + optional OCR.

    Returns ``(text, ocr_applied)`` for the page. The text is the
    pdfplumber extraction unless OCR was required and succeeded, in
    which case the OCR text replaces it. Raises the typed
    ``OcrRequiredUnavailableError`` on every OCR failure shape
    (no engine / engine raised / empty text); the caller is
    responsible for cleanup.
    """
    text = page.extract_text() or ""
    if ocr_required_marker is None and not detect_ocr_required(
        text=text,
        image_count=len(getattr(page, "images", []) or []),
    ):
        return text, False
    if ocr_engine is None:
        # EXIT: OCR-required unit + no engine -> typed EXIT; caller
        # cleans up partial doc_dir and re-raises.
        raise OcrRequiredUnavailableError(
            f"{document_id} page {page_num} needs OCR but no "
            "ocr_engine was supplied; pass an OCREngine or install "
            "the [ocr] extra"
        )
    text = _run_ocr_engine(
        engine=ocr_engine,
        pdf_path=pdf_path,
        page_num=page_num,
        document_id=document_id,
    )
    return text, True


def _ingest_one_page(
    *,
    page: object,
    page_num: int,
    document_id: str,
    pdf_path: Path,
    doc_dir: Path,
    version: str,
    ocr_engine: OCREngine | None,
    ocr_required_marker: str | None,
) -> tuple[str, bool, bool]:
    """Run one page through OCR (if required) and write its
    artifacts. Returns ``(text, ocr_applied, page_was_written)`` —
    the last flag mirrors ``_write_if_drifted``'s True/False so the
    caller can split the cumulative write/skip counters. Raises
    ``OcrRequiredUnavailableError`` for OCR-required failure shapes;
    the caller owns the partial-doc_dir cleanup.
    """
    text, ocr_applied = _process_page_with_ocr(
        page=page,
        page_num=page_num,
        document_id=document_id,
        pdf_path=pdf_path,
        ocr_engine=ocr_engine,
        ocr_required_marker=ocr_required_marker,
    )
    page_bytes = _page_payload(
        document_id=document_id,
        version=version,
        page=page_num,
        text=text,
        ocr_applied=ocr_applied,
    )
    passages_bytes = _passages_payload(
        document_id=document_id,
        version=version,
        page=page_num,
        passages=_chunk_page_text(text),
    )
    page_filename = f"page-{page_num:0{_PAGE_NUM_WIDTH}d}.json"
    passages_filename = f"passages-{page_num:0{_PAGE_NUM_WIDTH}d}.json"
    wrote_page = _write_if_drifted(doc_dir / page_filename, page_bytes)
    _write_if_drifted(doc_dir / passages_filename, passages_bytes)
    return text, ocr_applied, wrote_page


def _ingest_all_pages(
    *,
    pdf_path: Path,
    document_id: str,
    doc_dir: Path,
    version: str,
    ocr_engine: OCREngine | None,
    ocr_required_marker: str | None,
) -> tuple[int, list[int], int, int]:
    """Walk every page of ``pdf_path`` through ingest + optional OCR.

    Returns ``(pages_this_doc, ocr_applied_pages, written_delta,
    skipped_delta)``. Raises ``OcrRequiredUnavailableError`` for
    OCR-required failure shapes; the caller owns the partial-doc_dir
    cleanup.
    """
    pages_this_doc = 0
    ocr_applied_pages: list[int] = []
    written_delta = 0
    skipped_delta = 0
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            _text, ocr_applied, wrote_page = _ingest_one_page(
                page=page,
                page_num=page_idx,
                document_id=document_id,
                pdf_path=pdf_path,
                doc_dir=doc_dir,
                version=version,
                ocr_engine=ocr_engine,
                ocr_required_marker=ocr_required_marker,
            )
            pages_this_doc += 1
            if wrote_page:
                written_delta += 1
            else:
                skipped_delta += 1
            if ocr_applied:
                ocr_applied_pages.append(page_idx)
    return (
        pages_this_doc,
        ocr_applied_pages,
        written_delta,
        skipped_delta,
    )


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
    facts_root: Path | None = None,
    ocr_engine: OCREngine | None = None,
    ocr_required_marker: str | None = None,
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
    facts_root:
        Output directory for the fact table (S1 / ADR 0031). When
        ``None``, defaults to ``<data/corpus>/facts``. ``write_facts``
        is called for every document the manifest lists, populating
        the seed facts the later ``get_xbrl_fact`` lookup will read.
    ocr_engine:
        Optional OCR engine (``fin.corpus.ocr.OCREngine``). When
        ``None`` (default), OCR-required units raise the typed
        ``OcrRequiredUnavailableError`` and the unit is not
        written — per spec § Boundaries: "OCR is required (collapsed
        text-layer + images) AND engine/extra is missing OR OCR
        fails -> ingest does NOT succeed that unit with empty
        frozen text; typed non-zero / error, not silent skip."
    ocr_required_marker:
        Force OCR on every page regardless of the
        ``detect_ocr_required`` heuristic. Used by tests to drive
        the OCR path without a real scanned-PDF fixture. When
        truthy, the page is treated as OCR-required even if the
        text-layer extraction is rich.

    Raises
    ------
    ManifestShaMismatchError
        If the PDF file referenced by a manifest entry is missing, or
        if its sha256 does not match the manifest entry.
    OcrRequiredUnavailableError
        If an OCR-required unit is hit (collapsed text-layer +
        images, or ``ocr_required_marker`` is set) and no engine is
        supplied, or the engine raises / returns empty text. Per
        spec, the unit is not marked complete and no frozen text
        is written.

    IngestMissingError
        Not raised here — see ``fin.corpus.extracted.load_extracted_tree``
        for the read-side check.
    """
    if facts_root is None:
        facts_root = Path(__file__).resolve().parents[3] / "data" / "corpus" / "facts"

    # Import here so this module's pdfplumber dep does not transitively
    # leak into the ask path if the facts module is reached via
    # ``fin.corpus.facts.read_facts`` (the lookup side).
    from fin.corpus.facts import write_facts
    from fin.corpus.facts_seed import seed_facts_for_document

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
        ocr_applied_pages: list[int] = []
        try:
            (
                pages_this_doc,
                ocr_applied_pages,
                pages_written_delta,
                pages_skipped_delta,
            ) = _ingest_all_pages(
                pdf_path=pdf_path,
                document_id=document_id,
                doc_dir=doc_dir,
                version=expected_sha,
                ocr_engine=ocr_engine,
                ocr_required_marker=ocr_required_marker,
            )
        except OcrRequiredUnavailableError:
            # EXIT: OCR failure on any page -> clean partial doc_dir,
            # propagate typed EXIT so the CLI exits non-zero.
            if doc_dir.exists():
                shutil.rmtree(doc_dir)
            raise

        pages_written += pages_written_delta
        pages_skipped += pages_skipped_delta
        pages_total += pages_this_doc

        doc_index_entry: dict[str, object] = {
            "version": expected_sha,
            "page_count": pages_this_doc,
        }
        if ocr_applied_pages:
            doc_index_entry["ocr_applied"] = True
            doc_index_entry["ocr_applied_pages"] = sorted(ocr_applied_pages)
        index[document_id] = doc_index_entry

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

    # Seed the fact table for every document. The seed list is a
    # per-document static mapping in ``facts_seed`` — T1 only ships
    # the AAPL-2023Q3 row (the v1 gold span); other documents get an
    # empty fact list, which is a valid "no facts yet" catalog state.
    # ``write_facts`` is byte-idempotent, so a re-run that hits the
    # fast-skip path above still writes the same fact table.
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        document_id: str = doc["document_id"]
        expected_sha: str = doc["sha256"]
        seed = seed_facts_for_document(
            document_id=document_id, version=expected_sha
        )
        if seed is not None:
            write_facts(
                document_id=document_id, facts=seed, facts_root=facts_root
            )

    return IngestResult(
        documents=len(documents),
        pages=pages_total,
        pages_written=pages_written,
        pages_skipped=pages_skipped,
    )


def _default_paths() -> tuple[Path, Path, Path, Path]:
    """Compute default (manifest, pdf_dir, extracted_root, facts_root)
    for the CLI.

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
        corpus_dir / "facts",
    )


def _resolve_ocr_engine(name: str) -> OCREngine | None:
    """Resolve the ``--ocr-engine`` CLI choice.

    * ``tesseract`` — use ``TesseractEngine`` if the optional
      ``pytesseract`` extra is installed; fall back to ``None`` with
      a clear stderr message otherwise. OCR-required units will then
      fail with the typed ``OcrRequiredUnavailableError`` (spec
      contract: no silent skip).
    * ``none`` — always ``None``; OCR-required units fail typed.

    Returns ``None`` when OCR is disabled (no engine). The mock
    engine lives in ``tests/corpus/test_ocr.py``; tests construct
    ``MockOCREngine`` directly and pass it via ``run_ingest``,
    bypassing this helper entirely.
    """
    if name == "none":
        return None
    if name == "tesseract":
        try:
            import pytesseract  # noqa: F401
        except Exception as exc:
            # EXIT: pytesseract missing (optional [ocr] extra not
            # installed OR tesseract binary absent) -> OCR disabled;
            # OCR-required units will fail typed via
            # OcrRequiredUnavailableError, no silent skip.
            print(
                "pytesseract is not installed; install with "
                "`uv sync --extra ocr` (requires the tesseract "
                f"system binary). OCR will be disabled: {exc}",
                file=sys.stderr,
            )
            return None
        from fin.corpus.ocr import TesseractEngine

        return TesseractEngine()
    raise ValueError(f"unknown --ocr-engine choice: {name!r}")


def main(argv: list[str] | None = None) -> int:
    default_manifest, default_pdf_dir, default_extracted, default_facts = (
        _default_paths()
    )
    parser = argparse.ArgumentParser(
        prog="python -m fin.corpus.ingest",
        description=(
            "fin v1 ingest: extract frozen PDFs into the committed "
            "data/corpus/extracted/ tree (ADR 0020) and seed the "
            "fact table for `get_xbrl_fact` (ADR 0031)."
        ),
    )
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument("--pdf-dir", type=Path, default=default_pdf_dir)
    parser.add_argument(
        "--extracted-root", type=Path, default=default_extracted
    )
    parser.add_argument("--facts-root", type=Path, default=default_facts)
    parser.add_argument(
        "--ocr-engine",
        choices=("tesseract", "none"),
        default="tesseract",
        help=(
            "OCR backend. ``tesseract`` (default) requires the "
            "[ocr] extra + tesseract binary; falls back to no "
            "engine with a stderr message if either is missing. "
            "``none`` disables OCR (OCR-required units fail typed). "
            "Test doubles are constructed in-process and passed "
            "to ``run_ingest`` directly — they do not go through "
            "this CLI."
        ),
    )
    args = parser.parse_args(argv)
    ocr_engine = _resolve_ocr_engine(args.ocr_engine)
    result = run_ingest(
        manifest_path=args.manifest,
        pdf_dir=args.pdf_dir,
        extracted_root=args.extracted_root,
        facts_root=args.facts_root,
        ocr_engine=ocr_engine,
    )
    print(
        f"ingest: documents={result.documents} "
        f"pages={result.pages} "
        f"written={result.pages_written} "
        f"skipped={result.pages_skipped}"
    )
    return 0


__all__ = ["IngestResult", "run_ingest", "main", "_resolve_ocr_engine"]


if __name__ == "__main__":
    sys.exit(main())