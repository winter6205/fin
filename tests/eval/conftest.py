"""Shared fixtures and collection hooks for the nine gold eval cases (T10).

Why a dedicated conftest
------------------------
The eval suite lives under ``tests/eval/`` and is opt-in: the nine
gold cases are marked ``@pytest.mark.eval`` and are **not** part of
the default test run. ADR 0012 + spec success-criteria item 9 require
that ``uv run pytest -q`` exits 0 without invoking the eval cases
(no live LLM, no extra runtime budget), and that
``uv run pytest -q -m eval`` collects and runs exactly those nine.

Pytest's marker registration in ``pyproject.toml`` registers the
``eval`` marker but does **not** deselect it from the default run.
This conftest closes that gap with a ``pytest_collection_modifyitems``
hook: when the operator has not selected ``-m eval`` (or any
expression that selects the eval mark), the eval-marked items are
deselected, so the default ``pytest -q`` run does not even collect
them (they appear as ``N deselected`` in the summary).

When ``-m eval`` IS on the command line, the hook early-returns and
pytest's core marker filtering keeps exactly the nine gold cases.

Scope: a per-directory conftest is sufficient because the hook is
only loaded when collection reaches ``tests/eval/``, which only
happens when eval-marked items are candidates for collection.

Terminal-status counting (#135, spec SC10)
------------------------------------------
The graph handed to each gold case is wrapped so every ``invoke``
records the run's terminal status. At the end of an eval session the
tally is printed as one machine-readable line, which
``scripts/eval_counts.py`` parses into the documented done / refuse
integers. Counting here — rather than in the script — keeps the
numbers OBSERVED (what the runs did) instead of declared (what the
assertions expect), and costs the suite nothing when the line is not
read.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph


def _eval_counts_module() -> ModuleType:
    """Load ``scripts/eval_counts.py`` by path.

    The script is operator tooling outside the ``fin`` package, so it
    has no import path. Loading it here keeps the printed line's
    format single-sourced with the parser that reads it.
    """
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_counts.py"
    spec = importlib.util.spec_from_file_location("eval_counts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Terminal statuses observed this session, in invoke order. Module
# scope matches the session: the eval lane runs in one process.
_OBSERVED_STATUSES: list[str] = []


class _StatusRecordingGraph:
    """Pass-through graph proxy that records each run's terminal status.

    Only ``invoke`` is intercepted (the gold cases' single entry
    point); everything else delegates, so the wrapper cannot change
    what the suite exercises.
    """

    def __init__(self, graph: Any, sink: list[str]) -> None:
        self._graph = graph
        self._sink = sink

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        out = self._graph.invoke(*args, **kwargs)
        if isinstance(out, dict):
            self._sink.append(str(out.get("status", "")))
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)


@pytest.fixture
def scripted_graph_factory():
    """Return a factory that builds a fresh host graph wired to a scripted model.

    Parameters
    ----------
    script
        A list of ``AIMessage``s to hand to ``ScriptedChatModel``.
        The factory copies the list so tests can keep using the
        original (e.g. for debugging) without affecting the wired
        script. After the script is exhausted, the model's last
        ``AIMessage`` is returned forever (T6 ``ScriptedChatModel``
        contract) — this is how the bounce / refuse / step-cap
        loops are driven deterministically.

    Returns
    -------
    _LockedGraph
        A host graph ready to ``invoke`` with a
        ``{"messages": [...], "evidence": [], "repair_count": 0,
        "step_count": 0}`` input dict and a per-test ``thread_id``.
    """

    def _factory(script: Iterable[BaseMessage]):
        # Copy the script so callers can mutate their original list
        # without disturbing the model. ``list`` would also accept a
        # tuple / generator; we accept any iterable to match the
        # ticket's loose phrasing ("a script").
        model = ScriptedChatModel(script=list(script))
        # No ``adapter`` / paths: ``compile_graph`` defaults resolve
        # to the worktree's canonical corpus (graph.py
        # ``_DEFAULT_DATA_DIR``). ``known_document_ids`` is then
        # derived from the loaded tree — all 12 frozen manifest
        # ids. FOO-2023Q1-style unknowns trip the gate's
        # ``unknown_locator`` path; the 12 frozen docs do not.
        graph = compile_graph(
            checkpointer=InMemorySaver(),
            model=model,
        )
        return _StatusRecordingGraph(graph, _OBSERVED_STATUSES)

    return _factory


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect eval-marked items unless the operator selected ``-m eval``.

    The default ``pytest -q`` run must not invoke (or even collect)
    the nine gold cases: spec success-criteria item 9 + ADR 0012.
    When the ``-m`` option's value is empty or does not reference
    ``eval``, we remove every eval-marked item from the collection
    and notify pytest via the ``pytest_deselected`` hook so the
    summary line reports ``N deselected`` (not a hidden drop).
    """
    # ``markexpr`` is the parsed ``-m`` value; empty when unset.
    # Any expression that selects the eval mark (``-m eval``,
    # ``-m "eval or slow"``) contains the substring ``"eval"``,
    # which is enough to bypass the deselection — pytest's own
    # marker filtering then narrows to the requested subset.
    markexpr = config.getoption("markexpr") or ""
    if "eval" in markexpr:
        return

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker("eval") is not None:
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Print the observed terminal-status tally for the eval lane.

    Nothing is printed when no run was recorded — the default lane
    loads this conftest during collection, and a ``done=0 refuse=0``
    line there would give ``scripts/eval_counts.py`` a number that
    describes no suite.
    """
    if not _OBSERVED_STATUSES:
        return
    counts_module = _eval_counts_module()
    terminalreporter.write_line(
        counts_module.format_count_line(
            counts_module.count_terminal_statuses(_OBSERVED_STATUSES)
        )
    )