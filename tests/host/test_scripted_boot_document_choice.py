"""The ``document_choice`` scripted scenario (QA-chain T9 / GE-4).

The browser lane (``scripts/run-scripted-e2e.mjs``) can only observe what
its transcript makes happen. Before this scenario the lane's transcripts
were ``grounded`` and ``bounce`` — both of which answer — so the whole
grounded document-choice surface (the ``document_choice`` pause, the
choice card, the pick, the resumed run) had no browser coverage at all.

These tests pin the transcript itself at the host level, which is what the
browser check drives:

* the run really PAUSES on a ``document_choice`` interrupt, carrying the
  run id the question names (the receipt identity GE-4 asks the browser to
  preserve);
* the pause publishes two candidates, and BOTH are hit-backed — the
  transcript READS both filings, because admission drops a candidate no
  admitted hit backs, so a transcript that named two without reading two
  would publish a one-row card;
* the resumed run answers inside the picked filing only: it quotes the
  picked page, cites only the picked document, and never mentions the
  unpicked one;
* the run id is the SAME across the pause and the resume, which is the
  "preserves the original run" half of GE-4;
* the transcript is hermetic and deterministic — no provider client, and a
  second send on a fresh thread replays the same pause.

What these tests deliberately do NOT pin is the transport: the browser
check owns the card, the pick and the resume, because those are DOM and
SSE observations a host test cannot make.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fin.host.graph import DOCUMENT_CHOICE_INTERRUPT_KIND
from fin.host.grounded.grounded_decision import CANDIDATE_TERMINATOR
from fin.host.scripted_boot import (
    SCRIPTED_ALTERNATE_DOCUMENT_ID,
    SCRIPTED_BOOT_ENV_VAR,
    SCRIPTED_DELTA_MS_ENV_VAR,
    SCRIPTED_DOCUMENT_ID,
    SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
    SCRIPTED_SCENARIO_ENV_VAR,
    SCRIPTED_SCENARIOS,
    ScriptedBootConfigError,
    ScriptedBootFixtureError,
    build_scripted_transcript,
    compile_for_scripted_e2e,
    scripted_scenario,
)
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import accept_input
from fin.host.task.task_runtime_types import (
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    TaskRuntimeState,
)


NO_PACING = {SCRIPTED_BOOT_ENV_VAR: "1", SCRIPTED_DELTA_MS_ENV_VAR: "0"}
CHOICE_ENV = {**NO_PACING, SCRIPTED_SCENARIO_ENV_VAR: SCRIPTED_SCENARIO_DOCUMENT_CHOICE}

# The lane question, and it is part of what these tests pin: the document
# choice is only reachable on the ``grounded`` lane, and the rules tier
# reads this wording as ``grounded`` because it names a company, a metric
# and a period. A vaguer question ("What was Apple's total revenue?",
# which the rules tier answers with a clarify leaf) would run this whole
# transcript to a clarify answer and never reach the admission node — the
# lane would boot, and its card assertion would be vacuous.
QUESTION = "What was Apple's total revenue in Q3 2022?"

RUN_ID = "scripted-document-choice-run"


# ----- the scenario is a declared, opt-in transcript -----


def test_document_choice_is_one_of_the_declared_scenarios() -> None:
    """The lane validates its ``--scenario`` flag against this tuple.

    Without the membership the lane could name a transcript the boot
    refuses, and a lane that refuses to boot reports "could not run" —
    which is exit 2, never a pass, but also never the evidence GE-4
    asks for.
    """
    assert SCRIPTED_SCENARIO_DOCUMENT_CHOICE in SCRIPTED_SCENARIOS
    assert scripted_scenario(CHOICE_ENV) == SCRIPTED_SCENARIO_DOCUMENT_CHOICE


def test_the_candidate_filing_is_a_second_document_not_a_second_read() -> None:
    """The choice needs two DISTINCT filings.

    One document can never yield a pick: ``select_candidates`` dedups by
    document identity, so a transcript naming the same filing twice would
    publish one candidate and a card with nothing to choose.
    """
    assert SCRIPTED_ALTERNATE_DOCUMENT_ID != SCRIPTED_DOCUMENT_ID


def test_an_unknown_scenario_still_stops_the_boot() -> None:
    """Adding a third transcript must not soften the refusal.

    A misspelled scenario falling back to ``grounded`` would report a
    green document-choice check on a run that never paused.
    """
    with pytest.raises(ScriptedBootConfigError) as exc:
        scripted_scenario({SCRIPTED_SCENARIO_ENV_VAR: "document-choise"})
    assert SCRIPTED_SCENARIO_ENV_VAR in str(exc.value)


# ----- the transcript shape -----


def _transcript_texts(
    transcript: list[list[Any]],
) -> list[str]:
    return ["".join(chunk.content for chunk in step) for step in transcript]


def _read_source_args(transcript: list[list[Any]]) -> list[str]:
    """The ``read_source`` ARGS of every read step, in transcript order.

    Read from the tool-call args rather than the joined chunk text: the
    args live on the chunk that carries them, and a step's tool name and
    its args are split across chunks on purpose.
    """
    args: list[str] = []
    for step in transcript:
        for chunk in step:
            for call in chunk.tool_call_chunks or ():
                if call.get("args"):
                    args.append(str(call["args"]))
    return args


def test_the_transcript_reads_both_filings_before_it_asks() -> None:
    """Both candidates are backed by a page the run actually READ.

    Admission drops a ref no admitted hit backs, so a transcript that
    NAMED two candidates without reading them would publish a card with
    one row — and the lane would still boot. Reading both is what makes
    the second row real rather than requested.
    """
    transcript = build_scripted_transcript(
        document_id=SCRIPTED_DOCUMENT_ID,
        version="v" * 8,
        unit=1,
        unit_text="A" * 400,
        scenario=SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
        alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
        alternate_version="w" * 8,
        alternate_unit=1,
        alternate_unit_text="B" * 400,
    )
    reads = _read_source_args(transcript)
    assert len(reads) == 3, "two candidate reads, then one re-read on resume"
    assert SCRIPTED_DOCUMENT_ID in reads[0]
    assert SCRIPTED_ALTERNATE_DOCUMENT_ID in reads[1]
    # The resume leg re-reads the PICKED filing: the pause clears the
    # pre-pick evidence, so the resumed leaf re-retrieves in the
    # narrowed scope rather than reusing what the choice discarded.
    assert SCRIPTED_DOCUMENT_ID in reads[2]
    assert SCRIPTED_ALTERNATE_DOCUMENT_ID not in reads[2]


def test_every_read_uses_its_own_call_id() -> None:
    """Distinct tool-call ids, or the reads collapse into one hop.

    The tool node matches each ``ToolMessage`` to its own call; two calls
    sharing an id leave one read unmatched, and the evidence that backs a
    candidate would never arrive.
    """
    transcript = build_scripted_transcript(
        document_id=SCRIPTED_DOCUMENT_ID,
        version="v" * 8,
        unit=1,
        unit_text="A" * 400,
        scenario=SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
        alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
        alternate_version="w" * 8,
        alternate_unit=1,
        alternate_unit_text="B" * 400,
    )
    ids = [
        call.get("id")
        for step in transcript
        for chunk in step
        for call in chunk.tool_call_chunks or ()
        if call.get("name") == "read_source"
    ]
    assert len(ids) == len(set(ids)) == 3


def test_the_candidate_request_splits_the_terminator_across_deltas() -> None:
    """The terminator arrives in pieces, not as one whole delta.

    The transport withholds any answer suffix that could still grow into
    the terminator, so a whole-terminator single delta would leave that
    hold-back — the mechanism the production path depends on — untested by
    a lane whose only job is to drive the real surfaces.
    """
    transcript = build_scripted_transcript(
        document_id=SCRIPTED_DOCUMENT_ID,
        version="v" * 8,
        unit=1,
        unit_text="A" * 400,
        scenario=SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
        alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
        alternate_version="w" * 8,
        alternate_unit=1,
        alternate_unit_text="B" * 400,
    )
    request_steps = [
        step
        for step, text in zip(transcript, _transcript_texts(transcript))
        if CANDIDATE_TERMINATOR in text
    ]
    assert len(request_steps) == 1
    request = request_steps[0]
    assert len(request) > 1, "the request must span several deltas"
    # No single delta completes the marker, so no delta could release it.
    assert all(
        CANDIDATE_TERMINATOR not in chunk.content for chunk in request
    )


def test_the_request_names_both_filings() -> None:
    """The payload the parser reads names exactly the two candidates."""
    transcript = build_scripted_transcript(
        document_id=SCRIPTED_DOCUMENT_ID,
        version="v" * 8,
        unit=1,
        unit_text="A" * 400,
        scenario=SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
        alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
        alternate_version="w" * 8,
        alternate_unit=1,
        alternate_unit_text="B" * 400,
    )
    text = next(
        text
        for text in _transcript_texts(transcript)
        if CANDIDATE_TERMINATOR in text
    )
    payload = text[text.index(CANDIDATE_TERMINATOR) + len(CANDIDATE_TERMINATOR) :]
    import json

    parsed = json.loads(payload)
    assert [row["document_id"] for row in parsed["candidates"]] == [
        SCRIPTED_DOCUMENT_ID,
        SCRIPTED_ALTERNATE_DOCUMENT_ID,
    ]


def test_the_answer_quotes_only_the_picked_filing() -> None:
    """The resumed answer names one filing.

    Confinement has to be observable as TEXT, not only as a scope field:
    an answer that leaked the unpicked page would read differently on the
    wire, which is what makes the browser assertion meaningful.
    """
    transcript = build_scripted_transcript(
        document_id=SCRIPTED_DOCUMENT_ID,
        version="v" * 8,
        unit=1,
        unit_text="A" * 400,
        scenario=SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
        alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
        alternate_version="w" * 8,
        alternate_unit=1,
        alternate_unit_text="B" * 400,
    )
    answer = _transcript_texts(transcript)[-1]
    assert SCRIPTED_DOCUMENT_ID in answer
    assert SCRIPTED_ALTERNATE_DOCUMENT_ID not in answer
    assert CANDIDATE_TERMINATOR not in answer


def test_scripted_answer_quotes_relevant_source_text_without_cover_terms() -> None:
    """Keep the ID leak adversarial while grounding a relevant table row.

    The source excerpt is synthetic and explicitly marked: its first line
    models irrelevant filing-cover boilerplate, while the selected row is
    the source-backed net-sales fact the question asks about.
    """
    transcript = build_scripted_transcript(
        document_id=SCRIPTED_DOCUMENT_ID,
        version="v" * 8,
        unit=4,
        unit_text=(
            "Synthetic cover boilerplate: FORM 10-Q.\n"
            "Total net sales 82,959 81,434 304,182 282,457\n"
        ),
        scenario=SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
        alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
        alternate_version="w" * 8,
        alternate_unit=4,
        alternate_unit_text="Synthetic alternate revenue table row." * 4,
    )

    answer = _transcript_texts(transcript)[-1]
    assert f"Per {SCRIPTED_DOCUMENT_ID} page 4:" in answer
    assert "Total net sales 82,959 81,434 304,182 282,457" in answer
    assert "FORM 10-Q" not in answer
    assert SCRIPTED_ALTERNATE_DOCUMENT_ID not in answer


# ----- the compiled graph: the pause, the candidates, the resume -----


def _config(thread_id: str) -> dict[str, Any]:
    """The run config the SEND DOOR builds, reproduced.

    The accepted input rides run config and the committed envelope rides
    the run input state, exactly as ``send_message`` does, so the pause is
    reached through the real admission door rather than a stand-in.
    """
    envelope, accepted = accept_input(
        TaskRuntimeState(),
        kind="message",
        payload=QUESTION,
        provenance="workspace_send",
        run_id=RUN_ID,
    )
    return {
        "configurable": {
            "thread_id": thread_id,
            "run_id": RUN_ID,
            "accepted_task_input": accepted.to_state_dict(),
        }
    }


def _input_state() -> dict[str, Any]:
    envelope, _accepted = accept_input(
        TaskRuntimeState(),
        kind="message",
        payload=QUESTION,
        provenance="workspace_send",
        run_id=RUN_ID,
    )
    return {**reset_input_state(QUESTION), "task_runtime": envelope.to_state_dict()}


def _interrupt_payload(out: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = out.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


def _open(thread_id: str) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Open the scenario to its paused document choice."""
    graph = compile_for_scripted_e2e(env=CHOICE_ENV)
    out = graph.invoke(_input_state(), config=_config(thread_id))
    payload = _interrupt_payload(out)
    assert payload is not None, "the scenario must pause, or the card never renders"
    return graph, out, payload


