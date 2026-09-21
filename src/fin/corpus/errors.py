"""Typed exceptions for the corpus capability slice.

These are the typed error classes the rest of fin uses to distinguish
"the corpus is in a known-bad state" from generic runtime failures. The
host graph (T6), retrieve adapter (T4), and citation gate (T5) consume
``IngestMissingError`` to fail fast when the committed extracted tree is
absent or incomplete (ADR 0020).

``ManifestShaMismatchError`` is a subclass of ``IngestMissingError``:
both signal "the source no longer matches what was committed, refuse
to extract from a drifted source." Inheriting from
``IngestMissingError`` lets the rest of fin treat "missing PDF",
"sha drift", and "incomplete extracted tree" as the same family of
recoverable conditions — the operator must restore the frozen PDF (or
refresh the manifest) and re-run ingest.
"""

from __future__ import annotations


class IngestMissingError(RuntimeError):
    """Raised when the committed extracted tree is absent or incomplete.

    The host graph (T6) and the read-side tools (T3) treat this as the
    typed signal that ``data/corpus/extracted/`` is missing or that
    expected files for at least one document in the manifest are
    absent. The ask path must not fall back to opening a PDF (ADR
    0020).
    """


class MalformedExtractedIndexError(IngestMissingError):
    """An existing ``_index.json`` cannot satisfy the ingest schema.

    This is distinct from a missing index: replacing a damaged index
    could discard document records owned by another ingest path.
    """


class ManifestShaMismatchError(IngestMissingError):
    """A frozen PDF's sha256 does not match the value in the corpus manifest.

    Also raised when the manifest references a PDF file that is not
    present at all. Both conditions mean "the source no longer matches
    what was committed" — the safe behavior is identical: the operator
    must restore the frozen PDF (or refresh the manifest) and re-run
    ingest. We never silently re-extract from a drifted source.
    """

    pass


class CatalogCorruptError(IngestMissingError):
    """The catalog metadata table exists but cannot be read as the
    catalog schema (not valid JSON, not ``{"filings": [...]}``, or a
    stored row violating the row contract).

    A *missing* table file is NOT this error — the empty catalog is a
    valid state (ADR 0047: ``list_files`` over a never-ingested corpus
    is ``{"filings": []}``). This is the "the table is there but
    broken" shape. It inherits from ``IngestMissingError`` so the
    host's 503 ``ingest_missing`` mapping and the status strip's
    zero-count fallback treat it as the same known-bad corpus-state
    family (ADR 0020 / ADR 0033).
    """


class CatalogRowError(ValueError):
    """A row handed to ``fin.corpus.catalog.record_filings`` violates
    the catalog row contract (``{document_id, company, period,
    version, page_count}``, optional ``source_sha256`` /
    ``extract_method``), or ``switch_extract_method`` is called with a
    method outside the closed set.

    This is the named failure for bad rows — never a silent
    skip-as-success: the rejected write does not happen and the
    existing table is left untouched.
    """


class ExtractMethodConflictError(ValueError):
    """A second, DIFFERENT extract method was recorded on one corpus
    version through the ingest seam (ADR 0079: exactly one method per
    version).

    This is the typed rejection of the dual-graph shape — the table is
    left untouched, and the request is never silently merged into two
    graphs or silently overwritten. Changing a version's method is the
    operator switch's job (:func:`fin.corpus.catalog.switch_extract_method`),
    which records a rebuild intent instead.
    """


class ExtractMethodTargetMissingError(ValueError):
    """The operator switch targeted a ``(document_id, version)`` with
    no catalog row.

    The switch changes the method of a recorded version only — it is
    never a create path, so a missing row is a named failure rather
    than a half-initialized version.
    """


class ParseError(RuntimeError):
    """Base class for the multi-format parse path's named failures (T6).

    The seven-format parser (``fin.corpus.parsers``) never returns a
    "silently empty" result: every failure shape it can produce is a
    subclass of this family, so callers can distinguish "the source is
    in a known-bad state" from generic runtime failures (SC9 / SC40
    lineage: no empty string, no silent skip, no bare exception).

    Every subclass carries a distinct stable :attr:`code` (T8 / SC9):
    callers that prefer machine-readable classification over exception
    types read ``exc.code``. Codes are part of the failure surface —
    two shapes never share one.
    """

    code: str = "parse_error"


