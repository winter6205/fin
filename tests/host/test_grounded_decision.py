"""Tests for the pure grounded-decision admission module (plan T9).

The module is a pure function of ONE model output plus ONE admitted
evidence set, so every case here is deterministic and needs no live
LLM. The suite pins the contract clauses the specs freeze:

- GE-2 / spec ``qa-chain-grounded-experience.md``: at most four
  hit-backed candidates, multiple documents alone never imply
  ambiguity, evidence refusal only after a real retrieval attempt.
- GE-3 / ``fallback_merge`` with verified references on a malformed
  payload, ``fallback_refuse`` with none, and never trusting citation
  strings out of the failed payload.
- The one-call budget: the injected R3 verifier is called exactly once
  per admission on every branch.
- The shared wire contract's ``choice_details`` bounds and its exactly
  one trailing ``other`` action labelled “换个问法”.
- The readable-copy rule: user-visible Chinese naming no internal id,
  tool name or slot name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest

from fin.corpus.extracted import Locator
from fin.host.grounded.citation_gate import (
    Citation,
    DoneResult,
    EvidenceItem,
    GateResult,
    RefuseResult,
)
from fin.host.grounded.grounded_decision import (
    CANDIDATE_TERMINATOR,
    EVIDENCE_REFUSAL_REPHRASING_1,
    EVIDENCE_REFUSAL_REPHRASING_2,
    EVIDENCE_REFUSAL_SENTENCE,
    MAX_DOCUMENT_CANDIDATES,
    REPHASE_CHOICE_ID,
    REPHRASE_ACTION,
    DocumentCandidate,
    GroundedDecision,
    SourceRef,
    admit_decision,
    admitted_hits,
    candidate_bounds_ok,
    candidate_bounds_reasons,
    candidate_key,
    evidence_refusal_copy,
    parse_decision_output,
    rephrase_action,
)
from fin.host.task.task_runtime_types import MAX_DOCUMENT_CHOICES, OTHER_CHOICE_ID
from fin.retrieve.source_read import ATTACHMENT_KIND

_SHA = "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
_SHA2 = "b1f0e9372b1a3415fd56557bf0e82cacac71fc5173d82913963893ecf99ac903"


# ----- fixtures / helpers -----


def _company_hit(
    document_id: str,
    unit_text: str,
    *,
    version: str = _SHA,
    unit: int = 1,
) -> EvidenceItem:
    """One admitted corpus passage hit."""
    return EvidenceItem(
        locator=Locator(document_id=document_id, version=version, unit=unit),
        unit_text=unit_text,
    )


def _attachment_hit(
    attachment_id: str,
    unit_text: str,
    *,
    filename: str = "会议纪要.txt",
    version: str = "3",
    unit: int = 1,
) -> EvidenceItem:
    """One admitted thread-attachment hit (document slots stay empty)."""
    return EvidenceItem(
        locator=Locator(document_id="", version=version, unit=unit),
        unit_text=unit_text,
        kind=ATTACHMENT_KIND,
        attachment_id=attachment_id,
        version_int=int(version),
        filename=filename,
        display="unit",
    )


def _done(answer: str, *citations: Citation) -> GateResult:
    """A verifier verdict that admitted the answer with ``citations``."""
    return DoneResult(
        outcome="done", citations=tuple(citations), answer=answer
    )


def _refuse(reason: str = "unknown_locator") -> GateResult:
    """A verifier verdict that refused the answer."""
    return RefuseResult(outcome="refuse", reason=reason)


def _citation(document_id: str = "AAPL-2022Q3") -> Citation:
    return Citation(
        kind="company",
        document_id=document_id,
        attachment_id="",
        version=_SHA,
        unit=1,
        display="page",
        filename="",
        quote="quoted span",
    )


@dataclass
class _Verifier:
    """An injected R3 verifier that records every invocation.

    Counting the calls is the only way to pin the one-call budget: the
    budget is a property of how many times the seam was used, not of
    any returned value.
    """

    result: GateResult
    calls: list[str] = field(default_factory=list)

    def __call__(self, answer: str) -> GateResult:
        self.calls.append(answer)
        if isinstance(self.result, DoneResult) and self.result.answer == "":
            # Echo the text this verifier was given, the way the real
            # verifier does (DoneResult.answer is the answer it passed).
            return DoneResult(
                outcome="done",
                citations=self.result.citations,
                answer=answer,
            )
        return self.result


def _verdict_for(
    verdict: GateResult,
) -> _Verifier:
    """A verifier returning ``verdict`` verbatim."""
    return _Verifier(verdict)


def _admit(
    text: str,
    *,
    evidence: tuple[EvidenceItem, ...] = (),
    retrieval_attempted: bool = True,
    verifier: _Verifier,
) -> GroundedDecision:
    """Run the full parse-then-admit seam on one model output."""
    return admit_decision(
        answer_text=text,
        parsed=parse_decision_output(text),
        evidence=evidence,
        retrieval_attempted=retrieval_attempted,
        verify=verifier,
    )


def _request(*ids: str, field_name: str = "document_id") -> str:
    """A model output that asks for the given ids via the terminator."""
    payload = json.dumps({"candidates": [{field_name: i} for i in ids]})
    return f"这个问题有两种理解。{CANDIDATE_TERMINATOR}\n{payload}"


# ----- plain answers -----


def test_plain_answer_merges_with_verified_citations_and_one_verify_call() -> None:
    """Pins the merge branch: a plain answer with no terminator merges,
    with the citations the injected verifier returned, and the
    one-verifier-call budget holds."""
    answer = "公司本季度的收入主要来自硬件销售业务。"
    verify = _verdict_for(_done("", _citation()))
    decision = _admit(answer, verifier=verify)

    assert decision.outcome == "merge"
    assert decision.answer == answer
    assert decision.citations == (_citation(),)
    assert decision.fallback == "none"
    assert decision.parse_failed is False
    assert verify.calls == [answer]


def test_refuse_verdict_on_a_plain_answer_is_an_evidence_refusal() -> None:
    """Pins the merge branch's negative half: a verifier refusal is an
    evidence refusal with ZERO citations and no answer bytes."""
    verify = _verdict_for(_refuse())
    decision = _admit("公司本季度的收入主要来自硬件销售业务。", verifier=verify)

    assert decision.outcome == "evidence_refuse"
    assert decision.citations == ()
    assert decision.answer is None
    assert decision.fallback == "none"
    assert decision.retrieval_attempted is True


def test_evidence_refuse_without_a_retrieval_attempt_raises() -> None:
    """Pins GE-2/A06: a refusal without a real retrieval attempt is a
    contract violation — a turn that never searched must not be able to
    report that the corpus has no answer."""
    verify = _verdict_for(_refuse())
    with pytest.raises(ValueError, match="retrieval_attempted"):
        _admit(
            "公司本季度的收入主要来自硬件销售业务。",
            retrieval_attempted=False,
            verifier=verify,
        )


def test_multiple_admitted_documents_with_no_terminator_merge() -> None:
    """Pins "multiple documents alone never imply ambiguity": an evidence
    set spanning four documents with NO terminator is a merge, never a
    choice — the shape of the evidence set never derives a choice."""
    evidence = tuple(
        _company_hit(f"AAPL-2022Q{page}", f"第 {page} 页的正文内容足够长以便形成一句完整的描述。")
        for page in (1, 2, 3, 4)
    )
    verify = _verdict_for(_done("", _citation()))
    decision = _admit(
        "公司的收入结构在不同季度之间发生了变化。", evidence=evidence, verifier=verify
    )

    assert decision.outcome == "merge"
    assert decision.candidates == ()
    assert len(verify.calls) == 1


# ----- the candidate branch -----


def test_four_admitted_candidates_are_offered_and_the_fifth_is_dropped() -> None:
    """Pins the ≤4 bound: five admitted refs request candidates, exactly
    the first ``MAX_DOCUMENT_CANDIDATES`` are admitted and the fifth is
    dropped — never a fifth candidate."""
    evidence = tuple(
        _company_hit(f"AAPL-2022Q{q}", f"第 {q} 个季度的经营说明在这里。")
        for q in (1, 2, 3, 4, 5)
    )
    text = _request("AAPL-2022Q1", "AAPL-2022Q2", "AAPL-2022Q3", "AAPL-2022Q4", "AAPL-2022Q5")
    verify = _verdict_for(_done(""))
    decision = _admit(text, evidence=evidence, verifier=verify)

    assert decision.outcome == "choice"
    assert len(decision.candidates) == MAX_DOCUMENT_CANDIDATES
    assert [c.source_ref.source_id for c in decision.candidates] == [
        "AAPL-2022Q1",
        "AAPL-2022Q2",
        "AAPL-2022Q3",
        "AAPL-2022Q4",
    ]


def test_requested_ref_absent_from_admitted_hits_is_dropped() -> None:
    """Pins candidate membership: a ref no admitted hit backs is dropped,
    never invented from an eligible-but-unretrieved source."""
    evidence = (_company_hit("AAPL-2022Q3", "这个季度的经营说明在这里。"),)
    text = _request("AAPL-2022Q3", "MSFT-2023Q2")
    verify = _verdict_for(_done(""))
    decision = _admit(text, evidence=evidence, verifier=verify)

    assert decision.outcome == "choice"
    assert [c.source_ref.source_id for c in decision.candidates] == [
        "AAPL-2022Q3"
    ]


def test_request_whose_refs_are_all_absent_falls_back_to_refusal() -> None:
    """Pins the zero-candidate rule: a request every ref of which is
    absent takes the refusal path for that prose, never an empty choice
    set."""
    evidence = (_company_hit("AAPL-2022Q3", "这个季度的经营说明在这里。"),)
    text = _request("MSFT-2023Q2", "AMZN-2023Q3")
    verify = _verdict_for(_refuse())
    decision = _admit(text, evidence=evidence, verifier=verify)

    assert decision.outcome == "evidence_refuse"
    assert decision.candidates == ()
    assert decision.citations == ()
    assert len(verify.calls) == 1


def test_two_hits_from_the_same_document_and_section_dedup_to_one_candidate() -> None:
    """Pins the dedup rule: several hits backing ONE document/section are
    ONE candidate, and the admitted-hit index holds it once."""
    text_a = "Section 2.1\n第一段说明了收入确认的时点。"
    text_b = "Section 2.1\n第二段补充了收入确认的口径。"
    evidence = (
        _company_hit("AAPL-2022Q3", text_a, unit=4),
        _company_hit("AAPL-2022Q3", text_b, unit=9),
    )
    hits = admitted_hits(evidence)
    assert len(hits) == 1

    verify = _verdict_for(_done(""))
    decision = _admit(_request("AAPL-2022Q3"), evidence=evidence, verifier=verify)
    assert decision.outcome == "choice"
    assert len(decision.candidates) == 1
    candidate = decision.candidates[0]
    # The readable candidate may carry a trusted section, but its source
    # ID stays in the navigation reference rather than the default label.
    assert candidate.label == "资料来源 · Section 2.1"
    assert candidate.source_ref.source_id == "AAPL-2022Q3"
    assert candidate.summary == "第一段说明了收入确认的时点。"


def test_attachment_evidence_produces_an_attachment_candidate() -> None:
    """Pins the attachment identity rule: an attachment candidate binds
    its OWN id slot and the never-read-empty document slots stay empty —
    a document id is never minted from a locator the attachment does not
    have."""
    evidence = (
        _attachment_hit("att-42", "附件里写明了本季度的排产安排。"),
    )
    verify = _verdict_for(_done(""))
    decision = _admit(
        _request("att-42", field_name="attachment_id"),
        evidence=evidence,
        verifier=verify,
    )

    assert decision.outcome == "choice"
    candidate = decision.candidates[0]
    assert candidate.source_ref.source_kind == "attachment"
    assert candidate.source_ref.source_id == "att-42"
    assert candidate.label == "会议纪要.txt"
    assert candidate.summary == "附件里写明了本季度的排产安排。"


def test_database_row_evidence_is_never_a_selectable_candidate() -> None:
    """Pins the database-row identity rule: a ledger row is indexed under
    its own record id and its document slots stay empty, and it is never
    a selectable document — the wire contract closes source_kind to
    corpus|attachment, so a request for it yields no candidate."""
    from fin.host.grounded.citation_gate import (
        DATABASE_ROW_DISPLAY,
        DATABASE_ROW_KIND,
        DatabaseRow,
    )

    row = DatabaseRow(
        source="ar_ledger",
        record_key=(("invoice", "C-2291"),),
        queried_at="2026-03-14",
        fields=(("due_days", "30"),),
        summary="发票 C-2291 的账期为 30 天。",
    )
    evidence = (
        EvidenceItem(
            locator=Locator(document_id="", version="", unit=1),
            unit_text=row.summary,
            kind=DATABASE_ROW_KIND,
            display=DATABASE_ROW_DISPLAY,
            database_row=(row,),
        ),
    )
    hits = admitted_hits(evidence)
    assert len(hits) == 1
    (key,) = hits
    assert key.startswith("database:")
    # The row's own record id, escaped so it can never forge a key part.
    assert row.source_id.replace("#", "\\#") in key

    verify = _verdict_for(_refuse())
    decision = _admit(
        _request(row.source_id), evidence=evidence, verifier=verify
    )
    assert decision.outcome == "evidence_refuse"
    assert decision.candidates == ()


def test_candidate_label_uses_the_opaque_fallback_without_a_title_or_section() -> None:
    """An opaque document identifier is not copied into the default label."""
    evidence = (_company_hit("AAPL-2022Q3", "这段正文没有小节标题。"),)
    verify = _verdict_for(_done(""))
    decision = _admit(_request("AAPL-2022Q3"), evidence=evidence, verifier=verify)

    candidate = decision.candidates[0]
    assert candidate.label == "资料来源"
    assert candidate.source_ref.source_id == "AAPL-2022Q3"
    assert candidate.source_ref.section is None


# ----- parse failures and fallback admission -----


def test_malformed_payload_routes_the_original_text_through_verify() -> None:
    """Pins GE-3's admitted half: a terminator carrying non-JSON makes
    ``parse_failed=True``, the ORIGINAL text goes through the same
    verifier, and an admitted answer records ``fallback_merge`` with the
    verifier's citations."""
    text = "公司本季度的收入主要来自硬件销售业务。\n" + CANDIDATE_TERMINATOR + "not json at all"
    verify = _verdict_for(_done("", _citation()))
    decision = _admit(text, verifier=verify)

    assert decision.parse_failed is True
    assert decision.outcome == "merge"
    assert decision.fallback == "fallback_merge"
    assert decision.answer == text
    assert decision.citations == (_citation(),)
    assert verify.calls == [text]


