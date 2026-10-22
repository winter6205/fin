"""Contract tests for the document-choice extension of the pending question.

Pins specs/question-task-runtime-contract.md "Document clarification and
accepted-input scheduling": the three added ``PendingQuestion`` fields, the
document-choice shape they must satisfy, the 1-4 candidate bound, and the
per-field size limits. Also pins that a ``task_clarification`` question —
every question the host asked before this extension — decodes and encodes
exactly as it did, because the new fields default rather than migrate.

Pure value logic: no store, no network, no model, no I/O.
"""

from __future__ import annotations

import pytest

from fin.host.task.task_runtime_types import (
    MAX_DOCUMENT_CHOICES,
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    PURPOSE_TASK_CLARIFICATION,
    SOURCE_KIND_ATTACHMENT,
    SOURCE_KIND_CORPUS,
    TASK_INPUT_INVALID,
    ChoiceDetail,
    ChoiceSourceRef,
    PendingQuestion,
    TaskRuntimeError,
    TaskRuntimeState,
    offer_document_choice,
)

SEND = "send-door"


def _corpus_ref(source_id: str = "AAPL-2023Q3", version: str = "v1") -> ChoiceSourceRef:
    return ChoiceSourceRef(
        source_kind=SOURCE_KIND_CORPUS, source_id=source_id, version=version
    )


def _attachment_ref(section: str | None = None) -> ChoiceSourceRef:
    return ChoiceSourceRef(
        source_kind=SOURCE_KIND_ATTACHMENT,
        source_id="att-77",
        version="v3",
        section=section,
    )


def _detail(
    index: int,
    *,
    choice_id: str | None = None,
    label: str | None = None,
    summary: str | None = None,
    source_ref: ChoiceSourceRef | None = None,
) -> ChoiceDetail:
    """A well-formed detail, overridable one field at a time so a bound
    case changes exactly the field it names."""
    return ChoiceDetail(
        choice_id=choice_id if choice_id is not None else f"choice-{index}",
        label=label if label is not None else f"候选 {index}",
        summary=(
            summary if summary is not None else f"第 {index} 份资料的一句话概述。"
        ),
        source_ref=(
            source_ref if source_ref is not None else _corpus_ref(f"AAPL-2023Q{index}")
        ),
    )


def _details(count: int) -> tuple[ChoiceDetail, ...]:
    return tuple(_detail(index) for index in range(1, count + 1))


def _document_choice(
    details: tuple[ChoiceDetail, ...] | None = None,
    *,
    origin_run_id: str | None = "run-7",
    choices: tuple[str, ...] | None = None,
    prompt: str = "要用哪一份资料对比？",
) -> PendingQuestion:
    """A document choice over ``details``, defaulting to its own ids.

    ``choices`` is only passed by the cases that deliberately mismatch the
    detail ids, so every other case exercises the derived ones.
    """
    rows = _details(2) if details is None else details
    return PendingQuestion(
        question_id="q-doc",
        task_id="task-1",
        task_version=1,
        prompt=prompt,
        choices=(
            tuple(row.choice_id for row in rows) if choices is None else choices
        ),
        input_version=1,
        purpose=PURPOSE_DOCUMENT_CHOICE,
        origin_run_id=origin_run_id,
        choice_details=rows,
    )


def _clarification() -> PendingQuestion:
    return PendingQuestion(
        question_id="q1",
        task_id="task-1",
        task_version=1,
        prompt="Which period should I compare?",
        choices=("2022", "2023"),
        input_version=1,
    )


# ----- the defaults keep every existing question unchanged -----

def test_task_clarification_is_the_default_purpose() -> None:
    """Contract: ``purpose`` defaults to ``task_clarification`` on existing
    questions, which carry no choice details and no origin run."""
    question = _clarification()
    assert question.purpose == PURPOSE_TASK_CLARIFICATION
    assert question.origin_run_id is None
    assert question.choice_details == ()
    assert PURPOSE_TASK_CLARIFICATION == "task_clarification"


