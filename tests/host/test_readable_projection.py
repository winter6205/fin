"""The one admitted readable projection on the host.

The default view may carry an answer, its sources and its failure copy —
and none of the internal representations those bytes are built from.
These tests pin three separate claims so a failure says which one broke:

* the six-question text scan finds none of ``document_id`` /
  ``list_files`` / ``10-Q`` / ``XBRL`` in projected text;
* a leak split across chunk boundaries cannot survive the terminal
  projection, while a per-chunk filter provably lets it through;
* a legitimate finance title still reads as itself, through a readable
  label rather than an internal id.

The failure-copy tests pin the four DISTINCT classes and their exact
strings, because a degraded result that reads like an index outage sends
the operator to the wrong place.
"""

from __future__ import annotations

import json

import pytest

from fin.host.degradation_notice import (
    degradation_notice_entry,
    degradation_notice_line,
)
from fin.host.grounded.readable_projection import (
    ATTACHMENT_SCOPE_COPY,
    BUSY_COPY,
    DEGRADATION_SAFE_COPY,
    EVIDENCE_REFUSAL_COPY,
    FORBIDDEN_LABELS,
    HIDDEN_MARK,
    INDEX_WORK_COPY,
    UnclassifiableTerminal,
    classify_failure,
    failure_copy,
    leaked_fragments,
    project_admitted_answer,
    project_citation_row,
    project_text,
    projection_holdback,
    source_label,
)
from fin.retrieve.degradation import DegradationSignal


# ----- the six-question scan -----

SIX_QUESTION_FORBIDDEN = (
    "document_id",
    "list_files",
    "10-Q",
    "XBRL",
)


def _synthesized_answer() -> str:
    """A prose answer shaped like the leaks the six-question scan looks for.

    Nothing here is a fact the corpus supports; it is the *shape* of a
    leak — an internal field name, a tool name, a filing form, an
    extension standard, an exception type and a raw degradation reason.
    """
    return (
        "根据 document_id=AAPL-2022Q3 的 10-Q 报表，"
        "XBRL 口径下营收同比增长。\n"
        "可用工具：list_files / retrieve / get_xbrl_fact。\n"
        "本次降级原因：vector:attempt_failed；"
        "graph_ppr:not_attempted。\n"
        "内部错误 TimeoutError: pool exhausted。"
    )


def test_projected_answer_carries_none_of_the_six_question_labels() -> None:
    projected = project_text(_synthesized_answer())

    for label in SIX_QUESTION_FORBIDDEN:
        assert label.lower() not in projected.lower(), label


def test_projected_answer_carries_no_tool_engine_slot_or_reason_strings() -> None:
    projected = project_text(_synthesized_answer())

    for label in FORBIDDEN_LABELS:
        assert label.lower() not in projected.lower(), label


def test_projection_replaces_a_leak_rather_than_deleting_its_neighbours() -> None:
    """The surrounding prose is untouched — a filter that eats the sentence
    turns one leak into an unreadable answer."""

    projected = project_text("请查看 document_id 对应的报表。")

    assert "请查看" in projected
    assert "对应的报表。" in projected
    assert HIDDEN_MARK in projected


def test_a_healthy_answer_is_returned_unchanged() -> None:
    answer = "本季度净销售额同比增长九个百分点，详见资料来源。"

    assert project_text(answer) == answer


def test_leaked_fragments_names_the_survivors_before_projection() -> None:
    survivors = leaked_fragments(_synthesized_answer())

    assert "document_id" in survivors
    assert "list_files" in survivors
    assert not leaked_fragments("净销售额同比增长九个百分点。")


# ----- a leak split across SSE chunks -----

SPLIT_CHUNKS = (
    "结论成立，原因是 document_",
    "id 指向的记录缺失。",
)


def test_a_per_chunk_filter_lets_a_split_leak_through() -> None:
    """WHY the projection is applied to the admitted text and not to each
    chunk: a forbidden fragment can be cut in half by the transport, and
    neither half matches on its own."""

    per_chunk = [project_text(chunk) for chunk in SPLIT_CHUNKS]

    assert any("document_" in chunk for chunk in per_chunk)
    assert "".join(per_chunk) == "".join(SPLIT_CHUNKS)


def test_the_terminal_projection_admits_no_split_leak() -> None:
    projected = project_admitted_answer(SPLIT_CHUNKS)

    assert "document_" not in projected
    assert "id 指向的记录缺失" not in projected
    assert "结论成立" in projected


def test_the_terminal_projection_joins_the_chunks_it_admits() -> None:
    projected = project_admitted_answer(["第一段。", "第二段。", "第三段。"])

    assert projected == "第一段。第二段。第三段。"