@pytest.mark.parametrize(
    "payload",
    ["[1, 2, 3]", '{"candidates": "AAPL-2022Q3"}', '{"candidates": [{"id": "x"}]}'],
    ids=["json-not-object", "refs-not-a-list", "element-without-an-id"],
)
def test_payload_shape_violations_all_set_parse_failed(payload: str) -> None:
    """Pins the parse-failure classes: a JSON array, a non-list ref field
    and an element with no usable id are each a parse failure — a return
    value, never an exception."""
    parsed = parse_decision_output(f"回答正文。{CANDIDATE_TERMINATOR}{payload}")
    assert parsed.parse_failed is True
    assert parsed.terminator_present is True
    assert parsed.requested == ()


def test_payload_naming_both_id_slots_is_a_parse_failure() -> None:
    """Pins the unambiguous-identity rule: an element naming BOTH a
    document and an attachment does not name one source, so the payload
    fails wholesale rather than being half-interpreted."""
    payload = json.dumps(
        {"candidates": [{"document_id": "A", "attachment_id": "b"}]}
    )
    parsed = parse_decision_output(f"回答。{CANDIDATE_TERMINATOR}{payload}")
    assert parsed.parse_failed is True


def test_fallback_refuse_when_the_original_text_fails_verification() -> None:
    """Pins GE-3's rejected half: a malformed payload whose original text
    the verifier rejects records ``fallback_refuse`` with zero citations
    and returns no answer bytes at all."""
    text = "无法核实的一句话。" + CANDIDATE_TERMINATOR + "{{{bad"
    verify = _verdict_for(_refuse())
    decision = _admit(text, verifier=verify)

    assert decision.parse_failed is True
    assert decision.outcome == "evidence_refuse"
    assert decision.fallback == "fallback_refuse"
    assert decision.citations == ()
    assert decision.answer is None
    assert verify.calls == [text]


