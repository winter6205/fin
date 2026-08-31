"""Retrieve port: the v1 ``RetrievePort`` protocol and the ``Chunk`` value object.

The port is the seam between the v1 host graph (which only sees a
typed ``retrieve`` callable) and the adapter (which does the actual
ranking). T6 binds a concrete adapter to a LangChain ``@tool``; later
tickets can swap in an MCP-backed adapter (e.g. an iknow-memory
client) without touching the host graph.

The contract shape is fixed by ADR 0009 and ADR 0031:

    ``retrieve(query, *, document_id=None, cik=None, form=None,
                ticker=None, filing_date=None, limit=8)`` →
    ``{"chunks": [{"text", "source_ref", "score"}, ...]}``

Two answers, never conflated (T21 / SC42 / SC43 / §B2 第 5 条):

* **invalid input** raises a named
  :class:`~fin.retrieve.input_validation.InputValidationError` — an
  empty / whitespace query (``EmptyQueryError``) and a *provably*
  unsatisfiable filter set (``ConflictingFiltersError``, raised where
  the adapter holds the bindings to prove the exclusion). Each is
  counted on the adapter's
  ``input_validation_failures`` ledger and never sets ``degraded``.
* **legal empty result** returns ``{"chunks": []}`` without raising —
  unknown ``document_id``, unknown or empty ``cik`` / ``ticker`` /
  ``form`` / ``filing_date``, and a valid query with no hits.

``limit`` defaults to 8 and is hard-capped at 20. ``source_ref`` is a
``fin.corpus.Locator`` instance so the citation gate (T5) can read
``.document_id`` / ``.version`` / ``.page`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fin.corpus.extracted import Locator


@dataclass(frozen=True)
class Chunk:
    """One retrieved passage.

    ``source_ref`` IS a ``Locator`` (ADR 0006): ``{document_id, version, page}``.
    The BM25 ``score`` is the raw ``rank_bm25`` score — higher is
    better for hits, zero-or-negative for non-matches. The adapter
    returns chunks ordered descending by ``score``.
    """

    text: str
    source_ref: Locator
    score: float


@runtime_checkable
class RetrievePort(Protocol):
    """Structural protocol for v1 retrieve adapters.

    The protocol returns the contract dict shape directly; v1's only
    implementation is ``BM25Adapter`` (in ``bm25.py``). A future MCP
    adapter that produces the same dict shape satisfies this protocol
    structurally without inheriting from it.
    """

    def retrieve(
        self,
        query: str,
        *,
        document_id: str | None = None,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
        limit: int = 8,
    ) -> dict[str, list[Chunk]]:
        """Return ``{"chunks": [...]}`` ranked descending by BM25 score.

        Parameters
        ----------
        query
            The user's lexical query. Empty / whitespace-only queries
            must raise ``EmptyQueryError``: nothing was asked, which is
            not the same answer as "the corpus has no hits".
        document_id
            Optional filter. When set and the document is not in the
            loaded corpus, returns ``{"chunks": []}`` without raising.
            Unknown id is a "well-formed but no-result" call, not an
            error.
        cik / form / ticker / filing_date
            Additive EDGAR-shaped optional filters (ADR 0031).
            Empty and unknown filter values return ``{"chunks": []}``
            — legal empty results. A set the adapter can *prove* no
            document satisfies raises ``ConflictingFiltersError``.
            The adapter is responsible for consulting its manifest
            metadata to evaluate these filters and to prove a conflict;
            an adapter holding no bindings proves none and raises
            nothing.
        limit
            Maximum number of chunks to return. Defaults to 8;
            hard-capped at 20. Values ``<= 0`` are treated as the
            default of 8; values ``> 20`` are clamped to 20.

        Returns
        -------
        dict
            ``{"chunks": [Chunk, ...]}`` in descending score order.
            Each ``Chunk`` carries ``text``, ``source_ref`` (Locator),
            and ``score``.
        """
        ...


__all__ = ["Chunk", "RetrievePort"]