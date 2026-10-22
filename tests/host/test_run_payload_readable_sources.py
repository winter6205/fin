"""Readable default answers keep their verified citation navigation data."""

from __future__ import annotations

from fin.host.grounded.citation_gate import (
    DATABASE_ROW_KIND,
    DATABASE_ROW_SNAPSHOT_KEY,
    Citation,
)
from fin.host import run_payload


_DOCUMENT_ID = "AAPL-2022Q3"
_VERSION = "synthetic-catalog-version"


def _citation(*, version: str = _VERSION) -> Citation:
    return Citation(
        kind="company",
        document_id=_DOCUMENT_ID,
        attachment_id="",
        version=version,
        unit=4,
        display="page",
        filename="",
        quote="Total net sales 82,959 81,434 304,182 282,457",
    )


def test_admitted_answer_replaces_only_the_admitted_exact_document_id(
    monkeypatch,
) -> None:
    """The default answer is readable while the citation remains addressable."""
    monkeypatch.setattr(run_payload, "corpus_is_finance", lambda: True)
    monkeypatch.setattr(
        run_payload,
        "list_filings",
        lambda **_kwargs: {
            "filings": [
                {
                    "document_id": _DOCUMENT_ID,
                    "company": "Apple Inc.",
                    "period": "2022 Q3",
                    "version": _VERSION,
                    "page_count": 25,
                    "form": "10-Q",
                }
            ]
        },
    )

    payload = run_payload._run_to_response_payload(
        {
            "status": "done",
            "answer": (
                "Per AAPL-2022Q3 page 4: the table shows total net sales of "
                "$82,959 million. Total net sales 82,959 81,434 304,182 282,457."
            ),
            "citations": [_citation()],
        }
    )

    assert payload["answer"].startswith("Per Apple Inc. · 2022 Q3 page 4:")
    assert _DOCUMENT_ID not in payload["answer"]
    assert "10-Q" not in payload["answer"]
    assert payload["citations"] == [
        {
            "kind": "company",
            "document_id": _DOCUMENT_ID,
            "attachment_id": "",
            "version": _VERSION,
            "unit": 4,
            "display": "page",
            "filename": "",
            "quote": "Total net sales 82,959 81,434 304,182 282,457",
        }
    ]


def test_citation_quote_is_projected_with_the_answer(monkeypatch) -> None:
    """A quote naming its source must still match the readable answer."""
    monkeypatch.setattr(run_payload, "corpus_is_finance", lambda: True)
    monkeypatch.setattr(
        run_payload,
        "list_filings",
        lambda **_kwargs: {
            "filings": [
                {
                    "document_id": _DOCUMENT_ID,
                    "company": "Apple Inc.",
                    "period": "2022 Q3",
                    "version": _VERSION,
                    "page_count": 25,
                    "form": "10-Q",
                }
            ]
        },
    )
    citation = Citation(
        kind="company",
        document_id=_DOCUMENT_ID,
        attachment_id="",
        version=_VERSION,
        unit=4,
        display="page",
        filename="",
        quote=(
            "Unused leave follows the 5-day limit in "
            f"{_DOCUMENT_ID}, Section 1."
        ),
    )

    payload = run_payload._run_to_response_payload(
        {
            "status": "done",
            "answer": (
                "The guide says: Unused leave follows the 5-day limit in "
                "Apple Inc. · 2022 Q3, Section 1."
            ),
            "citations": [citation],
        }
    )

    quote = payload["citations"][0]["quote"]
    assert quote == (
        "Unused leave follows the 5-day limit in "
        "Apple Inc. · 2022 Q3, Section 1."
    )
    assert quote in payload["answer"]


def test_catalog_version_mismatch_uses_the_opaque_source_label(monkeypatch) -> None:
    """An unmatched catalog row never supplies a candidate title."""
    monkeypatch.setattr(run_payload, "corpus_is_finance", lambda: True)
    monkeypatch.setattr(
        run_payload,
        "list_filings",
        lambda **_kwargs: {
            "filings": [
                {
                    "document_id": _DOCUMENT_ID,
                    "company": "Wrong Version Company",
                    "period": "2022 Q3",
                    "version": "another-version",
                    "page_count": 25,
                    "form": "10-Q",
                }
            ]
        },
    )

    payload = run_payload._run_to_response_payload(
        {
            "status": "done",
            "answer": f"Per {_DOCUMENT_ID} page 4: verified sales data follows.",
            "citations": [_citation()],
        }
    )

    assert payload["answer"] == "Per 资料来源 page 4: verified sales data follows."
    assert _DOCUMENT_ID not in payload["answer"]
    assert payload["citations"][0]["document_id"] == _DOCUMENT_ID


