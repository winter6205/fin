"""Default-collected contract tests for the P4 acceptance evidence protocol.

Hermetic: no filesystem, environment, database, model, or browser access.
Every rejection in the P4 shared input contract has a case here, plus the
append-only attempt/denominator semantics and JSON round trips that the
store, report, and preflight workers build on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fin.eval.acceptance import (
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
from fin.eval.acceptance.protocol import CONTRACT_VERSION as PROTOCOL_VERSION


def _case(**overrides: Any) -> CaseSpec:
    fields: dict[str, Any] = {
        "case_id": "case-001",
        "case_version": "1.0.0",
        "lane": "mechanism",
        "primary_category": "same_thread_order",
        "fixture_version": "fixtures-single-doc@1.0.0",
        "provenance": "authorized synthetic fixture",
        "rebuild_instructions": "uv run python scripts/rebuild_fixture.py case-001",
        "required": True,
        "rubric_id": None,
        "rubric_version": None,
    }
    fields.update(overrides)
    return CaseSpec(**fields)


def _manifest(**overrides: Any) -> CaseManifest:
    fields: dict[str, Any] = {
        "manifest_version": "1.0.0",
        "lane": "mechanism",
        "dataset_manifest_version": "1.0.0",
        "cases": (_case(),),
        "created_by": "acceptance-test",
    }
    fields.update(overrides)
    return CaseManifest(**fields)


def _run(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "lane": "mechanism",
        "manifest_version": "1.0.0",
        "dataset_manifest_version": "1.0.0",
        "code_version": "deadbeef",
        "config_id": "config-1",
        "model_id": "model-1",
        "parser_id": "parser-1",
        "measured_run": True,
    }
    fields.update(overrides)
    return RunManifest(**fields)


def _attempt(**overrides: Any) -> AttemptRecord:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "case_id": "case-001",
        "case_version": "1.0.0",
        "manifest_version": "1.0.0",
        "attempt": 1,
        "status": "passed",
        "reason_code": "ok",
        "failure_class": "none",
        "evidence_refs": ("evidence/run-1/case-001/attempt-1.log",),
        "execution_ids": {"process_id": "4242"},
        "started_at": "2026-10-21T10:00:00Z",
        "finished_at": "2026-10-21T10:00:05Z",
        "details": {},
    }
    fields.update(overrides)
    return AttemptRecord(**fields)


def _outcome(**overrides: Any) -> CaseOutcome:
    fields: dict[str, Any] = {
        "case_id": "case-001",
        "case_version": "1.0.0",
        "status": "passed",
        "attempts": 1,
        "latest_reason_code": "ok",
        "failure_class": "none",
        "rubric_version": "",
        "evidence_refs": (),
    }
    fields.update(overrides)
    return CaseOutcome(**fields)


def _counts(**overrides: int) -> dict[str, int]:
    counts = {status: 0 for status in CASE_STATUSES}
    counts["passed"] = 1
    counts["total"] = 1
    counts.update(overrides)
    return counts


def _report(**overrides: Any) -> Report:
    fields: dict[str, Any] = {
        "report_id": "report-mechanism-1",
        "contract_version": CONTRACT_VERSION,
        "lane": "mechanism",
        "run_id": "run-1",
        "manifest_version": "1.0.0",
        "case_outcomes": (_outcome(),),
        "counts": _counts(),
        "denominator_total": 1,
        "accepted": True,
        "gate": "campaign",
    }
    fields.update(overrides)
    return Report(**fields)


def _joined(problems: list[str]) -> str:
    return " | ".join(problems)


# ---------------------------------------------------------------------------
# Constants and error hierarchy
# ---------------------------------------------------------------------------


def test_frozen_constants() -> None:
    assert CONTRACT_VERSION == PROTOCOL_VERSION == "1.0.0"
    assert LANES == ("mechanism", "browser", "business")
    assert ATTEMPT_STATUSES == (
        "passed",
        "failed",
        "not_run",
        "blocked",
        "interrupted",
    )
    assert CASE_STATUSES == ATTEMPT_STATUSES + ("flaky",)
    assert "none" in FAILURE_CLASSES
    assert "unavailable_capability" in FAILURE_CLASSES
    assert MAX_COUNT == 100_000
    assert MAX_CASES == 10_000


def test_error_hierarchy() -> None:
    assert issubclass(AcceptanceContractError, RuntimeError)
    assert issubclass(AcceptanceInputError, AcceptanceContractError)
    assert issubclass(ManifestValidationError, AcceptanceInputError)
    error = ManifestValidationError(["case[0].case_id is empty"])
    assert error.problems == ("case[0].case_id is empty",)


# ---------------------------------------------------------------------------
# Constructor helpers accept valid input
# ---------------------------------------------------------------------------


def test_case_spec_helper_returns_frozen_spec() -> None:
    spec = case_spec(
        case_id="case-001",
        case_version="1.0.0",
        lane="mechanism",
        primary_category="same_thread_order",
        fixture_version="fixtures-single-doc@1.0.0",
        provenance="authorized synthetic fixture",
        rebuild_instructions="rebuild fixture",
    )
    assert spec.required is True
    assert spec.rubric_id is None
    with pytest.raises(Exception):
        spec.case_id = "mutated"  # type: ignore[misc]


def test_case_manifest_helper_accepts_valid_manifest() -> None:
    manifest = case_manifest(
        manifest_version="1.0.0",
        lane="mechanism",
        dataset_manifest_version="1.0.0",
        cases=(_case(),),
        created_by="acceptance-test",
    )
    assert validate_case_manifest(manifest) == []


def test_run_manifest_helper_accepts_valid_run() -> None:
    run = run_manifest(
        run_id="run-1",
        lane="mechanism",
        manifest_version="1.0.0",
        dataset_manifest_version="1.0.0",
        code_version="deadbeef",
        config_id="config-1",
        model_id="model-1",
        parser_id="parser-1",
    )
    assert run.measured_run is False
    assert validate_run_manifest(run) == []


def test_attempt_record_helper_accepts_valid_record() -> None:
    record = attempt_record(
        run_id="run-1",
        case_id="case-001",
        case_version="1.0.0",
        manifest_version="1.0.0",
        attempt=1,
        status="passed",
        reason_code="ok",
        failure_class="none",
        evidence_refs=("evidence/run-1/case-001/attempt-1.log",),
        execution_ids={"process_id": "4242"},
        started_at="2026-10-21T10:00:00Z",
        finished_at="2026-10-21T10:00:05Z",
    )
    assert record.details == {}
    assert validate_attempt_record(record, _manifest()) == []


def test_report_helper_accepts_valid_report() -> None:
    built = report(
        report_id="report-mechanism-1",
        lane="mechanism",
        run_id="run-1",
        manifest_version="1.0.0",
        case_outcomes=(_outcome(),),
        gate="campaign",
    )
    assert built.contract_version == CONTRACT_VERSION
    assert built.counts["passed"] == 1
    assert built.denominator_total == 1
    assert built.accepted is False  # a gate result is never implied
    assert built.gate == "campaign"


def test_helpers_raise_manifest_validation_error_with_problems() -> None:
    with pytest.raises(ManifestValidationError) as caught:
        case_manifest(
            manifest_version="1.0.0",
            lane="mechanism",
            dataset_manifest_version="1.0.0",
            cases=(),
            created_by="acceptance-test",
        )
    assert caught.value.problems
    assert any("empty" in problem for problem in caught.value.problems)


# ---------------------------------------------------------------------------
# validate_case_manifest
# ---------------------------------------------------------------------------


def test_case_manifest_clean_manifest_has_no_problems() -> None:
    assert validate_case_manifest(_manifest()) == []


def test_case_manifest_rejects_empty_case_list() -> None:
    problems = validate_case_manifest(_manifest(cases=()))
    assert problems
    assert any("empty" in problem for problem in problems)


def test_case_manifest_rejects_more_than_max_cases() -> None:
    cases = tuple(
        _case(case_id=f"case-{index:05d}") for index in range(MAX_CASES + 1)
    )
    problems = validate_case_manifest(_manifest(cases=cases))
    assert problems
    assert any(str(MAX_CASES) in problem for problem in problems)


def test_case_manifest_rejects_duplicate_case_id_even_across_versions() -> None:
    cases = (
        _case(case_id="dup", case_version="1.0.0"),
        _case(case_id="dup", case_version="2.0.0"),
    )
    problems = validate_case_manifest(_manifest(cases=cases))
    assert problems
    assert any("duplicate" in problem.lower() for problem in problems)


def test_case_manifest_rejects_unknown_lane() -> None:
    problems = validate_case_manifest(_manifest(lane="production"))
    assert problems
    assert any("lane" in problem for problem in problems)


def test_case_manifest_rejects_case_lane_mismatch() -> None:
    problems = validate_case_manifest(
        _manifest(lane="mechanism", cases=(_case(lane="browser"),))
    )
    assert problems
    assert any("lane" in problem for problem in problems)


def test_case_manifest_rejects_empty_identity_and_provenance_fields() -> None:
    for field in (
        "case_id",
        "case_version",
        "primary_category",
        "fixture_version",
        "provenance",
    ):
        problems = validate_case_manifest(
            _manifest(cases=(_case(**{field: ""}),))
        )
        assert problems, field
        assert any(field in problem for problem in problems), (field, problems)


def test_case_manifest_requires_non_empty_fixture_version_but_not_dotted_numeric() -> None:
    # fixture_version is an identity, not a semantic version string.
    assert validate_case_manifest(_manifest()) == []
    problems = validate_case_manifest(_manifest(cases=(_case(fixture_version=""),)))
    assert any("fixture_version" in problem for problem in problems)


def test_case_manifest_requires_rebuild_instructions_for_required_case() -> None:
    problems = validate_case_manifest(
        _manifest(cases=(_case(required=True, rebuild_instructions=""),))
    )
    assert problems
    assert any("rebuild" in problem.lower() for problem in problems)


def test_case_manifest_optional_case_may_omit_rebuild_only_when_non_reproducible() -> None:
    non_reproducible = _manifest(
        cases=(
            _case(
                required=False,
                rebuild_instructions="",
                provenance="unlicensed capture; non-reproducible",
            ),
        )
    )
    assert validate_case_manifest(non_reproducible) == []

    reproducible = _manifest(
        cases=(
            _case(
                required=False,
                rebuild_instructions="",
                provenance="authorized synthetic fixture",
            ),
        )
    )
    assert validate_case_manifest(reproducible)


def test_case_manifest_rejects_rubric_id_without_version_and_back() -> None:
    id_only = validate_case_manifest(
        _manifest(cases=(_case(rubric_id="rubric-1", rubric_version=None),))
    )
    assert id_only
    assert any("rubric" in problem.lower() for problem in id_only)

    version_only = validate_case_manifest(
        _manifest(cases=(_case(rubric_id=None, rubric_version="1.0.0"),))
    )
    assert version_only
    assert any("rubric" in problem.lower() for problem in version_only)


def test_case_manifest_accepts_paired_rubric_and_release_candidate_versions() -> None:
    manifest = _manifest(
        manifest_version="2.0.0-rc.1",
        dataset_manifest_version="2026.10.21",
        cases=(
            _case(
                case_version="1.2.3-rc.1",
                rubric_id="rubric-table",
                rubric_version="1.0.0-rc.2",
            ),
        ),
    )
    assert validate_case_manifest(manifest) == []


@pytest.mark.parametrize("bad_version", ["", "v1.0.0", "1", "1.0.0 ", "1.0.0-", "one.two"])
def test_case_manifest_rejects_malformed_versions(bad_version: str) -> None:
    problems = validate_case_manifest(
        _manifest(cases=(_case(case_version=bad_version),))
    )
    assert problems, bad_version
    assert any("version" in problem.lower() for problem in problems), problems


def test_case_manifest_rejects_malformed_manifest_version() -> None:
    problems = validate_case_manifest(_manifest(manifest_version="latest"))
    assert problems
    assert any("manifest_version" in problem for problem in problems)


def test_case_manifest_rejects_empty_created_by() -> None:
    problems = validate_case_manifest(_manifest(created_by=""))
    assert problems
    assert any("created_by" in problem for problem in problems)


def test_case_manifest_rejects_non_tuple_cases() -> None:
    problems = validate_case_manifest(_manifest(cases=[_case()]))  # type: ignore[arg-type]
    assert problems
    assert any("cases" in problem for problem in problems)


# ---------------------------------------------------------------------------
# validate_run_manifest
# ---------------------------------------------------------------------------


def test_run_manifest_clean_run_has_no_problems() -> None:
    assert validate_run_manifest(_run()) == []


def test_run_manifest_rejects_unknown_lane() -> None:
    problems = validate_run_manifest(_run(lane="staging"))
    assert problems
    assert any("lane" in problem for problem in problems)


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "manifest_version",
        "dataset_manifest_version",
        "code_version",
        "config_id",
        "model_id",
        "parser_id",
    ],
)
def test_run_manifest_rejects_empty_required_strings(field: str) -> None:
    problems = validate_run_manifest(_run(**{field: ""}))
    assert problems, field
    assert any(field in problem for problem in problems), (field, problems)


@pytest.mark.parametrize("field", ["manifest_version", "dataset_manifest_version"])
def test_run_manifest_rejects_malformed_versions(field: str) -> None:
    problems = validate_run_manifest(_run(**{field: "main"}))
    assert problems, field
    assert any(field in problem for problem in problems)


def test_run_manifest_rejects_non_bool_measured_run() -> None:
    problems = validate_run_manifest(_run(measured_run=1))  # type: ignore[arg-type]
    assert problems
    assert any("measured_run" in problem for problem in problems)


# ---------------------------------------------------------------------------
# validate_attempt_record
# ---------------------------------------------------------------------------


def test_attempt_record_clean_record_has_no_problems() -> None:
    assert validate_attempt_record(_attempt(), _manifest()) == []


def test_attempt_record_rejects_unknown_status() -> None:
    problems = validate_attempt_record(
        _attempt(status="skipped", failure_class="none"), _manifest()
    )
    assert problems
    assert any("status" in problem for problem in problems)


def test_attempt_record_rejects_unknown_failure_class() -> None:
    problems = validate_attempt_record(
        _attempt(status="failed", failure_class="infra"), _manifest()
    )
    assert problems
    assert any("failure_class" in problem for problem in problems)


@pytest.mark.parametrize("attempt", [0, -1, MAX_COUNT + 1, True, "1", 1.5])
def test_attempt_record_rejects_invalid_attempt_numbers(attempt: Any) -> None:
    problems = validate_attempt_record(_attempt(attempt=attempt), _manifest())
    assert problems, attempt
    assert any("attempt" in problem for problem in problems)


def test_attempt_record_accepts_upper_bound_attempt() -> None:
    assert validate_attempt_record(_attempt(attempt=MAX_COUNT), _manifest()) == []


def test_attempt_record_rejects_passed_with_failure_class() -> None:
    problems = validate_attempt_record(
        _attempt(status="passed", failure_class="assertion"), _manifest()
    )
    assert problems
    assert any("passed" in problem for problem in problems)


def test_attempt_record_rejects_failed_without_failure_class() -> None:
    problems = validate_attempt_record(
        _attempt(status="failed", failure_class="none"), _manifest()
    )
    assert problems
    assert any("failed" in problem for problem in problems)


def test_attempt_record_rejects_empty_reason_code() -> None:
    problems = validate_attempt_record(_attempt(reason_code=""), _manifest())
    assert problems
    assert any("reason_code" in problem for problem in problems)


def test_attempt_record_rejects_list_evidence_refs_and_empty_refs() -> None:
    as_list = validate_attempt_record(
        _attempt(evidence_refs=["evidence/a.log"]),  # type: ignore[arg-type]
        _manifest(),
    )
    assert as_list
    assert any("evidence_refs" in problem for problem in as_list)

    empty_ref = validate_attempt_record(
        _attempt(evidence_refs=("",)), _manifest()
    )
    assert empty_ref
    assert any("evidence_refs" in problem for problem in empty_ref)


def test_attempt_record_rejects_non_mapping_execution_ids() -> None:
    problems = validate_attempt_record(
        _attempt(execution_ids=["4242"]),  # type: ignore[arg-type]
        _manifest(),
    )
    assert problems
    assert any("execution_ids" in problem for problem in problems)


def test_attempt_record_rejects_non_json_details() -> None:
    problems = validate_attempt_record(
        _attempt(details={"bad": object()}), _manifest()
    )
    assert problems
    assert any("details" in problem for problem in problems)


def test_attempt_record_rejects_newline_in_reason_code() -> None:
    problems = validate_attempt_record(
        _attempt(reason_code="first line\nsecond line"), _manifest()
    )
    assert problems
    assert any("reason_code" in problem for problem in problems)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-live-ABC123",
        "Authorization: Bearer abc123",
        "postgres://user:pw@host/db",
        "postgresql://user:pw@host/db",
        "redis://cache:6379/0",
        "PASSWORD=hunter2",
    ],
)
def test_attempt_record_rejects_credential_looking_text_without_echoing_it(
    secret: str,
) -> None:
    problems = validate_attempt_record(
        _attempt(reason_code=f"failed: {secret}"), _manifest()
    )
    assert problems
    assert any("reason_code" in problem for problem in problems)
    assert secret not in _joined(problems)

    ref_problems = validate_attempt_record(
        _attempt(evidence_refs=(f"artifacts/{secret}.log",)), _manifest()
    )
    assert ref_problems
    assert any("evidence_refs" in problem for problem in ref_problems)
    assert secret not in _joined(ref_problems)


def test_attempt_record_rejects_unknown_case() -> None:
    problems = validate_attempt_record(
        _attempt(case_id="case-999"), _manifest()
    )
    assert problems
    assert any("case_id" in problem for problem in problems)


def test_attempt_record_rejects_case_version_mismatch() -> None:
    problems = validate_attempt_record(
        _attempt(case_version="2.0.0"), _manifest()
    )
    assert problems
    assert any("case_version" in problem for problem in problems)


def test_attempt_record_rejects_manifest_version_mismatch() -> None:
    problems = validate_attempt_record(
        _attempt(manifest_version="9.9.9"), _manifest()
    )
    assert problems
    assert any("manifest_version" in problem for problem in problems)


def test_attempt_record_rejects_empty_run_id() -> None:
    problems = validate_attempt_record(_attempt(run_id=""), _manifest())
    assert problems
    assert any("run_id" in problem for problem in problems)


def test_attempt_record_rejects_negative_user_details_count() -> None:
    problems = validate_attempt_record(
        _attempt(details={"tool_attempts": -1}), _manifest()
    )
    assert problems
    assert any("count" in problem.lower() for problem in problems)


def test_attempt_record_rejects_overflowing_user_details_count() -> None:
    problems = validate_attempt_record(
        _attempt(details={"tool_attempts": MAX_COUNT + 1}), _manifest()
    )
    assert problems
    assert any("count" in problem.lower() for problem in problems)


def test_attempt_record_rejects_non_record_input() -> None:
    problems = validate_attempt_record(
        {"status": "passed"}, _manifest()  # type: ignore[arg-type]
    )
    assert problems
    assert any("AttemptRecord" in problem for problem in problems)


def test_attempt_record_accepts_identity_that_merely_contains_sk_dash() -> None:
    """Only a credential-shaped ``sk-`` token is rejected, not any substring."""
    assert (
        validate_attempt_record(
            _attempt(execution_ids={"worker_id": "task-run-7"}), _manifest()
        )
        == []
    )


# ---------------------------------------------------------------------------
# derive_case_status
# ---------------------------------------------------------------------------


def _attempts(*statuses: str) -> tuple[AttemptRecord, ...]:
    return tuple(
        _attempt(
            attempt=index + 1,
            status=status,
            reason_code="ok" if status == "passed" else status,
            failure_class="none" if status == "passed" else "assertion",
            finished_at=f"2026-10-21T10:00:0{index}Z",
        )
        for index, status in enumerate(statuses)
    )


def test_derive_case_status_no_attempts_is_not_run() -> None:
    assert derive_case_status((), declared_measured_run=True) == "not_run"


def test_derive_case_status_latest_attempt_wins() -> None:
    assert derive_case_status(_attempts("failed"), declared_measured_run=True) == "failed"
    assert derive_case_status(_attempts("blocked"), declared_measured_run=True) == "blocked"
    assert (
        derive_case_status(
            _attempts("interrupted"), declared_measured_run=True
        )
        == "interrupted"
    )


def test_derive_case_status_failure_then_pass_is_flaky() -> None:
    status = derive_case_status(
        _attempts("failed", "passed"), declared_measured_run=True
    )
    assert status == "flaky"
    assert status != "passed"


def test_derive_case_status_orders_by_attempt_not_input_order() -> None:
    out_of_order = (_attempts("failed", "passed")[1], _attempts("failed", "passed")[0])
    assert (
        derive_case_status(out_of_order, declared_measured_run=True) == "flaky"
    )


def test_derive_case_status_pass_then_fail_is_failed_not_flaky() -> None:
    assert (
        derive_case_status(_attempts("passed", "failed"), declared_measured_run=True)
        == "failed"
    )


def test_derive_case_status_pass_after_blocked_is_clean() -> None:
    assert (
        derive_case_status(_attempts("blocked", "passed"), declared_measured_run=True)
        == "passed"
    )


def test_derive_case_status_unmeasured_run_cannot_score_a_pass() -> None:
    assert (
        derive_case_status(_attempts("passed"), declared_measured_run=False)
        == "not_run"
    )
    # A retry pass stays flaky: it must never read as a clean pass.
    assert (
        derive_case_status(_attempts("failed", "passed"), declared_measured_run=False)
        == "flaky"
    )
    # A latest failure remains countable even in an undeclared run.
    assert (
        derive_case_status(_attempts("failed"), declared_measured_run=False)
        == "failed"
    )


def test_derive_case_status_rejects_unknown_status() -> None:
    with pytest.raises(AcceptanceInputError):
        derive_case_status(_attempts("skipped"), declared_measured_run=True)


def test_derive_case_status_returns_case_status_member() -> None:
    assert (
        derive_case_status(_attempts("failed", "passed"), declared_measured_run=True)
        in CASE_STATUSES
    )


# ---------------------------------------------------------------------------
# count_statuses
# ---------------------------------------------------------------------------


def test_count_statuses_emits_every_key_at_zero() -> None:
    counts = count_statuses(())
    assert set(counts) == set(CASE_STATUSES) | {"total"}
    assert all(counts[status] == 0 for status in CASE_STATUSES)
    assert counts["total"] == 0


def test_count_statuses_tallies_outcomes_and_total() -> None:
    outcomes = (
        _outcome(case_id="a", status="passed"),
        _outcome(case_id="b", status="failed", failure_class="assertion"),
        _outcome(case_id="c", status="flaky"),
        _outcome(case_id="d", status="not_run", attempts=0),
        _outcome(case_id="e", status="blocked"),
        _outcome(case_id="f", status="interrupted"),
    )
    counts = count_statuses(outcomes)
    assert counts["passed"] == 1
    assert counts["failed"] == 1
    assert counts["flaky"] == 1
    assert counts["not_run"] == 1
    assert counts["blocked"] == 1
    assert counts["interrupted"] == 1
    assert counts["total"] == 6


def test_count_statuses_rejects_unknown_status() -> None:
    with pytest.raises(AcceptanceInputError):
        count_statuses((_outcome(status="skipped"),))


# ---------------------------------------------------------------------------
# ensure_sanitized
# ---------------------------------------------------------------------------


def test_ensure_sanitized_returns_clean_text() -> None:
    assert ensure_sanitized("case-001 failed on assertion 3") == (
        "case-001 failed on assertion 3"
    )


@pytest.mark.parametrize(
    "text",
    [
        "line one\nline two",
        "carriage\rreturn",
        "token sk-abc",
        "authorization: bearer xyz",
        "dsn postgres://u:p@h/d",
        "dsn postgresql://u:p@h/d",
        "cache redis://h:6379",
        "password=hunter2",
    ],
)
def test_ensure_sanitized_rejects_newlines_and_credentials(text: str) -> None:
    with pytest.raises(AcceptanceInputError) as caught:
        ensure_sanitized(text)
    assert text not in str(caught.value)


# ---------------------------------------------------------------------------
# report validation
# ---------------------------------------------------------------------------


def test_report_clean_report_has_no_problems() -> None:
    assert report(
        report_id="report-mechanism-1",
        lane="mechanism",
        run_id="run-1",
        manifest_version="1.0.0",
        case_outcomes=(_outcome(),),
        accepted=True,
        gate="campaign",
    ).accepted is True


def test_report_rejects_missing_count_keys() -> None:
    counts = _counts()
    del counts["blocked"]
    with pytest.raises(ManifestValidationError) as caught:
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            counts=counts,
            gate="campaign",
        )
    assert any("counts" in problem for problem in caught.value.problems)


def test_report_rejects_negative_and_overflowing_counts() -> None:
    for bad_value in (-1, MAX_COUNT + 1):
        counts = _counts(**{"failed": bad_value})
        counts["total"] = 1 + bad_value
        with pytest.raises(ManifestValidationError):
            report(
                report_id="report-1",
                lane="mechanism",
                run_id="run-1",
                manifest_version="1.0.0",
                case_outcomes=(_outcome(),),
                counts=counts,
                denominator_total=1 + bad_value,
                gate="campaign",
            )


def test_report_rejects_bool_counts() -> None:
    with pytest.raises(ManifestValidationError):
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            counts=_counts(**{"passed": True}),  # type: ignore[dict-item]
            gate="campaign",
        )


def test_report_rejects_counts_that_do_not_reconcile_with_outcomes() -> None:
    with pytest.raises(ManifestValidationError) as caught:
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            counts=_counts(**{"passed": 2, "total": 2}),
            denominator_total=2,
            gate="campaign",
        )
    assert any("reconcile" in problem for problem in caught.value.problems)


def test_report_allows_a_denominator_above_the_recorded_outcomes() -> None:
    """An interim report may still owe cases; it just cannot claim acceptance."""
    built = report(
        report_id="report-1",
        lane="mechanism",
        run_id="run-1",
        manifest_version="1.0.0",
        case_outcomes=(_outcome(),),
        counts=_counts(),
        denominator_total=2,
        gate="campaign",
    )
    assert built.denominator_total == 2
    assert built.accepted is False


def test_report_rejects_denominator_below_recorded_outcomes() -> None:
    with pytest.raises(ManifestValidationError) as caught:
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(), _outcome(case_id="case-002")),
            denominator_total=1,
            gate="campaign",
        )
    assert any("denominator" in problem for problem in caught.value.problems)


def test_report_rejects_acceptance_while_cases_are_unrecorded() -> None:
    with pytest.raises(ManifestValidationError) as caught:
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            denominator_total=2,
            accepted=True,
            gate="campaign",
        )
    assert any("accepted" in problem for problem in caught.value.problems)


def test_report_rejects_accepted_when_a_required_case_did_not_pass() -> None:
    failing = _outcome(status="failed", failure_class="assertion")
    with pytest.raises(ManifestValidationError) as caught:
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(failing,),
            counts=_counts(**{"passed": 0, "failed": 1}),
            accepted=True,
            gate="campaign",
        )
    assert any("accepted" in problem for problem in caught.value.problems)


def test_report_rejects_unknown_lane_and_contract_version() -> None:
    with pytest.raises(ManifestValidationError):
        report(
            report_id="report-1",
            lane="staging",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            gate="campaign",
        )
    with pytest.raises(ManifestValidationError):
        report(
            report_id="report-1",
            contract_version="2.0.0",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            gate="campaign",
        )


def test_report_rejects_unknown_outcome_status() -> None:
    with pytest.raises(ManifestValidationError):
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(status="skipped"),),
            gate="campaign",
        )


def test_report_rejects_empty_gate_and_report_id() -> None:
    for overrides in ({"gate": ""}, {"report_id": ""}):
        with pytest.raises(ManifestValidationError):
            report(
                report_id=overrides.get("report_id", "report-1"),
                lane="mechanism",
                run_id="run-1",
                manifest_version="1.0.0",
                case_outcomes=(_outcome(),),
                gate=overrides.get("gate", "campaign"),
            )


def test_report_rejects_credential_in_gate() -> None:
    with pytest.raises(ManifestValidationError) as caught:
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(),),
            gate="postgres://host/db",
        )
    assert "postgres://host/db" not in _joined(caught.value.problems)


# ---------------------------------------------------------------------------
# JSON round trips
# ---------------------------------------------------------------------------


def test_case_manifest_round_trip_is_json_safe() -> None:
    manifest = _manifest(
        cases=(
            _case(),
            _case(
                case_id="case-002",
                required=False,
                provenance="non-reproducible capture",
                rebuild_instructions="",
                rubric_id="rubric-1",
                rubric_version="1.0.0",
            ),
        ),
        created_by="acceptance-test",
    )
    payload = manifest.to_dict()
    text = json.dumps(payload)
    restored = CaseManifest.from_dict(json.loads(text))
    assert restored == manifest
    assert validate_case_manifest(restored) == []


def test_run_manifest_round_trip_is_json_safe() -> None:
    run = _run(measured_run=True)
    restored = RunManifest.from_dict(json.loads(json.dumps(run.to_dict())))
    assert restored == run


def test_attempt_record_round_trip_is_json_safe() -> None:
    record = _attempt(
        status="failed",
        reason_code="assertion mismatch",
        failure_class="assertion",
        execution_ids={"process_id": "4242", "worker_id": "w-1"},
        details={"tool_attempts": 2, "duration_ms": 12.5, "retry_of": None},
    )
    restored = AttemptRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert restored == record
    assert isinstance(restored.evidence_refs, tuple)
    assert validate_attempt_record(restored, _manifest()) == []


def test_report_round_trip_is_json_safe() -> None:
    built = report(
        report_id="report-mechanism-1",
        lane="mechanism",
        run_id="run-1",
        manifest_version="1.0.0",
        case_outcomes=(_outcome(),),
        accepted=True,
        gate="campaign",
    )
    restored = Report.from_dict(json.loads(json.dumps(built.to_dict())))
    assert restored == built


def test_case_outcome_round_trip_preserves_rubric_and_evidence() -> None:
    outcome = _outcome(
        rubric_version="1.0.0",
        evidence_refs=("evidence/a.log", "evidence/b.log"),
    )
    built = report(
        report_id="report-mechanism-1",
        lane="mechanism",
        run_id="run-1",
        manifest_version="1.0.0",
        case_outcomes=(outcome,),
        accepted=True,
        gate="campaign",
    )
    restored = Report.from_dict(json.loads(json.dumps(built.to_dict())))
    assert restored == built
    restored_outcome = restored.case_outcomes[0]
    assert restored_outcome.rubric_version == "1.0.0"
    assert restored_outcome.evidence_refs == ("evidence/a.log", "evidence/b.log")
    assert isinstance(restored_outcome.evidence_refs, tuple)


def test_case_outcome_round_trip_preserves_empty_rubric_version() -> None:
    built = report(
        report_id="report-mechanism-1",
        lane="mechanism",
        run_id="run-1",
        manifest_version="1.0.0",
        case_outcomes=(_outcome(rubric_version="", evidence_refs=()),),
        accepted=True,
        gate="campaign",
    )
    restored = Report.from_dict(json.loads(json.dumps(built.to_dict())))
    assert restored.case_outcomes[0].rubric_version == ""
    assert restored.case_outcomes[0].evidence_refs == ()


def test_case_outcome_payload_requires_rubric_and_evidence_fields() -> None:
    payload = _report().to_dict()
    del payload["case_outcomes"][0]["rubric_version"]  # type: ignore[index]
    with pytest.raises(AcceptanceInputError):
        Report.from_dict(payload)

    payload = _report().to_dict()
    del payload["case_outcomes"][0]["evidence_refs"]  # type: ignore[index]
    with pytest.raises(AcceptanceInputError):
        Report.from_dict(payload)

    payload = _report().to_dict()
    payload["case_outcomes"][0]["unexpected"] = "value"  # type: ignore[index]
    with pytest.raises(AcceptanceInputError):
        Report.from_dict(payload)


def test_case_outcome_payload_rejects_malformed_evidence_refs() -> None:
    payload = _report().to_dict()
    payload["case_outcomes"][0]["evidence_refs"] = "evidence/a.log"  # type: ignore[index]
    with pytest.raises(AcceptanceInputError):
        Report.from_dict(payload)

    payload = _report().to_dict()
    payload["case_outcomes"][0]["evidence_refs"] = [""]  # type: ignore[index]
    with pytest.raises(ManifestValidationError):
        Report.from_dict(payload)


def test_report_rejects_non_string_outcome_rubric_version() -> None:
    with pytest.raises(ManifestValidationError):
        report(
            report_id="report-1",
            lane="mechanism",
            run_id="run-1",
            manifest_version="1.0.0",
            case_outcomes=(_outcome(rubric_version=1),),
            gate="campaign",
        )


def test_from_dict_rejects_missing_and_unknown_fields() -> None:
    payload = _manifest().to_dict()
    del payload["lane"]
    with pytest.raises(AcceptanceInputError):
        CaseManifest.from_dict(payload)

    payload = _manifest().to_dict()
    payload["unexpected"] = "value"
    with pytest.raises(AcceptanceInputError):
        CaseManifest.from_dict(payload)


def test_from_dict_rejects_malformed_shapes() -> None:
    with pytest.raises(AcceptanceInputError):
        CaseManifest.from_dict("not-a-mapping")  # type: ignore[arg-type]
    with pytest.raises(AcceptanceInputError):
        CaseManifest.from_dict({"manifest_version": "1.0.0"})
    with pytest.raises(AcceptanceInputError):
        AttemptRecord.from_dict(
            {
                **_attempt().to_dict(),
                "evidence_refs": "evidence/a.log",
            }
        )
    with pytest.raises(AcceptanceInputError):
        RunManifest.from_dict({**_run().to_dict(), "measured_run": "yes"})


def test_from_dict_revalidates_semantics() -> None:
    payload = _manifest().to_dict()
    payload["cases"][0]["case_version"] = "not-a-version"  # type: ignore[index]
    with pytest.raises(ManifestValidationError):
        CaseManifest.from_dict(payload)


def test_attempt_from_dict_rejects_credential_payload() -> None:
    payload = _attempt().to_dict()
    payload["reason_code"] = "postgres://user:pw@host/db"
    with pytest.raises(ManifestValidationError) as caught:
        AttemptRecord.from_dict(payload)
    assert "postgres://user:pw@host/db" not in _joined(caught.value.problems)


def test_to_dict_outputs_only_json_primitives() -> None:
    payload = _attempt(details={"nested": [1, "two", None, {"three": True}]}).to_dict()

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str)
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        else:
            assert value is None or isinstance(value, (str, int, float, bool))

    _walk(payload)
