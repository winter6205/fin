"""Corpus capability slice: frozen 10-Qs, ingest, and the page-level catalog.

Public surface:

* ``IngestMissingError`` / ``ManifestShaMismatchError`` — typed errors
  the host graph, retrieve adapter, and citation gate consume to fail
  fast when the committed extracted tree is absent or incomplete.
* ``Locator`` — the v1 cite identity ``{document_id, version, page}``
  per ADR 0006.
* ``ExtractedPage`` / ``Passage`` — the read-side value objects
  returned by ``load_extracted_tree``.
* ``load_extracted_tree`` — the ask-path loader. This module does NOT
  import ``pdfplumber``; if you see that import, the ask-path
  contract (ADR 0020) has been violated.
* ``run_ingest`` — the idempotent ingest command. Verifies the
  manifest's per-document sha256 against the on-disk PDFs and writes
  ``data/corpus/extracted/`` deterministically.
"""

from fin.corpus.errors import IngestMissingError, ManifestShaMismatchError
from fin.corpus.extracted import (
    ExtractedPage,
    Locator,
    Passage,
    load_extracted_tree,
)
from fin.corpus.ingest import run_ingest

__all__ = [
    "ExtractedPage",
    "IngestMissingError",
    "Locator",
    "ManifestShaMismatchError",
    "Passage",
    "load_extracted_tree",
    "run_ingest",
]