def test_admitted_attachment_id_uses_its_trusted_filename(monkeypatch) -> None:
    """Attachment navigation keeps its identity while prose uses its filename."""
    attachment_id = "att-private-9b4d"
    filename = "quarter-results.pdf"
    citation = Citation(
        kind="attachment",
        document_id="",
        attachment_id=attachment_id,
        version="7",
        unit=2,
        display="page",
        filename=filename,
        quote="Revenue was 12 million dollars for the quarter.",
    )

    payload = run_payload._run_to_response_payload(
        {
            "status": "done",
            "answer": (
                f"Per {attachment_id} page 2: Revenue was 12 million dollars "
                "for the quarter."
            ),
            "citations": [citation],
        }
    )

    assert payload["answer"].startswith(f"Per {filename} page 2:")
    assert attachment_id not in payload["answer"]
    assert payload["citations"][0]["attachment_id"] == attachment_id
    assert payload["citations"][0]["version"] == "7"


def test_ticker_period_fallback_keeps_same_period_sources_distinguishable(
    monkeypatch,
) -> None:
    """Catalog tickers disambiguate same-period filings without document IDs."""
    first_id, second_id = "AAPL-2023Q3", "MSFT-2023Q3"
    first_version, second_version = "first-v1", "second-v1"
    monkeypatch.setattr(run_payload, "corpus_is_finance", lambda: True)
    monkeypatch.setattr(
        run_payload,
        "list_filings",
        lambda **_kwargs: {
            "filings": [
                {
                    "document_id": first_id,
                    "company": "AAPL",
                    "period": "2023 Q3",
                    "version": first_version,
                },
                {
                    "document_id": second_id,
                    "company": "MSFT",
                    "period": "2023 Q3",
                    "version": second_version,
                },
            ]
        },
    )
    citations = [
        Citation(
            kind="company",
            document_id=document_id,
            attachment_id="",
            version=version,
            unit=4,
            display="page",
            filename="",
            quote="Verified financial data is shown in the table.",
        )
        for document_id, version in (
            (first_id, first_version),
            (second_id, second_version),
        )
    ]

    payload = run_payload._run_to_response_payload(
        {
            "status": "done",
            "answer": f"Compare {first_id} with {second_id}.",
            "citations": citations,
        }
    )

    assert payload["answer"] == "Compare AAPL · 2023 Q3 with MSFT · 2023 Q3."
    assert first_id not in payload["answer"]
    assert second_id not in payload["answer"]
    assert [row["document_id"] for row in payload["citations"]] == [
        first_id,
        second_id,
    ]
    assert [row["version"] for row in payload["citations"]] == [
        first_version,
        second_version,
    ]


def test_citation_to_wire_reads_a_mapping_restored_from_the_checkpoint() -> None:
    """A checkpoint restores ``Citation`` as a plain mapping, not the dataclass.

    The replay path feeds the persisted citation rows back through the same
    shaper, so both forms must publish the identical wire shape.
    """
    restored = {
        "kind": "company",
        "document_id": _DOCUMENT_ID,
        "attachment_id": "",
        "version": _VERSION,
        "unit": 4,
        "display": "page",
        "filename": "",
        "quote": "Total net sales 82,959 81,434 304,182 282,457",
    }

    assert run_payload.citation_to_wire(restored) == run_payload.citation_to_wire(
        _citation()
    )


def test_citation_to_wire_defaults_a_mapping_missing_wire_fields_to_empty_strings() -> None:
    """A partial replayed row publishes the same empty slots the dataclass does.

    A ``Citation`` always carries every wire field, so a field absent from a
    checkpoint-restored mapping must read as the field's own empty default
    rather than as ``None``: the two forms publish ONE wire shape, not a
    second one with nulls in it. The slots are defaulted here rather than left
    to every reader, exactly as the dataclass declares them.
    """
    restored = {
        "kind": "company",
        "document_id": _DOCUMENT_ID,
        "version": _VERSION,
        "quote": "Total net sales 82,959 81,434 304,182 282,457",
    }

    wire = run_payload.citation_to_wire(restored)

    assert wire == {
        "kind": "company",
        "document_id": _DOCUMENT_ID,
        "attachment_id": "",
        "version": _VERSION,
        "unit": "",
        "display": "",
        "filename": "",
        "quote": "Total net sales 82,959 81,434 304,182 282,457",
    }
    assert None not in wire.values()


def test_citation_to_wire_keeps_the_database_row_snapshot_for_a_mapping() -> None:
    """A ledger row replayed from its mapping still carries its snapshot."""
    restored = {
        "kind": DATABASE_ROW_KIND,
        "document_id": "",
        "attachment_id": "",
        "version": "2026-03-01T09:14:22Z",
        "unit": 1,
        "display": "record",
        "filename": "",
        "quote": "Contract C-2291 expires 2026-03-14 for Northwind Logistics",
        "database_source": "ledger_ro.contract_expiry",
        "database_record_key": [["contract_id", "C-2291"]],
        "database_fields": [["expiry_date", "2026-03-14"]],
    }

    wire = run_payload.citation_to_wire(restored)

    assert wire[DATABASE_ROW_SNAPSHOT_KEY] == {
        "source": "ledger_ro.contract_expiry",
        "record_key": {"contract_id": "C-2291"},
        "queried_at": "2026-03-01T09:14:22Z",
        "fields": {"expiry_date": "2026-03-14"},
        "summary": "Contract C-2291 expires 2026-03-14 for Northwind Logistics",
    }
