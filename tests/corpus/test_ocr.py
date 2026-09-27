"""Tests for the corpus OCR write-back interface (T4 / S1).

The spec contract (S1 / T4 acceptance):

* OCR-available fixture writes non-empty frozen text and records
  ``ocr_applied``.
* OCR-required + engine unavailable fails that unit
  (``ocr_required_unavailable``).
* Default pytest does not require a live OCR engine if the miss
  path is scripted.

The OCR interface lives at ``fin.corpus.ocr``. It is small and
self-contained: an ``OCREngine`` ``Protocol`` plus a lazy-imported
``TesseractEngine`` and a typed ``OcrRequiredUnavailableError``. The
ingest command consumes the interface, not a specific engine, so the
default pytest uses an in-process ``MockOCREngine`` and never imports
``pytesseract``.

The five boundary classes from the defensive-contract-validator:

* empty  — engine returns empty text for an OCR-required unit ->
  ``OcrRequiredUnavailableError``.
* negative — engine raises ``OCRError`` mid-ingest ->
  ``OcrRequiredUnavailableError`` (no silent skip).
* overflow — large page list runs to completion with OCR applied to
  every page.
* concurrent — N/A single-writer per spec; the read-side doesn't
  share OCR state.
* exception — OCR required + engine missing ->
  ``OcrRequiredUnavailableError``; OCR required + engine raises ->
  ``OcrRequiredUnavailableError``.
"""

from __future__ import annotations

import inspect
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from fin.corpus.ingest import run_ingest
from fin.corpus.ocr import (
    OCRError,
    OcrRequiredUnavailableError,
    detect_ocr_required,
)


# ---------- helpers ----------


def _project_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _real_manifest() -> tuple[Path, Path, list[dict]]:
    data_dir = _project_data_dir()
    manifest_path = data_dir / "manifest.json"
    pdf_dir = data_dir / "pdf"
    documents = json.loads(manifest_path.read_text())["documents"]
    return manifest_path, pdf_dir, documents


def _copy_pdf(src: Path, dst: Path) -> str:
    """Copy ``src`` to ``dst``; return the sha256 of the copy."""
    import hashlib

    shutil.copyfile(src, dst)
    h = hashlib.sha256()
    h.update(dst.read_bytes())
    return h.hexdigest()


def _write_manifest(manifest_path: Path, documents: list[dict]) -> None:
    payload = {
        "upstream": {
            "repo": "test",
            "commit": "test",
            "path": "test",
            "license": "test",
        },
        "tickers": [],
        "periods": [],
        "note": "ocr-test fixture",
        "documents": documents,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload))


def _stage_one_doc_ingest(
    tmp_path: Path,
    *,
    sample_index: int = 0,
) -> tuple[Path, Path, dict]:
    """Stage a one-document ingest fixture under ``tmp_path``.

    Returns ``(test_pdf_dir, test_manifest, sample)`` where
    ``sample`` is the manifest dict entry used. The real AAPL-2022Q3
    PDF is copied into ``tmp_path/pdf`` and a one-doc manifest is
    written at ``tmp_path/manifest.json`` with the actual sha.

    Plan C added EDGAR iXBRL primary docs (html_live kind); the local
    re-ingest fixture only owns PDF-kind entries, so skip the html_live
    ones when picking a sample.
    """
    _, pdf_dir, documents = _real_manifest()
    pdf_documents = [
        d for d in documents if str(d.get("filename", "")).lower().endswith(".pdf")
    ]
    sample = pdf_documents[sample_index]
    test_pdf_dir = tmp_path / "pdf"
    test_pdf_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = test_pdf_dir / sample["filename"]
    actual_sha = _copy_pdf(pdf_dir / sample["filename"], test_pdf)
    test_manifest = tmp_path / "manifest.json"
    _write_manifest(
        test_manifest,
        documents=[
            {
                "document_id": sample["document_id"],
                "filename": sample["filename"],
                "sha256": actual_sha,
            }
        ],
    )
    return test_pdf_dir, test_manifest, sample


# ---------- engine protocol ----------