def test_malformed_payload_containing_citation_markers_is_not_trusted() -> None:
    """Pins "do not salvage unverified citation strings": a failed payload
    that literally embeds ``[1]``-style markers contributes NO citation
    and NO answer text — every returned string comes from the verifier or
    is empty, and the payload's marker survives nowhere."""
    payload = '{"candidates": [{"document_id": "AAPL-2022Q3"'
    text = "公司本季度的收入主要来自硬件销售业务。[1]" + CANDIDATE_TERMINATOR + payload
    verify = _verdict_for(_refuse())
    decision = _admit(text, verifier=verify)

    assert decision.parse_failed is True
    assert decision.outcome == "evidence_refuse"
    assert decision.citations == ()
    assert decision.answer is None

    published = (decision.answer or "") + "".join(c.quote for c in decision.citations)
    assert "[1]" not in published
    assert CANDIDATE_TERMINATOR not in published
    assert "AAPL-2022Q3" not in published


# ----- parser tolerance -----


def test_parser_tolerates_no_terminator_whitespace_and_trailing_junk() -> None:
    """Pins the parser's tolerance rules: no terminator is a whole-text
    answer; the terminator is recognized after a line break; and trailing
    junk after a well-formed JSON object does not fail the parse."""
    plain = parse_decision_output("只有一个答案，没有终结标记。")
    assert plain.terminator_present is False
    assert plain.answer_text == "只有一个答案，没有终结标记。"
    assert plain.parse_failed is False
    assert plain.requested == ()

    spaced = parse_decision_output(
        "答案。\n\n  " + CANDIDATE_TERMINATOR + "\n  {\"candidates\": [{\"document_id\": \"AAPL-2022Q3\"}]}  \n"
    )
    assert spaced.answer_text == "答案。\n\n  "
    assert spaced.parse_failed is False
    assert [r.source_id for r in spaced.requested] == ["AAPL-2022Q3"]

    junked = parse_decision_output(
        "答案。" + CANDIDATE_TERMINATOR + '{"candidates": []} 以上。'
    )
    assert junked.parse_failed is False
    assert junked.requested == ()


