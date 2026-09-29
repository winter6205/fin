"""In-process fake retrieve port for tests (T3, ADR 0030).

The fake port fills the seam between the host graph and the
retrieve capability. It is the **default pytest adapter**:
``compile_graph`` auto-builds one unless the caller asks for
another kind, so the suite stays hermetic. The production entry
points in ``fin.host.compile`` ask for ``retrieve_adapter="mcp"``
and get an ``MCPRetrieveAdapter``. The choice is configuration —
it is NOT inferred from the checkpointer, so ``fin-serve`` on
``InMemorySaver`` still serves the product corpus.

The fake port mirrors the public ``RetrievePort`` semantics on
its ``retrieve()`` method:

* empty / whitespace-only ``query`` → ``EmptyQueryError`` (T21 /
  §B2 第 5 条: invalid input, counted, never an empty result).
* provably conflicting filters (when ``manifest_docs`` gives the
  fake the bindings to prove it) → ``ConflictingFiltersError``.
* unknown ``document_id`` (when ``known_document_ids`` is set) →
  ``{"chunks": []}`` — a legal empty result.
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

from typing import Any, Mapping, Sequence

from fin.corpus.extracted import ExtractedPage, Locator
from fin.retrieve.input_validation import (
    InputValidationLedger,
    validate_filters,
    validate_query,
)
from fin.retrieve.port import (
    Chunk,
    resolve_search_mode,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY,
)
from fin.retrieve.read_port import (
    unit_text_from_pages,
    validate_company_source_ref,
)
from fin.retrieve.source_read import (
    UNKNOWN_LOCATOR,
    bounded_slice,
    source_error,
)


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
    manifest_docs
        Optional ``{document_id: {cik, form, ticker,
        filing_date}}`` bindings, mirroring ``BM25Adapter``.
        Supplied, they let the fake *prove* a conflicting
        filter set (T21 / SC43); omitted, fin holds no binding
        and does not invent a contradiction.
    pages_by_doc
        Optional ``{document_id: ExtractedPage pages}`` read
        manifest (ADR 0046 / ADR 0047): the fake answers
        ``read_slice()`` (the bounded ``read_source`` leg, ADR 0088)
        from this table, so a scripted serve test can provide readable
        bodies without any on-disk extracted tree. Omitted (the
        default), the fake corpus has no readable objects and every
        ``read_slice()`` answers the typed ``unknown_locator`` failure.
    input_validation_ledger
        Optional「输入校验失败」count (T21). Omitted, the port
        keeps an in-memory one — quiet, never blind.
    fast_graph_empty
        Optional recorded Fast 抽图 job fact (spec
        graph-index-standard-fast SC5): the Fast extractor built an
        EMPTY entity-relation graph over this 资料 set. When set,
        every legal ``retrieve`` answer carries the
        ``fast_graph_empty`` key next to an empty chunk list — a named
        fact, never the silent ``{"chunks": []}`` shape an ordinary
        empty collection keeps. Default ``False``: ordinary fakes must
        not teach every empty corpus a Fast failure.
    community_summary_unready
        Optional recorded global 综述 report-index job fact: the open
        global 档's report-TEXT arm is not-ready (the 社区 summary index
        is absent or unpopulated). When set, every legal ``retrieve``
        answer carries the ``community_summary_unready`` key next to an
        empty chunk list — the RECORDED refusal, named, so the host
        exits with ``community_summary_index_failure`` rather than an
        ordinary empty. Default ``False``: ordinary fakes must not teach
        every empty corpus a summary-index failure.
    """

    def __init__(
        self,
        hits_by_query: dict[str, list[Chunk]] | None = None,
        known_document_ids: set[str] | None = None,
        *,
        manifest_docs: Mapping[str, Mapping[str, Any]] | None = None,
        pages_by_doc: Mapping[str, Sequence[ExtractedPage]] | None = None,
        input_validation_ledger: InputValidationLedger | None = None,
        fast_graph_empty: bool = False,
        community_summary_unready: bool = False,
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
        self._manifest_docs: dict[str, Mapping[str, Any]] | None = (
            dict(manifest_docs) if manifest_docs is not None else None
        )
        # Defensive copy so a caller mutating the source mapping after
        # construction cannot change what ``read_slice`` serves.
        self._pages_by_doc: dict[str, Sequence[ExtractedPage]] = (
            dict(pages_by_doc) if pages_by_doc is not None else {}
        )
        self._input_validation_ledger = input_validation_ledger
        self._fast_graph_empty = fast_graph_empty
        self._community_summary_unready = community_summary_unready
        # Every completed ``retrieve`` call, in order, as the kwargs the
        # port received (``test_retrieve_typed_exit``'s recording style).
        # Rejected calls (named input errors) are NOT recorded: nothing
        # was received.
        self.calls: list[dict[str, Any]] = []

    @property
    def input_validation_failures(self) -> InputValidationLedger:
        """The「输入校验失败」count for this port (T21 / SC42)."""
        ledger = self._input_validation_ledger
        if ledger is None:
            ledger = InputValidationLedger()
            self._input_validation_ledger = ledger
        return ledger

    def _passes_metadata(
        self,
        document_id: str,
        *,
        cik: str | None,
        form: str | None,
        ticker: str | None,
        filing_date: str | None,
    ) -> bool:
        """AND-evaluate the supplied filters against this doc's bindings.

        Mirrors ``BM25Adapter._passes_metadata``: a document with no
        bindings passes (only ``document_id`` is a hard membership test),
        a bound value that differs excludes the row.
        """
        docs = self._manifest_docs
        if docs is None:
            return True
        meta = docs.get(document_id)
        if meta is None:
            return True
        for field, value in (
            ("cik", cik),
            ("form", form),
            ("ticker", ticker),
            ("filing_date", filing_date),
        ):
            if value is not None and meta.get(field) != value:
                return False
        return True

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
        search_mode: str | None = None,
    ) -> dict[str, list[Chunk]]:
        """Return ``{"chunks": [...]}`` or ``{"chunks": []}``.

        Mirrors the ``RetrievePort`` protocol:

        * empty / whitespace ``query`` → ``EmptyQueryError``
        * provably conflicting filters (needs ``manifest_docs``)
          → ``ConflictingFiltersError``
        * ``search_mode`` mirrors the port's 档 contract exactly
          (T2): out-of-set → ``InvalidSearchModeError``, unopened
          global/sql → ``TierUnavailableError`` (Fake is not a 档 and does
          not rank differently per 档 — spec SC8); an accepted token
          is recorded on ``self.calls`` so tests can assert what the
          caller asked for
        * unknown ``document_id`` (when ``known_document_ids`` is
          set) → ``{"chunks": []}``
        * empty EDGAR-shaped filter values
          (``cik="" / form="" / ticker="" / filing_date=""``)
          → ``{"chunks": []}`` per spec "empty filter values
          behave as empty hits"
        * the four additive filters are NO-OPs without
          ``manifest_docs`` (there is no metadata table to
          consult); with it they AND-narrow the hits by the
          document's bindings, so an unknown value really does
          answer with an empty list
        * ``limit`` defaults to 8, hard cap 20 (ADR 0009)
        * constructed with ``fast_graph_empty=True``: every legal
          call answers ``{"chunks": [], "fast_graph_empty": True}`` —
          the RECORDED Fast 抽图 job fact (an empty entity-relation
          graph over the 资料 set, spec graph-index-standard-fast
          SC5). Invalid input still names first: the job fact is not
          a licence to swallow an ``EmptyQueryError``.
        * constructed with ``community_summary_unready=True``: every
          legal call answers
          ``{"chunks": [], "community_summary_unready": True}`` — the
          RECORDED report-index not-ready fact, so the run exits
          ``community_summary_index_failure`` instead of an ordinary
          empty. Invalid input still names first, exactly as above.
        """
        # Invalid input first, before any lookup: an empty query, a
        # provably unsatisfiable filter set, and an illegal / unopened
        # 档 are named failures (T21 / search-mode T2), never the miss
        # shape.
        ledger = self.input_validation_failures
        validate_query(query, ledger=ledger)
        effective_search_mode = resolve_search_mode(search_mode)
        validate_filters(
            document_id=document_id,
            cik=cik,
            form=form,
            ticker=ticker,
            filing_date=filing_date,
            manifest_docs=self._manifest_docs,
            ledger=ledger,
        )
        self.calls.append(
            {
                "query": query,
                "document_id": document_id,
                "cik": cik,
                "form": form,
                "ticker": ticker,
                "filing_date": filing_date,
                "limit": limit,
                "search_mode": effective_search_mode,
            }
        )

        # Unknown document_id filter: the call is well-formed
        # but the corpus has no such document. ``None`` (the
        # default) means "no membership test" so the filter
        # passes through.
        if self._fast_graph_empty:
            # The recorded Fast job fact: an empty entity-relation
            # graph can cite no chunk, so the hit table is deliberately
            # shadowed — and the emptiness arrives NAMED, which is the
            # whole difference from a silent empty collection (the host
            # decision must not have to guess 未就绪 here, spec SC5).
            return {"chunks": [], RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True}
        if self._community_summary_unready:
            # The recorded global 综述 report-index not-ready refusal — the
            # same un-gated shape as the Fast fact above: constructing the
            # fake this way IS the author asserting its report arm
            # refuses. The send answers empty but NAMED, so the host exits
            # ``community_summary_index_failure`` (a bare empty is a
            # healthy C miss, never this job fact).
            return {
                "chunks": [],
                RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
            }
        if (
            document_id is not None
            and self._known_doc_ids is not None
            and document_id not in self._known_doc_ids
        ):
            return {"chunks": []}

        # EDGAR-shaped empty filter values → miss shape per spec.
        if any(v == "" for v in (cik, form, ticker, filing_date)):
            return {"chunks": []}

        effective_limit = _resolve_limit(limit)
        chunks = self._hits.get(query, [])
        # Without bindings the metadata filters stay NO-OPs (the fake
        # has no metadata table to consult). With them, an unknown value
        # must actually narrow the result to nothing — otherwise the
        # fake could not tell an unknown filter (legal empty) apart from
        # a conflicting one (invalid input).
        if self._manifest_docs is not None:
            chunks = [
                chunk
                for chunk in chunks
                if self._passes_metadata(
                    chunk.source_ref.document_id,
                    cik=cik,
                    form=form,
                    ticker=ticker,
                    filing_date=filing_date,
                )
            ]
        # Truncate to the resolved limit; the underlying list
        # is not mutated so the next call sees the same corpus.
        return {"chunks": list(chunks[:effective_limit])}

    def read_slice(
        self,
        source_ref: Mapping[str, Any],
        position: int = 0,
    ) -> dict[str, Any]:
        """Answer one bounded ``read_source`` slice from ``pages_by_doc``.

        The fake company leg (ADR 0088, spec thread-attachment-source-read
        SC7/SC8/SC9): the hermetic stand-in for
        :meth:`MCPRetrieveAdapter.read_slice`, resolving one frozen unit
        from the same manifest and slicing it through the shared
        :func:`fin.retrieve.source_read.bounded_slice`, so a test drives
        the identical 8,000-code-point / 32-KiB bounds and the identical
        error codes without any MCP server. Locator shape failures are
        the shared ``invalid_locator`` verdict; a document, version, or
        unit the manifest does not carry is ``unknown_locator``. Never
        raises, never carries text on a failure.
        """
        located = validate_company_source_ref(source_ref)
        if not isinstance(located, tuple):
            # EXIT: the shape verdict is named before any lookup, so a
            # malformed identity never depends on what the manifest holds.
            return located
        document_id, version, unit = located
        text = unit_text_from_pages(
            self._pages_by_doc, document_id, version, unit
        )
        if text is None:
            # EXIT: no such document / version / unit in the manifest —
            # the named miss, never an empty slice.
            return source_error(UNKNOWN_LOCATOR)
        return bounded_slice(dict(source_ref), text, position)


__all__ = ["FakeRetrievePort"]
