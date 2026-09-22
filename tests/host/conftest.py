"""Host-suite collection hooks for Soft-Track opt-in contracts.

Soft-Track ship suites live under ``tests/host/`` and are marked
``@pytest.mark.soft_track``. They must **not** run under default
``uv run pytest -q`` (spec Soft-Track ship gate / plan Task 5).

Mirrors ``tests/eval/conftest.py``: register-style deselection via
``pytest_collection_modifyitems`` when ``-m soft_track`` (or any
expression selecting that mark) is absent from the command line.

Also owns the T3 comparison fact seed. The tool unit tests and the
in-thread / workspace tests assert against the SAME numbers, so the
seed is a fixture rather than constants each file re-declares
(``tests/host`` is not a package; a cross-module import would be
resolved by ``sys.path`` order, not by intent).

The live-Jev gate does NOT live here: it moved to ``tests/conftest.py``,
because ``jev_live`` suites now exist under ``tests/retrieve/`` too and a
second copy of a credential gate is how skip-vs-fail semantics drift.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

from dataclasses import dataclass
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, write_facts


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect opt-in suites unless the matching ``-m`` is on the CLI.

    Three marks live here:

    * ``soft_track``: Soft-Track ship suites (spec Soft-Track ship gate).
    * ``memory_on``: cases that exercise the ON memory path under
      pytest (T3 / SC3). Default ``uv run pytest -q`` must stay OFF
      even when the operator ``.env`` has ``FIN_MEMORY_ENABLED=true``
      (ADR 0035); the marker is the opt-in switch the gold set uses.
    * ``mcp``: live boot acceptance that drives the real
      `fin up` stack (Docker + a sibling iknow-memory checkout +
      minutes). `tests/retrieve/conftest.py` owns this mark for its own
      directory; the launcher's live acceptance lives under
      ``tests/host/``, so the same deselection has to exist here or it
      would run under the default hermetic suite.

    ``jev_live`` is deliberately NOT handled here: the root
    ``tests/conftest.py`` deselects it for every directory, because the
    live Jev suites are not confined to this one.

    All three are register-style deselection: a CLI ``-m mcp`` (etc.)
    re-runs them. The check is per-mark so a CLI ``-m memory_on`` keeps
    the ``soft_track`` items deselected and vice versa — they are
    independent opt-in switches.
    """
    markexpr = config.getoption("markexpr") or ""

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        markers = (
            "soft_track" if item.get_closest_marker("soft_track") is not None else None,
            "memory_on" if item.get_closest_marker("memory_on") is not None else None,
            "mcp" if item.get_closest_marker("mcp") is not None else None,
        )
        # Drop only the opt-in marks that the CLI did NOT request. An
        # unmarked test stays kept.
        opt_in_only = tuple(name for name in markers if name is not None)
        if opt_in_only and all(name not in markexpr for name in opt_in_only):
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


@pytest.fixture(autouse=True)
def _reset_knowledge_mcp_snapshot():
    """Clear the in-process knowledge-MCP snapshot around every host test.

    The snapshot is module-level state in ``fin.host.knowledge_mcp``
    (one process = one boot). Tests that publish 缺 / 坏 / 可达 must not
    leak that state into an unrelated test's conversation mouth, so this
    autouse fixture resets before and after each ``tests/host`` case.
    """
    from fin.host.knowledge_mcp import reset_knowledge_mcp_state

    reset_knowledge_mcp_state()
    yield
    reset_knowledge_mcp_state()


@pytest.fixture(autouse=True)
def _memory_on_env_for_marked_tests(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Re-install ``FIN_MEMORY_ENABLED=true`` for ``memory_on``-marked tests.

    The session-level clear in ``tests/conftest.py`` removes the var at
    import time, and the autouse strip in
    ``tests/host/test_memory_pytest_off.py._strip_operator_memory_env``
    drops it again before each unmarked test. A test marked
    ``@pytest.mark.memory_on`` opts out of THAT autouse strip; this
    autouse fixture is the paired side, restoring the value the
    operator's ``.env`` would have set so the test sees the real ON
    path. monkeypatch teardown cleans up after the test, so subsequent
    unmarked tests still see a clean env.
    """
    if request.node.get_closest_marker("memory_on") is None:
        return
    monkeypatch.setenv("FIN_MEMORY_ENABLED", "true")


# ----- T3 comparison fact seed (spec Focus 1) -----


@dataclass(frozen=True)
class CompareSeed:
    """The seeded fact table a comparison test compares against."""

    root: Path
    concept: str
    period_2022: str
    period_2023: str
    aapl_2022_doc: str
    aapl_2022_version: str
    aapl_2022_value: int
    aapl_2023_doc: str
    aapl_2023_version: str
    aapl_2023_value: int
    msft_2023_doc: str
    msft_2023_version: str
    msft_2023_value: int


@pytest.fixture
def compare_seed(tmp_path: Path) -> CompareSeed:
    """Three fact files: AAPL across two periods, MSFT for one.

    Cross-period needs two AAPL documents (a 10-Q reports its own
    period), which is exactly why an issuer row resolves across
    documents instead of pinning one ``document_id``.
    """
    seed = CompareSeed(
        root=tmp_path / "facts",
        concept="us-gaap:Revenues",
        period_2022="2022-Q3-YTD",
        period_2023="2023-Q3-YTD",
        aapl_2022_doc="AAPL-2022Q3",
        aapl_2022_version="a" * 64,
        aapl_2022_value=194140,
        aapl_2023_doc="AAPL-2023Q3",
        aapl_2023_version="b" * 64,
        aapl_2023_value=load_private_fixture("e8e6fa2502cf2441d4f1862bbbb7d147b7dbd57a3911be363417fe4342e69b94"),
        msft_2023_doc="MSFT-2023Q3",
        msft_2023_version="c" * 64,
        msft_2023_value=211915,
    )
    rows = (
        (
            seed.aapl_2022_doc,
            seed.period_2022,
            seed.aapl_2022_value,
            seed.aapl_2022_version,
            4,
        ),
        (
            seed.aapl_2023_doc,
            seed.period_2023,
            seed.aapl_2023_value,
            seed.aapl_2023_version,
            4,
        ),
        (
            seed.msft_2023_doc,
            seed.period_2023,
            seed.msft_2023_value,
            seed.msft_2023_version,
            5,
        ),
    )
    for document_id, period, value, version, page in rows:
        write_facts(
            document_id=document_id,
            facts=[
                Fact(
                    concept=seed.concept,
                    value=value,
                    period=period,
                    unit="USD",
                    scale="millions",
                    source_locator=Locator(
                        document_id=document_id, version=version, page=page
                    ),
                )
            ],
            facts_root=seed.root,
        )
    return seed
