"""T23 concurrency and supplementary evaluation drivers.

The package is evaluation-only. Product retrieval calls remain capability
agnostic; the only configuration-bearing calls are in the existing ablation
eval seam.
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
from fin.eval.concurrency.runner import (
    BlockCountUnavailableError,
    ConcurrencyRunError,
    IdempotencyReport,
    InvalidConcurrencyInputError,
    L1Report,
    L2BackendSetupError,
    L2CredentialsMissingError,
    L2LatencyContract,
    L2Report,
    ReportWriteError,
    build_contract_fake_push_records,
    l2_latency_contract,
    run_idempotency,
    run_l1,
    run_l2,
    validate_idempotency_report,
    validate_l1_report,
    validate_l2_report,
)
from fin.eval.concurrency.supplements import (
    FIN_EVAL_DIMENSIONS,
    FORBIDDEN_FIN_DIMENSIONS,
    SupplementError,
    SupplementInputError,
    build_supplement_report,
    citation_gate_dimension_report,
    duplicate_submission_dimension_report,
    first_hit_dimension_report,
    first_hit_position_distribution,
    refusal_rate_dimension_report,
    validate_supplement_report,
    validate_fin_dimensions,
)


def main(argv: list[str] | None = None) -> int:
    """Lazy CLI import, keeping normal product imports free of argparse."""
    from fin.eval.concurrency.__main__ import main as cli_main

    return cli_main(argv)


__all__ = [
    "B1_CONCURRENCY_CONTRACT",
    "BlockCountUnavailableError",
    "ConcurrencyRunError",
    "ConcurrencySpecDriftError",
    "ConcurrencySpecRowMissingError",
    "FIN_EVAL_DIMENSIONS",
    "FORBIDDEN_FIN_DIMENSIONS",
    "IdempotencyReport",
    "InvalidConcurrencyInputError",
    "L1Report",
    "L2BackendSetupError",
    "L2CredentialsMissingError",
    "L2LatencyContract",
    "L2Report",
    "PublishedConcurrencyContract",
    "PublishedIdempotencyConcurrency",
    "PublishedL1Concurrency",
    "PublishedL2Concurrency",
    "PublishedLatencyThreshold",
    "ReportWriteError",
    "SupplementError",
    "SupplementInputError",
    "assert_concurrency_matches_published",
    "build_contract_fake_push_records",
    "build_supplement_report",
    "citation_gate_dimension_report",
    "duplicate_submission_dimension_report",
    "first_hit_dimension_report",
    "first_hit_position_distribution",
    "l2_latency_contract",
    "main",
    "published_concurrency_contract",
    "refusal_rate_dimension_report",
    "run_idempotency",
    "run_l1",
    "run_l2",
    "validate_fin_dimensions",
    "validate_idempotency_report",
    "validate_l1_report",
    "validate_l2_report",
    "validate_supplement_report",
]
