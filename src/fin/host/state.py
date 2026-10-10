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
from fin.host.track_decision import Track


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


@dataclass(frozen=True)
class FanoutReset:
    """Sentinel value: replace ``AgentState["issuer_findings"]`` with ``[]``.

    Same shape and the same reason as :class:`EvidenceReset` (see its
    docstring for why an empty frozen dataclass and not ``__slots__``):
    the fan-out findings channel is additive so LangGraph can merge the
    parallel per-issuer branches, and an additive reducer cannot be
    cleared by writing ``[]``. The fan-out planner writes this sentinel
    one superstep before the workers run, so each fan-out starts from a
    clean roster even inside a single run.
    """


FANOUT_RESET: Final[FanoutReset] = FanoutReset()


def _issuer_findings_reducer(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]] | FanoutReset,
) -> list[dict[str, Any]]:
    """Reducer for ``AgentState["issuer_findings"]`` (T6, ADR 0034).

    Mirrors :func:`_evidence_reducer`: a ``FanoutReset`` clears the
    channel, an empty list is a no-op, and a non-empty list concats.
    The concat path is what makes the Send fan-out a map-reduce — each
    parallel issuer node returns ONLY its own finding and LangGraph
    merges them.
    """
    if isinstance(right, FanoutReset):
        return []
    if not right:
        return list(left or [])
    return list(left or []) + list(right)


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


@dataclass(frozen=True)
class VisualClaimsReset:
    """Sentinel value: replace ``AgentState["visual_claims"]`` with ``[]``.

    Same shape and the same reason as :class:`EvidenceReset` — including why
    an empty frozen dataclass and not a ``__slots__`` class, which is that the
    checkpointer serializes it through the dataclass branch: the visual-claim
    channel is additive so several reads of one run accumulate, and an
    additive channel cannot be cleared by writing ``[]``.
    """


VISUAL_CLAIMS_RESET: Final[VisualClaimsReset] = VisualClaimsReset()


def _visual_claims_reducer(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]] | VisualClaimsReset,
) -> list[dict[str, Any]]:
    """Reducer for ``AgentState["visual_claims"]`` (ADR 0089, spec T5).

    Mirrors :func:`_evidence_reducer`: the sentinel clears, an empty list is
    a no-op, and a non-empty list concats — several visual reads in one run
    keep several tokens. The reducer MUST NOT mutate either operand.
    """
    if isinstance(right, VisualClaimsReset):
        return []
    if not right:
        return list(left or [])
    return list(left or []) + list(right)


@dataclass(frozen=True)
class StructuredEvidenceReset:
    """Sentinel value: replace ``AgentState["structured_evidence"]`` with ``[]``.

    Same shape and the same reason as :class:`VisualClaimsReset`.
    """


STRUCTURED_EVIDENCE_RESET: Final[StructuredEvidenceReset] = StructuredEvidenceReset()


def _structured_evidence_reducer(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]] | StructuredEvidenceReset,
) -> list[dict[str, Any]]:
    """Reducer for ``AgentState["structured_evidence"]``.

    Mirrors :func:`_visual_claims_reducer`: the sentinel clears, an empty
    list is a no-op, and a non-empty list concats. The reducer MUST NOT
    mutate either operand.
    """
    if isinstance(right, StructuredEvidenceReset):
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
# One agent-loop iteration that calls a tool costs THREE supersteps
# in T1: ``model_node``, ``tool_node``, then the new
# ``compaction_gate`` (ADR 0044 / plan T1); the citation gate adds
# one more on the final turn. So the worst case before the fin
# fuse fires is ~``3 * STEP_CAP`` supersteps. The default must
# therefore exceed ``3 * STEP_CAP`` — a bare ``STEP_CAP + margin``
# would let ``GraphRecursionError`` preempt the fin-owned
# ``step_cap`` fuse, which the spec forbids (success criteria item
# 5: "LangGraph ``recursion_limit`` is > 32 so this fuse fires
# first" — fires first means AFTER the fin fuse, never instead of
# it).
RECURSION_LIMIT: Final[int] = 3 * STEP_CAP + 9  # = 105


# ----- Soft-Track status snapshot (T3) -----


