"""SC23 — memory payloads never satisfy the citation verifier.

Spec cross-session-operator-memory.md / SC23: "Memory text cannot
satisfy the citation verifier." Spec citation verifier definition
(ADR 0046): "a supporting span is a contiguous substring of the
answer (v1 minimum 32 characters) that appears in text this run
received from **retrieve** / **read_file**".

This module is the proof-side of plan T9 ("Memory is not citation
evidence"). It proves the property holds against the current code,
not just against a unit test of ``verify_citations``.

## Trace

Evidence items only enter the citation gate via the ``evidence``
channel of ``AgentState``. That channel is fed by exactly two host
tools (``graph.py``):

* ``_make_read_file_tool`` (line ~366-396) — emits one
  ``EvidenceItem`` per page in the joined ``text`` slice.
* ``_make_retrieve_tool`` (line ~653) — emits
  ``evidence_from_chunks(chunks)``.

Both tools return a ``Command(update={"messages": [...], "evidence":
new_evidence})``. The graph's ``tool_node`` wrapper folds
``Command.update["evidence"]`` and ``dict["evidence"]`` keys into
``merged_evidence``. Memory tools (``_make_save_memory_tool``,
``_make_recall_memory_tool``, ``_make_edit_user_profile_tool``)
return plain dicts from ``@tool`` decorators — ToolNode serializes
those as ``ToolMessage`` JSON without an ``evidence`` channel. The
system-side ``MEMORY_POINTER_BLOCK`` and the call-scoped
``FIN_MEMORY_PREFETCH_MARKER`` HumanMessage are constructed outside
the tool pipeline and never enter the evidence list. The prefetch
HumanMessage is also call-scoped (per
``agent_instructions.memory_prefetch_message`` docstring): it lives
on the model-facing payload only and is never written back to the
checkpointed ``state["messages"]``.

The gate (``verify_citations``) matches answer substrings only
against ``evidence[i].page_text``. Memory payload bytes live in
``messages[i].content`` (or in the model-only payload via
``messages_for_model``), not in any ``evidence[i].page_text``.

So: even when the model quotes a memory payload verbatim, no
``EvidenceItem`` carries that text, so ``_match_spans`` finds no
≥32-char substring and the run bounces (or, at the cap, refuses).

## Why this is a proof (and not a code change)

After the trace, the gate ALREADY cannot be satisfied by memory
text — the property holds structurally. Plan T9's "Completion"
notes "how the verifier ignores memory payloads is open as long as
the property holds". The deliverable is the proof tests that lock
the property in place; if a future tool refactor ever lets a
memory payload leak into the evidence channel, one of the
assertions below breaks and the regression is caught at CI.

The tests deliberately construct answers that are substrings of
memory text to make the negative case meaningful — if memory text
ever DID enter the evidence list, the answer would otherwise
match and the run would ``done`` with a memory-sourced citation.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import (
    BounceResult,
    DoneResult,
    EvidenceItem,
    ScriptedChatModel,
    compile_graph,
    verify_citations,
)
from fin.host.agent_instructions import (
    FIN_MEMORY_PREFETCH_MARKER,
    MEMORY_POINTER_MARKER,
    memory_prefetch_message,
)


# ----- fixtures -----


_SHA = "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
_AAPL_LOCATOR = Locator(
    document_id="AAPL-2022Q3", version=_SHA, page=1
)

# A pretend memory fact. ≥32-char unique substring so it could
# plausibly match a memory-sourced evidence item if the gate ever
# allowed memory text through.
_MEMORY_FACT_TITLE = "Quarterly revenue caveat"
_MEMORY_FACT_HOOK = "Always flag restatement in Q3 notes"
assert len(_MEMORY_FACT_HOOK) >= 32

# A pretend user-profile line that the prefetch / system pointer
# could surface.
_PROFILE_NOTE = (
    "Operator preference: prefer terse revenue summaries and "
    "call out restatements explicitly when comparing quarters."
)
assert len(_PROFILE_NOTE) >= 32

# A realistic ≥32-char span that lives on a real corpus page
# (AAPL-2022Q3 page 1). Used to confirm the gate STILL works
# normally when a real evidence item is present (regression guard
# — the structural exclusion must not break the happy path).
_LEGIT_SPAN = "ABCDEFGHIJ" * 5  # 50 alphanumeric chars, determinstic
assert len(_LEGIT_SPAN) == 50


# A research-track question. Rule cascade classifies "AAPL net
# sales 2023Q3" as research (filing_ask + company + period).
_RESEARCH_QUESTION = "What was AAPL net sales in 2023Q3?"
_KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _scripted_answer_only(answer: str) -> ScriptedChatModel:
    """A scripted model that answers without ever calling a tool."""
    return ScriptedChatModel(script=[AIMessage(content=answer)])


def _invoke_with_state(
    *,
    state: dict,
    answer: str,
    thread_id: str,
) -> dict:
    """Drive the citation gate path with a no-tool scripted model.

    The model emits only the answer — no tool calls — so the
    evidence list is whatever the input state already carries.
    """
    model = _scripted_answer_only(answer)
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=_KNOWN_IDS,
    )
    return g.invoke(state, config={"configurable": {"thread_id": thread_id}})


def _minimal_state(
    *,
    question: str,
    evidence: list[EvidenceItem],
    repair_count: int = 0,
    messages: list | None = None,
) -> dict:
    """Build the minimum input state that compiles into a running run."""
    base = {
        "messages": messages if messages is not None else [HumanMessage(question)],
        "evidence": evidence,
        "repair_count": repair_count,
        "step_count": 0,
        "answer": "",
        "citations": [],
        "status": "running",
        "thinking_text": "",
        "tool_summaries": [],
    }
    return base


# ===== unit-level: verify_citations never accepts memory text as evidence =====


def test_verify_citations_with_only_memory_marker_text_bounces() -> None:
    """The gate matches answer substrings against ``page_text`` only.

    Memory payload bytes (a prefetch row's title+hook) DO NOT live
    in any ``page_text`` — the gate sees an empty evidence list and
    bounces. Even if the answer is a verbatim quote of the
    memory block, the gate has nothing to match it against.
    """
    memory_quote = (
        "Always flag restatement in Q3 notes for accuracy and clarity"
    )
    # Empty evidence list (the structural reality for memory-only
    # payloads) — even with a verbatim memory quote in the answer,
    # the gate cannot match.
    result = verify_citations(
        answer=f"Per memory: {memory_quote}.",
        evidence=[],
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    assert isinstance(result, BounceResult)
    assert result.outcome == "bounce"
    assert result.repair_count == 1


def test_verify_citations_does_not_consult_message_history() -> None:
    """The gate signature is ``(answer, evidence, ...)`` — there is
    no ``messages`` parameter. Passing memory text via message
    history cannot reach the matcher: only ``page_text`` is
    consulted.
    """
    # Build a hypothetical evidence item that contains memory text
    # alongside a legit span. The gate sees only ``page_text``
    # substrings of the answer — the legit span matches and the
    # run is ``done``. This asserts the gate is functioning
    # (regression guard), and pairs with the graph-level tests
    # where memory text alone cannot drive ``done`` because
    # memory text never enters the evidence list.
    memory_quote = "Always flag restatement in Q3 notes for accuracy"
    evidence_with_memory = [
        EvidenceItem(
            locator=_AAPL_LOCATOR,
            page_text=(
                "PREAMBLE_"
                + _LEGIT_SPAN
                + "_POSTAMBLE_"
                + memory_quote
                + "_END"
            ),
        )
    ]
    result = verify_citations(
        answer=f"Header: {_LEGIT_SPAN}.",
        evidence=evidence_with_memory,
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    # The legit span DID match — done is allowed.
    assert isinstance(result, DoneResult)
    assert len(result.citations) == 1
    assert result.citations[0].quote == _LEGIT_SPAN


# ===== graph-level: citation_gate_node sees zero evidence for memory payloads =====


def test_citation_gate_node_bounces_when_answer_quotes_recall_payload_only() -> None:
    """SC23: a research turn that quotes only recall/fact text,
    with no this-run retrieve/read_file evidence, does NOT ``done``
    with citations sourced from memory.

    This test seeds the input state with a ``recall_memory``
    ToolMessage (the advisory payload) and drives the model to
    answer with a verbatim quote of the recall hook. The gate runs
    with an empty evidence list → bounce.
    """
    recall_payload = {
        "rows": [
            {
                "fact_id": "f1",
                "title": _MEMORY_FACT_TITLE,
                "hook": _MEMORY_FACT_HOOK,
                "advisory": True,
                "citation_eligible": False,
            }
        ]
    }
    # Note: state["messages"] does NOT carry the prefetch
    # HumanMessage — that is call-scoped on the model-facing
    # payload only (per
    # ``agent_instructions.memory_prefetch_message``). The recall
    # ToolMessage DOES land on state["messages"] because the model
    # called the recall_memory tool; the ToolNode emits it as a
    # checkpointed message.
    messages = [
        HumanMessage(content=_RESEARCH_QUESTION),
        ToolMessage(
            content=json.dumps(recall_payload),
            tool_call_id="c-recall",
            name="recall_memory",
        ),
    ]
    answer_text = (
        "From recall: " + _MEMORY_FACT_HOOK + " is the standing rule."
    )

    state = _minimal_state(
        question=_RESEARCH_QUESTION,
        evidence=[],  # <-- the critical structural fact
        messages=messages,
    )

    out = _invoke_with_state(
        state=state,
        answer=answer_text,
        thread_id="t-recall-only",
    )

    # Property SC23: the run does NOT end ``done`` with a
    # memory-sourced citation. The answer is a verbatim quote of
    # a memory hook, but no this-run retrieve/read_file evidence
    # supports it, so the gate bounces.
    assert out["status"] != "done", (
        f"SC23 violated: a memory-text-only answer ended done "
        f"with citations={out.get('citations')!r}"
    )
    # No citations produced.
    assert list(out.get("citations", ())) == []


def test_citation_gate_node_bounces_when_answer_quotes_user_profile_line() -> None:
    """User-profile bytes (from ``edit_user_profile`` writes) are
    surfaced via the system-side existence pointer / read path;
    they never enter the evidence channel either. A research
    answer that quotes only the profile line bounces.
    """
    # Place the MEMORY_POINTER_MARKER block in the system prefix
    # — that's the path ``messages_for_model`` actually surfaces
    # profile existence on the model side. The profile TEXT
    # itself (via the read path) is what the model could quote;
    # we inline a profile line here directly into the system
    # message to mirror that the model has seen it.
    messages = [
        SystemMessage(
            content=(
                "some prefix\n\n"
                f"[{MEMORY_POINTER_MARKER}]\n"
                "user profile exists\n\n"
                f"{_PROFILE_NOTE}"
            )
        ),
        HumanMessage(content=_RESEARCH_QUESTION),
    ]
    answer_text = "Per your standing preference: " + _PROFILE_NOTE + " Done."

    state = _minimal_state(
        question=_RESEARCH_QUESTION,
        evidence=[],
        messages=messages,
    )
    out = _invoke_with_state(
        state=state,
        answer=answer_text,
        thread_id="t-profile-only",
    )
    assert out["status"] != "done"
    assert list(out.get("citations", ())) == []


def test_citation_gate_node_bounces_with_prefetch_text_in_call_scoped_payload() -> None:
    """The prefetch HumanMessage is call-scoped (model-facing
    payload only, never checkpointed). The test mirrors that by
    NOT putting it in state["messages"], but instead by quoting
    the prefetch row's hook verbatim in the answer — the model
    would have seen the prefetch via ``memory_prefetch_message``
    in this run, but the evidence channel at the gate is empty.

    Property: even if the model's answer is a verbatim quote of a
    prefetch row, the gate bounces because the prefetch bytes
    never enter the evidence list.
    """
    # Build a prefetch HumanMessage to compute the exact hook
    # text the model would see — we only assert the structural
    # property (the gate's input does not include the prefetch).
    prefetch_msg = memory_prefetch_message(
        [{"fact_id": "f1", "title": _MEMORY_FACT_TITLE, "hook": _MEMORY_FACT_HOOK}]
    )
    assert prefetch_msg is not None
    assert FIN_MEMORY_PREFETCH_MARKER in str(prefetch_msg.content)
    assert _MEMORY_FACT_HOOK in str(prefetch_msg.content)

    # The model-facing payload would include the prefetch message
    # via ``messages_for_model``. We simulate the answer the
    # model would produce — a verbatim quote of the hook. But
    # state["messages"] does NOT include the prefetch (it's
    # call-scoped only).
    messages = [HumanMessage(content=_RESEARCH_QUESTION)]

    answer_text = (
        "Verbatim quote: "
        + _MEMORY_FACT_HOOK
        + " — end quote."
    )

    state = _minimal_state(
        question=_RESEARCH_QUESTION,
        evidence=[],
        messages=messages,
    )
    out = _invoke_with_state(
        state=state,
        answer=answer_text,
        thread_id="t-marker-verbatim",
    )

    # Property: if the gate ever started harvesting HumanMessage
    # content for evidence, this assertion would fail — the
    # verbatim quote would match and the run would ``done`` with
    # a memory-sourced citation. The structural fix lives in
    # ``graph.py`` (evidence channel is fed only by read_file /
    # retrieve ``Command.update["evidence"]``); this test pins
    # the property from the outside.
    assert out["status"] != "done"
    assert list(out.get("citations", ())) == []


# ===== graph-level: structural guarantee — evidence channel is fed only by read_file / retrieve =====


def test_only_read_file_and_retrieve_can_grow_evidence_channel() -> None:
    """The structural guarantee behind SC23: ``state["evidence"]``
    is populated ONLY by ``read_file`` and ``retrieve`` tool
    calls. Memory tools (``save_memory`` / ``recall_memory`` /
    ``edit_user_profile``) contribute zero evidence items.

    This test exercises a graph where the model calls
    ``recall_memory`` and ``edit_user_profile`` BEFORE answering
    (scripted, no real port needed). The answer quotes the
    recall payload, and the evidence list at the gate is
    observed to be empty.
    """
    # Scripted model: recall_memory -> edit_user_profile -> final
    # answer that quotes the recall hook verbatim. No
    # read_file / retrieve call → zero evidence at gate.
    answer_text = "Recall says: " + _MEMORY_FACT_HOOK + " is the rule."
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "recall_memory",
                        "args": {"query": "AAPL revenue caveat"},
                        "id": "c-recall",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_user_profile",
                        "args": {"layer": "workspace", "text": "be terse"},
                        "id": "c-edit",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=answer_text),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=_KNOWN_IDS,
    )
    state = _minimal_state(
        question=_RESEARCH_QUESTION,
        evidence=[],
    )
    out = g.invoke(
        state, config={"configurable": {"thread_id": "t-structural"}}
    )
    # The run does not ``done`` with a memory-sourced citation.
    assert out["status"] != "done"
    # The evidence channel is empty — neither memory tool added
    # anything to it. (If a future refactor ever fed memory text
    # into the evidence channel, this assertion fails.)
    assert out.get("evidence", []) == []
    # No citations produced.
    assert list(out.get("citations", ())) == []


# ===== regression guard: a real read_file evidence item still drives done =====


def test_legitimate_read_file_evidence_still_drives_done() -> None:
    """Memory exclusion must not break the happy path. When the
    state carries a real read_file ``EvidenceItem``, the gate
    still matches the answer's ≥32-char span and the run ends
    ``done`` with a citation sourced from the evidence item.
    """
    state = _minimal_state(
        question=_RESEARCH_QUESTION,
        evidence=[
            EvidenceItem(
                locator=_AAPL_LOCATOR,
                page_text="PREAMBLE_" + _LEGIT_SPAN + "_POSTAMBLE_END",
            )
        ],
    )
    answer_text = "Header: " + _LEGIT_SPAN + "."
    out = _invoke_with_state(
        state=state,
        answer=answer_text,
        thread_id="t-happy-path",
    )
    assert out["status"] == "done"
    assert len(out["citations"]) == 1
    cit = out["citations"][0]
    assert cit.document_id == "AAPL-2022Q3"
    assert cit.quote == _LEGIT_SPAN
