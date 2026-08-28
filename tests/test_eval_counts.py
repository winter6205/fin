"""Counting the nine gold eval cases (T1, #135, spec SC10).

Spec `specs/enterprise-kb-agent.md` Focus 5 / SC10: ``uv run pytest
-q -m eval`` stays the nine gold cases, and a repo-documented command
prints an integer done-count and refuse-count for that suite —
scripted, with no live LLM-as-judge.

The command is ``uv run python scripts/eval_counts.py``. It shells
out to the eval lane, and the lane's own conftest records the
terminal status of every run the gold cases invoke, then prints one
machine-readable line the script parses.

These tests cover the pure counting / formatting / parsing seam in
the DEFAULT lane; they never start the eval suite (that would nest
pytest inside pytest for no extra signal).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_eval_counts() -> ModuleType:
    """Import ``scripts/eval_counts.py`` by path.

    The script lives outside the ``fin`` package on purpose (it is
    operator tooling, not product code), so there is no import path
    to it — the eval conftest loads it the same way.
    """
    path = _repo_root() / "scripts" / "eval_counts.py"
    spec = importlib.util.spec_from_file_location("eval_counts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----- counting -----


def test_counts_the_three_terminal_statuses() -> None:
    mod = _load_eval_counts()

    counts = mod.count_terminal_statuses(
        ["done", "refuse", "done", "step_cap", "done"]
    )
    assert counts["done"] == 3
    assert counts["refuse"] == 1
    assert counts["step_cap"] == 1
    assert counts["total"] == 5


def test_counts_are_zero_integers_when_nothing_ran() -> None:
    """Empty input is zeros, not missing keys — the printed line must
    always carry both integers."""
    mod = _load_eval_counts()

    counts = mod.count_terminal_statuses([])
    assert counts["done"] == 0
    assert counts["refuse"] == 0
    assert counts["total"] == 0


def test_a_status_outside_the_closed_enum_is_counted_as_unknown() -> None:
    """``running`` is not a terminal status. Rather than silently
    folding it into a real bucket, it lands in ``unknown`` so the
    command can fail loudly on it."""
    mod = _load_eval_counts()

    counts = mod.count_terminal_statuses(["done", "running"])
    assert counts["done"] == 1
    assert counts["unknown"] == 1
    assert counts["total"] == 2


# ----- the wire line between the eval lane and the script -----


def test_format_and_parse_round_trip() -> None:
    mod = _load_eval_counts()

    counts = mod.count_terminal_statuses(["done"] * 6 + ["refuse"] * 3)
    line = mod.format_count_line(counts)
    assert line.startswith(mod.COUNT_LINE_PREFIX)
    assert mod.parse_count_line(line) == counts


def test_parse_finds_the_line_inside_a_full_pytest_report() -> None:
    mod = _load_eval_counts()

    output = "\n".join(
        [
            "collected 9 items",
            ".........                          [100%]",
            mod.format_count_line(
                mod.count_terminal_statuses(["done"] * 6 + ["refuse"] * 3)
            ),
            "9 passed, 800 deselected in 12.34s",
        ]
    )
    counts = mod.parse_count_line(output)
    assert counts["done"] == 6
    assert counts["refuse"] == 3


def test_parse_without_the_line_raises_typed_error() -> None:
    """A missing line means the eval lane did not record anything;
    printing ``done=0 refuse=0`` there would be a lie."""
    mod = _load_eval_counts()

    with pytest.raises(mod.EvalCountsError):
        mod.parse_count_line("9 passed in 12.34s")


# ----- the documented command -----


def test_script_drives_the_documented_eval_command() -> None:
    """SC10 pins the suite command; the counter must not run a
    different selection."""
    mod = _load_eval_counts()

    assert mod.DEFAULT_PYTEST_COMMAND == (
        "uv",
        "run",
        "pytest",
        "-q",
        "-m",
        "eval",
    )


def test_readme_documents_the_count_command() -> None:
    """"Repo-documented" is part of the acceptance, so the doc line is
    part of the contract."""
    readme = (_repo_root() / "README.md").read_text(encoding="utf-8")
    assert "uv run python scripts/eval_counts.py" in readme
