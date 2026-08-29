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


__all__ = [
    "IngestMissingError",
    "MalformedExtractedIndexError",
    "ManifestShaMismatchError",
]