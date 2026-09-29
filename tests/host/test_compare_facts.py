"""Unit tests for the ``compare_facts`` host tool (T3 / spec Focus 1).

``compare_facts`` is the comparison surface the research track calls
instead of authoring a prose table: cross-period (one issuer, several
periods) and peer (several issuers, one period) both resolve to the
SAME ``fin.comparison.v1`` envelope, whose numbers come from the
ingest-built fact table with a locator per cell. The model never
authors the arithmetic — deltas are computed here.

Five input classes are covered:

* empty      — no periods / no scope resolves to ``empty_comparison``
  with a still-renderable skeleton, never an exception;
* negative   — an unknown issuer / unknown period yields ``missing``
  cells with ``fact_not_found``, never a raise;
* overflow   — a request above ``MAX_CELLS`` is the typed
  ``comparison_overflow``, not a multi-thousand-cell table;
* concurrent — the lookup is read-only (no shared mutable state), so
  two threads comparing at once see identical envelopes;
* exception  — a malformed fact file degrades to missing cells
  (``read_facts`` already returns ``[]``), never a raise.

The seeded numbers come from the ``compare_seed`` fixture in this
suite's ``conftest`` so the in-thread / workspace tests assert
against the same table.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, write_facts
from fin.host.tools.compare_facts import (
    COMPARISON_SCHEMA,
    MAX_CELLS,
    compare_facts,
)


# ----- cross-period (issuer row, several period columns) -----


def test_cross_period_issuer_row_resolves_both_periods(compare_seed) -> None:
    """One issuer row, two period columns, both cells hit — and each
    cell carries the locator of the document that reported it."""
    result = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2022, compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )
    assert "error" not in result
    comparison = result["comparison"]
    assert comparison["schema"] == COMPARISON_SCHEMA
    assert comparison["kind"] == "table"
    assert comparison["axis"] == "period"
    assert comparison["concept"] == compare_seed.concept
    assert [c["key"] for c in comparison["columns"]] == [
        compare_seed.period_2022,
        compare_seed.period_2023,
    ]
    assert len(comparison["rows"]) == 1
    row = comparison["rows"][0]
    assert row["key"] == "AAPL"
    assert row["kind"] == "issuer"
    assert [cell["value"] for cell in row["cells"]] == [
        compare_seed.aapl_2022_value,
        compare_seed.aapl_2023_value,
    ]
    assert [cell["status"] for cell in row["cells"]] == ["hit", "hit"]
    assert row["cells"][0]["locator"] == {
        "document_id": compare_seed.aapl_2022_doc,
        "version": compare_seed.aapl_2022_version,
        "unit": 4,
    }
    assert comparison["hit_count"] == 2
    assert comparison["cell_count"] == 2
    assert comparison["missing"] == []


def test_cross_period_delta_is_code_computed(compare_seed) -> None:
    """The delta between consecutive period columns is arithmetic the
    tool performs, not prose the model authors."""
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2022, compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert len(comparison["deltas"]) == 1
    delta = comparison["deltas"][0]
    assert delta["from"] == {"row": "AAPL", "column": compare_seed.period_2022}
    assert delta["to"] == {"row": "AAPL", "column": compare_seed.period_2023}
    expected = compare_seed.aapl_2023_value - compare_seed.aapl_2022_value
    assert delta["absolute"] == pytest.approx(expected)
    assert delta["percent"] == pytest.approx(
        expected / compare_seed.aapl_2022_value * 100, abs=0.01
    )
    assert delta["direction"] == "up"
    assert delta["display"].startswith(f"+{expected:,}")


def test_cell_display_is_thousands_separated(compare_seed) -> None:
    """``display`` is the rendered string both the SPA and the T4
    export bind to, so the contract pins its shape here."""
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["rows"][0]["cells"][0]["display"] == "293,787"


# ----- peer (several issuer rows, one period column) -----


def test_peer_comparison_axis_and_baseline_delta(compare_seed) -> None:
    """Two issuers, one period: axis is ``peer`` and the delta is each
    row against the FIRST row (the baseline the operator named)."""
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        issuers=["AAPL", "MSFT"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["axis"] == "peer"
    assert [row["key"] for row in comparison["rows"]] == ["AAPL", "MSFT"]
    assert len(comparison["deltas"]) == 1
    delta = comparison["deltas"][0]
    assert delta["from"] == {"row": "AAPL", "column": compare_seed.period_2023}
    assert delta["to"] == {"row": "MSFT", "column": compare_seed.period_2023}
    expected = compare_seed.msft_2023_value - compare_seed.aapl_2023_value
    assert delta["absolute"] == pytest.approx(expected)
    assert delta["direction"] == "down"
    assert delta["display"].startswith(f"-{abs(expected):,}")


def test_document_ids_scope_rows_to_documents(compare_seed) -> None:
    """``document_ids`` rows are document-scoped; the row kind says so
    (the T4 export needs to label the axis)."""
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        document_ids=[compare_seed.aapl_2023_doc, compare_seed.msft_2023_doc],
        facts_root=compare_seed.root,
    )["comparison"]
    assert [row["kind"] for row in comparison["rows"]] == [
        "document",
        "document",
    ]
    assert [row["key"] for row in comparison["rows"]] == [
        compare_seed.aapl_2023_doc,
        compare_seed.msft_2023_doc,
    ]


def test_matrix_axis_when_both_dimensions_are_plural(compare_seed) -> None:
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2022, compare_seed.period_2023],
        issuers=["AAPL", "MSFT"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["axis"] == "matrix"


def test_single_axis_when_one_row_one_column(compare_seed) -> None:
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["axis"] == "single"
    assert comparison["deltas"] == []


def test_locators_are_deduped_in_first_seen_order(compare_seed) -> None:
    """``locators`` is the run's evidence handle for this comparison —
    T4's citation/comparison export binds to it."""
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2022, compare_seed.period_2023],
        issuers=["AAPL", "MSFT"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["locators"] == [
        {
            "document_id": compare_seed.aapl_2022_doc,
            "version": compare_seed.aapl_2022_version,
            "unit": 4,
        },
        {
            "document_id": compare_seed.aapl_2023_doc,
            "version": compare_seed.aapl_2023_version,
            "unit": 4,
        },
        {
            "document_id": compare_seed.msft_2023_doc,
            "version": compare_seed.msft_2023_version,
            "unit": 5,
        },
    ]


# ----- negative: unknown issuer / unknown period -----


def test_unknown_period_yields_missing_cell_not_exception(
    compare_seed,
) -> None:
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=["1999-Q1-YTD"],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )["comparison"]
    cell = comparison["rows"][0]["cells"][0]
    assert cell["status"] == "missing"
    assert cell["value"] is None
    assert cell["locator"] is None
    assert cell["display"] == "—"
    assert comparison["hit_count"] == 0
    assert comparison["missing"] == [
        {"row": "AAPL", "column": "1999-Q1-YTD", "reason": "fact_not_found"}
    ]


