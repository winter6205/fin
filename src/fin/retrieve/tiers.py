"""The 档 SSOT: the closed tier set and its named-failure tokens.

The routing-time codes for an unopened 档 live here (the table is EMPTY
since A2 opened ``sql``: no 档 is refused at routing time anymore, and the
mechanism stays for the next plan slot), plus the global
综述 档's one runtime code (:data:`COMMUNITY_SUMMARY_INDEX_FAILURE`) and
the sql 档's producer-side failure vocabulary (:data:
`SQL_PRODUCER_FAILURE_CODES`) — they belong with the 档 vocabulary they
qualify even though the runtime codes are emitted off the upstream
retrieve error rather than at routing time.

A neutral leaf (stdlib imports only) so BOTH sides can depend on it
without a cycle: :func:`fin.retrieve.port.resolve_search_mode`
validates against it at the port seam, and :mod:`fin.host.named_failures`
re-exports every name here so every existing
``from fin.host.named_failures import RETRIEVE_TIERS, ...`` surface
keeps working. The cycle this breaks is real: ``fin.host.graph`` imports
``fin.retrieve.port``, so the port may not import ``fin.host`` at module
level — before this module lived here the port hid that dependency in a
function-level lazy import, a layering inversion the review named
(F3).
"""

from __future__ import annotations

from typing import Final, Literal, get_args

# The routing-time named failures an unimplemented 档 yields. Public
# wire tokens (spec ``first-closed-loop`` Open Question 1);
# ``named_failures`` re-exports them onto the codes surface.
TIER_NOT_OPEN: Final[str] = "tier_not_open"

# After A2 the routing-time table below has ZERO rows — ``sql`` runs a
# real arm, so nothing is refused at routing time anymore (spec
# search-mode-business-sql-contract §4: "fin's change is one table row").
# The token survives NARROWED: it is the exit the PRODUCER declares when
# no business ledger is provisioned on its instance — after A2 it means
# NOT PROVISIONED only, never refused-at-door (spec §7: collapsing the two
# is the empty-vs-error conflation the port forbids).
REAL_DB_NOT_WIRED: Final[str] = "real_db_not_wired"

# ----- the sql 档's producer-side failure vocabulary (spec §7) -----
#
# These are NOT routing-time refusals: the producer (iknow's business-
# query module) declares one of them as the structured ``error`` field of
# an isError retrieve result, and fin's adapter reads the FIELD — never
# a substring of the message prose (§7.1: an untokened failure is a
# dead-server reading, so the closed set here is what the adapter will
# pass through under its own name). Shared tokens with the iknow side.
SQL_NO_MATCHING_ROWS: Final[str] = "sql_no_matching_rows"

SQL_QUERY_DENIED: Final[str] = "sql_query_denied"

SQL_QUERY_TIMEOUT: Final[str] = "sql_query_timeout"

SQL_AUTHORIZATION_UNVERIFIED: Final[str] = "sql_authorization_unverified"

# The request never reached the permitted query: unknown template, or a
# window the template cannot bind (missing / malformed / end < start).
# Refused before any connection — fin mirrors the same name at its own
# pre-transport seam so the exit is identical on both sides of the wire.
INVALID_BUSINESS_QUERY: Final[str] = "invalid_business_query"

# The named failures the sql arm can declare on the wire. Membership here
# is what keeps each exit under its OWN name instead of collapsing into
# ``retrieve_unavailable``; after A2 ``REAL_DB_NOT_WIRED`` means NOT
# PROVISIONED only, never refused-at-door (§7 — collapsing the two is the
# empty-vs-error conflation the port forbids).
SQL_PRODUCER_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        REAL_DB_NOT_WIRED,
        SQL_QUERY_DENIED,
        SQL_QUERY_TIMEOUT,
        SQL_AUTHORIZATION_UNVERIFIED,
        SQL_NO_MATCHING_ROWS,
        INVALID_BUSINESS_QUERY,
    }
)

# The global 综述 档 is OPEN, so its only remaining refusal is a runtime
# index fact: the 社区 summary index the report arm reads is not-ready (the
# Leiden / community-summary job failed or is incomplete). This is NOT a
# routing-time ``tier_not_open`` — the 档 runs a real arm, the arm simply has
# nothing indexed yet — so the code lives with the 档 vocabulary but is
# emitted at runtime, off the upstream retrieve error. Shared token with the
# iknow-memory side; re-exported by ``fin.host.named_failures``.
COMMUNITY_SUMMARY_INDEX_FAILURE: Final[str] = "community_summary_index_failure"

