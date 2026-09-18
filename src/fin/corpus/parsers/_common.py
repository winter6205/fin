"""Shared plumbing for the seven-format parsers (T6).

This module owns the pieces every format parser needs:

* the ``ParsedUnit`` / ``ParseResult`` / ``UnitLocator`` contract
  shapes, carrying the eight SC10 metadata fields;
* ``freeze_text`` — the only text path into a unit, forwarding to the
  single normalization outlet ``normalize_frozen_text`` (T5 / SC17);
  parsers must never post-process frozen text themselves;
* ``build_unit`` — constructs a unit and hard-fails with the named
  ``ParseMetadataIncompleteError`` if any of the eight fields would be
  missing (SC10: no "field missing but still successful" unit);
* ``detect_language`` — a deterministic heuristic (CJK share);
* ``merge_blocks`` — the deterministic "heading split + cap merge"
  assembly used by the HTML / Markdown / Word structure-block rule.

Granularity decisions finalized in this package (plan T6, "已决"):

* PDF and PPT: physical page / physical slide;
* Excel: consecutive row blocks within a worksheet (64 rows per unit);
* HTML / Markdown / Word: structure blocks (heading split + merge
  under ``BLOCK_MERGE_CAP_CHARS``);
* plain text: blank-line paragraphs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fin.corpus.errors import ParseMetadataIncompleteError
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.params import TABLE_VERSION as _PARAMS_TABLE_VERSION

# Deterministic merge cap for the structure-block rule (HTML /
# Markdown / Word). Consecutive blocks whose combined length stays
# within this cap are merged into one unit; a single block larger than
# the cap is never split (the cap bounds merging, not the unit ceiling).
# The value is a fixed constant of parse rule version ``parse-2`` —
# changing it is a rule-version bump.
BLOCK_MERGE_CAP_CHARS: Final[int] = 6000

# Excel granularity: one unit per 64 consecutive non-empty rows within
# a worksheet. Fixed constant of ``parse-2``; changing it is a
# rule-version bump.
XLSX_ROWS_PER_UNIT: Final[int] = 64

# Params-table version (T9 / SC13): the versioned lookup table lives
# in ``fin.corpus.params`` — chunk parameters are decided by one triad
# lookup there, and this stamp flows into every unit's metadata. Any
# row change bumps ``fin.corpus.params.TABLE_VERSION``.
PARAMS_TABLE_VERSION: Final[str] = _PARAMS_TABLE_VERSION

# Parse-rule version of this package. Any change to granularity
# constants, splitting, or freezing behavior must bump it (the spec's
# "就地全量重建" rule keys off this stamp).
PARSE_RULE_VERSION: Final[str] = "parse-2"

# The seven supported formats (plan T6 order).
SUPPORTED_FORMATS: Final[tuple[str, ...]] = (
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "html",
    "md",
    "txt",
)

# ---------------------------------------------------------------------------
# Content-signature families (T8) — failure classification only.
# ---------------------------------------------------------------------------

# Magic prefixes of the container families a source can carry. The
# mapping is deliberately small and exact: it exists to *classify
# failures* (encrypted source, declared-format/MIME mismatch), never to
# redirect or replace parsing — ``parse_document`` still dispatches on
# the caller-named format, and a signature absence is never a failure.
PDF_MAGIC: Final[bytes] = b"%PDF-"
ZIP_MAGIC: Final[bytes] = b"PK\x03\x04"
CFB_MAGIC: Final[bytes] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_GIF_MAGIC: Final[bytes] = b"GIF8"
_JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"

# Family labels returned by :func:`content_family`.
FAMILY_PDF: Final[str] = "pdf"
FAMILY_ZIP: Final[str] = "zip"
FAMILY_CFB: Final[str] = "cfb"
FAMILY_BINARY: Final[str] = "binary"


def content_family(source_bytes: bytes) -> str | None:
    """Classify the source's container family from its magic prefix.

    Returns ``"pdf"`` / ``"zip"`` / ``"cfb"`` / ``"binary"``, or
    ``None`` when no known signature is present (which includes every
    plain-text shape — html / md / txt have no magic). Deterministic
    prefix check, stdlib only; the result feeds only the T8 failure
    classification (encryption, declared-format mismatch), never the
    parse dispatch itself.
    """
    if source_bytes.startswith(PDF_MAGIC):
        return FAMILY_PDF
    if source_bytes.startswith(ZIP_MAGIC):
        return FAMILY_ZIP
    if source_bytes.startswith(CFB_MAGIC):
        return FAMILY_CFB
    if (
        source_bytes.startswith(_PNG_MAGIC)
        or source_bytes.startswith(_GIF_MAGIC)
        or source_bytes.startswith(_JPEG_MAGIC)
    ):
        return FAMILY_BINARY
    return None


@dataclass(frozen=True)
class UnitLocator:
    """The locator triple ``{document_id, version, unit}``.

    ``unit`` is the 1-based ordinal of the frozen unit within the
    document (for PDF it equals the physical page number; for PPT the
    physical slide number).
    """

    document_id: str
    version: str
    unit: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "document_id": self.document_id,
            "version": self.version,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ParsedUnit:
    """One frozen unit with the eight SC10 metadata fields.

    ``text`` is already frozen (a fixed point of the normalization
    outlet); ``char_count`` is ``len(text)``.
    """

    # 1. format
    fmt: str
    # 2. source byte hash (sha256 of the raw source bytes)
    source_sha256: str
    # 3. locator triple + 4. unit ordinal (inside the locator)
    locator: UnitLocator
    # 5. char count
    char_count: int
    # 6. language
    language: str
    # 7. parse rule version
    parse_rule_version: str
    # 8. params-table version (``params-1``, from fin.corpus.params)
    params_table_version: str
    # The frozen unit text itself (not one of the eight metadata
    # fields, but the payload they describe).
    text: str


@dataclass(frozen=True)
class ParseResult:
    """Document-level parse product: identity + ordered frozen units."""

    fmt: str
    source_sha256: str
    document_id: str
    version: str
    parse_rule_version: str
    params_table_version: str
    units: tuple[ParsedUnit, ...]


def freeze_text(raw: str) -> str:
    """Forward raw extractor output through the single normalization
    outlet. The only text path into a unit (SC17: no second rule).
    """
    return normalize_frozen_text(raw)


def detect_language(text: str) -> str:
    """Deterministic language heuristic: CJK-ideograph share of the
    letter population decides ``zh`` vs ``en``. Never empty for
    non-empty text (SC10 field 6).
    """
    cjk = 0
    latin = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    if cjk + latin == 0 or 4 * cjk <= latin:
        return "en"
    return "zh"


def build_unit(
    *,
    fmt: str,
    source_sha256: str,
    document_id: str,
    version: str,
    unit: int,
    text: str,
) -> ParsedUnit:
    """Assemble one unit, failing named if any metadata field is
    missing. This is the single construction point for units — the
    SC10 guarantee lives here, not in each format parser.
    """
    missing: list[str] = []
    if not fmt:
        missing.append("format")
    if not source_sha256:
        missing.append("source_sha256")
    if not document_id:
        missing.append("locator.document_id")
    if not version:
        missing.append("locator.version")
    if unit < 1:
        missing.append("locator.unit")
    if not text:
        missing.append("text")
    if missing:
        raise ParseMetadataIncompleteError(
            f"parse produced unit {unit} of {document_id!r} with missing "
            f"metadata fields: {', '.join(missing)}"
        )
    frozen = freeze_text(text)
    if not frozen:
        raise ParseMetadataIncompleteError(
            f"parse produced unit {unit} of {document_id!r} with empty "
            "frozen text"
        )
    return ParsedUnit(
        fmt=fmt,
        source_sha256=source_sha256,
        locator=UnitLocator(
            document_id=document_id, version=version, unit=unit
        ),
        char_count=len(frozen),
        language=detect_language(frozen),
        parse_rule_version=PARSE_RULE_VERSION,
        params_table_version=PARAMS_TABLE_VERSION,
        text=frozen,
    )


def merge_blocks(blocks: list[str], cap: int = BLOCK_MERGE_CAP_CHARS) -> list[str]:
    """Deterministically merge consecutive blocks under ``cap``.

    Walks the block list in order and folds the next block into the
    current unit while the combined length stays within ``cap``. A
    block longer than ``cap`` is never split — the cap bounds merging,
    not the unit ceiling. Same input, same output (no randomness, no
    locale, no clock).
    """
    merged: list[str] = []
    for block in blocks:
        if merged and len(merged[-1]) + len(block) + 1 <= cap:
            merged[-1] = merged[-1] + "\n" + block
        else:
            merged.append(block)
    return merged


__all__ = [
    "BLOCK_MERGE_CAP_CHARS",
    "CFB_MAGIC",
    "FAMILY_BINARY",
    "FAMILY_CFB",
    "FAMILY_PDF",
    "FAMILY_ZIP",
    "PARAMS_TABLE_VERSION",
    "PARSE_RULE_VERSION",
    "PDF_MAGIC",
    "SUPPORTED_FORMATS",
    "XLSX_ROWS_PER_UNIT",
    "ZIP_MAGIC",
    "ParseResult",
    "ParsedUnit",
    "UnitLocator",
    "build_unit",
    "content_family",
    "detect_language",
    "freeze_text",
    "merge_blocks",
]
