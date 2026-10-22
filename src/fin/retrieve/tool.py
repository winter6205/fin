"""Host tool wrapper for the v1 retrieve capability.

This module is the seam between the **retrieve port**
(``fin.retrieve.port``) and the host graph's surface. T6 binds
``retrieve`` as a third tool alongside ``list_files`` and
``read_source``; this function is the plain-Python callable the
LangChain ``@tool`` decorator wraps.

The wrapper does NOT re-implement scoring: it delegates to the
port's ``retrieve``. The adapter behind the port is the only source
of truth for ranking; the wrapper exists so the host graph has one
import point that returns the contract dict shape
(``{"chunks": [...]}``). Which adapter sits behind the port is the
factory's decision (ADR 0032: MCP client in product, fake port in
default pytest), so this module names no concrete implementation.

The host graph builds one adapter at boot and shares it across the
run by passing it in via the ``adapter`` keyword. The wrapper's
``adapter`` parameter is the seam that lets the host graph inject a
process-wide shared adapter without going through module-level
state.
"""

from __future__ import annotations

from typing import Any, Mapping

from fin.retrieve.port import DocumentScope, RetrievePort


def retrieve(
    query: str,
    *,
    document_id: str | None = None,
    cik: str | None = None,
    form: str | None = None,
    ticker: str | None = None,
    filing_date: str | None = None,
    limit: int = 8,
    search_mode: str | None = None,
    structured_request: Mapping[str, Any] | None = None,
    sql_window_start: str | None = None,
    sql_window_end: str | None = None,
    filters: Mapping[str, Any] | None = None,
    document_scope: DocumentScope | None = None,
    adapter: RetrievePort | None = None,
) -> dict[str, Any]:
    """Adapter-bound retrieve: returns the ADR 0009 contract shape.

    Parameters
    ----------
    query
        Lexical query. Empty / whitespace-only queries raise the
        port's ``EmptyQueryError`` (T21: invalid input, not a miss).
    document_id
        Optional corpus filter. Unknown document_id yields
        ``{"chunks": []}``.
    cik / form / ticker / filing_date
        Additive EDGAR-shaped optional filters (ADR 0031), forwarded
        to the adapter. Empty and unknown values yield
        ``{"chunks": []}``; a provably conflicting set raises the
        port's ``ConflictingFiltersError``.
    limit
        Default 8, hard cap 20 (ADR 0009). The adapter resolves the
        cap internally; we just pass through.
    search_mode
        检索档 token (spec ``search-mode-retrieve.md``): passed
        through untouched — the wrapper neither assigns a 档 (that is
        the host cascade's job) nor validates one (the port's named
        failures stay single-sourced behind the port). The host graph
        resolves the value PER CALL off that run's cascade audit
        (``fin.host.grounded.search_mode_decision.assigned_retrieve_mode``), so the
        wrapper is the seam, not the source: an omitted value means "send
        no 档" and iknow answers with its own default.
    sql_window_start / sql_window_end
        The ``sql`` 档's inclusive expiry window: passed through
        untouched like the 档 token — the port seam
        (``fin.retrieve.port.resolve_sql_window``) names a missing or
        malformed bound pair ``invalid_business_query`` before anything
        reaches the wire, and the wrapper does not duplicate that gate.
    structured_request
        Optional typed structured-query object. It is passed through
        unchanged to the adapter when present and omitted from legacy
        calls to preserve their existing adapter contract.
    filters
        The generic metadata filter object (spec
        ``qa-chain-corpus-retrieval``): at most the four catalog-owned
        keys ``title`` / ``doc_type`` / ``company`` / ``period``,
        passed through untouched — the port seam validates it BEFORE
        any I/O and answers ``{"error": "invalid_filter"}`` for an
        out-of-contract map. ``None`` (the default) is absent: whole
        authorized scope.
    document_scope
        Optional trusted allowlist of ``(document_id, version)`` entries.
        ``None`` preserves the legacy unrestricted call and is omitted from
        adapter kwargs; a present empty or malformed scope is refused as
        ``unsupported_source_scope``. An explicit ``document_id`` intersects
        this scope; a blank id keeps its legacy miss shape. Adapters refuse
        modes that cannot enforce the version pairs, such as global, SQL, or
        scoped reports.
    adapter
        The shared ``RetrievePort`` implementation. The host graph
        (T6) constructs one adapter per run and threads it through
        every ``retrieve`` call. If ``None``, the wrapper raises
        because silently constructing a default adapter would hide a
        misconfigured host graph — T6 always provides one.

    Returns
    -------
    dict
        ``{"chunks": [Chunk, ...]}`` in descending score order.
        Each ``Chunk`` carries ``text``, ``source_ref`` (Locator),
        and ``score``. A ``sql`` run may carry the ``database_rows``
        sibling beside an empty chunk list (spec §8.0).
    """
    if adapter is None:
        raise ValueError(
            "fin.retrieve.tool.retrieve requires an explicit `adapter`; "
            "the host graph (T6) must construct one retrieve port per "
            "run and share it across all calls."
        )
    arguments: dict[str, Any] = {
        "query": query,
        "document_id": document_id,
        "cik": cik,
        "form": form,
        "ticker": ticker,
        "filing_date": filing_date,
        "limit": limit,
        "search_mode": search_mode,
        "sql_window_start": sql_window_start,
        "sql_window_end": sql_window_end,
        "filters": filters,
    }
    if structured_request is not None:
        arguments["structured_request"] = structured_request
    if document_scope is not None:
        arguments["document_scope"] = document_scope
    return adapter.retrieve(**arguments)


__all__ = ["retrieve"]
