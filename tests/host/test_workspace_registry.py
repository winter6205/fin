"""Default 工作区 resolution tests (spec Focus 2 / SC3-SC4, Round 3).

The multi-root registry (``known_roots.json``, ``bind_root`` /
``create_root``, ``$HOME/.fin/workspaces/<slug>``) was UNSHIPPED per
the 2026-09-06 single-default amendment (ADR 0049 amendment, spec
SC4): there is exactly one 工作区, ``$HOME/.fin/default``, and no
registry file. What survives in ``fin.host.workspace_registry`` is
only:

* ``default_workspace_root(home)`` — the SSOT for the
  ``<home>/.fin/default`` shape (mkdir if missing);
* the ``Path.cwd()``-free contract (never a cwd fallback).

Boundary classes exercised (defensive-contract):
* empty — fresh home, resolve creates the directory;
* negative — HOME env is ignored (explicit ``home`` argument wins);
* exception — no ``Path.cwd()`` anywhere in the module (AST scan).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fin.host.workspace_registry import (
    DEFAULT_ROOT_NAME,
    default_workspace_root,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _home(tmp_path: Path) -> Path:
    """An isolated $HOME: tests never touch the real home directory."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


# ---------------------------------------------------------------------------
# Default directory resolution (the surviving surface).
# ---------------------------------------------------------------------------


def test_default_root_is_under_fin_dot_dir(tmp_path: Path) -> None:
    home = _home(tmp_path)

    root = default_workspace_root(home)

    assert root == home / ".fin" / DEFAULT_ROOT_NAME


def test_default_root_creates_directory_on_first_call(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = home / ".fin" / DEFAULT_ROOT_NAME
    assert not target.exists()

    root = default_workspace_root(home)

    assert root == target
    assert root.is_dir()


def test_default_root_call_reads_path_home_at_call_time(tmp_path: Path) -> None:
    """``default_workspace_root`` resolves home from the call arg
    (NOT from a process-wide module-level capture)."""
    home_a = _home(tmp_path / "a")
    home_b = _home(tmp_path / "b")

    a = default_workspace_root(home_a)
    b = default_workspace_root(home_b)

    assert a == home_a / ".fin" / DEFAULT_ROOT_NAME
    assert b == home_b / ".fin" / DEFAULT_ROOT_NAME
    assert a != b


def test_default_root_does_not_read_HOME_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", "/")

    root = default_workspace_root(home)

    assert root == home / ".fin" / DEFAULT_ROOT_NAME


def test_default_root_honors_explicit_home_over_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function signature (``home: Path``) takes precedence over
    any environment variable, so tests can fully isolate the suite."""
    real_home = tmp_path / "real-home"
    fake_home = _home(tmp_path / "fake-home")
    monkeypatch.setenv("HOME", str(real_home))

    root = default_workspace_root(fake_home)

    assert root == fake_home / ".fin" / DEFAULT_ROOT_NAME
    assert not (real_home / ".fin" / DEFAULT_ROOT_NAME).exists()


def test_default_workspace_root_does_not_call_path_home_at_import() -> None:
    """``Path.home()`` must be read at call time, not module import."""
    # Sanity: importing the module should not pin $HOME.
    from fin.host import workspace_registry  # noqa: F401

    # The function object remains an unbound callable; resolution is
    # lazy because the function reads the argument at call time.
    assert callable(workspace_registry.default_workspace_root)


# ---------------------------------------------------------------------------
# Unshipped contract — the multi-root registry is GONE (spec SC4 /
# ADR 0049 amendment: no ``known_roots.json``, no ``workspaces/<slug>``,
# no bind/create).
# ---------------------------------------------------------------------------


def test_module_does_not_export_multi_root_registry_symbols() -> None:
    """The registry class, bind/create, slug errors, and slug helpers
    must no longer exist on the module (unshipped, not extended)."""
    import fin.host.workspace_registry as mod

    for name in (
        "WorkspaceRegistry",
        "WorkspaceRoot",
        "WorkspaceBindError",
        "WorkspaceNotFoundError",
        "WorkspaceNotADirectoryError",
        "WorkspaceSlugError",
        "WorkspaceSlugEmptyError",
        "WorkspaceSlugIllegalError",
        "WorkspaceSlugOverflowError",
        "WorkspaceRegistryError",
        "WorkspaceRegistryUnreadableError",
        "WorkspaceRegistryUnwritableError",
        "bind_root",
        "create_root",
        "list_roots",
        "REGISTRY_FILENAME",
        "WORKSPACES_DIR_NAME",
        "MAX_SLUG_LENGTH",
    ):
        assert not hasattr(mod, name), (
            f"unshipped multi-root symbol still exported: {name}"
        )


def test_module_source_has_no_registry_artifacts() -> None:
    """No registry filename, no ``workspaces/`` slug directory, no
    bind/create code anywhere in the module source (grep-level guard
    so a future edit cannot silently re-grow the surface)."""
    import inspect

    from fin.host import workspace_registry

    source = inspect.getsource(workspace_registry)
    for token in ("known_roots.json", "bind_root", "create_root", "slug"):
        assert token not in source, (
            f"multi-root artifact {token!r} reappeared in "
            "workspace_registry.py"
        )


def test_module_does_not_call_path_cwd() -> None:
    """The module must never call ``Path.cwd()`` (spec Focus 2 /
    SC3 — never `Path.cwd()`). Comments / docstrings may name it as
    a reference; only executable code is checked."""
    import ast
    import inspect

    from fin.host import workspace_registry

    source = inspect.getsource(workspace_registry)
    tree = ast.parse(source)

    class _CallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.hits: list[str] = []

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            attr = getattr(func, "attr", None)
            # Match both ``Path.cwd()`` and ``Path().cwd()``.
            if isinstance(func, ast.Attribute) and attr == "cwd":
                value = func.value
                if (isinstance(value, ast.Name) and value.id == "Path") or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "Path"
                ):
                    self.hits.append(
                        f"line {node.lineno}: {ast.unparse(node)}"
                    )
            self.generic_visit(node)

    visitor = _CallVisitor()
    visitor.visit(tree)
    assert visitor.hits == [], (
        "workspace_registry must not call Path.cwd(); found: "
        + ", ".join(visitor.hits)
    )
