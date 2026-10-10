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
``fin.corpus.Locator`` instance so the citation gate can read
``.document_id`` / ``.version`` / ``.unit`` directly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Protocol, cast, runtime_checkable

from fin.corpus.extracted import Locator
from fin.retrieve.input_validation import InputValidationError
from fin.retrieve.tiers import (
    RETRIEVE_TIERS,
    SQL_NO_MATCHING_ROWS,
    TIER_UNAVAILABLE_CODE,
)

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
# off the named ``error`` code on the MCP error body;
# ``fin.host.named_failures`` re-exports it onto the codes surface and the
# public CODE is :data:`COMMUNITY_SUMMARY_INDEX_FAILURE`.
RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: Final[str] = "community_summary_unready"

# The outcome-record key by which the sql 档's RECORDED empty result
# travels: the permitted query ran correctly against the live ledger and
# matched no row (spec search-mode-business-sql-contract §7, code
# :data:`fin.retrieve.tiers.SQL_NO_MATCHING_ROWS`). The producer declares
# that token as its structured wire failure — it never answers an empty
# ``database_rows`` as success — and the adapter records it here, exactly
# like the community-summary flag, so the run refuses under its own name
# and not as ``corpus_not_ready`` (whose ingest advice is factually wrong
# after a database miss). Only the named upstream exit sets it.
RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS: Final[str] = SQL_NO_MATCHING_ROWS

# The envelope sibling key carrying this round's ledger rows (spec §8.0,
# E1 envelope contract enterprise-evidence-contract §4.4): a row is never
# a chunk, so it rides beside ``chunks`` as its own named key — and since
# E1 the value is an OBJECT ``{schema, schema_version, rows,
# result_count}``, each row ``{source, record_key, queried_at, fields,
# summary}`` with ``record_key`` an object, the producer's own spelling.
# The ENVELOPE names the format of the list; a row inside it does not
# re-declare a per-row ``schema``. The bare-array producer shape is
# obsolete. ABSENT (never an empty sibling) when the 档 was not sql, so
# every document-path envelope stays byte-identical.
DATABASE_ROWS_KEY: Final[str] = "database_rows"

# The envelope's own fields and the frozen schema token (iknow
# 02b8669). The token is the stable NAME of the shape; its version is
# the explicit integer sibling ``schema_version`` (spec §2.1 rule 1).
# Full strict validation of the envelope — unknown token or version,
# malformed object, ``rows``/``result_count`` consistency, and the
# per-row key set — is owned by the E3 evidence validator and is
# deliberately NOT done here; the two helpers below only READ and
# BUILD the shape.
DATABASE_ROWS_SCHEMA: Final[str] = "fin.evidence.retrieve_rows"
DATABASE_ROWS_SCHEMA_FIELD: Final[str] = "schema"
DATABASE_ROWS_SCHEMA_VERSION_FIELD: Final[str] = "schema_version"
# Minted only where the serialized shape can actually change (spec
# §2.1 rule 1), never as a label for the rollout stage.
DATABASE_ROWS_SCHEMA_VERSION: Final[int] = 1
DATABASE_ROWS_ROWS_FIELD: Final[str] = "rows"
DATABASE_ROWS_RESULT_COUNT_FIELD: Final[str] = "result_count"


def database_rows_of(sibling: object) -> list[Any] | None:
    """The row list inside one ``DATABASE_ROWS_KEY`` sibling value.

    ``None`` means "no rows applicable" — the key is absent or carries a
    shape this consumer cannot read (the obsolete bare array, a
    non-mapping, or no list under ``rows``). Reading stays permissive
    about what the rows themselves contain: per-row admission drops
    happen where evidence is built, and envelope-level contract
    validation is the E3 validator's boundary, not this reader's.
    """
    if not isinstance(sibling, Mapping):
        return None
    rows = sibling.get(DATABASE_ROWS_ROWS_FIELD)
    return list(rows) if isinstance(rows, list) else None