# The 档 tokens in operator-facing order (查找 / 知识问答 / 综述 / 真数据库,
# CONTEXT 检索档). Everything the host PRODUCES — the cascade, a pin, an
# operator pick, the audit — lands on one of these.
#
# Each token names the MECHANISM that answers under it, not the operator's
# intent (that is the label below), which is why the two bare letters this
# set once carried left as spelling and not as 档: ``A`` and ``D`` said
# nothing a reader could not already get wrong, while ``lexical`` and ``sql``
# say which arm runs.
#
# This tuple IS the closed set the port forwards from — one definition, so
# the wire can never accept a token the host does not produce, or refuse
# one it does. It is deliberately closed: the wayfinder doc (闭环与分档
# §1) forbids an open-ended 业务意图桶, so the only way to ask for a 档 is
# to NAME its token.
LASTING_RETRIEVE_TIERS: Final[tuple[str, ...]] = (
    "lexical",
    "graph",
    "global",
    "sql",
)

RETRIEVE_TIERS: Final[tuple[str, ...]] = LASTING_RETRIEVE_TIERS

RetrieveTier = Literal["lexical", "graph", "global", "sql"]

# A type expression cannot unpack a variable, so the literal is restated;
# this pin makes drift raise at import. Runtime-raise form because
# ``python -O`` strips bare asserts.
if tuple(get_args(RetrieveTier)) != RETRIEVE_TIERS:
    raise RuntimeError("RetrieveTier literal drifted from RETRIEVE_TIERS")

# The named failure each unimplemented 档 yields — EMPTY since A2, and
# kept as the mechanism: every refusal seam (the port's pre-transport
# check, the send door, the cascade's typed fail, the window's opened
# flag, the inherit filter) reads THIS table, so opening a 档 is one row
# here and nowhere else (spec §4). ``lexical`` 查找 is the fallback the
# wayfinder doc leaves openable, ``graph`` IS the default 知识问答 档,
# ``global`` runs a real report arm — and since A2 so does ``sql``: a
# ledger request runs against the business database, and its refusals are
# the producer's runtime facts (:data:`SQL_PRODUCER_FAILURE_CODES`),
# never this routing table.
#
# No retired letter has a row here either. With the word forms the whole
# closed set, a bare ``A`` / ``B`` / ``C`` / ``D`` is not a 档 spelling at
# all anymore: the port validates the CLOSED SET first, so a letter is
# ``invalid_search_mode`` before this table is consulted. Keeping a row for
# one would suggest a letter can still be routed, which is the silent
# mapping the rename forbids.
TIER_UNAVAILABLE_CODE: Final[dict[str, str]] = {}

# Operator-visible names keyed by the lasting token. The choice window
# renders these and an utterance is READ against them, so the four names
# have one home next to the tokens they describe.
TIER_LABELS: Final[dict[str, str]] = {
    "lexical": "查找",
    "graph": "知识问答",
    "global": "综述",
    "sql": "真数据库",
}

# Case-folded lookup: a 档 selector is a small closed token, not prose,
# so ``Graph`` and `` graph `` are the same request; anything unlisted —
# including a retired letter — resolves to nothing instead of to the
# nearest 档.
_TIER_BY_SPELLING: Final[dict[str, str]] = {
    token.lower(): token for token in LASTING_RETRIEVE_TIERS
}


def normalize_tier_token(raw: object) -> str | None:
    """Resolve a 档 spelling onto its lasting token, or ``None``.

    Total on purpose (``None`` for anything outside the closed set): the
    cascade must OBSERVE an out-of-set token as a typed EXIT and keep
    falling through, which :func:`fin.retrieve.port.resolve_search_mode`
    — raising at the wire seam — cannot serve.

    It runs strictly BEFORE anything reaches the port: the port's job is
    exact-match enforcement over the closed set, so normalizing there
    would turn a bad send into a guess and hide the wiring mistake the
    port exists to name.
    """
    if not isinstance(raw, str):
        return None
    return _TIER_BY_SPELLING.get(raw.strip().lower())


__all__ = [
    "COMMUNITY_SUMMARY_INDEX_FAILURE",
    "INVALID_BUSINESS_QUERY",
    "LASTING_RETRIEVE_TIERS",
    "REAL_DB_NOT_WIRED",
    "RETRIEVE_TIERS",
    "SQL_AUTHORIZATION_UNVERIFIED",
    "SQL_NO_MATCHING_ROWS",
    "SQL_PRODUCER_FAILURE_CODES",
    "SQL_QUERY_DENIED",
    "SQL_QUERY_TIMEOUT",
    "TIER_LABELS",
    "TIER_NOT_OPEN",
    "TIER_UNAVAILABLE_CODE",
    "RetrieveTier",
    "normalize_tier_token",
]
