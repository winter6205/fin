"""State schema and constants for the v1 host graph (T6).

This module defines ``AgentState`` — the ``TypedDict`` the
``StateGraph`` is built around — plus the run-status enum and the
two fuses that bound v1 runs:

* ``STEP_CAP`` — the agent-loop step cap, 32 (ADR 0008). Counts in
  fin state, not in LangGraph ``recursion_limit``.
* ``REPAIR_CAP`` — the citation-repair cap, 3 (ADR 0008). Lives in
  fin state too; it is independent of the step cap.

``AgentState`` is annotated so LangGraph merges ``messages`` via
``add_messages`` (preserves prior chat) and accumulates ``evidence``
via ``_evidence_reducer`` (concat within a run, explicit reset
across runs). Every other key is overwritten with the last value
the node returned. Nodes that mutate ``evidence`` return ONLY the
new items (or the ``EVIDENCE_RESET`` sentinel to clear); nodes
that do not mutate ``evidence`` omit it from their return.

The status field is a closed enum: ``"running"``, ``"done"``,
``"refuse"``, ``"step_cap"``. The graph factory raises if any node
returns a value outside this set.

Concurrency: the graph factory enforces "at most one active run per
thread_id" via a per-thread lock (see ``fin.host.graph``); that
mechanism is NOT part of ``AgentState`` because the lock lives
outside the LangGraph state machine.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

from fin.host.citation_gate import Citation, EvidenceItem


# ----- evidence reducer + reset sentinel (ADR 0016 / spec SC62) -----


@dataclass(frozen=True)
class EvidenceReset:
    """Sentinel value: replace ``AgentState["evidence"]`` with ``[]``.

    Threads accumulate ``messages`` via ``add_messages`` but each run
    must reset run-scoped ``evidence`` (ADR 0016, spec SC62). The
    previous ``operator.add`` reducer could not be cleared by passing
    ``[]`` because ``operator.add`` is identity-on-empty-list; the
    explicit sentinel is the only sound way to express reset intent.

    Use as::

        input_state = {
            "messages": [HumanMessage(content=question)],
            "evidence": EVIDENCE_RESET,
            "repair_count": 0,
            "step_count": 0,
        }

    The single canonical instance lives at ``EVIDENCE_RESET``; tests
    branch on ``isinstance(value, EvidenceReset)`` so a duplicate
    literal in another module would not accidentally route through
    the reset path.

    Why a frozen dataclass (NOT a plain ``__slots__ = ()`` class):
    LangGraph's checkpointer serializes channel writes via ormsgpack.
    The serializer handles arbitrary classes through a hand-curated
    dispatch table (UUID, Path, Enum, dataclass, ...). A frozen
    dataclass with NO fields routes through the dataclass branch
    (``EXT_CONSTRUCTOR_KW_ARGS``) and round-trips with no custom
    hook, no extra wire bytes, and no ops on the prod durable-graph
    path (PostgresSaver). A ``__slots__ = ()`` shape was tried first
    and crashed at ``dumps_typed`` with ``TypeError: Type is not
    msgpack serializable: EvidenceReset``. Empty-field dataclasses
    keep the stable ``EvidenceReset()`` repr and the same single
    singleton semantics.
    """


EVIDENCE_RESET: Final[EvidenceReset] = EvidenceReset()


def _evidence_reducer(
    left: list[EvidenceItem],
    right: list[EvidenceItem] | EvidenceReset,
) -> list[EvidenceItem]:
    """Reducer for ``AgentState["evidence"]``.

    - ``right`` is an ``EvidenceReset`` instance -> return ``[]``
      (the explicit reset path; ADR 0016 / spec SC62).
    - ``right`` is an empty list -> return ``left`` unchanged
      (defensive no-op; matches the prior behavior when a node
      returns ``{"evidence": []}`` because it produced nothing
      this turn but does not wish to wipe the run).
    - ``right`` is a non-empty list -> concat ``left + right``
      (intra-run accumulation across tool calls).

    The reducer MUST NOT mutate either operand (LangGraph may reuse
    list objects across reducer invocations within a single invoke).
    A cold-thread bootstrap (LangGraph's initial ``left`` is
    ``None``) is normalized to ``list(...)`` so the ``+`` path
    works without a ``TypeError``.
    """
    if isinstance(right, EvidenceReset):
        return []
    if not right:
        return list(left or [])
    return list(left or []) + list(right)


# ----- closed run-status enum -----

# Allowed values of ``AgentState["status"]``. The graph factory and the
# citation gate treat anything outside this set as a contract
# violation. ``"running"`` is the only non-terminal value; the other
# three end the run.
Status = Literal["running", "done", "refuse", "step_cap"]

# Status values that cause the graph to END. The conditional edges
# consult this set; nodes mutate ``state["status"]`` directly so the
# decision is data, not control flow.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"done", "refuse", "step_cap"})


# ----- the two fuses (ADR 0008) -----

# Agent-loop step cap. Counts model_node entries; 32 is the v1 fuse
# that stops runaway loops. NOT the LangGraph ``recursion_limit``.
# LangGraph's recursion_limit is set higher (see ``RECURSION_LIMIT``)
# so this fuse fires first.
STEP_CAP: Final[int] = 32

# Citation-repair cap. After ``REPAIR_CAP`` consecutive bounces, the
# citation gate refuses with reason ``"citation_repair_cap"`` instead
# of bouncing again. Independent of the step cap.
REPAIR_CAP: Final[int] = 3

# Minimum extractive span length the citation gate matches on
# (ADR 0021). Surfaced here so the graph factory can pass it through
# consistently; ``verify_citations`` accepts it as a kwarg too.
MIN_SPAN_LEN: Final[int] = 32


# ----- LangGraph recursion_limit -----

# LangGraph's recursion limit counts SUPERSTEPS (one node execution
# per superstep), while ``STEP_CAP`` counts ``model_node`` entries.
# One agent-loop iteration that calls a tool costs TWO supersteps
# (model_node, then tool_node); the citation gate adds one more on
# the final turn. So the worst case before the fin fuse fires is
# ~``2 * STEP_CAP`` supersteps. The default must therefore exceed
# ``2 * STEP_CAP`` — a bare ``STEP_CAP + margin`` would let
# ``GraphRecursionError`` preempt the fin-owned ``step_cap`` fuse,
# which the spec forbids (success criteria item 5: "LangGraph
# ``recursion_limit`` is > 32 so this fuse fires first" — fires
# first means AFTER the fin fuse, never instead of it).
RECURSION_LIMIT: Final[int] = 2 * STEP_CAP + 6  # = 70


# ----- AgentState -----

# ``total=False`` so the state is tolerant of fresh threads that
# haven't been written to yet. LangGraph's last-write-wins reducer
# applies to every field except ``messages``, which uses
# ``add_messages`` so prior chat survives across model_node cycles.
#
# ``answer`` / ``citations`` are populated only on ``status ==
# "done"``. ``status_reason`` carries the gate's refuse reason
# (``"unknown_locator"`` or ``"citation_repair_cap"``) or
# ``"step_cap"``.
class AgentState(TypedDict, total=False):
    """The state the host graph (T6) writes to.

    Keys
    ----
    messages
        Conversation history. ``add_messages`` reducer; prior turns
        survive across model_node cycles.
    evidence
        Cumulative list of ``EvidenceItem`` objects produced by this
        run's tool calls (``read_file`` page texts and ``retrieve``
        chunk texts). Reduced by ``_evidence_reducer``: nodes return
        ONLY the new items they produced this turn; passing the
        ``EVIDENCE_RESET`` sentinel clears the list (the only way
        to express reset intent, since ``operator.add + []`` is a
        no-op). A node that does not produce evidence and does not
        wish to clear the list must omit this key from its return.
    repair_count
        Number of consecutive citation-repair bounces already
        accumulated on this run. Reset to 0 on a fresh invoke.
    step_count
        Number of times ``model_node`` has been entered. Reset to 0
        on a fresh invoke.
    answer
        The model's final prose answer. Populated only on
        ``status == "done"``.
    citations
        Code-filled ``Citation`` tuple from the citation gate.
        Populated only on ``status == "done"``.
    status
        One of ``"running"``, ``"done"``, ``"refuse"``,
        ``"step_cap"``. The graph reads this to decide END vs.
        continue.
    status_reason
        Free-text reason. Set on ``"refuse"`` and ``"step_cap"``.
    thinking_text
        T3: the full thinking prose streamed by this run's LAST
        ``model_node`` entry (from ``additional_kwargs.reasoning_details``
        or inline ``<thinking>`` blocks). Overwrite semantics -
        each model call replaces the prior value; the workspace
        surface persists the final value on the turn row.
    tool_summaries
        T3: human-readable one-liners for each tool call observed
        this run, appended by ``tool_node``. Each entry is
        ``{name, summary, tool_call_id}``; the SPA renders
        ``summary`` and correlates by ``tool_call_id``. Raw tool
        JSON never reaches the wire (spec line 32).
    in_flight
        T4 (spec line 38: ``running`` signal while a run is in
        flight). Per-send run-scoped marker carried on the state
        schema so the reset contract covers it. The LIVE source of
        truth for GET-while-running is the per-thread lock
        (``graph.is_locked(thread_id)``); this field is state-schema
        bookkeeping so a follow-up send starts from a clean
        ``False`` rather than inheriting whatever the prior run
        wrote. Overwrite semantics; defaults ``False``.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    # Custom reducer (NOT ``operator.add``): nodes return ONLY the
    # new items they produced this turn; LangGraph concatenates
    # them onto the prior list. This makes cross-period /
    # multi-tool-call runs accumulate evidence instead of
    # overwriting on every tool invocation. To CLEAR the list
    # (e.g. on a follow-up run, ADR 0016 + spec SC62), a node
    # returns the ``EVIDENCE_RESET`` sentinel — ``operator.add + []``
    # would be a no-op (``[1,2,3] + [] == [1,2,3]``). A node that
    # does not produce evidence and does not wish to clear the list
    # must omit this key from its return.
    evidence: Annotated[list[EvidenceItem], _evidence_reducer]
    repair_count: int
    step_count: int
    answer: str
    citations: tuple[Citation, ...]
    status: Status
    status_reason: str
    # T3 streaming fields. ``thinking_text`` carries the full
    # streamed thinking prose for THIS run's last model call (the
    # SPA collapses it to a one-line duration label after the
    # run). ``tool_summaries`` carries one ``{name, summary,
    # tool_call_id}`` dict per tool call observed so far in the
    # run; ``tool_node_with_summaries`` returns the
    # ``operator.add`` delta so multi-round agent-loop iterations
    # accumulate across rounds instead of overwriting. Both are
    # reset per-run via ``reset_input_state`` (ADR 0016 / spec
    # line 34).
    thinking_text: str
    tool_summaries: Annotated[list[dict[str, str]], operator.add]
    # T4: run-scoped ``in_flight`` marker (overwrite semantics).
    # See docstring above. Cleared on every fresh send by
    # :func:`reset_input_state`.
    in_flight: bool


