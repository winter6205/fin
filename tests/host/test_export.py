"""Tests for the thread export builder (T4, #138).

Spec Focus 3 / SC6: "one authenticated-to-local GET or POST from the
thread returns a file body for citations or comparison; missing run ->
typed 404."

The comparison export binds to the T3 envelope ``fin.comparison.v1``
(shape SSOT: ``fin.host.tools.compare_facts``). CONTEXT's compare_facts
entry forbids "computing the delta in the SPA or the export", so every
test below asserts the file carries the envelope's OWN strings.
"""

from __future__ import annotations

import json

import pytest

from fin.host.export import (
    ExportEmptyError,
    RunNotFoundError,
    UnsupportedComparisonSchemaError,
    build_export,
)
from fin.host.thread_store import RunRecord
from fin.host.tools.compare_facts import COMPARISON_SCHEMA, compare_facts


THREAD_ID = "t-export"


def _comparison_envelope() -> dict:
    """A real ``fin.comparison.v1`` envelope from the T3 tool.

    Built by ``compare_facts`` itself rather than hand-written so the
    export can never drift from the shape the turn row persists.
    """
    result = compare_facts("us-gaap:Revenues", ["2022-Q3", "2023-Q3"])
    envelope = result["comparison"]
    # The frozen corpus fixture may hold no fact rows in this
    # environment; the export contract is about the ENVELOPE, so fill
    # the grid deterministically when the lookup came back empty.
    if not envelope["rows"]:
        envelope["columns"] = [
            {"key": "2022-Q3", "label": "2022-Q3"},
            {"key": "2023-Q3", "label": "2023-Q3"},
        ]
        envelope["axis"] = "period"
        envelope["unit"] = "USD"
        envelope["scale"] = "millions"
        envelope["rows"] = [
            {
                "key": "AAPL",
                "label": "AAPL",
                "kind": "issuer",
                "cells": [
                    {
                        "row": "AAPL",
                        "column": "2022-Q3",
                        "status": "hit",
                        "value": 194140.0,
                        "display": "194,140",
                        "unit": "USD",
                        "scale": "millions",
                        "locator": {
                            "document_id": "AAPL-2022Q3",
                            "version": "a7a0",
                            "page": 4,
                        },
                    },
                    {
                        "row": "AAPL",
                        "column": "2023-Q3",
                        "status": "missing",
                        "value": None,
                        "display": "—",
                        "unit": "",
                        "scale": "",
                        "locator": None,
                    },
                ],
            }
        ]
        envelope["deltas"] = [
            {
                "from": {"row": "AAPL", "column": "2022-Q3"},
                "to": {"row": "AAPL", "column": "2023-Q3"},
                "absolute": 99647.0,
                "percent": 51.33,
                "direction": "up",
                "display": "+99,647 (+51.33%)",
            }
        ]
        envelope["locators"] = [
            {"document_id": "AAPL-2022Q3", "version": "a7a0", "page": 4}
        ]
        envelope["hit_count"] = 1
        envelope["cell_count"] = 2
    return envelope


def _run(
    run_id: str,
    *,
    comparisons: list[dict] | None = None,
    citations: list[dict] | None = None,
) -> RunRecord:
    return RunRecord(
        id=run_id,
        thread_id=THREAD_ID,
        question="q",
        status="done",
        answer="a",
        citations=citations or [],
        comparisons=comparisons or [],
    )


CITATIONS = [
    {
        "document_id": "AAPL-2022Q3",
        "version": "a7a0",
        "page": 4,
        "quote": "Total net sales increased during the third quarter",
    }
]


# ----- missing run (SC6 negative) -----


def test_export_from_a_thread_with_no_runs_is_a_typed_missing_run(
) -> None:
    with pytest.raises(RunNotFoundError) as exc:
        build_export(runs=[], kind="comparison", thread_id=THREAD_ID)
    assert exc.value.code == "run_not_found"


def test_export_with_an_unknown_run_id_is_a_typed_missing_run() -> None:
    runs = [_run("run-1", comparisons=[_comparison_envelope()])]
    with pytest.raises(RunNotFoundError) as exc:
        build_export(
            runs=runs, kind="comparison", thread_id=THREAD_ID, run_id="run-nope"
        )
    assert exc.value.code == "run_not_found"


def test_export_when_no_run_carries_that_kind_is_typed_empty() -> None:
    """A thread with turns but nothing to export is a DIFFERENT typed
    404 than "there is no such run" — the operator's next action
    differs (ask something vs pick another turn)."""
    with pytest.raises(ExportEmptyError) as exc:
        build_export(runs=[_run("run-1")], kind="citations", thread_id=THREAD_ID)
    assert exc.value.code == "export_empty"