def new_database_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build one row-object sibling for an answer FIN itself authors.

    A real wire answer carries the PRODUCER's ``schema`` /
    ``result_count`` and the consumer passes them through untouched;
    this builder is only for the envelopes fin writes as author — the
    sql 档's recorded zero-row exit (empty rows, count 0) and the fake
    ports' scripted answers mirroring the wire shape.
    """
    return {
        DATABASE_ROWS_SCHEMA_FIELD: DATABASE_ROWS_SCHEMA,
        DATABASE_ROWS_SCHEMA_VERSION_FIELD: DATABASE_ROWS_SCHEMA_VERSION,
        DATABASE_ROWS_ROWS_FIELD: [dict(row) for row in rows],
        DATABASE_ROWS_RESULT_COUNT_FIELD: len(rows),
    }


# The structured-corpus-evidence lane's own sibling. It is NOT a
# ``database_rows`` sibling and not a ``chunks`` entry: it carries a typed
# entity/assertion result or an exact count receipt with its own finite
# evidence reference, and it never travels in ``chunks[]``. ABSENT (never
# an empty sibling) on every lane that did not run a structured query, so
# a document envelope stays byte-identical.
STRUCTURED_RESULT_KEY: Final[str] = "structured_result"

# QP8 keeps structured-query refusals distinct from empty results and the
# legacy SQL ledger's zero-row outcome.
MALFORMED_STRUCTURED_QUERY_CODE: Final[str] = "malformed_structured_query"
STRUCTURED_SEARCH_UNAVAILABLE_CODE: Final[str] = "structured_search_unavailable"

# The producer's token for that sibling and its explicit integer version
# (ADR 0093: the token is the stable name, never a versioned suffix).
STRUCTURED_RESULT_SCHEMA_FIELD: Final[str] = "schema"
STRUCTURED_RESULT_SCHEMA_VERSION_FIELD: Final[str] = "schema_version"
STRUCTURED_RESULT_ROWS_FIELD: Final[str] = "rows"
STRUCTURED_RESULT_COUNT_FIELD: Final[str] = "count"
STRUCTURED_RESULT_RECEIPT_FIELD: Final[str] = "count_receipt"


def structured_result_of(sibling: object) -> Mapping[str, Any] | None:
    """The one ``STRUCTURED_RESULT_KEY`` sibling value, or ``None``.

    ``None`` means "no structured result applicable" — the key is absent,
    or it carries a non-object this consumer cannot read. Reading stays
    permissive about the contents; the exact key set, version and bounds
    are the structured lane validator's boundary in
    :mod:`fin.host.structured_evidence`, not this reader's. The retrieve
    adapter and host use this accessor to preserve the sibling without
    interpreting its result fields.
    """
    if not isinstance(sibling, Mapping):
        return None
    value = sibling.get(STRUCTURED_RESULT_KEY)
    if not isinstance(value, Mapping):
        return None
    return value


def _structured_query_refusal_code(
    structured_request: object,
    *,
    search_mode: str | None,
    has_outer_filters: bool,
) -> str | None:
    """Return the fin-side refusal for a structured request it cannot bind.

    Query fields and predicates remain the structured backend's concern.
    """
    if structured_request is None:
        return None
    if (
        not isinstance(structured_request, Mapping)
        or search_mode != "sql"
        or has_outer_filters
    ):
        return MALFORMED_STRUCTURED_QUERY_CODE
    return None


# The sql 档's expiry-window routing filters (the iknow retrieve schema
# reads both keys inside ``filters`` and strips them before matching;
# ``mode-d-arm`` binds them as parameters and the executor alone decides
# whether a window is bindable). Both bounds are REQUIRED for a sql
# request — there is no defaulted window on either side of the wire.
SQL_WINDOW_START_FILTER_KEY: Final[str] = "sql_window_start"
SQL_WINDOW_END_FILTER_KEY: Final[str] = "sql_window_end"


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

    Since A2 the table (:data:`fin.retrieve.tiers.TIER_UNAVAILABLE_CODE`)
    is EMPTY — every 档 in the closed set is open — so this class is never
    raised on today's builds. It stays as the generic machinery for a
    future plan slot: should a 档 ever be closed again, the port, the send
    door, and the cascade all read the one table row and the pin gets the
    identical named failure. ``global`` is now open, so it is never raised
    for that 档. It subclasses the input-error family so every host seam
    that already converts a named input error converts this one too; the
    empty :attr:`input_class` marks that it is deliberately NOT a §B2
    ledger class (the 档 token itself is well-formed; the product just has
    not opened it).
    """

    input_class = ""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        # Instance-level: one class carries both「未开」codes, each read
        # straight from ``TIER_UNAVAILABLE_CODE`` at the raise site.
        self.code = code


class InvalidBusinessQueryError(InputValidationError):
    """A ``sql`` 档 request whose expiry window the query cannot bind.

    The port mirrors the producer's own refusal (iknow's business-query
    executor names ``invalid_business_query`` before any connection
    exists, and refuses to default a window), so a missing or malformed
    bound pair is named without spending a round trip. Same code, same
    meaning on both sides of the wire. Invalid input (T21 split): a
    request that never named its window is not a ledger with no rows.
    """

    code = "invalid_business_query"