def test_terminator_with_an_empty_ref_list_is_a_merge() -> None:
    """Pins the explicit no-candidates case: a well-formed empty ref list
    is a merge request, verified on the prose, not a choice."""
    text = "公司本季度的收入主要来自硬件销售业务。" + CANDIDATE_TERMINATOR + '{"candidates": []}'
    verify = _verdict_for(_done("", _citation()))
    decision = _admit(text, verifier=verify)

    assert decision.parse_failed is False
    assert decision.outcome == "merge"
    assert decision.candidates == ()
    assert verify.calls == ["公司本季度的收入主要来自硬件销售业务。"]


# ----- the one-call budget -----


def test_verify_is_called_exactly_once_in_every_branch() -> None:
    """Pins the deterministic budget: merge, refuse, choice, all-dropped
    choice and both fallback branches each call the verifier exactly
    once — no branch buys a second verifier call."""
    evidence = (_company_hit("AAPL-2022Q3", "Section 1\n这个季度的经营说明在这里。"),)
    cases = {
        "merge": "公司本季度的收入主要来自硬件销售业务。",
        "choice": _request("AAPL-2022Q3"),
        "fallback": "公司本季度的收入主要来自硬件销售业务。" + CANDIDATE_TERMINATOR + "bad",
    }
    for branch, text in cases.items():
        for verdict in (_done("", _citation()), _refuse()):
            verify = _verdict_for(verdict)
            _admit(text, evidence=evidence, verifier=verify)
            assert len(verify.calls) == 1, branch