def reset_input_state(question: str) -> dict[str, Any]:
    """Build the standard run-scoped reset input for ``graph.invoke``.

    Every invoke resets the eight run-scoped fields (ADR 0016, spec
    line 34 - the six v1 fields plus T3's ``thinking_text`` /
    ``tool_summaries``) while leaving ``messages`` to
    ``add_messages`` accumulation. Centralised so the three call
    sites (``/ask``, ``/api/threads/{id}/messages``, CLI ``run``)
    cannot drift on which fields are reset.

    Returns a dict suitable for ``graph.invoke(state, config=...)``
    that clears ``evidence`` (via the ``EVIDENCE_RESET`` sentinel),
    zero-out ``step_count`` / ``repair_count``, and blanks
    ``answer`` / ``citations`` / ``status``. ``messages`` carries
    only the new ``HumanMessage(question)``; the checkpointer's
    ``add_messages`` reducer merges it with prior conversation.
    """
    return {
        "messages": [HumanMessage(content=question)],
        "evidence": EVIDENCE_RESET,
        "repair_count": 0,
        "step_count": 0,
        "answer": "",
        "citations": [],
        "status": "running",
        "thinking_text": "",
        "tool_summaries": [],
        # T4: every fresh send clears the in-flight marker. The
        # live GET-while-running signal lives on the per-thread
        # lock (graph.is_locked), not on this state field; we
        # reset it for state-schema completeness so a follow-up
        # send starts from a clean ``False`` (T4 acceptance).
        "in_flight": False,
    }


__all__ = [
    "AgentState",
    "EVIDENCE_RESET",
    "EvidenceReset",
    "MIN_SPAN_LEN",
    "REPAIR_CAP",
    "RECURSION_LIMIT",
    "STEP_CAP",
    "Status",
    "TERMINAL_STATUSES",
    "_evidence_reducer",
    "reset_input_state",
]
