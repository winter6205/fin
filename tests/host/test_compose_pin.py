"""Production volume pin and the dev compose that must not touch it.

``docker compose config`` is read-only. These tests do not start containers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
PROD_COMPOSE = REPO_ROOT / "compose" / "docker-compose.production.yml"
INSTALLER = REPO_ROOT / "scripts" / "install-production-compose.sh"
FORBIDDEN_VOLUMES = {"fin_postgres_data", "fin_postgres-data"}


def _compose_config(path: Path) -> dict:
    if shutil.which("docker") is None:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded
    result = subprocess.run(
        ["docker", "compose", "-f", str(path), "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return loaded


def _published_ports(service: dict) -> list[int]:
    ports: list[int] = []
    for entry in service.get("ports") or []:
        if isinstance(entry, dict):
            ports.append(int(entry["published"]))
            continue
        parts = str(entry).split(":")
        ports.append(int(parts[-2]))
    return ports


def _volume_names(config: dict) -> set[str]:
    names: set[str] = set()
    for key, spec in (config.get("volumes") or {}).items():
        names.add(str(key))
        if isinstance(spec, dict) and spec.get("name"):
            names.add(str(spec["name"]))
    return names


def test_production_compose_pins_the_external_volume() -> None:
    config = _compose_config(PROD_COMPOSE)
    assert config.get("name") == "fin"
    volumes = config.get("volumes") or {}
    assert set(volumes) == {"fin_postgres_data"}
    spec = volumes["fin_postgres_data"]
    assert spec.get("external") is True
    assert spec.get("name") == "fin_postgres_data"
    service = config["services"]["postgres"]
    assert service.get("container_name") == "fin-postgres"
    assert service.get("image") == "postgres:16-alpine"
    assert service.get("restart") == "unless-stopped"
    assert _published_ports(service) == [5433]


def test_dev_compose_does_not_name_the_production_volume() -> None:
    config = _compose_config(DEV_COMPOSE)
    assert config.get("name") == "fin-dev"
    assert _volume_names(config).isdisjoint(FORBIDDEN_VOLUMES)
    spec = (config.get("volumes") or {})["fin_dev_postgres_data"]
    assert spec.get("external") in (None, False)
    assert spec.get("name") == "fin_dev_postgres_data"
    service = config["services"]["postgres"]
    assert service.get("container_name") != "fin-postgres"
    assert 5433 not in _published_ports(service)


def test_dev_down_stops_only_the_dev_project() -> None:
    text = (REPO_ROOT / "scripts" / "dev-down.sh").read_text(encoding="utf-8")
    assert 'docker compose -p fin-dev -f "$REPO_ROOT/docker-compose.yml" down' in text
    assert "fin down stops only the fin-dev project" in text
    assert ".fin/compose" not in text
    assert "-p fin " not in text
    assert "-p fin\n" not in text


def test_linked_worktree_guard_runs_before_compose_up() -> None:
    text = (REPO_ROOT / "scripts" / "dev-up.sh").read_text(encoding="utf-8")
    executed = text.split('if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then', 1)[1]
    guard = executed.index('if [[ -f "$REPO_ROOT/.git" ]]; then')
    call = executed.index("\nstart_compose\n")
    assert guard < call
    assert 'docker compose -p fin -f "$compose_file" up -d --wait' in text
    assert 'docker compose -f "$REPO_ROOT/docker-compose.yml" up' not in text


def test_fin_usage_names_backup_and_restore() -> None:
    text = (REPO_ROOT / "scripts" / "fin").read_text(encoding="utf-8")
    usage = text.split("# usage:begin", 1)[1].split("# usage:end", 1)[0]
    assert "fin backup" in usage
    assert "fin restore" in usage


def test_installer_copies_the_template_and_does_not_run_compose(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALL_LOG\"\nexit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "CALL_LOG": str(log),
    }
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    dest = tmp_path / "home" / ".fin" / "compose" / "docker-compose.production.yml"
    assert dest.read_bytes() == PROD_COMPOSE.read_bytes()
    assert not log.exists() or log.read_text(encoding="utf-8") == ""
