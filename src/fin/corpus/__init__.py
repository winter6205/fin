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

Fact table (S1 / ADR 0031): the research track's ``get_xbrl_fact``
lookup reads from a separate ``facts`` module
(``fin.corpus.facts`` — not re-exported here). It is intentionally
NOT on the ask-path re-export list because that module is on the
ingest side; the lookup itself is read-only and stays on the ask
path via ``fin.corpus.facts.read_facts`` (T5 imports it directly).

NOT re-exported here (intentional — importing ``fin.corpus`` MUST stay
pdfplumber-free on the ask path, per the contract above; callers that
need the ingest command use ``fin.corpus.ingest.run_ingest`` directly,
which only the operator ever runs):

* ``run_ingest`` — the idempotent local ingest command. Imports
  pdfplumber.
* ``fin.corpus.live_fetch.fetch_and_ingest`` — the UA-gated live
  EDGAR fetch (T1). Imports ``urllib.request``; not on the ask path.
* ``fin.corpus.facts.write_facts`` — the fact-table write; called by
  the ingest CLI, not by the ask path.
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