"""Executable catalog of the corpus-retrieval failure surface (T24).

The catalog is deliberately built from the owners' published codes,
rather than repeating string literals in a document.  Each entry names
one failure category, its assertable trace, and a callable that reads
the code from the real implementation.  T24 tests exercise the
corresponding seams and compare their observed trace with this catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final

from fin.corpus.errors import EmptySourceParseError
from fin.corpus.ocr import OcrRequiredUnavailableError
from fin.retrieve.hit_mapping import MAPPING_FAILURE_UNMAPPED_SOURCE_REF
from fin.retrieve.invalidation_port import InvalidationServiceUnavailableError
from fin.retrieve.push_batching import PushBatchFailedError
from fin.retrieve.push_port import (
    PushProtocolError,
    PushServiceUnavailableError,
)


FAILURE_CATEGORY_PARSE: Final[str] = "parse_failure"
FAILURE_CATEGORY_OCR: Final[str] = "ocr_unavailable"
FAILURE_CATEGORY_PUSH_UNAVAILABLE: Final[str] = "push_unavailable"
FAILURE_CATEGORY_PROTOCOL: Final[str] = "protocol_failure"
FAILURE_CATEGORY_MAPPING: Final[str] = "mapping_failure"
FAILURE_CATEGORY_INVALIDATION: Final[str] = "invalidation_failure"
FAILURE_CATEGORY_BACKGROUND: Final[str] = "background_failure"

FAILURE_EVIDENCE_NAMED_ERROR: Final[str] = "named_error"
FAILURE_EVIDENCE_COUNT: Final[str] = "count"
FAILURE_EVIDENCE_RECORD: Final[str] = "record"


@dataclass(frozen=True)
class FailureCatalogEntry:
    """One required failure category and its assertable trace."""

    category: str
    code: str
    evidence: str
    carrier: str
    code_source: Callable[[], str]


def _class_code(error_type: type[BaseException]) -> Callable[[], str]:
    return lambda: str(getattr(error_type, "code"))


def _constant_code(code: str) -> Callable[[], str]:
    return lambda: code


FAILURE_CATALOG: Final[tuple[FailureCatalogEntry, ...]] = (
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_PARSE,
        code=EmptySourceParseError.code,
        evidence=FAILURE_EVIDENCE_NAMED_ERROR,
        carrier="fin.corpus.errors.EmptySourceParseError.code",
        code_source=_class_code(EmptySourceParseError),
    ),
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_OCR,
        code=OcrRequiredUnavailableError.code,
        evidence=FAILURE_EVIDENCE_NAMED_ERROR,
        carrier="fin.corpus.ocr.OcrRequiredUnavailableError.code",
        code_source=_class_code(OcrRequiredUnavailableError),
    ),
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_PUSH_UNAVAILABLE,
        code=PushServiceUnavailableError.code,
        evidence=FAILURE_EVIDENCE_NAMED_ERROR,
        carrier="fin.retrieve.push_port.PushServiceUnavailableError.code",
        code_source=_class_code(PushServiceUnavailableError),
    ),
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_PROTOCOL,
        code=PushProtocolError.code,
        evidence=FAILURE_EVIDENCE_NAMED_ERROR,
        carrier="fin.retrieve.push_port.PushProtocolError.code",
        code_source=_class_code(PushProtocolError),
    ),
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_MAPPING,
        code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
        evidence=FAILURE_EVIDENCE_COUNT,
        carrier="fin.retrieve.hit_mapping.MappingFailureLedger.count_by_code",
        code_source=_constant_code(MAPPING_FAILURE_UNMAPPED_SOURCE_REF),
    ),
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_INVALIDATION,
        code=InvalidationServiceUnavailableError.code,
        evidence=FAILURE_EVIDENCE_RECORD,
        carrier="fin.retrieve.invalidation_port.InvalidationLedger.failures",
        code_source=_class_code(InvalidationServiceUnavailableError),
    ),
    FailureCatalogEntry(
        category=FAILURE_CATEGORY_BACKGROUND,
        code=PushBatchFailedError.code,
        evidence=FAILURE_EVIDENCE_RECORD,
        carrier="fin.retrieve.push_batching.PushJournal.failures",
        code_source=_class_code(PushBatchFailedError),
    ),
)


def failure_catalog() -> tuple[FailureCatalogEntry, ...]:
    """Return the immutable T24 failure list."""

    return FAILURE_CATALOG


def untraced_failure_categories() -> tuple[str, ...]:
    """Return catalog entries that lack an assertable trace."""

    return tuple(
        entry.category
        for entry in FAILURE_CATALOG
        if not entry.code or not entry.evidence or not entry.carrier
    )


__all__ = [
    "FAILURE_CATALOG",
    "FAILURE_CATEGORY_BACKGROUND",
    "FAILURE_CATEGORY_INVALIDATION",
    "FAILURE_CATEGORY_MAPPING",
    "FAILURE_CATEGORY_OCR",
    "FAILURE_CATEGORY_PARSE",
    "FAILURE_CATEGORY_PROTOCOL",
    "FAILURE_CATEGORY_PUSH_UNAVAILABLE",
    "FAILURE_EVIDENCE_COUNT",
    "FAILURE_EVIDENCE_NAMED_ERROR",
    "FAILURE_EVIDENCE_RECORD",
    "FailureCatalogEntry",
    "failure_catalog",
    "untraced_failure_categories",
]
