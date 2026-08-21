"""Corpus capability slice: frozen 10-Qs and the page-level catalog.

Public surface (ask-path only — no PDF deps):

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

NOT re-exported here (intentional — importing ``fin.corpus`` MUST stay
pdfplumber-free on the ask path, per the contract above; callers that
need the ingest command use ``fin.corpus.ingest.run_ingest`` directly,
which only the operator ever runs):

* ``run_ingest`` — the idempotent ingest command. Imports pdfplumber.
"""

from fin.corpus.errors import IngestMissingError, ManifestShaMismatchError
from fin.corpus.extracted import (
    ExtractedPage,
    Locator,
    Passage,
    load_extracted_tree,
)

__all__ = [
    "ExtractedPage",
    "IngestMissingError",
    "Locator",
    "ManifestShaMismatchError",
    "Passage",
    "load_extracted_tree",
]