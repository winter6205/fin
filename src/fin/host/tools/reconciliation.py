"""P3 T5: reconcile an uncertain tool effect before another execution.

:func:`execution_decision` is a pure function of the canonical ledger
facts. It never reads an unknown outcome as "no effect": a known failure
re-executes only under a retry policy the tool's contract DECLARES, and an
uncertain outcome stays unknown until a reconciliation proves its state by
stable operation identity.

:func:`record_reconciliation` writes that proof as one more ledger fact
through the ledger's fenced ``record`` path, so a stale worker cannot
resolve an outcome it no longer owns.

No registered tool declares a reconciliation policy today, so an operation
that needs one is reported as ``reconciliation_policy_open`` instead of a
guessed replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol, Sequence

from fin.host.tools.contracts import ToolContract
from fin.host.tools.ledger import (
    ResolutionCode,
    ToolOutcomeLedgerPort,
    ToolOutcomeObservation,
)

__all__ = [
    "REFUSAL_CODES",
    "ExecutionDecision",
    "ExecutionDecisionKind",
    "OutcomeFactView",
    "OwnershipView",
    "ReconciliationRefusal",
    "ReconciliationRefused",
    "ResolutionCode",
    "execution_decision",
    "record_reconciliation",
    "refusal_code",
]

#: What the decision layer says about one business operation.
ExecutionDecisionKind = Literal[
    "execute",
    "duplicate_delivery",
    "retry_not_permitted",
    "reconciliation_required",
    "reconciliation_policy_open",
]

#: The fixed, value-free code a blocked decision answers with. The model
#: sees the code only: no operation id, no tool result, no reason text.
REFUSAL_CODES: Final[dict[ExecutionDecisionKind, str]] = {
    "duplicate_delivery": "tool_duplicate_delivery",
    "retry_not_permitted": "tool_retry_not_permitted",
    "reconciliation_required": "tool_outcome_unknown_reconciliation_required",
    "reconciliation_policy_open": "tool_reconciliation_policy_open",
}

#: The fact kinds that decide whether an operation may run again. The
#: validation facts do not: they say nothing about an effect.
#: ``execution_started`` does: a trailing in-flight fact with no later
#: terminal is an unknown effect, not an absent one.
_DECIDING_KINDS: Final[frozenset[str]] = frozenset(
    {
        "execution_started",
        "succeeded",
        "known_failed",
        "outcome_unknown",
        "reconciled",
    }
)


class OutcomeFactView(Protocol):
    """The part of a ledger fact the decision reads."""

    @property
    def kind(self) -> str:
        ...

    @property
    def logical_call_id(self) -> str:
        ...

    @property
    def business_operation_id(self) -> str | None:
        ...

    @property
    def outcome_code(self) -> str | None:
        ...

    @property
    def model_attempt(self) -> int:
        ...

    @property
    def provider_tool_call_id(self) -> str | None:
        ...

    @property
    def execution_attempt(self) -> int | None:
        ...


class OwnershipView(Protocol):
    """The Runtime identity a fenced ledger write commits under."""

    @property
    def thread_id(self) -> str:
        ...

    @property
    def run_id(self) -> str:
        ...

    @property
    def attempt_id(self) -> str:
        ...

    @property
    def attempt_generation(self) -> int:
        ...

    @property
    def thread_generation(self) -> int:
        ...


@dataclass(frozen=True)
class ExecutionDecision:
    """Why one business operation may (or may not) execute again.

    ``observed_kind`` is the fact kind the decision was read from, so a
    caller can say WHY it refused without the decision carrying a value.
    """

    kind: ExecutionDecisionKind
    observed_kind: str | None = None


def execution_decision(
    contract: ToolContract,
    facts: Sequence[OutcomeFactView],
    *,
    business_operation_id: str | None = None,
    logical_call_id: str | None = None,
) -> ExecutionDecision:
    """Decide whether one business operation may execute again.

    The facts are scoped to the stable business operation id when one is
    supplied, and to the logical call id otherwise. An operation with no
    prior outcome fact may execute. A known success is a duplicate
    delivery. A known failure re-executes only under a DECLARED retry
    policy; an absent policy does not permit a retry. An uncertain
    outcome stays unknown until a later ``reconciled`` fact proves it:
    with a declared reconciliation policy the operation needs that proof,
    and without one it can never be proven safe to replay.
    """
    if business_operation_id is not None:
        scoped = [
            fact
            for fact in facts
            if fact.business_operation_id == business_operation_id
        ]
    elif logical_call_id is not None:
        scoped = [fact for fact in facts if fact.logical_call_id == logical_call_id]
    else:
        raise ValueError(
            "business_operation_id or logical_call_id is required to scope "
            "an execution decision"
        )

    observed: OutcomeFactView | None = None
    for fact in scoped:
        if fact.kind in _DECIDING_KINDS:
            observed = fact
    if observed is None:
        return ExecutionDecision("execute")

    if observed.kind == "succeeded":
        return ExecutionDecision("duplicate_delivery", observed.kind)
    if observed.kind == "reconciled":
        # Only a proof that the effect did NOT happen may run again; a
        # proof that it did is as final as a success.
        if observed.outcome_code == "proven_not_applied":
            return ExecutionDecision("execute", observed.kind)
        return ExecutionDecision("duplicate_delivery", observed.kind)
    if observed.kind == "known_failed":
        if contract.retry.status == "declared":
            return ExecutionDecision("execute", observed.kind)
        return ExecutionDecision("retry_not_permitted", observed.kind)
    # outcome_unknown (or a trailing execution_started): the effect may
    # already have happened.
    if contract.reconciliation.status == "declared":
        return ExecutionDecision("reconciliation_required", observed.kind)
    return ExecutionDecision("reconciliation_policy_open", observed.kind)


def refusal_code(decision: ExecutionDecision) -> str | None:
    """The fixed ToolMessage code for a blocked decision, ``None`` to run."""
    if decision.kind == "execute":
        return None
    return REFUSAL_CODES[decision.kind]


#: Why a reconciliation write was refused.
ReconciliationRefusal = Literal[
    "policy_not_declared",
    "business_operation_missing",
    "business_operation_not_found",
]


class ReconciliationRefused(Exception):
    """A reconciliation this contract or identity does not permit."""

    def __init__(self, reason: ReconciliationRefusal) -> None:
        super().__init__(reason)
        self.reason = reason


def record_reconciliation(
    ledger: ToolOutcomeLedgerPort,
    facts: Sequence[OutcomeFactView],
    *,
    contract: ToolContract,
    business_operation_id: str | None,
    resolution_code: ResolutionCode,
    ownership: OwnershipView,
) -> int:
    """Append the one ``reconciled`` fact that resolves an unknown outcome.

    The write goes through the ledger's fenced ``record`` path, so it
    commits only under the worker attempt that still owns the thread, and
    a repeated delivery adds no second row. The fact carries the
    resolution code and the operation identity, never a value.
    """
    if contract.reconciliation.status != "declared":
        raise ReconciliationRefused("policy_not_declared")
    if not business_operation_id:
        raise ReconciliationRefused("business_operation_missing")
    matching = [
        fact
        for fact in facts
        if fact.business_operation_id == business_operation_id
    ]
    if not matching:
        raise ReconciliationRefused("business_operation_not_found")
    latest = matching[-1]
    observation = ToolOutcomeObservation(
        logical_call_id=latest.logical_call_id,
        tool_name=contract.name,
        model_attempt=latest.model_attempt,
        kind="reconciled",
        provider_tool_call_id=latest.provider_tool_call_id,
        execution_attempt=latest.execution_attempt,
        business_operation_id=business_operation_id,
        outcome_code=resolution_code,
    )
    return ledger.record(
        [observation],
        thread_id=ownership.thread_id,
        run_id=ownership.run_id,
        attempt_id=ownership.attempt_id,
        attempt_generation=ownership.attempt_generation,
        thread_generation=ownership.thread_generation,
    )
