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
# ``filters`` record (sibling spec ``search-mode-retrieve.md``:
# ``filters.search_mode`` ∈ ``A|B|C|D``; missing → iknow's default B).
# The value set is the closed 档 SSOT (``fin.retrieve.tiers``): an
# out-of-set value is the documented invalid-mode code
# ``invalid_search_mode`` — the same token the iknow retrieve schema
# answers with, mirrored port-side by ``InvalidSearchModeError``.
# One place; the adapters and the tests import it.
SEARCH_MODE_FILTER_KEY: Final[str] = "search_mode"


class InvalidSearchModeError(InputValidationError):
    """``search_mode`` is not one of the closed 档 tokens.

    Invalid input (the T21 split), not a miss: it raises rather than
    answering through some other 档 — "not silent C" and "not silent
    B" are the same rule. The host tool's existing
    ``InputValidationError`` handling turns this into the named
    ``{"error": "invalid_search_mode"}`` envelope. Not counted on the
    input-validation ledger: the ledger's classes are the §B2 four,
    and this port-level token is not one of them.
    """

    code = "invalid_search_mode"


class TierUnavailableError(InputValidationError):
    """A legal 档 token whose product side is not open yet.

    Carries the SAME public code the send door uses for the request
    (``TIER_UNAVAILABLE_CODE``: C → ``tier_not_open``,
    D → ``real_db_not_wired``) so a pin that reaches the port gets
    the identical named failure — never a retrieve dressed as another
    档. It subclasses the input-error family so every host seam that
    already converts a named input error converts this one too; the
    empty :attr:`input_class` marks that it is deliberately NOT a §B2
    ledger class (the 档 token itself is well-formed; the product just
    has not opened it).
    """

    input_class = ""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        # Instance-level: one class carries both「未开」codes, each read
        # straight from ``TIER_UNAVAILABLE_CODE`` at the raise site.
        self.code = code


def resolve_search_mode(search_mode: str | None) -> str | None:
    """Validate a 档 token against the closed set; return it for the wire.

    ``None`` passes through as "do not send" (iknow's default B).
    Matching is EXACT — the send door normalizes human input, so a
    port caller that sends ``"c"`` or ``" summary "`` made a wiring
    mistake this helper must name, not guess around.

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
            does NOT send the field — iknow's own default B answers,
            backward compatible. An out-of-set token raises
            ``InvalidSearchModeError``; a legal but unopened 档 raises
            ``TierUnavailableError`` carrying the ``TIER_UNAVAILABLE_CODE``
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
