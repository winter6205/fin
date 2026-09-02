"""Typed failures for selective OCR write-back."""

from __future__ import annotations


class OCRWriteBackError(Exception):
    """Base class for selective OCR write-back failures."""


class OCRWriteBackEngineError(OCRWriteBackError):
    """The configured OCR engine failed or returned an invalid result."""


class OCRWriteBackEmptyTextError(OCRWriteBackEngineError):
    """The OCR engine returned no usable text for an OCR candidate."""


class InvalidOCRWriteBackInputError(OCRWriteBackError):
    """The write-back request contains an invalid input value."""


__all__ = [
    "InvalidOCRWriteBackInputError",
    "OCRWriteBackEmptyTextError",
    "OCRWriteBackEngineError",
    "OCRWriteBackError",
]
