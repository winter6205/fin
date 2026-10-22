"""QUESTION capability graph-node behaviour.

The QUESTION capability owns the graph nodes that ask, refuse or offer a
choice: ``clarify``, ``dialogue``, ``off_topic``, ``catalog_overview``,
``mode_choice``, ``extract_method_choice``, ``document_choice`` and
``document_choice_wait``. Their bodies live here as module-level
functions; the host composition root wires them through the
``build_*_node`` functions below and passes every dependency explicitly in
:class:`QuestionNodeDeps`.

Nothing here imports the composition root, the transport or the application
boundary: the capability reaches the host graph only through the typed,
frozen dependency bundle the composition root fills in. The capability
names only the TYPES it operates on; every VALUE it reads is injected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from fin.host.agent_instructions import messages_for_model
from fin.host.question.extract_method_proposal import (
    apply_extract_method_pick,
    build_proposal_audit,
    normalize_extract_method,
    propose_window_payload,
)
from fin.host.run_call_supervisor import stream_model_call
from fin.host.state import AgentState, EvidenceReset
from fin.host.stream_map import assemble_final_ai_message
from fin.host.task.task_runtime_types import (
    AcceptedInput,
    ChoiceDetail,
    PendingQuestion,
    TaskRuntimeState,
)


class _ClarifyBinding(Protocol):
    def __call__(self, *, thinking_off: bool) -> Any: ...


class _PolicyRefusalUpdate(Protocol):
    def __call__(self, state: AgentState, *, step_count: int) -> dict[str, Any]: ...


class _OperatorPick(Protocol):
    def __call__(
        self,
        decision: dict[str, Any],
        *,
        pick: str,
        snapshots: list[Any] | None = None,
        rejected: list[str] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ClarifyToolCall:
    """One accepted clarify tool call and the identities it carries.

    The execution attempt counts the DISPATCHES of the logical call, not
    the model attempts that produced it; the logical call id and the
    model attempt are the identity the ledger files the facts under.
    """

    name: str
    args: dict
    tool_call_id: str
    logical_call_id: str
    model_attempt: int
    execution_attempt: int


@dataclass(frozen=True)
class ClarifyToolValidation:
    """The outcome of validating one clarify turn's pending tool calls.

    ``accepted`` are the calls the canonical contract allows to run;
    ``refusals`` are the value-free answers the rejected calls get, in
    the order the model emitted them.
    """

    accepted: list[ClarifyToolCall]
    refusals: list[tuple[str, BaseMessage]]


@dataclass(frozen=True)
class QuestionNodeDeps:
    """Everything the QUESTION nodes need from the host composition root.

    The bundle is explicit and frozen on purpose: the composition root
    fills it in once, the node bodies read it, and no node reaches back
    into ``fin.host.graph`` for a helper. Every field is one host fact
    the moved body used to read from its enclosing scope.
    """

    select_unbound_model: Callable[[RunnableConfig], Any]
    with_turn_attachments: Callable[
        [AgentState, list[BaseMessage]], list[BaseMessage]
    ]
    read_profile_text: Callable[[], str]
    clarify_binding: _ClarifyBinding
    clarify_tool_dispatcher: ToolNode
    #: The validation of the clarify lane's pending tool calls, run by
    #: the composition root against the canonical contracts. The rejected
    #: calls are answered with the value-free payload the ordinary tool
    #: lane uses, never with a raw-kwargs rendering.
    validate_clarify_tool_calls: Callable[
        [RunnableConfig, AgentState, list[tuple[str, dict, str]]],
        ClarifyToolValidation,
    ]
    #: The terminal facts of the clarify lane's dispatch, recorded after
    #: the tools answered.
    record_clarify_tool_execution: Callable[
        [RunnableConfig, Sequence[ClarifyToolCall], Sequence[BaseMessage]],
        None,
    ]
    list_files_tool: BaseTool
    step_cap: int
    cpath: Path
    clarify_missing_slots: Callable[[AgentState], list[str]]
    extract_thinking_text: Callable[[AIMessage], str]
    has_provider_policy_refusal: Callable[[AIMessage], bool]
    last_ai_text: Callable[[list[BaseMessage]], str]
    policy_refusal_update: _PolicyRefusalUpdate
    build_tool_summaries: Callable[
        [list[tuple[str, dict, str]], list[BaseMessage]], list[dict[str, str]]
    ]
    fold_tool_update: Callable[[Any], dict[str, list[Any]]]
    pending_tool_calls: Callable[[AgentState], list[tuple[str, dict, str]]]
    catalog_overview_answer: Callable[[Sequence[Mapping[str, Any]]], str]
    choice_window_interrupt_kind: str
    normalize_search_mode: Callable[[object], str | None]
    clarify_options: Callable[[], list[dict[str, Any]]]
    apply_operator_pick: _OperatorPick

    # ----- the grounded document choice (QA-chain T9) -----
    #
    # The two document-choice nodes sit on the grounded lane between the
    # grounded leaf and the citation gate. Their values are host facts the
    # composition root already holds: the grounded admission helpers (the
    # GROUNDED capability owns them), the document-choice helpers (they
    # live in the composition root), the task-runtime doors and the run's
    # own configuration.
    known_document_ids: set[str] | None
    attachment_source_reader_factory: Callable[[str], Any] | None
    attachment_identity_current_check: Callable[
        [Callable[[str], Any] | None, str], Callable[[Any], bool]
    ]
    verify_citations: Callable[..., Any]
    admit_decision: Callable[..., Any]
    parse_decision_output: Callable[..., Any]
    offer_document_choice: Callable[..., PendingQuestion]
    open_pending_question: Callable[
        [TaskRuntimeState, PendingQuestion], TaskRuntimeState
    ]
    accept_input: Callable[..., tuple[TaskRuntimeState, AcceptedInput]]
    input_reply_kind: str
    document_choice_purpose: str
    document_choice_prompt: str
    document_choice_details: Callable[..., tuple[ChoiceDetail, ...]]
    document_choice_reply_accepted_this_run: Callable[..., bool]
    document_choice_payload: Callable[..., dict[str, Any]]
    validate_document_choice_pick: Callable[..., PendingQuestion | None]
    document_choice_reply_already_accepted: Callable[..., bool]
    document_choice_reply_free_text: Callable[..., str | None]
    document_choice_idempotency_key: Callable[..., str]
    document_choice_scope: Callable[..., Any]
    commit_document_choice: Callable[..., TaskRuntimeState | None]
    pick_instruction_text: Callable[[], str]
    bound_run_id: Callable[[RunnableConfig], str | None]
    bound_thread_id: Callable[[Mapping[str, Any] | None], str]
    repair_cap: int
    evidence_reset: EvidenceReset
    logger: logging.Logger


def _non_research_leaf(
    deps: QuestionNodeDeps,
    state: AgentState,
    config: RunnableConfig,
    *,
    track: Literal["dialogue", "clarify", "off_topic"],
) -> dict[str, Any]:
    """Call unbound model with track prompt; done + empty citations."""
    selected = deps.select_unbound_model(config)
    missing_slots = (
        deps.clarify_missing_slots(state) if track == "clarify" else None
    )
    # The user profile is operator-profile machinery,
    # NOT memory-fact machinery. Surface the merged profile text
    # into the system prefix regardless of memory_on. Read at
    # call time (not compile-time) so file edits between calls
    # take effect on the next turn.
    stream = stream_model_call(
        selected,
        deps.with_turn_attachments(
            state,
            messages_for_model(
                cast("dict[str, Any]", state)["messages"],
                track=track,
                missing_slots=missing_slots,
                user_profile=deps.read_profile_text(),
            ),
        )
    )
    answer_message = assemble_final_ai_message(stream)
    previous = int(state.get("step_count", 0))
    if deps.has_provider_policy_refusal(answer_message):
        return deps.policy_refusal_update(state, step_count=previous + 1)
    this_call_thinking = deps.extract_thinking_text(answer_message)
    prior_thinking = str(state.get("thinking_text", "") or "")
    if prior_thinking and this_call_thinking:
        thinking_text = prior_thinking + "\n" + this_call_thinking
    else:
        thinking_text = prior_thinking or this_call_thinking
    answer = deps.last_ai_text([answer_message])
    return {
        "messages": [answer_message],
        "step_count": previous + 1,
        "answer": answer,
        "citations": (),
        "status": "done",
        "status_reason": "done",
        "thinking_text": thinking_text,
        "track": track,
    }


def _only_tool_calls(message: AIMessage, call_ids: set[str]) -> AIMessage:
    """The message with only the accepted tool calls declared.

    The rejected calls are answered by the host, so the dispatcher must
    not see them: a rejected call that reached the tool body would run
    with arguments the canonical contract already refused.
    """
    return message.model_copy(
        update={
            "tool_calls": [
                call
                for call in message.tool_calls
                if str(call.get("id") or "") in call_ids
            ]
        }
    )


def _tool_messages_in_call_order(
    pending: Sequence[tuple[str, dict, str]],
    dispatched: Sequence[BaseMessage],
) -> list[BaseMessage]:
    """The tool answers in the order the model emitted the calls.

    A real result and a validation refusal answer the same call id, so
    the order follows the calls, not the arrival of the messages.
    """
    by_call: dict[str, list[BaseMessage]] = {}
    extra: list[BaseMessage] = []
    for message in dispatched:
        call_id = str(getattr(message, "tool_call_id", "") or "")
        if call_id:
            by_call.setdefault(call_id, []).append(message)
        else:
            extra.append(message)
    ordered: list[BaseMessage] = []
    for _, _, call_id in pending:
        ordered.extend(by_call.pop(call_id, []))
    for leftover in by_call.values():
        ordered.extend(leftover)
    ordered.extend(extra)
    return ordered


def _clarify_leaf(
    deps: QuestionNodeDeps,
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Ask for the missing slots, grounded in the real catalog.

    Same shape as the grounded lane (call the model → if it asked
    for the catalog, run the narrow ToolNode → call again) and the
    same terminal contract as the other non-research leaves: done,
    EMPTY citations, no citation gate. ``list_files`` output is
    catalog metadata, not a citable span, so it stays off the
    ``evidence`` channel exactly as an attachment-text block does —
    a clarify answer can never pass the grounded citation gate.

    The step count advances once per model call, so the fuse counts
    the tool hop as a real step; the cap is the same
    :data:`fin.host.state.STEP_CAP` the grounded lane uses.
    """
    configurable = dict((config or {}).get("configurable") or {})
    thinking_off = configurable.get("thinking_type") == "disabled"
    bound = deps.clarify_binding(thinking_off=thinking_off)
    missing_slots = deps.clarify_missing_slots(state)
    previous = int(state.get("step_count", 0))
    if previous + 1 >= deps.step_cap:
        return {
            "messages": [],
            "step_count": previous + 1,
            "status": "step_cap",
            "status_reason": "step_cap",
            "thinking_text": str(state.get("thinking_text", "") or ""),
            "track": "clarify",
        }

    def _model_messages() -> list[BaseMessage]:
        return deps.with_turn_attachments(
            state,
            messages_for_model(
                cast("dict[str, Any]", state)["messages"],
                track="clarify",
                missing_slots=missing_slots,
                user_profile=deps.read_profile_text(),
            ),
        )

    stream = stream_model_call(bound, _model_messages())
    first = assemble_final_ai_message(stream)
    messages: list[BaseMessage] = [first]
    step = previous + 1
    if deps.has_provider_policy_refusal(first):
        return deps.policy_refusal_update(state, step_count=step)

    # One catalog hop only: the ask is a missing SLOT, so a second
    # round of tool calls cannot produce information the first one
    # did not already return. A model that still asks for a tool
    # after the catalog came back is answered from what it has.
    if first.tool_calls:
        pending_state: dict[str, Any] = {
            **cast("dict[str, Any]", state),
            "messages": [*list(state.get("messages") or []), first],
        }
        # The pending call is validated against its canonical contract
        # BEFORE the dispatcher sees it: a rejected call is answered with
        # the value-free payload the ordinary tool lane uses, so the
        # model never reads a raw-kwargs rendering of its own arguments.
        validation = deps.validate_clarify_tool_calls(
            config,
            state,
            deps.pending_tool_calls(cast(AgentState, pending_state)),
        )
        accepted_ids = {call.tool_call_id for call in validation.accepted}
        if validation.accepted:
            dispatch_state: dict[str, Any] = {
                **cast("dict[str, Any]", state),
                "messages": [
                    *list(state.get("messages") or []),
                    _only_tool_calls(first, accepted_ids),
                ],
            }
            raw_update: Any = deps.clarify_tool_dispatcher.invoke(
                dispatch_state
            )
            merged = deps.fold_tool_update(raw_update)
            tool_messages = _tool_messages_in_call_order(
                deps.pending_tool_calls(cast(AgentState, pending_state)),
                [*merged["messages"], *(m for _, m in validation.refusals)],
            )
        else:
            tool_messages = [
                message for _, message in validation.refusals
            ]
        deps.record_clarify_tool_execution(
            config, validation.accepted, tool_messages
        )
        messages = [*messages, *tool_messages]
        step += 1
        if step >= deps.step_cap:
            return {
                "messages": messages,
                "step_count": step,
                "answer": deps.last_ai_text(messages),
                "citations": (),
                "status": "step_cap",
                "status_reason": "step_cap",
                "thinking_text": str(state.get("thinking_text", "") or ""),
                "track": "clarify",
            }
        # The second call must carry the AIMessage that PRODUCED the
        # tool result, immediately before that ToolMessage. The
        # grounded lane gets this ordering from LangGraph (model
        # node → tools node → back to model node); this leaf
        # assembles its own second call, so it owns the ordering.
        # ``_model_messages()`` rebuilds from ``state["messages"]``,
        # which by construction never contains this turn's ``first``
        # (the leaf is still inside the call that produced it), so
        # the ToolMessage would be an ORPHAN and the provider
        # rejects it with "tool result's tool id ... not found".
        #
        # ``first`` is appended once, at the tail, right before its
        # results — never merged into the head, so the track prompt /
        # profile / attachment blocks stay exactly the single set
        # ``_model_messages()`` built.
        stream = stream_model_call(
            bound,
            _model_messages() + [first, *tool_messages],
        )
        final = assemble_final_ai_message(stream)
        if deps.has_provider_policy_refusal(final):
            return deps.policy_refusal_update(state, step_count=step)
        messages = [*messages, final]

    answer_message = (
        messages[-1] if isinstance(messages[-1], AIMessage) else first
    )
    this_call_thinking = deps.extract_thinking_text(answer_message)
    prior_thinking = str(state.get("thinking_text", "") or "")
    if prior_thinking and this_call_thinking:
        thinking_text = prior_thinking + "\n" + this_call_thinking
    else:
        thinking_text = prior_thinking or this_call_thinking
    # The pending call is read off THIS turn's own messages (the
    # catalog hop's AIMessage), not off the incoming state, so the
    # trace row names the tool this leaf actually ran.
    turn_messages: AgentState = {"messages": messages}
    tool_summaries = deps.build_tool_summaries(
        deps.pending_tool_calls(turn_messages),
        messages,
    )
    return {
        "messages": messages,
        "step_count": step,
        "answer": deps.last_ai_text(messages),
        "citations": (),
        "status": "done",
        "status_reason": "done",
        "thinking_text": thinking_text,
        "tool_summaries": list(state.get("tool_summaries") or [])
        + tool_summaries,
        "track": "clarify",
    }