def test_ocr_engine_protocol_is_structural() -> None:
    """A class with a single ``recognize(source=..., page=...)`` method
    is structurally conformant with the ``OCREngine`` Protocol.

    This is the v1 contract: any object that exposes
    ``recognize(source: Path, page: int) -> str`` can be passed to
    ``run_ingest``. Real engines (Tesseract) and test doubles
    (``MockOCREngine``) both satisfy it without inheriting from
    ``OCREngine``.
    """

    class _Stub:
        def recognize(self, *, source: Path, page: int) -> str:
            return f"stub text for {source.name} page {page}"

    stub = _Stub()
    assert stub.recognize(source=Path("x.pdf"), page=1) == (
        "stub text for x.pdf page 1"
    )
    # The Protocol only constrains the surface; we don't try to
    # isinstance-check it (runtime_checkable is intentionally
    # avoided — see module docstring of fin.corpus.ocr).
    sig = inspect.signature(_Stub.recognize)
    assert list(sig.parameters) == ["self", "source", "page"]


# ---------- detection heuristic ----------


def test_detect_ocr_required_short_text_with_images() -> None:
    """Empty/short text + images present => OCR required."""
    assert detect_ocr_required(text="", image_count=3, min_text_chars=100) is True


def test_detect_ocr_required_long_text_no_ocr() -> None:
    """Long text-layer extraction suppresses the OCR requirement."""
    assert detect_ocr_required(
        text="x" * 200, image_count=3, min_text_chars=100
    ) is False


def test_detect_ocr_required_no_images_no_ocr() -> None:
    """No images on the page => even empty text is NOT OCR-required.

    A pure blank-text page with no embedded image is the operator's
    problem (e.g., an empty cover sheet), not an OCR target.
    """
    assert detect_ocr_required(text="", image_count=0, min_text_chars=100) is False


def test_detect_ocr_required_short_text_no_images() -> None:
    """Both terms must hold: a short text layer alone is not enough."""
    assert detect_ocr_required(
        text="x" * 99, image_count=0, min_text_chars=100
    ) is False
    assert detect_ocr_required(
        text="x" * 100, image_count=0, min_text_chars=100
    ) is False


def test_detect_ocr_required_boundary_at_the_named_threshold() -> None:
    """The boundary is exclusive: under the threshold fires, at it does not."""
    assert detect_ocr_required(
        text="x" * 99, image_count=1, min_text_chars=100
    ) is True
    assert detect_ocr_required(
        text="x" * 100, image_count=1, min_text_chars=100
    ) is False


def test_detect_ocr_required_custom_min_text_chars() -> None:
    """Operator-tunable threshold for tighter/looser detection."""
    assert detect_ocr_required(
        text="x" * 50, image_count=1, min_text_chars=40
    ) is False
    assert detect_ocr_required(
        text="x" * 50, image_count=1, min_text_chars=60
    ) is True


