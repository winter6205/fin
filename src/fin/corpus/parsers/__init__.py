"""Seven-format document parsing: frozen-unit text + structured
metadata (T6 / SC6 / SC7 / SC10).

The public surface is :func:`parse_document`:

.. code-block:: python

    result = parse_document(
        path, fmt="xlsx", document_id="AAPL-2022Q3", version=sha256
    )
    for unit in result.units:
        unit.text                    # frozen text (normalization outlet)
        unit.locator                 # {document_id, version, unit}
        unit.source_sha256           # sha256 of the raw source bytes
        ...

Every unit carries the eight SC10 metadata fields (format, source
byte hash, locator triple, unit ordinal, char count, language, parse
rule version, params-table version); the construction point
``_common.build_unit`` raises the named
``ParseMetadataIncompleteError`` if any field would be missing — a
"field missing but still successful" unit is never emitted (SC10).

Granularity (plan T6 "已决"): PDF and PPT per physical page / slide;
Excel per consecutive row block within a worksheet; HTML / Markdown /
Word per structure block (heading split + deterministic cap merge);
plain text per blank-line paragraph.

Determinism (SC12, function level): the same source bytes and the
same rule version always produce equal ``ParseResult`` values — all
iteration is in document order, no clock / locale / randomness enters
the pipeline. The full-corpus sha256 assertion is T12's deliverable.

Versioning: any change to splitting, merging, or granularity constants
must bump :data:`PARSE_RULE_VERSION` (``parse-2``); the params-table
version is :data:`PARAMS_TABLE_VERSION` (``params-1``), owned by
``fin.corpus.params`` — the versioned (document type x section x
content density) lookup table landed by T9, consumed by the T11
splitter.

Named failures (SC9 / SC40 lineage; all in ``fin.corpus.errors``):
``EmptySourceParseError`` (0-byte source), ``UnsupportedFormatParseError``
(non-target format name), ``FormatMismatchParseError`` (bytes carry a
signature conflicting with the declared format — T8's non-target-MIME
shape), ``EncryptedSourceParseError`` (encrypted PDF / encrypted OOXML
package — T8), ``MalformedSourceParseError`` (corrupt container /
bad XML / undecodable text), ``NoUnitsParseError`` (zero non-empty
units), ``ParseMetadataIncompleteError`` (missing metadata field).
Each subclass carries a distinct stable ``code``. The T8 signature
gate classifies failures only — it never redirects parsing. No
failure shape escapes as a bare exception; no failure yields a
silent empty success.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fin.corpus.errors import (
    EmptySourceParseError,
    EncryptedSourceParseError,
    FormatMismatchParseError,
    NoUnitsParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.parsers._common import (
    FAMILY_CFB,
    FAMILY_PDF,
    FAMILY_ZIP,
    PARAMS_TABLE_VERSION,
    PARSE_RULE_VERSION,
    SUPPORTED_FORMATS,
    ParseResult,
    ParsedUnit,
    UnitLocator,
    content_family,
)
from fin.corpus.parsers._docx import parse_docx
from fin.corpus.parsers._html import parse_html
from fin.corpus.parsers._markdown import parse_markdown
from fin.corpus.parsers._pdf import parse_pdf
from fin.corpus.parsers._pptx import parse_pptx
from fin.corpus.parsers._text import parse_plain_text
from fin.corpus.parsers._xlsx import parse_xlsx


def _family_accepts(fmt: str, family: str) -> bool:
    """Does the declared format accept this content-signature family?

    Only strong foreign signatures fail: ``pdf`` must not arrive as a
    zip / CFB / image, the OOXML trio must not arrive as a PDF or
    image, and the text formats must not arrive as any known binary.
    ``None`` (no signature — the shape of every plain-text source) is
    decided by the format parser, never by this gate.
    """
    if family == FAMILY_PDF:
        return fmt == "pdf"
    if family == FAMILY_ZIP:
        return fmt in ("docx", "xlsx", "pptx")
    # FAMILY_CFB / FAMILY_BINARY: no declared format accepts them here
    # (CFB + OOXML trio is re-classified as encrypted by the caller).
    return False


def parse_document(
    source: Path | bytes,
    *,
    fmt: str,
    document_id: str,
    version: str,
    max_units: int | None = None,
) -> ParseResult:
    """Parse one document into frozen units with structured metadata.

    Parameters
    ----------
    source:
        A filesystem path or the raw source bytes.
    fmt:
        One of :data:`SUPPORTED_FORMATS` (``pdf`` / ``docx`` /
        ``xlsx`` / ``pptx`` / ``html`` / ``md`` / ``txt``). The format
        is named by the caller — there is no content sniffing.
    document_id:
        Locator field 1. Must be non-empty (named failure otherwise).
    version:
        Locator field 2 — the frozen source version (the manifest
        sha256 lineage). Must be non-empty (named failure otherwise).
    max_units:
        Optional deterministic cap: parse only the first ``max_units``
        units of work (for PDF: the first ``max_units`` physical
        pages). Used by bounded contexts (the default pytest suite) to
        cap work on the frozen 10-Qs; ``None`` (default) parses
        everything. Only PDF honors it today; the other formats are
        already whole-document passes over in-memory bytes.

    Returns
    -------
    ParseResult
        Document identity plus the ordered tuple of frozen units.

    Raises
    ------
    EmptySourceParseError
        The source bytes are empty.
    UnsupportedFormatParseError
        ``fmt`` is not one of the seven supported formats.
    FormatMismatchParseError
        The bytes carry a content signature that conflicts with the
        declared ``fmt`` (T8: non-target MIME given the explicit-name
        API).
    EncryptedSourceParseError
        The source is encrypted (PDF standard security handler, or an
        ECMA-376 encrypted OOXML package in a CFB container) and no
        credentials were supplied.
    MalformedSourceParseError
        The source cannot be parsed as its claimed format.
    NoUnitsParseError
        The parse produced zero non-empty frozen units.
    ParseMetadataIncompleteError
        Any of the eight metadata fields would be missing.
    """
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedFormatParseError(
            f"unsupported format {fmt!r}; supported: "
            f"{', '.join(SUPPORTED_FORMATS)}"
        )
    source_bytes = (
        source.read_bytes() if isinstance(source, Path) else source
    )
    if not source_bytes:
        raise EmptySourceParseError(
            f"source for {document_id!r} is empty (0 bytes)"
        )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    # T8 signature gate — failure classification only. A known magic
    # that cannot belong to the declared format is a named mismatch
    # (SC9's non-target-MIME shape under the explicit-name API); an
    # encrypted OOXML package (CFB container) is named encrypted. No
    # signature never fails, and dispatch below is untouched.
    family = content_family(source_bytes)
    if family is not None and not _family_accepts(fmt, family):
        if family == FAMILY_CFB and fmt in ("docx", "xlsx", "pptx"):
            raise EncryptedSourceParseError(
                f"source for {document_id!r} is an encrypted OOXML "
                f"package (Compound File Binary container, declared "
                f"format {fmt!r}) and no credentials were supplied"
            )
        raise FormatMismatchParseError(
            f"source for {document_id!r} carries a {family!r} content "
            f"signature but was declared as {fmt!r}"
        )

    parser_kwargs = {
        "source_bytes": source_bytes,
        "source_sha256": source_sha256,
        "document_id": document_id,
        "version": version,
    }
    if fmt == "pdf":
        units = parse_pdf(**parser_kwargs, max_units=max_units)
    elif fmt == "docx":
        units = parse_docx(**parser_kwargs)
    elif fmt == "xlsx":
        units = parse_xlsx(**parser_kwargs)
    elif fmt == "pptx":
        units = parse_pptx(**parser_kwargs)
    elif fmt == "html":
        units = parse_html(**parser_kwargs)
    elif fmt == "md":
        units = parse_markdown(**parser_kwargs)
    else:
        units = parse_plain_text(**parser_kwargs)

    return ParseResult(
        fmt=fmt,
        source_sha256=source_sha256,
        document_id=document_id,
        version=version,
        parse_rule_version=PARSE_RULE_VERSION,
        params_table_version=PARAMS_TABLE_VERSION,
        units=units,
    )


__all__ = [
    "PARAMS_TABLE_VERSION",
    "PARSE_RULE_VERSION",
    "SUPPORTED_FORMATS",
    "ParseResult",
    "ParsedUnit",
    "UnitLocator",
    "parse_document",
]