def test_comparison_export_refuses_an_envelope_from_another_schema() -> None:
    """A future ``fin.comparison.v2`` row must not be rendered as if it
    were v1 — the export is BOUND to the schema it understands."""
    alien = dict(_comparison_envelope(), schema="fin.comparison.v2")
    with pytest.raises(UnsupportedComparisonSchemaError) as exc:
        build_export(
            runs=[_run("run-1", comparisons=[alien])],
            kind="comparison",
            thread_id=THREAD_ID,
        )
    assert exc.value.code == "unsupported_comparison_schema"


# ----- comparison export (SC6 positive) -----


def test_comparison_csv_carries_the_envelope_cells_and_deltas() -> None:
    envelope = _comparison_envelope()
    export = build_export(
        runs=[_run("run-1", comparisons=[envelope])],
        kind="comparison",
        thread_id=THREAD_ID,
    )

    assert export.media_type == "text/csv; charset=utf-8"
    assert export.filename.endswith(".csv")
    assert "run-1"[:8] in export.filename
    body = export.body
    # Bound to the T3 schema, and the concept it compared.
    assert COMPARISON_SCHEMA in body
    assert envelope["concept"] in body
    # Column labels and the cell ``display`` strings the tool computed —
    # the export renders, it never formats a number itself.
    for column in envelope["columns"]:
        assert column["label"] in body
    for row in envelope["rows"]:
        assert row["label"] in body
        for cell in row["cells"]:
            assert cell["display"] in body
    # Deltas ride their own section using the envelope's own display
    # string (CONTEXT: the export does not compute the delta).
    for delta in envelope["deltas"]:
        assert delta["display"] in body
        assert delta["direction"] in body
    # The trail back to the filing.
    for locator in envelope["locators"]:
        assert locator["document_id"] in body
        assert str(locator["page"]) in body


def test_filename_never_ends_on_a_separator() -> None:
    # A run id whose 8-character prefix lands on a hyphen must not leak a
    # dangling separator into the download name.
    export = build_export(
        runs=[_run("t4-live-comparison", comparisons=[_comparison_envelope()])],
        kind="comparison",
        thread_id=THREAD_ID,
    )

    assert export.filename == "fin-comparison-t4-live.csv"


def test_comparison_json_export_is_the_envelope_verbatim() -> None:
    envelope = _comparison_envelope()
    export = build_export(
        runs=[_run("run-1", comparisons=[envelope])],
        kind="comparison",
        thread_id=THREAD_ID,
        fmt="json",
    )

    assert export.media_type == "application/json; charset=utf-8"
    payload = json.loads(export.body)
    assert payload["thread_id"] == THREAD_ID
    assert payload["run_id"] == "run-1"
    assert payload["kind"] == "comparison"
    assert payload["comparisons"] == [envelope]


def test_comparison_export_defaults_to_the_latest_run_that_has_one() -> None:
    older = _run("run-old", comparisons=[dict(_comparison_envelope(), concept="OLD")])
    newer = _run("run-new", comparisons=[dict(_comparison_envelope(), concept="NEW")])
    no_comparison = _run("run-plain")

    export = build_export(
        runs=[older, newer, no_comparison], kind="comparison", thread_id=THREAD_ID
    )

    assert export.run_id == "run-new"
    assert "NEW" in export.body
    assert "OLD" not in export.body


def test_explicit_run_id_selects_that_run(
) -> None:
    older = _run("run-old", comparisons=[dict(_comparison_envelope(), concept="OLD")])
    newer = _run("run-new", comparisons=[dict(_comparison_envelope(), concept="NEW")])

    export = build_export(
        runs=[older, newer],
        kind="comparison",
        thread_id=THREAD_ID,
        run_id="run-old",
    )

    assert export.run_id == "run-old"
    assert "OLD" in export.body


def test_export_of_every_comparison_the_run_produced() -> None:
    """A turn may call ``compare_facts`` more than once; dropping all but
    one would silently lose evidence the operator watched arrive."""
    first = dict(_comparison_envelope(), concept="us-gaap:Revenues")
    second = dict(_comparison_envelope(), concept="us-gaap:GrossProfit")

    export = build_export(
        runs=[_run("run-1", comparisons=[first, second])],
        kind="comparison",
        thread_id=THREAD_ID,
    )

    assert "us-gaap:Revenues" in export.body
    assert "us-gaap:GrossProfit" in export.body


# ----- csv layout (byte-level) -----