def build_dialogue_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``dialogue`` node body."""

    def dialogue_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        return _non_research_leaf(deps, state, config, track="dialogue")

    return dialogue_node


def build_clarify_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``clarify`` node body."""

    def clarify_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        return _clarify_leaf(deps, state, config)

    return clarify_node


def build_off_topic_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``off_topic`` node body."""

    def off_topic_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        return _non_research_leaf(deps, state, config, track="off_topic")

    return off_topic_node


def build_catalog_overview_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``catalog_overview`` node body."""

    def catalog_overview_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        """Show the scoped catalog directly; metadata is not citable evidence."""
        result = cast(
            dict[str, Any], deps.list_files_tool.invoke({}, config=config)
        )
        if "error" in result:
            # EXIT: the existing list_files scope gate refused enumeration;
            # preserve its typed reason instead of rendering an empty catalog.
            return {
                "answer": "",
                "citations": (),
                "status": "refuse",
                "status_reason": cast(str, result["error"]),
            }
        filings = cast(Sequence[Mapping[str, Any]], result["filings"])
        return {
            "answer": deps.catalog_overview_answer(filings),
            "citations": (),
            "status": "done",
            "status_reason": "done",
        }

    return catalog_overview_node


def build_mode_choice_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``mode_choice`` node body."""

    def mode_choice_node(state: AgentState) -> dict[str, Any]:
        """检索档 choice window (search-mode host T2 / ADR 0062 HITL).

        Reached on ``from_classify`` when the grounded turn carries a
        PENDING 档 choice window — a LEGACY receipt, since the cascade
        no longer emits ``clarify`` (ADR 0104) — and on
        ``from_extract_method`` after the propose window. The run pauses
        here on ``interrupt`` until the operator picks a 档, then
        proceeds to the SAME compaction-gate → grounded leaf path
        (retrieve stays ONE tool).
        The loop is the documented validate-then-reopen shape: an
        out-of-list pick reopens the window (never a silent C), and a
        pick of an UNOPENED option is merged as the 具名失败 typed_fail
        by ``apply_operator_pick``. No LLM call happens in this node, so
        LangGraph's replay-on-resume of the node body is side-effect
        free.
        """
        decision = dict(state.get("search_mode_decision") or {})
        options = list(decision.get("options") or []) or deps.clarify_options()
        rejected: list[str] = []
        while True:
            window: dict[str, Any] = {
                "kind": deps.choice_window_interrupt_kind,
                "title": "选择检索档",
                "prompt": "为本次 grounded 提问选择检索档后继续。",
                "options": options,
            }
            if rejected:
                window["rejected_picks"] = list(rejected)
            raw = str(interrupt(window) or "").strip()
            # The option ids are lasting tokens and the pick is resolved
            # through the SSOT total function over the closed set, so a
            # retired letter names no option id and is rejected exactly like
            # ``Z``. Upper-casing a pick here would destroy the lowercase
            # tokens and refuse a client that sends one correctly.
            picked = deps.normalize_search_mode(raw)
            if picked is not None and any(
                str(option.get("id")) == picked for option in options
            ):
                break
            rejected.append(raw)
        assert picked is not None  # reached only through the match above
        merged = deps.apply_operator_pick(
            decision,
            pick=picked,
            snapshots=state.get("status_snapshots"),
            rejected=rejected,
        )
        update: dict[str, Any] = {"search_mode_decision": merged["audit"]}
        if merged["snapshot_patch"]:
            update["status_snapshots"] = merged["snapshot_patch"]
        return update

    return mode_choice_node


def build_extract_method_choice_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``extract_method_choice`` node body."""

    def extract_method_choice_node(state: AgentState) -> dict[str, Any]:
        """Fast 抽图 propose window (search-mode host T4 / ADR 0068 / 0079).

        Reached ONLY on ``from_classify`` when the run input carries an
        explicit ``{"proposed": "fast"}`` seed — the ordinary grounded
        flow never opens it. Same validate-then-reopen interrupt loop as
        ``mode_choice`` (no LLM call), and the payload stays
        ``kind="choice_window"`` so the SPA renders it unchanged.

        A resolved pick routes onto the corpus version switch through
        ``apply_extract_method_pick``: only a valid ``fast`` pick on a request
        naming a recorded ``(document_id, version)`` flips that version
        (the catalog then marks the rebuild intent and the audit reads
        ``applied is True``). Every other outcome applies nothing —
        the switch call is idempotent, so LangGraph's replay-on-resume
        of the node body cannot double-apply. No snapshot channel is
        touched: the extract method never rides the 档 audit.
        """
        request = dict(state.get("extract_method_proposal") or {})
        rejected: list[str] = []
        while True:
            raw = str(interrupt(propose_window_payload(rejected)) or "").strip()
            picked = normalize_extract_method(raw)
            if picked is not None:
                break
            rejected.append(raw)
        applied, apply_error = apply_extract_method_pick(
            request, picked, catalog_path=deps.cpath
        )
        return {
            "extract_method_proposal": build_proposal_audit(
                request,
                picked=picked,
                rejected=rejected,
                applied=applied,
                apply_error=apply_error,
            )
        }

    return extract_method_choice_node