class EmptySourceParseError(ParseError):
    """The source bytes are empty (0-byte file) — SC9's empty-input
    shape. Parsing cannot succeed because there is nothing to parse;
    the unit is refused, not marked complete with empty frozen text.
    """

    code = "empty_source"


class UnsupportedFormatParseError(ParseError):
    """The requested format is not one of the seven supported formats
    (pdf / docx / xlsx / pptx / html / md / txt) — the "caller named a
    format we do not speak" half of SC9's non-target-MIME shape. The
    caller names the format explicitly; an unknown name is an EXIT,
    not a best-effort guess.
    """

    code = "unsupported_format"


class MalformedSourceParseError(ParseError):
    """The source cannot be opened as its claimed format: corrupt
    container (bad zip / bad XML / no valid xref) or a parser-level
    decode failure — SC9's corrupt-input shape. Refusing beats
    guessing: a half-readable document must not yield units.

    Encrypted sources are *not* this class: they are named by
    :class:`EncryptedSourceParseError` so callers can tell "repair the
    file" from "supply credentials".
    """

    code = "malformed_source"


class EncryptedSourceParseError(ParseError):
    """The source is encrypted and no credentials were supplied —
    SC9's encrypted-input shape (T8), kept distinct from the corrupt
    shape because the remediation differs: an encrypted PDF / OOXML
    package is structurally intact, so "corrupt" would misdirect the
    operator. Recognized shapes: PDF standard security handler
    (pdfminer ``PDFEncryptionError``, including password-incorrect),
    and ECMA-376 encrypted OOXML packages (Compound File Binary
    container instead of a zip).
    """

    code = "encrypted_source"


class FormatMismatchParseError(ParseError):
    """The declared format conflicts with the bytes' content signature
    — the "bytes are not the MIME the caller declared" half of SC9's
    non-target-MIME shape (T8). ``parse_document`` names its format
    explicitly, so a PDF's magic fed as ``xlsx`` (or a binary image fed
    as ``txt``, which would otherwise silently yield garbage "text"
    units) is this named failure, not a best-effort parse. The
    signature check is failure classification only: absence of a known
    signature never fails, and parsing itself is never redirected.
    """

    code = "format_mismatch"


class NoUnitsParseError(ParseError):
    """The parse succeeded mechanically but produced zero non-empty
    frozen units (e.g. a whitespace-only document) — SC6's non-empty
    unit floor is violated, so the parse is a named failure instead of
    an empty success. This is SC9's empty-file shape: a file that
    exists but carries no parseable content.
    """

    code = "no_units"


class ParseMetadataIncompleteError(ParseError):
    """A produced unit (or the parse identity itself) is missing one of
    the eight structured-metadata fields — format, source byte hash,
    locator triple, unit ordinal, char count, language, parse rule
    version, params-table version (SC10). The parse terminates with
    this named failure; a "field missing but still successful" unit is
    never emitted.
    """

    code = "metadata_incomplete"


class ParseHeadingSpanError(ParseError):
    """A parser's claimed heading lines are not a valid, locatable
    claim about the text it produced.

    The heading-span channel exists so a format whose structure is a
    TAG (HTML ``h1``-``h6``) can name its heading lines without
    widening the window classifier (ADR 0053 / 0060: the classifier
    stays ATX + UPPERCASE-only). A claim that cannot be located in the
    text it describes is a parser bug, not a corpus property: an index
    outside the line range, a repeated index, an unsorted sequence, or
    a claim whose raw→frozen mapping premise does not hold all land
    here. Silently dropping the claim would make the title quietly
    disappear, which is exactly the SC7 gap this channel closes.
    """

    code = "heading_span_invalid"


__all__ = [
    "IngestMissingError",
    "CatalogCorruptError",
    "CatalogRowError",
    "ExtractMethodConflictError",
    "ExtractMethodTargetMissingError",
    "MalformedExtractedIndexError",
    "ManifestShaMismatchError",
    "ParseError",
    "EmptySourceParseError",
    "UnsupportedFormatParseError",
    "MalformedSourceParseError",
    "EncryptedSourceParseError",
    "FormatMismatchParseError",
    "NoUnitsParseError",
    "ParseHeadingSpanError",
    "ParseMetadataIncompleteError",
]