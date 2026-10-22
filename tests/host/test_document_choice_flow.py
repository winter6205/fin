"""Document-choice flow: the committed envelope and the graph interrupt.

Two surfaces live here.

TASK 1 pins a confirmed defect: ``task_runtime.decide()`` rebuilt a
``PendingQuestion`` from six fields, so committing a ``document_choice``
through the real ``decide()`` dropped ``purpose``, ``origin_run_id`` and
``choice_details``. The tests below go through ``decide()`` and read the
committed envelope back — value-level checks would not have caught it.

TASK 2 pins the graph slice: the grounded lane consults the admitted
decision and, when it is a ``choice``, interrupts on one document-choice
question and resumes the SAME run on a validated pick.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import (
    DOCUMENT_CHOICE_INTERRUPT_KIND,
    _document_choice_details,
    _document_choice_idempotency_key,
    _document_choice_reply_accepted_this_run,
    _document_choice_reply_already_accepted,
)
from fin.host.grounded.grounded_decision import (
    CANDIDATE_TERMINATOR,
    DocumentCandidate,
    GroundedDecision,
    SourceRef,
)
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import (
    accept_input,
    decide,
    open_pending_question,
    pending_question_payload,
)
from fin.host.task.task_runtime_types import (
    DISPOSITION_AWAIT_REPLY,
    INPUT_MESSAGE,
    INPUT_REPLY,
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    TASK_INPUT_INVALID,
    ChoiceDetail,
    ChoiceSourceRef,
    PendingQuestion,
    ReconciliationProposal,
    TaskRuntimeError,
    TaskRuntimeState,
    offer_document_choice,
)
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort

SEND = "send-door"


def _detail(choice_id: str, source_id: str) -> ChoiceDetail:
    return ChoiceDetail(
        choice_id=choice_id,
        label=f"label-{choice_id}",
        summary=f"summary-{choice_id}",
        source_ref=ChoiceSourceRef(
            source_kind="corpus",
            source_id=source_id,
            version="v1",
            section=None,
        ),
    )


def _document_choice_proposal(origin_run_id: str) -> ReconciliationProposal:
    details = (_detail("doc-a", "src-a"), _detail("doc-b", "src-b"))
    question = offer_document_choice(
        question_id="q-doc",
        task_id="unbound",
        task_version=1,
        prompt="Which document should I use?",
        choice_details=details,
        origin_run_id=origin_run_id,
        input_version=0,
    )
    return ReconciliationProposal(
        kind=INPUT_MESSAGE,
        goal="summarize the filing",
        question=question,
    )


# ----- TASK 1: the decide() message-path rebuild preserves the extension -----


def test_decide_message_preserves_document_choice_extension_fields() -> None:
    """Committing a document choice through ``decide`` keeps all three
    extension fields, bound to the resulting task."""
    state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload="summarize the filing",
        provenance=SEND,
    )
    committed = decide(
        state, accepted, _document_choice_proposal(origin_run_id="run-42")
    )

    question = committed.pending_question
    assert question is not None
    assert question.purpose == PURPOSE_DOCUMENT_CHOICE
    assert question.origin_run_id == "run-42"
    assert [detail.choice_id for detail in question.choice_details] == [
        "doc-a",
        "doc-b",
    ]
    assert question.choices == ("doc-a", "doc-b")
    assert question.task_id == committed.current_task.task_id
    assert committed.reconciliation.disposition == DISPOSITION_AWAIT_REPLY


def test_decide_message_document_choice_survives_the_state_dict_round_trip() -> None:
    """The committed envelope re-reads with the extension fields intact.

    This is the exact read the defect produced as
    ``task_clarification / () / None``: encode, decode, and read back.
    """
    state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload="summarize the filing",
        provenance=SEND,
    )
    committed = decide(
        state, accepted, _document_choice_proposal(origin_run_id="run-42")
    )

    reread = TaskRuntimeState.from_state_dict(committed.to_state_dict())
    question = reread.pending_question
    assert question is not None
    assert question.purpose == PURPOSE_DOCUMENT_CHOICE
    assert question.origin_run_id == "run-42"
    assert tuple(detail.choice_id for detail in question.choice_details) == (
        "doc-a",
        "doc-b",
    )
    # The read surface carries the same three fields to the wire.
    payload = pending_question_payload(reread)
    assert payload is not None
    assert payload["purpose"] == PURPOSE_DOCUMENT_CHOICE
    assert payload["origin_run_id"] == "run-42"
    assert [row["choice_id"] for row in payload["choice_details"]] == [
        "doc-a",
        "doc-b",
    ]


def test_decide_message_binds_document_choice_to_the_resulting_task() -> None:
    """The rebuild still rebinds ``task_id`` / ``task_version`` / ``input_version``
    to the resulting task — the extension must not bypass the binding."""
    state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload="summarize the filing",
        provenance=SEND,
    )
    committed = decide(
        state, accepted, _document_choice_proposal(origin_run_id="run-42")
    )
    question = committed.pending_question
    assert question is not None
    assert question.input_version == accepted.input_version
    assert question.task_version == committed.current_task.task_version


def test_decide_message_still_accepts_a_plain_clarification() -> None:
    """A legacy ``task_clarification`` proposal (no extension) is unchanged."""
    state, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload="compare revenue",
        provenance=SEND,
    )
    question = PendingQuestion(
        question_id="q-plain",
        task_id="",
        task_version=1,
        prompt="Which period should I compare?",
        choices=("2022", "2023"),
        input_version=0,
    )
    committed = decide(
        state,
        accepted,
        ReconciliationProposal(
            kind=INPUT_MESSAGE, goal="compare revenue", question=question
        ),
    )
    stored = committed.pending_question
    assert stored is not None
    assert stored.purpose == "task_clarification"
    assert stored.origin_run_id is None
    assert stored.choice_details == ()


# ----- TASK 1b: publishing a question accepts no input -----


def test_open_pending_question_publishes_without_advancing_input_version() -> None:
    """Publishing a question is transport, not an input.

    The document choice is published long after the opening message was
    reconciled, so nothing is left to reconcile — and manufacturing an
    accepted input would advance ``input_version`` for something the user
    never did.
    """
    started, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload="summarize the filing",
        provenance=SEND,
    )
    reconciled = decide(
        started,
        accepted,
        ReconciliationProposal(kind=INPUT_MESSAGE, goal="summarize the filing"),
    )
    before = reconciled.input_version

    published = open_pending_question(
        reconciled, _document_choice_proposal(origin_run_id="run-42").question
    )

    assert published.input_version == before
    question = published.pending_question
    assert question is not None
    assert question.purpose == PURPOSE_DOCUMENT_CHOICE
    assert question.origin_run_id == "run-42"
    assert [row.choice_id for row in question.choice_details] == ["doc-a", "doc-b"]
    # Bound to the CURRENT task, exactly like the decide() rebuild.
    assert question.task_id == published.current_task.task_id


# ----- TASK 2: the graph slice -----

GROUNDED = "What was AAPL net sales in 2023 Q3?"
DOC_A = "AAPL-2023Q3"
DOC_B = "AAPL-2022Q3"
DOC_C = "MSFT-2023Q1"
KNOWN_IDS = {DOC_A, DOC_B, DOC_C}
RUN_ID = "run-doc-choice-1"


def test_choice_details_use_exact_catalog_identity_for_readable_metadata(
    tmp_path,
) -> None:
    """Synthetic catalog metadata may label a hit only at the same version.

    The synthetic row makes the trusted title/period available without
    copying any source document into a fixture. A stale version keeps the
    opaque label, while both machine source references remain unchanged.
    """
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "filings": [
                    {
                        "document_id": "SYNTH-2022Q3",
                        "company": "Example Company",
                        "period": "2022 Q3",
                        "version": "synthetic-v1",
                        "page_count": 3,
                        "form": "10-Q",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    decision = GroundedDecision(
        outcome="choice",
        candidates=(
            DocumentCandidate(
                choice_id="candidate-current",
                label="SYNTH-2022Q3",
                summary="UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 10-Q.",
                source_ref=SourceRef(
                    source_kind="corpus",
                    source_id="SYNTH-2022Q3",
                    version="synthetic-v1",
                    section=None,
                ),
            ),
            DocumentCandidate(
                choice_id="candidate-stale",
                label="SYNTH-2022Q3",
                summary="UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 10-Q.",
                source_company="Wrong Version Corporation",
                source_ref=SourceRef(
                    source_kind="corpus",
                    source_id="SYNTH-2022Q3",
                    version="stale-v0",
                    section=None,
                ),
            ),
        ),
    )

    details = _document_choice_details(decision, catalog_path=catalog)

    assert details[0].label == "Example Company · 2022 Q3"
    assert details[0].summary == "Reporting period: 2022 Q3."
    assert details[1].label == "资料来源"
    assert details[1].summary == "资料来源"
    assert details[0].source_ref.source_id == details[1].source_ref.source_id
    assert details[0].source_ref.version == "synthetic-v1"
    assert details[1].source_ref.version == "stale-v0"
    for detail in details:
        assert "SYNTH-2022Q3" not in detail.label + detail.summary
        assert "10-Q" not in detail.label + detail.summary


def test_choice_title_is_independent_of_the_hit_summary(
    tmp_path,
) -> None:
    """A ticker-only catalog field does not replace the hit's legal name.

    The title comes from the same admitted hit's company line, while the
    summary comes from its first readable table row. Both remain paired
    with the exact catalog document/version identity.
    """
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "filings": [
                    {
                        "document_id": "SYNTH-AAPL-2022Q3",
                        "company": "AAPL",
                        "period": "2022 Q3",
                        "version": "synthetic-v1",
                        "page_count": 3,
                        "form": "10-Q",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    decision = GroundedDecision(
        outcome="choice",
        candidates=(
            DocumentCandidate(
                choice_id="candidate-current",
                label="资料来源",
                summary=(
                    "资料中列出了“Products $ 60,584 $ 63,355 "
                    "$ 230,901 $ 245,241”。"
                ),
                source_company="Apple Inc.",
                source_ref=SourceRef(
                    source_kind="corpus",
                    source_id="SYNTH-AAPL-2022Q3",
                    version="synthetic-v1",
                    section=None,
                ),
            ),
        ),
    )

    detail = _document_choice_details(decision, catalog_path=catalog)[0]

    assert detail.label == "Apple Inc. · 2022 Q3"
    assert detail.summary == (
        "资料中列出了“Products $ 60,584 $ 63,355 $ 230,901 $ 245,241”。"
    )
    assert "AAPL" not in detail.label + detail.summary
    assert "SYNTH-AAPL-2022Q3" not in detail.label + detail.summary
    assert "10-Q" not in detail.label + detail.summary
    assert detail.source_ref.source_id == "SYNTH-AAPL-2022Q3"
    assert detail.source_ref.section is None

DOC_A_TEXT = (
    "Section 1. Apple reported net sales of 89,493 million dollars for the "
    "fiscal 2023 fourth quarter."
)
DOC_B_TEXT = (
    "Section 1. Apple reported net sales of 82,959 million dollars for the "
    "fiscal 2022 fourth quarter."
)


def _chunk(document_id: str, text: str, unit: int) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(document_id=document_id, version="v" * 8, unit=unit),
        score=1.0,
    )


def _choice_output(*document_ids: str) -> str:
    """One model decision output that explicitly asks for a choice."""
    import json

    payload = {"candidates": [{"document_id": doc} for doc in document_ids]}
    return f"这个问题有两种理解。{CANDIDATE_TERMINATOR}{json.dumps(payload)}"


def _script(answer: str) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": GROUNDED},
                    "id": "c-retrieve-doc",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _compile(answer: str) -> Any:
    model = ScriptedChatModel(script=_script(answer))
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(
            hits_by_query={
                GROUNDED: [
                    _chunk(DOC_A, DOC_A_TEXT, 1),
                    _chunk(DOC_B, DOC_B_TEXT, 1),
                ]
            },
            known_document_ids=set(KNOWN_IDS),
        ),
        known_document_ids=set(KNOWN_IDS),
    )
    # The ``_LockedGraph`` proxy does not expose the inner model; the
    # convention (see ``test_graph_visual_lane``) is to stash it here so
    # a test that resumes a paused run can script the continuation.
    graph.test_model = model
    return graph


def _cfg(thread_id: str, **configurable: Any) -> dict[str, Any]:
    """The run config the SEND DOOR builds, reproduced.

    The accepted input rides run config (acceptance happens before the
    graph runs) and the committed envelope rides the run input state, so
    the tests drive the graph exactly as ``send_message`` does rather than
    short-circuiting the admission door.
    """
    envelope, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload=GROUNDED,
        provenance="workspace_send",
        run_id=RUN_ID,
    )
    return {
        "configurable": {
            "thread_id": thread_id,
            "run_id": RUN_ID,
            "accepted_task_input": accepted.to_state_dict(),
            **configurable,
        }
    }


def _input_state() -> dict[str, Any]:
    envelope, _accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload=GROUNDED,
        provenance="workspace_send",
        run_id=RUN_ID,
    )
    return {
        **reset_input_state(GROUNDED),
        "task_runtime": envelope.to_state_dict(),
    }


def _interrupt_payload(out: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = out.get("__interrupt__") or ()
    if not interrupts:
        return None
    return interrupts[0].value


def _open(answer: str, thread_id: str) -> tuple[Any, dict[str, Any]]:
    g = _compile(answer)
    out = g.invoke(_input_state(), config=_cfg(thread_id))
    return g, out


def test_admitted_choice_pauses_on_the_new_document_choice_interrupt() -> None:
    """A grounded run whose decision is a ``choice`` pauses on the NEW
    pinned kind — never on the 检索档 ``choice_window``."""
    _g, out = _open(_choice_output(DOC_A, DOC_B), "dc-open")
    payload = _interrupt_payload(out)
    assert payload is not None
    assert payload["kind"] == DOCUMENT_CHOICE_INTERRUPT_KIND
    assert payload["kind"] != "choice_window"
    assert payload["purpose"] == PURPOSE_DOCUMENT_CHOICE
    # The payload REFERENCES the committed question — it does not carry a
    # second candidate list.
    assert "choice_details" not in payload


def test_committed_pending_question_holds_the_admitted_candidates() -> None:
    """The single question authority holds one detail per admitted hit,
    with the ids the reply resolves against, plus one trailing Other."""
    _g, out = _open(_choice_output(DOC_A, DOC_B), "dc-committed")
    runtime = TaskRuntimeState.from_state_dict(out["task_runtime"])
    question = runtime.pending_question
    assert question is not None
    assert question.purpose == PURPOSE_DOCUMENT_CHOICE
    assert question.origin_run_id == RUN_ID
    ids = [row.choice_id for row in question.choice_details]
    assert len(ids) == 2
    assert ids == list(question.choices)
    # Exactly one trailing Other on the wire, and it is not a candidate.
    payload = pending_question_payload(runtime)
    assert payload is not None
    assert payload["choices"] == [*ids, OTHER_CHOICE_ID]
    assert OTHER_CHOICE_ID not in ids


def test_candidates_are_backed_by_admitted_hits_only() -> None:
    """A requested ref no admitted hit backs is dropped, not invented."""
    _g, out = _open(_choice_output(DOC_A, "NOT-RETRIEVED-9999"), "dc-admitted")
    runtime = TaskRuntimeState.from_state_dict(out["task_runtime"])
    question = runtime.pending_question
    assert question is not None
    ids = [row.choice_id for row in question.choice_details]
    assert len(ids) == 1
    assert question.choices == tuple(ids)


def test_plain_answer_never_pauses_on_a_document_choice() -> None:
    """A merge decision passes straight through: the admission node is a
    pass-through and no document choice is published."""
    _g, out = _open("Apple reported net sales growth.", "dc-merge")
    assert _interrupt_payload(out) is None
    runtime = TaskRuntimeState.from_state_dict(out["task_runtime"])
    assert runtime.pending_question is None
    assert out["grounded_decision"]["outcome"] != "choice"


def test_resume_with_a_valid_pick_continues_the_same_run_and_commits_scope() -> None:
    """The pick advances ``input_version`` exactly once, reconciles
    through Runtime, and commits the checked scope proposal."""
    g, out = _open(_choice_output(DOC_A, DOC_B), "dc-resume")
    assert _interrupt_payload(out) is not None
    before = TaskRuntimeState.from_state_dict(out["task_runtime"]).input_version
    assert g.checkpoint_run_id("dc-resume") == RUN_ID
    picked = out["task_runtime"]["pending_question"]["choice_details"][0]["choice_id"]

    # A resume carries NO accepted input on its config — the workspace
    # route reuses the checkpoint's run id and passes only the pick, so
    # the reply is accepted by the node, not manufactured by the transport.
    done = g.invoke(
        Command(resume=picked),
        config={"configurable": {"thread_id": "dc-resume", "run_id": RUN_ID}},
    )

    runtime = TaskRuntimeState.from_state_dict(done["task_runtime"])
    assert runtime.input_version == before + 1
    assert runtime.pending_question is None
    # The checked scope proposal committed with the reconciled reply.
    scope = done["source_scope"]
    assert scope["mode"] == "restricted"
    assert [row["source_id"] for row in scope["sources"]] == [DOC_A]
    assert scope["accepted_input_version"] == before + 1
    # SAME run.
    assert g.checkpoint_run_id("dc-resume") == RUN_ID


def _resume(g: Any, thread_id: str, pick: str) -> dict[str, Any]:
    return g.invoke(
        Command(resume=pick),
        config={"configurable": {"thread_id": thread_id, "run_id": RUN_ID}},
    )


def _open_with_two_candidates(thread_id: str) -> tuple[Any, dict[str, Any], str]:
    g, out = _open(_choice_output(DOC_A, DOC_B), thread_id)
    assert _interrupt_payload(out) is not None
    ids = out["task_runtime"]["pending_question"]["choice_details"]
    return g, out, str(ids[0]["choice_id"])


def test_duplicate_pick_is_recognized_in_accepted_history_and_never_increments() -> None:
    """A double click is recognized against ACCEPTED HISTORY.

    Once the first pick committed, a repeat of the same reply is a no-op
    at the graph level: the checkpoint holds no pending node, so the
    continuation re-drives nothing and ``input_version`` cannot move a
    second time. The guard that makes this hold when the node DOES
    re-enter is pinned separately below.
    """
    g, out, picked = _open_with_two_candidates("dc-dup")
    before = TaskRuntimeState.from_state_dict(out["task_runtime"]).input_version

    first = _resume(g, "dc-dup", picked)
    after_first = TaskRuntimeState.from_state_dict(first["task_runtime"])
    assert after_first.input_version == before + 1
    scope_first = first["source_scope"]

    # The SAME reply again — recognized in history, so nothing advances.
    repeat = _resume(g, "dc-dup", picked)
    after_repeat = TaskRuntimeState.from_state_dict(repeat["task_runtime"])
    assert after_repeat.input_version == before + 1
    assert after_repeat.input_version == after_first.input_version
    assert repeat.get("source_scope", scope_first) == scope_first


def test_the_duplicate_guard_refuses_to_re_accept_a_recorded_pick() -> None:
    """The guard the node applies: a pick already in accepted history is
    recognized, so the node returns WITHOUT calling ``accept_input``.

    This is the mechanism that makes the retry idempotent even when the
    pause node re-enters (a concurrent second resume, a host restart).
    """
    _g, out, picked = _open_with_two_candidates("dc-guard")
    opened = TaskRuntimeState.from_state_dict(out["task_runtime"])
    question_id = opened.pending_question.question_id
    assert _document_choice_reply_already_accepted(
        opened, question_id=question_id, choice_id=picked
    ) is False

    accepted_runtime, accepted = accept_input(
        opened,
        kind=INPUT_REPLY,
        payload=None,
        provenance="document_choice_resume",
        question_id=question_id,
        choice_id=_document_choice_idempotency_key(
            opened, question_id=question_id, choice_id=picked
        ),
        run_id=RUN_ID,
    )
    assert accepted.input_version == opened.input_version + 1
    # Now the SAME pick is recognized in history...
    assert _document_choice_reply_already_accepted(
        accepted_runtime, question_id=question_id, choice_id=picked
    ) is True
    # ...while a DIFFERENT pick on the same question is not, and neither
    # is the same pick against a different question.
    assert _document_choice_reply_already_accepted(
        accepted_runtime, question_id=question_id, choice_id="another-pick"
    ) is False
    assert _document_choice_reply_already_accepted(
        accepted_runtime, question_id="a-different-question", choice_id=picked
    ) is False


def test_an_invalid_pick_reopens_the_same_question_and_writes_nothing() -> None:
    """An id naming no candidate reopens the SAME question — it never
    accepts an arbitrary id and never widens the scope."""
    g, out, picked = _open_with_two_candidates("dc-invalid")
    before = TaskRuntimeState.from_state_dict(out["task_runtime"])
    question_id = before.pending_question.question_id

    reopened = _resume(g, "dc-invalid", "not-a-candidate")
    payload = _interrupt_payload(reopened)
    assert payload is not None
    assert payload["question_id"] == question_id
    # No accepted input and no scope write happened.
    unchanged = TaskRuntimeState.from_state_dict(reopened["task_runtime"])
    assert unchanged.input_version == before.input_version
    assert unchanged.pending_question.question_id == question_id
    assert "source_scope" not in reopened

    # A later VALID pick still resolves against the same question.
    done = _resume(g, "dc-invalid", picked)
    settled = TaskRuntimeState.from_state_dict(done["task_runtime"])
    assert settled.input_version == before.input_version + 1
    assert settled.pending_question is None


def test_a_pick_from_another_thread_run_is_refused() -> None:
    """The pick is validated against THIS run: resuming the paused
    checkpoint under a different run id reopens the question instead of
    accepting it."""
    g, out, picked = _open_with_two_candidates("dc-foreign")
    before = TaskRuntimeState.from_state_dict(out["task_runtime"])

    foreign = g.invoke(
        Command(resume=picked),
        config={"configurable": {"thread_id": "dc-foreign", "run_id": "some-other-run"}},
    )
    payload = _interrupt_payload(foreign)
    assert payload is not None
    unchanged = TaskRuntimeState.from_state_dict(foreign["task_runtime"])
    assert unchanged.input_version == before.input_version
    assert "source_scope" not in foreign


def test_picking_other_proposes_no_scope_restriction() -> None:
    """“换个问法” names no source: it re-asks and commits no restriction,
    so the previous (absent) scope stands."""
    g, out, _picked = _open_with_two_candidates("dc-other")
    before = TaskRuntimeState.from_state_dict(out["task_runtime"])

    done = _resume(g, "dc-other", OTHER_CHOICE_ID)
    settled = TaskRuntimeState.from_state_dict(done["task_runtime"])
    assert settled.input_version == before.input_version + 1
    assert settled.pending_question is None
    assert "source_scope" not in done


def test_a_rejected_pick_repeats_without_accumulating_inputs() -> None:
    """Repeated invalid picks never accumulate an accepted input — the
    retry loop is pure validation, so no overflow path exists."""
    g, out, picked = _open_with_two_candidates("dc-retry")
    before = TaskRuntimeState.from_state_dict(out["task_runtime"])
    for bogus in ("x", "y", "z"):
        reopened = _resume(g, "dc-retry", bogus)
        assert _interrupt_payload(reopened) is not None
        runtime = TaskRuntimeState.from_state_dict(reopened["task_runtime"])
        assert runtime.input_version == before.input_version
        assert len(runtime.input_history) == len(before.input_history)
    done = _resume(g, "dc-retry", picked)
    runtime = TaskRuntimeState.from_state_dict(done["task_runtime"])
    assert runtime.input_version == before.input_version + 1
    assert len(runtime.input_history) == len(before.input_history) + 1


# ----- resumed-answer confinement (QA-chain grounded experience, HIGH 2) -----


def test_resumed_answer_confines_itself_to_the_picked_document() -> None:
    """The answer the paused run resumes with carries ONLY the picked
    document's evidence.

    The accepted pick narrows the turn to one source: the resumed
    citation-gate answer must quote the picked document's figure, must
    never quote the unselected document's figure, and must never
    forward the candidate request itself to the answer stream. The
    committed scope projection is the observable narrowing receipt.
    """
    g, out, picked = _open_with_two_candidates("dc-confined")
    # The resumed leaf re-retrieves within the narrowed scope and answers
    # with a merge — two further model steps after the pause.
    g.test_model._index = 0
    g.test_model.script = _script(
        "Apple reported net sales of 89,493 million dollars for the "
        "fiscal 2023 fourth quarter. [1]"
    )
    done = _resume(g, "dc-confined", picked)
    messages = list(done["messages"])
    assert isinstance(messages[-1], AIMessage)
    answer = str(messages[-1].content)
    assert CANDIDATE_TERMINATOR not in answer
    assert "89,493" in answer
    scope = done["source_scope"]
    assert [row["source_id"] for row in scope["sources"]] == [DOC_A]
    # The answer's citation names the PICKED document — the narrowing is
    # observable on the admitted output, not just on the scope receipt.
    assert [row.document_id for row in done["citations"]] == [DOC_A]


def test_resumed_answer_cannot_cite_the_unpicked_document() -> None:
    """Narrowing is load-bearing: the unpicked document is not retrievable.

    Excludes the possibility that the confinement test above passes for a
    reason other than the accepted pick. The resumed run re-retrieves
    under the narrowed scope, so the unselected document's figure never
    enters evidence — an answer that quotes it cannot ground and the run
    admits no citation to it.
    """
    g, out, picked = _open_with_two_candidates("dc-confined-negative")
    g.test_model._index = 0
    # Quote the UNPICKED document's figure. Without narrowing, DOC_B is
    # retrievable and this quote would ground; with narrowing it cannot.
    g.test_model.script = _script(
        "Apple reported net sales of 82,959 million dollars. [1]"
    )
    done = _resume(g, "dc-confined-negative", picked)
    # DOC_B's figure cannot ground under the narrowed scope, so the run
    # refuses rather than admitting an uncited answer.
    assert done["status"] == "refuse"
    assert [row.document_id for row in done["citations"]] == []
    scope = done["source_scope"]
    assert [row["source_id"] for row in scope["sources"]] == [DOC_A]


# ----- the one-decision guard is run-scoped, never thread-wide -----


def _runtime_with_an_accepted_document_reply() -> TaskRuntimeState:
    """A thread that already settled one document choice in an EARLIER run.

    The accepted reply carries the earlier run's ``run_id``, so it is
    exactly the row a thread-wide scan would misread as this run having
    spent its decision budget.
    """
    opened = TaskRuntimeState()
    settled, _accepted = accept_input(
        opened,
        kind=INPUT_REPLY,
        payload=None,
        provenance="document_choice_resume",
        question_id="q-earlier",
        choice_id=_document_choice_idempotency_key(
            opened, question_id="q-earlier", choice_id="doc-old"
        ),
        run_id="an-earlier-run",
    )
    return settled


def test_an_unbound_run_never_inherits_an_earlier_runs_spent_budget() -> None:
    """The UNBOUND fallback admits rather than suppressing the choice.

    ``_document_choice_reply_accepted_this_run`` is a one-way
    "suppress the choice" switch, so an unbound call that scanned the
    thread-wide history would permanently disable document choice for a
    thread after its first pick — the exact defect the run-scoped guard
    removed. With no run identity there is no way to tell this call's
    reply from an older turn's, and the recoverable error is the
    re-offered choice, not the silently suppressed one.
    """
    runtime = _runtime_with_an_accepted_document_reply()
    assert runtime.pending_question is None
    # The row really is there: a thread-wide scan WOULD find it.
    assert any(row.kind == INPUT_REPLY for row in runtime.input_history)
    # BOUND: a different run does not inherit the earlier run's spend.
    assert (
        _document_choice_reply_accepted_this_run(
            runtime, config={"configurable": {"run_id": "a-fresh-run"}}
        )
        is False
    )
    # UNBOUND: the fallback must not fall back to the thread-wide scan.
    assert (
        _document_choice_reply_accepted_this_run(
            runtime, config={"configurable": {"thread_id": "t"}}
        )
        is False
    )
    assert _document_choice_reply_accepted_this_run(runtime, config={}) is False


def test_a_bound_run_still_recognizes_its_own_accepted_reply() -> None:
    """The guard's own case is untouched: the run that accepted the reply
    recognizes its OWN spend and suppresses a second decision call."""
    runtime = _runtime_with_an_accepted_document_reply()
    assert (
        _document_choice_reply_accepted_this_run(
            runtime, config={"configurable": {"run_id": "an-earlier-run"}}
        )
        is True
    )


# ----- idempotency identity (one key, one matcher) -----


def test_idempotency_matcher_accepts_only_the_composite_identity() -> None:
    """A history row storing only the BARE candidate id is a different
    identity from the composite key the writer stores, so it never
    satisfies ``already accepted`` — the composite key is the one
    identity, and the matcher reads nothing looser."""
    _g, out, picked = _open_with_two_candidates("dc-bare")
    opened = TaskRuntimeState.from_state_dict(out["task_runtime"])
    question_id = opened.pending_question.question_id
    bare_runtime, _accepted = accept_input(
        opened,
        kind=INPUT_REPLY,
        payload=None,
        provenance="bare-history-row",
        question_id=question_id,
        choice_id=picked,
        run_id=RUN_ID,
    )
    assert (
        _document_choice_reply_already_accepted(
            bare_runtime, question_id=question_id, choice_id=picked
        )
        is False
    )


# ----- TASK 3: wire exposure -----


@pytest.fixture
def http_app() -> Any:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: Any) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(http_app)


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    import json

    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def _install_graph(http_app: Any, answer: str) -> Any:
    from fin.host.http import get_graph

    g = _compile(answer)
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _rephrase_answer() -> str:
    """A resumed answer the FREE TEXT is what answers.

    Scripted for the ``Other`` leg: the leaf re-retrieves and then answers
    with the operator's own rephrase echoed in prose, so a resume that
    dropped the free text and answered the literal ``"other"`` produces a
    measurably different turn than one that kept it. The quote is a
    grounded span from the retrieved corpus so the citation gate admits
    it.
    """
    return _script(
        "Apple reported net sales of 89,493 million dollars for the "
        "fiscal 2023 fourth quarter. [1]"
    )


def _reply_rows(client: Any, thread_id: str) -> list[dict[str, Any]]:
    """The thread's stored accepted-input rows, oldest first."""
    detail = client.get(f"/api/threads/{thread_id}").json()
    history = detail["task_runtime"]["input_history"]
    return [row for row in history if row["kind"] == INPUT_REPLY]


