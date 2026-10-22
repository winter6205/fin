"""Boot-time knowledge-MCP probe and in-process snapshot (T1).

The product path derives its three-state knowledge-MCP field from a
REAL handshake — ``initialize`` then ``notifications/initialized`` then
``tools/list`` — never from a ``PATH`` lookup alone. This file pins:

* 缺 (``missing``): the launch command is not on ``PATH``;
* 坏 (``fault``): the command exists but the handshake fails / times
  out / answers garbage / exits non-zero;
* 可达 (``reachable``): a fixture server completes ``initialize`` +
  ``tools/list``;
* SC9: a ``PATH``-only binary that cannot complete the handshake must
  never publish ``reachable``;
* SC5 / assumption 8: an explicit fake / scripted adapter publishes
  ``missing``, never ``reachable``;
* the child process never survives a probe.

``GET /api/status`` keeps serving 200 with the typed three-state field
in every state. The snapshot lives in ``fin.host.knowledge_mcp`` and is
reset by the autouse fixture in ``tests/host/conftest.py`` so no state
leaks between tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fin.host.knowledge_mcp import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    KNOWLEDGE_MCP_FAULT,
    KNOWLEDGE_MCP_MISSING,
    KNOWLEDGE_MCP_REACHABLE,
    KNOWLEDGE_MCP_STATES,
    knowledge_mcp_gate_closed,
    knowledge_mcp_state,
    probe_knowledge_mcp,
    publish_knowledge_mcp,
    publish_knowledge_mcp_state,
    resolve_bound_retrieve_adapter_kind,
)

FIXTURE_SERVER = (
    Path(__file__).resolve().parents[1]
    / "retrieve"
    / "mcp_fixture_server.py"
)


def _fixture_command() -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE_SERVER))


def _bin_dir(tmp_path: Path, name: str, script: str) -> str:
    bindir = tmp_path / f"bin-{name}"
    bindir.mkdir(exist_ok=True)
    binary = bindir / "iknow-memory"
    binary.write_text(script)
    binary.chmod(0o755)
    return str(bindir)


def _wrapper_script() -> str:
    """A POSIX ``iknow-memory`` wrapper that execs the fixture server."""
    return (
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{FIXTURE_SERVER}" "$@"\n'
    )


@pytest.fixture
def empty_bin(tmp_path: Path) -> str:
    bindir = tmp_path / "bin-empty"
    bindir.mkdir(exist_ok=True)
    return str(bindir)


# ----- probe: the three states from a real handshake -----


def test_default_probe_timeout_covers_a_cold_stdio_handshake() -> None:
    """One cold stdio handshake must fit inside the default attempt budget.

    Config, generation id, and server connect run past 2s. A shorter
    default kills the child before ``tools/list``, and the retry starts
    another cold process, latching fault.
    """
    assert DEFAULT_PROBE_TIMEOUT_SECONDS >= 8.0


def test_probe_missing_when_command_absent_from_path(empty_bin: str) -> None:
    assert (
        probe_knowledge_mcp(environ={"PATH": empty_bin})
        == KNOWLEDGE_MCP_MISSING
    )


def test_probe_empty_command_is_missing_not_fault() -> None:
    """No command configured is 缺 (nothing to launch), not 坏."""
    assert (
        probe_knowledge_mcp(command=[], environ={})
        == KNOWLEDGE_MCP_MISSING
    )


def test_probe_explicit_environ_without_path_does_not_use_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-built ``environ`` with no ``PATH`` key is 缺.

    A fully working ``iknow-memory`` wrapper is on the ambient
    ``os.environ['PATH']`` here, but the caller passed an explicit
    mapping that does not carry one, so the probe must not silently
    consult the ambient ``PATH``.
    """
    bindir = _bin_dir(tmp_path, "ambient", _wrapper_script())
    monkeypatch.setenv("PATH", bindir)

    assert (
        probe_knowledge_mcp(
            command=("iknow-memory",), environ={}, timeout_seconds=2.0
        )
        == KNOWLEDGE_MCP_MISSING
    )


def test_probe_none_environ_still_reads_os_environ(
    monkeypatch: pytest.MonkeyPatch, empty_bin: str
) -> None:
    """``environ=None`` is the product path: it reads ``os.environ``.

    Guards against fixing the explicit-mapping bug by breaking the
    default call path.
    """
    monkeypatch.setenv("PATH", empty_bin)
    assert probe_knowledge_mcp(environ=None) == KNOWLEDGE_MCP_MISSING