class StatusSnapshot(TypedDict, total=False):
    """One append-only research working-context record.

    Latest snapshot is the active routing view. On company change,
    dependent fields (``period``, ``document_ids``) may be empty.

    ``search_mode`` (search-mode host T1): the 检索档 assigned to this
    retrieve job, carried by the mode-patch append
    (``fin.host.search_mode_decision.mode_snapshot_patch``). A
    company-change wipe emits no such key, which is exactly what makes
    the inherit tier stop inheriting across a new job.
    """

    company: str
    period: str
    document_ids: list[str]
    search_mode: str


# ----- AgentState -----

# ``total=False`` so the state is tolerant of fresh threads that
# haven't been written to yet. LangGraph's last-write-wins reducer
# applies to every field except ``messages``, which uses
# ``add_messages`` so prior chat survives across model_node cycles.
#
# ``answer`` / ``citations`` are populated only on ``status ==
# "done"``. ``status_reason`` carries the gate's refuse reason or
# ``"step_cap"``; the full vocabulary lives in ONE place,
# ``fin.host.named_failures`` (the 具名失败 codes) plus the
# ``fin.host.citation_gate`` verdicts — this comment deliberately
# does not re-list them, because a re-listed set drifts.
class AgentState(TypedDict, total=False):
    """The state the host graph (T6) writes to.

    Keys
    ----
    messages
        Conversation history. ``add_messages`` reducer; prior turns
        survive across model_node cycles.
    evidence
        Cumulative list of ``EvidenceItem`` objects produced by this
        run's tool calls (``read_source`` unit slices and ``retrieve``
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
    track
        Soft-Track T1: the per-run track chosen by ``classify``
        (``dialogue`` / ``clarify`` / ``off_topic`` / ``grounded``).
    track_decision
        Soft-Track T1: auditable dict with ``track``, ``confidence``,
        ``decision_tier``, and structured ``signals``. Cleared by
        ``reset_input_state`` for each new run.
    search_mode_decision
        search-mode host T1: auditable dict mirroring the
        ``track_decision`` shape — ``search_mode`` (explicit ``None``
        on clarify / typed fail), ``confidence``, ``decision_tier``,
        structured ``signals``, ``options`` (choice-window data for
        T2) and ``failure_code`` (具名失败 on typed fail). Written only
        on ``grounded`` runs; cleared by ``reset_input_state`` for each
        new run. The 档 is NOT a fifth track: ``track`` stays the
        Soft-Track leaf.
    extract_method_proposal
        search-mode host T4 (ADR 0068 / 0079): the Fast 抽图 PROPOSE
        audit — ``{proposed, picked, applied, operator_confirmed,
        decision_tier}`` plus ``apply_error`` on a typed refusal (shape
        SSOT: ``fin.host.extract_method_proposal``). Doubles as the
        minimal trigger: an input seed ``{"proposed": "fast"}`` with no
        ``picked`` opens the propose window on a grounded run; the node
        completes the audit with the operator pick. ``applied`` is
        ``True`` ONLY when a valid fast pick named a recorded
        ``(document_id, version)`` (the seed's optional context keys)
        and the catalog switch flipped that row — every other
        outcome keeps ``False``: no silent Fast. The
        Fast string is NOT a 检索档 and never rides
        ``search_mode_decision``. Overwrite semantics; run-scoped via
        ``reset_input_state``.
    status_snapshots
        Soft-Track T3: append-only research working-context history
        (``company``, ``period``, ``document_ids``). Latest entry is
        the active routing view. ``reset_input_state`` must NOT wipe
        this channel.
    comparisons
        T3 (enterprise-kb Focus 1): the ``fin.comparison``
        envelopes this run's ``compare_facts`` calls produced, each
        carrying the ``tool_call_id`` that generated it. Structured
        data, not prose — the workspace turn row renders a table off
        it and the numeric gate reads its columns as claims. Run
        scoped: ``reset_input_state`` clears the channel so a
        follow-up never inherits the previous turn's table.
    analysis_results
        T6: structured redline, table, and statement envelopes
        produced by host tools. Each entry carries the producing
        ``tool_call_id`` and is reset between turns.
    issuer_plan
        T6 (enterprise-kb Focus 4 / ADR 0034): the roster the fan-out
        planner approved for this run — ``{issuers, query, cap,
        requested}`` — or the typed ``issuer_overflow`` /
        ``empty_fanout`` EXIT it refused with. Overwrite semantics
        (one writer, the planner node). Shape SSOT is
        ``fin.host.tools.issuer_fanout``.
    issuer_findings
        T6: one record per fan-out worker (``issuer``, ``status``,
        ``chunk_count``, ``locators``, ``error``). Additive via
        :func:`_issuer_findings_reducer` because LangGraph merges the
        parallel ``Send`` branches into this channel; the planner
        writes ``FANOUT_RESET`` before the branches start so a second
        fan-out in the same run does not read the first one's rows.
    issuer_fanout
        T6: the ``fin.issuer_fanout.v1`` envelope the join node built
        from ``issuer_findings``. Structured data the workspace tool
        trace narrates and the citation gate reads to enforce
        "every contributing issuer keeps a citation" (SC7). Overwrite
        semantics; run scoped via ``reset_input_state``.
    attachments
        corpus-serve-ingest T4 (ADR 0047 **thread attach**): the
        turn-scoped, read-only attachment blobs the operator attached
        to THIS send — one ``{attachment_id, filename, text}`` dict
        per file. Overwrite semantics; ``reset_input_state`` carries
        the incoming list on the new run and clears it (``[]``) on
        every run without attachments, so the text is gone from the
        next turn. The text is INJECTED CALL-SCOPED at model time
        (see ``fin.host.agent_instructions``) and NEVER enters the
        ``evidence`` channel — an attachment-only span cannot pass
        the citation gate (ADR 0046). Storage is a turn blob in the
        workspace router's memory; nothing is written to the corpus
        store or any extracted tree, and nothing is pushed.
    retrieve_outcomes
        T3 (spec SC4–SC5): one ``{"chunks": int, "mapping_failures":
        int}`` record per completed ``retrieve`` call this run —
        admitted chunk count plus the drops THAT CALL produced, read
        off the ledger's per-thread counter so a concurrent run's drop
        is never charged here (the ledger is process-wide, the
        concurrent-run guard is per ``thread_id``). The citation gate
        reads the channel to name an empty-bound 资料集合
        (``corpus_not_ready``) or a mapping drop
        (``mapping_drop``) instead of letting one verifier reason
        stand for three different product facts. Overwrite semantics
        with a single writer (``tool_node_with_summaries``, which
        folds prior + new itself), so ``reset_input_state``'s ``[]``
        genuinely clears it and a follow-up send cannot inherit the
        previous run's verdict.
    visual_claims
        ADR 0089 / spec T5: one provenance row per visual observation this
        run read (shape SSOT:
        :data:`fin.host.visual_evidence.VISUAL_CLAIM_FIELDS`). A
        generated observation is DERIVED material, never a source
        quote, so it keeps its own channel rather than joining
        ``citations`` — the citation gate never sees these rows and
        cannot accidentally demand a verbatim span from them. Additive
        via :func:`_visual_claims_reducer` because several reads in one
        run keep several tokens; nodes return ONLY the rows they
        produced, and clearing needs the ``VISUAL_CLAIMS_RESET``
        sentinel (``prior + [] == prior``), so
        ``reset_input_state`` passes the sentinel and a follow-up send
        cannot cite the previous turn's page.
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
    track: Track
    track_decision: dict[str, Any]
    # search-mode host T1: run-scoped 档 audit (overwrite semantics,
    # single writer ``classify_node``); see the field docstring above.
    search_mode_decision: dict[str, Any]
    # search-mode host T4: run-scoped Fast propose audit AND the
    # explicit propose-request seed; see the field docstring above.
    extract_method_proposal: dict[str, Any]
    status_snapshots: Annotated[list[StatusSnapshot], operator.add]
    # T3 (enterprise-kb Focus 1): OVERWRITE semantics, not
    # ``operator.add``. ``tool_node`` already reads ``state`` and so
    # returns prior + new itself; an additive reducer would make
    # ``reset_input_state``'s ``[]`` a no-op (``prior + [] == prior``)
    # and a follow-up run would inherit the previous turn's table.
    comparisons: list[dict[str, Any]]
    analysis_results: list[dict[str, Any]]
    # T6 (enterprise-kb Focus 4 / ADR 0034). ``issuer_plan`` and
    # ``issuer_fanout`` have single writers (the planner and the join
    # node), so they stay overwrite-semantics and ``reset_input_state``
    # genuinely clears them. ``issuer_findings`` is the only channel
    # several nodes write in the SAME superstep, which is why it needs
    # a reducer and a reset sentinel.
    issuer_plan: dict[str, Any]
    issuer_findings: Annotated[list[dict[str, Any]], _issuer_findings_reducer]
    issuer_fanout: dict[str, Any]
    # corpus-serve-ingest T4 (ADR 0047): turn-scoped attachment blobs.
    # Overwrite semantics (single writer: the run input); see the
    # field docstring above for the citation/isolation contract.
    attachments: list[dict[str, str]]
    # T3 (spec SC4–SC5): OVERWRITE semantics, not ``operator.add``.
    # ``tool_node_with_summaries`` already reads ``state`` and so folds
    # prior + new itself; an additive reducer would make
    # ``reset_input_state``'s ``[]`` a no-op (``prior + [] == prior``)
    # and a follow-up send would inherit the previous run's outcome —
    # the exact stale verdict the run-scoped reset exists to prevent.
    retrieve_outcomes: list[dict[str, int]]
    # ADR 0089 / spec T5: the visual-claim rows this run read, on their OWN
    # channel (never ``citations``). Additive — several reads in one run keep
    # several tokens — which is why it takes a reducer and a reset sentinel
    # rather than ``[]``: the writer (``tool_node_with_summaries``) returns
    # ONLY the new rows, exactly like ``evidence``.
    visual_claims: Annotated[list[dict[str, Any]], _visual_claims_reducer]
    # structured-corpus-evidence: the stored structured snapshots this run
    # admitted, one ``{snapshot, digest, selector}`` row per cited row or
    # count receipt, on their OWN channel (never ``citations`` — a
    # structured value supports a structured claim and can never authorize
    # a quotation).
    #
    # WHY additive and not overwrite-style: several retrieve calls in one
    # run are several admitted result sets, and the writer returns ONLY the
    # records this tool call produced — so a prior call's records would be
    # overwritten by the next call's. The same reason makes ``[]`` useless
    # as a reset (an empty right operand is a no-op under this reducer,
    # not a clear), which is why clearing takes the sentinel instead.
    structured_evidence: Annotated[list[dict[str, Any]], _structured_evidence_reducer]
    # Question/task runtime T2–T4: the thread-scoped task envelope
    # (``fin.host.task_runtime_types.TaskRuntimeState``), stored as its
    # one state dict. Thread-scoped and therefore OMITTED from
    # ``reset_input_state`` below, exactly like ``status_snapshots``:
    # a run-scoped reset, a resume, and a compaction all preserve task
    # history.
    #
    # ONE key rather than sibling channels because the task transition,
    # its reconciliation marker, its pending question, and its execution
    # disposition commit together — separate keys would let a partial
    # write look like a committed decision. Overwrite semantics: a single
    # writer returns the whole next envelope, and ``{}`` genuinely clears
    # it. A thread whose checkpoint predates this field simply has no
    # key and reads as empty (no backfill migration).
    task_runtime: dict[str, Any]
    # The grounded-answer peer's verdict for THIS turn, read only by the
    # task-outcome boundary. Declared here because an undeclared key is
    # silently dropped by the checkpointer, which would leave the peer
    # channel unreadable rather than merely unwired. Run-scoped: the
    # verdict is about one turn's evidence and must not survive into the
    # next.
    grounding_assessment: dict[str, Any]


def reset_input_state(
    question: str,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the standard run-scoped reset input for ``graph.invoke``.

    Every invoke resets the run-scoped fields (ADR 0016, spec line 34
    - the six v1 fields plus T3's ``thinking_text`` /
    ``tool_summaries`` and the later comparison / fan-out channels)
    while leaving ``messages`` to ``add_messages`` accumulation.
    Centralised so the three call sites (``/ask``,
    ``/api/threads/{id}/messages``, CLI ``run``) cannot drift on which
    fields are reset.

    Returns a dict suitable for ``graph.invoke(state, config=...)``
    that clears ``evidence`` (via the ``EVIDENCE_RESET`` sentinel),
    zero-out ``step_count`` / ``repair_count``, and blanks
    ``answer`` / ``citations`` / ``status``. ``messages`` carries
    only the new ``HumanMessage(question)``; the checkpointer's
    ``add_messages`` reducer merges it with prior conversation.

    ``attachments`` (corpus-serve-ingest T4, ADR 0047): the caller
    passes the turn-scoped attachment blobs (``{attachment_id,
    filename, text}``) referenced by THIS send; the default ``None``
    yields ``[]`` so a follow-up send without attachments genuinely
    clears the channel — the previous turn's attachment text cannot
    leak into the next run. The text is injected call-scoped at model
    time and never enters ``evidence`` (ADR 0046).
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
        # T3 (enterprise-kb Focus 1): the comparison channel is
        # overwrite-semantics, so ``[]`` genuinely clears it and a
        # follow-up starts without the previous turn's table.
        "comparisons": [],
        "analysis_results": [],
        # T6 (enterprise-kb Focus 4): the fan-out channels are run
        # scoped too. ``issuer_findings`` is additive, so it takes the
        # sentinel rather than ``[]``.
        "issuer_plan": {},
        "issuer_findings": FANOUT_RESET,
        "issuer_fanout": {},
        # T4: every fresh send clears the in-flight marker. The
        # live GET-while-running signal lives on the per-thread
        # lock (graph.is_locked), not on this state field; we
        # reset it for state-schema completeness so a follow-up
        # send starts from a clean ``False`` (T4 acceptance).
        "in_flight": False,
        # Soft-Track T1: clear run-scoped track decision; classify
        # rewrites track + track_decision on the new run.
        # status_snapshots are durable thread history (T3) — omit.
        "track_decision": {},
        # search-mode host T1: the 档 audit is run-scoped like the track
        # audit; the durable 档 lives on ``status_snapshots`` (inherit
        # source), not here — omit that channel as above.
        "search_mode_decision": {},
        # search-mode host T4: the propose audit/seed is run-scoped — a
        # stale Fast proposal must never reopen the window on a fresh
        # send (``proposal_is_pending`` only fires on an unpicked seed).
        "extract_method_proposal": {},
        # corpus-serve-ingest T4 (ADR 0047): the incoming attachment
        # blobs ride THIS run's input; every run without attachments
        # (default ``None``) clears the channel so attachment text is
        # this-turn only.
        "attachments": list(attachments or []),
        # T3 (spec SC4–SC5): the retrieve-outcome channel is run
        # scoped, so a follow-up send's verdict comes from its OWN
        # retrieve calls — a stale ``corpus_not_ready`` cannot outlive
        # its run.
        "retrieve_outcomes": [],
        # ADR 0089: a visual claim belongs to the run that proved the
        # raster it names, so a follow-up send starts with none — the
        # page may have been re-uploaded under a new version and the
        # stored observation no longer describes these pixels. The
        # channel is additive, so clearing it takes the sentinel.
        "visual_claims": VISUAL_CLAIMS_RESET,
        # structured-corpus-evidence: a stored snapshot belongs to the run
        # whose same-turn result proved it, so a follow-up send starts with
        # none. The channel is additive, so clearing it takes the sentinel.
        "structured_evidence": STRUCTURED_EVIDENCE_RESET,
        # Task runtime T6: the grounded-answer verdict is about ONE
        # turn's evidence, so a follow-up send starts with none — a
        # stale ``supported`` assessment would complete the NEXT task
        # on evidence gathered for a different question.
        "grounding_assessment": {},
        # task_runtime is deliberately OMITTED: it is thread-scoped task
        # history, and a run-scoped reset must not erase it. Same reason
        # ``status_snapshots`` is omitted above.
    }


__all__ = [
    "AgentState",
    "EVIDENCE_RESET",
    "EvidenceReset",
    "FANOUT_RESET",
    "FanoutReset",
    "MIN_SPAN_LEN",
    "REPAIR_CAP",
    "RECURSION_LIMIT",
    "STEP_CAP",
    "STRUCTURED_EVIDENCE_RESET",
    "Status",
    "StatusSnapshot",
    "TERMINAL_STATUSES",
    "Track",
    "VISUAL_CLAIMS_RESET",
    "VisualClaimsReset",
    "_evidence_reducer",
    "_issuer_findings_reducer",
    "_structured_evidence_reducer",
    "_visual_claims_reducer",
    "reset_input_state",
]
