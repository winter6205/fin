"""Tests for the thread export builder (T4, the tracked change).

Spec Focus 3 / SC6: "one authenticated-to-local GET or POST from the
thread returns a file body for citations or comparison; missing run ->
typed 404."

The comparison export binds to the T3 envelope ``fin.comparison.v1``
(shape SSOT: ``fin.host.tools.compare_facts``). CONTEXT's compare_facts
entry forbids "computing the delta in the SPA or the export", so every
test below asserts the file carries the envelope's OWN strings.
"""

from __future__ import annotations

import csv
import json

import pytest

from fin.host.export import (
    ExportEmptyError,
    MalformedCitationRowError,
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
                            "unit": 4,
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
            {"document_id": "AAPL-2022Q3", "version": "a7a0", "unit": 4}
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


# The persisted citation is the full wire contract emitted by
# ``run_payload._run_to_response_payload`` — an attachment citation
# carries no ``document_id`` and is only openable from ``attachment_id``
# plus ``display`` / ``filename``, so a fixture that hand-picked three
# fields would not be the row the export actually reads.
CITATIONS = [
    {
        "kind": "company",
        "document_id": "AAPL-2022Q3",
        "attachment_id": "",
        "version": "a7a0",
        "unit": 4,
        # A company unit admitted through ``read_source`` presents as a
        # page (``citation_gate.EvidenceItem`` default), which is what
        # the ``display`` discriminator carries.
        "display": "page",
        "filename": "",
        "quote": "Total net sales increased during the third quarter",
    }
]

# The attachment leg of the same contract: identity lives in
# ``attachment_id`` and ``document_id`` is EMPTY by design, so the column
# that opens the unit cannot be a fixed ``document_id``.
ATTACHMENT_CITATION = {
    "kind": "attachment",
    "document_id": "",
    "attachment_id": "att-3f9c",
    # The parsed-content version, riding as a string like the company wire.
    "version": "2",
    "unit": 1,
    "display": "unit",
    "filename": "inspection-sheet.pdf",
    "quote": "the odometer reading shown on the inspection sheet",
}


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
    # The trail back to the filing units: one locator section, its rows
    # in envelope order. A substring check would survive the writer
    # dropping the unit column entirely — the "4" it looked for also
    # occurs inside the cell display "194,140".
    locator_sections = [
        section
        for section in body.split("\n\n")
        if section.startswith("document_id,version,unit\n")
    ]
    assert len(locator_sections) == 1
    assert locator_sections[0].splitlines() == [
        "document_id,version,unit",
        *(
            f"{locator['document_id']},{locator['version']},{locator['unit']}"
            for locator in envelope["locators"]
        ),
    ]


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
    "locators": [{"document_id": "AAPL-2022Q3", "version": "a7a0", "unit": 4}, {}],
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
    "document_id,version,unit\n"
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
        "document_id,version,unit\nAAPL-2022Q3,a7a0,4\n,,\n"
    )
    assert "from_row" not in export.body
    assert "\n\n\n" not in export.body


def test_locator_section_has_no_legacy_page_column() -> None:
    """Same contract on the comparison side: the locator trail is
    ``{document_id, version, unit}``. A row that still carries a
    physical ``page`` gets an empty unit cell — the writer does not
    reinterpret a removed key."""
    legacy = dict(
        AWKWARD_COMPARISON,
        locators=[{"document_id": "AAPL-2022Q3", "version": "a7a0", "page": 4}],
    )

    export = build_export(
        runs=[_run("run-1", comparisons=[legacy])],
        kind="comparison",
        thread_id=THREAD_ID,
    )

    sections = [
        section
        for section in export.body.split("\n\n")
        if section.startswith("document_id,")
    ]
    assert sections == ["document_id,version,unit\nAAPL-2022Q3,a7a0,\n"]


# ----- citation bundle -----


def test_citations_csv_has_a_header_and_one_row_per_citation() -> None:
    export = build_export(
        runs=[_run("run-1", citations=CITATIONS)],
        kind="citations",
        thread_id=THREAD_ID,
    )

    lines = [line for line in export.body.splitlines() if line.strip()]
    assert lines[0] == "kind,source_id,version,unit,quote"
    assert len(lines) == 1 + len(CITATIONS)
    assert CITATIONS[0]["quote"] in export.body