def test_task_clarification_stores_no_choice_details_and_emits_them_empty() -> None:
    """Contract: a ``task_clarification`` has ``choice_details=[]`` and may
    have ``origin_run_id=null``; both are stored, not omitted."""
    stored = _clarification().to_state_dict()
    assert stored["purpose"] == PURPOSE_TASK_CLARIFICATION
    assert stored["origin_run_id"] is None
    assert stored["choice_details"] == []
    # The stored list still carries exactly one trailing Other.
    assert stored["choices"] == ["2022", "2023", OTHER_CHOICE_ID]


def test_legacy_stored_question_without_the_new_keys_reads_as_today() -> None:
    """Contract: no backfill — a checkpoint written before the extension has
    no ``purpose``/``origin_run_id``/``choice_details`` and must decode into
    exactly the question it would have produced then, fabricating nothing."""
    restored = TaskRuntimeState.from_state_dict(
        {
            "pending_question": {
                "question_id": "q1",
                "task_id": "task-1",
                "task_version": 1,
                "prompt": "Which period should I compare?",
                "choices": ["2022", "2023", OTHER_CHOICE_ID],
                "input_version": 1,
            }
        }
    )
    assert restored.pending_question == _clarification()
    assert restored.pending_question.purpose == PURPOSE_TASK_CLARIFICATION
    assert restored.pending_question.origin_run_id is None
    assert restored.pending_question.choice_details == ()
    assert restored.to_state_dict() == TaskRuntimeState(
        pending_question=_clarification()
    ).to_state_dict()


def test_task_clarification_is_not_held_to_the_document_choice_limits() -> None:
    """Contract: "these limits do not retroactively truncate unrelated
    existing task questions" — an old long clarification stays valid."""
    long_prompt = "请补充" + "很长的信息" * 2000
    question = PendingQuestion(
        question_id="q1",
        task_id="task-1",
        task_version=1,
        prompt=long_prompt,
        choices=("2022",),
        input_version=1,
    )
    assert question.prompt == long_prompt


# ----- the document-choice round trip -----

@pytest.mark.parametrize("count", [1, 2, 3, MAX_DOCUMENT_CHOICES])
def test_document_choice_round_trips_all_three_new_fields(count: int) -> None:
    """Contract: one to four distinct admitted source choices, whose
    ``choice_details`` IDs equal the non-Other IDs in the same order."""
    question = _document_choice(_details(count))
    stored = question.to_state_dict()
    assert stored["purpose"] == PURPOSE_DOCUMENT_CHOICE
    assert stored["origin_run_id"] == "run-7"
    assert [row["choice_id"] for row in stored["choice_details"]] == [
        row.choice_id for row in question.choice_details
    ]
    assert stored["choices"] == [
        *(row.choice_id for row in question.choice_details),
        OTHER_CHOICE_ID,
    ]
    restored = TaskRuntimeState.from_state_dict({"pending_question": stored})
    assert restored.pending_question == question


def test_document_choice_stores_the_frozen_envelope_projection() -> None:
    """Contract "Wire projection": the stored question carries the complete
    document-choice projection, so a reload needs no execution resume."""
    stored = _document_choice(_details(2)).to_state_dict()
    assert stored == {
        "question_id": "q-doc",
        "task_id": "task-1",
        "task_version": 1,
        "prompt": "要用哪一份资料对比？",
        "choices": ["choice-1", "choice-2", "other"],
        "input_version": 1,
        "purpose": "document_choice",
        "origin_run_id": "run-7",
        "choice_details": [
            {
                "choice_id": "choice-1",
                "label": "候选 1",
                "summary": "第 1 份资料的一句话概述。",
                "source_ref": {
                    "source_kind": "corpus",
                    "source_id": "AAPL-2023Q1",
                    "version": "v1",
                    "section": None,
                },
            },
            {
                "choice_id": "choice-2",
                "label": "候选 2",
                "summary": "第 2 份资料的一句话概述。",
                "source_ref": {
                    "source_kind": "corpus",
                    "source_id": "AAPL-2023Q2",
                    "version": "v1",
                    "section": None,
                },
            },
        ],
    }


