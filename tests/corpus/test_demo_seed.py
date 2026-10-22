"""The demo seed is one source, read back through the real catalog seam.

The demo rows are no longer a hand-written trio. Per specs/demo-seed.md item 4
(last bullets) they are *generated from the fixture*: one row per ``source_ref``
in ``fixtures/demo-corpus/``, with the row's form and ``page_count`` taken from
the fixture's own ``metadata`` rather than from constants. So these tests read
the fixture files off disk and compare against that, instead of asking
``demo_catalog_rows`` what it thinks it produces -- a row that drifts from its
document fails here rather than agreeing with itself.

The ``multi_form_corpus`` test fixture and the ``seed:demo`` command both call
``seed_demo_catalog``, so this asserts the single property that makes that
sharing safe: the rows that get written are the rows the fixture describes, and
they round-trip through ``record_filings`` / ``list_filings`` with the form
contract intact. ``tmp_path`` is passed explicitly -- the development catalog is
never a test target.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fin.corpus.catalog import list_filings
from fin.corpus.demo_seed import demo_catalog_rows, seed_demo_catalog

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "demo-corpus"

#: One row per fixture document. Pinned so a seed that silently finds no
#: documents fails loudly instead of passing on an empty table.
EXPECTED_ROW_COUNT = 20
EXPECTED_CATEGORY_COUNTS = {
    "employee_handbook": 3,
    "product_description": 3,
    "policy_process": 3,
    "meeting_minutes": 3,
    "financial_report_summary": 2,
    "contract_template": 2,
    "faq": 2,
    "project_weekly_report": 2,
}


def _fixture_documents() -> dict[str, dict[str, Any]]:
    """``source_ref`` -> ``metadata`` for every fixture document, read off disk.

    ``expected.json`` is the question set that travels with the corpus, not a
    document, so it is skipped -- the same distinction the seed makes.
    """
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        if path.name == "expected.json":
            continue
        envelope = json.loads(path.read_text(encoding="utf-8"))
        documents[str(envelope["source_ref"])] = dict(envelope["metadata"])
    return documents


def _fixture_envelopes() -> list[dict[str, Any]]:
    """Read the authored documents in the same order as the seed loader."""
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE_DIR.glob("zeus-*.json"))
    ]


def test_demo_corpus_has_twenty_documents_with_page_section_coverage() -> None:
    """The corpus is diverse, locatable, mixed-length, and question-ready."""
    envelopes = _fixture_envelopes()
    documents = {str(envelope["source_ref"]): envelope for envelope in envelopes}

    assert len(documents) == EXPECTED_ROW_COUNT
    assert Counter(
        str(envelope["metadata"]["document_type"]) for envelope in envelopes
    ) == EXPECTED_CATEGORY_COUNTS
    assert {int(envelope["metadata"]["page_count"]) for envelope in envelopes} == {
        1,
        2,
        3,
    }

    chunk_lengths: list[int] = []
    cross_referenced_documents = 0
    source_refs = set(documents)
    for envelope in envelopes:
        title = str(envelope["title"])
        chunks = envelope["chunks"]
        assert envelope["metadata"]["page_count"] == len(chunks)
        assert [chunk["index"] for chunk in chunks] == list(range(len(chunks)))
        assert envelope["content"] == "\n\n".join(
            str(chunk["content"]) for chunk in chunks
        )
        for page_number, chunk in enumerate(chunks, start=1):
            lines = str(chunk["content"]).splitlines()
            assert lines[0] == f"{title} · Page {page_number}"
            assert lines[1].startswith("Section ")
            chunk_lengths.append(len(str(chunk["content"])))
        source_ref = str(envelope["source_ref"])
        if any(ref in str(envelope["content"]) for ref in source_refs - {source_ref}):
            cross_referenced_documents += 1

    assert min(chunk_lengths) < 450
    assert max(chunk_lengths) > 900
    assert max(chunk_lengths) > min(chunk_lengths) * 2
    assert cross_referenced_documents >= 2

    expected = json.loads((FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))
    questions = expected["questions"]
    by_id = {str(question["id"]): question for question in questions}
    assert len(questions) == 15
    assert {"q1", "q2", "q3", "q4", "q5", "q6"} <= set(by_id)
    answerable_categories = {
        str(documents[str(question["source_ref"])]["metadata"]["document_type"])
        for question in questions
        if question["expected_kind"] == "answerable"
        and question.get("source_ref") is not None
    }
    assert answerable_categories == set(EXPECTED_CATEGORY_COUNTS)
    assert by_id["q6"]["expected_kind"] == "refusal"
    assert by_id["q6"]["source_ref"] is None
    assert "advanced propulsion division" not in "\n".join(
        str(envelope["content"]) for envelope in envelopes
    ).casefold()

    question_modes = {
        str(question["search_mode"])
        for question in questions
        if question.get("search_mode") is not None
    }
    assert question_modes == {"lexical", "graph", "global", "sql"}
    cross_document_questions = [
        question for question in questions if question.get("additional_sources")
    ]
    assert cross_document_questions
    assert any(question.get("follow_up") for question in questions)
    sql_boundary = by_id["q14"]
    assert sql_boundary["expected_kind"] == "refusal"
    assert sql_boundary["source_ref"] is None
    sql_answer = by_id["q15"]
    assert sql_answer["expected_kind"] == "answerable"
    assert sql_answer["search_mode"] == "sql"
    assert sql_answer["source_ref"] is None
    assert sql_answer["rows"] == [
        {
            "contract_id": "C-2315",
            "counterparty": "Northwind Logistics",
            "expiry_date": "2026-01-24",
            "notice_by": "2026-01-17",
        },
        {
            "contract_id": "C-2302",
            "counterparty": "Meridian Analytics GmbH",
            "expiry_date": "2026-02-28",
            "notice_by": "2025-12-30",
        },
        {
            "contract_id": "C-2291",
            "counterparty": "Northwind Logistics",
            "expiry_date": "2026-03-14",
            "notice_by": "2026-02-12",
        },
        {
            "contract_id": "C-2024Q1",
            "counterparty": "Halcyon Print Works",
            "expiry_date": "2026-04-30",
            "notice_by": "2026-03-31",
        },
    ]
    assert sql_answer["key_facts"] == [
        str(value) for row in sql_answer["rows"] for value in row.values()
    ]

    def cited_text(source: dict[str, Any]) -> str:
        source_ref = str(source["source_ref"])
        envelope = documents[source_ref]
        title_page, section_number = str(source["locator"]).rsplit(
            ", Section ", maxsplit=1
        )
        assert title_page.startswith(f"{envelope['title']} · Page ")
        for chunk in envelope["chunks"]:
            lines = str(chunk["content"]).splitlines()
            if lines[0] == title_page and lines[1].startswith(
                f"Section {section_number}"
            ):
                return str(chunk["content"])
        raise AssertionError(f"locator does not identify a chunk: {source['locator']}")

    for question in questions:
        if question["expected_kind"] != "answerable" or question.get("source_ref") is None:
            continue
        evidence = "\n".join(
            [cited_text(question)]
            + [
                cited_text(source)
                for source in question.get("additional_sources", [])
            ]
        ).casefold()
        assert all(str(fact).casefold() in evidence for fact in question["key_facts"])

        follow_up = question.get("follow_up")
        if follow_up:
            follow_up_evidence = cited_text(follow_up).casefold()
            assert all(
                str(fact).casefold() in follow_up_evidence
                for fact in follow_up["key_facts"]
            )


def test_demo_rows_cover_each_fixture_document_for_the_synth_issuer() -> None:
    """One row per fixture document, all issuer ZEUS, each with its own form."""
    documents = _fixture_documents()
    rows = demo_catalog_rows()

    assert len(documents) == EXPECTED_ROW_COUNT
    assert len(rows) == EXPECTED_ROW_COUNT
    assert {row["document_id"] for row in rows} == set(documents)
    assert {str(metadata["company"]) for metadata in documents.values()} == {"ZEUS"}
    assert {row["company"] for row in rows} == {"ZEUS"}

    by_id = {row["document_id"]: row for row in rows}
    for source_ref, metadata in documents.items():
        # The catalog `form` column is EDGAR-shaped with exact-match filter
        # semantics, so it carries the fixture's `form` (10-K / 8-K / 10-Q),
        # not the prose `document_type`. Writing document_type here would make
        # list_filings(form="10-K") return nothing.
        assert by_id[source_ref]["form"] == str(metadata["form"])
        assert by_id[source_ref]["page_count"] == int(metadata["page_count"])

    # Every seeded row carries a real, non-empty EDGAR form ...
    assert all(isinstance(row["form"], str) and row["form"] for row in rows)
    # ... drawn from the fixture's own vocabulary, and that vocabulary really
    # discriminates: the seeded rows span more than one form, so a form filter
    # is a working selector rather than a column that happens to be populated.
    declared_forms = {str(metadata["form"]) for metadata in documents.values()}
    assert {row["form"] for row in rows} == declared_forms
    assert 1 < len(declared_forms) < EXPECTED_ROW_COUNT


def test_seed_writes_rows_and_reads_back_through_the_catalog(tmp_path: Path) -> None:
    """The seed uses the ingest seam, so a read returns exactly what it wrote."""
    catalog_path = tmp_path / "catalog.json"
    documents = _fixture_documents()

    written, resolved = seed_demo_catalog(catalog_path)

    assert written == EXPECTED_ROW_COUNT
    assert resolved == catalog_path

    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert len(filings) == EXPECTED_ROW_COUNT
    assert {f["document_id"] for f in filings} == set(documents)
    assert {f["form"] for f in filings} == {
        str(metadata["form"]) for metadata in documents.values()
    }

    by_id = {f["document_id"]: f for f in filings}
    for source_ref, metadata in documents.items():
        assert by_id[source_ref]["page_count"] == int(metadata["page_count"])


def test_form_filter_returns_the_seeded_rows_for_each_form(tmp_path: Path) -> None:
    """A form filter round-trips each seeded row through the real catalog.

    This is the positive case that keeps the filter honest: every form the
    fixture declares must return a non-empty, exact set of rows, while an
    unknown form still yields empty hits rather than raising (ADR 0047).
    """
    catalog_path = tmp_path / "catalog.json"
    documents = _fixture_documents()
    seed_demo_catalog(catalog_path)

    rows_by_form: dict[str, set[str]] = {}
    for source_ref, metadata in documents.items():
        rows_by_form.setdefault(str(metadata["form"]), set()).add(source_ref)

    assert rows_by_form  # the fixture declares at least one form
    for form, source_refs in rows_by_form.items():
        assert {
            f["document_id"]
            for f in list_filings(form=form, catalog_path=catalog_path)["filings"]
        } == source_refs

    assert list_filings(form="not-a-form", catalog_path=catalog_path)["filings"] == []