def test_the_scenario_pauses_on_a_document_choice_interrupt() -> None:
    """The lane's transcript really suspends on the NEW pinned kind.

    A scripted scenario that ran to a terminal answer would leave the
    choice card with nothing to paint and the browser check asserting
    nothing, so the pause is the first thing pinned.
    """
    _graph, out, payload = _open("dc-open")
    assert payload["kind"] == DOCUMENT_CHOICE_INTERRUPT_KIND
    assert payload["purpose"] == PURPOSE_DOCUMENT_CHOICE
    # The interrupt references the committed question; it carries no
    # second candidate list, so the projection stays the only authority.
    assert "choice_details" not in payload
    assert out["status"] == "running"
    assert payload["origin_run_id"] == RUN_ID


def test_the_paused_question_offers_two_candidates_and_one_other() -> None:
    """Two hit-backed rows plus exactly one trailing rephrase.

    GE-4's ``≤4 hit-backed candidates have title/section/summary and
    trailing rephrase`` — the count, the one Other, and the per-row
    presentation fields, read off the committed projection the card
    renders from.
    """
    _graph, out, payload = _open("dc-candidates")
    question = TaskRuntimeState.from_state_dict(out["task_runtime"]).pending_question
    assert question is not None
    assert question.purpose == PURPOSE_DOCUMENT_CHOICE
    assert question.origin_run_id == RUN_ID
    details = question.choice_details
    assert 2 <= len(details) <= 4
    ids = [row.choice_id for row in details]
    assert ids == list(question.choices)
    assert question.offered_choices == (*ids, OTHER_CHOICE_ID)
    assert payload["choices"] == [*ids, OTHER_CHOICE_ID]
    assert payload["other_label"] == "换个问法"
    for row in details:
        # The presentation fields GE-4 names are minted from the ADMITTED
        # HIT, so both rows carry a real title and a real summary — the
        # model's own wording is never shown.
        assert row.label
        assert row.summary
        assert row.source_ref.source_kind == "corpus"
        assert row.source_ref.source_id in {
            SCRIPTED_DOCUMENT_ID,
            SCRIPTED_ALTERNATE_DOCUMENT_ID,
        }
        assert row.source_ref.version
        assert row.source_ref.section == "Item 1. Financial Statements"
        assert row.source_ref.source_id not in row.label

    expected_summaries = {
        SCRIPTED_DOCUMENT_ID: (
            "资料中列出了“Products $ 63,355 $ 63,948 "
            "$ 245,241 $ 232,309”。"
        ),
        SCRIPTED_ALTERNATE_DOCUMENT_ID: (
            "资料中列出了“Products $ 60,584 $ 63,355 "
            "$ 230,901 $ 245,241”。"
        ),
    }
    assert {
        row.source_ref.source_id: row.summary for row in details
    } == expected_summaries
    assert {
        row.source_ref.source_id: row.label for row in details
    } == {
        SCRIPTED_DOCUMENT_ID: "Apple Inc. · 2022 Q3",
        SCRIPTED_ALTERNATE_DOCUMENT_ID: (
            "Apple Inc. · 2023 Q3"
        ),
    }