# ----- HIGH 1: Other preserves the operator's free text (contract line 90) -----

RETHRASE = "换成现金流量表的口径再看一次净利润"


def test_other_posted_to_the_send_door_preserves_the_exact_free_text(
    client: Any, http_app: Any
) -> None:
    """“换个问法” stores the user's EXACT text as the reply payload.

    ``specs/question-task-runtime-contract.md:90``: "Other submits
    ``choice_id=other`` and preserves the user's exact nonempty
    ``free_text`` as the reply payload." A resume that carries only the
    ``other`` token records ``payload: null`` and answers the literal
    word "other" — the operator's rephrase is silently discarded, so the
    stored history is missing exactly the thing the reply is for.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    assert (
        client.post(
            f"/api/threads/{thread_id}/messages", json={"question": GROUNDED}
        ).status_code
        == 200
    )

    paused = client.get(f"/api/threads/{thread_id}").json()
    question = paused["task_runtime"]["pending_question"]
    assert question is not None
    assert OTHER_CHOICE_ID in question["choices"]

    # The resumed leaf re-retrieves and answers; the answer text is
    # scripted, so what is asserted below is the STORED payload, not a
    # coincidence of the script.
    g.test_model._index = 0
    g.test_model.script = _rephrase_answer()

    replied = client.post(
        f"/api/threads/{thread_id}/messages",
        json={
            "question": RETHRASE,
            "question_id": question["question_id"],
            "choice_id": OTHER_CHOICE_ID,
            "free_text": RETHRASE,
        },
    )
    assert replied.status_code == 200

    rows = _reply_rows(client, thread_id)
    assert len(rows) == 1
    # The EXACT submitted text — not a trim, not a paraphrase, not the
    # literal "other".
    assert rows[0]["payload"] == RETHRASE
    # The structured ids survive alongside it: this is still the reply to
    # THIS question with THIS pick.
    assert rows[0]["question_id"] == question["question_id"]
    assert rows[0]["choice_id"] == (
        f"{question['question_id']}/{OTHER_CHOICE_ID}"
    )
    # “换个问法” names no source, so it proposes no restriction.
    assert client.get(f"/api/threads/{thread_id}").json()["source_scope"] is None


def test_other_does_not_answer_the_literal_other_token(
    client: Any, http_app: Any
) -> None:
    """The resumed turn carries the rephrase, never the word "other".

    A ``HumanMessage(content="other")`` is what a node that reads only the
    resume token produces; the run then answers a one-word question. The
    turn's stored question is the resume node's user turn, so reading it
    back off the projection is the observable difference.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})
    paused = client.get(f"/api/threads/{thread_id}").json()
    question = paused["task_runtime"]["pending_question"]

    g.test_model._index = 0
    g.test_model.script = _rephrase_answer()
    replied = client.post(
        f"/api/threads/{thread_id}/messages",
        json={
            "question": RETHRASE,
            "question_id": question["question_id"],
            "choice_id": OTHER_CHOICE_ID,
            "free_text": RETHRASE,
        },
    )
    assert replied.status_code == 200

    rows = _reply_rows(client, thread_id)
    assert [row["payload"] for row in rows] == [RETHRASE]
    assert all(row["payload"] != OTHER_CHOICE_ID for row in rows)