def test_the_terminal_projection_strips_the_raw_degradation_reasons() -> None:
    chunks = ("结果可用。", "原因：bm25:timeout 与 vector:timeout。")

    projected = project_admitted_answer(
        chunks, degradation_reasons=("bm25:timeout", "vector:timeout")
    )

    assert "结果可用。" in projected
    assert "bm25:timeout" not in projected
    assert "vector:timeout" not in projected


# ----- the streaming hold-back this module tells a stream about -----


def test_a_label_prefix_is_withheld_so_it_can_finish() -> None:
    """An unfinished label must not be released.

    ``document_`` matches no rule today and every rule once the next delta
    arrives, so a stream that released it has put a fragment on the wire
    that it cannot take back.
    """
    assert projection_holdback("结论成立，原因是 document_") > 0
    assert projection_holdback("document") > 0


def test_a_complete_label_is_not_withheld() -> None:
    """A label the buffer has finished is decided, not ambiguous."""
    assert projection_holdback("原因见 document_id。") == 0
    assert projection_holdback("这是一句普通的结论。") == 0
    assert projection_holdback("") == 0


def test_an_open_value_at_the_end_is_withheld_whole() -> None:
    """A closed match whose VALUE can still grow is still ambiguous.

    ``document_id: A`` matches today, and ``AAPL-2023Q3`` would extend the
    value past the rule's character class if the next delta arrived, so the
    whole match is withheld rather than released and contradicted.
    """
    text = "结论：document_id: A"

    assert projection_holdback(text) == len(text) - len("结论：")


def test_a_match_closed_by_a_later_character_needs_no_holdback() -> None:
    """The mark that replaced it means there is nothing left to leak."""
    text = "结论：document_id: AAPL-2023Q3。"

    assert projection_holdback(text) == 0


def test_the_finance_conditional_holds_back_the_same_way() -> None:
    """``EDGAR`` is a subject matter on a filings corpus and boilerplate
    off one, so the non-finance reading withholds the whole word and the
    finance reading does not. (The finance reading still holds a trailing
    character whose prefix is shared with an identifier like ``retrieve``
    — a one-character pacing cost, not the word.)"""
    off_finance = projection_holdback("提交给 EDGAR")
    on_finance = projection_holdback("提交给 EDGAR", corpus_is_finance=True)

    assert off_finance >= len("EDGAR")
    assert on_finance < len("EDGAR")
    assert projection_holdback("document_", corpus_is_finance=True) > 0


def test_holding_back_a_suffix_never_hides_a_whole_label() -> None:
    """The hold-back is bounded: an answer is never withheld wholesale.

    A hold-back that grows with the buffer would stall the stream to a
    single frame at the end, which passes every leak assertion and breaks
    the pacing the browser lane measures.
    """
    text = "Apple reported net sales of 89,493 million dollars for the quarter."
    held = projection_holdback(text)

    assert held < len(text) / 2


def test_a_label_prefix_inside_a_word_is_not_ambiguous() -> None:
    """The rules refuse to match a label inside a word (``(?<![\\w-])``),
    so a prefix that starts there cannot grow into one and is decided.

    ``SECURITIES`` ends in ``S``, which is one character of ``source_ref``,
    but the ``E`` before it is a word character: no delta can turn that
    ``S`` into the label, because the label may not begin mid-word. Holding
    it back anyway delays a clean answer's frames for nothing.
    """
    assert projection_holdback("Per the filing: UNITED STATES SECURITIES") == 0
    assert projection_holdback("tokenS") == 0


# ----- a legitimate finance title stays representable -----

FINANCE_TITLE = "AAPL 2022 财年第三季度 10-Q 业绩说明"


def test_a_finance_source_title_survives_a_finance_corpus_projection() -> None:
    """Blanket redaction would delete the very terminology an ordinary
    person asked about. The same words are dropped only when the corpus
    is not a finance corpus, where they are leaked boilerplate."""

    projected = project_text(FINANCE_TITLE, corpus_is_finance=True)

    assert projected == FINANCE_TITLE


def test_the_same_filing_form_is_dropped_for_a_non_finance_corpus() -> None:
    projected = project_text(FINANCE_TITLE, corpus_is_finance=False)

    assert "10-Q" not in projected
    assert "AAPL" in projected


def test_internal_field_names_are_dropped_even_for_a_finance_corpus() -> None:
    projected = project_text(
        "document_id=AAPL-2022Q3 的 10-Q 显示营收增长。",
        corpus_is_finance=True,
    )

    assert "document_id" not in projected
    assert "10-Q" in projected


# ----- the readable source label -----


def test_a_company_source_label_reads_as_its_title() -> None:
    row = {"kind": "company", "document_id": "zeus-annual-results-summary"}

    label = source_label(row, title="ZEUS 年度业绩摘要")

    assert label == "ZEUS 年度业绩摘要"