def test_answer_text_not_matching_the_parsed_text_raises() -> None:
    """Pins the seam's caller contract: the text admitted and the text
    parsed must be the same model output, or the citation quotes would
    not be substrings of the published answer."""
    with pytest.raises(ValueError, match="original_text"):
        admit_decision(
            answer_text="另一段文字",
            parsed=parse_decision_output("公司本季度的收入主要来自硬件销售业务。"),
            evidence=(),
            retrieval_attempted=True,
            verify=_verdict_for(_done("")),
        )


# ----- size bounds -----


def _oversized_candidate(**overrides: object) -> DocumentCandidate:
    """A legal candidate with one field replaced by ``overrides``."""
    base: dict[str, object] = {
        "choice_id": candidate_key("corpus", "AAPL-2022Q3", _SHA, None),
        "label": "AAPL-2022Q3",
        "summary": "这个季度的经营说明在这里。",
        "source_ref": SourceRef(
            source_kind="corpus",
            source_id="AAPL-2022Q3",
            version=_SHA,
            section=None,
        ),
    }
    base.update(overrides)
    return DocumentCandidate(**base)  # type: ignore[arg-type]


def test_legal_candidate_passes_the_bounds() -> None:
    """Pins the bounds' positive case so the negatives below are not
    passing by accident."""
    assert candidate_bounds_ok(_oversized_candidate()) is True
    assert candidate_bounds_reasons(_oversized_candidate()) == ()


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    [
        ("label", "标" * 200, "label_oversized"),
        ("summary", "第一句。" + "很长的补充说明。" * 100, "summary_oversized"),
        ("summary", "第一句。第二句。", "summary_not_one_sentence"),
        ("summary", "   ", "empty_summary"),
        (
            "source_ref",
            SourceRef("corpus", "A" * 300, _SHA, None),
            "source_id_oversized",
        ),
        (
            "source_ref",
            SourceRef("corpus", "AAPL-2022Q3", _SHA2 * 5, None),
            "version_oversized",
        ),
        (
            "source_ref",
            SourceRef("corpus", "", _SHA, None),
            "empty_source_id",
        ),
        (
            "source_ref",
            SourceRef("corpus", "AAPL-2022Q3", "bad\x07version", None),
            "control_char_in_version",
        ),
    ],
)
def test_oversized_or_malformed_fields_are_rejected_by_name(
    field_name: str, value: object, reason: str
) -> None:
    """Pins "reject an oversized proposal before publishing it, without
    truncating": each violation is named, never silently shortened."""
    candidate = _oversized_candidate(**{field_name: value})
    assert candidate_bounds_ok(candidate) is False
    assert reason in candidate_bounds_reasons(candidate)


