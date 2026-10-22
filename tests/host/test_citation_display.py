"""Display-only source labels must come from trusted admitted evidence."""

from fin.corpus.extracted import Locator
from fin.host.grounded.citation_gate import EvidenceItem
from fin.host.run_payload import citation_sections_from_evidence


def citation(**overrides):
    row = {
        "kind": "company",
        "document_id": "ZEUS-2024Q3",
        "attachment_id": "",
        "version": "v1",
        "unit": 2,
        "display": "page",
        "filename": "",
        "quote": "A sufficiently long verified passage from this source.",
    }
    row.update(overrides)
    return row


def evidence(unit_text: str) -> EvidenceItem:
    return EvidenceItem(
        locator=Locator(document_id="ZEUS-2024Q3", version="v1", unit=2),
        unit_text=unit_text,
    )


def test_display_section_comes_from_the_matched_trusted_unit_text() -> None:
    sections = citation_sections_from_evidence(
        [citation()],
        [evidence("ZEUS Annual Results · Page 2\nSection 3: Outlook\nDetails")],
    )

    assert sections == ["Section 3"]


def test_display_section_does_not_come_from_a_model_supplied_citation_key() -> None:
    row = citation(section="Section 99")

    sections = citation_sections_from_evidence(
        [row],
        [evidence("ZEUS Annual Results · Page 2\nSection 3: Outlook\nDetails")],
    )

    assert sections == ["Section 3"]


def test_missing_or_ambiguous_trusted_section_has_no_display_value() -> None:
    missing = citation_sections_from_evidence([citation()], [evidence("A plain passage")])
    ambiguous = citation_sections_from_evidence(
        [citation()],
        [
            evidence("ZEUS Annual Results · Page 2\nSection 2: Results"),
            evidence("ZEUS Annual Results · Page 2\nSection 3: Outlook"),
        ],
    )

    assert missing == [None]
    assert ambiguous == [None]


def test_database_rows_do_not_receive_document_sections() -> None:
    ledger = citation(
        kind="database-row",
        document_id="",
        display="record",
        database_row={"source": "ledger", "record_key": {"id": "1"}},
    )

    assert citation_sections_from_evidence([ledger], []) == [None]
