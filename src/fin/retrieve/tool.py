"""Host tool wrapper for the v1 retrieve capability.

This module is the seam between the **retrieve port**
(``fin.retrieve.port``) and the host graph's surface. T6 binds
``retrieve`` as a third tool alongside ``list_files`` and
``read_file``; this function is the plain-Python callable the
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

from fin.retrieve.port import Chunk, RetrievePort


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
    adapter: RetrievePort | None = None,
) -> dict[str, list[Chunk]]:
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
        (``fin.host.search_mode_decision.assigned_retrieve_mode``), so the
        wrapper is the seam, not the source: an omitted value means "send
        no 档" and iknow answers with its own default.
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
        and ``score``.
    """
    if adapter is None:
        raise ValueError(
            "fin.retrieve.tool.retrieve requires an explicit `adapter`; "
            "the host graph (T6) must construct one retrieve port per "
            "run and share it across all calls."
        )
    return adapter.retrieve(
        query=query,
        document_id=document_id,
        cik=cik,
        form=form,
        ticker=ticker,
        filing_date=filing_date,
        limit=limit,
        search_mode=search_mode,
    )


__all__ = ["retrieve"]