"""Task capability graph nodes.

The ``reconcile_task`` and ``task_outcome`` nodes the host graph owns
for the TASK capability. Their dependencies arrive through the frozen
:class:`TaskNodeDeps` bundle, so this package never reaches the
composition root.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig

from fin.host.state import AgentState
from fin.host.task.task_runtime import decide as decide_task, is_reconciled
from fin.host.task.task_runtime_types import (
    AcceptedInput,
    ReconciliationProposal,
    TaskRuntimeError,
    TaskRuntimeState,
)


class TaskProposalBuilder(Protocol):
    """The composition root's task-proposal derivation for ``reconcile_task``."""

    def __call__(
        self,
        *,
        state: AgentState,
        runtime: TaskRuntimeState,
        accepted: AcceptedInput,
        config: RunnableConfig,
    ) -> ReconciliationProposal: ...


@dataclass(frozen=True)
class TaskNodeDeps:
    """The dependencies the task nodes take from the composition root.

    The helpers and the typed error carrier the composition root owns
    arrive here instead of through a module import, and the bundle is
    frozen so no node can swap a dependency under the graph.
    """

    logger: logging.Logger
    accepted_task_input_from_config: Callable[[RunnableConfig], AcceptedInput | None]
    unreconciled_accepted_input: Callable[[TaskRuntimeState], AcceptedInput | None]
    task_proposal_for_track: TaskProposalBuilder
    reconciliation_error: Callable[[str, str], Exception]
    apply_task_outcome: Callable[[Mapping[str, Any]], dict[str, Any]]


class ReconcileTaskNode(Protocol):
    """The node shape LangGraph calls: ``(state)`` plus the run config.

    A protocol rather than a bare ``Callable``: LangGraph passes
    ``state`` by name, and a plain callable type would lose the
    parameter name, so the composition root's ``add_node`` call would
    not type-check.
    """

    def __call__(
        self, state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]: ...


class TaskOutcomeNode(Protocol):
    """The node shape LangGraph calls: ``(state)``."""

    def __call__(self, state: AgentState) -> dict[str, Any]: ...


def build_reconcile_task_node(
    deps: TaskNodeDeps,
) -> ReconcileTaskNode:
    """The ``reconcile_task`` node, bound to its dependencies."""

    def reconcile_task_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        """Reconcile this turn's accepted input against the current task.

        Question/task runtime T2. Runs on the existing lane between
        ``classify`` and the compaction gate, so it adds no second
        classifier, no second task store, and no new graph topology
        beyond one node: ``decide_track`` already said whether the turn
        is research-bearing, and this node turns that answer into a
        committed task transition.

        The node is idempotent by construction, and it resolves its input
        from two sources in order. The first pass carries the accepted
        record on run config; a choice-window pause skips this node and
        its resume carries no config, so the node falls back to the
        committed envelope's newest unreconciled input. Either way, when
        there is nothing left to reconcile it returns ``{}`` and the lane
        proceeds unchanged — which is what stops a later model or tool
        iteration, or a replayed node, from reconciling twice.
        """
        runtime = TaskRuntimeState.from_state_dict(state.get("task_runtime"))
        accepted = deps.accepted_task_input_from_config(config)
        if accepted is None:
            accepted = deps.unreconciled_accepted_input(runtime)
        if accepted is None:
            return {}
        if is_reconciled(runtime, accepted.input_version):
            return {}
        proposal = deps.task_proposal_for_track(
            state=state,
            runtime=runtime,
            accepted=accepted,
            config=config,
        )
        try:
            committed = decide_task(runtime, accepted, proposal)
        except TaskRuntimeError as exc:
            # Fail CLOSED. The last consistent envelope stays authoritative
            # because this node returns nothing; the run ends as a typed
            # system outcome instead of starting retrieval, tools, or
            # answer formation on an input that was never reconciled.
            #
            # It is deliberately NOT converted into a clarification: an
            # operational failure that asks the user a question keeps the
            # run alive by asking something no answer can fix.
            deps.logger.warning(
                "task reconciliation refused: kind=%s input_version=%s",
                exc.kind,
                accepted.input_version,
                exc_info=True,
            )
            raise deps.reconciliation_error(exc.kind, str(exc)) from exc
        return {"task_runtime": committed.to_state_dict()}

    return reconcile_task_node


def build_task_outcome_node(
    deps: TaskNodeDeps,
) -> TaskOutcomeNode:
    """The ``task_outcome`` node, bound to its dependencies."""

    def task_outcome_node(state: AgentState) -> dict[str, Any]:
        """Apply the grounded-answer peer's assessment to the current task.

        Question/task runtime T6. This is the OUTCOME boundary: a turn
        has ended (the citation gate reached its terminal status) and
        only NOW may the task's outcome change — which is the whole point
        of keeping task outcome and turn outcome separate.

        The assessment is peer-owned and OPTIONAL. With no peer wired
        there is no assessment, so the task stays ACTIVE: a finished turn
        is not a finished task, and this node is the only place that
        distinction becomes state. Host mechanics alone certify nothing,
        so a task reaches ``complete`` only through
        :func:`apply_assessment`, and only when the assessment supports
        the goal with no material gap and every host gate passes.

        The same apply writes ``semantic_outcome``. Technical status is
        not part of this update.
        """
        return deps.apply_task_outcome(state)

    return task_outcome_node