def test_control_characters_in_a_label_are_rejected() -> None:
    """Pins "reject control characters in identifiers/labels": a label
    carrying a control character is refused, while the same prose may
    carry ordinary line breaks."""
    assert "control_char_in_label" in candidate_bounds_reasons(
        _oversized_candidate(label="AAPL\x002022Q3")
    )
    prose_with_break = _oversized_candidate(
        summary="Section 1\n这个季度的经营说明在这里。",
        label="AAPL-2022Q3\n第二部分",
    )
    assert candidate_bounds_ok(prose_with_break) is True


def test_oversized_generated_candidate_is_dropped_with_a_reason() -> None:
    """Pins the drop-not-truncate rule at the selection seam: a requested
    ref whose trusted display name overshoots the label bound is dropped
    and the reason is exposed for the caller to log, while the other
    admitted candidate still stands."""
    huge_id = "AAPL-2022Q3-" + "x" * 300
    evidence = (
        _company_hit(huge_id, "这个季度的经营说明在这里。"),
        _company_hit("AAPL-2022Q3", "这个季度的经营说明在这里。"),
    )
    from fin.host.grounded.grounded_decision import select_candidates

    parsed = parse_decision_output(_request(huge_id, "AAPL-2022Q3"))
    candidates, reasons = select_candidates(parsed.requested, evidence)

    assert [c.source_ref.source_id for c in candidates] == ["AAPL-2022Q3"]
    assert "source_id_oversized" in reasons


