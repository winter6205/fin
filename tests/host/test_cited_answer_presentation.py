"""Cited-answer presentation, backend half (``specs/demo-seed.md`` item 6).

The user-facing answer must carry its evidence: inline superscripts
pointing at a sources list whose entries name document, section and
page, and an explicit ``资料中未找到相关内容`` when the corpus does not
support the question.

Two halves of that requirement live on very different surfaces, and
this suite pins each one where it actually is:

* The **refusal string** is shared by the model instruction and the
  terminal response mapping. ``run_payload`` maps typed no-evidence
  outcomes to that same sentence; the citation gate's ``RefuseResult``
  and the evidence contract's ``EvidenceRefusal`` remain machine codes.
  The drift guard keeps the literal defined only in the instruction
  module.

* The **citation presentation fields** keep the persisted citation
  contract: a company citation presents ``unit`` under
  ``display: "page"``, an attachment citation adds its safe
  ``filename``, and a ledger row presents NEITHER — no document, no
  page — as ``display: "record"`` with ``unit`` fixed at 1. Active
  responses may additionally derive a display-only section from trusted
  evidence text; it is not accepted from model evidence or persisted.

WHAT THIS SUITE DOES NOT CLAIM: a company citation's ``filename`` stays
empty by design, so a sources list names it by ``document_id``. The
model/evidence citation channel still refuses a supplied ``section``;
the response layer derives display metadata separately from trusted
evidence text.
"""

from __future__ import annotations

from pathlib import Path

from fin.corpus.extracted import Locator
from fin.host.agent_instructions import (
    HOST_SYSTEM_PROMPT,
    NO_EVIDENCE_REFUSAL,
    messages_for_model,
)
from fin.host.grounded.citation_gate import (
    COMPANY_KIND,
    DATABASE_ROW_DISPLAY,
    DATABASE_ROW_KIND,
    DatabaseRow,
    DoneResult,
    EvidenceItem,
    format_database_row_source_id,
    verify_citations,
)
from fin.host.evidence.evidence_contract import (
    CITATION_CHANNEL_SCHEMA,
    DATABASE_ROW_SNAPSHOT_SCHEMA,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_FIELD,
    UNSUPPORTED_EVIDENCE_CONTRACT,
    AdmissionContext,
    validate_evidence,
)
from fin.retrieve.source_read import ATTACHMENT_KIND

# The corpus the demo seed loads: a fictional company whose documents are
# enterprise material (handbook, product description, policy, minutes,
# financial-report summary). The ids below are that corpus's own shape —
# a company locator is not the v1 ``AAPL-2022Q3`` filing id, so the gate's
# locator-forgery regex cannot fire on them and the tests stay about
# presentation only.
HANDBOOK_DOC = "zeus-handbook"
HANDBOOK_VERSION = "sha256:0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"
ATTACHMENT_ID = "att-zeus-0001"
ATTACHMENT_VERSION = "17"

# A ≥32-character extractive span (the gate's default floor), so a
# document citation verifies by a verbatim copy rather than by the
# ledger rule.
HANDBOOK_QUOTE = (
    "Annual leave is granted at the operator's discretion and must be "
    "requested five working days in advance."
)
ATTACHMENT_QUOTE = (
    "The procurement policy caps single-source orders below fifty "
    "thousand yuan without finance sign-off."
)

LEDGER_VIEW = "ledger_ro.contract_expiry"
LEDGER_SUMMARY = (
    "Contract C-2291 standard notice period is 30 days."
)
LEDGER_KEY = (("contract_id", "C-2291"),)
LEDGER_QUERY_TIME = "2026-03-01T09:14:22Z"


# ----- the no-evidence refusal string (spec item 6) -----


def test_the_no_evidence_refusal_is_this_exact_string() -> None:
    """The operator requirement names the sentence literally.

    ``资料中未找到相关内容`` is what acceptance asks the answer to say, so
    it is pinned as an exact value rather than as a substring: a
    reworded refusal that still "means" the same thing fails the demo.
    """
    assert NO_EVIDENCE_REFUSAL == "资料中未找到相关内容"


def test_the_grounded_track_is_instructed_to_say_it() -> None:
    """The grounded (research) prompt carries the refusal.

    Only the grounded track answers from the corpus, so only it carries
    the sentence; a citation span cannot stand in for it.
    """
    assert NO_EVIDENCE_REFUSAL in HOST_SYSTEM_PROMPT


