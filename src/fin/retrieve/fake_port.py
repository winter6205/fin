"""In-process fake retrieve port for tests (T3, ADR 0030).

The fake port fills the seam between the host graph and the
retrieve capability. It is the **default pytest adapter** —
production ``compile_graph`` auto-builds an ``MCPRetrieveAdapter``;
``InMemorySaver`` callers (tests, ``compile_for_runtime_dev``)
auto-build a ``FakeRetrievePort`` so the suite stays hermetic.

The fake port mirrors the public ``RetrievePort`` semantics on
its ``retrieve()`` method:

* empty / whitespace-only ``query`` → ``{"chunks": []}``
  (short-circuits; never raises).
* unknown ``document_id`` (when ``known_document_ids`` is set) →
  ``{"chunks": []}``.
* ``limit`` defaults to 8, hard cap 20; ``<= 0`` falls back to 8;
  ``> 20`` clamps to 20 (ADR 0009).
* the constructor holds a static dict; ``retrieve()`` does not
  re-tokenize or re-build, so concurrent calls share state
  read-only and are safe.

The fake does NOT implement a tokenized index or BM25 ranking.
It is a pure dictionary lookup keyed on the raw query string;
tests that need ranking semantics use ``BM25Adapter`` (legacy)
or ``MCPRetrieveAdapter`` (with a scripted transport).
"""

from __future__ import annotations

from fin.corpus.extracted import Locator
from fin.retrieve.port import Chunk


_DEFAULT_LIMIT = 8
_LIMIT_CAP = 20


def _resolve_limit(limit: int) -> int:
    """Apply the port's limit rules: default 8, cap 20 (ADR 0009).

    * ``limit <= 0`` → 8 (treat as "use the default").
    * ``limit > 20`` → 20 (the hard cap).
    * otherwise → ``limit`` unchanged.
    """
    if limit <= 0:
        return _DEFAULT_LIMIT
    if limit > _LIMIT_CAP:
        return _LIMIT_CAP
    return limit


class FakeRetrievePort:
    """In-process fake that satisfies ``RetrievePort``.

    Parameters
    ----------
    hits_by_query
        Static corpus keyed by the raw query string. Each
        value is the chunk list returned for that query (in
        order; truncated to ``limit``).
    known_document_ids
        Optional set of accepted ``document_id`` values. When
        supplied, an unknown ``document_id`` filter yields
        ``{"chunks": []}`` without an index lookup; when
        omitted (the default), no ``document_id`` membership
        test is performed (all filters pass through).
    """

    def __init__(
        self,
        hits_by_query: dict[str, list[Chunk]] | None = None,
        known_document_ids: set[str] | None = None,
    ) -> None:
        # Defensive copy so a caller mutating the source dict
        # after construction cannot affect the fake.
        self._hits: dict[str, list[Chunk]] = {
            key: list(value) for key, value in (hits_by_query or {}).items()
        }
        # ``None`` disables the membership test (every filter
        # passes); an empty set rejects every filter.
        self._known_doc_ids: frozenset[str] | None = (
            frozenset(known_document_ids)
            if known_document_ids is not None
            else None
        )

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
        """Return ``{"chunks": [...]}`` or ``{"chunks": []}``.

        Mirrors the ``RetrievePort`` protocol:

        * empty / whitespace ``query`` → ``{"chunks": []}``
        * unknown ``document_id`` (when ``known_document_ids`` is
          set) → ``{"chunks": []}``
        * empty EDGAR-shaped filter values
          (``cik="" / form="" / ticker="" / filing_date=""``)
          → ``{"chunks": []}`` per spec "empty filter values
          behave as empty hits"
        * the four additive filters are NO-OPs on the fake
          (no manifest metadata table); callers that need
          manifest-aware filtering use ``BM25Adapter`` (legacy)
          or ``MCPRetrieveAdapter`` (production)
        * ``limit`` defaults to 8, hard cap 20 (ADR 0009)
        """
        # Empty / whitespace query: short-circuit before the
        # dict lookup. The model may pass ``""`` while
        # reasoning aloud — we must NOT treat that as a hit.
        if not query or not query.strip():
            return {"chunks": []}

        # Unknown document_id filter: the call is well-formed
        # but the corpus has no such document. ``None`` (the
        # default) means "no membership test" so the filter
        # passes through.
        if (
            document_id is not None
            and self._known_doc_ids is not None
            and document_id not in self._known_doc_ids
        ):
            return {"chunks": []}

        # EDGAR-shaped empty filter values → miss shape per spec.
        # Non-None values are NO-OPs on the fake (no manifest
        # metadata table) — the dict lookup proceeds unchanged.
        if any(v == "" for v in (cik, form, ticker, filing_date)):
            return {"chunks": []}

        effective_limit = _resolve_limit(limit)
        chunks = self._hits.get(query, [])
        # Truncate to the resolved limit; the underlying list
        # is not mutated so the next call sees the same corpus.
        return {"chunks": list(chunks[:effective_limit])}


__all__ = ["FakeRetrievePort"]