# ----- user-visible actions and copy -----


def test_rephrase_action_is_the_frozen_chinese_label_on_the_trailing_other() -> None:
    """Pins the shared wire contract's Other rule: the rephrase action is
    the label “换个问法” and its choice id is the reserved trailing
    ``other`` token, identical to the one the runtime spells."""
    assert rephrase_action() == "换个问法"
    assert REPHRASE_ACTION == "换个问法"
    assert REPHASE_CHOICE_ID == "other"
    assert REPHASE_CHOICE_ID == OTHER_CHOICE_ID


def test_the_admitted_candidate_bound_is_the_runtime_choice_bound() -> None:
    """Pins the one number a document choice is sized by, in BOTH places it
    is spelled.

    This module deliberately re-states the shared literal instead of
    importing it (it keeps a limited import surface), and the runtime
    types module holds the authority the wire projection is checked
    against. Two spellings with nothing tying them together can drift
    silently: the admission would build a candidate list the runtime then
    refuses as over-limit, so the run would publish no question at all.
    """
    assert MAX_DOCUMENT_CANDIDATES == MAX_DOCUMENT_CHOICES == 4


def test_a_candidate_choice_id_never_collides_with_the_other_token() -> None:
    """Pins the exactly-one-trailing-Other rule at the seam: an admitted
    candidate's ``choice_id`` is a derived key, so it can never be the
    reserved ``other`` token a reply is recognised by."""
    evidence = (_company_hit("AAPL-2022Q3", "这个季度的经营说明在这里。"),)
    decision = _admit(
        _request("AAPL-2022Q3"), evidence=evidence, verifier=_verdict_for(_done(""))
    )
    assert all(c.choice_id != OTHER_CHOICE_ID for c in decision.candidates)


def test_evidence_refusal_copy_is_three_distinct_plain_chinese_strings() -> None:
    """Pins "evidence refusal states insufficient evidence and supplies two
    supported ways to rephrase without inventing facts": the copy is a
    refusal sentence plus two distinct rephrasings."""
    copy = evidence_refusal_copy()
    assert len(copy) == 3
    assert len(set(copy)) == 3
    assert copy == (
        EVIDENCE_REFUSAL_SENTENCE,
        EVIDENCE_REFUSAL_REPHRASING_1,
        EVIDENCE_REFUSAL_REPHRASING_2,
    )
    for text in copy:
        assert re.search(r"[一-鿿]", text), text


def test_user_visible_copy_names_no_internal_id_tool_or_slot() -> None:
    """Pins the readable-projection rule: no user-visible string in this
    ticket leaks ``document_id`` / ``list_files`` / ``10-Q`` / ``XBRL``
    or a version hash."""
    forbidden = ("document_id", "list_files", "10-Q", "XBRL")
    hash_like = re.compile(r"[0-9a-f]{32,}", re.IGNORECASE)
    for text in (rephrase_action(), *evidence_refusal_copy()):
        for token in forbidden:
            assert token.lower() not in text.lower(), text
        assert not hash_like.search(text), text