def test_the_assembled_grounded_prompt_carries_the_refusal_once() -> None:
    """Exactly one occurrence in the assembled system prefix.

    Two copies of one user-facing string drift: a later edit to one is
    invisible in the other, and which one the model saw depends on
    assembly order. One occurrence in the shipped prefix makes the
    duplication an immediate failure here.
    """
    messages = messages_for_model([], "grounded")
    prefix = messages[0].content
    assert isinstance(prefix, str)
    assert prefix.count(NO_EVIDENCE_REFUSAL) == 1, prefix


def test_the_refusal_string_is_defined_in_exactly_one_host_module() -> None:
    """Drift guard: no second definition anywhere under ``fin.host``.

    The instruction is the single source of truth (see the module
    docstring), the same rule the gate follows for
    ``HIDDEN_BOUNCE_MARKER``. Scanning the source rather than importing
    every module catches a copy written straight into a string literal.
    """
    host_root = Path(__file__).resolve().parents[2] / "src" / "fin" / "host"
    carriers = sorted(
        path.relative_to(host_root).as_posix()
        for path in host_root.rglob("*.py")
        if NO_EVIDENCE_REFUSAL in path.read_text(encoding="utf-8")
    )
    assert carriers == ["agent_instructions.py"], carriers


# ----- what each citation kind can present (spec item 6) -----


def _company_citation(answer: str) -> DoneResult:
    """Run the gate over one company-document answer."""
    result = verify_citations(
        answer=answer,
        evidence=[
            EvidenceItem(
                locator=Locator(
                    document_id=HANDBOOK_DOC,
                    version=HANDBOOK_VERSION,
                    unit=4,
                ),
                unit_text=HANDBOOK_QUOTE,
                kind=COMPANY_KIND,
            )
        ],
        repair_count=0,
        known_document_ids={HANDBOOK_DOC},
    )
    assert isinstance(result, DoneResult), result
    return result


def test_a_company_citation_presents_its_unit_as_a_page() -> None:
    """A company unit is presented as a page, not as a bare ordinal.

    ``display`` is what the renderer branches on (``p.4`` vs ``u.4``),
    so the page is carried by the pair, not by ``unit`` alone.
    """
    citation = _company_citation(
        f"Leave is requested five working days ahead. {HANDBOOK_QUOTE}"
    ).citations[0]

    assert citation.kind == COMPANY_KIND
    assert citation.document_id == HANDBOOK_DOC
    assert citation.unit == 4
    assert citation.display == "page"


def test_a_company_citation_names_no_document_title_and_no_section() -> None:
    """Persisted citation identity does not carry presentation labels.

    A company citation reaches the gate as a locator triple, so it has
    no title to carry: ``filename`` stays empty and the sources list
    names the document by its id. A section is derived later for active
    presentation and stays outside the citation value and stored row.
    """
    citation = _company_citation(
        f"Leave is requested five working days ahead. {HANDBOOK_QUOTE}"
    ).citations[0]

    assert citation.filename == ""
    assert "section" not in {f.name for f in citation.__dataclass_fields__.values()}


def test_an_attachment_citation_presents_its_filename_and_page() -> None:
    """The attachment leg is the one kind that carries a display name.

    The trust re-check is the caller's own store question (the gate
    holds no store), supplied here as the trusted owner of this thread's
    attachment — the same collaborator the host graph supplies.
    """
    result = verify_citations(
        answer=f"Procurement needs sign-off. {ATTACHMENT_QUOTE}",
        evidence=[
            EvidenceItem(
                locator=Locator(document_id="", version=ATTACHMENT_VERSION, unit=2),
                unit_text=ATTACHMENT_QUOTE,
                kind=ATTACHMENT_KIND,
                attachment_id=ATTACHMENT_ID,
                version_int=17,
                filename="zeus-procurement-policy.pdf",
                display="page",
            )
        ],
        repair_count=0,
        known_document_ids=set(),
        attachment_identity_current=lambda item: (
            item.attachment_id == ATTACHMENT_ID and item.version_int == 17
        ),
    )

    assert isinstance(result, DoneResult), result
    citation = result.citations[0]
    assert citation.kind == ATTACHMENT_KIND
    assert citation.filename == "zeus-procurement-policy.pdf"
    assert citation.unit == 2
    assert citation.display == "page"
    assert citation.document_id == ""