def test_a_candidate_click_stores_no_payload(client: Any, http_app: Any) -> None:
    """The contrast: a CLICK has ``payload=null`` (contract line 90).

    "A click has ``payload=null``, with its original structured IDs
    retained; do not fabricate user-authored prose from the label." So the
    Other fix must NOT make a click carry the candidate's label as prose
    either — the two legs stay distinguishable in stored history.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})
    paused = client.get(f"/api/threads/{thread_id}").json()
    question = paused["task_runtime"]["pending_question"]
    pick = question["choice_details"][0]["choice_id"]

    g.test_model._index = 0
    g.test_model.script = _rephrase_answer()
    replied = client.post(
        f"/api/threads/{thread_id}/messages",
        json={
            "question": "",
            "question_id": question["question_id"],
            "choice_id": pick,
        },
    )
    assert replied.status_code == 200

    rows = _reply_rows(client, thread_id)
    assert len(rows) == 1
    assert rows[0]["payload"] is None
    assert rows[0]["choice_id"] == f"{question['question_id']}/{pick}"
    # The label is presentation metadata, never a stored payload.
    label = question["choice_details"][0]["label"]
    assert label
    assert rows[0]["payload"] != label


def test_an_other_pick_is_still_recognized_as_a_duplicate(
    client: Any, http_app: Any
) -> None:
    """The idempotency key stays ``question_id/other`` (no free text).

    ``specs/question-task-runtime-contract.md:92`` recognizes an identical
    retry by the original tuple, and the composite key the graph stores is
    ``f"{question_id}/{choice_id}"``. Threading the free text through the
    resume must not widen that key, or an identical retry would be
    accepted a second time.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})
    paused = client.get(f"/api/threads/{thread_id}").json()
    question = paused["task_runtime"]["pending_question"]

    g.test_model._index = 0
    g.test_model.script = _rephrase_answer()
    body = {
        "question": RETHRASE,
        "question_id": question["question_id"],
        "choice_id": OTHER_CHOICE_ID,
        "free_text": RETHRASE,
    }
    assert (
        client.post(f"/api/threads/{thread_id}/messages", json=body).status_code
        == 200
    )
    after_first = client.get(f"/api/threads/{thread_id}").json()
    version_first = after_first["task_runtime"]["input_version"]

    # The SAME reply again — recognized in accepted history.
    repeat = client.post(f"/api/threads/{thread_id}/messages", json=body)
    assert repeat.status_code == 200
    final = client.get(f"/api/threads/{thread_id}").json()
    assert final["task_runtime"]["input_version"] == version_first
    rows = _reply_rows(client, thread_id)
    assert len(rows) == 1


