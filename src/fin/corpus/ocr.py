"""Algorithmic OCR for ingest (T4 / S1).

This module owns the small OCR interface the ingest command
consumes. It is intentionally narrow:

* An ``OCREngine`` ``Protocol`` describing one call:
  ``recognize(*, source: Path, page: int) -> str``, extended — not
  replaced — by ``RasterOCREngine``, which adds
  ``recognize_raster(*, png: bytes, reference: str) -> str`` for the
  pixels of one region. Production implements both with
  ``TesseractEngine`` (lazy-imported ``pytesseract``); tests use
  ``MockOCREngine`` defined in ``tests/corpus/test_ocr.py``.
* A typed ``OCRError`` hierarchy. ``OcrRequiredUnavailableError``
  is the spec's ``ocr_required_unavailable`` EXIT: the unit needs
  OCR and either the engine is missing OR the OCR call failed OR
  the engine returned empty text. Per spec § Boundaries: "OCR is
  required (collapsed/no text-layer + images) AND engine/extra is
  missing OR OCR fails -> ingest does NOT succeed that unit with
  empty frozen text; typed non-zero / error, not silent skip."
* The trigger rules, one per question. :func:`detect_ocr_required` here
  asks the page-level question — did the text layer collapse while the
  page still carries images — and takes its threshold from the caller, so
  no lane inherits a number it did not choose. The region-level question —
  does this figure's own box hold text that only pixels carry — is
  answered by the core, not here:
  :func:`fin.corpus.parsers._pages._crop_is_legible` reads a region's
  kind and its short edge in raster pixels against
  :data:`fin.corpus.parsers._pages.MIN_OCR_REGION_EDGE_PX`, and
  ``PageRegionParser`` runs that crop rule only on a page whose whole
  raster it did not already read, so "do not recognize the same pixels
  twice" is control flow there rather than a third input here. A mirror
  of either rule in this module would be a rule the product does not run
  and a threshold with two homes.

Why a module and not a class hierarchy across the host graph:
the spec (CONTEXT ``ingest``: "A CLI that writes frozen locator-unit
text from official filings") keeps OCR inside the corpus capability
slice; it is an ingest-time write-back, not a runtime tool. The
interface here is one call per page or per region, no streaming, no
callback plumbing — the simplest shape that satisfies the contract.
"""

from __future__ import annotations