def test_a_ledger_citation_presents_neither_document_nor_page() -> None:
    """A ledger row opens through no document slot and no page.

    There is no filing behind a database row, so naming a page would
    point the citation at a position in a document that does not
    exist. ``record`` plus a fixed ``unit`` makes that absence explicit
    for a renderer instead of leaving it to a default.
    """
    row = DatabaseRow(
        source_id=format_database_row_source_id(LEDGER_VIEW, dict(LEDGER_KEY)),
        source=LEDGER_VIEW,
        record_key=LEDGER_KEY,
        queried_at=LEDGER_QUERY_TIME,
        fields=(("notice_days", "30"),),
        summary=LEDGER_SUMMARY,
    )
    result = verify_citations(
        answer=f"{LEDGER_SUMMARY} The notice period is 30 days.",
        evidence=[
            EvidenceItem(
                locator=Locator(document_id="", version="", unit=1),
                unit_text=LEDGER_SUMMARY,
                kind=DATABASE_ROW_KIND,
                display=DATABASE_ROW_DISPLAY,
                database_row=(row,),
            )
        ],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    citation = result.citations[0]
    assert citation.kind == DATABASE_ROW_KIND
    assert citation.display == DATABASE_ROW_DISPLAY
    assert citation.unit == 1
    assert citation.document_id == ""
    assert citation.attachment_id == ""


# ----- the wire contract that presentation rides on -----


def _channel(citation: dict[str, object]) -> dict[str, object]:
    return {
        "evidence_schema": CITATION_CHANNEL_SCHEMA,
        EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
        "citations": [citation],
    }


_DOCUMENT_CITATION: dict[str, object] = {
    "kind": COMPANY_KIND,
    "document_id": HANDBOOK_DOC,
    "attachment_id": "",
    "version": HANDBOOK_VERSION,
    "unit": 4,
    "display": "page",
    "filename": "",
    "quote": HANDBOOK_QUOTE,
}

_SNAPSHOT: dict[str, object] = {
    "schema": DATABASE_ROW_SNAPSHOT_SCHEMA,
    EVIDENCE_SCHEMA_VERSION_FIELD: EVIDENCE_SCHEMA_VERSION,
    "source": LEDGER_VIEW,
    "record_key": dict(LEDGER_KEY),
    "queried_at": LEDGER_QUERY_TIME,
    "fields": {"notice_days": "30"},
    "summary": LEDGER_SUMMARY,
}

_ROW_CITATION: dict[str, object] = {
    "kind": DATABASE_ROW_KIND,
    "document_id": "",
    "attachment_id": "",
    "version": LEDGER_QUERY_TIME,
    "unit": 1,
    "display": DATABASE_ROW_DISPLAY,
    "filename": "",
    "quote": LEDGER_SUMMARY,
    "database_row": _SNAPSHOT,
}

_CTX = AdmissionContext(
    resolvable_locators=frozenset({(HANDBOOK_DOC, HANDBOOK_VERSION)}),
    received_texts={(HANDBOOK_DOC, HANDBOOK_VERSION, 4): HANDBOOK_QUOTE},
)


def test_both_citation_kinds_validate_on_the_citation_channel() -> None:
    """The presentation shapes above are the ones the contract accepts.

    Each is driven through the public validator with the real harness
    context, so "the gate emits it" and "the wire admits it" cannot
    answer differently.
    """
    for citation in (_DOCUMENT_CITATION, _ROW_CITATION):
        result = validate_evidence(_channel(citation), _CTX)
        assert result.accepted, result.refusal


def test_a_ledger_citation_may_not_claim_a_page() -> None:
    """``record`` + ``unit: 1`` is the row's only legal presentation.

    A row that dressed itself as page 3 of something would put an
    invented locator in front of the user, which is the failure the
    ``display`` vocabulary exists to prevent.
    """
    for unit, display in ((3, "page"), (1, "page")):
        forged = {**_ROW_CITATION, "unit": unit, "display": display}
        result = validate_evidence(_channel(forged), _CTX)
        assert not result.accepted, (unit, display)
        assert result.refusal is not None


def test_the_citation_channel_refuses_a_section_key() -> None:
    """Model-supplied section metadata remains outside the gate contract.

    The evidence key set is frozen, so a citation carrying ``section``
    is refused as unsupported rather than trusted. The HTTP layer derives
    its separate display-only section from the full trusted evidence
    passage after this gate.
    """
    result = validate_evidence(
        _channel({**_DOCUMENT_CITATION, "section": "第三章 假期"}), _CTX
    )

    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == UNSUPPORTED_EVIDENCE_CONTRACT
