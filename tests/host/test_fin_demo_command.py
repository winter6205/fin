"""Command-contract tests for the local demo launcher.

These tests run the public ``fin demo`` entrypoint and the pre-commit hook in
temporary checkouts. No Docker daemon, database, user home, or credential is
used.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIN = REPO_ROOT / "scripts" / "fin"


def _demo_checkout(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Create a credential-free CLI harness around the real demo entrypoint."""
    repo = tmp_path / "fin"
    for relative in (
        "scripts/lib",
        "src/fin/host",
        "scripts",
        ".venv/bin",
        "tests/retrieve/fixtures/ledger",
    ):
        (repo / relative).mkdir(parents=True, exist_ok=True)
    for relative in (
        "scripts/fin",
        "scripts/demo.sh",
        "scripts/demo_checkpoint_preflight.py",
        "scripts/lib/readiness.sh",
        "scripts/lib/fin-root.sh",
        "scripts/lib/demo-ledger.sh",
    ):
        shutil.copy(REPO_ROOT / relative, repo / relative)
    for relative in (
        "tests/retrieve/fixtures/ledger/001_schema.sql",
        "tests/retrieve/fixtures/ledger/002_seed.sql",
    ):
        shutil.copy(REPO_ROOT / relative, repo / relative)
    (repo / "src/fin/host/http.py").touch()

    fake_python = repo / ".venv/bin/python"
    fake_python.write_text(
        """#!/usr/bin/env bash
if [[ "$*" == *resolve_fin_home* ]]; then
  printf '%s\\n' "$FAKE_FIN_HOME"
  exit 0
fi
if [[ "${1:-}" == *demo_checkpoint_preflight.py ]]; then
  printf 'python script %s\\n' "${1##*/}" >>"$TRACE_FILE"
  export PYTHONPATH="$FAKE_FIN_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
  exec "$FAKE_REAL_PYTHON" "$@"
fi
printf '%s\\n' "$FAKE_FIN_HOME"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    trace = tmp_path / "commands.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tools = {
        "docker": """#!/usr/bin/env bash
printf 'docker %s\\n' \"$*\" >>\"$TRACE_FILE\"
case \"${1:-}\" in
  info) exit 0 ;;
  ps)
    if [[ \"$*\" == *-aq* ]]; then printf 'synthetic-container-id\\n'; fi
    exit 0
    ;;
  inspect) printf '%s\\n' \"${FAKE_LEDGER_LABEL:-fin-demo-business-ledger-v1}\"; exit 0 ;;
  run) printf 'synthetic-container-id\\n'; exit 0 ;;
  port) printf '%s\\n' \"${FAKE_LEDGER_BINDING:-127.0.0.1:55432}\"; exit 0 ;;
  exec)
    if [[ \"$*\" == *pg_isready* ]]; then exit 0; fi
    if [[ \"$*\" == *\"SELECT current_database()\"* ]]; then
      printf 'fin_business_demo_test\\n'
      exit 0
    fi
    if [[ \"$*\" == *\"SHOW default_transaction_read_only\"* ]]; then
      printf 'on\\n'
      exit 0
    fi
    if [[ \"$*\" == *\"-f -\"* ]]; then
      cat >>\"$TRACE_FILE\"
      [[ \"${FAKE_LEDGER_FAIL_INIT:-0}\" != 1 ]]
      exit $?
    fi
    exit 0
    ;;
  rm) exit 0 ;;
  *) exit 0 ;;
esac
""",
        "uv": """#!/usr/bin/env bash
printf 'uv %s\\n' \"$*\" >>\"$TRACE_FILE\"
if [[ \"${1:-}\" == run && \"${2:-}\" == fin-serve ]]; then
  if [[ -n \"${FIN_BUSINESS_LEDGER_URL:-}\" ]]; then
    printf 'demo serve received business ledger\\n' >>\"$TRACE_FILE\"
  else
    printf 'demo serve missing business ledger\\n' >>\"$TRACE_FILE\"
  fi
fi
if [[ \"$*\" == *seed_demo.py* && -n \"${GRAPHRAG_MEMORY_DB_URL:-}\" ]]; then
  printf 'seed received database setting\\n' >>\"$TRACE_FILE\"
fi
if [[ \"$*\" == *seed_demo.py* && -n \"${FIN_BUSINESS_LEDGER_URL:-}\" ]]; then
  printf 'seed received business ledger setting\\n' >>\"$TRACE_FILE\"
fi
if [[ -n \"${GRAPHRAG_MEMORY_ES_URL:-}\" && -n \"${GRAPHRAG_MEMORY_NEO4J_URI:-}\" && -n \"${FIN_DEMO_EMBED_DIMENSIONS:-}\" && -n \"${FIN_DEMO_EMBED_GENERATION:-}\" ]]; then
  printf 'seed runtime environment ready\\n' >>\"$TRACE_FILE\"
fi
exit 0
""",
        "curl": """#!/usr/bin/env bash
sleep 0.05
printf '%s\\n' \"{\\\"knowledge_mcp\\\":\\\"${FAKE_KNOWLEDGE_MCP:-reachable}\\\",\\\"retrieve_adapter_kind\\\":\\\"mcp\\\",\\\"bound_collection_name\\\":\\\"demo\\\",\\\"model_identity\\\":\\\"fixture\\\"}\"
exit 0
""",
        "wslview": """#!/usr/bin/env bash
printf 'wslview %s\\n' \"$*\" >>\"$TRACE_FILE\"
exit 0
""",
        "xdg-open": """#!/usr/bin/env bash
printf 'xdg-open %s\\n' \"$*\" >>\"$TRACE_FILE\"
exit 1
""",
        "node": """#!/usr/bin/env bash
printf 'node setting=%s home=%s\\n' \"${FIN_DEMO_SETTING_KEY:-}\" \"$IKNOW_MEMORY_HOME\" >>\"$TRACE_FILE\"
if [[ -n \"${FAKE_SETTING_VALUE:-}\" ]]; then printf '%s' \"$FAKE_SETTING_VALUE\"; exit 0; fi
exit 3
""",
    }
    for name, body in fake_tools.items():
        executable = bin_dir / name
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    for name in (
        "bash",
        "dirname",
        "env",
        "flock",
        "id",
        "mkdir",
        "nohup",
        "sleep",
    ):
        executable = shutil.which(name)
        assert executable is not None, f"required executable unavailable: {name}"
        (bin_dir / name).symlink_to(executable)
    jq = shutil.which("jq")
    assert jq is not None, "jq is required to exercise the readiness classifier"
    (bin_dir / "jq").symlink_to(jq)

    iknow = tmp_path / "iknow-memory"
    (iknow / "bin").mkdir(parents=True)
    (iknow / "package.json").write_text("{}\n", encoding="utf-8")
    (iknow / "bin/iknow-memory.js").touch()

    (repo / "scripts/dev-up.sh").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'dev-up %s mode=%s stack=%s home=%s settings=%s dimensions=%s generation=%s ledger=%s\\n' \"$*\" \"${FIN_DEMO_MODE:-}\" \"${FIN_IKNOW_STACK:-}\" \"${IKNOW_MEMORY_HOME:-}\" \"${FIN_SETTINGS_FILE:-}\" \"${FIN_DEMO_EMBED_DIMENSIONS:-}\" \"${FIN_DEMO_EMBED_GENERATION:-}\" \"${FIN_BUSINESS_LEDGER_URL:+set}\" >>\"$TRACE_FILE\"
if [[ \"$#\" -eq 0 ]]; then
  if [[ \"${FAKE_NO_SERVE_PID:-0}\" == 1 ]]; then exit 0; fi
  uv run fin-serve
  mkdir -p \"$FIN_HOME/run\"
  printf '%s\\n' \"$$\" >\"$FIN_HOME/run/serve.pid\"
  exec sleep 60
fi
exit 0
""",
        encoding="utf-8",
    )
    (repo / "scripts/dev-down.sh").write_text(
        """#!/usr/bin/env bash
printf 'dev-down %s home=%s\\n' \"$*\" \"${IKNOW_MEMORY_HOME:-}\" >>\"$TRACE_FILE\"
exit 0
""",
        encoding="utf-8",
    )
    (repo / "scripts/dev-up.sh").chmod(0o755)
    (repo / "scripts/dev-down.sh").chmod(0o755)

    fin_home = tmp_path / "operator-home"
    fin_home.mkdir()
    (fin_home / "settings.yaml").write_text(
        "checkpoint:\n  url: fixture-checkpoint-url\n",
        encoding="utf-8",
    )
    isolated_home = fin_home / "demo/iknow-memory"
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TRACE_FILE": str(trace),
        "FAKE_FIN_HOME": str(fin_home),
        "FAKE_REAL_PYTHON": sys.executable,
        "FAKE_FIN_SOURCE": str(REPO_ROOT / "src"),
        "FIN_IKNOW_ROOT": str(iknow),
        "IKNOW_MEMORY_HOME": str(isolated_home),
        "TMPDIR": str(temp_dir),
    }
    for name in (
        "FIN_HOME",
        "FIN_SETTINGS_FILE",
        "FIN_DEMO_MODE",
        "FIN_IKNOW_STACK",
        "FIN_DEMO_EMBED_DIMENSIONS",
        "FIN_DEMO_EMBED_GENERATION",
        "FIN_CHECKPOINT_URL",
        "GRAPHRAG_MEMORY_DB_URL",
        "GRAPHRAG_MEMORY_ES_URL",
        "GRAPHRAG_MEMORY_NEO4J_URI",
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS",
        "GRAPHRAG_MEMORY_EMBED_MODEL",
    ):
        env.pop(name, None)
    return repo, env, trace


def _kill_demo_serve(env: dict[str, str]) -> None:
    pid_file = Path(env["FAKE_FIN_HOME"]) / "demo/run/serve.pid"
    if not pid_file.is_file():
        return
    try:
        os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass


def test_demo_rejects_arguments_with_exit_two() -> None:
    result = subprocess.run(
        ["bash", str(FIN), "demo", "unexpected"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "fin demo accepts only --stop" in result.stderr


def test_demo_starts_dev_stack_seeds_and_requires_reachable_mcp(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_SETTING_VALUE": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
        }
    )

    try:
        result = subprocess.run(
            ["bash", str(repo / "scripts/fin"), "demo"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        _kill_demo_serve(env)

    commands = trace.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "knowledge_mcp=reachable" in result.stdout
    assert "http://127.0.0.1:8000" in result.stdout
    assert "docker ps -q --filter label=com.docker.compose.project=fin-bench" in commands
    assert "dev-up --demo-stack-only mode=1 stack=dev" in commands
    assert f"settings={env['FAKE_FIN_HOME']}/settings.yaml" in commands
    assert "uv run --no-sync python" in commands
    assert "--embed-dimensions 768 --embed-generation fixture-embedding-model" in commands
    assert "seed runtime environment ready" in commands
    assert "dimensions=768 generation=fixture-embedding-model" in commands
    assert "node setting=GRAPHRAG_MEMORY_DB_URL" in commands
    assert f"home={env['IKNOW_MEMORY_HOME']}" in commands
    assert "seed received database setting" in commands
    assert "dev-up  mode=1 stack=dev" in commands
    assert "xdg-open http://127.0.0.1:8000" in commands
    assert "wslview http://127.0.0.1:8000" in commands
    assert "fixture-memory-db" not in result.stdout


def test_demo_serves_with_a_disposable_read_only_sql_ledger(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_SETTING_VALUE": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
            "FIN_DEMO_SKIP_BROWSER": "1",
            "FIN_DEMO_SUPPRESS_START_LOG": "1",
        }
    )

    try:
        result = subprocess.run(
            ["bash", str(repo / "scripts/fin"), "demo"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        _kill_demo_serve(env)

    commands = trace.read_text(encoding="utf-8")
    ledger_name = f"fin-demo-business-ledger-{os.getuid()}"
    assert result.returncode == 0, result.stderr
    assert f"docker run --rm --detach --publish 127.0.0.1::5432" in commands
    assert f"--name {ledger_name} postgres:16-alpine" in commands
    assert "POSTGRES_DB=fin_business_demo_test" in commands
    assert "com.fin.demo.ledger=fin-demo-business-ledger-v1" in commands
    assert "CREATE ROLE fin_sql_reader LOGIN" in commands
    assert "GRANT SELECT ON ledger_ro.contract_expiry TO fin_sql_reader" in commands
    assert "ALTER ROLE fin_sql_reader SET default_transaction_read_only = on" in commands
    assert "docker port " in commands
    assert "demo serve received business ledger" in commands
    assert "dev-up  mode=1 stack=dev" in commands
    assert "generation=fixture-embedding-model ledger=\n" in commands
    assert "seed received business ledger setting" not in commands
    assert "docker rm --force " + ledger_name in commands
    assert "postgresql://" not in commands
    assert "postgresql://" not in result.stdout
    assert "postgresql://" not in result.stderr


def test_demo_stop_removes_only_its_owned_sql_ledger(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo", "--stop"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    ledger_name = f"fin-demo-business-ledger-{os.getuid()}"
    commands = trace.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert f"docker inspect --format" in commands
    assert f"docker rm --force {ledger_name}" in commands


def test_demo_rejects_a_non_loopback_sql_ledger_mapping_and_cleans_up(
    tmp_path: Path,
) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_SETTING_VALUE": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
            "FAKE_LEDGER_BINDING": "0.0.0.0:55432",
            "FIN_DEMO_SKIP_BROWSER": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    commands = trace.read_text(encoding="utf-8")
    ledger_name = f"fin-demo-business-ledger-{os.getuid()}"
    startup_log = Path(env["FAKE_FIN_HOME"]) / "demo/run/demo-start.log"
    assert result.returncode != 0
    assert "fin-serve exited before readiness" in result.stderr
    assert "not bound to a valid loopback port" in startup_log.read_text(encoding="utf-8")
    assert "available at" not in result.stdout
    assert f"docker rm --force {ledger_name}" in commands
    assert "postgresql://" not in commands
    assert "postgresql://" not in result.stdout
    assert "postgresql://" not in result.stderr


def test_demo_refuses_to_remove_an_unowned_container_with_the_ledger_name(
    tmp_path: Path,
) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_SETTING_VALUE": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
            "FAKE_LEDGER_LABEL": "another-owner",
            "FIN_DEMO_SKIP_BROWSER": "1",
            "FIN_DEMO_SUPPRESS_START_LOG": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    commands = trace.read_text(encoding="utf-8")
    assert result.returncode != 0
    assert "without the demo ledger ownership label" in result.stderr
    assert "docker run " not in commands
    assert "docker rm --force " not in commands
    assert "dev-down --demo" in commands


def test_demo_startup_failure_removes_the_owned_sql_ledger(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_SETTING_VALUE": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
            "FAKE_LEDGER_FAIL_INIT": "1",
            "FIN_DEMO_SKIP_BROWSER": "1",
            "FIN_DEMO_SUPPRESS_START_LOG": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    commands = trace.read_text(encoding="utf-8")
    ledger_name = f"fin-demo-business-ledger-{os.getuid()}"
    assert result.returncode != 0
    assert "could not load the synthetic SQL ledger schema" in result.stderr
    assert "available at" not in result.stdout
    assert f"docker rm --force {ledger_name}" in commands
    assert "dev-down --demo" in commands


def test_demo_acceptance_skip_browser_keeps_ready_service_without_opening_desktop(
    tmp_path: Path,
) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_SETTING_VALUE": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
            "FIN_DEMO_SKIP_BROWSER": "1",
            "FIN_DEMO_SUPPRESS_START_LOG": "1",
        }
    )

    try:
        result = subprocess.run(
            ["bash", str(repo / "scripts/fin"), "demo"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        _kill_demo_serve(env)

    commands = trace.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "http://127.0.0.1:8000" in result.stdout
    assert "browser opening skipped" in result.stdout
    assert "xdg-open http://127.0.0.1:8000" not in commands
    assert "wslview http://127.0.0.1:8000" not in commands
    assert not (Path(env["FAKE_FIN_HOME"]) / "demo/run/demo-start.log").exists()


def test_demo_does_not_report_available_when_knowledge_mcp_is_gated(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_KNOWLEDGE_MCP": "fault",
            "GRAPHRAG_MEMORY_DB_URL": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
        }
    )

    try:
        result = subprocess.run(
            ["bash", str(repo / "scripts/fin"), "demo"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        _kill_demo_serve(env)

    assert result.returncode != 0
    assert "GATED — knowledge_mcp=fault" in result.stderr
    assert "available at" not in result.stdout
    assert "dev-down --demo" in trace.read_text(encoding="utf-8")


def test_demo_does_not_accept_an_unowned_server_on_the_readiness_port(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "FAKE_NO_SERVE_PID": "1",
            "GRAPHRAG_MEMORY_DB_URL": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "fin-serve exited before readiness" in result.stderr
    assert "available at" not in result.stdout
    assert "dev-down --demo" in trace.read_text(encoding="utf-8")


def test_demo_stop_is_idempotent_without_iknow_checkout_or_docker(tmp_path: Path) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env["FIN_IKNOW_ROOT"] = str(tmp_path / "missing-iknow")
    (trace.parent / "bin/jq").unlink()
    (trace.parent / "bin/docker").unlink()
    env["PATH"] = str(trace.parent / "bin")

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo", "--stop"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    commands = trace.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "stopped" in result.stdout
    assert "dev-down --demo" in commands
    assert "docker " not in commands
    assert "fin-bench" not in commands
    assert not (Path(env["FAKE_FIN_HOME"]) / "demo").exists()


@pytest.mark.parametrize(
    ("missing_env", "missing_setting"),
    [
        ("GRAPHRAG_MEMORY_DB_URL", "GRAPHRAG_MEMORY_DB_URL"),
        ("GRAPHRAG_MEMORY_ES_URL", "GRAPHRAG_MEMORY_ES_URL"),
        ("GRAPHRAG_MEMORY_NEO4J_URI", "GRAPHRAG_MEMORY_NEO4J_URI"),
        ("FIN_DEMO_EMBED_DIMENSIONS", "GRAPHRAG_MEMORY_EMBED_DIMENSIONS"),
        ("FIN_DEMO_EMBED_GENERATION", "GRAPHRAG_MEMORY_EMBED_MODEL"),
    ],
)
def test_demo_rejects_missing_memory_settings_before_service_changes(
    tmp_path: Path, missing_env: str, missing_setting: str
) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    env.update(
        {
            "GRAPHRAG_MEMORY_DB_URL": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "fixture-elastic-url",
            "GRAPHRAG_MEMORY_NEO4J_URI": "fixture-neo4j-uri",
            "FIN_DEMO_EMBED_DIMENSIONS": "768",
            "FIN_DEMO_EMBED_GENERATION": "fixture-embedding-model",
        }
    )
    env.pop(missing_env)

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    commands = trace.read_text(encoding="utf-8")
    assert result.returncode != 0
    assert f"missing {missing_setting}" in result.stderr
    assert "docker " not in commands
    assert "dev-down " not in commands
    assert "dev-up " not in commands
    assert "uv " not in commands
    assert not (Path(env["FAKE_FIN_HOME"]) / "demo").exists()


def test_demo_rejects_missing_checkpoint_before_stopping_or_starting_services(
    tmp_path: Path,
) -> None:
    repo, env, trace = _demo_checkout(tmp_path)
    (Path(env["FAKE_FIN_HOME"]) / "settings.yaml").write_text(
        "llm:\n  model: fixture-model\n",
        encoding="utf-8",
    )
    env.update(
        {
            "GRAPHRAG_MEMORY_DB_URL": "fixture-memory-db",
            "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
            "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": "768",
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fixture-embedding-model",
            "FIN_DEMO_SUPPRESS_START_LOG": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "scripts/fin"), "demo"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    commands = trace.read_text(encoding="utf-8") if trace.exists() else ""
    assert result.returncode != 0
    assert "missing FIN_CHECKPOINT_URL" in result.stderr
    assert "python script demo_checkpoint_preflight.py" in commands
    assert "docker " not in commands
    assert "dev-down " not in commands
    assert "dev-up " not in commands
    assert "uv " not in commands
    assert not (Path(env["FAKE_FIN_HOME"]) / "demo").exists()


def test_pre_commit_runs_without_npm_on_path(tmp_path: Path) -> None:
    """The hook invokes the staged checks directly, so WSL need not resolve npm."""
    repo = tmp_path / "repo"
    (repo / ".husky").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy(REPO_ROOT / ".husky" / "pre-commit", repo / ".husky" / "pre-commit")
    shutil.copy(REPO_ROOT / "scripts" / "lint-s5.sh", repo / "scripts" / "lint-s5.sh")
    shutil.copy(
        REPO_ROOT / "scripts" / "pyright-staged.mjs",
        repo / "scripts" / "pyright-staged.mjs",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sh", "node", "git"):
        executable = shutil.which(name)
        assert executable is not None, f"required executable unavailable: {name}"
        (bin_dir / name).symlink_to(executable)
    # Deliberately omit npm while keeping only the tools the hook needs.
    env = {**os.environ, "PATH": str(bin_dir), "S5_CHECK": ""}
    result = subprocess.run(
        ["sh", ".husky/pre-commit"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "nothing staged" in result.stderr
