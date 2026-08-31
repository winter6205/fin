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


__all__ = [
    "IngestMissingError",
    "MalformedExtractedIndexError",
    "ManifestShaMismatchError",
    "ParseError",
    "EmptySourceParseError",
    "UnsupportedFormatParseError",
    "MalformedSourceParseError",
    "EncryptedSourceParseError",
    "FormatMismatchParseError",
    "NoUnitsParseError",
    "ParseMetadataIncompleteError",
]