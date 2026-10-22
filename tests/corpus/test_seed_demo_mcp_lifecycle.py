"""Regression coverage for the seed CLI's shared MCP process lifecycle."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

from fin.corpus import demo_seed
from fin.corpus.demo_seed import DemoWorkerUnavailableError, EmbedGeneration
from fin.host.compile import DEFAULT_MCP_COMMAND
from fin.retrieve.push_port import PushProtocolError

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
_SEED_SPEC = importlib.util.spec_from_file_location("seed_demo_cli", _SEED_SCRIPT)
assert _SEED_SPEC is not None and _SEED_SPEC.loader is not None
seed_demo = importlib.util.module_from_spec(_SEED_SPEC)
_SEED_SPEC.loader.exec_module(seed_demo)


_LINGERING_MCP_SERVER = r'''\
import json
import os
import sys
import time

from pathlib import Path

pid_path = Path(os.environ["MCP_CHILD_PID_FILE"])
pid_path.write_text(pid_path.read_text() + str(os.getpid()) + "\n" if pid_path.exists() else str(os.getpid()) + "\n")
calls_path = Path(os.environ["MCP_CHILD_CALLS_FILE"])
pushed = False
polled = False

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        response = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        name = request["params"]["name"]
        arguments = request["params"]["arguments"]
        with calls_path.open("a", encoding="utf-8") as stream:
            stream.write(name + "\n")
        if name == "submit_index_job" and not pushed:
            body = {"error": "index_job_unknown_source", "message": "synthetic"}
            result = {"isError": True, "content": [{"type": "text", "text": json.dumps(body)}]}
        elif name == "push":
            items = arguments["items"]
            if os.environ.get("MCP_DEMO_FAIL_PUSH") == "1":
                body = {"chunk_ids": []}
            else:
                body = {
                    "chunk_ids": [f"chunk-{index}" for index in range(sum(len(item["chunks"]) for item in items))],
                    "content_identities": ["server-identity" for _item in items],
                }
            pushed = True
            result = {"content": [{"type": "text", "text": json.dumps(body)}]}
        elif name == "submit_index_job":
            result = {
                "content": [{"type": "text", "text": json.dumps({
                    "job_id": "demo-job",
                    "source_ref": arguments["source_ref"],
                    "content_identity": arguments["content_identity"],
                    "status": "in_progress",
                    "reused": False,
                })}]
            }
        elif name == "poll_index_job":
            if not polled:
                # A real stack cannot answer the first poll before the worker has
                # claimed the job; without the wait a test would race the worker's
                # own startup rather than exercise its teardown.
                polled = True
                if os.environ.get("MCP_DEMO_SLOW_FIRST_POLL") == "1":
                    time.sleep(float(os.environ["MCP_DEMO_POLL_DELAY_SECONDS"]))
            result = {
                "content": [{"type": "text", "text": json.dumps({
                    "job_id": arguments["job_id"],
                    "source_ref": "demo-document",
                    "content_identity": "demo-identity",
                    "status": "ready",
                    "community_index_state": {"status": "ready", "failure_code": None},
                })}]
            }
        else:
            raise RuntimeError("unexpected tool")
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    else:
        raise RuntimeError("unexpected method")
    print(json.dumps(response), flush=True)

# A one-shot communicate() caller waits for process exit after closing stdin.
# Keep the process alive to reproduce that timeout; MCPStdioSession owns and
# terminates this process after the seed chain finishes.
while True:
    time.sleep(10)
'''


class _PlanA:
    def apply_schema(self) -> None:
        pass


class _EmbedProbe:
    def read_embedding_generation(self) -> EmbedGeneration:
        return EmbedGeneration(dimensions=1536, generation="demo-generation")


class _Worker:
    pid = 123

    def start(self, *, memory_home: str) -> int:
        return self.pid

    def terminate_and_wait(self, *, timeout_seconds: float, open_jobs: int = 0) -> int:
        return 0


class _SignallingWorker(_Worker):
    """A worker that SIGTERMs the seed process as it starts.

    Self-signal from inside ``start`` is the deterministic form of the case: the
    handler the CLI installed is on the stack at that point, so the chain's
    ``finally`` is what has to run.
    """

    def __init__(self) -> None:
        self.terminated: list[int] = []

    def start(self, *, memory_home: str) -> int:
        os.kill(os.getpid(), signal.SIGTERM)
        return self.pid

    def terminate_and_wait(self, *, timeout_seconds: float, open_jobs: int = 0) -> int:
        self.terminated.append(open_jobs)
        return 0


def _fixture_dir(root: Path) -> Path:
    fixture_dir = root / "fixture"
    fixture_dir.mkdir()
    # The chunk carries the fixture's page marker, the way the shipped corpus
    # does: the seed reads the page from it and refuses a chunk whose heading
    # and ordinal disagree.
    page = "Demo document · Page 1\nA synthetic demo paragraph."
    document = {
        "source_ref": "demo-document",
        "title": "Demo document",
        "content": page,
        "chunks": [{"index": 0, "content": page}],
        "metadata": {
            "document_type": "handbook",
            "page_count": 1,
            "form": "10-K",
        },
        "valid_from": "2026-01-01T00:00:00.000Z",
    }
    (fixture_dir / "demo-document.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return fixture_dir


def _configure_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_push: bool = False,
    worker: _Worker | None = None,
) -> tuple[list[str], Path]:
    server_path = tmp_path / "lingering_mcp_server.py"
    server_path.write_text(_LINGERING_MCP_SERVER, encoding="utf-8")
    pid_path = tmp_path / "mcp-child-pids.txt"
    calls_path = tmp_path / "mcp-calls.txt"
    monkeypatch.setenv("FIN_MCP_COMMAND", f"{sys.executable} {server_path}")
    monkeypatch.setenv("FIN_MCP_TIMEOUT_SECONDS", "0.4")
    monkeypatch.setenv("IKNOW_MEMORY_HOME", str(tmp_path / "isolated-memory"))
    monkeypatch.setenv("MCP_CHILD_PID_FILE", str(pid_path))
    monkeypatch.setenv("MCP_CHILD_CALLS_FILE", str(calls_path))
    monkeypatch.delenv("MCP_DEMO_FAIL_PUSH", raising=False)
    if fail_push:
        monkeypatch.setenv("MCP_DEMO_FAIL_PUSH", "1")

    monkeypatch.setattr(
        demo_seed,
        "guard_dev_stack",
        lambda environ, *, user_home=None: environ["IKNOW_MEMORY_HOME"],
    )
    monkeypatch.setattr(
        seed_demo,
        "PlanADevStackAdapter",
        lambda *_args, **_kwargs: _PlanA(),
    )
    monkeypatch.setattr(
        seed_demo,
        "PostgresEmbedProbe",
        lambda _connect: _EmbedProbe(),
    )
    the_worker = worker if worker is not None else _Worker()
    monkeypatch.setattr(
        seed_demo,
        "_worker_factory",
        lambda *_args, **_kwargs: (lambda: the_worker),
    )
    argv = [
        "--iknow-root",
        str(tmp_path),
        "--fixture-dir",
        str(_fixture_dir(tmp_path)),
        "--catalog-path",
        str(tmp_path / "catalog.json"),
        "--embed-dimensions",
        "1536",
        "--embed-generation",
        "demo-generation",
        "--yes",
        "--poll-timeout-seconds",
        "1",
        "--poll-interval-seconds",
        "0",
    ]
    return argv, pid_path


def _assert_child_stopped(pid_path: Path) -> None:
    pids = [int(value) for value in pid_path.read_text().splitlines()]
    assert len(pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_mcp_command_keeps_cli_environment_and_default_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_MCP_COMMAND", "environment-command --environment")

    assert seed_demo._mcp_command("cli-command --cli") == [
        "cli-command",
        "--cli",
    ]
    assert seed_demo._mcp_command(None) == [
        "environment-command",
        "--environment",
    ]

    monkeypatch.delenv("FIN_MCP_COMMAND")
    assert seed_demo._mcp_command(None) == list(DEFAULT_MCP_COMMAND)


def test_worker_factory_resolves_npm_to_an_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_path = tmp_path / "bin" / "npm"
    npm_path.parent.mkdir()
    npm_path.touch()
    monkeypatch.setattr(shutil, "which", lambda _program: str(npm_path))

    worker = seed_demo._worker_factory(tmp_path, {})()

    assert worker._command[0] == str(npm_path.resolve())
    assert Path(worker._command[0]).is_absolute()


def test_worker_factory_names_missing_npm_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _program: None)

    with pytest.raises(DemoWorkerUnavailableError, match="npm"):
        seed_demo._worker_factory(tmp_path, {})()


def test_seed_cli_shares_one_mcp_child_and_closes_it_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, pid_path = _configure_main(tmp_path, monkeypatch)

    assert seed_demo.main(argv) == 0

    assert (tmp_path / "mcp-calls.txt").read_text().splitlines() == [
        "submit_index_job",
        "push",
        "submit_index_job",
        "poll_index_job",
    ]
    _assert_child_stopped(pid_path)


def test_seed_cli_closes_mcp_child_when_seed_fails_after_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, pid_path = _configure_main(tmp_path, monkeypatch, fail_push=True)

    with pytest.raises(PushProtocolError):
        seed_demo.main(argv)

    _assert_child_stopped(pid_path)


def test_seed_cli_sigterm_runs_the_worker_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ``finally`` covers an exception and Ctrl-C but not SIGTERM, whose default
    # disposition ends the process at the signal. Without the handler the worker
    # teardown never runs and the npm-spawned worker outlives the seed.
    worker = _SignallingWorker()
    argv, pid_path = _configure_main(tmp_path, monkeypatch, worker=worker)
    previous = signal.getsignal(signal.SIGTERM)

    with pytest.raises(SystemExit) as raised:
        seed_demo.main(argv)

    assert raised.value.code == 128 + signal.SIGTERM
    assert worker.terminated == [0], "the teardown did not run on SIGTERM"
    _assert_child_stopped(pid_path)
    # The handler is scoped to the run: a library caller keeps SIGTERM's default.
    assert signal.getsignal(signal.SIGTERM) is previous


def _stderr_handler_records(line: str) -> str:
    """Format ``line`` with the handler a fresh ``seed:demo`` would install.

    pytest's logging plugin keeps a root handler installed, which makes
    ``basicConfig`` a no-op inside the test process; the handler the CLI really
    creates is reproduced here, off the same arguments, because "the INFO line
    reaches the terminal" is a property of the CLI's logging configuration and
    not of what pytest happens to leave in the root logger.
    """
    stderr = io.StringIO()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=stderr, force=True)
    logging.getLogger("fin.corpus.demo_seed").info(line)
    return stderr.getvalue().strip()


def test_seed_cli_configures_logging_so_the_teardown_line_is_visible() -> None:
    # Without this, demo_seed's INFO teardown line is dropped by the root
    # handler's default WARNING level and the operator never sees which group
    # the run ended.
    stderr = io.StringIO()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=stderr, force=True)

    assert logging.getLogger("fin.corpus.demo_seed").isEnabledFor(logging.INFO)
    assert logging.getLogger("fin.corpus.demo_seed").handlers == []

    logging.getLogger("fin.corpus.demo_seed").info("worker teardown survivors=[]")
    assert "worker teardown survivors=[]" in stderr.getvalue()


def test_seed_cli_reaps_a_child_spawning_worker_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The whole point of the fix, end to end through ``main``: a worker that
    # spawns a child, as npm does, is gone by the time main returns, and the
    # teardown line names the group it ended.
    marker = tmp_path / "grandchild.pid"
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to model the npm -> shell -> worker chain"
    real_worker = demo_seed.SubprocessWorker(
        # An absolute path, because the worker refuses a bare program name.
        [bash, "-c", f"sleep 120 & echo $! > '{marker}'; wait"],
        cwd=tmp_path,
        environ=dict(os.environ),
    )
    monkeypatch.setattr(seed_demo, "SubprocessWorker", lambda *a, **k: real_worker)
    monkeypatch.setenv("MCP_DEMO_SLOW_FIRST_POLL", "1")
    monkeypatch.setenv("MCP_DEMO_POLL_DELAY_SECONDS", "0.25")
    argv, _pid_path = _configure_main(tmp_path, monkeypatch, worker=real_worker)

    assert seed_demo.main(argv) == 0

    line = " ".join(
        record.getMessage()
        for record in caplog.records
        if "worker teardown" in record.getMessage()
    )
    assert f"worker_pgid={real_worker.pid}" in line
    # The stub job reached ready, so nothing was open at teardown.
    assert "open_jobs=0" in line
    assert "survivors=[]" in line
    assert _stderr_handler_records(line) == line, (
        "the teardown line must survive the CLI's own log format unchanged"
    )
    grandchild = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5.0
    while os.path.exists(f"/proc/{grandchild}") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not os.path.exists(f"/proc/{grandchild}")