import io
import re
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

    This Protocol stays at exactly one member. A page-only engine is
    already wired in several places, so the pixel-level call that the
    page/region core needs is added by :class:`RasterOCREngine`
    instead of here.
    """

    def recognize(self, *, source: Path, page: int) -> str: ...


class RasterOCREngine(OCREngine, Protocol):
    """An :class:`OCREngine` that can also read a region's own pixels.

    ``png`` is the image to recognize — a crop cut out of a rendered
    page, a standalone image attachment, or a whole page raster — and
    ``reference`` names what is being read so a failure can say so.
    ``reference`` is never interpreted, only echoed.

    Implementation note: this is the same call shape as
    ``fin.corpus.parsers._pages.RasterOcrPort``, which is what the
    page/region core injects.
    """

    def recognize_raster(self, *, png: bytes, reference: str) -> str: ...


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
#
# The measurement keeps this default. Over the committed gold set the two
# languages plus ``eng`` alone reproduce the same number of committed quotes
# with the same recall of the source filing's own text layer, and the extra
# characters the Chinese model emits on Latin-only pages are inter-token
# spacing that :func:`fin.corpus.normalization.normalize_frozen_text`
# collapses: the same chart read comes back as 608 raw characters under
# ``chi_sim+eng`` and 313 under ``eng``, and as 312 against 313 once frozen.
# What the second model does cost is CPU — measured per page alongside the
# mode decision below — and that price is paid for the filings this corpus
# actually contains.
DEFAULT_TESSERACT_LANG: Final[str] = "chi_sim+eng"

# The "text layer collapsed to pixels" threshold, with one named home for
# the two lanes that ask that same page-level question: the page/region
# core (``fin.corpus.parsers._pages``) and the ingest lane
# (``fin.corpus.ingest``). The scanned-page parser (``parsers._pdf``) asks
# a different question — "is there a text layer at all" — and names its
# own 1 at that call. The shared rule still takes the threshold as a
# required argument (see :func:`detect_ocr_required`) so no lane inherits
# an answer it did not choose; only the duplicated literal is removed here.
PAGE_COLLAPSE_TEXT_CHARS: Final[int] = 100

# Tesseract page-segmentation mode the shipped configuration names. Left
# unset, the binary silently uses mode 3 — which is what every cost figure
# recorded before this constant existed was measured under, chosen by nobody.
# The ladder 3 / 6 / 11 was run at 150 and 200 dpi over the three committed
# gold cases, scoring each read against the committed per-page expectations:
# on the chart pixels mode 3 reproduces 3 of 5 (page 3 crop at 200 dpi: 3 of
# 4) and mode 11 four of five, while mode 6 reproduces all five at both
# scales. On the image-only pages the three modes agree — every one reaches
# the committed heading and full recall of the source filing's own text layer
# — so the mode is decided by the figure lane, which only mode 6 reads whole.
# Mode 6 is also Tesseract's own setting for a single uniform text block,
# which is what a crop or a page-image scan hands it.
DEFAULT_TESSERACT_PSM: Final[str] = "6"

_PSM_RE: Final[re.Pattern[str]] = re.compile(r"^(?:[1-9]|1[0-3])$")
_LANG_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")


def _checked_psm(psm: str) -> str:
    if not isinstance(psm, str) or not _PSM_RE.match(psm):
        raise ValueError(
            f"psm must be a Tesseract page-segmentation mode 1-13, got {psm!r}"
        )
    return psm


def _checked_lang(lang: str) -> str:
    if not isinstance(lang, str) or not _LANG_RE.match(lang.strip()):
        raise ValueError(
            f"lang must be a '+'-joined Tesseract language list, got {lang!r}"
        )
    return lang.strip()


class TesseractEngine:
    """Production OCR engine backed by ``pytesseract``.

    The import is lazy: ``pytesseract`` is an optional
    ``[project.optional-dependencies] ocr`` extra. When it is not
    installed (the default pytest path), ``__init__`` succeeds but
    every ``recognize`` call raises the typed
    ``OcrRequiredUnavailableError`` so callers can fail fast without
    us swallowing the missing-extra condition.

    Default ``lang`` is :data:`DEFAULT_TESSERACT_LANG` (``chi_sim+eng``)
    and default ``psm`` is :data:`DEFAULT_TESSERACT_PSM`; both are sent on
    every call, page-level or region-level, so the configuration the cost
    figures were priced against is the configuration that runs.
    """

    def __init__(
        self,
        *,
        lang: str = DEFAULT_TESSERACT_LANG,
        psm: str = DEFAULT_TESSERACT_PSM,
    ) -> None:
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
        self._lang = _checked_lang(lang)
        self._psm = _checked_psm(psm)

    @property
    def lang(self) -> str:
        """Tesseract ``-l`` language string passed to ``image_to_string``."""
        return self._lang

    @property
    def psm(self) -> str:
        """Tesseract ``--psm`` page-segmentation mode passed to the binary."""
        return self._psm

    @property
    def tesseract_config(self) -> str:
        """The argv fragment every call ships, exposed for observability."""
        return f"--psm {self._psm}"

    # -- shared call, two entry points --

    def _recognize_image(self, image: object, *, name: str) -> str:
        if self._pytesseract is None:
            raise OcrRequiredUnavailableError(
                "pytesseract is not installed; install the "
                "[ocr] extra or use --ocr-engine none/mocked"
            ) from self._import_error
        pytesseract = self._pytesseract
        try:
            text = pytesseract.image_to_string(  # type: ignore[union-attr]
                image, lang=self._lang, config=self.tesseract_config
            )
        except OcrRequiredUnavailableError:
            raise
        except Exception as exc:
            raise OcrRequiredUnavailableError(
                f"OCR failed for {name}: {exc}"
            ) from exc
        if not text or not text.strip():
            raise OcrRequiredUnavailableError(
                f"OCR returned empty text for {name}"
            )
        return text

    def recognize(self, *, source: Path, page: int) -> str:
        """OCR ``page`` (1-based) of ``source``.

        On any failure path (import missing, tesseract binary
        absent, image decode error, empty result) raises the typed
        ``OcrRequiredUnavailableError``. Callers should not catch
        this — it is the spec EXIT shape.
        """
        # ``image_to_string`` accepts a raster file path directly; for a PDF
        # it does not, and this contract stays free of pdf2image (another
        # optional dep). A PDF path therefore reaches the binary and comes
        # back as the typed error, naming the page; a raster path is the
        # production pairing with a renderer. Region pixels go through
        # :meth:`recognize_raster` instead and never need a file at all.
        return self._recognize_image(str(source), name=f"{source.name} page {page}")

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        """OCR the pixels of one region, page, or standalone image.

        ``png`` is decoded in memory — nothing is written, no page number
        is needed — which is how a figure crop on a page that also has a
        text layer gets read. ``reference`` names the thing being read and
        appears in every failure message so the caller can say which unit
        could not be frozen.
        """
        if not isinstance(png, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"recognize_raster needs image bytes, got {type(png).__name__}"
            )
        from PIL import Image

        name = reference or "region raster"
        try:
            with Image.open(io.BytesIO(bytes(png))) as image:
                # Pixels are decoded here, not by the binary: a truncated or
                # non-image buffer would otherwise be reported as an OCR
                # failure with no text rather than as undecodable input.
                image.load()
                ready = image.convert("RGB")
        except Exception as exc:
            raise OcrRequiredUnavailableError(
                f"OCR could not decode the pixels for {name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return self._recognize_image(ready, name=name)


def detect_ocr_required(
    *, text: str, image_count: int, min_text_chars: int
) -> bool:
    """Decide whether a page needs OCR.

    Heuristic: a page needs OCR when (a) the text-layer extraction
    yielded less than ``min_text_chars`` AND (b) the page references
    at least one image. Erring toward false positives (we just OCR the
    page) is the safe direction — false negatives produce empty frozen
    units, which the spec explicitly forbids.

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
        Text-layer threshold below which the page counts as "collapsed".
        Required, with no default: the lanes that ask this question price
        it differently — the collapse check answers with this module's
        :data:`PAGE_COLLAPSE_TEXT_CHARS`, imported by the page/region
        core and the ingest lane, while the scanned-page parser asks "no
        text layer at all" and names its own 1. A default here would be
        a silent second home for whichever lane's number it copied.

    Returns
    -------
    bool
        ``True`` iff OCR must run for this page.
    """
    return len(text.strip()) < min_text_chars and image_count > 0


__all__ = [
    "DEFAULT_TESSERACT_LANG",
    "DEFAULT_TESSERACT_PSM",
    "PAGE_COLLAPSE_TEXT_CHARS",
    "OCREngine",
    "OCRError",
    "OcrRequiredUnavailableError",
    "RasterOCREngine",
    "TesseractEngine",
    "detect_ocr_required",
]