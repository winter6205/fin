"""Tests for operator ``.env`` loading at process entry.

``compile_for_runtime`` keeps reading ``os.environ`` only — tests
of that helper stay hermetic. The loader is what ``fin-serve`` and
``python -m fin`` call before compile, so a local ``.env`` is part
of boot rather than an extra shell step.
"""

from __future__ import annotations

from pathlib import Path

from fin.host.operator_env import apply_dotenv, find_env_file, parse_env_file


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


def test_serve_applies_dotenv_before_uvicorn(monkeypatch) -> None:
    order: list[str] = []

    def _dotenv() -> Path | None:
        order.append("dotenv")
        return None

    def _run(*_args, **_kwargs) -> None:
        order.append("uvicorn")

    monkeypatch.setattr("fin.host.http.apply_dotenv", _dotenv)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _run)
    from fin.host.http import serve

    serve()
    assert order == ["dotenv", "uvicorn"]


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