def test_both_candidates_are_backed_by_a_page_the_run_read() -> None:
    """Neither row is a requested-but-unretrieved source.

    Excludes the possibility that the second row exists only because the
    transcript NAMED it: the evidence set at the pause must actually
    carry a hit per candidate, which is what admission resolved against.
    """
    _graph, out, _payload = _open("dc-hit-backed")
    read_document_ids = {
        item.locator.document_id for item in out.get("evidence") or []
    }
    assert {
        SCRIPTED_DOCUMENT_ID,
        SCRIPTED_ALTERNATE_DOCUMENT_ID,
    } <= read_document_ids
    question = TaskRuntimeState.from_state_dict(out["task_runtime"]).pending_question
    assert question is not None
    for row in question.choice_details:
        assert row.source_ref.source_id in read_document_ids


def test_resuming_with_a_pick_answers_inside_the_picked_filing() -> None:
    """The resumed turn quotes and cites the picked source only.

    This is the answer half of the browser check, asserted at host level:
    the same ``Command(resume=pick)`` the resume route issues.
    """
    graph, out, _payload = _open("dc-resume")
    question = TaskRuntimeState.from_state_dict(out["task_runtime"]).pending_question
    assert question is not None
    picked = next(
        row.source_ref.source_id
        for row in question.choice_details
        if row.source_ref.source_id == SCRIPTED_DOCUMENT_ID
    )
    choice_id = next(
        row.choice_id
        for row in question.choice_details
        if row.source_ref.source_id == picked
    )

    done = graph.invoke(
        Command(resume=choice_id),
        config={"configurable": {"thread_id": "dc-resume", "run_id": RUN_ID}},
    )

    assert done["status"] == "done"
    assert done["answer"].strip()
    assert SCRIPTED_DOCUMENT_ID in done["answer"]
    assert SCRIPTED_ALTERNATE_DOCUMENT_ID not in done["answer"]
    assert "Total net sales 82,959 81,434 304,182 282,457" in done["answer"]
    assert "10-Q" not in done["answer"]
    # The citation names the picked document, and ONLY it — the narrowing
    # is observable on the admitted output, not just on a scope field.
    assert [row.document_id for row in done["citations"]] == [
        SCRIPTED_DOCUMENT_ID
    ]
    # The accepted pick commits the checked scope proposal.
    scope = done["source_scope"]
    assert scope["mode"] == "restricted"
    assert [row["source_id"] for row in scope["sources"]] == [
        SCRIPTED_DOCUMENT_ID
    ]
    # The question is settled and no second candidate leaked into scope.
    settled = TaskRuntimeState.from_state_dict(done["task_runtime"])
    assert settled.pending_question is None


