"""Retrieve-suite collection hook for the opt-in MCP protocol smoke.

The MCP 2.0 smoke (``test_mcp_smoke.py``) is marked
``@pytest.mark.mcp``. It spawns a fixture MCP server as a subprocess,
so it must **not** run under default ``uv run pytest -q`` (spec SC6 /
plan Task 4): the default run stays hermetic and fast.

Registering the marker in ``pyproject.toml`` silences the unknown-mark
warning but does not deselect anything. This hook closes that gap the
same way ``tests/eval/conftest.py`` and ``tests/host/conftest.py`` do:
when the operator has not put the mark on the command line, the
marked items are deselected and reported in the ``N deselected``
summary rather than silently dropped. With ``-m mcp`` present the hook
early-returns and pytest's own marker filtering selects the smoke.

A per-directory conftest is enough because the mark only appears under
``tests/retrieve/``.
"""

from __future__ import annotations

import pytest


MCP_MARK = "mcp"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect ``mcp``-marked items unless ``-m`` selects that mark."""
    markexpr = config.getoption("markexpr") or ""
    if MCP_MARK in markexpr:
        return

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker(MCP_MARK) is not None:
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
