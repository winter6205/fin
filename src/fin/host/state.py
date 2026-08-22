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
via ``operator.add`` (cumulative across this run's tool calls).
Every other key is overwritten with the last value the node
returned. Nodes that mutate ``evidence`` return ONLY the new items;
nodes that do not mutate ``evidence`` omit it from their return.

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
from typing import Annotated, Final, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from fin.host.citation_gate import Citation, EvidenceItem


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
        chunk texts). No reducer — nodes return the full list.
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
    """

    messages: Annotated[list[BaseMessage], add_messages]
    # operator.add accumulator: nodes return ONLY the new items they
    # produced this turn; LangGraph appends to the prior list. This
    # makes cross-period / multi-tool-call runs accumulate evidence
    # instead of overwriting on every tool invocation. A node that
    # does not produce evidence must omit this key from its return.
    evidence: Annotated[list[EvidenceItem], operator.add]
    repair_count: int
    step_count: int
    answer: str
    citations: tuple[Citation, ...]
    status: Status
    status_reason: str


__all__ = [
    "AgentState",
    "MIN_SPAN_LEN",
    "REPAIR_CAP",
    "RECURSION_LIMIT",
    "STEP_CAP",
    "Status",
    "TERMINAL_STATUSES",
]