def test_the_pause_and_the_resume_share_one_run_id() -> None:
    """GE-4's "preserves the original run/receipt", pinned where it lives.

    The run id is minted once at the send door and rides the checkpoint;
    the resume addresses the same checkpoint. If a reply could mint a
    fresh run, the receipt the operator kept would name a run that never
    produced their answer.
    """
    graph, out, payload = _open("dc-same-run")
    assert payload["origin_run_id"] == RUN_ID
    assert graph.checkpoint_run_id("dc-same-run") == RUN_ID

    question = TaskRuntimeState.from_state_dict(out["task_runtime"]).pending_question
    assert question is not None
    done = graph.invoke(
        Command(resume=question.choice_details[0].choice_id),
        config={"configurable": {"thread_id": "dc-same-run", "run_id": RUN_ID}},
    )
    assert graph.checkpoint_run_id("dc-same-run") == RUN_ID
    accepted = done["task_runtime"]["input_history"][-1]
    assert accepted["kind"] == "reply"
    assert accepted["run_id"] == RUN_ID


def test_a_fresh_thread_replays_the_same_pause() -> None:
    """A server compiles once and answers every send from one model.

    Without a deterministic replay, send 2 of this lane would differ from
    send 1 and a green browser check would describe a run that cannot
    repeat.
    """
    _first, first_out, first_payload = _open("dc-repeat-a")
    _second, second_out, second_payload = _open("dc-repeat-b")
    assert first_payload["choices"] == second_payload["choices"]
    assert first_payload["prompt"] == second_payload["prompt"]

    def _question(out: dict[str, Any]) -> Any:
        return TaskRuntimeState.from_state_dict(out["task_runtime"]).pending_question

    first_q = _question(first_out)
    second_q = _question(second_out)
    assert first_q is not None and second_q is not None
    assert [row.label for row in first_q.choice_details] == [
        row.label for row in second_q.choice_details
    ]
    assert [row.summary for row in first_q.choice_details] == [
        row.summary for row in second_q.choice_details
    ]


