"""Default-collected tests for the P4 fail-fast lane preflight.

Hermetic: temporary roots, injected probes, and environment values set and
cleared per test. The only subprocess any check may start is ``node``; the
browser tests here point it at an empty root so their verdict does not depend
on an installed ``@playwright/test``, and the success path injects the probe.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from fin.eval.acceptance.preflight import (
    LANES_BY_NAME,
    PROBE_BROWSER_SMOKE,
    PROBE_MODEL_AND_TOOLS,
    PROBE_PLAYWRIGHT,
    PROBE_REQUEST_EVENT_SCHEMAS,
    PROBE_RUNTIME_READINESS,
    PreflightCheck,
    PreflightResult,
    RecordedRun,
    parse_count,
    run_preflight,
    select_lane,
    validate_campaign_inputs,
)
from fin.eval.acceptance.protocol import (
    LANES,
    MAX_COUNT,
    AcceptanceInputError,
    CaseManifest,
    CaseSpec,
    RunManifest,
)

CHECK_IDS = (
    "required_packages",
    "database_target",
    "fixture_provenance",
    "seam_imports",
    "request_event_schemas",
    "runtime_readiness",
    "browser_smoke",
    "model_and_tools",
)
TEST_DSN = "postgresql://placeholder@localhost:5432/fin_acceptance_test"


@pytest.fixture(autouse=True)
def _clean_acceptance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FIN_TEST_DSN", "FIN_API_KEY", "FIN_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _case(**overrides: Any) -> CaseSpec:
    fields: dict[str, Any] = {
        "case_id": "M01",
        "case_version": "1.0.0",
        "lane": "mechanism",
        "primary_category": "ordering",
        "fixture_version": "fixture-mechanism@1.0.0",
        "provenance": "authorized synthetic fixture",
        "rebuild_instructions": "uv run python scripts/rebuild_fixture.py M01",
        "required": True,
        "rubric_id": None,
        "rubric_version": None,
    }
    fields.update(overrides)
    return CaseSpec(**fields)


def _manifest(
    lane: str = "mechanism",
    cases: tuple[CaseSpec, ...] | None = None,
    **overrides: Any,
) -> CaseManifest:
    fields: dict[str, Any] = {
        "manifest_version": "1.0.0",
        "lane": lane,
        "dataset_manifest_version": "1.0.0",
        "cases": cases if cases is not None else (_case(lane=lane),),
        "created_by": "preflight-test",
    }
    fields.update(overrides)
    return CaseManifest(**fields)


def _run(lane: str = "mechanism", **overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "lane": lane,
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


def _ready_probes(**extra: Any) -> dict[str, Any]:
    probes: dict[str, Any] = {
        PROBE_RUNTIME_READINESS: lambda: None,
        PROBE_REQUEST_EVENT_SCHEMAS: lambda: None,
    }
    probes.update(extra)
    return probes


def _result_for(
    lane: str,
    root: Path,
    *,
    manifest: CaseManifest | None = None,
    requires_live_model: bool = False,
    probes: dict[str, Any] | None = None,
    command: str = "uv run pytest -q",
) -> PreflightResult:
    return run_preflight(
        lane,
        root=root,
        command=command,
        case_manifest=manifest if manifest is not None else _manifest(lane),
        requires_live_model=requires_live_model,
        probes=_ready_probes() if probes is None else probes,
    )


def _check(result: PreflightResult, check_id: str) -> PreflightCheck:
    for check in result.checks:
        if check.check_id == check_id:
            return check
    raise AssertionError(f"{check_id} is missing from the preflight result")


# ---------------------------------------------------------------------------
# Lane selector
# ---------------------------------------------------------------------------


def test_lane_selector_covers_every_protocol_lane() -> None:
    assert tuple(LANES_BY_NAME) == LANES
    for name in LANES:
        assert select_lane(name).name == name


def test_lane_selector_rejects_an_unknown_lane() -> None:
    with pytest.raises(AcceptanceInputError):
        select_lane("nonexistent")


def test_run_preflight_rejects_an_unknown_lane(tmp_path: Path) -> None:
    with pytest.raises(AcceptanceInputError):
        run_preflight(
            "nonexistent",
            root=tmp_path,
            command="uv run pytest -q",
            case_manifest=_manifest(),
        )


# ---------------------------------------------------------------------------
# Passing preflight and the check inventory
# ---------------------------------------------------------------------------


def test_mechanism_lane_passes_when_every_requirement_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for("mechanism", tmp_path)

    assert result.passed is True
    assert result.exit_code == 0
    assert result.missing_capability is None
    assert tuple(check.check_id for check in result.checks) == CHECK_IDS
    assert all(check.ok for check in result.checks)
    assert _check(result, "browser_smoke").required is False
    assert _check(result, "model_and_tools").required is False
    assert _check(result, "database_target").required is True


def test_database_check_reports_the_name_and_never_the_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for("mechanism", tmp_path)

    check = _check(result, "database_target")
    assert "fin_acceptance_test" in check.detail
    rendered = json.dumps(result.to_dict())
    assert "placeholder@" not in rendered
    assert "postgresql://" not in rendered


def test_keyword_conninfo_form_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FIN_TEST_DSN",
        "host=localhost port=5432 dbname=fin_acceptance_test user=placeholder",
    )

    result = _result_for("mechanism", tmp_path)

    assert _check(result, "database_target").ok is True


# ---------------------------------------------------------------------------
# Missing capabilities
# ---------------------------------------------------------------------------


def test_unset_dsn_blocks_the_lane(tmp_path: Path) -> None:
    result = _result_for("mechanism", tmp_path)

    check = _check(result, "database_target")
    assert check.ok is False
    assert check.required is True
    assert "FIN_TEST_DSN" in check.detail
    assert result.missing_capability == "test_database"
    assert result.exit_code == 2
    assert result.passed is False


def test_dsn_naming_a_development_database_blocks_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FIN_TEST_DSN", "postgresql://placeholder@localhost:5432/fin_dev"
    )

    result = _result_for("mechanism", tmp_path)

    check = _check(result, "database_target")
    assert check.ok is False
    assert "fin_dev" in check.detail
    assert "_test" in check.detail
    assert "placeholder@" not in check.detail
    assert result.missing_capability == "test_database"


def test_unparseable_dsn_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", "not-a-connection-setting")

    result = _result_for("mechanism", tmp_path)

    assert _check(result, "database_target").ok is False
    assert result.missing_capability == "test_database"


def test_dsn_without_a_database_name_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", "postgresql://placeholder@localhost:5432")

    result = _result_for("mechanism", tmp_path)

    assert _check(result, "database_target").ok is False
    assert result.missing_capability == "test_database"


def test_browser_lane_without_playwright_blocks_the_lane(tmp_path: Path) -> None:
    result = _result_for("browser", tmp_path)

    check = _check(result, "required_packages")
    assert check.ok is False
    assert str(tmp_path) not in check.detail
    assert result.missing_capability == "playwright_test_dependency"
    assert result.exit_code == 2


def test_later_probe_checks_do_not_preempt_earlier_required_checks(
    tmp_path: Path,
) -> None:
    # request_event_schemas and runtime_readiness are absent for both lanes here;
    # the earlier required checks must still report the missing capability first.
    mechanism = _result_for("mechanism", tmp_path, probes={})
    assert mechanism.missing_capability == "test_database"
    assert _check(mechanism, "request_event_schemas").ok is False

    browser = _result_for("browser", tmp_path, probes={})
    assert browser.missing_capability == "playwright_test_dependency"
    assert _check(browser, "request_event_schemas").ok is False


def test_an_unimportable_required_package_names_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "fin.host.http", None)

    result = _result_for("mechanism", tmp_path)

    check = _check(result, "required_packages")
    assert check.ok is False
    assert "fin.host.http" in check.detail
    assert result.missing_capability == "required_packages"


def test_browser_lane_with_playwright_still_needs_the_smoke(tmp_path: Path) -> None:
    result = _result_for(
        "browser", tmp_path, probes=_ready_probes(**{PROBE_PLAYWRIGHT: lambda: None})
    )

    assert _check(result, "required_packages").ok is True
    smoke = _check(result, "browser_smoke")
    assert smoke.ok is False
    assert smoke.required is True
    assert result.missing_capability == "browser_smoke"


def test_browser_smoke_probe_can_clear_the_check(tmp_path: Path) -> None:
    result = _result_for(
        "browser",
        tmp_path,
        probes=_ready_probes(
            **{
                PROBE_PLAYWRIGHT: lambda: None,
                PROBE_BROWSER_SMOKE: lambda: None,
            }
        ),
    )

    assert _check(result, "browser_smoke").ok is True
    assert result.passed is True
    assert result.exit_code == 0


def test_browser_lane_does_not_require_a_database(tmp_path: Path) -> None:
    result = _result_for("browser", tmp_path)

    database = _check(result, "database_target")
    assert database.required is False
    assert database.ok is True


def test_absent_readiness_probe_blocks_the_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism", tmp_path, probes={PROBE_REQUEST_EVENT_SCHEMAS: lambda: None}
    )

    check = _check(result, "runtime_readiness")
    assert check.ok is False
    assert check.required is True
    assert result.missing_capability == "runtime_readiness"
    assert result.exit_code == 2


def test_readiness_probe_reason_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism",
        tmp_path,
        probes={
            PROBE_REQUEST_EVENT_SCHEMAS: lambda: None,
            PROBE_RUNTIME_READINESS: lambda: "schema migration is behind",
        },
    )

    check = _check(result, "runtime_readiness")
    assert check.ok is False
    assert "schema migration is behind" in check.detail
    assert result.missing_capability == "runtime_readiness"


def test_empty_readiness_reason_counts_as_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism", tmp_path, probes=_ready_probes(**{PROBE_RUNTIME_READINESS: lambda: ""})
    )

    assert _check(result, "runtime_readiness").ok is True
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Fixture provenance
# ---------------------------------------------------------------------------


def test_missing_fixture_provenance_blocks_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    manifest = _manifest(cases=(_case(provenance=""),))

    result = _result_for("mechanism", tmp_path, manifest=manifest)

    check = _check(result, "fixture_provenance")
    assert check.ok is False
    assert "provenance" in check.detail
    assert result.missing_capability == "fixture_provenance"


def test_missing_fixture_version_blocks_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    manifest = _manifest(cases=(_case(fixture_version=""),))

    result = _result_for("mechanism", tmp_path, manifest=manifest)

    check = _check(result, "fixture_provenance")
    assert check.ok is False
    assert "fixture_version" in check.detail


def test_missing_rebuild_instructions_block_a_required_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    manifest = _manifest(cases=(_case(rebuild_instructions=""),))

    result = _result_for("mechanism", tmp_path, manifest=manifest)

    check = _check(result, "fixture_provenance")
    assert check.ok is False
    assert "rebuild_instructions" in check.detail
    assert result.missing_capability == "fixture_provenance"


def test_optional_non_reproducible_case_may_omit_rebuild_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    manifest = _manifest(
        cases=(
            _case(
                required=False,
                rebuild_instructions="",
                provenance="authorized non-reproducible capture",
            ),
        ),
    )

    result = _result_for("mechanism", tmp_path, manifest=manifest)

    assert _check(result, "fixture_provenance").ok is True


def test_manifest_lane_mismatch_blocks_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    manifest = _manifest(lane="browser", cases=(_case(lane="browser"),))

    result = _result_for("mechanism", tmp_path, manifest=manifest)

    check = _check(result, "fixture_provenance")
    assert check.ok is False
    assert "lane" in check.detail
    assert result.missing_capability == "fixture_provenance"


# ---------------------------------------------------------------------------
# Seam imports and probe/check exceptions
# ---------------------------------------------------------------------------


def test_seam_import_failure_names_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    monkeypatch.setitem(sys.modules, "fin.host.thread_store", None)

    result = _result_for("mechanism", tmp_path)

    check = _check(result, "seam_imports")
    assert check.ok is False
    assert "fin.host.thread_store" in check.detail
    assert result.missing_capability == "seam_imports"
    assert result.exit_code == 2


def test_a_raising_probe_is_reported_by_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    secret = "postgresql://placeholder:hunter2@localhost:5432/fin_dev"

    def _explode() -> str | None:
        raise RuntimeError(secret)

    result = _result_for(
        "mechanism", tmp_path, probes=_ready_probes(**{PROBE_RUNTIME_READINESS: _explode})
    )

    check = _check(result, "runtime_readiness")
    assert check.ok is False
    assert check.detail == "check raised RuntimeError"
    assert "hunter2" not in check.detail
    assert "postgresql" not in check.detail
    assert result.missing_capability == "runtime_readiness"
    assert result.exit_code == 2


def test_a_check_that_raises_is_reported_by_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    class _BrokenRoot:
        def __str__(self) -> str:
            raise RuntimeError("postgresql://placeholder:hunter2@localhost/fin_dev")

    result = run_preflight(
        "browser",
        root=_BrokenRoot(),  # type: ignore[arg-type]
        command="uv run pytest -q",
        case_manifest=_manifest("browser", cases=(_case(lane="browser"),)),
    )

    check = _check(result, "required_packages")
    assert check.ok is False
    assert check.detail == "check raised RuntimeError"
    assert "hunter2" not in check.detail
    assert result.missing_capability == "required_packages"


def test_a_non_callable_probe_fails_the_check_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism", tmp_path, probes={PROBE_RUNTIME_READINESS: "ready"}
    )

    assert _check(result, "runtime_readiness").ok is False
    assert result.exit_code == 2


def test_a_sensitive_probe_reason_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism",
        tmp_path,
        probes={
            PROBE_RUNTIME_READINESS: lambda: (
                "cannot connect to postgresql://placeholder:hunter2@localhost/db"
            )
        },
    )

    check = _check(result, "runtime_readiness")
    assert check.ok is False
    assert "hunter2" not in check.detail
    assert "postgresql://" not in check.detail


# ---------------------------------------------------------------------------
# Request/event schemas
# ---------------------------------------------------------------------------


def test_absent_request_event_schema_probe_blocks_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism", tmp_path, probes={PROBE_RUNTIME_READINESS: lambda: None}
    )

    check = _check(result, "request_event_schemas")
    assert check.ok is False
    assert check.required is True
    assert result.missing_capability == "request_event_schemas"
    assert result.exit_code == 2
    assert result.passed is False


def test_request_event_schema_probe_reason_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism",
        tmp_path,
        probes=_ready_probes(
            **{PROBE_REQUEST_EVENT_SCHEMAS: lambda: "event schema version is stale"}
        ),
    )

    check = _check(result, "request_event_schemas")
    assert check.ok is False
    assert "event schema version is stale" in check.detail
    assert result.missing_capability == "request_event_schemas"


def test_request_event_schema_probe_returning_none_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for(
        "mechanism",
        tmp_path,
        probes=_ready_probes(**{PROBE_REQUEST_EVENT_SCHEMAS: lambda: None}),
    )

    assert _check(result, "request_event_schemas").ok is True
    assert result.passed is True
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Live model and tools
# ---------------------------------------------------------------------------


def test_business_lane_requires_the_model_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for("business", tmp_path)

    check = _check(result, "model_and_tools")
    assert check.ok is False
    assert check.required is True
    assert "FIN_API_KEY" in check.detail
    assert "FIN_MODEL" in check.detail
    assert result.missing_capability == "live_model"
    assert result.exit_code == 2


def test_business_lane_with_settings_but_no_probe_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    monkeypatch.setenv("FIN_API_KEY", "placeholder-key")
    monkeypatch.setenv("FIN_MODEL", "placeholder-model")

    result = _result_for("business", tmp_path)

    check = _check(result, "model_and_tools")
    assert check.ok is False
    assert check.required is True
    assert "not supplied" in check.detail
    assert result.missing_capability == "live_model"
    assert result.exit_code == 2


def test_business_lane_passes_with_model_settings_and_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    monkeypatch.setenv("FIN_API_KEY", "placeholder-key")
    monkeypatch.setenv("FIN_MODEL", "placeholder-model")

    result = _result_for(
        "business", tmp_path, probes=_ready_probes(**{PROBE_MODEL_AND_TOOLS: lambda: None})
    )

    assert _check(result, "model_and_tools").ok is True
    assert result.passed is True
    rendered = json.dumps(result.to_dict())
    assert "placeholder-key" not in rendered
    assert "placeholder-model" not in rendered


def test_model_probe_reason_blocks_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    monkeypatch.setenv("FIN_API_KEY", "placeholder-key")
    monkeypatch.setenv("FIN_MODEL", "placeholder-model")

    result = _result_for(
        "business",
        tmp_path,
        probes=_ready_probes(
            **{PROBE_MODEL_AND_TOOLS: lambda: "model endpoint is unreachable"}
        ),
    )

    check = _check(result, "model_and_tools")
    assert check.ok is False
    assert "model endpoint is unreachable" in check.detail
    assert result.missing_capability == "live_model"
    assert result.exit_code == 2


def test_mechanism_lane_requires_model_settings_only_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    monkeypatch.setenv("FIN_API_KEY", "placeholder-key")

    result = _result_for("mechanism", tmp_path, requires_live_model=True)

    check = _check(result, "model_and_tools")
    assert check.ok is False
    assert check.required is True
    assert "FIN_MODEL" in check.detail
    assert "FIN_API_KEY" not in check.detail
    assert result.missing_capability == "live_model"


def test_mechanism_lane_skips_model_settings_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for("mechanism", tmp_path)

    check = _check(result, "model_and_tools")
    assert check.required is False
    assert check.ok is True
    assert result.passed is True


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_result_to_dict_is_json_safe_with_the_contract_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)

    result = _result_for("mechanism", tmp_path, command="uv run pytest -q tests/host")
    payload = result.to_dict()

    assert set(payload) == {
        "lane",
        "checks",
        "command",
        "exit_code",
        "missing_capability",
    }
    assert payload["lane"] == "mechanism"
    assert payload["command"] == "uv run pytest -q tests/host"
    assert payload["exit_code"] == 0
    assert payload["missing_capability"] is None
    assert isinstance(payload["checks"], list)
    for entry in payload["checks"]:
        assert set(entry) == {"check_id", "ok", "detail", "required"}
    json.dumps(payload)


def test_result_dataclasses_are_frozen() -> None:
    check = PreflightCheck(
        check_id="seam_imports", ok=True, detail="ok", required=True
    )
    result = PreflightResult(
        lane="mechanism",
        checks=(check,),
        command="uv run pytest -q",
        exit_code=0,
        missing_capability=None,
    )
    with pytest.raises(Exception):
        check.ok = False  # type: ignore[misc]
    with pytest.raises(Exception):
        result.exit_code = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Campaign-start input contract
# ---------------------------------------------------------------------------


def test_clean_campaign_inputs_are_accepted() -> None:
    assert validate_campaign_inputs(_manifest(), _run(), ()) == []


def test_an_empty_manifest_is_rejected() -> None:
    problems = validate_campaign_inputs(_manifest(cases=()), _run(), ())

    assert any("no cases" in problem for problem in problems)


def test_a_lane_mismatch_between_run_and_manifest_is_rejected() -> None:
    problems = validate_campaign_inputs(_manifest("mechanism"), _run("browser"), ())

    assert any("lane" in problem for problem in problems)


def test_a_manifest_version_mismatch_is_rejected() -> None:
    problems = validate_campaign_inputs(
        _manifest(), _run(manifest_version="2.0.0"), ()
    )

    assert any("manifest_version" in problem for problem in problems)


def test_a_dataset_manifest_mismatch_is_rejected() -> None:
    problems = validate_campaign_inputs(
        _manifest(), _run(dataset_manifest_version="9.9.9"), ()
    )

    assert any("dataset_manifest_version" in problem for problem in problems)


def test_a_field_failing_the_protocol_validators_is_rejected() -> None:
    problems = validate_campaign_inputs(_manifest(), _run(run_id=""), ())

    assert any("run_id" in problem for problem in problems)


def test_a_required_case_without_provenance_is_rejected() -> None:
    manifest = _manifest(cases=(_case(provenance=""),))

    problems = validate_campaign_inputs(manifest, _run(), ())

    assert any("provenance" in problem for problem in problems)


def test_an_identical_recorded_run_payload_is_not_a_competing_start() -> None:
    recorded = RecordedRun(run_id="run-1", payload=_run().to_dict())

    assert validate_campaign_inputs(_manifest(), _run(), (recorded,)) == []


def test_a_bare_recorded_run_id_is_a_resume_not_a_conflict() -> None:
    recorded = RecordedRun(run_id="run-1")

    assert validate_campaign_inputs(_manifest(), _run(), (recorded,)) == []


def test_a_recorded_payload_with_the_same_run_id_but_other_fields_is_rejected() -> None:
    recorded = RecordedRun(
        run_id="run-1", payload=_run(code_version="cafebabe").to_dict()
    )

    problems = validate_campaign_inputs(_manifest(), _run(), (recorded,))

    assert any("already recorded" in problem for problem in problems)


def test_a_malformed_recorded_payload_is_rejected() -> None:
    problems = validate_campaign_inputs(
        _manifest(), _run(), ("{not json",)  # type: ignore[arg-type]
    )

    assert any("malformed" in problem for problem in problems)


def test_another_run_id_in_the_history_is_not_a_competing_start() -> None:
    recorded = RecordedRun(run_id="run-0")

    assert validate_campaign_inputs(_manifest(), _run(), (recorded,)) == []


def test_a_held_campaign_lock_rejects_the_start() -> None:
    problems = validate_campaign_inputs(_manifest(), _run(), (), ("run-0",))

    assert any("lock" in problem for problem in problems)


def test_empty_lock_sequences_allow_the_start() -> None:
    assert validate_campaign_inputs(_manifest(), _run(), (), (), ()) == []


def test_required_cases_are_never_dropped() -> None:
    cases = (_case(case_id="M01"), _case(case_id="M02"))
    manifest = _manifest(cases=cases)

    validate_campaign_inputs(manifest, _run(), ())

    assert tuple(case.case_id for case in manifest.cases) == ("M01", "M02")


def test_a_non_manifest_input_is_rejected() -> None:
    problems = validate_campaign_inputs(object(), _run(), ())  # type: ignore[arg-type]

    assert problems


# ---------------------------------------------------------------------------
# Count parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    (("0", 0), ("1", 1), ("12", 12), (" 7 ", 7), (str(MAX_COUNT), MAX_COUNT)),
)
def test_parse_count_accepts_non_negative_integers(text: str, expected: int) -> None:
    assert parse_count(text) == expected


@pytest.mark.parametrize(
    "text",
    ("-1", "-0", "1.5", "abc", "", "  ", "+3", "1e3", str(MAX_COUNT + 1)),
)
def test_parse_count_rejects_invalid_counts(text: str) -> None:
    with pytest.raises(AcceptanceInputError):
        parse_count(text)


def test_parse_count_rejects_a_non_string() -> None:
    with pytest.raises(AcceptanceInputError):
        parse_count(12)  # type: ignore[arg-type]
