"""P4 acceptance evidence contract.

Frozen data model and validation only: no storage, CLI, or report building.
Later P4 modules import these names unchanged.
"""

from __future__ import annotations

from fin.eval.acceptance.protocol import (
    ATTEMPT_STATUSES,
    CASE_STATUSES,
    CONTRACT_VERSION,
    FAILURE_CLASSES,
    LANES,
    MAX_CASES,
    MAX_COUNT,
    AcceptanceContractError,
    AcceptanceInputError,
    AttemptRecord,
    CaseManifest,
    CaseOutcome,
    CaseSpec,
    ManifestValidationError,
    Report,
    RunManifest,
    attempt_record,
    case_manifest,
    case_spec,
    count_statuses,
    derive_case_status,
    ensure_sanitized,
    report,
    run_manifest,
    validate_attempt_record,
    validate_case_manifest,
    validate_run_manifest,
)

__all__ = [
    "ATTEMPT_STATUSES",
    "CASE_STATUSES",
    "CONTRACT_VERSION",
    "FAILURE_CLASSES",
    "LANES",
    "MAX_CASES",
    "MAX_COUNT",
    "AcceptanceContractError",
    "AcceptanceInputError",
    "AttemptRecord",
    "CaseManifest",
    "CaseOutcome",
    "CaseSpec",
    "ManifestValidationError",
    "Report",
    "RunManifest",
    "attempt_record",
    "case_manifest",
    "case_spec",
    "count_statuses",
    "derive_case_status",
    "ensure_sanitized",
    "report",
    "run_manifest",
    "validate_attempt_record",
    "validate_case_manifest",
    "validate_run_manifest",
]
