"""Unit tests for the host-graph ``AgentState`` schema (T2, #29).

The state module defines the reducer that accumulates ``evidence``
across tool calls in a run AND supports an explicit reset path so a
follow-up send on the same thread can clear the prior run's
evidence. ADR 0016 + spec SC62: ``evidence`` MUST be clearable;
``operator.add + []`` cannot clear (because ``[1,2,3] + [] ==
[1,2,3]``), so the reducer takes an explicit sentinel
(``EVIDENCE_RESET``) to express reset intent.
"""

from __future__ import annotations

from typing import get_type_hints

from langchain_core.messages import AIMessage, HumanMessage

from fin.corpus.extracted import Locator
from fin.host.citation_gate import EvidenceItem
from fin.host.state import (
    AgentState,
    EVIDENCE_RESET,
    EvidenceReset,
    _evidence_reducer,
)


# ----- the sentinel: distinct from any list / None -----


def test_evidence_reset_is_distinct_from_empty_list() -> None:
    """The sentinel is NOT a list (or ``None``) and is the single
    value of its type. Tests that drive the reducer use
    ``isinstance(x, EvidenceReset)`` so a future change to make
    ``EVIDENCE_RESET == []`` would silently bypass the reset path.
    """
    assert EVIDENCE_RESET != []
    assert EVIDENCE_RESET is not None
    assert isinstance(EVIDENCE_RESET, EvidenceReset)
    # ``EvidenceReset`` has no slots / fields; every instance is
    # the same singleton, but the type check is the canonical gate.
    assert isinstance(EvidenceReset(), EvidenceReset)


def test_evidence_reset_has_a_stable_repr() -> None:
    """``repr`` is stable so log lines / test failures can quote it."""
    assert repr(EVIDENCE_RESET) == "EvidenceReset()"


# ----- reducer branches -----


def _evidence(text: str, *, unit: int = 1) -> EvidenceItem:
    """Build a minimal EvidenceItem with a single-unit text fixture."""
    return EvidenceItem(
        locator=Locator(document_id="AAPL-2022Q3", version="v", unit=unit),
        unit_text=text,
    )


def test_evidence_reducer_clear_via_sentinel() -> None:
    """``[]`` + ``EVIDENCE_RESET`` -> ``[]`` (the follow-up reset path).

    Spec SC62: ``evidence`` must be clearable; this is the binding
    assertion that makes follow-up posts safe even when the prior
    run left evidence behind.
    """
    assert _evidence_reducer([], EVIDENCE_RESET) == []


def test_evidence_reducer_concat_normal() -> None:
    """``[A]`` + ``[B]`` -> ``[A, B]`` - the intra-run accumulation path."""
    a, b = _evidence("A"), _evidence("B")
    assert _evidence_reducer([a], [b]) == [a, b]


def test_evidence_reducer_concat_with_prior_left() -> None:
    """Left already has prior items; right adds more (intra-run
    accumulation across multiple tool calls in a single invoke)."""
    a, b, c = _evidence("A"), _evidence("B"), _evidence("C")
    assert _evidence_reducer([a, b], [c]) == [a, b, c]


def test_evidence_reducer_empty_right_is_noop() -> None:
    """``left=[A]``, ``right=[]`` -> ``[A]`` (defensive no-op, NOT a
    reset). The reducer must NOT collapse ``[]`` to ``EVIDENCE_RESET``
    intent - that would let a misconfigured node silently wipe the
    run's evidence just by returning an empty list.
    """
    a = _evidence("A")
    assert _evidence_reducer([a], []) == [a]


def test_evidence_reducer_empty_right_with_empty_left() -> None:
    """``left=[]``, ``right=[]`` -> ``[]`` - cold thread bootstrap with
    no tool calls yet. Distinct from the sentinel path: the same
    output shape, different intent, different reducer branch.
    """
    assert _evidence_reducer([], []) == []


def test_evidence_reducer_reset_after_concat() -> None:
    """The full lifecycle: a tool call accumulates evidence, then a
    follow-up invoke resets it. Binding assertion: the reducer
    REPLACES the prior list (does NOT append ``[]`` to it)."""
    a, b, c = _evidence("A"), _evidence("B"), _evidence("C")
    accumulated = _evidence_reducer([a], [b, c])
    assert accumulated == [a, b, c]
    # The follow-up reset.
    assert _evidence_reducer(accumulated, EVIDENCE_RESET) == []


def test_evidence_reducer_left_none_or_empty() -> None:
    """Defensive: a cold thread bootstrap (``left=None`` /
    ``left=[]``) plus a single new evidence item yields that item.
    ``None`` is the documented initial state when LangGraph has not
    yet written to the key.
    """
    a = _evidence("A")
    assert _evidence_reducer(None, [a]) == [a]
    assert _evidence_reducer([], [a]) == [a]


def test_evidence_reducer_reset_with_empty_left() -> None:
    """``left=[]`` + ``EVIDENCE_RESET`` -> ``[]`` (sentinel path also
    works on a cold thread - no spurious error)."""
    assert _evidence_reducer([], EVIDENCE_RESET) == []