# Inclusive ISO date (YYYY-MM-DD) — the producer's bound-parameter shape.
# ``date.fromisoformat`` alone is too generous (Python accepts the
# basic-format ``20260101``), so the regex pins the spelled form the
# ledger template binds and the parser pins the calendar day.
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _iso_day(raw: str | None) -> date | None:
    """The calendar day ``raw`` spells as YYYY-MM-DD, or ``None``."""
    if not isinstance(raw, str):
        return None
    match = _ISO_DATE_RE.match(raw)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def resolve_sql_window(
    search_mode: str | None,
    sql_window_start: str | None,
    sql_window_end: str | None,
) -> tuple[str, str] | None:
    """Validate the expiry window a ``sql`` 档 request must carry.

    Returns the ``(start, end)`` pair verbatim for a ``sql`` request with
    a bindable window, ``None`` for every non-sql 档 (the window keys are
    the sql 档's alone and must never ride a document request), and
    raises :class:`InvalidBusinessQueryError` when a ``sql`` request is
    missing a bound, cannot be read as a calendar day, or orders the
    bounds backwards. No default exists here on purpose: a window fin
    invented would be a query the operator never asked.
    """
    if search_mode != "sql":
        return None
    start = _iso_day(sql_window_start)
    end = _iso_day(sql_window_end)
    if start is None or end is None or end < start:
        raise InvalidBusinessQueryError(
            "检索档 sql requires filters.sql_window_start / "
            "sql_window_end as inclusive YYYY-MM-DD dates "
            f"(got {sql_window_start!r} .. {sql_window_end!r})"
        )
    # Both bounds proved to be YYYY-MM-DD strings via ``_iso_day``;
    # ``cast`` states that narrowing explicitly instead of asserting it.
    return cast(str, sql_window_start), cast(str, sql_window_end)


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

    ``source_ref`` IS a ``Locator`` (ADR 0006): ``{document_id, version, unit}``.
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

    The protocol returns the contract envelope directly; v1's only
    implementation is ``BM25Adapter`` (in ``bm25.py``). A future MCP
    adapter that produces the same dict shape satisfies this protocol
    structurally without inheriting from it.

    The envelope is the documented ``dict[str, Any]`` also spelled by
    :func:`fin.retrieve.tool.retrieve` and ``FakeRetrievePort``: one
    spelling, because its value type is genuinely mixed — a
    ``Sequence[Chunk]`` under ``chunks``, an E1 row OBJECT under
    :data:`DATABASE_ROWS_KEY`, and string codes under ``error`` on a
    named exit. A narrower value type would be a lie about the wire.
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
        structured_request: Mapping[str, Any] | None = None,
        sql_window_start: str | None = None,
        sql_window_end: str | None = None,
    ) -> dict[str, Any]:
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
        sql_window_start / sql_window_end
            The ``sql`` 档's inclusive expiry window (YYYY-MM-DD),
            forwarded to the wire as the ``SQL_WINDOW_*_FILTER_KEY``
            filter pair for legacy business queries. Both are REQUIRED
            together with ``search_mode="sql"`` and are validated
            pre-transport by :func:`resolve_sql_window` — a missing or
            malformed bound raises ``InvalidBusinessQueryError`` rather
            than traveling with an invented default. Structured requests
            do not use this legacy window. Non-sql 档 must not pass them;
            :func:`resolve_sql_window` returns ``None`` for those and
            the keys never reach the wire.
        structured_request
            Optional typed structured-query object. The host cascade
            binds it to the ``sql`` 档; the adapter carries it as the
            object-valued ``filters.structured_query`` field. It cannot
            be combined with legacy outer filters or expiry windows.

        Returns
        -------
        dict
            The documented envelope, one of three shapes. Success:
            ``{"chunks": [Chunk, ...]}`` in descending score order,
            each ``Chunk`` carrying ``text``, ``source_ref`` (Locator),
            and ``score``. A ``sql`` run may additionally carry the
            ``DATABASE_ROWS_KEY`` or independent ``STRUCTURED_RESULT_KEY``
            sibling; neither is stored inside ``chunks``.
            A named exit (spec §7) is ``{"error": <code>}`` with no
            ``chunks`` key at all.
        """
        ...


__all__ = [
    "DATABASE_ROWS_KEY",
    "DATABASE_ROWS_RESULT_COUNT_FIELD",
    "DATABASE_ROWS_ROWS_FIELD",
    "DATABASE_ROWS_SCHEMA",
    "DATABASE_ROWS_SCHEMA_FIELD",
    "DATABASE_ROWS_SCHEMA_VERSION",
    "DATABASE_ROWS_SCHEMA_VERSION_FIELD",
    "STRUCTURED_RESULT_COUNT_FIELD",
    "STRUCTURED_RESULT_KEY",
    "MALFORMED_STRUCTURED_QUERY_CODE",
    "STRUCTURED_RESULT_RECEIPT_FIELD",
    "STRUCTURED_RESULT_ROWS_FIELD",
    "STRUCTURED_RESULT_SCHEMA_FIELD",
    "STRUCTURED_RESULT_SCHEMA_VERSION_FIELD",
    "STRUCTURED_SEARCH_UNAVAILABLE_CODE",
    "structured_result_of",
    "Chunk",
    "InvalidBusinessQueryError",
    "InvalidSearchModeError",
    "RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS",
    "SQL_WINDOW_END_FILTER_KEY",
    "SQL_WINDOW_START_FILTER_KEY",
    "RetrievePort",
    "SEARCH_MODE_FILTER_KEY",
    "TierUnavailableError",
    "database_rows_of",
    "new_database_rows",
    "resolve_search_mode",
    "resolve_sql_window",
]
