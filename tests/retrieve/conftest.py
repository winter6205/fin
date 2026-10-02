"""Retrieve-suite collection hook for the opt-in opt-in marks (mcp, sql_live).

The MCP 2.0 smoke (``test_mcp_smoke.py``) is marked
``@pytest.mark.mcp``. It spawns a fixture MCP server as a subprocess,
so it must **not** run under default ``uv run pytest -q`` (spec SC6 /
plan Task 4): the default run stays hermetic and fast.

The live business-ledger acceptance
(``test_live_sql_business_db.py``, ``test_ledger_readonly_boundary.py``)
carries its OWN mark, ``@pytest.mark.sql_live``: it drives a real
PostgreSQL and the real iknow executor, which spec §9 and AGENTS.md
require to stay DISTINGUISHABLE from a fixture-server protocol smoke —
a green ``-m mcp`` must never silently include (or be mistaken for) the
live-boundary legs, and vice versa. Both marks share the same posture:
deselected by default, and a missing live dependency is a named failure
(blocked evidence), never a skip.

Registering the markers in ``pyproject.toml`` silences the unknown-mark
warning but does not deselect anything. This hook closes that gap the
same way ``tests/eval/conftest.py`` and ``tests/host/conftest.py`` do:
when the operator has not put a mark on the command line, items carrying
that mark are deselected and reported in the ``N deselected`` summary
rather than silently dropped. With ``-m mcp`` (or ``-m sql_live``)
present, pytest's own marker filtering selects that suite and the hook
stands aside for that mark only.

A per-directory conftest is enough because the marks only appear under
``tests/retrieve/``.
"""

from __future__ import annotations

import pytest


# Each entry is an opt-in mark: deselected unless the operator names it
# in ``-m``. The marks are independent — selecting one never drags the
# other's items into the run.
OPT_IN_MARKS: tuple[str, ...] = ("mcp", "sql_live")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect items whose opt-in mark is not named in ``-m``."""
    markexpr = config.getoption("markexpr") or ""

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if any(
            mark not in markexpr
            and item.get_closest_marker(mark) is not None
            for mark in OPT_IN_MARKS
        ):
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
