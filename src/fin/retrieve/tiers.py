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

RetrieveTier = Literal[*RETRIEVE_TIERS]

# The named failure each unimplemented 档 yields. ``lexical`` 查找 is the
# lexical fallback the same doc leaves openable, so it is NOT in this
# table: an 查找 send runs a real arm. Neither is ``graph``: it IS the
# default 知识问答 档.
#
# No retired letter has a row here either. With the word forms the whole
# closed set, a bare ``A`` / ``B`` / ``C`` / ``D`` is not a 档 spelling at
# all anymore: the port validates the CLOSED SET first, so a letter is
# ``invalid_search_mode`` before this table is consulted. Keeping a row for
# one would suggest a letter can still be routed, which is the silent
# mapping the rename forbids.
TIER_UNAVAILABLE_CODE: Final[dict[str, str]] = {
    "global": TIER_NOT_OPEN,
    "sql": REAL_DB_NOT_WIRED,
}

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
    "LASTING_RETRIEVE_TIERS",
    "REAL_DB_NOT_WIRED",
    "RETRIEVE_TIERS",
    "TIER_LABELS",
    "TIER_NOT_OPEN",
    "TIER_UNAVAILABLE_CODE",
    "RetrieveTier",
    "normalize_tier_token",
]