def test_detect_ocr_required_threshold_has_no_default() -> None:
    """A caller that does not name the threshold is refused, not defaulted.

    The lanes price this number differently (a paragraph for the page rule,
    "no text layer at all" for the scanned-page rule), so a default here
    would be a second home for a threshold the caller already owns — and it
    would be the quieter of the two when they disagreed.
    """
    with pytest.raises(TypeError):
        detect_ocr_required(text="", image_count=1)
    signature = inspect.signature(detect_ocr_required)
    parameter = signature.parameters["min_text_chars"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ---------- mock engine ----------


@dataclass
class MockOCREngine:
    """In-process OCR engine for tests; not part of the product API.

    ``default_text`` is returned for any page not in ``text_by_page``;
    the test corpus AAPL PDF has 28 pages, so a fixed
    ``text_by_page`` dict alone leaves the later pages empty.
    """

    text_by_page: dict[int, str] | None = None
    default_text: str = "DEFAULT OCR TEXT FOR PAGE {page}"
    raises: type[BaseException] | None = None

    def recognize(self, *, source: Path, page: int) -> str:
        if self.raises is not None:
            raise self.raises("simulated OCR failure")
        if self.text_by_page is not None and page in self.text_by_page:
            return self.text_by_page[page]
        return self.default_text.format(page=page)


# ---------- ingest integration: exception boundary ----------


def test_ocr_required_no_engine_raises_typed_exit(tmp_path: Path) -> None:
    """``run_ingest`` with ``ocr_required_marker`` set and no engine
    raises ``OcrRequiredUnavailableError`` (typed EXIT).

    Per spec: "OCR-required + engine unavailable fails that unit
    (``ocr_required_unavailable``) ... not silent skip". The
    extracted tree for that document must NOT be marked complete in
    ``_index.json`` — the typed error short-circuits before
    completion.
    """
    test_pdf_dir, test_manifest, sample = _stage_one_doc_ingest(tmp_path)
    extracted_root = tmp_path / "extracted"

    with pytest.raises(OcrRequiredUnavailableError):
        run_ingest(
            manifest_path=test_manifest,
            pdf_dir=test_pdf_dir,
            extracted_root=extracted_root,
            ocr_engine=None,
            ocr_required_marker="ocr-required",
        )

    # The failed document must NOT appear as complete in the index.
    # We assert via the file system: no doc dir, no _index.json.
    doc_dir = extracted_root / sample["document_id"]
    assert not doc_dir.exists(), (
        "OCR-required unit wrote an extracted tree despite engine "
        "being unavailable (silent skip)"
    )
    assert not (extracted_root / "_index.json").exists(), (
        "OCR-required unit wrote _index.json despite engine being "
        "unavailable (silent skip)"
    )


def test_ocr_required_engine_raises_typed_error_propagates(
    tmp_path: Path,
) -> None:
    """When the OCR engine raises ``OCRError`` mid-ingest, the
    ``run_ingest`` call converts it to ``OcrRequiredUnavailableError``
    (no silent skip, no empty frozen text).
    """
    test_pdf_dir, test_manifest, _ = _stage_one_doc_ingest(tmp_path)
    extracted_root = tmp_path / "extracted"
    failing_engine = MockOCREngine(text_by_page={}, raises=OCRError)

    with pytest.raises(OcrRequiredUnavailableError):
        run_ingest(
            manifest_path=test_manifest,
            pdf_dir=test_pdf_dir,
            extracted_root=extracted_root,
            ocr_engine=failing_engine,
            ocr_required_marker="ocr-required",
        )


def test_ocr_required_engine_returns_empty_raises_typed_error(
    tmp_path: Path,
) -> None:
    """When the OCR engine returns an empty string, the call still
    raises ``OcrRequiredUnavailableError`` — per spec "OCR-required
    unit must not succeed with empty frozen text".
    """
    test_pdf_dir, test_manifest, _ = _stage_one_doc_ingest(tmp_path)
    extracted_root = tmp_path / "extracted"
    empty_engine = MockOCREngine(text_by_page={}, default_text="")

    with pytest.raises(OcrRequiredUnavailableError):
        run_ingest(
            manifest_path=test_manifest,
            pdf_dir=test_pdf_dir,
            extracted_root=extracted_root,
            ocr_engine=empty_engine,
            ocr_required_marker="ocr-required",
        )


# ---------- ingest integration: success path ----------


def test_ocr_required_with_mock_engine_writes_text(tmp_path: Path) -> None:
    """OCR-available unit + mock engine writes OCR text into the
    frozen locator unit AND records ``ocr_applied: True`` per page.

    The OCR text the engine returns must be the frozen text on the
    page (it is what the citation verifier will check against).
    """
    test_pdf_dir, test_manifest, sample = _stage_one_doc_ingest(tmp_path)
    extracted_root = tmp_path / "extracted"
    test_pdf = test_pdf_dir / sample["filename"]

    engine = MockOCREngine(default_text="OCR PAGE TEXT")

    run_ingest(
        manifest_path=test_manifest,
        pdf_dir=test_pdf_dir,
        extracted_root=extracted_root,
        ocr_engine=engine,
        ocr_required_marker="ocr-required",
    )

    # Every page file must carry ocr_applied=True and the OCR text.
    page_files = sorted((extracted_root / sample["document_id"]).glob("page-*.json"))
    assert page_files, "OCR run wrote no pages"

    for page_path in _assert_ocr_page_written(page_files, engine, test_pdf):
        # ``_assert_ocr_page_written`` already asserts the page-level
        # invariants; the loop is for readability / future hooks.
        assert page_path.exists()


def _assert_ocr_page_written(
    page_files: list[Path], engine: MockOCREngine, source_pdf: Path
) -> list[Path]:
    """Assert every OCR'd page carries ``ocr_applied=True`` and the
    OCR text the engine returned. Returns the same list so callers
    can chain additional assertions.
    """
    for page_path in page_files:
        payload = json.loads(page_path.read_text())
        assert payload["ocr_applied"] is True, (
            f"{page_path.name} missing ocr_applied=True"
        )
        assert payload["text"].strip(), (
            f"{page_path.name} has empty frozen text"
        )
        expected = engine.recognize(source=source_pdf, page=payload["page"])
        assert expected in payload["text"], (
            f"{page_path.name} text does not contain the OCR "
            f"output for page {payload['page']}"
        )
    return page_files


def test_ocr_applied_recorded_in_index(tmp_path: Path) -> None:
    """After a successful OCR run, ``_index.json`` carries
    ``ocr_applied_pages`` for the OCR'd document.

    The accumulated list lets the host graph / ask path know which
    locator units came from OCR vs pdfplumber — useful for downstream
    trust signal (the OCR text is harder to cite).
    """
    test_pdf_dir, test_manifest, sample = _stage_one_doc_ingest(tmp_path)
    extracted_root = tmp_path / "extracted"
    engine = MockOCREngine(default_text="OCR TEXT")

    run_ingest(
        manifest_path=test_manifest,
        pdf_dir=test_pdf_dir,
        extracted_root=extracted_root,
        ocr_engine=engine,
        ocr_required_marker="ocr-required",
    )

    index_payload = json.loads((extracted_root / "_index.json").read_text())
    doc_record = index_payload["documents"][sample["document_id"]]
    assert doc_record.get("ocr_applied") is True
    ocr_pages = doc_record.get("ocr_applied_pages")
    assert isinstance(ocr_pages, list)
    assert len(ocr_pages) >= 1
    assert all(isinstance(n, int) and n >= 1 for n in ocr_pages)
    assert ocr_pages == sorted(ocr_pages)


def test_ocr_not_applied_when_text_layer_succeeds(tmp_path: Path) -> None:
    """When the text-layer extraction yields plenty of text, OCR is
    NOT invoked even if an engine is supplied. The index has no
    ``ocr_applied_pages`` for that document.
    """
    test_pdf_dir, test_manifest, sample = _stage_one_doc_ingest(tmp_path)
    extracted_root = tmp_path / "extracted"

    # Engine that raises if ever called.
    class _SentinelEngine:
        call_count = 0

        def recognize(self, *, source: Path, page: int) -> str:
            type(self).call_count += 1
            raise AssertionError(
                "OCR engine must NOT be invoked when text-layer "
                "extraction succeeds"
            )

    sentinel = _SentinelEngine()

    run_ingest(
        manifest_path=test_manifest,
        pdf_dir=test_pdf_dir,
        extracted_root=extracted_root,
        ocr_engine=sentinel,
    )

    assert _SentinelEngine.call_count == 0, (
        "OCR engine was invoked despite text-layer extraction being "
        "sufficient"
    )

    index_payload = json.loads((extracted_root / "_index.json").read_text())
    doc_record = index_payload["documents"][sample["document_id"]]
    assert "ocr_applied_pages" not in doc_record or not doc_record[
        "ocr_applied_pages"
    ]
    assert doc_record.get("ocr_applied") is not True


# ---------- overflow boundary ----------


def test_ocr_required_large_page_count_applies_to_every_page(
    tmp_path: Path,
) -> None:
    """Overflow boundary: a 28-page real PDF (AAPL-2022Q3) with
    ``ocr_required_marker`` set must apply OCR to every page and
    record every page number in ``_index.json['ocr_applied_pages']``.

    This is the defensive-contract-validator's "overflow" class —
    every page in a large document runs through the OCR path without
    silently skipping any.
    """
    test_pdf_dir, test_manifest, sample = _stage_one_doc_ingest(
        tmp_path, sample_index=0
    )
    extracted_root = tmp_path / "extracted"
    test_pdf = test_pdf_dir / sample["filename"]

    engine = MockOCREngine(default_text="OCR OVERFLOW TEXT")

    result = run_ingest(
        manifest_path=test_manifest,
        pdf_dir=test_pdf_dir,
        extracted_root=extracted_root,
        ocr_engine=engine,
        ocr_required_marker="ocr-required",
    )

    # AAPL-2022Q3 has 28 pages — assert OCR ran for every one.
    assert result.pages == 28, f"expected 28 pages, got {result.pages}"

    doc_dir = extracted_root / sample["document_id"]
    page_files = sorted(doc_dir.glob("page-*.json"))
    assert len(page_files) == 28

    _assert_ocr_page_written(page_files, engine, test_pdf)

    index_payload = json.loads((extracted_root / "_index.json").read_text())
    doc_record = index_payload["documents"][sample["document_id"]]
    ocr_pages = doc_record["ocr_applied_pages"]
    assert len(ocr_pages) == 28
    assert ocr_pages == list(range(1, 29))


# ---------- default pytest without OCR ----------


def test_default_pytest_does_not_import_pytesseract() -> None:
    """Importing ``fin.corpus.ocr`` MUST NOT pull pytesseract.

    Per spec: "default pytest does not require a live OCR engine".
    ``TesseractEngine`` lazily imports pytesseract inside ``__init__``;
    instantiating the engine without that env raises the typed error.
    Here we only import the module and assert pytesseract is not in
    ``sys.modules``.
    """
    import sys
    import fin.corpus.ocr  # noqa: F401  (the import IS the test)

    leaked = sorted(
        m for m in sys.modules if m == "pytesseract" or m.startswith("pytesseract.")
    )
    assert leaked == [], (
        f"fin.corpus.ocr leaked pytesseract on import: {leaked}"
    )


def test_tesseract_engine_raises_typed_when_pytesseract_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pytesseract is not installed, ``TesseractEngine`` raises
    the typed ``OcrRequiredUnavailableError`` on ``recognize``.

    This keeps the contract: missing optional extra => typed EXIT,
    no silent ``"".
    """
    # Block the pytesseract import for the duration of this test.
    monkeypatch.setitem(
        __import__("sys").modules,
        "pytesseract",
        None,
    )

    from fin.corpus.ocr import TesseractEngine

    engine = TesseractEngine()
    with pytest.raises(OcrRequiredUnavailableError):
        engine.recognize(source=tmp_path / "anything.pdf", page=1)


def test_tesseract_engine_default_lang_includes_simplified_chinese() -> None:
    """Default Tesseract langs cover mixed CN/EN scans (chi_sim+eng).

    English-only default would drop 年报扫描件. Callers may still pass
    ``lang=``; ingest's TesseractEngine() uses this default.
    """
    from fin.corpus.ocr import TesseractEngine

    engine = TesseractEngine()
    assert engine.lang == "chi_sim+eng"


# ---------- ingest with no OCR configured stays on text-layer path ----------


def test_run_ingest_default_does_not_require_ocr_engine(tmp_path: Path) -> None:
    """``run_ingest`` with no OCR args over the real corpus writes the
    locator tree normally (no OCR invoked, no OCR error raised).

    This is the "OCR available fixture writes non-empty frozen text"
    half of the T4 acceptance — the real AAPL PDF yields plenty of
    text without OCR.
    """
    manifest_path, pdf_dir, _ = _real_manifest()
    # Reuse a one-document manifest pointing at AAPL-2023Q3 (Plan C
    # added EDGAR iXBRL primary docs that sort before AAPL-2023Q3, so
    # locate by document_id rather than by position).
    sample_doc = next(
        d
        for d in json.loads(manifest_path.read_text())["documents"]
        if d["document_id"] == "AAPL-2023Q3"
    )
    test_manifest = tmp_path / "manifest.json"
    _write_manifest(test_manifest, documents=[sample_doc])
    extracted_root = tmp_path / "extracted"

    result = run_ingest(
        manifest_path=test_manifest,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )
    assert result.documents == 1
    assert result.pages >= 1

    index_payload = json.loads((extracted_root / "_index.json").read_text())
    doc_record = index_payload["documents"][sample_doc["document_id"]]
    assert "ocr_applied_pages" not in doc_record or not doc_record[
        "ocr_applied_pages"
    ]


# ---------- error class identity ----------


def test_ocr_required_unavailable_is_subclass_of_ocr_error() -> None:
    """``OcrRequiredUnavailableError`` MUST be a subclass of
    ``OCRError`` so callers can catch the family.
    """
    assert issubclass(OcrRequiredUnavailableError, OCRError)
    assert isinstance(OcrRequiredUnavailableError("x"), OCRError)


def test_ocr_error_is_distinct_from_ingest_missing_error() -> None:
    """OCR errors are NOT the same family as ingest-missing errors.

    The two failure modes are different: an OCR-unavailable unit
    may have a healthy manifest + PDF; only the OCR path is blocked.
    Conflating them would force callers to re-extract instead of
    fixing the OCR engine / installing the extra.
    """
    from fin.corpus.errors import IngestMissingError

    assert not issubclass(OcrRequiredUnavailableError, IngestMissingError)
    assert not issubclass(OCRError, IngestMissingError)