def test_document_choice_detail_stores_the_frozen_source_ref_shape() -> None:
    """Contract ``source_ref``: ``source_kind``, ``source_id``, ``version``,
    ``section`` — spelled here once so a wire reader has an exact example."""
    detail = _detail(
        1, source_ref=ChoiceSourceRef(
            source_kind=SOURCE_KIND_ATTACHMENT,
            source_id="att-77",
            version="v3",
            section="合并利润表",
        )
    )
    stored = _document_choice((detail,)).to_state_dict()
    assert stored["choice_details"] == [
        {
            "choice_id": "choice-1",
            "label": "候选 1",
            "summary": "第 1 份资料的一句话概述。",
            "source_ref": {
                "source_kind": "attachment",
                "source_id": "att-77",
                "version": "v3",
                "section": "合并利润表",
            },
        }
    ]


def test_document_choice_without_a_section_reads_back_as_null() -> None:
    """Contract: ``section`` is ``string | null``; an absent section is
    absent, not an empty string that looks like a named section."""
    stored = _document_choice().to_state_dict()
    assert stored["choice_details"][0]["source_ref"]["section"] is None
    restored = TaskRuntimeState.from_state_dict({"pending_question": stored})
    assert restored.pending_question == _document_choice()


def test_document_choice_offered_choices_ends_with_exactly_one_other() -> None:
    """Contract: exactly one trailing ``other``, supplied by the projection
    rather than by the candidate list."""
    question = _document_choice(_details(3))
    offered = question.offered_choices
    assert offered == ("choice-1", "choice-2", "choice-3", OTHER_CHOICE_ID)
    assert offered.count(OTHER_CHOICE_ID) == 1
    assert OTHER_CHOICE_ID not in question.choices


def test_max_document_choices_is_the_shared_contract_four() -> None:
    """Contract: the candidate bound is the shared "at most four readable
    candidates" rule, so the number has ONE definition."""
    assert MAX_DOCUMENT_CHOICES == 4


# ----- the refusals: shape -----

def test_document_choice_without_details_is_refused() -> None:
    """Contract: ``choice_details`` IDs equal the non-Other IDs, so a
    document choice with none has no answerable candidate."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(())
    assert err.value.kind == TASK_INPUT_INVALID


def test_document_choice_beyond_four_details_is_refused() -> None:
    """Contract: at most four admitted candidates, plus the trailing Other."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(_details(MAX_DOCUMENT_CHOICES + 1))
    assert err.value.kind == TASK_INPUT_INVALID


@pytest.mark.parametrize("origin_run_id", [None, "", "   "])
def test_document_choice_without_an_origin_run_is_refused(origin_run_id: str | None) -> None:
    """Contract: a new ``document_choice`` requires its original nonempty
    ``origin_run_id`` — the reply must resume THAT run."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(origin_run_id=origin_run_id)
    assert err.value.kind == TASK_INPUT_INVALID


def test_document_choice_choices_must_match_the_detail_ids() -> None:
    """Contract: ``choices`` holds exactly the ``choice_details`` IDs in the
    same order, so a reply's ``choice_id`` can be resolved without guessing."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(choices=("choice-1",))
    assert err.value.kind == TASK_INPUT_INVALID
    with pytest.raises(TaskRuntimeError) as swapped:
        _document_choice(choices=("choice-2", "choice-1"))
    assert swapped.value.kind == TASK_INPUT_INVALID


