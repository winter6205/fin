"""Host-suite collection hooks for Soft-Track opt-in contracts.

Soft-Track ship suites live under ``tests/host/`` and are marked
``@pytest.mark.soft_track``. They must **not** run under default
``uv run pytest -q`` (spec Soft-Track ship gate / plan Task 5).

Mirrors ``tests/eval/conftest.py``: register-style deselection via
``pytest_collection_modifyitems`` when ``-m soft_track`` (or any
expression selecting that mark) is absent from the command line.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect soft_track-marked items unless ``-m soft_track``."""
    markexpr = config.getoption("markexpr") or ""
    if "soft_track" in markexpr:
        return

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker("soft_track") is not None:
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