def test_probe_fault_when_command_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = _bin_dir(tmp_path, "exit1", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", bindir)

    assert (
        probe_knowledge_mcp(
            command=("iknow-memory",),
            environ={"PATH": bindir},
            timeout_seconds=1.0,
            attempts=1,
        )
        == KNOWLEDGE_MCP_FAULT
    )


def test_probe_fault_when_command_prints_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = _bin_dir(tmp_path, "garbage", "#!/bin/sh\necho not-json\nexit 0\n")
    monkeypatch.setenv("PATH", bindir)

    assert (
        probe_knowledge_mcp(
            command=("iknow-memory",),
            environ={"PATH": bindir},
            timeout_seconds=1.0,
            attempts=1,
        )
        == KNOWLEDGE_MCP_FAULT
    )


def test_probe_fault_when_handshake_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = (
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    )
    assert (
        probe_knowledge_mcp(
            command=slow, environ=dict(os.environ), timeout_seconds=0.2, attempts=1
        )
        == KNOWLEDGE_MCP_FAULT
    )


def test_probe_reachable_when_fixture_completes_the_handshake() -> None:
    assert (
        probe_knowledge_mcp(
            command=_fixture_command(),
            environ=dict(os.environ),
            timeout_seconds=5.0,
            attempts=1,
        )
        == KNOWLEDGE_MCP_REACHABLE
    )


def test_path_only_binary_that_fails_handshake_is_never_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC9: presence on ``PATH`` is not product truth. A binary that
    exits 0 without answering ``initialize`` / ``tools/list`` is 坏, not
    可达."""
    bindir = _bin_dir(tmp_path, "exit0", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", bindir)

    state = probe_knowledge_mcp(
        command=("iknow-memory",),
        environ={"PATH": bindir},
        timeout_seconds=1.0,
        attempts=1,
    )

    assert state == KNOWLEDGE_MCP_FAULT
    assert state != KNOWLEDGE_MCP_REACHABLE


def test_probe_never_leaves_a_child_process_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session is closed in a ``finally`` — no orphan MCP child."""
    import fin.host.knowledge_mcp as knowledge_mcp

    closed: list[str] = []
    real_session = knowledge_mcp.MCPStdioSession

    class RecordingSession(real_session):  # type: ignore[misc, valid-type]
        def close(self) -> None:
            closed.append("closed")
            super().close()

    monkeypatch.setattr(knowledge_mcp, "MCPStdioSession", RecordingSession)

    assert (
        probe_knowledge_mcp(
            command=_fixture_command(),
            environ=dict(os.environ),
            timeout_seconds=5.0,
            attempts=1,
        )
        == KNOWLEDGE_MCP_REACHABLE
    )
    assert closed == ["closed"]


def test_probe_short_retry_then_still_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded retry re-judges; it does not busy-loop forever."""
    bindir = _bin_dir(tmp_path, "always", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", bindir)

    assert (
        probe_knowledge_mcp(
            command=("iknow-memory",),
            environ={"PATH": bindir},
            timeout_seconds=0.5,
            attempts=3,
        )
        == KNOWLEDGE_MCP_FAULT
    )


# ----- adapter-kind interaction (SC5 / assumption 8) -----


def _warning_messages(caplog) -> list[str]:
    import logging

    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "fin.host.knowledge_mcp"
    ]


def test_fake_lane_warning_does_not_claim_conversation_refusal(
    empty_bin: str, caplog
) -> None:
    """H1: the fake / scripted gate is open, so the WARNING must not say
    conversation entry is refused there."""
    import logging

    with caplog.at_level(logging.WARNING, logger="fin.host.knowledge_mcp"):
        state = publish_knowledge_mcp(
            environ={"FIN_RETRIEVE_ADAPTER": "fake", "PATH": empty_bin}
        )

    assert state == KNOWLEDGE_MCP_MISSING
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1, warnings
    message = warnings[0]
    assert "refused" not in message
    assert "NOT gated" in message
    assert "test-only adapter" in message


def test_product_mcp_lane_warning_claims_conversation_refusal(
    empty_bin: str, caplog
) -> None:
    """H1: on the product lane the gate IS closed, so the WARNING (and
    only that lane) may claim the refusal."""
    import logging

    with caplog.at_level(logging.WARNING, logger="fin.host.knowledge_mcp"):
        state = publish_knowledge_mcp(
            environ={"FIN_RETRIEVE_ADAPTER": "mcp", "PATH": empty_bin}
        )

    assert state == KNOWLEDGE_MCP_MISSING
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1, warnings
    message = warnings[0]
    assert "refused with knowledge_mcp_fault" in message
    assert "test-only adapter" not in message


def test_fake_adapter_publishes_missing_never_reachable(empty_bin: str) -> None:
    state = publish_knowledge_mcp(
        environ={"FIN_RETRIEVE_ADAPTER": "fake", "PATH": empty_bin}
    )

    assert state == KNOWLEDGE_MCP_MISSING
    assert state != KNOWLEDGE_MCP_REACHABLE
    assert knowledge_mcp_state() == KNOWLEDGE_MCP_MISSING


def test_fake_adapter_does_not_spawn_even_with_a_broken_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fake`` short-circuits before any ``PATH`` lookup or process."""
    import subprocess

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the fake adapter must not spawn an MCP probe")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    assert (
        publish_knowledge_mcp(
            environ={"FIN_RETRIEVE_ADAPTER": "fake", "PATH": str(tmp_path)}
        )
        == KNOWLEDGE_MCP_MISSING
    )