def test_a_company_source_label_derives_a_readable_name_without_its_id() -> None:
    row = {"kind": "company", "document_id": "zeus-expense-policy"}

    label = source_label(row)

    assert "zeus-expense-policy" not in label
    assert "Zeus Expense Policy" == label


def test_an_opaque_filing_id_never_survives_as_a_label() -> None:
    row = {"kind": "company", "document_id": "AAPL-2022Q3"}

    label = source_label(row)

    assert "AAPL-2022Q3" not in label
    assert "AAPL" not in label


def test_a_finance_source_title_survives_the_projection_through_a_label() -> None:
    """The spec's 「legitimate finance-source title」 case end to end: an
    ordinary person asking about a filing reads the filing's name, and the
    internal ``document_id`` beside it is what gets hidden."""

    row = {"kind": "company", "document_id": "AAPL-2022Q3"}

    label = source_label(row, title=FINANCE_TITLE, corpus_is_finance=True)

    assert "10-Q" in label
    assert "AAPL" in label
    assert "document_id" not in label
    assert row["document_id"] not in label


def test_the_same_finance_title_is_filtered_away_from_a_finance_corpus() -> None:
    row = {"kind": "company", "document_id": "zeus-handbook"}

    label = source_label(row, title=FINANCE_TITLE)

    assert "10-Q" not in label
    assert "AAPL" in label


def test_a_source_label_prefers_the_section_over_a_derived_name() -> None:
    row = {"kind": "company", "document_id": "zeus-handbook"}

    label = source_label(row, section="Section 3 · p.4")

    assert label == "Section 3 · p.4"


def test_an_attachment_source_label_names_the_file_not_its_id() -> None:
    row = {
        "kind": "attachment",
        "attachment_id": "att-9f3c2b",
        "filename": "报销制度.pdf",
    }

    label = source_label(row)

    assert label == "报销制度.pdf"
    assert "att-9f3c2b" not in label


def test_an_unnamed_attachment_still_reads_as_an_attachment() -> None:
    row = {"kind": "attachment", "attachment_id": "att-9f3c2b"}

    assert source_label(row) == "本对话中的附件"


def test_a_ledger_source_label_never_prints_the_view_and_key() -> None:
    row = {
        "kind": "database-row",
        "database_row": {
            "source": "hr_leave_balance",
            "record_key": {"employee_id": "E-8891"},
            "queried_at": "2026-03-14",
            "fields": {"days": 12},
            "summary": "该员工本年假余额 12 天",
        },
    }

    label = source_label(row)

    assert "hr_leave_balance" not in label
    assert "E-8891" not in label
    assert "12 天" in label


def test_a_ledger_row_without_a_summary_reads_as_a_record() -> None:
    row = {
        "kind": "database-row",
        "database_row": {
            "source": "hr_leave_balance",
            "record_key": {"employee_id": "E-8891"},
            "queried_at": "2026-03-14",
        },
    }

    assert source_label(row) == "业务记录"


def test_a_source_label_is_projected_when_a_title_carries_a_leak() -> None:
    row = {"kind": "company", "document_id": "zeus-handbook"}

    label = source_label(row, title="ZEUS 手册（document_id=zeus-handbook）")

    assert "document_id" not in label
    assert "ZEUS 手册" in label


def test_a_citation_row_keeps_its_machine_identity_and_gains_the_label() -> None:
    """The click still has to resolve, so the wire identity stays; only the
    text a human reads is added."""

    row = {
        "kind": "company",
        "document_id": "zeus-handbook",
        "version": "v-" + "a" * 60,
        "unit": 3,
        "display": "page",
        "filename": "",
        "quote": "年假按入职年限计算。",
    }

    projected = project_citation_row(row, title="ZEUS 员工手册")

    assert projected["document_id"] == "zeus-handbook"
    assert projected["version"] == "v-" + "a" * 60
    assert projected["unit"] == 3
    assert projected["quote"] == "年假按入职年限计算。"
    assert projected["source_label"] == "ZEUS 员工手册"


def test_a_citation_row_with_no_title_still_carries_a_readable_label() -> None:
    projected = project_citation_row({"kind": "company", "document_id": "zeus-handbook"})

    assert projected["source_label"] == "Zeus Handbook"


# ----- the four failure classes -----

NOT_READY_INDEX = {"error": "not_ready", "cause": "index", "stage": "index"}
NOT_READY_QUEUE = {"error": "not_ready", "cause": "busy", "stage": "queue"}


def test_index_work_and_busy_carry_their_own_exact_copy() -> None:
    assert INDEX_WORK_COPY == "资料仍在整理中"
    assert BUSY_COPY == "检索繁忙，请稍后重试"


def test_index_work_is_not_read_as_a_busy_backend() -> None:
    assert classify_failure(status_reason="not_ready", readiness=NOT_READY_INDEX) == (
        "index_work"
    )
    assert classify_failure(status_reason="not_ready", readiness=NOT_READY_QUEUE) == (
        "busy"
    )


