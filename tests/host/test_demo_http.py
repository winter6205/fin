"""Contract for the demo-only status and reset doors."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fin.corpus.demo_seed import (
    SEED_RESULT_FAILED,
    SEED_RESULT_PREFIX,
    SEED_RESULT_REFUSED,
    format_seed_result,
    parse_seed_result,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_IKNOW_ROOT = "/tmp/fin-demo-iknow"
_DEMO_LOGGER = "fin.host.demo_http"


@pytest.fixture
def demo_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from fin.host.http import app

    monkeypatch.setenv("FIN_DEMO_MODE", "1")
    monkeypatch.setenv("FIN_DEMO_EMBED_DIMENSIONS", "768")
    monkeypatch.setenv("FIN_DEMO_EMBED_GENERATION", "demo-embedding-v1")
    monkeypatch.setenv("FIN_IKNOW_ROOT", _IKNOW_ROOT)
    monkeypatch.setenv("IKNOW_MEMORY_HOME", "/tmp/fin-demo-memory")
    return TestClient(app)


def _demo_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _DEMO_LOGGER]


def test_demo_status_reports_only_the_explicit_demo_switch(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert demo_client.get("/api/demo").json() == {"enabled": True}

    monkeypatch.setenv("FIN_DEMO_MODE", "true")
    assert demo_client.get("/api/demo").json() == {"enabled": False}


def test_reset_runs_the_fixed_seed_command_with_server_configuration(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route restores the demo data with the seed the demo itself starts.

    The command is fixed and comes from the server environment: the request
    body cannot add a flag, and the seed runs WITHOUT ``--reset`` -- the
    destructive dev-stack wipe is the paired repository's capability, and fin
    has no bolt client for its graph half, so asking for it could only fail.
    """
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> None:
        calls.append({"command": command, **kwargs})

    monkeypatch.setattr("fin.host.demo_http.subprocess.run", run)

    response = demo_client.post(
        "/api/demo/reset",
        json={"command": "arbitrary-command", "args": ["--fixture-dir", "/tmp/elsewhere"]},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "reset"}
    assert len(calls) == 1
    call = calls[0]
    assert call["command"] == [
        sys.executable,
        str(_REPO_ROOT / "scripts" / "seed_demo.py"),
        "--yes",
        "--embed-dimensions",
        "768",
        "--embed-generation",
        "demo-embedding-v1",
        "--iknow-root",
        _IKNOW_ROOT,
    ]
    # The request body may not become command data.
    assert "--reset" not in call["command"]
    assert "--fixture-dir" not in call["command"]
    assert "arbitrary-command" not in call["command"]
    assert call["cwd"] == _REPO_ROOT
    assert call["check"] is True
    assert call["env"]["IKNOW_MEMORY_HOME"] == "/tmp/fin-demo-memory"
    assert call["env"]["FIN_IKNOW_ROOT"] == _IKNOW_ROOT
    assert call["capture_output"] is True
    assert call["text"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"command": "arbitrary-command", "args": ["--fixture-dir", "/tmp/elsewhere"]},
        {"--reset": True},
        {"iknow_root": "/tmp/elsewhere"},
        ["--reset", "--iknow-root", "/tmp/elsewhere"],
        "arbitrary-command",
    ],
)
def test_no_request_body_shape_can_influence_the_seed_command(
    demo_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
) -> None:
    """The command is fixed: the body is never read as command data."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post("/api/demo/reset", json=body)

    assert response.status_code == 200
    assert len(calls) == 1
    command = calls[0]
    assert "--reset" not in command
    assert "--fixture-dir" not in command
    assert "/tmp/elsewhere" not in command
    assert command[-2:] == ["--iknow-root", _IKNOW_ROOT]


def test_reset_refuses_a_cross_origin_request_without_running_seed(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post(
        "/api/demo/reset", headers={"Origin": "https://attacker.invalid"}
    )

    assert response.status_code == 403
    assert response.json() == {"error": "demo_origin_forbidden"}
    assert calls == []


def test_reset_refuses_cross_site_fetch_metadata_without_origin(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post(
        "/api/demo/reset", headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert response.json() == {"error": "demo_origin_forbidden"}
    assert calls == []


def test_reset_accepts_same_origin_fetch(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post(
        "/api/demo/reset", headers={"Origin": "http://testserver"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_reset_is_not_available_outside_demo_mode(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("FIN_DEMO_MODE", "0")
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 404
    assert response.json() == {"error": "demo_mode_disabled"}
    assert calls == []


@pytest.mark.parametrize(
    "setting,value",
    [
        ("FIN_DEMO_EMBED_DIMENSIONS", None),
        ("FIN_DEMO_EMBED_GENERATION", None),
        ("FIN_DEMO_EMBED_DIMENSIONS", "-1"),
        ("FIN_DEMO_EMBED_DIMENSIONS", "not-a-number"),
        ("FIN_DEMO_EMBED_GENERATION", "  "),
    ],
)
def test_reset_refuses_missing_or_invalid_seed_configuration_without_running(
    demo_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str | None,
) -> None:
    calls: list[list[str]] = []
    if value is None:
        monkeypatch.delenv(setting, raising=False)
    else:
        monkeypatch.setenv(setting, value)
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 503
    assert response.json()["error"] in {
        "demo_configuration_missing",
        "demo_configuration_invalid",
    }
    assert calls == []


def test_reset_refuses_a_missing_iknow_root_before_running_the_seed(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paired checkout is server configuration, not request data.

    Without it the seed has no checkout to run the worker from, so the door
    says which setting is missing instead of failing inside the seed.
    """
    calls: list[list[str]] = []
    monkeypatch.delenv("FIN_IKNOW_ROOT", raising=False)
    monkeypatch.setattr(
        "fin.host.demo_http.subprocess.run",
        lambda command, **_: calls.append(command),
    )

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 503
    assert response.json() == {
        "error": "demo_seed_configuration_missing",
        "setting": "FIN_IKNOW_ROOT",
    }
    assert calls == []


