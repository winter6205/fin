"""Retrieve capability slice: the retrieve port and its adapters.

Public surface (T3 — ADR 0030: MCP is product, BM25 archived):

* ``RetrievePort`` — structural ``Protocol`` for any retrieve
  implementation. The MCP-backed ``MCPRetrieveAdapter`` is the
  v1 product implementation; ``FakeRetrievePort`` is the
  default pytest surface; ``BM25Adapter`` is retained for
  offline / legacy tests but NOT bound in the product compile
  path.
* ``Chunk`` — the value object returned inside the contract
  dict's ``"chunks"`` list: ``{text, source_ref: Locator,
  score}``.
* ``MCPRetrieveAdapter`` — stdio JSON-RPC client to
  **iknow-memory**. ``compile_graph(retrieve_adapter="mcp")``
  builds one; that is what ``fin.host.compile`` selects for both
  production boots. ``RetrieveUnavailableError`` is the typed
  EXIT on MCP failure (mapped to
  ``{"error": "retrieve_unavailable"}`` by ``retrieve``).
* ``FakeRetrievePort`` — in-memory dict-backed adapter and
  ``compile_graph``'s default kind, so the default pytest run
  never touches MCP; tests can also pass one explicitly.
* ``BM25Adapter`` — in-process lexical ranker. RETAINED for
  offline tests (``tests/retrieve/test_bm25.py``); NOT bound
  in the product compile path.
* ``retrieve`` — the plain-Python host tool wrapper. Accepts
  any ``RetrievePort`` via the ``adapter`` keyword; it does NOT
  re-implement scoring.
"""

from fin.retrieve.bm25 import BM25Adapter
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter, RetrieveUnavailableError
from fin.retrieve.port import Chunk, RetrievePort
from fin.retrieve.tool import retrieve

__all__ = [
    "BM25Adapter",
    "Chunk",
    "FakeRetrievePort",
    "MCPRetrieveAdapter",
    "RetrievePort",
    "RetrieveUnavailableError",
    "retrieve",
]