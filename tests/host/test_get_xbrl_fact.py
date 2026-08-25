"""Boundary tests for the v1 host tool wrapper ``get_xbrl_fact``.

Per ADR 0031 / CONTEXT ``get_xbrl_fact``: the research-track tool is a
**lookup** of one tagged financial fact from an ingest-built fact
table, NOT a retriever. The wrapper translates the
``lookup_fact`` return value into the contract dict the model sees:

* hit  -> ``{"concept", "value", "period", "unit", "scale",
  "source_locator": {...}}`` (no ``error`` key).
* miss -> ``{"error": "fact_not_found", "concept", "period",
  "document_id"}`` (no exception, no raise).

Per spec § Boundaries ("empty hits not exceptions") and the
defensive-contract axis: every miss is a structured dict, never a
raise, so the agent loop cannot leak a typed error out of the
``ToolNode``.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, write_facts
from fin.host.tools.get_xbrl_fact import get_xbrl_fact


def _seeded_locator() -> Locator:
    return Locator(
        document_id="AAPL-2023Q3",
        version="7b9b54830f070aab56cd842b391dd81d21613aacda2cb92234f43b997930b0ea",
        page=4,
    )


def _seeded_facts_root(tmp_path: Path) -> Path:
    """Write the seeded ``us-gaap:Revenues / 2023-Q3-YTD`` row."""
    facts_root = tmp_path / "facts"
    write_facts(
        document_id="AAPL-2023Q3",
        facts=[
            Fact(
                concept="us-gaap:Revenues",
                value=383285000000,
                period="2023-Q3-YTD",
                unit="USD",
                scale="thousands",
                source_locator=_seeded_locator(),
            )
        ],
        facts_root=facts_root,
    )
    return facts_root


# ===== boundary 1: happy path =====


def test_get_xbrl_fact_tool_returns_hit_dict(tmp_path: Path) -> None:
    """Happy path: ``get_xbrl_fact`` against the seeded row returns the
    hit dict (no ``error`` key) with all six contract fields."""
    facts_root = _seeded_facts_root(tmp_path)

    result = get_xbrl_fact(
        concept="us-gaap:Revenues",
        period="2023-Q3-YTD",
        document_id="AAPL-2023Q3",
        facts_root=facts_root,
    )
    assert "error" not in result
    assert result["concept"] == "us-gaap:Revenues"
    assert result["period"] == "2023-Q3-YTD"
    assert result["unit"] == "USD"
    assert result["scale"] == "thousands"
    assert result["value"] == 383285000000
    locator = result["source_locator"]
    assert locator["document_id"] == "AAPL-2023Q3"
    assert locator["page"] == 4


# ===== boundary 2: negative (unknown concept) =====


def test_get_xbrl_fact_tool_returns_miss_dict(tmp_path: Path) -> None:
    """Negative: an unknown concept returns the typed miss dict
    (NOT a raise) per spec § Boundaries ("empty hits not
    exceptions")."""
    facts_root = _seeded_facts_root(tmp_path)

    result = get_xbrl_fact(
        concept="us-gaap:DoesNotExist",
        period="2023-Q3-YTD",
        document_id="AAPL-2023Q3",
        facts_root=facts_root,
    )
    assert result.get("error") == "fact_not_found"
    assert result["concept"] == "us-gaap:DoesNotExist"
    assert result["period"] == "2023-Q3-YTD"
    assert result["document_id"] == "AAPL-2023Q3"
    # The hit shape's keys MUST NOT leak into the miss shape.
    assert "value" not in result
    assert "source_locator" not in result


# ===== boundary 3: empty input (model passes empty strings) =====


def test_get_xbrl_fact_tool_empty_concept_returns_miss(tmp_path: Path) -> None:
    """Empty class: empty ``concept`` returns the miss dict (NOT a
    raise) so the model can recover."""
    facts_root = _seeded_facts_root(tmp_path)
    result = get_xbrl_fact(
        concept="",
        period="2023-Q3-YTD",
        facts_root=facts_root,
    )
    assert result.get("error") == "fact_not_found"


def test_get_xbrl_fact_tool_empty_period_returns_miss(tmp_path: Path) -> None:
    """Empty class: empty ``period`` returns the miss dict."""
    facts_root = _seeded_facts_root(tmp_path)
    result = get_xbrl_fact(
        concept="us-gaap:Revenues",
        period="",
        facts_root=facts_root,
    )
    assert result.get("error") == "fact_not_found"


# ===== boundary 4: concurrent (T5 acceptance) =====


def test_get_xbrl_fact_tool_concurrent_independent(tmp_path: Path) -> None:
    """Concurrent class (T5 acceptance: "two overlapping lookups do
    not raise"): two threads call the host tool with overlapping
    args; both return their respective results without raising."""
    facts_root = _seeded_facts_root(tmp_path)

    def call(args: tuple[str, str]) -> dict:
        return get_xbrl_fact(
            concept=args[0],
            period=args[1],
            document_id="AAPL-2023Q3",
            facts_root=facts_root,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_hit = ex.submit(call, ("us-gaap:Revenues", "2023-Q3-YTD"))
        f_miss = ex.submit(call, ("us-gaap:Missing", "2023-Q3-YTD"))
        hit_result = f_hit.result(timeout=5.0)
        miss_result = f_miss.result(timeout=5.0)

    assert "error" not in hit_result
    assert miss_result.get("error") == "fact_not_found"


# ===== boundary 5: serialization (the tool message JSON is JSON-friendly) =====


def test_get_xbrl_fact_tool_hit_is_json_serializable(tmp_path: Path) -> None:
    """The hit dict MUST be JSON-serializable so ``ToolMessage.content``
    can carry it (LangChain requires string content)."""
    facts_root = _seeded_facts_root(tmp_path)

    result = get_xbrl_fact(
        concept="us-gaap:Revenues",
        period="2023-Q3-YTD",
        document_id="AAPL-2023Q3",
        facts_root=facts_root,
    )
    # Round-trip: must encode/decode without raising or losing keys.
    encoded = json.dumps(result, default=str)
    decoded = json.loads(encoded)
    assert decoded["concept"] == "us-gaap:Revenues"
    assert decoded["source_locator"]["page"] == 4