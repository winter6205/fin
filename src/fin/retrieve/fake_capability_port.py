"""Capability-aware contract fake for the eval path (T19, SC26).

``FakeRetrievePort`` answers a query from a static dict and knows nothing about
capabilities, which is right for every ticket that does not care how a hit was
produced. SC26 does care: it asserts the product path and the "all on" eval
configuration reach the same locators, and that assertion is empty unless the
port can tell the 8 capabilities apart.

So this fake scripts each hit with the capabilities that can produce it, and
exposes two seams:

* :meth:`CapabilityAwareFakePort.retrieve` — the product seam. Its signature is
  the ``RetrievePort`` signature, with no capability parameter to pass; the
  service runs everything on by default (ADR 0039).
* :meth:`CapabilityAwareFakePort.retrieve_for_eval` — the eval seam, the only
  place a capability configuration is accepted. The configuration must decide
  all 8 keys; a partial one is a named failure, not a silently completed run.

Six of the keys produce hits (a hit surfaces when any of its producing
retrievers is on); ``rrf`` and ``rerank`` are fusion stages that move order
only. That split is what lets SC26's "顺序与分数可不同，locator 集合相同" be a
real distinction rather than a restatement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from fin.retrieve.capabilities import (
    CapabilityConfigurationIncompleteError,
    UnknownCapabilityError,
    all_on_configuration,
    load_capability_keys,
    validate_capability_configuration,
)
from fin.retrieve.input_validation import (
    InputValidationLedger,
    validate_query,
)
from fin.retrieve.port import Chunk


_DEFAULT_LIMIT = 8
_LIMIT_CAP = 20

#: Capabilities that can produce a hit. The remaining two (``rrf``,
#: ``rerank``) fuse and reorder what the producers found.
PRODUCING_CAPABILITIES = frozenset(
    {
        "vector",
        "keyword_bm25",
        "cjk_segmentation",
        "phrase",
        "field_boost",
        "graph_ppr",
    }
)


def _resolve_limit(limit: int) -> int:
    if limit <= 0:
        return _DEFAULT_LIMIT
    if limit > _LIMIT_CAP:
        return _LIMIT_CAP
    return limit


@dataclass(frozen=True)
class CapabilityHit:
    """A scripted hit plus the capabilities able to produce it."""

    chunk: Chunk
    produced_by: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "produced_by", frozenset(self.produced_by))


@dataclass(frozen=True)
class RecordedCall:
    """One call, and which capability configuration (if any) it requested."""

    query: str
    requested_capabilities: dict[str, bool] | None
    effective_capabilities: dict[str, bool]


class CapabilityAwareFakePort:
    """In-process fake that satisfies ``RetrievePort`` and scripts ablation.

    Parameters
    ----------
    hits_by_query
        Scripted eval set: query string → hits, in the order the producers
        found them.
    """

    def __init__(
        self,
        hits_by_query: Mapping[str, Sequence[CapabilityHit]] | None = None,
    ) -> None:
        self._keys = load_capability_keys()
        self._hits: dict[str, list[CapabilityHit]] = {
            query: list(hits) for query, hits in (hits_by_query or {}).items()
        }
        self._validate_script()
        self.product_calls: list[RecordedCall] = []
        self.eval_calls: list[RecordedCall] = []
        self.input_validation_failures = InputValidationLedger()

    def _validate_script(self) -> None:
        known = set(self._keys)
        for query, hits in self._hits.items():
            for hit in hits:
                unknown = sorted(hit.produced_by - known)
                if unknown:
                    raise UnknownCapabilityError(
                        f"scripted hit for {query!r} names capabilities outside "
                        f"the snapshot: {unknown}"
                    )
                if not hit.produced_by & PRODUCING_CAPABILITIES:
                    raise CapabilityConfigurationIncompleteError(
                        f"scripted hit for {query!r} names no producing "
                        "capability, so no configuration could ever return it"
                    )

    # ----- product seam: no capability parameter exists to pass -----

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
    ) -> dict[str, list[Chunk]]:
        """Return ``{"chunks": [...]}`` with every capability on.

        The product path does not choose: the service's default is all-on, so
        the fake resolves the configuration itself rather than reading one off
        the call.
        """
        effective = all_on_configuration(self._keys)
        self.product_calls.append(
            RecordedCall(
                query=query,
                requested_capabilities=None,
                effective_capabilities=effective,
            )
        )
        return self._run(query, effective, limit)

    # ----- eval seam: the only place a configuration is accepted -----

    def retrieve_for_eval(
        self,
        query: str,
        *,
        capabilities: Mapping[str, Any],
        limit: int = 8,
    ) -> dict[str, list[Chunk]]:
        """Run one ablation configuration. All 8 keys must be decided."""
        effective = validate_capability_configuration(
            capabilities, keys=self._keys
        )
        self.eval_calls.append(
            RecordedCall(
                query=query,
                requested_capabilities=dict(effective),
                effective_capabilities=effective,
            )
        )
        return self._run(query, effective, limit)

    # ----- shared retrieval body -----

    def _run(
        self,
        query: str,
        capabilities: Mapping[str, bool],
        limit: int,
    ) -> dict[str, list[Chunk]]:
        # An empty query is invalid input on every port implementation
        # (T21 / §B2 第 5 条), the eval seam included — an ablation run
        # must not read a refused call as "this configuration found
        # nothing".
        validate_query(query, ledger=self.input_validation_failures)
        enabled = {key for key, on in capabilities.items() if on}
        found = [
            hit.chunk
            for hit in self._hits.get(query, [])
            if hit.produced_by & enabled
        ]
        if "rrf" in enabled:
            # Fusion: one merged ranking by score instead of the producers'
            # arrival order.
            found = sorted(found, key=lambda chunk: -chunk.score)
        if "rerank" in enabled:
            # A deterministic order-only permutation standing in for a real
            # cross-encoder: longest passage first, ties keep their position.
            found = sorted(found, key=lambda chunk: -len(chunk.text))
        return {"chunks": found[: _resolve_limit(limit)]}


__all__ = [
    "PRODUCING_CAPABILITIES",
    "CapabilityAwareFakePort",
    "CapabilityHit",
    "RecordedCall",
]