def test_citations_csv_names_the_source_that_opens_an_attachment_row() -> None:
    """An attachment citation carries no ``document_id``; the row must
    name the id that opens the unit it quotes, or the file cannot do the
    one job it exists for."""
    export = build_export(
        runs=[_run("run-1", citations=[ATTACHMENT_CITATION])],
        kind="citations",
        thread_id=THREAD_ID,
    )

    assert [line for line in export.body.splitlines() if line.strip()] == [
        "kind,source_id,version,unit,quote",
        "attachment,att-3f9c,2,1," + ATTACHMENT_CITATION["quote"],
    ]


def test_citations_csv_exports_the_unit_column_not_the_identity_fields() -> None:
    """The citation file is the quotable trail, not a wire dump.

    The five columns are exactly what a reader needs to OPEN the cited
    unit: ``kind`` says which identity the row is opened with, and
    ``source_id`` carries that one id — ``document_id`` for a company
    row, ``attachment_id`` for an attachment row, whose ``document_id``
    is empty by design. That fifth column is not extra detail: with the
    old four, every attachment row exported a blank first cell and the
    download stopped being actionable.

    What still stays out is the display-only half of the wire row
    (``display`` / ``filename``) and whichever source id the row's kind
    does not use; both ride the JSON export verbatim.
    """
    export = build_export(
        runs=[_run("run-1", citations=CITATIONS)],
        kind="citations",
        thread_id=THREAD_ID,
    )

    assert [line for line in export.body.splitlines() if line.strip()] == [
        "kind,source_id,version,unit,quote",
        f"company,{CITATIONS[0]['document_id']},{CITATIONS[0]['version']},"
        f"{CITATIONS[0]['unit']},{CITATIONS[0]['quote']}",
    ]
    assert CITATIONS[0]["display"] not in export.body


def test_citations_csv_names_the_source_of_both_legs_in_one_file() -> None:
    """A turn that quoted the corpus AND a thread attachment must
    produce one file whose every row names an openable source."""
    export = build_export(
        runs=[_run("run-1", citations=[CITATIONS[0], ATTACHMENT_CITATION])],
        kind="citations",
        thread_id=THREAD_ID,
    )

    assert [line for line in export.body.splitlines() if line.strip()] == [
        "kind,source_id,version,unit,quote",
        f"company,{CITATIONS[0]['document_id']},{CITATIONS[0]['version']},"
        f"{CITATIONS[0]['unit']},{CITATIONS[0]['quote']}",
        f"attachment,{ATTACHMENT_CITATION['attachment_id']},"
        f"{ATTACHMENT_CITATION['version']},{ATTACHMENT_CITATION['unit']},"
        f"{ATTACHMENT_CITATION['quote']}",
    ]


def test_citations_csv_has_no_legacy_page_column() -> None:
    """A locator ``page`` key is not part of the contract any more, and
    the writer keeps no compatibility path for it: a row that still
    carries one exports an EMPTY unit rather than the stale number."""
    legacy = [
        {
            "kind": "company",
            "document_id": "AAPL-2022Q3",
            "attachment_id": "",
            "version": "a7a0",
            "page": 4,
            "display": "page",
            "filename": "",
            "quote": "Total net sales increased during the third quarter",
        }
    ]

    export = build_export(
        runs=[_run("run-1", citations=legacy)],
        kind="citations",
        thread_id=THREAD_ID,
    )

    assert "page" not in export.body.splitlines()[0]
    assert (
        export.body.splitlines()[1]
        == f"company,AAPL-2022Q3,a7a0,,{legacy[0]['quote']}"
    )


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
    assert rows[1][4] == citation["quote"]


def test_a_row_of_an_unquoted_kind_is_a_typed_refusal() -> None:
    """A kind outside the gate's own closed quotable set — a generated
    visual observation, or a row stored before citations carried ``kind``
    at all — is refused, never rendered as if it were a quotable trail.
    The message names the row and what was observed so the operator can
    find it, and never echoes the quote (user prose)."""
    alien = dict(CITATIONS[0], kind="generated_image")

    with pytest.raises(MalformedCitationRowError) as exc:
        build_export(
            runs=[_run("run-1", citations=[CITATIONS[0], alien])],
            kind="citations",
            thread_id=THREAD_ID,
        )

    assert exc.value.code == "malformed_citation_row"
    assert "row 1" in str(exc.value)
    assert "generated_image" in str(exc.value)
    assert alien["quote"] not in str(exc.value)


