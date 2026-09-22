"""The live-Jev gate is a named FAIL, and the default run never reaches it.

Hermetic pin for the gate hoisted into ``tests/conftest.py``. It lives in the
root because the ``jev_live`` suites are spread over ``tests/host/`` and
``tests/retrieve/``: one copy of the skip-vs-fail semantics and one copy of
the opt-in deselection is what keeps a credential gate from drifting between
directories, and this file is what stops either half from silently regressing.

Unmarked on purpose: it runs under default ``uv run pytest -q`` and contacts
nothing — an empty environ is passed explicitly and the deselection hook is fed
stand-in items, so no ``.env`` read and no network call is involved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# Loaded by path, not by name: pytest imports each directory's conftest as the
# module ``conftest``, so ``from conftest import`` binds whichever one was
# imported last — not necessarily the one that owns the gate.
_ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _root_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fin_root_tests_conftest", _ROOT_CONFTEST
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_missing_key_gate_is_named_fail() -> None:
    gate = _root_conftest()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        gate.build_jev_live_client({})
    assert gate.JEV_LIVE_KEY_MISSING in str(excinfo.value)


def test_gate_names_the_credential_it_needs() -> None:
    """The message has to be actionable: which variable to set, not just
    that something is missing."""
    gate = _root_conftest()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        gate.build_jev_live_client({})
    assert "FIN_JEV_API_KEY" in str(excinfo.value)


class _Item:
    """A stand-in for a collected item, carrying only its marks."""

    def __init__(self, name: str, *marks: str) -> None:
        self.name = name
        self._marks = set(marks)

    def get_closest_marker(self, name: str) -> Any:
        return object() if name in self._marks else None


class _HookRecorder:
    def __init__(self) -> None:
        self.deselected: list[_Item] = []

    def pytest_deselected(self, items: list[_Item]) -> None:
        self.deselected.extend(items)


class _Config:
    def __init__(self, markexpr: str) -> None:
        self.hook = _HookRecorder()
        self._markexpr = markexpr

    def getoption(self, name: str) -> str:
        assert name == "markexpr"
        return self._markexpr


def _run_gate_hook(markexpr: str, items: list[_Item]) -> _Config:
    config = _Config(markexpr)
    _root_conftest().pytest_collection_modifyitems(config, items)
    return config


def test_default_collection_deselects_every_jev_live_item() -> None:
    """``-m`` empty is what default ``uv run pytest -q`` passes: no
    ``jev_live`` item may survive it, in either directory — including the
    ``mcp`` + ``jev_live`` combination the live graph case carries."""

    plain = _Item("tests/host/test_track.py::t")
    host_live = _Item("tests/host/test_live_jev_track.py::t", "jev_live")
    retrieve_live = _Item(
        "tests/retrieve/test_live_jev_tier_reaches_wire.py::t",
        "mcp",
        "jev_live",
    )
    items = [plain, host_live, retrieve_live]
    config = _run_gate_hook("", items)

    assert items == [plain], [item.name for item in items]
    assert [item.name for item in config.hook.deselected] == [
        host_live.name,
        retrieve_live.name,
    ]


def test_named_mark_keeps_the_live_items_and_the_other_marks() -> None:
    """The deselection is an opt-in switch, not a ban: naming ``jev_live``
    (alone or combined with ``mcp`` the way the named commands do) keeps the
    live items, and an unmarked item is never dropped either way."""

    for markexpr in ("jev_live", "mcp and jev_live"):
        plain = _Item("tests/host/test_track.py::t")
        live = _Item("tests/retrieve/test_live_jev_tier_reaches_wire.py::t", "mcp", "jev_live")
        items = [plain, live]
        _run_gate_hook(markexpr, items)

        assert items == [plain, live], markexpr