def test_duplicate_choice_ids_are_refused() -> None:
    """Contract: the candidates are DISTINCT admitted hits; a repeated ID
    would make one reply look like two candidates."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice((_detail(1), _detail(2, choice_id="choice-1")))
    assert err.value.kind == TASK_INPUT_INVALID


def test_unknown_source_kind_is_refused() -> None:
    """Contract ``source_ref.source_kind``: the closed pair
    ``corpus | attachment`` — an unknown kind is never passed through."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(
            (_detail(1, source_ref=ChoiceSourceRef(
                source_kind="spreadsheet",  # type: ignore[arg-type]
                source_id="s-1",
                version="v1",
            )),)
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_the_other_token_is_never_a_document_choice_candidate() -> None:
    """Contract: exactly one ``other``, always trailing, supplied by
    ``offered_choices`` — so a candidate may not BE Other, which would
    store the token twice and make the free-text path ambiguous."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice((_detail(1, choice_id=OTHER_CHOICE_ID),))
    assert err.value.kind == TASK_INPUT_INVALID


def test_source_kinds_are_the_closed_pair() -> None:
    """Contract: the vocabulary is spelled here once."""
    assert (SOURCE_KIND_CORPUS, SOURCE_KIND_ATTACHMENT) == ("corpus", "attachment")
    assert PURPOSE_DOCUMENT_CHOICE == "document_choice"


# ----- the refusals: per-field limits -----

# Contract: IDs and source versions are nonempty and at most 256 UTF-8
# bytes; labels and section labels at most 512; summaries nonempty and at
# most 1,024. One entry per field so each violation is exercised — and
# named — on its own rather than behind a single blanket refusal.
BOUND_CASES: dict[str, dict[str, object]] = {
    "choice_id_over_256_bytes": {"choice_id": "c" * 257},
    "choice_id_empty": {"choice_id": ""},
    "label_over_512_bytes": {"label": "l" * 513},
    "empty_summary": {"summary": ""},
    "summary_over_1024_bytes": {"summary": "s" * 1025},
    "control_character_in_choice_id": {"choice_id": "choice\n1"},
    "control_character_in_label": {"label": "候选\n1"},
    "source_id_over_256_bytes": {"source_ref": _corpus_ref(source_id="s" * 257)},
    "source_id_empty": {"source_ref": _corpus_ref(source_id="")},
    "version_over_256_bytes": {"source_ref": _corpus_ref(version="v" * 257)},
    "section_over_512_bytes": {
        "source_ref": _attachment_ref(section="t" * 513)
    },
    "control_character_in_source_id": {
        "source_ref": _corpus_ref(source_id="s\n1")
    },
    "control_character_in_section": {
        "source_ref": _attachment_ref(section="合并\n利润表")
    },
}


@pytest.mark.parametrize("case", sorted(BOUND_CASES))
def test_document_choice_field_limits_are_refused_individually(
    case: str,
) -> None:
    """Contract: "reject an oversized proposal before publishing it, without
    truncating away material meaning" — every field carries its own bound
    and each violation is refused rather than shortened to fit."""
    overrides = {
        "choice_id": "choice-1",
        "label": "候选 1",
        "summary": "第 1 份资料的一句话概述。",
        "source_ref": _corpus_ref(),
        **BOUND_CASES[case],
    }
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice((_detail(1, **overrides),))
    assert err.value.kind == TASK_INPUT_INVALID, case


def test_document_choice_with_an_empty_prompt_is_refused() -> None:
    """Contract: "the complete question is nonempty" — a document choice
    with no prompt cannot be answered by any candidate."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(prompt="   ")
    assert err.value.kind == TASK_INPUT_INVALID


def test_document_choice_prompt_over_8192_bytes_is_refused() -> None:
    """Contract: the complete question is at most 8,192 bytes, checked
    against the complete prompt rather than truncated to fit."""
    with pytest.raises(TaskRuntimeError) as err:
        _document_choice(prompt="x" * 8193)
    assert err.value.kind == TASK_INPUT_INVALID


