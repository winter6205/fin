"""The GROUNDED capability's ``citation_gate`` graph node.

The node body lives HERE, in the capability that owns the behaviour,
rather than in the host-graph composition root
(:mod:`fin.host.graph`). The composition root builds the node through
:func:`build_citation_gate_node` and hands it every dependency it
needs explicitly, so the capability package never reaches back into
the composition root.

P0 is behaviour preserving: the body below is the body the
composition root used to define inline, with its free variables
rewritten to the frozen dependency bundle. The registered node name
stays ``citation_gate``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from fin.host.grounded.citation_gate import (
    BounceResult,
    Citation,
    DoneResult,
    EvidenceItem,
    RefuseResult,
    verify_citations,
)
from fin.host.grounded.numeric_gate import NumericGateResult
from fin.host.named_failures import named_refusal_reason
from fin.host.span_seams import SegmentIdentity, traced_seam
from fin.host.state import REPAIR_CAP, AgentState


@dataclass(frozen=True)
class GroundedNodeDeps:
    """Everything the ``citation_gate`` node reads from outside itself.

    The two run-scoped values (``known_document_ids`` and
    ``attachment_source_reader_factory``) are the composition root's
    own configuration. The callables below are the host helpers the
    node body reads; they are injected rather than imported so the
    capability package keeps ONE definition of each of them instead of
    a second copy that could drift.
    """

    known_document_ids: set[str] | None
    attachment_source_reader_factory: Callable[[str], Any] | None
    last_ai_text: Callable[[list[BaseMessage]], str]
    format_bounce_template: Callable[[str], str]
    attachment_identity_current_check: Callable[
        [Callable[[str], Any] | None, str], Callable[[EvidenceItem], bool]
    ]
    uncited_contributors: Callable[[dict[str, Any], Iterable[Citation]], list[str]]
    evaluate_numeric_gate_in_node: Callable[
        [list[BaseMessage]], NumericGateResult | None
    ]
    bound_thread_id: Callable[[Mapping[str, Any] | None], str]
    seam_identity: Callable[[RunnableConfig | None], SegmentIdentity | None]


class CitationGateNode(Protocol):
    """The node shape LangGraph calls: ``(state)`` plus the run config."""

    def __call__(
        self, state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]: ...


def build_citation_gate_node(
    deps: GroundedNodeDeps,
) -> CitationGateNode:
    """Build the ``citation_gate`` node over the frozen dependency bundle."""

    @traced_seam("citation_gate_node", deps.seam_identity)
    def citation_gate_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        # Pull the last ``AIMessage``'s text as the candidate
        # answer. Empty content (model returned only tool_calls)
        # falls through to the bounce path so the model can retry
        # with prose.
        messages = cast("dict[str, Any]", state)["messages"]
        candidate_answer = deps.last_ai_text(messages)
        evidence = list(state.get("evidence", []))
        repair_count = int(state.get("repair_count", 0))

        # ADR 0046: the verifier's locator allow-list is THIS run's
        # evidence set — the document ids the run actually retrieved
        # or read — unioned with any static allow-list the caller
        # configured. It is never derived from a manifest or a local
        # extracted tree, so a locator claim nothing brought in this
        # run refuses immediately (SC9).
        known_ids = set(deps.known_document_ids or ())
        known_ids.update(
            item.locator.document_id for item in evidence
        )

        gate_result = verify_citations(
            answer=candidate_answer,
            evidence=evidence,
            repair_count=repair_count,
            known_document_ids=known_ids,
            repair_cap=REPAIR_CAP,
            # The thread is read from the run config — never from state
            # or the model's arguments — so a stale or re-owned
            # attachment cannot keep grounding a quote after the read
            # that produced it.
            attachment_identity_current=deps.attachment_identity_current_check(
                deps.attachment_source_reader_factory, deps.bound_thread_id(config)
            ),
        )

        if gate_result.outcome == "done":
            # ``verify_citations`` builds the outcome token and the
            # class together, so the ``outcome`` leg pins this cast.
            done = cast(DoneResult, gate_result)
            # T5: numeric gate. Walk the AIMessage history for
            # ``get_xbrl_fact`` tool calls this run; each call is
            # an implicit (concept, period) claim. The matching
            # ToolMessage JSON carries the fact value (or the
            # ``fact_not_found`` miss shape). If the model claimed
            # a number that doesn't match an evidenced fact row,
            # the run refuses immediately — T5 spec ("research
            # done does not pass when a claimed numeric fact has
            # no matching evidenced row"). The numeric gate is
            # SKIPPED when the model made no ``get_xbrl_fact`` call
            # (CONTEXT ``get_xbrl_fact``: "non-research skips the
            # fact gate"; here, research without a fact call also
            # skips).
            numeric_outcome = deps.evaluate_numeric_gate_in_node(messages)
            if numeric_outcome is not None and numeric_outcome.outcome == "fail":
                return {
                    "messages": [],
                    "status": "refuse",
                    "status_reason": "numeric_gate_fail",
                    "repair_count": repair_count,
                }
            # T6 (spec Focus 4 / SC7): a fan-out run must keep every
            # issuer that contributed evidence in the synthesis. The
            # verifier above already passed — on the OTHER issuers'
            # spans — so without this check a five-issuer ask could
            # ``done`` on four. Runs with no fan-out skip it entirely.
            dropped = deps.uncited_contributors(
                dict(state.get("issuer_fanout") or {}), done.citations
            )
            if dropped:
                return {
                    "messages": [],
                    "status": "refuse",
                    "status_reason": "issuer_coverage_fail",
                    "repair_count": repair_count,
                }
            return {
                "messages": [],
                "answer": done.answer,
                "citations": tuple(done.citations),
                "status": "done",
                "status_reason": "done",
                "repair_count": repair_count,
            }

        if gate_result.outcome == "refuse":
            refused = cast(RefuseResult, gate_result)
            # T3: the run had NOTHING to cite and retrieve is the
            # reason. The verifier's own vocabulary is untouched
            # (ADR 0008) — the refusal it produced is RE-LABELLED
            # with the more specific retrieve-side fact, which is
            # the whole point of 「具名失败」: an operator must not
            # have to read three different product facts as one
            # ``unknown_locator``. With evidence present the gate's
            # verdict stands EXCEPT on the E2 route
            # (spec ``enterprise-evidence-contract`` §6): an
            # enterprise-material run whose retrieve delivered material
            # but that the verifier capped with
            # ``citation_repair_cap`` names 「the authorized material
            # does not state the asked rule」. With no retrieve at all
            # the gate's verdict stands unchanged.
            return {
                "messages": [],
                "status": "refuse",
                "status_reason": named_refusal_reason(
                    evidence=evidence,
                    outcomes=state.get("retrieve_outcomes"),
                    verifier_reason=refused.reason,
                    track_decision=state.get("track_decision"),
                ),
                "repair_count": repair_count,
            }

        # Bounce: append the hidden template as a SystemMessage at
        # the end of the message list (ADR 0021). The template is
        # data; CLI / HTTP rendering (T7 / T9) strips the marker
        # before printing.
        bounce = cast(BounceResult, gate_result)
        hidden = SystemMessage(
            content=deps.format_bounce_template(bounce.hidden_template)
        )
        return {
            "messages": [hidden],
            "status": "running",
            "status_reason": "",
            "repair_count": bounce.repair_count,
        }

    return citation_gate_node


__all__ = ["CitationGateNode", "GroundedNodeDeps", "build_citation_gate_node"]
