"""Default-collected tests for ``python -m fin.eval.acceptance``.

The CLI is driven in process through :func:`fin.eval.acceptance.__main__.main`
so failures are attributable; one subprocess test pins the module entry point
that P8/P9/P10 will actually record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fin.eval.acceptance.__main__ import main
from fin.eval.acceptance.protocol import CaseManifest, RunManifest

TEST_DSN = "postgresql://placeholder@localhost:5432/fin_acceptance_test"
REPO_ROOT = Path(__file__).resolve().parents[3]
KEY_VALUE = "placeholder-key-value"
MODEL_VALUE = "placeholder-model-value"


@pytest.fixture(autouse=True)
def _clean_acceptance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FIN_TEST_DSN", "FIN_API_KEY", "FIN_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _case(lane: str, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "case_id": "M01",
        "case_version": "1.0.0",
        "lane": lane,
        "primary_category": "ordering",
        "fixture_version": "fixture-mechanism@1.0.0",
        "provenance": "authorized synthetic fixture",
        "rebuild_instructions": "uv run python scripts/rebuild_fixture.py M01",
        "required": True,
        "rubric_id": None,
        "rubric_version": None,
    }
    fields.update(overrides)
    return fields


def _manifest_payload(lane: str = "mechanism", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "manifest_version": "1.0.0",
        "lane": lane,
        "dataset_manifest_version": "1.0.0",
        "cases": [_case(lane)],
        "created_by": "cli-test",
    }
    payload.update(overrides)
    return payload


def _run_payload(lane: str = "mechanism", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    payload.update(overrides)
    return payload


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _paths(
    tmp_path: Path, lane: str = "mechanism", **run_overrides: Any
) -> tuple[Path, Path]:
    return (
        _write(tmp_path / "manifest.json", _manifest_payload(lane)),
        _write(tmp_path / "run.json", _run_payload(lane, **run_overrides)),
    )


def _preflight_args(
    manifest: Path, run: Path, lane: str = "mechanism", root: Path | None = None
) -> list[str]:
    args = [
        "preflight",
        f"--lane={lane}",
        f"--manifest={manifest}",
        f"--run-manifest={run}",
    ]
    if root is not None:
        args.append(f"--root={root}")
    return args


def _ready_probe_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module = tmp_path / "acceptance_probe.py"
    module.write_text(
        "def ready():\n"
        "    return None\n"
        "\n"
        "def broken():\n"
        "    return 'runtime is not ready'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "acceptance_probe"


def _json(captured: pytest.CaptureFixture[str]) -> dict[str, Any]:
    payload = json.loads(captured.readouterr().out)
    assert isinstance(payload, dict)
    return payload


def _combined(captured: pytest.CaptureFixture[str]) -> str:
    result = captured.readouterr()
    return result.out + result.err


# ---------------------------------------------------------------------------
# Exit-code mapping
# ---------------------------------------------------------------------------


def test_ready_environment_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    module = _ready_probe_module(tmp_path, monkeypatch)
    manifest, run = _paths(tmp_path)

    code = main(
        _preflight_args(manifest, run, root=tmp_path)
        + [
            f"--probe=runtime_readiness={module}:ready",
            f"--probe=request_event_schemas={module}:ready",
        ]
    )

    assert code == 0
    payload = _json(capsys)
    assert payload["exit_code"] == 0
    assert payload["lane"] == "mechanism"
    assert payload["missing_capability"] is None
    assert isinstance(payload["command"], str)
    assert payload["command"].startswith("python -m fin.eval.acceptance preflight")


def test_a_blocked_lane_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, run = _paths(tmp_path)

    code = main(_preflight_args(manifest, run))

    assert code == 2
    payload = _json(capsys)
    assert payload["exit_code"] == 2
    assert payload["missing_capability"] == "test_database"
    assert [check["check_id"] for check in payload["checks"]] == [
        "required_packages",
        "database_target",
        "fixture_provenance",
        "seam_imports",
        "request_event_schemas",
        "runtime_readiness",
        "browser_smoke",
        "model_and_tools",
    ]
    for check in payload["checks"]:
        assert set(check) == {"check_id", "ok", "detail", "required"}


def test_a_probe_reason_blocks_with_its_capability(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    module = _ready_probe_module(tmp_path, monkeypatch)
    manifest, run = _paths(tmp_path)

    code = main(
        _preflight_args(manifest, run, root=tmp_path)
        + [
            f"--probe=runtime_readiness={module}:broken",
            f"--probe=request_event_schemas={module}:ready",
        ]
    )

    assert code == 2
    payload = _json(capsys)
    assert payload["missing_capability"] == "runtime_readiness"


def test_a_request_event_schema_probe_reason_blocks_with_its_capability(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    module = _ready_probe_module(tmp_path, monkeypatch)
    manifest, run = _paths(tmp_path)

    code = main(
        _preflight_args(manifest, run, root=tmp_path)
        + [
            f"--probe=runtime_readiness={module}:ready",
            f"--probe=request_event_schemas={module}:broken",
        ]
    )

    assert code == 2
    payload = _json(capsys)
    assert payload["missing_capability"] == "request_event_schemas"


def test_an_invalid_manifest_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = _paths(tmp_path)
    manifest = _write(tmp_path / "manifest.json", {"lane": "mechanism"})

    code = main(_preflight_args(manifest, run))

    assert code == 1
    assert "manifest" in _combined(capsys).lower()


def test_an_empty_manifest_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = _paths(tmp_path)
    manifest = _write(tmp_path / "manifest.json", _manifest_payload(cases=[]))

    code = main(_preflight_args(manifest, run))

    assert code == 1
    assert "cases" in _combined(capsys)


def test_a_malformed_json_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = _paths(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")

    code = main(_preflight_args(manifest, run))

    assert code == 1
    assert "JSON" in _combined(capsys)


def test_a_missing_manifest_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = _paths(tmp_path)

    code = main(_preflight_args(tmp_path / "absent.json", run))

    assert code == 1
    assert "cannot read" in _combined(capsys)


def test_an_unknown_lane_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, run = _paths(tmp_path)

    code = main(_preflight_args(manifest, run, lane="nonexistent"))

    assert code == 1
    assert "nonexistent" in _combined(capsys)


def test_a_run_manifest_lane_mismatch_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write(tmp_path / "manifest.json", _manifest_payload())
    run = _write(tmp_path / "run.json", _run_payload("browser"))

    code = main(_preflight_args(manifest, run))

    assert code == 1
    assert "lane" in _combined(capsys)


def test_an_unknown_probe_name_exits_sixty_four(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, run = _paths(tmp_path)

    code = main(_preflight_args(manifest, run) + ["--probe=unknown=os:getcwd"])

    assert code == 64
    assert "unknown probe" in _combined(capsys)


def test_a_probe_module_that_cannot_import_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, run = _paths(tmp_path)

    code = main(
        _preflight_args(manifest, run)
        + ["--probe=runtime_readiness=no_such_module:ready"]
    )

    assert code == 1
    assert "cannot be imported" in _combined(capsys)


def test_a_usage_error_exits_sixty_four(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["preflight"])

    assert code == 64
    assert "usage" in _combined(capsys).lower()


def test_an_unknown_argument_exits_sixty_four(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, run = _paths(tmp_path)

    code = main(_preflight_args(manifest, run) + ["--surprise=1"])

    assert code == 64
    assert "usage" in _combined(capsys).lower()


def test_no_subcommand_exits_sixty_four(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main([])

    assert code == 64
    assert "usage" in _combined(capsys).lower()


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--help"])

    assert code == 0
    assert "preflight" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_output_never_carries_a_dsn_or_key_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_TEST_DSN", TEST_DSN)
    monkeypatch.setenv("FIN_API_KEY", KEY_VALUE)
    monkeypatch.setenv("FIN_MODEL", MODEL_VALUE)
    module = _ready_probe_module(tmp_path, monkeypatch)
    manifest, run = _paths(tmp_path, "business")

    code = main(
        _preflight_args(manifest, run, lane="business", root=tmp_path)
        + [
            "--live-model",
            f"--probe=runtime_readiness={module}:ready",
            f"--probe=request_event_schemas={module}:ready",
            f"--probe=model_and_tools={module}:ready",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    for secret in (TEST_DSN, "placeholder@", KEY_VALUE, MODEL_VALUE):
        assert secret not in out
    assert "fin_acceptance_test" in out


def test_a_browser_block_names_the_capability_and_hides_the_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, run = _paths(tmp_path, "browser")

    code = main(_preflight_args(manifest, run, lane="browser", root=tmp_path))

    out = capsys.readouterr().out
    assert code == 2
    payload = json.loads(out)
    assert payload["missing_capability"] == "playwright_test_dependency"
    details = [check["detail"] for check in payload["checks"]]
    assert not any(str(tmp_path) in detail for detail in details)


def test_an_invalid_input_message_hides_the_raw_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "postgresql://placeholder:hunter2@localhost:5432/fin_dev"
    _, run = _paths(tmp_path)
    payload = _manifest_payload()
    payload["unexpected_field"] = secret
    manifest = _write(tmp_path / "manifest.json", payload)

    code = main(_preflight_args(manifest, run))

    assert code == 1
    out = _combined(capsys)
    assert "hunter2" not in out
    assert "unexpected" in out


# ---------------------------------------------------------------------------
# validate subcommand
# ---------------------------------------------------------------------------


def test_validate_accepts_a_clean_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write(tmp_path / "manifest.json", _manifest_payload())

    code = main(["validate", f"--manifest={manifest}"])

    payload = _json(capsys)
    assert code == 0
    assert payload["lane"] == "mechanism"
    assert payload["exit_code"] == 0
    assert payload["missing_capability"] is None
    assert payload["checks"][0]["check_id"] == "case_manifest"


def test_validate_rejects_an_invalid_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write(tmp_path / "manifest.json", _manifest_payload(cases=[]))

    code = main(["validate", f"--manifest={manifest}"])

    assert code == 1
    assert "cases" in _combined(capsys)


def test_validate_rejects_a_negative_case_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write(tmp_path / "manifest.json", _manifest_payload())

    code = main(["validate", f"--manifest={manifest}", "--cases=-1"])

    assert code == 1
    assert "non-negative" in _combined(capsys)


def test_validate_rejects_an_overflowing_case_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write(tmp_path / "manifest.json", _manifest_payload())

    code = main(["validate", f"--manifest={manifest}", "--cases=999999999"])

    assert code == 1
    assert "MAX_COUNT" in _combined(capsys)


def test_validate_accepts_an_explicit_clean_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write(tmp_path / "manifest.json", _manifest_payload())

    code = main(["validate", f"--manifest={manifest}", "--cases=1"])

    assert code == 0
    _json(capsys)


# ---------------------------------------------------------------------------
# Entry point and manifest construction
# ---------------------------------------------------------------------------


def test_default_root_is_the_repository_root() -> None:
    from fin.eval.acceptance import __main__ as cli

    assert cli.DEFAULT_ROOT == REPO_ROOT


def test_cli_builds_the_protocol_manifests_from_the_files(tmp_path: Path) -> None:
    manifest_path, run_path = _paths(tmp_path)

    manifest = CaseManifest.from_dict(json.loads(manifest_path.read_text()))
    run = RunManifest.from_dict(json.loads(run_path.read_text()))

    assert manifest.lane == "mechanism"
    assert run.run_id == "run-1"


def test_running_as_a_module_reports_a_block_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, run = _paths(tmp_path)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("FIN_TEST_DSN", "FIN_API_KEY", "FIN_MODEL")
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "fin.eval.acceptance",
            "preflight",
            "--lane=mechanism",
            f"--manifest={manifest}",
            f"--run-manifest={run}",
            f"--root={tmp_path}",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["missing_capability"] == "test_database"
    assert "postgresql://" not in completed.stdout
