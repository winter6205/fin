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