def build_document_choice_admit_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``document_choice`` node body."""

    def document_choice_admit_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        """The grounded document choice, PART 1 of 2: admission (QA-chain T9).

        Reached on the grounded lane AFTER the compaction gate and BEFORE
        the citation gate — that is the admission point: retrieval has run,
        so this run's evidence is the complete admitted set, and nothing
        has yet been published on the wire. The decision is admitted once
        through :func:`fin.host.grounded.grounded_decision.admit_decision`, whose
        injected ``verify`` IS this host's one R3 verifier
        (:func:`verify_citations`) — this lane therefore owns no second
        citation parser.

        This node only ADMITS and COMMITS; it never pauses. That split is
        what makes the question durable: LangGraph's ``interrupt`` raises,
        so any state this node returned would be discarded — the committed
        question would be lost and the resume would have nothing to
        validate against. :func:`document_choice_interrupt_node` runs next
        and pauses on the question this node already committed.

        Exits:

        * EXIT merge / evidence_refuse: nothing is published here, so the
          citation gate remains the only terminal authority and this node
          cannot bypass it.
        * EXIT pending: the run-scoped ``grounded_decision`` audit records
          ``outcome="choice"``, and the ONE ``PendingQuestion`` is built
          through ``offer_document_choice`` and published on the single
          thread-scoped ``task_runtime`` envelope through Runtime's
          :func:`open_pending_question`. No accepted input is
          manufactured: publishing a question is transport, not an input.
        """
        runtime = TaskRuntimeState.from_state_dict(state.get("task_runtime"))
        # Resume of a paused document choice: the question is ALREADY
        # committed, so this node must not admit a second one. Publishing a
        # replacement would both duplicate the question authority and
        # discard the accepted reply the pause just recorded.
        pending = runtime.pending_question
        if pending is not None and pending.purpose == deps.document_choice_purpose:
            return {"grounded_decision": {"outcome": "choice_resume"}}
        # A reply THIS RUN accepted means the decision call has already
        # been spent for this evidence set: the pause cleared the question,
        # so a citation-gate bounce routes back through this node, and
        # admitting again would re-open the question the user just
        # answered AND re-run the one decision/synthesis call the budget
        # allows only once per evidence set. The resumed run
        # re-synthesizes against the chosen scope instead (the pause node
        # routes it back to the leaf).
        #
        # Scoped to THIS run, not to the thread. ``input_history`` is
        # bounded thread-wide history, so scanning it for ANY accepted
        # reply made document choice permanently unavailable for the
        # thread after its first pick — a fresh, later, ambiguous question
        # would publish nothing and refuse the turn. The grounding
        # experience offers a choice "for a retrieved evidence set"
        # (specs/qa-chain-grounded-experience.md:48), which is per turn.
        # The accepted reply carries the run that accepted it, and a fresh
        # send mints a fresh run id, so the identity that matters is the
        # run — never recency, and never the whole history.
        if deps.document_choice_reply_accepted_this_run(runtime, config=config):
            return {"grounded_decision": {"outcome": "choice_answered"}}
        evidence = list(state.get("evidence", []))

        # The one admission call for this turn. Its ``verify`` is bound to
        # everything but the answer text, so it is the SAME R3 verifier
        # call the citation gate makes — not a second parser.
        known_ids = set(deps.known_document_ids or ())
        known_ids.update(item.locator.document_id for item in evidence)

        def _verify(answer: str) -> Any:
            return deps.verify_citations(
                answer=answer,
                evidence=evidence,
                repair_count=int(state.get("repair_count", 0)),
                known_document_ids=known_ids,
                repair_cap=deps.repair_cap,
                attachment_identity_current=deps.attachment_identity_current_check(
                    deps.attachment_source_reader_factory,
                    deps.bound_thread_id(config),
                ),
            )

        # GE-2 (specs/qa-chain-grounded-experience.md): the state key is
        # ``retrieve_attempted``; ``admit_decision`` names the parameter
        # ``retrieval_attempted``. The outcome fallback is the pre-flag
        # derivation, so a checkpoint written by older code resumes with
        # the same admission instead of losing an attempt.
        retrieval_attempted = bool(state.get("retrieve_attempted")) or any(
            int(row.get("chunks") or 0) > 0
            for row in state.get("retrieve_outcomes") or ()
        )

        messages = cast("dict[str, Any]", state)["messages"]
        decision_output = deps.last_ai_text(messages)
        parsed = deps.parse_decision_output(decision_output)
        try:
            decision = deps.admit_decision(
                answer_text=decision_output,
                parsed=parsed,
                evidence=evidence,
                retrieval_attempted=retrieval_attempted,
                verify=_verify,
            )
        except ValueError:
            # ``admit_decision`` refuses to claim an evidence refusal for a
            # turn that never searched. That is a contract violation of the
            # CALLER (the attempt flag disagrees with the recorded
            # outcomes), so it must not become a user-visible question:
            # the run falls through to the citation gate unchanged.
            deps.logger.warning(
                "grounded admission refused: evidence refusal without a "
                "recorded retrieval attempt (thread_id=%s)",
                deps.bound_thread_id(config),
            )
            return {}

        audit: dict[str, Any] = {
            "outcome": decision.outcome,
            "fallback": decision.fallback,
            "parse_failed": decision.parse_failed,
            "retrieval_attempted": decision.retrieval_attempted,
            "candidate_count": len(decision.candidates),
        }
        if decision.outcome != "choice":
            return {"grounded_decision": audit}

        origin_run_id = deps.bound_run_id(config)
        if origin_run_id is None:
            # A document choice must name the run it interrupts. Without a
            # bound run id there is nothing honest to name, so the run
            # falls through to the citation gate rather than publishing a
            # question no reply could ever be validated against.
            deps.logger.warning(
                "grounded admission produced a choice with no bound run id; "
                "continuing to the citation gate"
            )
            return {"grounded_decision": audit}

        question = deps.offer_document_choice(
            question_id=f"doc-choice-{origin_run_id}",
            task_id=runtime.current_task.task_id if runtime.current_task else "",
            task_version=(
                runtime.current_task.task_version if runtime.current_task else 1
            ),
            prompt=deps.document_choice_prompt,
            choice_details=deps.document_choice_details(
                decision, catalog_path=deps.cpath
            ),
            origin_run_id=origin_run_id,
            input_version=runtime.input_version,
        )
        return {
            "grounded_decision": audit,
            "task_runtime": deps.open_pending_question(
                runtime, question
            ).to_state_dict(),
        }

    return document_choice_admit_node


def build_document_choice_interrupt_node(
    deps: QuestionNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``document_choice_wait`` node body."""

    def document_choice_interrupt_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        """The grounded document choice, PART 2 of 2: pause and resume.

        One node, two halves, joined by the ``interrupt``:

        * no pending ``document_choice`` — EXIT immediately with ``{}``, so
          a merge / evidence-refusal turn costs nothing and continues to
          the citation gate unchanged;
        * one pending ``document_choice`` — pause on a payload that
          REFERENCES the committed question (``question_id``, the ordered
          ``choices``, the prompt) instead of storing a second candidate
          list, then validate-then-reopen on resume.

        The loop is the same shape as ``mode_choice_node``: an invalid,
        stale, foreign or out-of-list pick REOPENS the same committed
        question rather than accepting it. No model call and no store
        access happens in the loop, so LangGraph's replay-on-resume of
        this body is side-effect free.

        A valid pick advances ``input_version`` EXACTLY ONCE through the
        single :func:`accept_input` door, reconciles the reply through
        Runtime, and commits the checked source-scope proposal in the SAME
        node update — never a scope write before Runtime accepted the
        reply.
        """
        runtime = TaskRuntimeState.from_state_dict(state.get("task_runtime"))
        question = runtime.pending_question
        if question is None or question.purpose != deps.document_choice_purpose:
            return {}

        resume_value: Any = None
        while True:
            resume_value = interrupt(deps.document_choice_payload(question))
            if isinstance(resume_value, Mapping):
                # The doors build a dict so the reserved Other token can
                # carry the user's rephrase (contract line 90). Only the
                # pick is validated; an unreadable shape validates to
                # nothing and reopens the same question.
                raw = str(resume_value.get("choice_id") or "").strip()
            else:
                # A bare string is the pick: ``/resume``'s ``pick`` and a
                # direct ``Command(resume=<choice_id>)`` both send one.
                raw = str(resume_value or "").strip()
            validated = deps.validate_document_choice_pick(
                runtime, config=config, raw_pick=raw
            )
            if validated is not None:
                break

        question = validated
        if deps.document_choice_reply_already_accepted(
            runtime,
            question_id=question.question_id,
            choice_id=raw,
        ):
            # Idempotent double click: this exact reply is already in
            # accepted history. Applying nothing is what makes the retry
            # idempotent — ``accept_input`` is NOT called, so
            # ``input_version`` cannot move again.
            return {"grounded_decision": {"pick": raw, "duplicate_pick": True}}

        # “换个问法” carries the operator's EXACT rephrase as the reply
        # payload; a candidate click has no user-authored text and stores
        # ``None`` (contract line 90 — never prose fabricated from the
        # label).
        reply_text = deps.document_choice_reply_free_text(
            resume_value, choice_id=raw
        )
        accepted_runtime, accepted = deps.accept_input(
            runtime,
            kind=deps.input_reply_kind,
            payload=reply_text,
            provenance="document_choice_resume",
            question_id=question.question_id,
            choice_id=deps.document_choice_idempotency_key(
                runtime,
                question_id=question.question_id,
                choice_id=raw,
            ),
            run_id=question.origin_run_id,
        )
        scope = deps.document_choice_scope(
            question, pick=raw, accepted_input_version=accepted.input_version
        )
        committed = deps.commit_document_choice(accepted_runtime, accepted=accepted)
        if committed is None:
            # Runtime refused the reply, so NO input and NO scope write
            # happens: the envelope stays exactly as it was and the run
            # continues unchanged.
            return {"grounded_decision": {"pick": raw, "refused": True}}
        update: dict[str, Any] = {
            "task_runtime": committed.to_state_dict(),
            # The resumed turn answers under the accepted selection. The
            # pre-pause evidence accumulated BEFORE the pick was accepted,
            # so it is dropped here and the leaf re-retrieves within the
            # narrowed scope; the pick itself rides the user turn (plain
            # free text) with the marker instruction system message
            # beside it, in the bounce's message-carrying house style.
            "evidence": deps.evidence_reset,
            "messages": [
                # For Other this is the operator's own rephrase — the
                # question the leaf must answer. A click has no prose of
                # its own, so its id rides the turn exactly as before.
                HumanMessage(content=reply_text or raw),
                SystemMessage(content=deps.pick_instruction_text()),
            ],
        }
        if scope is not None:
            update["source_scope"] = scope.to_state_dict()
        return update

    return document_choice_interrupt_node


__all__ = [
    "ClarifyToolCall",
    "ClarifyToolValidation",
    "QuestionNodeDeps",
    "build_catalog_overview_node",
    "build_clarify_node",
    "build_dialogue_node",
    "build_document_choice_admit_node",
    "build_document_choice_interrupt_node",
    "build_extract_method_choice_node",
    "build_mode_choice_node",
    "build_off_topic_node",
]