def test_document_choice_within_every_limit_is_accepted() -> None:
    """Contract: a bound is an inclusive ceiling, so the value AT the limit
    is admitted rather than refused."""
    question = _document_choice(
        (
            _detail(
                1,
                choice_id="c" * 256,
                label="l" * 512,
                summary="s" * 1024,
                source_ref=ChoiceSourceRef(
                    source_kind=SOURCE_KIND_CORPUS,
                    source_id="s" * 256,
                    version="v" * 256,
                    section="t" * 512,
                ),
            ),
        ),
        prompt="p" * 8192,
    )
    assert question.to_state_dict()["choice_details"][0]["summary"] == "s" * 1024


def test_document_choice_prose_may_contain_ordinary_line_breaks() -> None:
    """Contract: "prose may contain ordinary line breaks" — only
    identifiers and labels reject control characters."""
    question = _document_choice(
        (_detail(1, summary="第一行概述\n第二行概述"),),
        prompt="要用哪一份资料对比？\n（也可以换个问法）",
    )
    assert question.choice_details[0].summary == "第一行概述\n第二行概述"


# ----- task-clarification option bounds (same family as document choice) -----


def _clarification_with(choices: tuple[str, ...], prompt: str = "Which period?") -> PendingQuestion:
    return PendingQuestion(
        question_id="q1",
        task_id="task-1",
        task_version=1,
        prompt=prompt,
        choices=choices,
        input_version=1,
    )


def test_a_clarification_beyond_the_shared_choice_bound_is_refused() -> None:
    """The option card is bounded by the SAME four-candidate rule as a
    document choice; the value type refuses a longer list instead of
    letting a renderer truncate it."""
    choices = tuple(f"option-{index}" for index in range(MAX_DOCUMENT_CHOICES + 1))
    with pytest.raises(TaskRuntimeError) as err:
        _clarification_with(choices)
    assert err.value.kind == TASK_INPUT_INVALID
    assert MAX_DOCUMENT_CHOICES == 4


def test_a_clarification_option_over_the_label_byte_ceiling_is_refused() -> None:
    """Labels are bounded in UTF-8 bytes, measured exactly like a
    document-choice label."""
    with pytest.raises(TaskRuntimeError) as err:
        _clarification_with(("x" * (512 + 1),))
    assert err.value.kind == TASK_INPUT_INVALID
    # The ceiling is inclusive: exactly 512 bytes is accepted.
    assert _clarification_with(("x" * 512,)).choices == ("x" * 512,)


@pytest.mark.parametrize("label", ["bad\tlabel", "bad\x00label", "bad\x1blabel"])
def test_a_clarification_option_with_a_control_character_is_refused(label: str) -> None:
    with pytest.raises(TaskRuntimeError) as err:
        _clarification_with((label,))
    assert err.value.kind == TASK_INPUT_INVALID


def test_a_blank_clarification_option_is_refused() -> None:
    with pytest.raises(TaskRuntimeError) as err:
        _clarification_with(("   ",))
    assert err.value.kind == TASK_INPUT_INVALID


def test_duplicate_clarification_options_are_refused_case_insensitively() -> None:
    """Two buttons the operator cannot tell apart are one option, so the
    duplicate is refused rather than silently collapsed."""
    with pytest.raises(TaskRuntimeError) as err:
        _clarification_with(("ZEUS 年假", "zeus 年假"))
    assert err.value.kind == TASK_INPUT_INVALID


def test_the_reserved_other_token_is_last_and_single() -> None:
    """The free-text-only shape stores Other as its only choice; a real
    option list lets the projection append the one trailing token."""
    free_text_only = _clarification_with((OTHER_CHOICE_ID,))
    assert free_text_only.offered_choices == (OTHER_CHOICE_ID,)
    listed = _clarification_with(("2022", "2023"))
    assert listed.offered_choices == ("2022", "2023", OTHER_CHOICE_ID)
    with pytest.raises(TaskRuntimeError) as err:
        _clarification_with((OTHER_CHOICE_ID, "2022"))
    assert err.value.kind == TASK_INPUT_INVALID


