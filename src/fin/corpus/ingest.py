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

The run also refreshes the EDGAR-shaped catalog fields (``cik`` /
``accession`` / ``filing_date`` / ``form`` / ``ticker``, ADR 0031) on
every manifest row — see ``update_catalog_fields``. That step reads
only local metadata plus an optional operator seed file; it never
opens a SEC host. The gated live path lives in
``fin.corpus.live_fetch``.

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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

from fin.corpus.errors import (
    IngestMissingError,
    MalformedExtractedIndexError,
    ManifestShaMismatchError,
)
from fin.corpus.ocr import (
    OCREngine,
    OCRError,
    OcrRequiredUnavailableError,
    detect_ocr_required,
)
from fin.corpus.metadata_store import (
    MetadataStore,
    default_metadata_store,
)
from fin.corpus.ingest_state import (
    IngestState,
    IngestStateStore,
    default_ingest_state_store,
)
from fin.corpus.ingest_state_errors import (
    DuplicateJobError,
    IngestStepError,
)
from fin.corpus.object_store import ObjectStore
from fin.corpus.object_store_errors import (
    ObjectStoreError,
    ObjectStoreUnavailableError,
)
from fin.corpus.structure_units import (
    StructureParser,
    StructureUnit,
    parse_structure_units,
)


_PASSAGE_SIZE = 512
_PASSAGE_OVERLAP = 64
_PAGE_NUM_WIDTH = 3

# The EDGAR-shaped catalog fields ``list_filings`` filters on (ADR
# 0031). Ingest owns writing them onto the manifest rows; the catalog
# only reads them.
_EDGAR_CATALOG_FIELDS = ("cik", "accession", "filing_date", "form", "ticker")

# Local seed for the frozen-12 demo corpus. EDGAR assigns one CIK per
# registrant for the life of the issuer, so the mapping is stable
# enough to carry in-repo and lets the CLI fill ``cik`` with no SEC
# GET. ``accession`` / ``filing_date`` are per-filing and cannot be
# derived from local metadata — they come from ``--catalog-seed``.
_ISSUER_CIK: dict[str, str] = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "AMZN": "0001018724",
}


class CatalogSeedError(IngestMissingError):
    """Typed EXIT: the ``--catalog-seed`` file is missing or malformed.

    Refusing beats guessing: a seed the operator meant to apply but
    that cannot be parsed must not silently degrade into "catalog
    fields stay as they were".
    """


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
    catalog_updated: int = 0
    raw_storage: str = "local_fallback"


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
    """Read the committed ``_index.json`` for the fast-skip predicate.

    A missing index is an initialization case and returns ``None``.
    An existing unreadable or structurally invalid index is corruption:
    raising before any output write prevents a damaged catalog from
    being silently replaced.
    """
    from fin.corpus.extracted_index import read_existing_extracted_index

    return read_existing_extracted_index(extracted_root)


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


def load_catalog_seed(seed_path: Path) -> dict[str, dict[str, str]]:
    """Read an operator ``--catalog-seed`` JSON file.

    Shape: ``{"documents": {"<document_id>": {"accession": ..., ...}}}``.
    Only the five EDGAR-shaped keys are read; anything else in a seed
    row is ignored so an operator can keep notes alongside the values.

    Raises
    ------
    CatalogSeedError
        The file is absent, is not JSON, or has no ``documents``
        mapping. A seed that cannot be applied is an EXIT, not a
        silent no-op.
    """
    try:
        payload = json.loads(seed_path.read_text())
    except OSError as exc:
        raise CatalogSeedError(f"catalog seed not readable: {seed_path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogSeedError(
            f"catalog seed is not valid JSON: {seed_path} ({exc.msg})"
        ) from exc
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, dict):
        raise CatalogSeedError(
            f"catalog seed {seed_path} has no 'documents' object mapping "
            "document_id -> EDGAR fields"
        )
    return {
        str(document_id): {
            key: str(value)
            for key, value in row.items()
            if key in _EDGAR_CATALOG_FIELDS and value is not None
        }
        for document_id, row in documents.items()
        if isinstance(row, dict)
    }


