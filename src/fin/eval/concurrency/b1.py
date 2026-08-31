"""T23's published-number facade.

The parser remains in ``fin.eval.ablation.matrix`` so T22 and T23 share one
§B1 reading mechanism. This module only gives the concurrency package a
focused import surface.
"""

from fin.eval.ablation.matrix import (
    B1_CONCURRENCY_CONTRACT,
    ConcurrencySpecDriftError,
    ConcurrencySpecRowMissingError,
    PublishedConcurrencyContract,
    PublishedIdempotencyConcurrency,
    PublishedL1Concurrency,
    PublishedL2Concurrency,
    PublishedLatencyThreshold,
    assert_concurrency_matches_published,
    published_concurrency_contract,
)

__all__ = [
    "B1_CONCURRENCY_CONTRACT",
    "ConcurrencySpecDriftError",
    "ConcurrencySpecRowMissingError",
    "PublishedConcurrencyContract",
    "PublishedIdempotencyConcurrency",
    "PublishedL1Concurrency",
    "PublishedL2Concurrency",
    "PublishedLatencyThreshold",
    "assert_concurrency_matches_published",
    "published_concurrency_contract",
]