def test_reset_refuses_a_guard_refusal_from_the_seed(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 is the seed's own guard refusal, and stays a 409."""
    calls: list[list[str]] = []

    def refuse(command: list[str], **kwargs: Any) -> None:
        calls.append(command)
        raise subprocess.CalledProcessError(
            2,
            command,
            output="",
            stderr=(
                "[seed:demo] refused (demo_seed_memory_home_unset): no stack\n"
                + format_seed_result(
                    SEED_RESULT_REFUSED, "demo_seed_memory_home_unset"
                )
                + "\n"
            ),
        )

    monkeypatch.setattr("fin.host.demo_http.subprocess.run", refuse)

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 409
    assert response.json() == {"error": "demo_seed_refused"}
    assert len(calls) == 1


def test_reset_answers_a_named_seed_failure_with_the_seed_s_own_code(
    demo_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typed seed failure is 503 and names itself.

    The code is the seed's, not the route's: the operator reads the same name
    here as in the seed's own output, and the serve log records it.
    """
    caplog.set_level(logging.WARNING, logger=_DEMO_LOGGER)

    def fail(command: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr=(
                "[seed:demo] refused (demo_seed_plan_a_entry_point_missing): "
                "the allowlist is not published\n"
                + format_seed_result(
                    SEED_RESULT_FAILED, "demo_seed_plan_a_entry_point_missing"
                )
                + "\n"
            ),
        )

    monkeypatch.setattr("fin.host.demo_http.subprocess.run", fail)

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 503
    assert response.json() == {"error": "demo_seed_plan_a_entry_point_missing"}
    records = _demo_records(caplog)
    assert records
    message = records[-1].getMessage()
    assert "demo_seed_plan_a_entry_point_missing" in message
    assert "exit_code=1" in message


def test_reset_does_not_leak_seed_process_error_output(
    demo_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An untyped crash stays the opaque 500, and the log names it."""
    caplog.set_level(logging.WARNING, logger=_DEMO_LOGGER)

    def fail(command: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            1, command, output="operator detail", stderr="unsafe detail"
        )

    monkeypatch.setattr("fin.host.demo_http.subprocess.run", fail)

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 500
    assert response.json() == {"error": "demo_reset_failed"}
    assert "unsafe detail" not in response.text
    assert "operator detail" not in response.text
    records = _demo_records(caplog)
    assert records
    message = records[-1].getMessage()
    assert "seed_code=none" in message
    assert "exit_code=1" in message


def test_the_failure_log_masks_credentials_in_the_seed_output(
    demo_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The serve log records the failure without becoming a credential file."""
    caplog.set_level(logging.WARNING, logger=_DEMO_LOGGER)

    def fail(command: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr=(
                format_seed_result(SEED_RESULT_FAILED, "demo_seed_store_unavailable")
                + "\n"
                + "postgresql://demo:hunter2@127.0.0.1:5434/fin "
                + "password=hunter2 token=abc123\n"
            ),
        )

    monkeypatch.setattr("fin.host.demo_http.subprocess.run", fail)

    response = demo_client.post("/api/demo/reset")

    assert response.status_code == 503
    assert response.json() == {"error": "demo_seed_store_unavailable"}
    records = _demo_records(caplog)
    assert records
    message = records[-1].getMessage()
    assert "demo_seed_store_unavailable" in message
    assert "hunter2" not in message
    assert "abc123" not in message
    assert "***" in message


def test_concurrent_demo_resets_do_not_start_a_second_seed_process(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[list[str]] = []

    def block(command: list[str], **_: Any) -> None:
        calls.append(command)
        entered.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr("fin.host.demo_http.subprocess.run", block)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(demo_client.post, "/api/demo/reset")
        assert entered.wait(timeout=2)
        second = demo_client.post("/api/demo/reset")
        release.set()
        first_response = first.result(timeout=2)

    assert first_response.status_code == 200
    assert second.status_code == 409
    assert second.json() == {"error": "demo_reset_in_progress"}
    assert len(calls) == 1


def test_the_typed_result_channel_round_trips_a_seed_failure() -> None:
    """The channel has one owner: the library builds it and reads it back."""
    line = format_seed_result(SEED_RESULT_FAILED, "demo_seed_plan_a_entry_point_missing")

    assert line.startswith(SEED_RESULT_PREFIX)
    assert parse_seed_result(line) == (
        SEED_RESULT_FAILED,
        "demo_seed_plan_a_entry_point_missing",
    )
    assert parse_seed_result("[seed:demo] refused (code): prose") is None
    assert parse_seed_result(f"{SEED_RESULT_PREFIX}status=failed code=Not A Code") is None
    assert parse_seed_result(f"{SEED_RESULT_PREFIX}status=unknown code=some_code") is None


def test_the_seed_cli_prints_the_typed_result_line_the_route_reads() -> None:
    """The producer side of the channel: the CLI's own refusal carries it.

    The refusal is provoked by the missing paired checkout, which the seed
    refuses before it opens a store or a socket.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "seed_demo.py"),
            "--yes",
            "--embed-dimensions",
            "768",
            "--embed-generation",
            "demo-embedding-v1",
        ],
        cwd=_REPO_ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "FIN_IKNOW_ROOT"
        },
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 1
    # The human-readable refusal line is unchanged.
    assert "[seed:demo] refused (" in result.stderr
    parsed = parse_seed_result(result.stderr)
    assert parsed is not None
    assert parsed[0] == SEED_RESULT_FAILED
    assert parsed[1] == "demo_seed_error"