def test_unknown_issuer_still_renders_a_row_of_missing_cells(
    compare_seed,
) -> None:
    """An issuer with no fact file is a row of misses, not a dropped
    row: silently shrinking the table would hide the gap."""
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        issuers=["AAPL", "NFLX"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert [row["key"] for row in comparison["rows"]] == ["AAPL", "NFLX"]
    assert comparison["rows"][1]["cells"][0]["status"] == "missing"
    assert comparison["hit_count"] == 1


def test_delta_skipped_when_either_endpoint_is_missing(compare_seed) -> None:
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        issuers=["AAPL", "NFLX"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["deltas"] == []


def test_zero_baseline_delta_has_null_percent(tmp_path: Path) -> None:
    """Relative change is undefined at a zero baseline; the absolute
    delta still renders."""
    root = tmp_path / "facts"
    for doc, period, value in (
        ("AAPL-2022Q3", "2022-Q3-YTD", 0),
        ("AAPL-2023Q3", "2023-Q3-YTD", 500),
    ):
        write_facts(
            document_id=doc,
            facts=[
                Fact(
                    concept="us-gaap:Revenues",
                    value=value,
                    period=period,
                    unit="USD",
                    scale="millions",
                    source_locator=Locator(
                        document_id=doc, version="d" * 64, unit=1
                    ),
                )
            ],
            facts_root=root,
        )
    comparison = compare_facts(
        concept="us-gaap:Revenues",
        periods=["2022-Q3-YTD", "2023-Q3-YTD"],
        issuers=["AAPL"],
        facts_root=root,
    )["comparison"]
    delta = comparison["deltas"][0]
    assert delta["absolute"] == pytest.approx(500)
    assert delta["percent"] is None
    assert delta["display"] == "+500"


def test_string_valued_fact_is_not_comparable(tmp_path: Path) -> None:
    """XBRL allows string-tagged values; they are not arithmetic, so
    the cell reports the text and no delta is computed off it."""
    root = tmp_path / "facts"
    write_facts(
        document_id="AAPL-2023Q3",
        facts=[
            Fact(
                concept="us-gaap:Revenues",
                value="not disclosed",
                period="2023-Q3-YTD",
                unit="USD",
                scale="",
                source_locator=Locator(
                    document_id="AAPL-2023Q3", version="e" * 64, unit=2
                ),
            )
        ],
        facts_root=root,
    )
    comparison = compare_facts(
        concept="us-gaap:Revenues",
        periods=["2023-Q3-YTD"],
        issuers=["AAPL"],
        facts_root=root,
    )["comparison"]
    cell = comparison["rows"][0]["cells"][0]
    assert cell["status"] == "hit"
    assert cell["value"] is None
    assert cell["display"] == "not disclosed"


# ----- empty -----


def test_no_periods_returns_typed_empty_comparison(compare_seed) -> None:
    result = compare_facts(
        concept=compare_seed.concept,
        periods=[],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )
    assert result["error"] == "empty_comparison"
    # The skeleton is still present so the SPA / export never branch
    # on a missing key.
    comparison = result["comparison"]
    assert comparison["schema"] == COMPARISON_SCHEMA
    assert comparison["rows"] == []
    assert comparison["columns"] == []
    assert comparison["cell_count"] == 0


def test_no_resolvable_scope_returns_typed_empty_comparison(
    tmp_path: Path,
) -> None:
    """An empty facts root has no documents to discover, so an
    unscoped compare is empty rather than a zero-row table."""
    result = compare_facts(
        concept="us-gaap:Revenues",
        periods=["2023-Q3-YTD"],
        facts_root=tmp_path / "facts",
    )
    assert result["error"] == "empty_comparison"


def test_blank_entries_are_dropped_before_scoping(compare_seed) -> None:
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=["", compare_seed.period_2023, compare_seed.period_2023],
        issuers=["", "AAPL"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert [c["key"] for c in comparison["columns"]] == [
        compare_seed.period_2023
    ]
    assert [r["key"] for r in comparison["rows"]] == ["AAPL"]


def test_empty_concept_is_a_typed_empty_comparison(compare_seed) -> None:
    result = compare_facts(
        concept="",
        periods=[compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )
    assert result["error"] == "empty_comparison"


# ----- overflow -----


def test_request_above_cell_cap_is_typed_overflow(compare_seed) -> None:
    """A comparison the operator cannot read is a typed refusal, not a
    thousand-cell payload on the SSE wire."""
    periods = [f"20{n:02d}-Q1-YTD" for n in range(40)]
    result = compare_facts(
        concept=compare_seed.concept,
        periods=periods,
        issuers=["AAPL", "MSFT"],
        facts_root=compare_seed.root,
    )
    assert result["error"] == "comparison_overflow"
    assert result["limit"] == MAX_CELLS
    assert result["requested"] == len(periods) * 2
    assert result["comparison"]["rows"] == []


# ----- exception class + concurrency -----


def test_malformed_fact_file_degrades_to_missing_cells(compare_seed) -> None:
    (compare_seed.root / f"{compare_seed.aapl_2023_doc}.json").write_text(
        "{ not json"
    )
    comparison = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )["comparison"]
    assert comparison["rows"][0]["cells"][0]["status"] == "missing"


def test_concurrent_compares_are_identical(compare_seed) -> None:
    def run() -> str:
        return json.dumps(
            compare_facts(
                concept=compare_seed.concept,
                periods=[compare_seed.period_2022, compare_seed.period_2023],
                issuers=["AAPL", "MSFT"],
                facts_root=compare_seed.root,
            ),
            sort_keys=True,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(lambda _: run(), range(8)))
    assert len(set(payloads)) == 1


def test_result_round_trips_through_json(compare_seed) -> None:
    """The envelope rides a ``ToolMessage`` (JSON) and a JSONB column,
    so every value must be plain JSON — no dataclasses, no Paths."""
    result = compare_facts(
        concept=compare_seed.concept,
        periods=[compare_seed.period_2022, compare_seed.period_2023],
        issuers=["AAPL"],
        facts_root=compare_seed.root,
    )
    assert json.loads(json.dumps(result)) == result