def test_scripted_boot_also_publishes_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FIN_SCRIPTED_MODEL`` forces the fake adapter (scripted_boot), so
    the scripted lane publishes 缺 too — even when an MCP-looking binary
    happens to sit on ``PATH``."""
    import subprocess

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("a scripted boot must not spawn an MCP probe")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    environ = {
        "FIN_SCRIPTED_MODEL": "1",
        "FIN_RETRIEVE_ADAPTER": "mcp",
        "PATH": str(tmp_path),
    }

    assert publish_knowledge_mcp(environ=environ) == KNOWLEDGE_MCP_MISSING


# ----- publish-level product ``mcp`` coverage (H5) -----


def test_publish_mcp_adapter_without_command_is_missing(
    empty_bin: str,
) -> None:
    """(a) Product adapter + no ``iknow-memory`` on ``PATH`` → 缺."""
    state = publish_knowledge_mcp(
        environ={"FIN_RETRIEVE_ADAPTER": "mcp", "PATH": empty_bin}
    )

    assert state == KNOWLEDGE_MCP_MISSING
    assert knowledge_mcp_state() == KNOWLEDGE_MCP_MISSING


def test_publish_mcp_adapter_with_working_wrapper_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) Product adapter + a PATH ``iknow-memory`` that execs the
    fixture server → 可达, proving the publish seam runs the real probe."""
    bindir = _bin_dir(tmp_path, "publish-mcp", _wrapper_script())
    # ``publish_knowledge_mcp`` probes ``DEFAULT_MCP_COMMAND`` and the
    # session's ``Popen`` resolves the bare name against the ambient
    # ``os.environ['PATH']``, so both must carry the wrapper dir.
    monkeypatch.setenv("PATH", bindir)

    state = publish_knowledge_mcp(
        environ={"FIN_RETRIEVE_ADAPTER": "mcp", "PATH": bindir}
    )

    assert state == KNOWLEDGE_MCP_REACHABLE
    assert knowledge_mcp_state() == KNOWLEDGE_MCP_REACHABLE


# ----- the BOUND adapter kind (ADR 0033 amendment 2026-09-20) -----
#
# The route publishes, and the gate reads, the port this process
# actually bound — not the ``FIN_RETRIEVE_ADAPTER`` string. A scripted
# boot compiles the fake port whatever that string names, so the two
# resolutions diverge on exactly that lane; the classifier in
# ``scripts/lib/readiness.sh`` reads the published value and must land
# on the same verdict as ``knowledge_mcp_gate_closed``.


@pytest.mark.parametrize("configured_adapter", [None, "mcp"])
def test_bound_kind_is_fake_on_a_scripted_boot(
    configured_adapter: str | None,
) -> None:
    """``FIN_SCRIPTED_MODEL`` binds the fake port whatever is configured.

    ``compile_for_scripted_e2e`` passes ``retrieve_adapter="fake"`` to
    ``compile_graph``, so the seam a scripted process serves is the fake
    one even when ``FIN_RETRIEVE_ADAPTER=mcp``.
    """
    environ: dict[str, str] = {"FIN_SCRIPTED_MODEL": "1"}
    if configured_adapter is not None:
        environ["FIN_RETRIEVE_ADAPTER"] = configured_adapter

    assert resolve_bound_retrieve_adapter_kind(environ=environ) == "fake"


def test_bound_kind_is_fake_when_the_configured_adapter_is_fake() -> None:
    environ = {"FIN_RETRIEVE_ADAPTER": "fake"}
    assert resolve_bound_retrieve_adapter_kind(environ=environ) == "fake"


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"FIN_RETRIEVE_ADAPTER": ""},
        {"FIN_SCRIPTED_MODEL": "0"},
        {"FIN_SCRIPTED_MODEL": ""},
        {"FIN_SCRIPTED_MODEL": "false"},
    ],
)
def test_bound_kind_is_the_product_default_without_a_fake_boot(
    environ: dict[str, str],
) -> None:
    """Absent / falsy switches keep the product ``mcp`` port.

    Covers the unset var, a blank value, and the falsy spellings the
    scripted switch accepts — the gate must stay closed on all of them
    when the snapshot is not 可达.
    """
    assert resolve_bound_retrieve_adapter_kind(environ=environ) == "mcp"