def _catalog_fields_for(
    doc: Mapping[str, object], seed_row: Mapping[str, str]
) -> dict[str, object | None]:
    """Resolve the five EDGAR-shaped fields for one manifest row.

    Precedence is seed → value already on the row → local metadata the
    manifest already carries. ``accession`` and ``filing_date`` have no
    local fallback: when neither the seed nor the row supplies them the
    field resolves to ``None`` and is written as an explicit null, so
    the gap is visible in the catalog instead of silently absent.
    """
    ticker = (
        seed_row.get("ticker")
        or doc.get("ticker")
        or doc.get("source_id")
        or doc.get("company")
    )
    return {
        "cik": seed_row.get("cik")
        or doc.get("cik")
        or _ISSUER_CIK.get(str(ticker)),
        "accession": seed_row.get("accession") or doc.get("accession"),
        "filing_date": seed_row.get("filing_date") or doc.get("filing_date"),
        "form": seed_row.get("form") or doc.get("form") or doc.get("doc_type"),
        "ticker": ticker,
    }


def update_catalog_fields(
    *,
    manifest_path: Path,
    catalog_seed: Mapping[str, Mapping[str, str]] | None = None,
) -> int:
    """Emit / update the EDGAR-shaped catalog fields on every manifest
    row and return the number of rows that changed.

    This is the local ingest path's catalog step (spec § Success
    Criteria: "Ingest CLI over existing corpus can emit/update catalog
    fields for ``AAPL-2023Q3`` without SEC GET"). It reads only the
    manifest's own local metadata plus ``catalog_seed`` — no SEC host
    is opened here, and no other module on this path imports
    ``urllib``.

    The drift check is semantic, not byte-level: when no row's fields
    change the file is not rewritten at all, so an ingest run over an
    already-complete manifest leaves its bytes (and its hand-authored
    formatting) untouched.
    """
    payload = json.loads(manifest_path.read_text())
    seed = catalog_seed or {}
    changed_rows = 0
    for doc in payload["documents"]:
        resolved = _catalog_fields_for(doc, seed.get(doc["document_id"], {}))
        row_changed = False
        for key in _EDGAR_CATALOG_FIELDS:
            value = resolved[key]
            if key not in doc or doc[key] != value:
                doc[key] = value
                row_changed = True
        if row_changed:
            changed_rows += 1
    if changed_rows:
        _atomic_write(
            manifest_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n",
        )
    return changed_rows


def _prepare_state_job(
    state_store: IngestStateStore,
    document_id: str,
) -> bool:
    """Create a job, or identify an already-ready job for a rerun."""
    try:
        state_store.create_job(document_id)
    except DuplicateJobError:
        state = state_store.get_job(document_id).state
        if state is IngestState.READY:
            return True
        if state is IngestState.FAILED:
            return False
        # An active job belongs to another caller. Keep the original
        # duplicate/busy signal instead of claiming it.
        raise
    return False