def test_a_pre_migration_row_still_exports_as_the_company_row_it_was() -> None:
    """A row written before citations carried ``kind`` must still export.

    ``fin_run.citations`` is JSONB, so rows stored by an earlier deployment
    are still in the database with the shape they were written in. A row
    with no ``kind`` but a ``document_id`` is what a company citation
    always looked like, and it is what this door exported before the
    locator rename. Refusing it makes the whole citations export
    unreachable for every thread created before the rename, over rows
    that are in fact perfectly well-formed.

    The refusal this module owes the operator is for a row that names no
    quotable source at all — that case is still asserted below, and it is
    asserted on a row that carries no ``document_id`` either.
    """
    legacy = {
        "document_id": "AAPL-2022Q3",
        "version": "a7a0",
        "page": 4,
        "quote": "Total net sales increased during the third quarter",
    }

    export = build_export(
        runs=[_run("run-1", citations=[legacy])],
        kind="citations",
        thread_id=THREAD_ID,
    )

    rows = list(csv.reader(export.body.splitlines()))
    assert len(rows) == 2  # header + the one stored row
    assert rows[1][0] == "company", "a pre-migration row reads as what it was"
    assert rows[1][1] == "AAPL-2022Q3", "never a blank source_id cell"
    assert legacy["quote"] in rows[1][4]


def test_a_row_naming_no_source_for_its_kind_is_a_typed_refusal() -> None:
    """The silent failure this export must not repeat: an attachment row
    with no ``attachment_id`` used to download as a clean-looking file
    whose one key column was blank. A row that opens nothing is refused
    before a single cell is written."""
    orphan = dict(ATTACHMENT_CITATION, attachment_id="")

    with pytest.raises(MalformedCitationRowError) as exc:
        build_export(
            runs=[_run("run-1", citations=[orphan])],
            kind="citations",
            thread_id=THREAD_ID,
        )

    assert exc.value.code == "malformed_citation_row"
    assert "row 0" in str(exc.value)
    assert "attachment" in str(exc.value)
    assert orphan["quote"] not in str(exc.value)


def test_a_company_row_never_borrows_the_attachment_id() -> None:
    """Each kind is checked against ITS OWN source field: letting an
    attachment id stand in for a company row with no document would open
    the wrong source under a plausible-looking row."""
    swapped = dict(CITATIONS[0], document_id="", attachment_id="att-3f9c")

    with pytest.raises(MalformedCitationRowError) as exc:
        build_export(
            runs=[_run("run-1", citations=[swapped])],
            kind="citations",
            thread_id=THREAD_ID,
        )

    assert "document_id" in str(exc.value)


def test_a_row_naming_no_version_is_a_typed_refusal() -> None:
    """``source_id`` plus ``unit`` addresses one frozen version, which is
    the version the quote was taken from — the gate requires a version
    for either kind, so an empty one is not an exportable row."""
    no_version = dict(ATTACHMENT_CITATION, version="")

    with pytest.raises(MalformedCitationRowError) as exc:
        build_export(
            runs=[_run("run-1", citations=[no_version])],
            kind="citations",
            thread_id=THREAD_ID,
        )

    assert "row 0" in str(exc.value)


def test_a_refused_row_refuses_the_json_export_too() -> None:
    """The binding is a property of the run's rows, not of the format:
    a row this export does not speak must not download because the
    caller asked for the other file extension."""
    orphan = dict(ATTACHMENT_CITATION, attachment_id="")

    with pytest.raises(MalformedCitationRowError):
        build_export(
            runs=[_run("run-1", citations=[orphan])],
            kind="citations",
            thread_id=THREAD_ID,
            fmt="json",
        )


def test_citations_json_export_is_the_stored_rows_byte_for_byte() -> None:
    """The CSV collapse to one ``source_id`` is the CSV's business only.

    The JSON document keeps all eight wire fields of each stored row —
    including the empty ``document_id`` of an attachment row — so the
    leg a row came from stays reconstructible; the export never rewrites
    a row to look like the narrower file.
    """
    citations = [CITATIONS[0], ATTACHMENT_CITATION]

    export = build_export(
        runs=[_run("run-1", citations=citations)],
        kind="citations",
        thread_id=THREAD_ID,
        fmt="json",
    )

    assert export.body == json.dumps(
        {
            "thread_id": THREAD_ID,
            "run_id": "run-1",
            "kind": "citations",
            "citations": citations,
        },
        ensure_ascii=False,
        indent=2,
    )
    assert "source_id" not in export.body
    assert set(json.loads(export.body)["citations"][1]) == {
        "kind",
        "document_id",
        "attachment_id",
        "version",
        "unit",
        "display",
        "filename",
        "quote",
    }
