"""Selective algorithmic OCR write-back for frozen locator units.

This module operates on an already extracted page. It calls the existing
algorithmic :class:`OCREngine` only when the page has image content and
no usable text layer. The page locator is preserved, and OCR text becomes
the page text plus one citable full-page passage. There is no vision-model
or image-attachment path here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from fin.corpus.extracted import ExtractedPage, Passage
from fin.corpus.ocr import (
    OCREngine,
    OCRError,
    OcrRequiredUnavailableError,
)
from fin.corpus.ocr_writeback_errors import (
    InvalidOCRWriteBackInputError,
    OCRWriteBackEmptyTextError,
    OCRWriteBackEngineError,
)


@dataclass(frozen=True)
class OCRWriteBackResult:
    """The page after selective OCR and whether text was written."""

    page: ExtractedPage
    applied: bool


def selective_ocr_write_back(
    page: ExtractedPage,
    *,
    source: Path,
    image_count: int,
    engine: OCREngine,
) -> OCRWriteBackResult:
    """OCR a textless image page and return its frozen unit.

    Pages with no images or existing text are returned by identity and
    the engine is not invoked. For an OCR candidate, the locator is
    unchanged and the OCR output becomes both page text and a full-page
    passage. Existing ``OcrRequiredUnavailableError`` failures retain
    their established named failure; other backend failures use the
    write-back typed error.
    """
    if not isinstance(image_count, int) or isinstance(image_count, bool):
        raise InvalidOCRWriteBackInputError(
            f"image_count must be an integer, got {image_count!r}"
        )
    if image_count < 0:
        raise InvalidOCRWriteBackInputError(
            f"image_count must be non-negative, got {image_count}"
        )

    if image_count == 0 or page.text.strip():
        return OCRWriteBackResult(page=page, applied=False)

    if engine is None:
        raise OCRWriteBackEngineError(
            f"OCR engine is unavailable for "
            f"{page.locator.document_id} page {page.locator.unit}"
        )
    try:
        text = engine.recognize(source=source, page=page.locator.unit)
    except OcrRequiredUnavailableError:
        raise
    except OCRWriteBackEngineError:
        raise
    except OCRError as exc:
        raise OCRWriteBackEngineError(
            f"OCR failed for {page.locator.document_id} "
            f"page {page.locator.unit}: {exc}"
        ) from exc
    except Exception as exc:
        raise OCRWriteBackEngineError(
            f"OCR failed for {page.locator.document_id} "
            f"page {page.locator.unit}: {exc}"
        ) from exc

    if not isinstance(text, str):
        raise OCRWriteBackEngineError(
            f"OCR returned {type(text).__name__}, expected str"
        )
    if not text.strip():
        raise OCRWriteBackEmptyTextError(
            f"OCR returned empty text for {page.locator.document_id} "
            f"page {page.locator.unit}"
        )

    written_page = replace(
        page,
        text=text,
        passages=(Passage(text=text, char_start=0, char_end=len(text)),),
    )
    return OCRWriteBackResult(page=written_page, applied=True)


__all__ = [
    "OCRWriteBackResult",
    "selective_ocr_write_back",
]
