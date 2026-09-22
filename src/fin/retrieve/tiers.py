"""The 档 SSOT: the closed tier set and its routing-time codes.

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

from typing import Final, Literal

# The routing-time named failures an unimplemented 档 yields. Public
# wire tokens (spec ``first-closed-loop`` Open Question 1);
# ``named_failures`` re-exports them onto the codes surface.
TIER_NOT_OPEN: Final[str] = "tier_not_open"

REAL_DB_NOT_WIRED: Final[str] = "real_db_not_wired"

# The 档 selectors whose product surfaces accept (spec Open Question 1).
# The list is deliberately closed: the wayfinder doc (闭环与分档
# §1) forbids an open-ended 业务意图桶, so the only way to ask for a
# 档 is to NAME its token.
#
# This tuple is the SSOT for the closed set. :data:`RetrieveTier` is the
# SAME list spelled as a ``Literal`` so the wire schema cannot drift
# from it: adding a 档 here (and only here) updates the enum the send
# door validates against, and ``TIER_UNAVAILABLE_CODE``'s keys must stay
# a subset (a code for a 档 outside the closed list is unreachable).
#
# The lasting MCP tokens are ``A`` / ``graph`` / ``global`` / ``D``
# (:data:`LASTING_RETRIEVE_TIERS`). ``B`` and ``C`` stay members of this
# tuple for one more increment — the send door still TYPES a request with
# them, so dropping a letter here would reject a live request's own
# token — but nothing that READS a 档 resolves to one any more:
# :func:`normalize_tier_token` maps them onto their lasting token first.
RETRIEVE_TIERS: Final[tuple[str, ...]] = (
    "A",
    "B",
    "C",
    "D",
    "graph",
    "global",
)

RetrieveTier = Literal[*RETRIEVE_TIERS]

# The named failure each unimplemented 档 yields. A is 查找 — the
# lexical fallback the same doc leaves openable, so it is NOT in this
# table: an A send runs the ordinary (default 知识问答) path today.
# Neither is ``graph``: it IS that default 档, renamed from ``B``.
# ``C`` keeps its own row only until the send door stops accepting the
# letter at all: the port validates raw wire tokens exactly, so a
# leftover ``C`` must name itself instead of falling to
# ``invalid_search_mode``.
TIER_UNAVAILABLE_CODE: Final[dict[str, str]] = {
    "C": TIER_NOT_OPEN,
    "global": TIER_NOT_OPEN,
    "D": REAL_DB_NOT_WIRED,
}

# The 档 tokens in operator-facing order (查找 / 知识问答 / 综述 / 真数据库,
# CONTEXT 检索档). Everything the host PRODUCES — the cascade, a pin, an
# operator pick, the audit — lands on one of these; the retired spellings
# below are read, never written.
LASTING_RETRIEVE_TIERS: Final[tuple[str, ...]] = ("A", "graph", "global", "D")

# Operator-visible names keyed by the lasting token. The choice window
# renders these and an utterance is READ against them, so the four names
# have one home next to the tokens they describe.
TIER_LABELS: Final[dict[str, str]] = {
    "A": "查找",
    "graph": "知识问答",
    "global": "综述",
    "D": "真数据库",
}

# Retired spellings → the lasting token each one meant. One map, so a
# surface that still receives an old letter (an utterance written before
# the rename, a stored pin, a client's cached pick) resolves to the 档
# it named rather than forwarding a token the wire no longer carries.
RETIRED_TIER_TOKENS: Final[dict[str, str]] = {"B": "graph", "C": "global"}

# Case-folded lookup: a 档 selector is a small closed token, not prose,
# so ``a`` and ``GRAPH`` are the same request; anything unlisted resolves
# to nothing instead of to the nearest 档.
_TIER_BY_SPELLING: Final[dict[str, str]] = {
    **{token.lower(): token for token in LASTING_RETRIEVE_TIERS},
    **{
        retired.lower(): lasting
        for retired, lasting in RETIRED_TIER_TOKENS.items()
    },
}


def normalize_tier_token(raw: object) -> str | None:
    """Resolve any accepted spelling onto its lasting 档 token.

    Total on purpose (``None`` for anything outside the map): the
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
    "LASTING_RETRIEVE_TIERS",
    "REAL_DB_NOT_WIRED",
    "RETIRED_TIER_TOKENS",
    "RETRIEVE_TIERS",
    "RetrieveTier",
    "TIER_LABELS",
    "TIER_NOT_OPEN",
    "TIER_UNAVAILABLE_CODE",
    "normalize_tier_token",
]