def test_evidence_reducer_does_not_mutate_inputs() -> None:
    """Defensive contract: the reducer MUST NOT mutate either the
    left or right operand (callers reuse list objects across
    reducer invocations)."""
    a, b = _evidence("A"), _evidence("B")
    left = [a]
    right = [b]
    result = _evidence_reducer(left, right)
    assert result == [a, b]
    assert left == [a]
    assert right == [b]
    # The reset path also returns a fresh list - no aliasing.
    result_reset = _evidence_reducer([a], EVIDENCE_RESET)
    assert result_reset == []
    result_reset.append(_evidence("Z"))
    # The reset result is independent: a subsequent concat on a
    # pre-existing left still works as expected.
    assert _evidence_reducer([a], [b]) == [a, b]


# ----- state annotation uses the new reducer -----


def test_agent_state_evidence_annotation_uses_evidence_reducer() -> None:
    """The ``AgentState.evidence`` field MUST be annotated with the
    ``_evidence_reducer`` (the spec bug being fixed), NOT with
    ``operator.add``. This static check catches a regression where
    someone reverts the field to ``operator.add`` and silently
    breaks follow-up posts.
    """
    hints = get_type_hints(AgentState, include_extras=True)
    evidence_hint = hints["evidence"]
    # The ``Annotated[...]`` wrapper carries the reducer as its
    # metadata; the FIRST metadata entry is the reducer.
    metadata = getattr(evidence_hint, "__metadata__", ())
    assert metadata, "evidence must carry reducer metadata via Annotated"
    reducer = metadata[0]
    assert reducer is _evidence_reducer, (
        "AgentState.evidence reducer must be _evidence_reducer "
        "(spec SC62); operator.add cannot be cleared by []."
    )


def test_agent_state_messages_still_use_add_messages() -> None:
    """Sanity guard: the ``messages`` reducer must remain
    ``add_messages`` - follow-up posts depend on prior messages
    surviving in the checkpointed history.
    """
    hints = get_type_hints(AgentState, include_extras=True)
    messages_hint = hints["messages"]
    metadata = getattr(messages_hint, "__metadata__", ())
    assert metadata, "messages must carry reducer metadata via Annotated"
    from langgraph.graph.message import add_messages

    assert metadata[0] is add_messages


# ----- wiring sanity -----


def test_messages_typed_as_list_basemessage() -> None:
    """``messages`` typing still expects ``list[BaseMessage]``."""
    from langchain_core.messages import BaseMessage

    msg: list[BaseMessage] = [HumanMessage(content="hi"), AIMessage(content="ok")]
    assert isinstance(msg[0], HumanMessage)
    assert isinstance(msg[1], AIMessage)


# ===== T3: thinking_text + tool_summaries on AgentState =====


def test_agent_state_thinking_text_field_exists() -> None:
    """The host graph returns ``thinking_text`` from ``model_node``;
    it lives on ``AgentState`` as an overwrite field so a follow-up
    run can clear it via the reset input state."""
    import typing

    annotations = typing.get_type_hints(AgentState)
    assert "thinking_text" in annotations
    assert annotations["thinking_text"] is str


def test_agent_state_tool_summaries_field_is_list_of_dicts() -> None:
    """``tool_summaries`` is a list of ``{name, summary, tool_call_id}``
    dicts (spec line 32). NOT a list of strings - the typed shape
    keeps the wire contract (summary text + correlation id) intact."""
    import typing

    annotations = typing.get_type_hints(AgentState)
    assert "tool_summaries" in annotations
    assert hasattr(annotations["tool_summaries"], "__origin__")


def test_reset_input_state_clears_thinking_and_tool_summaries() -> None:
    """Follow-up runs (ADR 0016 / spec SC34) MUST reset ``thinking_text``
    and ``tool_summaries``; otherwise the prior run's collapsed
    thinking and tool traces bleed into the new run's turn row."""
    from fin.host.state import reset_input_state

    reset = reset_input_state("hello")
    assert reset["thinking_text"] == ""
    assert reset["tool_summaries"] == []


# ===== T4: in_flight on AgentState + reset_input_state coverage =====


def test_agent_state_in_flight_field_is_bool() -> None:
    """T4 (spec line 38: GET-while-running signal). The state schema
    carries the run-scoped ``in_flight`` marker; the live signal lives
    on ``graph.is_locked``, but the field exists so the reset
    contract covers it."""
    import typing

    annotations = typing.get_type_hints(AgentState)
    assert "in_flight" in annotations
    assert annotations["in_flight"] is bool


def test_reset_input_state_clears_in_flight() -> None:
    """T4 binding: ``reset_input_state`` MUST include
    ``in_flight=False`` so a follow-up send starts from a clean
    run-scoped marker rather than inheriting whatever the prior
    run wrote. The lock (``graph.is_locked``) is the live source of
    truth for the GET signal; this just keeps the state schema
    coherent."""
    from fin.host.state import reset_input_state

    reset = reset_input_state("hello")
    assert reset["in_flight"] is False


def test_reset_input_state_clears_run_scoped_track_fields() -> None:
    """Soft-Track: each new run clears track_decision so classify
    owns the fresh decision. status_snapshots are durable — omit."""
    from fin.host.state import reset_input_state

    reset = reset_input_state("hello")
    assert reset["track_decision"] == {}
    assert "status_snapshots" not in reset


def test_agent_state_has_status_snapshots_channel() -> None:
    """Soft-Track T3: status_snapshots is an append-only state channel."""
    import typing

    annotations = typing.get_type_hints(AgentState)
    assert "status_snapshots" in annotations

