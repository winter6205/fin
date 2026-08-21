"""fin — a bounded 10-Q research agent.

Capability slices live in sub-packages:
- ``fin.corpus``: frozen 10-Q corpus, ingest, and page-level catalog.
- ``fin.retrieve``: the retrieve port and its in-process BM25 adapter.
- ``fin.host``: the host graph, tool wrappers, citation gate, CLI, and HTTP.
"""

__version__ = "0.1.0"