def test_unreadable_scripted_switch_is_not_confirmed_scripted() -> None:
    """A malformed ``FIN_SCRIPTED_MODEL`` keeps the product kind here.

    ``compile_for_http`` raises ``ScriptedBootConfigError`` on such a
    value, so no process serves on it; the resolver only refuses to
    claim "scripted" without confirmation — it never raises on a
    read-only route.
    """
    environ = {"FIN_SCRIPTED_MODEL": "maybe"}
    assert resolve_bound_retrieve_adapter_kind(environ=environ) == "mcp"


def test_bound_kind_keeps_scripted_boot_out_of_the_import_graph() -> None:
    """The resolver never imports ``scripted_boot`` at module scope.

    Checked on the module's IMPORT graph, which is what would break the
    import cycle this helper was told to avoid: a fresh process that
    imports ``fin.host.knowledge_mcp`` must not pull the scripted boot
    (a model + a corpus reader) into ``sys.modules``. The functions do
    import it lazily on the scripted branch — asserted by the
    ``fake``-on-a-scripted-boot case above, which cannot answer without
    it.
    """
    import subprocess
    import sys as _sys

    probe = (
        "import sys; import fin.host.knowledge_mcp; "
        "print('fin.host.scripted_boot' in sys.modules)"
    )
    completed = subprocess.run(
        [_sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == "False", completed.stdout


# ----- snapshot lifecycle -----


def test_publish_state_validates_against_the_known_states() -> None:
    assert KNOWLEDGE_MCP_STATES == (
        KNOWLEDGE_MCP_MISSING,
        KNOWLEDGE_MCP_FAULT,
        KNOWLEDGE_MCP_REACHABLE,
    )
    with pytest.raises(ValueError):
        publish_knowledge_mcp_state("probe-went-sideways")


def test_gate_is_open_when_this_process_never_probed() -> None:
    """Hermetic default suite: no boot → no snapshot → gate open."""
    assert knowledge_mcp_state() is None
    assert knowledge_mcp_gate_closed(environ={"FIN_RETRIEVE_ADAPTER": "mcp"}) is False


def test_gate_closed_only_for_mcp_adapter_in_a_non_reachable_state(
    empty_bin: str,
) -> None:
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_MISSING)
    assert knowledge_mcp_gate_closed(environ={"FIN_RETRIEVE_ADAPTER": "mcp"}) is True
    assert knowledge_mcp_gate_closed(environ={"FIN_RETRIEVE_ADAPTER": "fake"}) is False

    publish_knowledge_mcp_state(KNOWLEDGE_MCP_FAULT)
    assert knowledge_mcp_gate_closed(environ={"FIN_RETRIEVE_ADAPTER": "mcp"}) is True

    publish_knowledge_mcp_state(KNOWLEDGE_MCP_REACHABLE)
    assert knowledge_mcp_gate_closed(environ={"FIN_RETRIEVE_ADAPTER": "mcp"}) is False


# ----- GET /api/status stays 200 with the typed three-state field -----


def _status() -> dict:
    from fin.host.http import app

    response = TestClient(app).get("/api/status")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


@pytest.mark.parametrize(
    "state",
    [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT, KNOWLEDGE_MCP_REACHABLE],
)
def test_status_serves_200_with_the_published_state(state: str) -> None:
    publish_knowledge_mcp_state(state)

    body = _status()

    # The three lifecycle counts are aggregates with no identity, so they
    # are admitted here; every other key still has to reopen this set.
    assert set(body.keys()) == {
        "knowledge_mcp",
        "retrieve_adapter_kind",
        "model_identity",
        "corpus_filing_count",
        "bound_collection_name",
        "run_counts",
    }
    assert body["knowledge_mcp"] == state


def test_status_fake_adapter_is_missing_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake lane reports 缺 and never 可达 (ADR 0033 amendment).

    缺 is published explicitly instead of being inherited from the
    never-probed snapshot: this process never probed, so the route reports
    未探测 for that case (see
    ``tests/host/test_knowledge_mcp_status_not_probed.py``), and the
    point here is the fake lane's own 缺.
    """
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_MISSING)
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")

    body = _status()

    assert body["knowledge_mcp"] == KNOWLEDGE_MCP_MISSING
    assert body["knowledge_mcp"] != KNOWLEDGE_MCP_REACHABLE
    assert body["retrieve_adapter_kind"] == "fake"
