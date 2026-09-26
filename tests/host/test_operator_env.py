"""Tests for operator ``.env`` loading at process entry.

``compile_for_runtime`` keeps reading ``os.environ`` only — tests
of that helper stay hermetic. The loader is what ``fin-serve`` and
``python -m fin`` call before compile, so a local ``.env`` is part
of boot rather than an extra shell step.

The same entry point runs the retrieve-adapter boot probe (T9 /
spec SC14): ``FIN_RETRIEVE_ADAPTER`` defaults to ``mcp``, so a
laptop without the ``iknow-memory`` server on ``PATH`` boots a
graph whose every retrieve call returns the typed
``retrieve_unavailable`` EXIT. That is a WARNING — never a boot
failure, same class as the missing-``web/dist`` warning.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fin.host.operator_env import (
    RETRIEVE_REACHABLE,
    RETRIEVE_UNREACHABLE,
    apply_dotenv,
    find_env_file,
    parse_env_file,
    probe_retrieve_reachability,
    resolve_retrieve_adapter_kind,
    warn_if_mcp_retrieve_unreachable,
)


def test_parse_env_file_empty_text_returns_empty() -> None:
    assert parse_env_file("") == {}
    assert parse_env_file("\n# only comments\n") == {}


def test_parse_env_file_skips_malformed_and_comments() -> None:
    text = "\n".join(
        [
            "# comment",
            "FIN_MODEL=fin/MiniMax-M3",
            "not-a-pair",
            'FIN_BASE_URL="http://127.0.0.1:20128/v1"',
            "export FIN_CHECKPOINT_URL=postgresql://fin:fin@localhost:5432/fin",
        ]
    )
    parsed = parse_env_file(text)
    assert parsed["FIN_MODEL"] == "fin/MiniMax-M3"
    assert parsed["FIN_BASE_URL"] == "http://127.0.0.1:20128/v1"
    assert parsed["FIN_CHECKPOINT_URL"].startswith("postgresql://")
    assert "not-a-pair" not in parsed


def test_find_env_file_missing_at_project_root_returns_none(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    nested = tmp_path / "web"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert find_env_file(start=nested) is None


def test_find_env_file_walks_up_to_project_root(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    env = tmp_path / ".env"
    env.write_text("FIN_MODEL=from-root\n")
    nested = tmp_path / "web" / "deep"
    nested.mkdir(parents=True)
    found = find_env_file(start=nested)
    assert found == env


def test_apply_dotenv_missing_file_is_noop(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.chdir(tmp_path)
    environ: dict[str, str] = {}
    assert apply_dotenv(environ=environ, start=tmp_path) is None
    assert environ == {}


def test_apply_dotenv_fills_unset_and_does_not_override(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".env").write_text(
        "FIN_MODEL=from-file\nFIN_BASE_URL=http://example.invalid/v1\n"
    )
    environ = {"FIN_MODEL": "already-set"}
    path = apply_dotenv(environ=environ, start=tmp_path)
    assert path == tmp_path / ".env"
    assert environ["FIN_MODEL"] == "already-set"
    assert environ["FIN_BASE_URL"] == "http://example.invalid/v1"


def test_apply_dotenv_twice_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".env").write_text("FIN_MODEL=once\n")
    environ: dict[str, str] = {}
    apply_dotenv(environ=environ, start=tmp_path)
    apply_dotenv(environ=environ, start=tmp_path)
    assert environ == {"FIN_MODEL": "once"}


def test_serve_applies_dotenv_before_uvicorn(
    monkeypatch, deployed_attachment_bounds
) -> None:
    order: list[str] = []

    def _dotenv() -> Path | None:
        order.append("dotenv")
        return None

    def _run(*_args, **_kwargs) -> None:
        order.append("uvicorn")

    monkeypatch.setattr("fin.host.http.apply_dotenv", _dotenv)
    monkeypatch.setattr(
        "fin.host.http.compile_for_http", lambda: order.append("compile")
    )
    # The knowledge-MCP probe is a separate boot step; stub it so this
    # ordering assertion does not depend on a live/absent MCP binary.
    monkeypatch.setattr("fin.host.http.publish_knowledge_mcp", lambda: None)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _run)
    from fin.host.http import serve

    serve()
    assert order == ["dotenv", "compile", "uvicorn"]


def _path_without_mcp(tmp_path: Path) -> dict[str, str]:
    """An environ whose ``PATH`` holds no ``iknow-memory`` binary."""
    empty = tmp_path / "bin-empty"
    empty.mkdir()
    return {"PATH": str(empty)}


def _path_with_mcp(tmp_path: Path) -> dict[str, str]:
    """An environ whose ``PATH`` holds an executable ``iknow-memory``."""
    bindir = tmp_path / "bin-with-mcp"
    bindir.mkdir()
    binary = bindir / "iknow-memory"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return {"PATH": str(bindir)}


def test_boot_probe_warns_when_mcp_default_and_command_missing(
    tmp_path: Path, caplog
) -> None:
    """Adapter defaults to ``mcp`` + no reachable command → WARNING.

    Spec SC14: the boot must still proceed (the probe returns a
    flag, it does not raise), and the operator must be told that
    retrieve calls will answer ``retrieve_unavailable``.
    """
    with caplog.at_level(logging.WARNING, logger="fin.host.operator_env"):
        unreachable = warn_if_mcp_retrieve_unreachable(
            environ=_path_without_mcp(tmp_path)
        )

    assert unreachable is True
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1, records
    message = records[0].getMessage()
    assert "FIN_RETRIEVE_ADAPTER" in message
    assert "iknow-memory" in message
    # Same typed EXIT the adapter returns with no transport, so the
    # boot log and the run-time error read as one story.
    assert "retrieve_unavailable" in message


def test_boot_probe_silent_when_command_is_on_path(
    tmp_path: Path, caplog
) -> None:
    with caplog.at_level(logging.WARNING, logger="fin.host.operator_env"):
        unreachable = warn_if_mcp_retrieve_unreachable(
            environ=_path_with_mcp(tmp_path)
        )

    assert unreachable is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_boot_probe_silent_when_adapter_is_not_mcp(
    tmp_path: Path, caplog
) -> None:
    """``FIN_RETRIEVE_ADAPTER=fake`` is the documented opt-out."""
    environ = _path_without_mcp(tmp_path) | {"FIN_RETRIEVE_ADAPTER": "fake"}
    with caplog.at_level(logging.WARNING, logger="fin.host.operator_env"):
        unreachable = warn_if_mcp_retrieve_unreachable(environ=environ)

    assert unreachable is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_serve_warns_about_unreachable_mcp_and_still_serves(
    tmp_path: Path, monkeypatch, caplog, deployed_attachment_bounds
) -> None:
    """``fin-serve`` boot publishes 缺, logs the WARNING, keeps booting.

    The product seam (workbench retrieve product seam) replaces the
    PATH-only ``warn_if_mcp_retrieve_unreachable`` call with
    ``publish_knowledge_mcp``, which probes a real handshake and stores
    the three-state snapshot. The WARNING still must not stop the boot,
    exactly like the missing-``web/dist`` case.
    """
    served: list[str] = []

    monkeypatch.setattr("fin.host.http.apply_dotenv", lambda: None)
    monkeypatch.setattr("fin.host.http.compile_for_http", lambda: None)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append("uvicorn"))
    monkeypatch.setenv("PATH", str(_path_without_mcp(tmp_path)["PATH"]))
    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)

    from fin.host.http import serve
    from fin.host.knowledge_mcp import (
        KNOWLEDGE_MCP_MISSING,
        knowledge_mcp_state,
        reset_knowledge_mcp_state,
    )

    reset_knowledge_mcp_state()
    with caplog.at_level(logging.WARNING, logger="fin.host.knowledge_mcp"):
        serve()

    assert served == ["uvicorn"], "the WARNING must not stop the boot"
    assert knowledge_mcp_state() == KNOWLEDGE_MCP_MISSING
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "fin.host.knowledge_mcp"
    ]
    assert any("knowledge_mcp_fault" in m for m in warnings), warnings


# ----- shared reachability probe (T10, ADR 0033) -----
#
# The boot WARNING above and the workspace status surface answer the
# same question — "will every retrieve call return the typed
# retrieve_unavailable EXIT?" — so they read one probe instead of two
# copies of the PATH lookup.


def test_probe_reports_unreachable_for_mcp_default_without_the_command(
    tmp_path: Path,
) -> None:
    assert (
        probe_retrieve_reachability(environ=_path_without_mcp(tmp_path))
        == RETRIEVE_UNREACHABLE
    )


def test_probe_reports_reachable_when_the_command_is_on_path(
    tmp_path: Path,
) -> None:
    assert (
        probe_retrieve_reachability(environ=_path_with_mcp(tmp_path))
        == RETRIEVE_REACHABLE
    )


def test_probe_reports_reachable_for_the_fake_adapter(tmp_path: Path) -> None:
    """The fake port answers every call — it is a healthy boot.

    ADR 0033 keeps the two states about the PORT, not about the
    corpus: ``retrieve_adapter_kind`` is what says the answers carry
    no real evidence.
    """
    environ = _path_without_mcp(tmp_path) | {"FIN_RETRIEVE_ADAPTER": "fake"}
    assert probe_retrieve_reachability(environ=environ) == RETRIEVE_REACHABLE


def test_probe_does_not_spawn_the_mcp_server(tmp_path: Path, monkeypatch) -> None:
    """PATH lookup only — no subprocess, no health-check RPC.

    ADR 0033 keeps the MCP client out of the status read seam; a
    probe spawn per request would also cost a subprocess on every
    Sidebar poll.
    """
    import subprocess

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the reachability probe must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)

    assert (
        probe_retrieve_reachability(environ=_path_with_mcp(tmp_path))
        == RETRIEVE_REACHABLE
    )


def test_resolve_adapter_kind_defaults_to_the_product_adapter() -> None:
    from fin.host.compile import DEFAULT_RETRIEVE_ADAPTER

    assert resolve_retrieve_adapter_kind(environ={}) == DEFAULT_RETRIEVE_ADAPTER
    assert resolve_retrieve_adapter_kind(environ={"FIN_RETRIEVE_ADAPTER": ""}) == (
        DEFAULT_RETRIEVE_ADAPTER
    )


def test_resolve_adapter_kind_reads_the_configured_kind() -> None:
    assert (
        resolve_retrieve_adapter_kind(environ={"FIN_RETRIEVE_ADAPTER": " fake "})
        == "fake"
    )


def test_resolve_adapter_kind_clamps_an_unknown_kind_and_warns(caplog) -> None:
    """A typo can never be a third reported kind.

    ``compile_graph`` refuses an unknown kind, so a serving process
    cannot actually be on one — but the status surface must still
    answer with one of the two typed kinds rather than echo the typo
    or raise.
    """
    from fin.host.compile import DEFAULT_RETRIEVE_ADAPTER

    with caplog.at_level(logging.WARNING, logger="fin.host.operator_env"):
        kind = resolve_retrieve_adapter_kind(
            environ={"FIN_RETRIEVE_ADAPTER": "bm25"}
        )

    assert kind == DEFAULT_RETRIEVE_ADAPTER
    assert any("bm25" in r.getMessage() for r in caplog.records)


def test_default_graph_factory_applies_dotenv_before_compile(
    monkeypatch,
) -> None:
    order: list[str] = []

    def _dotenv() -> Path | None:
        order.append("dotenv")
        return None

    def _compile() -> str:
        order.append("compile")
        return "graph"

    monkeypatch.setattr("fin.host.cli.apply_dotenv", _dotenv)
    monkeypatch.setattr("fin.host.compile.compile_for_runtime", _compile)
    from fin.host.cli import _default_graph_factory

    assert _default_graph_factory() == "graph"
    assert order == ["dotenv", "compile"]
