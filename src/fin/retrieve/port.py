"""Retrieve port: the v1 ``RetrievePort`` protocol and the ``Chunk`` value object.

The port is the seam between the v1 host graph (which only sees a
typed ``retrieve`` callable) and the adapter (which does the actual
ranking). T6 binds a concrete adapter to a LangChain ``@tool``; later
tickets can swap in an MCP-backed adapter (e.g. an iknow-memory
client) without touching the host graph.

The contract shape is fixed by ADR 0009 and ADR 0031:

    ``retrieve(query, *, document_id=None, cik=None, form=None,
                ticker=None, filing_date=None, limit=8,
                search_mode=None)`` →
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
from typing import Final, Protocol, runtime_checkable

from fin.corpus.extracted import Locator
from fin.retrieve.input_validation import InputValidationError
from fin.retrieve.tiers import RETRIEVE_TIERS, TIER_UNAVAILABLE_CODE

# The wire key the iknow-memory ``retrieve`` schema accepts inside its
# ``filters`` record (sibling spec ``search-mode-retrieve.md``, tokens
# renamed by ``search-mode-lexical-sql-tokens.md``): its value is one member
# of the closed 档 set in :mod:`fin.retrieve.tiers`, re-spelled nowhere on
# this side; a missing key means iknow's own default 知识问答 = ``graph``.
# The letters those tokens once wore are retired everywhere, entry included,
# so nothing downstream has to know them. An out-of-set value is the
# documented invalid-mode code ``invalid_search_mode`` — the same token the
# iknow retrieve schema answers with, mirrored port-side by
# ``InvalidSearchModeError``. One place; the adapters and the tests import it.
SEARCH_MODE_FILTER_KEY: Final[str] = "search_mode"

# The outcome-record key by which a RECORDED Fast 抽图 job fact travels:
# the Fast extractor built an empty entity-relation graph over the
# 资料 set (spec graph-index-standard-fast SC5). Only an explicit
# ``True`` is the fact — a silent empty result is 资料未就绪
# (``corpus_not_ready``), and fin must never upgrade absence into
# this code. The port layer owns the KEY (the fake port answers with
# it, the host decision reads it; ``fin.host.named_failures``
# re-exports it onto the codes surface); the public CODE spelling is
# that module's reserved token and awaits the spec's Open Question 1.
RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: Final[str] = "fast_graph_empty"

# The outcome-record key by which a RECORDED global 综述 index-not-ready
# fact travels: the upstream retrieve answered its typed
# ``community_summary_index_failure`` refusal (the 社区 summary index the
# report arm reads is absent or unpopulated). Only that named upstream
# refusal sets it — a silent empty is still ``corpus_not_ready``, and fin
# must never upgrade a bare empty into this code. The adapter records it
# off the MCP error text; ``fin.host.named_failures`` re-exports it onto
# the codes surface and the public CODE is :data:`COMMUNITY_SUMMARY_INDEX_FAILURE`.
RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: Final[str] = "community_summary_unready"


class InvalidSearchModeError(InputValidationError):
    """``search_mode`` is not one of the closed 档 tokens.

    Invalid input (the T21 split), not a miss: it raises rather than
    answering through some other 档 — "not silent ``global``" and "not
    silent ``graph``" are the same rule. The host tool's existing
    ``InputValidationError`` handling turns this into the named
    ``{"error": "invalid_search_mode"}`` envelope. Not counted on the
    input-validation ledger: the ledger's classes are the §B2 four,
    and this port-level token is not one of them.
    """

    code = "invalid_search_mode"


class TierUnavailableError(InputValidationError):
    """A legal 档 token whose product side is not open yet.

    Carries the SAME public code the send door uses for the request
    (``TIER_UNAVAILABLE_CODE``: ``sql`` → ``real_db_not_wired``;
    ``global`` is now open, so it is never raised for that 档) so a pin
    that reaches the port gets the identical named failure — never a
    retrieve dressed as another 档. It subclasses the input-error family so
    every host seam that already converts a named input error converts
    this one too; the empty :attr:`input_class` marks that it is
    deliberately NOT a §B2 ledger class (the 档 token itself is
    well-formed; the product just has not opened it).
    """

    input_class = ""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        # Instance-level: one class carries both「未开」codes, each read
        # straight from ``TIER_UNAVAILABLE_CODE`` at the raise site.
        self.code = code


def resolve_search_mode(search_mode: str | None) -> str | None:
    """Validate a 档 token against the closed set; return it for the wire.

    ``None`` passes through as "do not send" — iknow then answers with
    its own default 知识问答 (``graph``), never with a spelling the host chose.
    Matching is EXACT, and that exactness is the point: every entry surface
    types this value from the closed set already, so a port caller that
    arrives with ``"sql "`` or ``" SQL "`` reached the wire past the boundary
    that would have refused it — a wiring mistake this helper must name, not
    guess around.

    That exactness is also why a retired letter is REFUSED here and at
    every entry seam alike: there is no second spelling of any 档 to
    resolve, so the closed set IS the whole vocabulary from the operator's
    keystroke to the wire. A letter reaching THIS seam means the boundary
    was skipped. Normalizing at the seam would hide that bug and put a 档
    choice behind a request that never made it — the silent mapping the
    rename forbids.

    The 档 SSOT lives in the neutral leaf :mod:`fin.retrieve.tiers`,
    imported at module level: the port may not import ``fin.host``
    (whose graph imports this module), and :mod:`fin.host.named_failures`
    re-exports the same constants from there.
    """
    if search_mode is None:
        return None
    if search_mode not in RETRIEVE_TIERS:
        raise InvalidSearchModeError(
            f"search_mode {search_mode!r} is not one of "
            f"{'|'.join(RETRIEVE_TIERS)}"
        )
    if search_mode in TIER_UNAVAILABLE_CODE:
        raise TierUnavailableError(
            f"检索档 {search_mode} is not open on this port",
            code=TIER_UNAVAILABLE_CODE[search_mode],
        )
    return search_mode


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
        search_mode: str | None = None,
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
        search_mode
            检索档 (spec ``search-mode-retrieve.md``): one of the
            closed ``RETRIEVE_TIERS`` tokens forwarded to the ranking
            service as ``filters.search_mode``. ``None`` (the default)
            does NOT send the field — iknow's own default 知识问答
            (``graph``) answers, backward compatible. An out-of-set
            token raises ``InvalidSearchModeError``; a legal but
            unopened 档 raises ``TierUnavailableError`` carrying the
            ``TIER_UNAVAILABLE_CODE``
            token. Never a silent downgrade to another 档 — call
            :func:`resolve_search_mode` to enforce this.

        Returns
        -------
        dict
            ``{"chunks": [Chunk, ...]}`` in descending score order.
            Each ``Chunk`` carries ``text``, ``source_ref`` (Locator),
            and ``score``.
        """
        ...


__all__ = [
    "Chunk",
    "InvalidSearchModeError",
    "RetrievePort",
    "SEARCH_MODE_FILTER_KEY",
    "TierUnavailableError",
    "resolve_search_mode",
]