def test_candidate_labels_and_summaries_never_leak_a_version_hash() -> None:
    """Pins the same readable rule on the generated candidates: a version
    rides the ``source_ref``, never the label or the summary the user
    reads."""
    evidence = (_company_hit("AAPL-2022Q3", "Section 3\n这个季度的经营说明在这里。"),)
    decision = _admit(
        _request("AAPL-2022Q3"), evidence=evidence, verifier=_verdict_for(_done(""))
    )
    candidate = decision.candidates[0]
    assert candidate.source_ref.version == _SHA
    assert _SHA not in candidate.label
    assert _SHA not in candidate.summary


def test_summary_is_the_first_sentence_of_the_hit_text_not_model_prose() -> None:
    """Pins the deterministic summary rule: the summary is the FIRST
    sentence of the admitted hit's own text (after a section-heading
    line), minted without a model call — so model prose before the
    terminator can never become the summary. The hit's SECOND sentence
    is left out."""
    hit_text = "Section 4.2\n这一段说明了现金流状况。后一段讲了别的事情。"
    evidence = (_company_hit("AAPL-2022Q3", hit_text),)
    model_prose = "我猜这段讲的是资产负债表和现金流趋势。"
    text = model_prose + CANDIDATE_TERMINATOR + json.dumps(
        {"candidates": [{"document_id": "AAPL-2022Q3"}]}
    )
    verify = _verdict_for(_done(""))
    decision = _admit(text, evidence=evidence, verifier=verify)

    candidate = decision.candidates[0]
    assert candidate.summary == "这一段说明了现金流状况。"
    assert candidate.summary in hit_text
    assert "后一段讲了别的事情。" not in candidate.summary
    assert model_prose not in candidate.summary


def test_summary_skips_filing_cover_and_item_heading_before_hit_content() -> None:
    """Quote the first readable table row after filing structure."""
    hit_text = (
        "PART I — FINANCIAL INFORMATION\n"
        "Item 1. Financial Statements\n"
        "Apple Inc.\n"
        "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited)\n"
        "(In millions, except number of shares and per share amounts)\n"
        "Three Months Ended Nine Months Ended\n"
        "July 1, June 25, July 1, June 25,\n"
        "2023 2022 2023 2022\n"
        "Net sales:\n"
        "Products $ 60,584 $ 63,355 $ 230,901 $ 245,241\n"
        "Total net sales 81,797 82,959 293,787 304,182\n"
        "Total operating expenses 12,000 13,000 40,000 41,000"
    )
    evidence = (_company_hit("AAPL-2022Q3", hit_text),)
    decision = _admit(
        _request("AAPL-2022Q3"),
        evidence=evidence,
        verifier=_verdict_for(_done("")),
    )

    summary = decision.candidates[0].summary

    assert summary == (
        "资料中列出了“Products $ 60,584 $ 63,355 $ 230,901 $ 245,241”。"
    )
    assert "Products $ 60,584 $ 63,355 $ 230,901 $ 245,241" in summary
    assert "PART I" not in summary
    assert "Item 1." not in summary


def test_summary_keeps_narrative_that_begins_with_a_month() -> None:
    hit_text = (
        "March 2023 marked a shift in our product mix. "
        "Later source detail is outside the first sentence."
    )
    evidence = (_company_hit("AAPL-2023Q1", hit_text),)
    decision = _admit(
        _request("AAPL-2023Q1"),
        evidence=evidence,
        verifier=_verdict_for(_done("")),
    )

    summary = decision.candidates[0].summary

    assert summary == "March 2023 marked a shift in our product mix."
    assert summary in hit_text


def test_summary_keeps_body_after_inline_section_marker() -> None:
    hit_text = (
        "Section 1. Apple reported net sales of 89,493 million dollars for "
        "the fiscal 2023 fourth quarter."
    )
    evidence = (_company_hit("AAPL-2023Q4", hit_text),)
    decision = _admit(
        _request("AAPL-2023Q4"),
        evidence=evidence,
        verifier=_verdict_for(_done("")),
    )

    summary = decision.candidates[0].summary

    assert summary == (
        "Apple reported net sales of 89,493 million dollars for "
        "the fiscal 2023 fourth quarter."
    )
    assert summary in hit_text
