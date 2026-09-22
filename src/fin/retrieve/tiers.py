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
# The lasting MCP tokens are ``A`` / ``graph`` / ``global`` / ``D``.
# ``B`` and ``C`` stay members for exactly one more increment: every
# surface that still PRODUCES a letter (the cascade's normalize, the
# pin, the choice-window ids) has to retire them first, and dropping a
# letter before that would make a live request's own token invalid.
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
TIER_UNAVAILABLE_CODE: Final[dict[str, str]] = {
    "C": TIER_NOT_OPEN,
    "global": TIER_NOT_OPEN,
    "D": REAL_DB_NOT_WIRED,
}


__all__ = [
    "REAL_DB_NOT_WIRED",
    "RETRIEVE_TIERS",
    "RetrieveTier",
    "TIER_NOT_OPEN",
    "TIER_UNAVAILABLE_CODE",
]