def test_every_busy_stage_resolves_to_the_same_busy_copy() -> None:
    for stage in ("queue", "startup", "response"):
        readiness = {"error": "not_ready", "cause": "busy", "stage": stage}
        assert failure_copy(
            classify_failure(status_reason="not_ready", readiness=readiness)
        ) == BUSY_COPY


def test_an_unproven_readiness_pair_is_not_read_as_index_work() -> None:
    """Index work is only claimed when the cause AND the stage are the exact
    proven pair; a lookalike envelope falls through to the reason's own class
    rather than borrowing the index sentence.

    The reason is ``insufficient_evidence``, not ``corpus_not_ready``: GE-5
    moved ``corpus_not_ready`` onto its OWN class, so it can no longer stand
    in for the refusal the fall-through lands on."""

    assert (
        classify_failure(
            status_reason="insufficient_evidence",
            readiness={"cause": "index", "stage": "queue"},
        )
        == "evidence_refusal"
    )
    assert (
        classify_failure(
            status_reason="insufficient_evidence",
            readiness={"cause": "busy"},
        )
        == "evidence_refusal"
    )


def test_evidence_refusal_states_the_insufficiency_and_two_rephrasings() -> None:
    refusal = failure_copy(classify_failure(status_reason="insufficient_evidence"))

    assert refusal == EVIDENCE_REFUSAL_COPY
    assert "不足以回答" in refusal
    assert refusal.count("（一）") == 1
    assert refusal.count("（二）") == 1


def test_an_evidence_refusal_invents_no_fact() -> None:
    """The refusal names no figure, no document and no period: a copy that
    asserted one would be an answer the corpus never supported."""

    digits = [ch for ch in EVIDENCE_REFUSAL_COPY if ch.isdigit()]
    assert digits == []
    assert "AAPL" not in EVIDENCE_REFUSAL_COPY
    assert not leaked_fragments(EVIDENCE_REFUSAL_COPY)


def test_the_four_failure_classes_are_four_different_sentences() -> None:
    copies = {
        failure_copy("index_work"),
        failure_copy("busy"),
        failure_copy("evidence_refusal"),
        failure_copy("retrieval_degraded"),
    }

    assert len(copies) == 4


def test_degraded_results_say_the_partial_availability_sentence() -> None:
    degraded = failure_copy("retrieval_degraded")

    assert degraded == DEGRADATION_SAFE_COPY
    assert "部分资料暂不可用，当前结果可能不完整" in degraded


def test_a_degraded_run_prefers_its_degradation_copy_over_a_bare_refusal() -> None:
    """A degraded answer is 可用但必须可见: the notice is what the operator
    reads, and it is not the same sentence as any refusal."""

    assert failure_copy(
        classify_failure(status_reason="corpus_not_ready", degraded=True)
    ) == DEGRADATION_SAFE_COPY


def test_an_unrecognized_terminal_reason_is_named_rather_than_guessed() -> None:
    """A terminal reason outside the table raises so a caller cannot ship a
    refusal with no sentence; it never silently borrows another class."""

    with pytest.raises(UnclassifiableTerminal) as caught:
        classify_failure(status_reason="something_new")

    assert "something_new" in str(caught.value)


def test_attachments_state_they_are_conversation_scoped() -> None:
    assert ATTACHMENT_SCOPE_COPY == "仅在本对话中可用"


# ----- the degradation notice -----

SIGNAL = DegradationSignal(
    degraded=True, degraded_reasons=("vector:attempt_failed", "graph:not_attempted"),
)


def test_the_visible_degradation_line_carries_no_raw_reason() -> None:
    line = degradation_notice_line(SIGNAL)

    assert DEGRADATION_SAFE_COPY in line
    for reason in SIGNAL.degraded_reasons:
        assert reason not in line


def test_the_tool_trace_row_carries_no_raw_reason_either() -> None:
    entry = degradation_notice_entry(SIGNAL, tool_call_id="c-retrieve")

    rendered = json.dumps(entry, ensure_ascii=False)
    for reason in SIGNAL.degraded_reasons:
        assert reason not in rendered
    assert DEGRADATION_SAFE_COPY in rendered


def test_an_undegraded_signal_still_has_no_visible_line() -> None:
    assert degradation_notice_line(DegradationSignal()) == ""


# ----- the contract itself -----


@pytest.mark.parametrize(
    "label",
    (
        "document_id",
        "list_files",
        "get_xbrl_fact",
        "research_issuers",
        "bm25",
        "missing_slots",
        "corpus_not_ready",
        "retrieve_unavailable",
    ),
)
def test_every_forbidden_label_is_actually_projected_out(label: str) -> None:
    assert label not in project_text(f"检索结果说明：{label}")