AWKWARD_COMPARISON = {
    "schema": COMPARISON_SCHEMA,
    "concept": "us-gaap:Revenues",
    "axis": "period",
    "unit": "USD",
    "scale": "millions",
    # Second column has no ``label``: the key is the fallback.
    "columns": [{"key": "2022-Q3", "label": "2022-Q3"}, {"key": "2023-Q3"}],
    "rows": [
        {
            "key": "AAPL",
            "label": "AAPL",
            "cells": [{"display": "194,140"}, {"display": "—"}],
        },
        # No ``label``, a display holding a comma AND quotes, and a cell
        # with no display at all.
        {"key": "MSFT", "cells": [{"display": '1,2 "x"'}, {}]},
    ],
    "deltas": [
        {
            "from": {"row": "AAPL", "column": "2022-Q3"},
            "to": {"row": "AAPL", "column": "2023-Q3"},
            "direction": "up",
            "display": "+99,647 (+51.33%)",
        },
        {"direction": "flat"},
    ],
    "locators": [{"document_id": "AAPL-2022Q3", "version": "a7a0", "page": 4}, {}],
}

BARE_COMPARISON = {"schema": COMPARISON_SCHEMA, "concept": "us-gaap:GrossProfit"}

EXPECTED_CSV = (
    "schema,fin.comparison.v1\n"
    "concept,us-gaap:Revenues\n"
    "axis,period\n"
    "unit,USD\n"
    "scale,millions\n"
    "\n"
    "row,2022-Q3,2023-Q3\n"
    'AAPL,"194,140",—\n'
    'MSFT,"1,2 ""x""",\n'
    "\n"
    "from_row,from_column,to_row,to_column,direction,delta\n"
    'AAPL,2022-Q3,AAPL,2023-Q3,up,"+99,647 (+51.33%)"\n'
    ",,,,flat,\n"
    "\n"
    "document_id,version,page\n"
    "AAPL-2022Q3,a7a0,4\n"
    ",,\n"
    "\n"
    "schema,fin.comparison.v1\n"
    "concept,us-gaap:GrossProfit\n"
    "axis,\n"
    "unit,\n"
    "scale,\n"
    "\n"
    "row\n"
)


def test_comparison_csv_is_byte_stable() -> None:
    """The whole document, byte for byte.

    The per-section assertions above pass on any layout that merely
    CONTAINS the strings; this one pins the layout itself — section
    order, the single blank separator row, the key fallbacks, and the
    quoting — so a refactor of the writer cannot silently reshape a
    file operators already parse downstream.
    """
    export = build_export(
        runs=[_run("run-1", comparisons=[AWKWARD_COMPARISON, BARE_COMPARISON])],
        kind="comparison",
        thread_id=THREAD_ID,
    )

    assert export.body == EXPECTED_CSV


def test_an_absent_section_costs_no_blank_row() -> None:
    """A comparison with no deltas must not trail the padding of the
    section it does not have — an empty section is skipped WITH its
    separator, not left as a blank row a CSV reader then sees."""
    no_deltas = {k: v for k, v in AWKWARD_COMPARISON.items() if k != "deltas"}

    export = build_export(
        runs=[_run("run-1", comparisons=[no_deltas])],
        kind="comparison",
        thread_id=THREAD_ID,
    )

    assert export.body.endswith(
        "document_id,version,page\nAAPL-2022Q3,a7a0,4\n,,\n"
    )
    assert "from_row" not in export.body
    assert "\n\n\n" not in export.body


# ----- citation bundle -----


def test_citations_csv_has_a_header_and_one_row_per_citation() -> None:
    export = build_export(
        runs=[_run("run-1", citations=CITATIONS)],
        kind="citations",
        thread_id=THREAD_ID,
    )

    lines = [line for line in export.body.splitlines() if line.strip()]
    assert lines[0] == "document_id,version,page,quote"
    assert len(lines) == 1 + len(CITATIONS)
    assert CITATIONS[0]["quote"] in export.body


def test_citations_json_export_carries_the_run_identity() -> None:
    export = build_export(
        runs=[_run("run-1", citations=CITATIONS)],
        kind="citations",
        thread_id=THREAD_ID,
        fmt="json",
    )

    payload = json.loads(export.body)
    assert payload["run_id"] == "run-1"
    assert payload["citations"] == CITATIONS


def test_a_quote_with_a_comma_stays_one_csv_field() -> None:
    """CSV quoting is the ONE place an export can silently corrupt the
    evidence — a citation quote is prose and will contain commas."""
    citation = dict(CITATIONS[0], quote='He said, "revenue rose", loudly')
    export = build_export(
        runs=[_run("run-1", citations=[citation])],
        kind="citations",
        thread_id=THREAD_ID,
    )

    import csv
    import io

    rows = list(csv.reader(io.StringIO(export.body)))
    assert rows[1][3] == citation["quote"]
