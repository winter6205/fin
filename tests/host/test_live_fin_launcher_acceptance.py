"""Live acceptance for the `fin` one-key start — the operator's own path.

Why this file exists
--------------------
The `fin` launcher's own contract test (`test_fin_launcher.py`) drives a
STUB `/api/status`. That is the right shape for pinning the three-state
classification, and it is why the classification can be tested at all
without Docker. But it leaves the acceptance claim — "`fin` brings the
stack up and the knowledge MCP actually became reachable" — unobserved:
a stub cannot fail to reach iknow-memory, so a green stub suite says
nothing about whether the operator's `fin` reaches READABLE on a real
machine. That gap is exactly where the feature's original bug lived
("fin 起来了、记忆服务没起", surfacing as a 409 on the first question).

So this module boots the REAL stack and asserts the operator-visible
end state. It is marked `mcp` and therefore deselected by default (see
`tests/host/conftest.py`): it needs Docker, a sibling iknow-memory
checkout, and a few minutes. Run it deliberately:

    uv run pytest -q -m mcp tests/host/test_live_fin_launcher_acceptance.py

What it proves, and what it does not
------------------------------------
Proven: `fin up` reaches `knowledge_mcp=reachable` on a warm machine,
`fin status` agrees with the boot reporter's verdict, `fin logs` has a
producer, the verdict file records the code, and — the assertion the
whole feature exists for — conversation entry is NOT refused with
`knowledge_mcp_fault` afterwards.

NOT proven here: the cold-boot `fault` race — the boot probe's 2s budget
vs a cold handshake ≥~2s. A cold boot legitimately reports
`fault` and the documented remedy is a warm restart; asserting
`reachable` on a cold machine would encode a race as a contract. This
module waits for the infra to be healthy before it serves, which is the
warm case.

Reach limitation (stated, not faked): the boot verdict is read from
`GET /api/status` and the verdict file, not from a browser. The SPA
render of the same payload is covered by `web/src/knowledgeMcp.test.ts`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIN = REPO_ROOT / "scripts" / "fin"
IKNOW_ROOT = Path(
    os.environ.get("FIN_IKNOW_ROOT", str(Path.home() / "projects" / "iknow-memory"))
)
SERVE_URL = "http://127.0.0.1:8000"
STATUS_URL = f"{SERVE_URL}/api/status"
BOOT_TIMEOUT_SECONDS = int(os.environ.get("FIN_LIVE_BOOT_TIMEOUT", "300"))

pytestmark = pytest.mark.mcp


def _require_live_toolchain() -> None:
    """Inverse of a skip guard: a MISSING prerequisite must fail loudly.

    These tests are opt-in, so their preconditions are the operator's
    deliberate statement that the live stack is wanted. A silent skip
    here would let `-m mcp` exit 0 while proving nothing, which is the
    false-green this module was written to prevent.
    """
    missing = [tool for tool in ("docker", "node", "uv", "jq") if not shutil.which(tool)]
    assert not missing, f"live acceptance needs {missing} on PATH"
    assert IKNOW_ROOT.is_dir(), f"iknow-memory checkout missing at {IKNOW_ROOT}"
    assert (IKNOW_ROOT / "bin" / "iknow-memory.js").is_file(), (
        f"{IKNOW_ROOT} is not an iknow-memory checkout"
    )


def _get_json(url: str, *, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _serve_is_up() -> bool:
    return _get_json(STATUS_URL) is not None


def _fin(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FIN), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=BOOT_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture(scope="module")
def live_stack() -> Iterator[None]:
    """Boot the real stack through `fin up`, then stop it.

    Uses the operator's own entrypoint rather than a reimplementation, so
    this exercises the same code path the operator runs. `fin up` ends in
    an `exec` of fin-serve, so it is started detached and then polled —
    its exit code belongs to fin-serve, not to the boot verdict; the
    verdict is read from `/api/status` and `.fin/ready`.
    """
    _require_live_toolchain()
    if _serve_is_up():
        pytest.skip(
            "a fin-serve already answers on :8000; this module manages the "
            "stack itself (run `fin down` first to exercise the boot)"
        )

    up = subprocess.Popen(
        ["bash", str(FIN), "up"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _serve_is_up():
                break
            if up.poll() is not None:
                output = up.stdout.read() if up.stdout else ""
                raise AssertionError(f"`fin up` exited {up.returncode} before serving:\n{output}")
            time.sleep(2)
        else:
            raise AssertionError(f"fin-serve did not answer on {STATUS_URL} within {BOOT_TIMEOUT_SECONDS}s")
        yield
    finally:
        subprocess.run(
            ["bash", str(FIN), "down"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=BOOT_TIMEOUT_SECONDS,
            check=False,
        )
        if up.poll() is None:
            up.terminate()
            try:
                up.wait(timeout=30)
            except subprocess.TimeoutExpired:
                up.kill()


def test_up_reaches_a_reachable_knowledge_mcp(live_stack: None) -> None:
    """The acceptance claim: after `fin`, the memory service is really there.

    A cold machine may legitimately report `fault` (R1 race) and the
    documented remedy is `fin down && fin up`. This test therefore does
    the warm restart itself rather than encoding a race as a contract —
    and it fails if the warm restart STILL cannot reach the MCP, which is
    the actual regression this feature was opened for.
    """
    body = _get_json(STATUS_URL)
    assert body is not None, f"no /api/status body at {STATUS_URL}"

    if body["knowledge_mcp"] != "reachable":
        assert _fin("down").returncode == 0, "fin down must succeed"
        subprocess.Popen(
            ["bash", str(FIN), "up"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            body = _get_json(STATUS_URL)
            if body and body["knowledge_mcp"] == "reachable":
                break
            time.sleep(2)
        else:
            raise AssertionError(
                "warm restart still did not reach knowledge_mcp=reachable; "
                f"last body: {body}. Infra is probably not healthy — check "
                f"`docker compose -f {IKNOW_ROOT}/docker-compose.yml ps`."
            )

    assert body["retrieve_adapter_kind"] == "mcp", (
        "the product boot must publish the MCP port; a fake kind here means "
        "the classification and the gate have drifted"
    )
    assert body["bound_collection_name"] != "unbound", (
        "reachable + mcp must name the bound 资料集合"
    )


def test_status_agrees_with_the_boot_verdict(live_stack: None) -> None:
    """`fin status` and the boot reporter are one classification, so they agree.

    They share `scripts/lib/readiness.sh`; this is the live proof that the
    sharing holds on a real payload rather than on a stub.
    """
    result = _fin("status")
    assert result.returncode == 0, (
        f"fin status must exit 0 (ready) on a reachable stack; got "
        f"{result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ready" in result.stdout
    assert "knowledge_mcp=reachable" in result.stdout


def test_verdict_file_records_the_ready_code(live_stack: None) -> None:
    """`fin up` cannot set an exit code (it execs fin-serve); `.fin/ready` is
    the durable verdict an operator or script can read."""
    verdict = REPO_ROOT / ".fin" / "ready"
    assert verdict.is_file(), "fin up must write its verdict to .fin/ready"
    assert verdict.read_text(encoding="utf-8").strip() == "0 ready"


def test_serve_log_has_a_producer(live_stack: None) -> None:
    """`fin logs` was documented and impossible: nothing wrote the file.

    `fin up` must leave serve's own output in `.fin/serve.log`, or the
    command `fin logs` tails is a dead end.
    """
    log = REPO_ROOT / ".fin" / "serve.log"
    assert log.is_file(), "fin up must tee fin-serve output to .fin/serve.log"
    assert log.stat().st_size > 0, "the serve log must not be empty"
    assert "Uvicorn running" in log.read_text(encoding="utf-8", errors="replace")


def test_conversation_entry_is_not_refused(live_stack: None) -> None:
    """The end the whole feature exists for: asking must not 409.

    The original complaint was that fin served while the memory service
    was absent, so the first question failed with `knowledge_mcp_fault`.
    A reachable MCP must leave conversation entry open.
    """
    request = urllib.request.Request(
        f"{SERVE_URL}/api/threads",
        data=json.dumps({"first_message": "ping"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200, f"thread creation must be 200; got {response.status}"
        payload = json.loads(response.read().decode())
    assert payload.get("error") != "knowledge_mcp_fault", (
        "a reachable knowledge MCP must not refuse conversation entry"
    )