def test_a_task_clarification_with_no_current_task_is_not_published() -> None:
    """The publication seam refuses a task-bound question with no task.

    The wire shape requires a real ``task_id`` (the workspace decoder
    rejects an empty one), so publishing it would strand an unanswerable
    card. A document choice keeps its contractually empty id.
    """
    from fin.host.task.task_runtime import open_pending_question

    with pytest.raises(TaskRuntimeError) as err:
        open_pending_question(TaskRuntimeState(), _clarification())
    assert err.value.kind == TASK_INPUT_INVALID
    assert err.value.kind == "task_input_invalid"

    detail = _detail(1)
    document_choice = offer_document_choice(
        question_id="q-doc",
        task_id="",
        task_version=1,
        prompt="要用哪一份资料对比？",
        choice_details=(detail,),
        origin_run_id="run-7",
        input_version=0,
    )
    published = open_pending_question(TaskRuntimeState(), document_choice)
    assert published.pending_question is not None
    assert published.pending_question.task_id == ""


# ----- the constructor -----

def test_offer_document_choice_publishes_the_admitted_candidates() -> None:
    """Contract: a document choice publishes one to four admitted hits in
    order, with the Other handled by the projection, not stored twice."""
    question = offer_document_choice(
        question_id="q-doc",
        task_id="task-1",
        task_version=2,
        prompt="要用哪一份资料对比？",
        choice_details=_details(3),
        origin_run_id="run-7",
        input_version=4,
    )
    assert question.purpose == PURPOSE_DOCUMENT_CHOICE
    assert question.origin_run_id == "run-7"
    assert question.choices == ("choice-1", "choice-2", "choice-3")
    assert question.choice_details == _details(3)
    assert question.offered_choices[-1] == OTHER_CHOICE_ID


