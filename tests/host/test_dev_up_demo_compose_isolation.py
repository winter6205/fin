"""Demo startup must leave the fin production Compose project alone.

The harness invokes the real launcher with synthetic homes and mocked Docker,
network probes, and serve commands. No service or database is started.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_UP = REPO_ROOT / "scripts" / "dev-up.sh"


class _StartupScenario(NamedTuple):
    name: str
    args: tuple[str, ...]
    demo_mode: bool
    stack: str | None
    checkpoint_owner: str
    expected_projects: tuple[str, ...]


def _launcher_harness(
    tmp_path: Path,
    *,
    demo_mode: bool,
    stack: str | None,
    checkpoint_owner: str,
) -> tuple[Path, dict[str, str], Path]:
    checkout = tmp_path / "checkout"
    for relative in ("scripts/lib", "scripts", ".venv/bin", "web/dist"):
        (checkout / relative).mkdir(parents=True, exist_ok=True)
    shutil.copy(DEV_UP, checkout / "scripts/dev-up.sh")
    for name in ("readiness.sh", "fin-root.sh", "attachment-bounds.sh"):
        shutil.copy(REPO_ROOT / "scripts/lib" / name, checkout / "scripts/lib" / name)
    shutil.copy(REPO_ROOT / "settings.example.yaml", checkout / "settings.example.yaml")
    (checkout / "web/dist/index.html").write_text("synthetic SPA\n", encoding="utf-8")

    if demo_mode:
        # Exercise the linked-worktree guard under its explicitly validated
        # demo + dev-stack exception.
        (checkout / ".git").write_text("gitdir: synthetic\n", encoding="utf-8")

    python = checkout / ".venv/bin/python"
    python.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)

    operator_home = tmp_path / "operator-home"
    fin_home = tmp_path / "fin-home"
    iknow_root = tmp_path / "iknow-memory"
    for path in (
        operator_home / ".fin/compose",
        operator_home / ".iknow-memory/compose",
        iknow_root / "bin",
        iknow_root / "dist",
        iknow_root / "compose",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (operator_home / ".fin/compose/docker-compose.production.yml").write_text(
        "name: fin\n", encoding="utf-8"
    )
    (operator_home / ".iknow-memory/compose/docker-compose.production.yml").write_text(
        "name: iknow-memory\n", encoding="utf-8"
    )
    (iknow_root / "package.json").write_text("{}\n", encoding="utf-8")
    (iknow_root / "bin/iknow-memory.js").write_text("\n", encoding="utf-8")
    (iknow_root / "bin/iknow-memory-config.js").write_text("\n", encoding="utf-8")
    (iknow_root / "dist/index.js").write_text("\n", encoding="utf-8")
    (iknow_root / "docker-compose.yml").write_text("name: iknow-memory-dev\n", encoding="utf-8")
    (iknow_root / "compose/docker-compose.production.yml").write_text(
        "name: iknow-memory\n", encoding="utf-8"
    )
    fin_home.mkdir()
    settings = tmp_path / "settings.yaml"
    settings.write_text("checkpoint:\n  url: synthetic-checkpoint\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    curl_count = tmp_path / "curl-count"
    fake_tools = {
        "docker": """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = "ps" ]; then
  case "$*" in
    *publish=5433*) printf '%s\\n' "$CHECKPOINT_OWNER" ;;
  esac
  exit 0
fi
exit 0
""",
        "node": "#!/bin/sh\nexit 0\n",
        "uv": """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = "run" ] && [ "$2" = "fin-serve" ]; then
  sleep 2
fi
exit 0
""",
        "curl": """#!/bin/sh
count=0
if [ -f "$CURL_COUNT_FILE" ]; then read -r count < "$CURL_COUNT_FILE"; fi
count=$((count + 1))
printf '%s\\n' "$count" > "$CURL_COUNT_FILE"
if [ "$count" -eq 1 ]; then exit 7; fi
printf '%s\\n' '{"knowledge_mcp":"reachable","retrieve_adapter_kind":"mcp","bound_collection_name":"demo","model_identity":"fixture"}'
""",
    }
    for name, body in fake_tools.items():
        executable = bin_dir / name
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    jq = shutil.which("jq")
    assert jq is not None, "jq is required to exercise the real readiness classifier"
    (bin_dir / "jq").symlink_to(jq)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(operator_home),
        "FIN_HOME": str(fin_home),
        "FIN_SETTINGS_FILE": str(settings),
        "FIN_IKNOW_ROOT": str(iknow_root),
        "FIN_READY_TIMEOUT_SECONDS": "5",
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "CALL_LOG": str(call_log),
        "CURL_COUNT_FILE": str(curl_count),
        "CHECKPOINT_OWNER": checkpoint_owner,
    }
    if demo_mode:
        env["FIN_DEMO_MODE"] = "1"
    if stack is not None:
        env["FIN_IKNOW_STACK"] = stack
    return checkout / "scripts/dev-up.sh", env, call_log


@pytest.mark.parametrize(
    "scenario",
    [
        _StartupScenario(
            "demo-stack-only",
            ("--demo-stack-only",),
            True,
            "dev",
            "external-checkpoint",
            ("iknow-memory-dev",),
        ),
        _StartupScenario(
            "demo-service-start",
            (),
            True,
            "dev",
            "external-checkpoint",
            ("iknow-memory-dev",),
        ),
        _StartupScenario(
            "ordinary-production-stack",
            (),
            False,
            None,
            "fin",
            ("fin", "iknow-memory"),
        ),
        _StartupScenario(
            "ordinary-dev-stack",
            (),
            False,
            "dev",
            "fin",
            ("fin", "iknow-memory-dev"),
        ),
    ],
    ids=lambda scenario: scenario.name,
)
def test_dev_up_compose_projects_follow_validated_demo_mode(
    tmp_path: Path,
    scenario: _StartupScenario,
) -> None:
    script, env, call_log = _launcher_harness(
        tmp_path,
        demo_mode=scenario.demo_mode,
        stack=scenario.stack,
        checkpoint_owner=scenario.checkpoint_owner,
    )

    result = subprocess.run(
        ["bash", str(script), *scenario.args],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    calls = call_log.read_text(encoding="utf-8")
    assert result.returncode == 0, f"{scenario.name}: {result.stderr}"
    projects = [
        project
        for project in ("fin", "iknow-memory", "iknow-memory-dev")
        if f"compose -p {project} " in calls
    ]
    assert projects == list(scenario.expected_projects)
    if scenario.demo_mode:
        assert "publish=5433" not in calls
        assert "production.yml" not in calls
    else:
        assert "publish=5433" in calls