# ----- hermeticity: the lane must need no credentials -----


def test_the_scenario_still_builds_no_provider_client(monkeypatch) -> None:
    """Credential-free by construction, on the new transcript too.

    A scenario that reached for a provider would make the browser lane
    depend on a key — the property the whole scripted lane exists to hold.
    """
    import fin.host.compile as compile_module

    def explode(**_kwargs: Any) -> Any:
        raise AssertionError("scripted boot must not build a provider client")

    monkeypatch.setattr(compile_module, "_build_default_model", explode)
    monkeypatch.setenv("FIN_API_KEY", "sk-should-never-be-read")

    graph, out, payload = _open("dc-no-provider")
    assert payload["kind"] == DOCUMENT_CHOICE_INTERRUPT_KIND


def test_a_corpus_without_the_candidate_filing_refuses_to_boot(
    tmp_path: Any,
) -> None:
    """The second filing is a REQUIRED fixture, typed at boot.

    Silently serving one candidate would produce a lane whose card renders
    a single row and whose pick assertion is vacuous, so the absence is a
    :class:`ScriptedBootFixtureError` instead.
    """
    with pytest.raises(ScriptedBootFixtureError) as exc:
        compile_for_scripted_e2e(
            env=CHOICE_ENV,
            manifest_path=tmp_path / "manifest.json",
            extracted_root=tmp_path / "extracted",
        )
    # The extracted tree itself is missing, which is the same refusal:
    # a lane cannot supply a candidate it cannot read.
    assert "extracted tree" in str(exc.value)


# The graph a host test builds directly, pinned so the scenario tests keep
# a real checkpointer rather than a plain invoke.
def test_the_pause_leaves_the_checkpoint_pending() -> None:
    """The run is genuinely resumable, not merely reported as paused."""
    graph, _out, _payload = _open("dc-pending")
    assert graph.pending_nodes("dc-pending")
    assert (
        graph.interrupt_pending("dc-pending") or {}
    )["kind"] == DOCUMENT_CHOICE_INTERRUPT_KIND


def test_compile_is_still_on_the_shared_checkpointer_shape() -> None:
    """The boot path is the SAME ``compile_graph`` the product serves."""
    graph = compile_for_scripted_e2e(env=CHOICE_ENV)
    assert hasattr(graph, "checkpoint_run_id")
    assert hasattr(graph, "interrupt_pending")
    assert isinstance(graph._graph.checkpointer, InMemorySaver)
