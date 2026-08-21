"""Host tool wrapper for the v1 retrieve capability.

This module is the seam between the BM25 capability (``fin.retrieve.bm25``)
and the host graph's surface. T6 binds ``retrieve`` as a third tool
alongside ``list_files`` and ``read_file``; this function is the
plain-Python callable the LangChain ``@tool`` decorator wraps.

The wrapper does NOT re-implement scoring: it delegates to
``BM25Adapter.retrieve``. The adapter is the only source of truth for
ranking; the wrapper exists so the host graph has one import point
that returns the contract dict shape (``{"chunks": [...]}``).

T6 calls ``load_extracted_tree`` once at boot, constructs one
``BM25Adapter``, and shares it across the run by passing it in via the
``adapter`` keyword. The wrapper's ``adapter`` parameter is the
seam that lets the host graph inject a process-wide shared adapter
without going through module-level state.
"""

from __future__ import annotations

from fin.retrieve.bm25 import BM25Adapter
from fin.retrieve.port import Chunk


def retrieve(
    query: str,
    document_id: str | None = None,
    limit: int = 8,
    *,
    adapter: BM25Adapter | None = None,
) -> dict[str, list[Chunk]]:
    """Adapter-bound retrieve: returns the ADR 0009 contract shape.

    Parameters
    ----------
    query
        Lexical query. Empty / whitespace-only queries yield
        ``{"chunks": []}`` without touching the index.
    document_id
        Optional filter. Unknown document_id yields
        ``{"chunks": []}``.
    limit
        Default 8, hard cap 20 (ADR 0009). The BM25Adapter resolves
        the cap internally; we just pass through.
    adapter
        The shared ``BM25Adapter`` instance. The host graph (T6)
        constructs one adapter per run and threads it through every
        ``retrieve`` call. If ``None``, the wrapper raises because
        silently constructing a default adapter would hide a
        misconfigured host graph — T6 always provides one.

    Returns
    -------
    dict
        ``{"chunks": [Chunk, ...]}`` in descending score order.
        Each ``Chunk`` carries ``text``, ``source_ref`` (Locator),
        and ``score``.
    """
    if adapter is None:
        raise ValueError(
            "fin.retrieve.tool.retrieve requires an explicit `adapter`; "
            "the host graph (T6) must construct one BM25Adapter per run "
            "and share it across all calls."
        )
    return adapter.retrieve(
        query=query,
        document_id=document_id,
        limit=limit,
    )


__all__ = ["retrieve"]