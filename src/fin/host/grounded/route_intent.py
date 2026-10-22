"""Route intent: the product action for one input, distinct from ``track``.

``route intent`` is the disposition the receipt records and the user
experiences — one of exactly four wire values — while the Soft-Track
``track`` stays the graph's execution lane. The two are related but not
equivalent, so this module owns the single projection from a decided
:class:`~fin.host.grounded.track_decision.TrackDecision` onto
``(RouteIntent, RouteSource)`` instead of spreading the mapping across
the graph, the receipt and the frontend.

Three rules decide the projection:

- ``chat`` covers ``dialogue`` and ``off_topic``. Off-topic is an
  out-of-domain answer, not a refusal under system/provider policy.
- ``overview`` covers catalog discovery: the two existing rules arms
  (``corpus_inventory``, ``delegated_scope``) plus the bare "let me see
  what exists" view (see :func:`is_bare_catalog_view`).
- ``retrieve`` covers every other grounded route, including the Jev
  failure residual. A missing client, a typed ``JevChoiceError``, an
  unexpected throw and a sub-threshold hint all record
  ``signals["jev_exit"]`` and mean the judge decided nothing, so the turn
  retrieves rather than clarifying or refusing. ``clarify`` survives
  only as an actionable missing user condition (``missing_slots``), which
  keeps the underlying ``retrieve`` intent.

``policy_refuse`` is deliberately NOT derivable from a track decision.
It is established only by an observed system/model-policy refusal, and
even then it refines the intent without rewriting the recorded source —
so a receipt keeps saying how the turn was routed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fin.host.grounded.track_decision import (
    BARE_CATALOG_VIEW_RE,
    TrackDecision,
    is_bare_catalog_view,
    normalize_track,
)

RouteIntent = Literal["retrieve", "overview", "chat", "policy_refuse"]
RouteSource = Literal["rule", "jev", "fallback"]

_VALID_INTENTS: tuple[str, ...] = ("retrieve", "overview", "chat", "policy_refuse")
_VALID_SOURCES: tuple[str, ...] = ("rule", "jev", "fallback")

# Catalog-discovery rule signals. Both arms already land on the
# ``grounded`` track; only their product action differs from an ordinary
# knowledge question, so they are read as a set rather than re-listed per
# call site. ``bare_catalog_view`` is stamped by the rule cascade itself
# (``track_decision._rules_decision``) through :func:`is_bare_catalog_view`,
# which is re-exported here so callers have one place to reach it.
_CATALOG_VIEW_SIGNALS: frozenset[str] = frozenset(
    {"corpus_inventory", "delegated_scope", "bare_catalog_view"}
)


def normalize_route_intent(raw: object) -> RouteIntent | None:
    """Map a wire/checkpoint route intent onto the live enum.

    Case and surrounding whitespace are tolerated because the value
    crosses a wire boundary; anything unrecognized yields ``None`` so an
    unread value is never mistaken for a decided intent.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    if candidate in _VALID_INTENTS:
        return candidate  # type: ignore[return-value]
    return None


def normalize_route_source(raw: object) -> RouteSource | None:
    """Map a wire/checkpoint route source onto the live enum."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    if candidate in _VALID_SOURCES:
        return candidate  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class RouteDecision:
    """The receipt-facing product disposition for one turn.

    ``source`` survives a policy-refusal refinement: how the turn was
    routed stays a fact about routing, while the intent records what the
    turn turned out to be.
    """

    intent: RouteIntent
    source: RouteSource

    def refine_for_policy_refusal(self, observed: bool) -> RouteDecision | None:
        """Refine to ``policy_refuse`` only on a REAL refusal signal.

        ``observed`` must come from an explicit system/provider policy
        refusal actually produced for this turn. It must never be
        inferred from a Jev outage, an off-topic classification, a
        permission error, a citation failure, or missing evidence — each
        of those is a different disposition, and inventing a refusal from
        them would put a policy claim in the receipt that no policy made.
        """
        if not observed:
            return None
        return RouteDecision(intent="policy_refuse", source=self.source)


def refine_for_policy_refusal(
    decision: RouteDecision, observed: bool
) -> RouteDecision | None:
    """Module-level spelling of :meth:`RouteDecision.refine_for_policy_refusal`.

    It takes the prior decision explicitly: the refined route must keep
    the source the turn was actually routed by, so a function that only
    saw ``observed`` could not honour that.
    """
    return decision.refine_for_policy_refusal(observed)


def _source_for_tier(tier: str) -> RouteSource:
    if tier == "jev":
        return "jev"
    # ``rules``, ``inherit``, the missing-slots ``clarify`` and the
    # ``conditional_policy`` residual are all deterministic, so they share
    # the rule attribution rather than being reported as fallbacks.
    return "rule"


def _intent_for_track(track: str) -> RouteIntent:
    # ``dialogue`` and ``off_topic`` are both the chat leaf: off-topic is
    # an out-of-domain answer, not a refusal under system/provider policy.
    if track in ("dialogue", "off_topic"):
        return "chat"
    # ``grounded`` and ``clarify`` — plus any unrecognized track, whose
    # uncertainty must not become a refusal — retrieve.
    return "retrieve"


def decide_route_intent(decision: TrackDecision) -> RouteDecision:
    """Project one track decision onto its product route intent.

    Total over any decision the cascade can produce, and over any track
    string it can carry: an unrecognized track resolves to ``retrieve``
    rather than to a refusal or a new intent, because uncertainty alone
    must not refuse.

    The Jev-failure branch is checked before the catalog arms: it is the
    one state in which nothing was decided, and the rule arms that set a
    catalog signal run ahead of the judge in the cascade, so a decision
    carrying both is not one the cascade can produce.
    """
    signals: dict[str, object] = getattr(decision, "signals", None) or {}
    tier = str(getattr(decision, "decision_tier", "") or "")

    if "jev_exit" in signals:
        return RouteDecision(intent="retrieve", source="fallback")

    raw_track = str(getattr(decision, "track", "") or "")
    # The checkpoint wire alias for the grounded leaf, canonicalized by
    # the track module rather than re-spelled here.
    track = normalize_track(raw_track) or raw_track.strip().lower()

    if any(signal in signals for signal in _CATALOG_VIEW_SIGNALS):
        return RouteDecision(intent="overview", source="rule")

    return RouteDecision(
        intent=_intent_for_track(track),
        source=_source_for_tier(tier),
    )


__all__ = [
    "BARE_CATALOG_VIEW_RE",
    "RouteDecision",
    "RouteIntent",
    "RouteSource",
    "decide_route_intent",
    "is_bare_catalog_view",
    "normalize_route_intent",
    "normalize_route_source",
    "refine_for_policy_refusal",
]
