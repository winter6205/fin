"""Algorithmic OCR for ingest (T4 / S1).

This module owns the small OCR interface the ingest command
consumes. It is intentionally narrow:

* An ``OCREngine`` ``Protocol`` describing one call:
  ``recognize(*, source: Path, page: int) -> str``. Production
  implements it with ``TesseractEngine`` (lazy-imported
  ``pytesseract``); tests use ``MockOCREngine`` defined in
  ``tests/corpus/test_ocr.py``.
* A typed ``OCRError`` hierarchy. ``OcrRequiredUnavailableError``
  is the spec's ``ocr_required_unavailable`` EXIT: the unit needs
  OCR and either the engine is missing OR the OCR call failed OR
  the engine returned empty text. Per spec § Boundaries: "OCR is
  required (collapsed/no text-layer + images) AND engine/extra is
  missing OR OCR fails -> ingest does NOT succeed that unit with
  empty frozen text; typed non-zero / error, not silent skip."
* A ``detect_ocr_required`` heuristic that the ingest command
  calls per page to decide whether OCR must run.

Why a module and not a class hierarchy across the host graph:
the spec (CONTEXT ``ingest``: "A CLI that writes frozen locator-unit
text from official filings") keeps OCR inside the corpus capability
slice; it is an ingest-time write-back, not a runtime tool. The
interface here is a single call per page, no streaming, no callback
plumbing — the simplest shape that satisfies the contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Protocol


class OCREngine(Protocol):
    """Recognize text from a single page of ``source``.

    ``source`` is the original PDF (or scanned image) path; ``page``
    is the 1-based page number. The engine returns the recognized
    text — non-empty for a successful OCR pass, empty (or an
    ``OCRError``) for a failure. The ingest command converts both
    failure shapes into ``OcrRequiredUnavailableError`` so the typed
    EXIT surface stays consistent.
    """

    def recognize(self, *, source: Path, page: int) -> str: ...


class OCRError(Exception):
    """Base class for OCR failures."""


class OcrRequiredUnavailableError(OCRError):
    """OCR is required (collapsed text-layer + images) AND engine is
    missing OR OCR call failed OR OCR returned empty text.

    Per spec § Boundaries: "ingest does NOT succeed that unit with
    empty frozen text; typed non-zero / error, not silent skip."
    The ingest command raises this on every OCR-required failure
    shape so the host graph / CLI can refuse cleanly.
    """

    code = "ocr_required_unavailable"


# Mixed CN/EN scans. OS tessdata must include ``chi_sim`` and ``eng``.
DEFAULT_TESSERACT_LANG: Final[str] = "chi_sim+eng"


class TesseractEngine:
    """Production OCR engine backed by ``pytesseract``.

    The import is lazy: ``pytesseract`` is an optional
    ``[project.optional-dependencies] ocr`` extra. When it is not
    installed (the default pytest path), ``__init__`` succeeds but
    every ``recognize`` call raises the typed
    ``OcrRequiredUnavailableError`` so callers can fail fast without
    us swallowing the missing-extra condition.

    Default ``lang`` is :data:`DEFAULT_TESSERACT_LANG` (``chi_sim+eng``).
    """

    def __init__(self, *, lang: str = DEFAULT_TESSERACT_LANG) -> None:
        # Lazy import: do not pull pytesseract on module import
        # (default pytest contract — see ``test_default_pytest_does_not_import_pytesseract``).
        try:
            import pytesseract  # type: ignore[import-untyped]  # noqa: F401
        except Exception as exc:  # pragma: no cover - exercised in CI without the extra
            self._pytesseract: object | None = None
            self._import_error: Exception | None = exc
        else:
            self._pytesseract = pytesseract  # type: ignore[assignment]
            self._import_error = None
        self._lang = lang

    @property
    def lang(self) -> str:
        """Tesseract ``-l`` language string passed to ``image_to_string``."""
        return self._lang

    def recognize(self, *, source: Path, page: int) -> str:
        """OCR ``page`` (1-based) of ``source``.

        On any failure path (import missing, tesseract binary
        absent, image decode error, empty result) raises the typed
        ``OcrRequiredUnavailableError``. Callers should not catch
        this — it is the spec EXIT shape.
        """
        if self._pytesseract is None:
            raise OcrRequiredUnavailableError(
                "pytesseract is not installed; install the "
                "[ocr] extra or use --ocr-engine none/mocked"
            ) from self._import_error

        # ``image_to_string`` accepts a file path directly; for
        # PDFs the caller is expected to render the page to an
        # image first. To keep this contract free of pdf2image
        # (another optional dep), ``TesseractEngine`` only OCRs
        # rendered images — when called with a PDF path, it raises
        # the typed error and the CLI surfaces a clear message. In
        # production the operator pairs this engine with a
        # pdf2image front-end; the test path uses ``MockOCREngine``
        # and never reaches this code.
        pytesseract = self._pytesseract
        try:
            text = pytesseract.image_to_string(  # type: ignore[union-attr]
                str(source), lang=self._lang
            )
        except Exception as exc:
            raise OcrRequiredUnavailableError(
                f"OCR failed for {source.name} page {page}: {exc}"
            ) from exc

        if not text or not text.strip():
            raise OcrRequiredUnavailableError(
                f"OCR returned empty text for {source.name} page {page}"
            )
        return text


def detect_ocr_required(
    *, text: str, image_count: int, min_text_chars: int = 100
) -> bool:
    """Decide whether a page needs OCR.

    Heuristic: a page needs OCR when (a) the text-layer extraction
    yielded less than ``min_text_chars`` AND (b) the page references
    at least one image. The defaults err on the side of false
    positives (we just OCR the page) — false negatives are worse
    because they produce empty frozen units, which the spec
    explicitly forbids.

    Parameters
    ----------
    text:
        Text extracted from the page by the upstream extractor
        (``pdfplumber.Page.extract_text()`` or HTML strip).
    image_count:
        Number of ``<image>`` references on the page. The
        ``extract_text`` path does not surface this directly; the
        ingest command passes the count from
        ``page.images``/equivalent HTML scan.
    min_text_chars:
        Text-layer threshold below which we consider the page
        "collapsed". Default 100 — a single dense paragraph.

    Returns
    -------
    bool
        ``True`` iff OCR must run for this page.
    """
    return len(text.strip()) < min_text_chars and image_count > 0


__all__ = [
    "DEFAULT_TESSERACT_LANG",
    "OCREngine",
    "OCRError",
    "OcrRequiredUnavailableError",
    "TesseractEngine",
    "detect_ocr_required",
]