def _structure_units_payload(units: tuple[StructureUnit, ...]) -> bytes:
    """Serialize structure units without changing their frozen text."""
    return json.dumps(
        {
            "units": [
                {
                    "locator": {
                        "document_id": unit.locator.document_id,
                        "version": unit.locator.version,
                        "unit": unit.locator.unit,
                    },
                    "text": unit.text,
                    "title": unit.title,
                    "parent_unit": unit.parent_unit,
                }
                for unit in units
            ]
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
    catalog_seed: Mapping[str, Mapping[str, str]] | None = None,
    catalog_seed_path: Path | None = None,
    object_store: ObjectStore | None = None,
    metadata_store: MetadataStore | None = None,
    ingest_state_store: IngestStateStore | None = None,
    structure_parser: StructureParser | None = None,
    max_structure_units: int = 1000,
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
    catalog_seed:
        In-process ``{document_id: {edgar_field: value}}`` seed for
        the catalog step (see ``update_catalog_fields``). Mutually
        exclusive with ``catalog_seed_path``.
    catalog_seed_path:
        Path to the operator's ``--catalog-seed`` JSON file. Mutually
        exclusive with ``catalog_seed``.
    object_store:
        Optional raw-byte store. When supplied, verified PDF bytes are
        written under ``filings/<document_id>/<filename>`` and the
        resulting pointer is recorded in the manifest and metadata
        store. When omitted, the local PDF directory is the documented
        fallback and remains the source for extraction.
    metadata_store:
        Store for the durable document-to-object pointer. When raw
        object storage is enabled and this is omitted, the configured
        Postgres store is selected from the environment, or an
        in-memory store is used for local tests.
    ingest_state_store:
        Optional state/audit store. When omitted, an offline in-memory
        store is used. Each local PDF job is advanced through the
        documented state machine to ``READY``.
    structure_parser:
        Optional parser adapter for structure-aware units. When omitted,
        the existing PDF extraction stack supplies the default parser;
        no alternate splitter is introduced.
    max_structure_units:
        Positive cap on structure units emitted per document.

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
    CatalogSeedError
        If ``catalog_seed_path`` names a file that cannot be read or
        parsed as a seed.

    IngestMissingError
        Otherwise not raised here — see
        ``fin.corpus.extracted.load_extracted_tree`` for the read-side
        check.
    """
    if facts_root is None:
        facts_root = Path(__file__).resolve().parents[3] / "data" / "corpus" / "facts"
    if catalog_seed is not None and catalog_seed_path is not None:
        raise ValueError(
            "pass either catalog_seed or catalog_seed_path, not both"
        )
    if catalog_seed_path is not None:
        catalog_seed = load_catalog_seed(catalog_seed_path)
    pointer_store = (
        metadata_store or default_metadata_store()
        if object_store is not None
        else None
    )
    state_store = ingest_state_store or default_ingest_state_store()

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
    manifest_updated = False
    # Start from the entries this run has no authority over. The
    # manifest is the SSOT for the frozen PDFs only; documents the
    # gated live path froze into the same tree (keyed by accession)
    # must survive a local re-ingest, or the two writers take turns
    # deleting each other's catalog.
    index: dict[str, dict[str, object]] = {
        document_id: entry
        for document_id, entry in (existing_index or {}).items()
        if document_id not in {doc["document_id"] for doc in documents}
    }

    # Sort by document_id so on-disk ordering is deterministic across
    # runs and machines.
    reused_ready_jobs: dict[str, bool] = {}
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        document_id: str = doc["document_id"]
        filename: str = doc["filename"]
        expected_sha: str = doc["sha256"]
        pdf_path = pdf_dir / filename
        doc_dir = extracted_root / document_id
        doc_dir_existed = doc_dir.exists()
        reused_ready = _prepare_state_job(state_store, document_id)
        reused_ready_jobs[document_id] = reused_ready
        if not reused_ready:
            state_store.transition(
                document_id,
                IngestState.FETCHING,
                metadata={"filename": filename},
            )

        try:
            if not pdf_path.exists():
                # The local re-ingest path does not own EDGAR iXBRL
                # primary documents. Preserve a matching live entry
                # already frozen by ``fin.corpus.live_fetch``.
                existing_doc = (
                    existing_index.get(document_id) if existing_index else None
                )
                if (
                    existing_doc is not None
                    and existing_doc.get("version") == expected_sha
                    and isinstance(existing_doc.get("page_count"), int)
                ):
                    index[document_id] = {
                        "version": expected_sha,
                        "page_count": int(existing_doc["page_count"]),
                    }
                    pages_total += int(existing_doc["page_count"])
                    pages_skipped += int(existing_doc["page_count"])
                    if not reused_ready:
                        state_store.transition(
                            document_id, IngestState.EXTRACTING
                        )
                        state_store.transition(
                            document_id, IngestState.CATALOGING
                        )
                    continue
                raise ManifestShaMismatchError(
                    f"missing frozen PDF for {document_id}: {pdf_path}"
                )

            raw_bytes: bytes | None = None
            if object_store is not None:
                # Hash and upload the same buffer so the pointer cannot
                # describe bytes other than those verified for parsing.
                raw_bytes = pdf_path.read_bytes()
                actual_sha = _sha256_bytes(raw_bytes)
            else:
                actual_sha = _sha256_file(pdf_path)
            if actual_sha != expected_sha:
                raise ManifestShaMismatchError(
                    f"sha256 mismatch for {document_id}: "
                    f"manifest={expected_sha} actual={actual_sha}"
                )
            if object_store is not None:
                assert raw_bytes is not None
                raw_key = f"filings/{document_id}/{filename}"
                pointer = object_store.put(
                    raw_key,
                    raw_bytes,
                    content_type="application/pdf",
                )
                if pointer.sha256 != expected_sha or pointer.size_bytes != len(
                    raw_bytes
                ):
                    raise ObjectStoreUnavailableError(
                        f"object store returned an invalid pointer for "
                        f"{document_id}: key={pointer.key!r}"
                    )
                if pointer_store is None:
                    raise ObjectStoreUnavailableError(
                        "raw object storage has no metadata pointer store"
                    )
                pointer_store.put_pointer(document_id, pointer)
                raw_object = {
                    "key": pointer.key,
                    "size_bytes": pointer.size_bytes,
                    "sha256": pointer.sha256,
                    "content_type": pointer.content_type,
                }
                if doc.get("raw_object") != raw_object:
                    doc["raw_object"] = raw_object
                    manifest_updated = True

            if not reused_ready:
                state_store.transition(
                    document_id,
                    IngestState.EXTRACTING,
                    metadata={"sha256": expected_sha},
                )
            doc_dir.mkdir(parents=True, exist_ok=True)

            existing_doc = (
                existing_index.get(document_id) if existing_index else None
            )
            if (
                existing_doc is not None
                and existing_doc.get("version") == expected_sha
                and isinstance(existing_doc.get("page_count"), int)
                and isinstance(existing_doc.get("unit_count"), int)
                and (doc_dir / "units.json").is_file()
                and _artifacts_present(doc_dir, int(existing_doc["page_count"]))
            ):
                # Fast-skip: trust the index and existence checks.
                page_count = int(existing_doc["page_count"])
                index[document_id] = {
                    "version": expected_sha,
                    "page_count": page_count,
                    "unit_count": int(existing_doc["unit_count"]),
                }
                pages_total += page_count
                pages_skipped += page_count
                if not reused_ready:
                    state_store.transition(
                        document_id, IngestState.CATALOGING
                    )
                continue

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
            units = parse_structure_units(
                document_id=document_id,
                version=expected_sha,
                source=pdf_path,
                parser=structure_parser,
                max_units=max_structure_units,
            )
            _write_if_drifted(
                doc_dir / "units.json",
                _structure_units_payload(units),
            )

            pages_written += pages_written_delta
            pages_skipped += pages_skipped_delta
            pages_total += pages_this_doc

            doc_index_entry: dict[str, object] = {
                "version": expected_sha,
                "page_count": pages_this_doc,
                "unit_count": len(units),
            }
            if ocr_applied_pages:
                doc_index_entry["ocr_applied"] = True
                doc_index_entry["ocr_applied_pages"] = sorted(ocr_applied_pages)
            index[document_id] = doc_index_entry
            if not reused_ready:
                if ocr_applied_pages:
                    state_store.transition(document_id, IngestState.OCR)
                state_store.transition(document_id, IngestState.CATALOGING)
        except Exception as exc:
            # Remove only a newly-created partial tree. Existing committed
            # artifacts must survive a failed verification retry.
            if not doc_dir_existed and doc_dir.exists():
                shutil.rmtree(doc_dir)
            if not reused_ready:
                try:
                    state_store.fail_step(
                        document_id, step="ingest", cause=exc
                    )
                except IngestStepError as state_error:
                    # Keep the established ingest error as the public
                    # failure while retaining the audit row.
                    raise exc from state_error
            raise

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

    if manifest_updated:
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n",
        )

    # Catalog step: only after every document extracted cleanly, so a
    # run that dies on a sha mismatch or an OCR-required unit does not
    # leave a catalog row claiming a filing this run never wrote.
    try:
        catalog_updated = update_catalog_fields(
            manifest_path=manifest_path, catalog_seed=catalog_seed
        )
    except Exception as exc:
        failures: list[IngestStepError] = []
        for doc in sorted(documents, key=lambda d: d["document_id"]):
            document_id = str(doc["document_id"])
            if reused_ready_jobs.get(document_id):
                continue
            try:
                state_store.fail_step(
                    document_id,
                    step="catalog",
                    cause=exc,
                )
            except IngestStepError as state_error:
                failures.append(state_error)
        if failures:
            raise failures[0] from exc
        raise

    # Seed the fact table for every document. The seed list is a
    # per-document static mapping in ``facts_seed`` — T1 only ships
    # the AAPL-2023Q3 row (the v1 gold span); other documents get an
    # empty fact list, which is a valid "no facts yet" catalog state.
    # ``write_facts`` is byte-idempotent, so a re-run that hits the
    # fast-skip path above still writes the same fact table.
    for doc in sorted(documents, key=lambda d: d["document_id"]):
        document_id: str = doc["document_id"]
        expected_sha: str = doc["sha256"]
        try:
            seed = seed_facts_for_document(
                document_id=document_id, version=expected_sha
            )
            if seed is not None:
                write_facts(
                    document_id=document_id, facts=seed, facts_root=facts_root
                )
        except Exception as exc:
            if not reused_ready_jobs.get(document_id):
                state_store.fail_step(
                    document_id,
                    step="fact_seeding",
                    cause=exc,
                )
            raise
        if not reused_ready_jobs.get(document_id):
            state_store.transition(document_id, IngestState.READY)

    return IngestResult(
        documents=len(documents),
        pages=pages_total,
        pages_written=pages_written,
        pages_skipped=pages_skipped,
        catalog_updated=catalog_updated,
        raw_storage=(
            "object_store" if object_store is not None else "local_fallback"
        ),
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


def _resolve_object_store() -> ObjectStore | None:
    """Resolve raw storage when its endpoint and bucket are configured."""
    endpoint = (
        os.environ.get("FIN_OBJECT_STORE_URL")
        or os.environ.get("FIN_OBJECT_STORE_ENDPOINT")
        or ""
    ).strip()
    bucket = os.environ.get("FIN_OBJECT_STORE_BUCKET", "").strip()
    if not endpoint and not bucket:
        # Documented local fallback: the verified local PDF remains the
        # extraction source and no raw bytes are silently discarded.
        return None
    from fin.corpus.object_store import object_store_from_env

    return object_store_from_env(environ=os.environ)


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
        "--catalog-seed",
        type=Path,
        default=None,
        help=(
            "JSON file of EDGAR-shaped catalog values to write onto "
            "the manifest rows, shaped "
            '{"documents": {"<document_id>": {"accession": ..., '
            '"filing_date": ..., "cik": ..., "form": ..., '
            '"ticker": ...}}}. Read locally — this CLI never opens a '
            "SEC host; the gated live fetch is "
            "`python -m fin.corpus.live_fetch`."
        ),
    )
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
    try:
        object_store = _resolve_object_store()
        pointer_store = (
            default_metadata_store() if object_store is not None else None
        )
    except ObjectStoreError as exc:
        print(f"ingest: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        result = run_ingest(
            manifest_path=args.manifest,
            pdf_dir=args.pdf_dir,
            extracted_root=args.extracted_root,
            facts_root=args.facts_root,
            ocr_engine=ocr_engine,
            catalog_seed_path=args.catalog_seed,
            object_store=object_store,
            metadata_store=pointer_store,
        )
    except (MalformedExtractedIndexError, ObjectStoreError) as exc:
        print(f"ingest: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"ingest: documents={result.documents} "
        f"pages={result.pages} "
        f"written={result.pages_written} "
        f"skipped={result.pages_skipped} "
        f"catalog_updated={result.catalog_updated} "
        f"raw_storage={result.raw_storage}"
    )
    return 0


__all__ = [
    "CatalogSeedError",
    "IngestResult",
    "MalformedExtractedIndexError",
    "load_catalog_seed",
    "run_ingest",
    "update_catalog_fields",
    "main",
    "_resolve_ocr_engine",
    "_resolve_object_store",
]


if __name__ == "__main__":
    sys.exit(main())