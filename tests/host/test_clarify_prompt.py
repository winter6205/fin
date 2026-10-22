"""T7: the clarify prompt names THIS turn's missing slots.

Spec `specs/vertical-slice-hardening.md` Focus 1 / SC13: the
clarify-track call-scoped prompt injects the current missing slots
(from the turn's track decision or the latest status snapshot)
instead of asking one static question every time.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import compile_graph
from fin.host.agent_instructions import (
    CLARIFY_INSTRUCTIONS_MARKER,
    CLARIFY_SYSTEM_PROMPT,
    DIALOGUE_SYSTEM_PROMPT,
    clarify_prompt,
    messages_for_model,
)
from fin.host.scripted_model import ScriptedChatModel
from jev_fake import FakeJevChoice

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _recording_model() -> tuple[ScriptedChatModel, list[list]]:
    """Scripted model plus the sink holding every model-facing list."""
    seen: list[list] = []

    class Recording(ScriptedChatModel):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    model = Recording(
        script=[AIMessage(content="Which period should I look at?")]
    )
    return model, seen


@pytest.fixture
def recording_model() -> tuple[ScriptedChatModel, list[list]]:
    return _recording_model()


# ----- prompt composition -----


def test_clarify_prompt_without_known_slots_is_the_static_prompt() -> None:
    """No derived gap → the base prompt, unchanged."""
    assert clarify_prompt(None) == CLARIFY_SYSTEM_PROMPT
    assert clarify_prompt([]) == CLARIFY_SYSTEM_PROMPT


def test_clarify_prompt_names_the_single_missing_slot() -> None:
    """A period-only gap asks for the period and not the company."""
    prompt = clarify_prompt(["period"])
    assert prompt != CLARIFY_SYSTEM_PROMPT
    assert prompt.startswith(CLARIFY_SYSTEM_PROMPT)
    assert "period" in prompt.rsplit("\n", 1)[-1]
    assert "company" not in prompt.rsplit("\n", 1)[-1]


def test_clarify_prompt_names_both_missing_slots() -> None:
    injected = clarify_prompt(["company", "period"]).rsplit("\n", 1)[-1]
    assert "company" in injected
    assert "period" in injected


def test_clarify_prompt_ignores_unknown_slot_names() -> None:
    """Unknown slot labels never leak into operator-facing copy."""
    assert clarify_prompt(["sector"]) == CLARIFY_SYSTEM_PROMPT
    assert "sector" not in clarify_prompt(["sector", "period"])


def test_clarify_prompt_dedupes_repeated_slots() -> None:
    injected = clarify_prompt(["period", "period"]).rsplit("\n", 1)[-1]
    assert injected.count("period") == clarify_prompt(["period"]).rsplit(
        "\n", 1
    )[-1].count("period")


def test_messages_for_model_injects_slots_on_the_clarify_track() -> None:
    out = messages_for_model(
        [HumanMessage(content="What was AAPL net sales?")],
        track="clarify",
        missing_slots=["period"],
    )
    assert isinstance(out[0], SystemMessage)
    assert CLARIFY_INSTRUCTIONS_MARKER in str(out[0].content)
    assert out[0].content == clarify_prompt(["period"])


def test_messages_for_model_ignores_slots_off_the_clarify_track() -> None:
    """Slot injection is clarify-only; other tracks keep their prompt."""
    out = messages_for_model(
        [HumanMessage(content="Hi")],
        track="dialogue",
        missing_slots=["period"],
    )
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT


# ----- SC13: the graph's clarify leaf injects the turn's gaps -----


def test_filing_gap_with_company_never_injects_the_slot_prompt(
    recording_model,
):
    """A clarify-labelled filing ask is retrieval-first, never a slot ask.

    The clarify leaf's slot prompt is model-facing internal vocabulary;
    the correction replaces it with corpus-backed scope discovery. With
    no adapter there is no scope to prove, so the gate records
    ``unavailable`` and the turn continues grounded — the slot prompt is
    never composed, even though the company is known.
    """
    model, seen = recording_model
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        jev_client=FakeJevChoice([("clarify", 0.99)]),
    )
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="What was AAPL net sales?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-clarify-slots-period"),
    )
    assert out["track"] == "grounded"
    assert out["track_decision"]["signals"]["clarify_scope"]["outcome"] == (
        "unavailable"
    )
    assert (out.get("task_runtime") or {}).get("pending_question") is None
    assert CLARIFY_INSTRUCTIONS_MARKER not in "".join(
        str(messages[0].content) for messages in seen
    )


def test_filing_gap_without_company_never_injects_the_slot_prompt(
    recording_model,
):
    """The same rule holds when neither slot is named.

    "What was revenue?" used to ask the operator to fill company and
    period through the clarify leaf. It now enters the retrieval-first
    gate and, without a proven scope, continues grounded without ever
    publishing or prompting with the internal slot names.
    """
    model, seen = recording_model
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        jev_client=FakeJevChoice([("clarify", 0.99)]),
    )
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="What was revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-clarify-slots-both"),
    )
    assert out["track"] == "grounded"
    assert out["track_decision"]["signals"]["clarify_scope"]["outcome"] == (
        "unavailable"
    )
    assert (out.get("task_runtime") or {}).get("pending_question") is None
    assert CLARIFY_INSTRUCTIONS_MARKER not in "".join(
        str(messages[0].content) for messages in seen
    )


def test_snapshot_gap_alone_no_longer_opens_a_slot_clarification(
    recording_model,
):
    """QA design correction: a clarify label with no filing ask is decided
    by retrieval, never by a status-snapshot gap.

    The old path read the latest snapshot, found ``period`` empty, and put
    ``请补充：期间`` in front of the operator even though the utterance was
    not about a period. No accepted input/task exists in this direct
    invocation, so the retrieval-first gate cannot bind a question and
    continues on the grounded lane; what matters here is that the
    snapshot gap no longer selects the clarify leaf or its slot prompt.
    """

    model, seen = recording_model
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=FakeJevChoice([("clarify", 0.9)]),
        known_document_ids=KNOWN_IDS,
    )
    out = graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "I am honestly not sure how to word my question, "
                        "can you help me out here?"
                    )
                )
            ],
            "status_snapshots": [
                {
                    "company": "AMZN",
                    "period": "",
                    "document_ids": ["AMZN-2023Q1"],
                }
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config("t-clarify-slots-snapshot"),
    )
    assert out["track"] == "grounded"
    assert out["clarify_scope_note"] == ""
    assert (out.get("task_runtime") or {}).get("pending_question") is None
    assert seen
    assert CLARIFY_INSTRUCTIONS_MARKER not in str(seen[0][0].content)


def test_clarify_prompt_stays_out_of_the_checkpoint(recording_model):
    """Injection is call-scoped: the slot-bearing SystemMessage is
    never written back to ``messages``."""
    model, _seen = recording_model
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    thread_id = "t-clarify-slots-call-scoped"
    graph.invoke(
        {
            "messages": [HumanMessage(content="What was revenue?")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config(thread_id),
    )
    snapshot = graph._graph.get_state(_thread_config(thread_id))
    checkpointed = list((snapshot.values or {}).get("messages") or [])
    assert not any(
        isinstance(m, SystemMessage)
        and CLARIFY_INSTRUCTIONS_MARKER in str(m.content)
        for m in checkpointed
    )
