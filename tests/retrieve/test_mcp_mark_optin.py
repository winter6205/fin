"""The ``mcp`` mark's opt-in contract, asserted in the DEFAULT run (SC6).

The smoke itself is deselected by default, so nothing in
``test_mcp_smoke.py`` can guard the thing SC6 actually promises: that
the mark exists, that it is excluded from default collection, and that
adding it left the other opt-in marks alone (SC18). These tests carry
no mark and therefore run in every ``uv run pytest -q``.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CONFTEST = Path(__file__).with_name("conftest.py")
_SMOKE = Path(__file__).with_name("test_mcp_smoke.py")

# SC18: this spec must not touch the ``eval`` marker configuration.
# The literal below is the line as it stood before the ``mcp`` mark was
# added, so any edit to it fails here rather than in a later review.
_EVAL_MARKER_LINE = "eval: opt-in eval cases (default `pytest -q` does NOT run these)"
_SOFT_TRACK_MARKER_LINE = (
    "soft_track: Soft-Track opt-in contracts (deselected by default)"
)


def _load(path: Path, name: str) -> ModuleType:
    """Import ``path`` under an explicit module name.

    Loading by path rather than by import name keeps this independent of
    which of the sibling ``conftest.py`` files happens to be first on
    ``sys.path`` during a given run.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _markers() -> list[str]:
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["pytest"]["ini_options"]["markers"]


# ----- marker registration -----


def test_mcp_marker_is_registered() -> None:
    """``mcp`` is a registered marker, so the smoke does not run under a
    ``PytestUnknownMarkWarning`` and ``-m mcp`` is a supported selector."""
    names = [marker.split(":", 1)[0].strip() for marker in _markers()]
    assert "mcp" in names, _markers()


def test_sql_live_marker_is_registered_and_distinct() -> None:
    """#322 review: the live business-ledger acceptance rides its OWN
    ``sql_live`` mark — the registered meaning of ``mcp`` is the
    fixture-server protocol smoke, and spec §9 / AGENTS.md require the
    two to stay distinguishable. Both are opt-in (deselected by
    default), so neither leaks into the hermetic run."""
    names = [marker.split(":", 1)[0].strip() for marker in _markers()]
    assert "sql_live" in names, _markers()
    assert names.index("sql_live") != names.index("mcp")


def test_existing_optin_marker_lines_are_untouched() -> None:
    """SC18: adding ``mcp`` must not change the ``eval`` (or
    ``soft_track``) marker configuration."""
    markers = _markers()
    assert _EVAL_MARKER_LINE in markers
    assert _SOFT_TRACK_MARKER_LINE in markers


def test_default_addopts_do_not_filter_marks() -> None:
    """Deselection lives in the conftest hook, not in ``addopts``.

    Putting ``-m "not mcp"`` in ``addopts`` would silently override any
    ``-m`` the operator passes on the command line, which is why the
    sibling opt-in marks use a collection hook instead.
    """
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-m" not in addopts.split(), addopts


# ----- the deselection hook -----


class _StubItem:
    """Minimal stand-in for a collected item."""

    def __init__(self, name: str, *marks: str) -> None:
        self.name = name
        self._marks = set(marks)

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._marks else None

    def __repr__(self) -> str:
        return f"<item {self.name}>"


class _StubHook:
    def __init__(self) -> None:
        self.deselected: list[Any] = []

    def pytest_deselected(self, *, items: list[Any]) -> None:
        self.deselected.extend(items)


class _StubConfig:
    def __init__(self, markexpr: str) -> None:
        self._markexpr = markexpr
        self.hook = _StubHook()

    def getoption(self, name: str) -> str:
        assert name == "markexpr", name
        return self._markexpr


def _run_hook(markexpr: str, items: list[Any]) -> tuple[_StubConfig, list[Any]]:
    conftest = _load(_CONFTEST, "_retrieve_conftest_under_test")
    config = _StubConfig(markexpr)
    conftest.pytest_collection_modifyitems(config, items)  # type: ignore[arg-type]
    return config, items


def test_hook_deselects_mcp_items_when_the_mark_is_not_selected() -> None:
    """Default run: marked items leave the collection and are reported
    to pytest as deselected, not dropped on the floor."""
    marked = _StubItem("smoke", "mcp")
    plain = _StubItem("unit")
    config, items = _run_hook("", [marked, plain])
    assert items == [plain]
    assert config.hook.deselected == [marked]


def test_hook_keeps_mcp_items_when_the_mark_is_selected() -> None:
    """``-m mcp``: the hook stands aside and lets pytest's own marker
    filtering pick the smoke."""
    marked = _StubItem("smoke", "mcp")
    plain = _StubItem("unit")
    config, items = _run_hook("mcp", [marked, plain])
    assert items == [marked, plain]
    assert config.hook.deselected == []


def test_hook_deselects_sql_live_unless_it_is_named() -> None:
    """The two opt-in marks are independent: ``-m mcp`` selects the
    fixture-server smoke but must NOT drag the live business-ledger
    acceptance in (and the default run drops both)."""
    mcp_item = _StubItem("smoke", "mcp")
    live_item = _StubItem("live", "sql_live")
    plain = _StubItem("unit")

    config, items = _run_hook("", [mcp_item, live_item, plain])
    assert items == [plain]
    assert config.hook.deselected == [mcp_item, live_item]

    config, items = _run_hook("mcp", [mcp_item, live_item, plain])
    assert items == [mcp_item, plain]
    assert config.hook.deselected == [live_item]

    config, items = _run_hook("sql_live", [mcp_item, live_item, plain])
    assert items == [live_item, plain]
    assert config.hook.deselected == [mcp_item]


@pytest.mark.parametrize("markexpr", ["", "eval", "soft_track", "not slow"])
def test_hook_never_deselects_unmarked_items(markexpr: str) -> None:
    """No selector that leaves ``mcp`` unmentioned may cost an ordinary
    test its place in the run."""
    plain = [_StubItem("a"), _StubItem("b")]
    config, items = _run_hook(markexpr, list(plain))
    assert items == plain
    assert config.hook.deselected == []


def test_hook_is_inert_when_nothing_is_marked() -> None:
    """With no marked items the hook reports no deselection at all, so
    the summary line is unchanged for suites that never touch MCP."""
    plain = [_StubItem("a")]
    config, items = _run_hook("", list(plain))
    assert items == plain
    assert config.hook.deselected == []


# ----- the smoke module is fully covered by the mark -----


def test_every_smoke_test_carries_the_mcp_mark() -> None:
    """A module-level ``pytestmark`` is what keeps a newly added smoke
    test from leaking into the default run: forget a per-test decorator
    and the default suite starts spawning subprocesses.

    Importing the module by path also proves it imports cleanly during a
    default collection, which is when a broken import would surface.
    """
    module = _load(_SMOKE, "_mcp_smoke_module_under_test")
    assert module.pytestmark.name == "mcp"
    per_test_marks = [
        name
        for name, value in vars(module).items()
        if name.startswith("test_") and callable(value)
    ]
    assert per_test_marks, "the smoke module defines no tests"
