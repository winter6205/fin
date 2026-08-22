"""Retrieve capability slice: the retrieve port and its in-process BM25 adapter.

Public surface (T4 — in-process BM25, not a stub; ADR 0007):

* ``RetrievePort`` — structural ``Protocol`` for any retrieve
  implementation. v1's only implementation is ``BM25Adapter``; a
  future MCP-backed adapter (e.g. iknow-memory) can replace it
  without touching the host graph.
* ``Chunk`` — the value object returned inside the contract dict's
  ``"chunks"`` list: ``{text, source_ref: Locator, score}``.
* ``BM25Adapter`` — the in-process lexical ranker. Built once per
  process / once per host-graph lifetime; the index lives in RAM and
  is NOT persisted to disk.
* ``retrieve`` — the plain-Python host tool wrapper. T6 binds this as
  a third tool alongside ``list_files`` and ``read_file`` from T3.
  The wrapper takes the shared ``BM25Adapter`` as an explicit
  keyword arg; it does NOT re-implement scoring.

The empty corpus case is valid: ``BM25Adapter({})`` constructs an
empty index and answers every query with ``{"chunks": []}``.
"""

from fin.retrieve.bm25 import BM25Adapter
from fin.retrieve.port import Chunk, RetrievePort
from fin.retrieve.tool import retrieve

__all__ = ["BM25Adapter", "Chunk", "RetrievePort", "retrieve"]