def test_the_resume_route_also_preserves_an_other_free_text(
    client: Any, http_app: Any
) -> None:
    """``/resume`` is the other reply door and must keep the free text too.

    ``pickChoice`` drives ``POST /resume``, so a resume body that carries
    only the pick would drop the rephrase exactly as the send door did.
    The door is the one under test; the run still reconciles through the
    same node, so the stored row is the same observable.
    """
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})
    paused = client.get(f"/api/threads/{thread_id}").json()
    question = paused["task_runtime"]["pending_question"]

    resumed = client.post(
        f"/api/threads/{thread_id}/resume",
        json={"pick": OTHER_CHOICE_ID, "free_text": RETHRASE},
    )
    assert resumed.status_code == 200

    rows = _reply_rows(client, thread_id)
    assert len(rows) == 1
    assert rows[0]["payload"] == RETHRASE


# ----- HIGH 2: the answered-reply guard is scoped to THIS question -----

FOLLOW_UP = "那利润呢？"


def test_a_follow_up_after_a_pick_can_open_a_new_document_choice(
    client: Any, http_app: Any
) -> None:
    """Document choice stays available after one pick on the thread.

    ``specs/qa-chain-grounded-experience.md:48-52``: a decision call
    offers a choice "for a retrieved evidence set" — per evidence set,
    not once per thread. A guard that scans the WHOLE thread's input
    history would make the second ambiguous question in the thread's life
    publish nothing and refuse the turn, which is what this pins.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})
    paused = client.get(f"/api/threads/{thread_id}").json()
    answered_question_id = paused["task_runtime"]["pending_question"]["question_id"]
    pick = paused["task_runtime"]["pending_question"]["choice_details"][0][
        "choice_id"
    ]
    g.test_model._index = 0
    g.test_model.script = _rephrase_answer()
    assert (
        client.post(
            f"/api/threads/{thread_id}/resume", json={"pick": pick}
        ).status_code
        == 200
    )
    settled = client.get(f"/api/threads/{thread_id}").json()
    assert settled["task_runtime"]["pending_question"] is None
    assert _reply_rows(client, thread_id), "the first pick must be recorded"

    # A NEW ambiguous question on the SAME thread. The scripted model
    # answers the second send with the candidate request again, so a
    # thread-wide guard would publish nothing and the turn would refuse.
    g.test_model._index = 0
    g.test_model.script = _script(_choice_output(DOC_A, DOC_B))
    second = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": FOLLOW_UP}
    )
    assert second.status_code == 200
    events = dict(_parse_sse_events(second.text))
    assert "document_choice" in events, (
        "a fresh ambiguous question must be able to open a NEW choice; a "
        "thread-wide answered-reply guard suppresses it forever"
    )
    after = client.get(f"/api/threads/{thread_id}").json()
    reopened = after["task_runtime"]["pending_question"]
    assert reopened is not None
    assert reopened["purpose"] == PURPOSE_DOCUMENT_CHOICE
    # A NEW question, not the answered one reopened.
    assert reopened["question_id"] != answered_question_id
    assert reopened["choices"][-1] == OTHER_CHOICE_ID
    assert reopened["choice_details"], "a published choice names its candidates"
    # The accepted restriction RETAINS for a short follow-up (contract
    # line 79 / the transition table), so the candidates this turn can
    # admit are drawn from the narrowed source — one row here, and it is
    # the picked source. What this test pins is that a choice is offered
    # AT ALL: the thread-wide guard published nothing and refused.
    assert reopened["choice_details"][0]["source_ref"]["source_id"] == (
        DOC_A
    )
    assert after["source_scope"]["sources"][0]["source_id"] == DOC_A


def _new_thread(client: Any) -> str:
    created = client.post("/api/threads", json={"first_message": "研究"}).json()
    return created["id"]


def test_thread_projection_exposes_the_pending_document_choice(client: Any, http_app: Any) -> None:
    """Reload reads the paused choice off the thread projection, using the
    FROZEN field spellings and no second question authority."""
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    sent = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": GROUNDED}
    )
    assert sent.status_code == 200

    detail = client.get(f"/api/threads/{thread_id}").json()
    pending = detail["task_runtime"]["pending_question"]
    assert pending is not None
    assert pending["purpose"] == PURPOSE_DOCUMENT_CHOICE
    assert pending["origin_run_id"]
    details = pending["choice_details"]
    assert details and len(details) <= 4
    for row in details:
        assert set(row) >= {"choice_id", "label", "summary", "source_ref"}
        assert set(row["source_ref"]) >= {
            "source_kind",
            "source_id",
            "version",
            "section",
        }
    ids = [row["choice_id"] for row in details]
    assert pending["choices"] == [*ids, OTHER_CHOICE_ID]
    assert detail["pending_interrupt_kind"] == DOCUMENT_CHOICE_INTERRUPT_KIND
    # No accepted restriction yet — which is distinct from a clear.
    assert detail["source_scope"] is None


def test_sse_stream_carries_the_document_choice_frame(client: Any, http_app: Any) -> None:
    """The pause rides its OWN event on the wire, with the same frozen
    spellings, and the paused run emits no terminal frames."""
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    sent = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": GROUNDED}
    )
    assert sent.status_code == 200
    events = _parse_sse_events(sent.text)
    names = [name for name, _ in events]
    assert "document_choice" in names
    assert "choice_window" not in names
    frame = dict(events)[ "document_choice" ]
    assert frame["kind"] == DOCUMENT_CHOICE_INTERRUPT_KIND
    assert frame["choices"][-1] == OTHER_CHOICE_ID
    assert frame["other_label"] == "换个问法"
    # A paused run is not a finished turn.
    assert "citations" not in names


def test_source_scope_appears_only_after_an_accepted_pick(client: Any, http_app: Any) -> None:
    """The accepted scope rides the thread projection only after Runtime
    accepted the reply — never while the question is merely pending."""
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})
    assert client.get(f"/api/threads/{thread_id}").json()["source_scope"] is None

    detail = client.get(f"/api/threads/{thread_id}").json()
    pick = detail["task_runtime"]["pending_question"]["choice_details"][0][
        "choice_id"
    ]
    resumed = client.post(f"/api/threads/{thread_id}/resume", json={"pick": pick})
    assert resumed.status_code == 200

    after = client.get(f"/api/threads/{thread_id}").json()
    assert after["task_runtime"]["pending_question"] is None
    scope = after["source_scope"]
    assert scope["mode"] == "restricted"
    assert scope["sources"][0]["source_id"] == DOC_A


def test_a_pick_posted_to_the_send_door_continues_the_paused_run(
    client: Any, http_app: Any
) -> None:
    """The SEND door continues the paused run — it does not start a fresh one.

    The SPA's choice card submits the reply through
    ``submitTaskReply`` -> ``api.sendMessage`` ->
    ``POST /api/threads/{id}/messages`` (the SEND door). The shared
    contract (``specs/question-task-runtime-contract.md`` lines 21 / 94 /
    96) is explicit: "A validated document reply then resumes its
    original run" and "Keep the same ``run_id``". A send door that accepts
    the reply but then mints a brand-new run id and starts from START
    produces the exact defect this pins:

    * the post-pick turn's ``run_id`` must EQUAL the paused question's
      ``origin_run_id`` (continuity — never a fresh run);
    * the accepted pick commits the ``restricted`` source scope on the
      picked document;
    * the resumed answer's citation names the picked document.

    The pick rides the SEND door with the exact body the SPA posts:
    ``{question: "", question_id, choice_id}``.
    """
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    sent = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": GROUNDED}
    )
    assert sent.status_code == 200

    paused = client.get(f"/api/threads/{thread_id}").json()
    question = paused["task_runtime"]["pending_question"]
    assert question is not None
    origin_run_id = question["origin_run_id"]
    assert origin_run_id
    version_before = paused["task_runtime"]["input_version"]
    pick = question["choice_details"][0]["choice_id"]

    # The resumed leaf re-retrieves inside the narrowed scope and answers
    # with the PICKED document's figure (same script the graph-level
    # confinement test drives).
    g.test_model._index = 0
    g.test_model.script = _script(
        "Apple reported net sales of 89,493 million dollars for the "
        "fiscal 2023 fourth quarter. [1]"
    )

    # The SPA posts the pick to the SEND door, not to ``/resume``.
    replied = client.post(
        f"/api/threads/{thread_id}/messages",
        json={
            "question": "",
            "question_id": question["question_id"],
            "choice_id": pick,
        },
    )
    assert replied.status_code == 200

    after = client.get(f"/api/threads/{thread_id}").json()
    assert after["task_runtime"]["pending_question"] is None
    # The node is the ONE accepter: the reply advances ``input_version``
    # EXACTLY once. A door that also accepted the pick would double-advance
    # (the node's duplicate guard matches only the composite key).
    assert after["task_runtime"]["input_version"] == version_before + 1
    # (a) continuity: the post-pick turn carries the ORIGINAL run id.
    assert after["turns"], "the reply must persist one answered turn"
    assert after["turns"][-1]["run_id"] == origin_run_id
    # (b) the accepted pick commits the restriction to the picked source.
    scope = after["source_scope"]
    assert scope is not None
    assert scope["mode"] == "restricted"
    assert [row["source_id"] for row in scope["sources"]] == [DOC_A]
    # (c) the resumed answer cites the PICKED document.
    cited = [
        row["document_id"]
        for row in after["turns"][-1]["citations"]
        if row.get("document_id")
    ]
    assert cited == [DOC_A]


# ----- T9's receipt half: the count the shared contract binds to this kind -----


def test_a_document_reply_counts_once_onto_the_receipt_it_continues(
    client: Any, http_app: Any
) -> None:
    """``specs/qa-chain-server-runtime.md:43``: "A valid document choice
    or rephrase is one accepted reply ... **accepted_input_count**
    increases once for that newly accepted reply", and
    ``specs/qa-chain-server-runtime.md:108``: "the receipt records that
    authoritative resulting version within the signed 64-bit bound and
    increments this logical turn's **accepted_input_count** once".

    The document reply is accepted INSIDE the graph — the
    ``document_choice_interrupt_node`` is the one accept/reconcile
    authority for this kind — so this door holds no acceptance of its own
    to project. The receipt must therefore learn the version the node
    actually reached, or this acceptance sentence is unmet.
    """
    from fin.host.http import get_thread_store
    from fin.host.thread_store import InMemoryThreadStore
    from fin.host.turn_receipts import default_receipts

    # The receipt is read through the SAME store the mounted doors write
    # to; a locally built one would answer this read with an empty
    # registry rather than the receipt the door actually projected.
    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    g = _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)
    client.post(f"/api/threads/{thread_id}/messages", json={"question": GROUNDED})

    receipts = default_receipts(store)
    paused = receipts.store.list_receipts(thread_id)
    assert len(paused) == 1, "the pause opens exactly one receipt"
    assert paused[0].accepted_input_count == 1


    question = client.get(f"/api/threads/{thread_id}").json()["task_runtime"][
        "pending_question"
    ]
    before = g.task_runtime_state(thread_id).input_version

    g.test_model._index = 0
    g.test_model.script = _rephrase_answer()
    replied = client.post(
        f"/api/threads/{thread_id}/messages",
        json={
            "question": "",
            "question_id": question["question_id"],
            "choice_id": question["choice_details"][0]["choice_id"],
        },
    )
    assert replied.status_code == 200, replied.text

    after = g.task_runtime_state(thread_id).input_version
    assert after == before + 1, "the pick advanced Task Runtime exactly once"
    receipt = receipts.store.list_receipts(thread_id)[0]
    assert receipt.accepted_input_count == 2, (
        "one newly accepted document reply increments this turn's count "
        f"once, not zero (got {receipt.accepted_input_count})"
    )
    assert receipt.accepted_input_version == after, (
        "the receipt records the version Task Runtime's acceptance "
        "reached, not a count it keeps itself"
    )