@pytest.mark.parametrize("count", [0, MAX_DOCUMENT_CHOICES + 1])
def test_offer_document_choice_refuses_a_candidate_count_outside_one_to_four(
    count: int,
) -> None:
    """Contract: the factory refuses the bound rather than dropping a
    candidate, which would silently publish a question the user cannot
    fully answer."""
    with pytest.raises(TaskRuntimeError) as err:
        offer_document_choice(
            question_id="q-doc",
            task_id="task-1",
            task_version=1,
            prompt="要用哪一份资料对比？",
            choice_details=_details(count),
            origin_run_id="run-7",
            input_version=1,
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_offer_document_choice_refuses_duplicate_candidate_ids() -> None:
    """Contract: the candidates are DISTINCT admitted hits."""
    with pytest.raises(TaskRuntimeError) as err:
        offer_document_choice(
            question_id="q-doc",
            task_id="task-1",
            task_version=1,
            prompt="要用哪一份资料对比？",
            choice_details=(_detail(1), _detail(2, choice_id="choice-1")),
            origin_run_id="run-7",
            input_version=1,
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_offer_document_choice_refuses_an_out_of_bound_source_ref() -> None:
    """Contract: the bound is checked before publishing, not after."""
    with pytest.raises(TaskRuntimeError) as err:
        offer_document_choice(
            question_id="q-doc",
            task_id="task-1",
            task_version=1,
            prompt="要用哪一份资料对比？",
            choice_details=(
                _detail(1, source_ref=_corpus_ref(version="v" * 257)),
            ),
            origin_run_id="run-7",
            input_version=1,
        )
    assert err.value.kind == TASK_INPUT_INVALID


@pytest.mark.parametrize("origin_run_id", [None, ""])
def test_offer_document_choice_refuses_a_missing_origin_run(
    origin_run_id: str | None,
) -> None:
    """Contract: the reply resumes the originating run, so the run id is
    required rather than inferred later."""
    with pytest.raises(TaskRuntimeError) as err:
        offer_document_choice(
            question_id="q-doc",
            task_id="task-1",
            task_version=1,
            prompt="要用哪一份资料对比？",
            choice_details=_details(1),
            origin_run_id=origin_run_id,
            input_version=1,
        )
    assert err.value.kind == TASK_INPUT_INVALID


# ----- the envelope -----

def test_document_choice_survives_the_envelope_round_trip() -> None:
    """Contract: ``pending_question`` is the single question/candidate
    authority, including while the graph is interrupted, so the envelope
    carries the whole document-choice projection."""
    question = _document_choice(_details(2))
    state = TaskRuntimeState(pending_question=question, input_version=1)
    stored = state.to_state_dict()
    assert stored["pending_question"]["choice_details"][0]["choice_id"] == "choice-1"
    assert stored["pending_question"]["purpose"] == PURPOSE_DOCUMENT_CHOICE
    assert TaskRuntimeState.from_state_dict(stored) == state


def test_document_choice_labels_may_carry_ordinary_spacing_characters() -> None:
    """Contract: "reject control characters in identifiers/labels" — only
    control characters. A no-break or ideographic space is ordinary text a
    title may contain, so refusing it would refuse a legitimate document."""
    detail = _detail(1, label="AAPL 2023 年报　摘要")
    question = _document_choice((detail,))
    assert question.choice_details[0].label == "AAPL 2023 年报　摘要"


def test_a_stored_document_choice_that_contradicts_itself_is_refused_on_read() -> None:
    """Contract: a document choice's ``choice_details`` IDs equal its
    non-Other ``choices``.

    The other envelope fields degrade to a conservative default on an
    unrecognisable stored token, but this one is not degradable: a stored
    question whose ids do not resolve to its own details would present a
    reply target that names no admitted hit, so the read is refused rather
    than repaired. The contract leaves that choice open — this pins it as
    "refuse, never silently downgrade to a clarification".
    """
    with pytest.raises(TaskRuntimeError) as err:
        TaskRuntimeState.from_state_dict(
            {
                "pending_question": {
                    "question_id": "q-doc",
                    "task_id": "task-1",
                    "task_version": 1,
                    "prompt": "要用哪一份资料对比？",
                    "choices": ["choice-1", OTHER_CHOICE_ID],
                    "input_version": 1,
                    "purpose": PURPOSE_DOCUMENT_CHOICE,
                    "origin_run_id": "run-7",
                    "choice_details": [],
                }
            }
        )
    assert err.value.kind == TASK_INPUT_INVALID


def test_a_stored_unknown_purpose_degrades_to_the_clarification_default() -> None:
    """Contract: the envelope reads tolerate a token this build does not
    know, because one unrecognisable field must not strand a whole thread.
    An unknown purpose carries no candidate detail, so the conservative
    default is the reading that keeps the listed choices answerable."""
    restored = TaskRuntimeState.from_state_dict(
        {
            "pending_question": {
                "question_id": "q1",
                "task_id": "task-1",
                "task_version": 1,
                "prompt": "Which period?",
                "choices": ["2022", OTHER_CHOICE_ID],
                "input_version": 1,
                "purpose": "followup",
            }
        }
    )
    assert restored.pending_question.purpose == PURPOSE_TASK_CLARIFICATION
    assert restored.pending_question.choices == ("2022",)


def test_a_purpose_outside_the_closed_vocabulary_is_refused() -> None:
    """Contract: ``purpose`` is the closed pair — an unknown token is
    refused rather than stored as an unreadable question."""
    with pytest.raises(TaskRuntimeError) as err:
        PendingQuestion(
            question_id="q1",
            task_id="task-1",
            task_version=1,
            prompt="Which?",
            choices=("a",),
            input_version=1,
            purpose="followup",  # type: ignore[arg-type]
        )
    assert err.value.kind == TASK_INPUT_INVALID
