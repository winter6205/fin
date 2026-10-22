"""Attachment capability: the ``ensure_attachment_ready`` graph node.

The node body lives in the capability; the host-graph composition root
wires it and passes its dependencies explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from fin.host.degradation_notice import ATTACHMENT_PREPARATION_FAILED
from fin.host.state import AgentState


class PreparationOutcomeView(Protocol):
    """What the gate reads off ``request_preparation``'s answer.

    The service module's frozen ``PreparationRequest`` dataclass
    satisfies this structurally; the view exists so the node's
    consumption is typed and a violated contract raises ``AttributeError``
    at the read instead of degrading into the generic fallback refusal.
    """

    state: str
    code: str | None


class ResolvedAttachmentView(Protocol):
    """What the gate reads off ``resolve_for_turn``'s answer.

    Same consumer-side shape as :class:`PreparationOutcomeView` (the
    service's ``ResolvedAttachment`` satisfies it structurally).
    """

    filename: str
    text: str


class AttachmentPreparationServicePort(Protocol):
    """Consumer-side seam of the ``ensure_attachment_ready`` gate.

    Declared here (not imported from ``fin.host.attachment.attachment_prepare``)
    on purpose: the capability owns the node and must not couple to its own
    implementation module. The real service satisfies this shape
    structurally — its ``request_preparation`` / ``resolve_for_turn``
    carry exactly these call signatures.

    ``budgets`` is the deployed page-lane bound the visual ledger prices
    against; the real service always carries it (possibly ``None`` on a
    text-only deployment, which mints no ledger).
    """

    budgets: Any

    def request_preparation(
        self, thread_id: str, attachment_id: str
    ) -> PreparationOutcomeView: ...

    def resolve_for_turn(
        self, *, thread_id: str, attachment_id: str
    ) -> ResolvedAttachmentView: ...


@dataclass(frozen=True)
class AttachmentNodeDeps:
    """The ``ensure_attachment_ready`` node's explicit dependencies.

    ``attachment_service`` is the preparation service the gate drives.
    ``interrupt_kind`` is the pinned wire token the gate's interrupt
    payload carries; the composition root holds the single definition of
    that token and passes it in.
    """

    attachment_service: AttachmentPreparationServicePort | None
    interrupt_kind: str


def _attachment_resolve_race_errors() -> tuple[type[Exception], ...]:
    """The capability module's typed not-ready pair, caught by TYPE.

    Resolved lazily because the module graph forbids a top-level import:
    ``attachment_prepare`` reaches ``fin.host.compile`` (through
    ``attachment_budgets``/``attachment_vision``), which imports
    ``fin.host.graph``, which imports this module. ``resolve_for_turn``
    raises the pair when a row's derived data went unreadable after the
    gate's ready verdict; any other exception is a real bug and
    propagates.
    """
    from fin.host.attachment.attachment_prepare import (
        AttachmentNotFoundError,
        AttachmentNotReadyError,
    )

    return (AttachmentNotReadyError, AttachmentNotFoundError)


def build_ensure_attachment_ready_node(
    deps: AttachmentNodeDeps,
) -> Callable[..., dict[str, Any]]:
    """Build the ``ensure_attachment_ready`` node over ``deps``."""

    def ensure_attachment_ready_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        """Attachment preparation gate (T2f / ADR 0089).

        Reached on the grounded route between ``compaction_gate`` and
        the ``model`` leaf whenever an ``attachment_service`` is wired.
        Orchestration only — the attachment capability owns parse /
        vision / index execution (spec: "the node does not parse,
        render, call OCR/vision, or build an index itself").

        Durability shape (same as the choice window): ``interrupt``
        ends the stream generator, which releases the thread lock and
        leaves the wait in the checkpointer under the SAME run id;
        resume replays this node body, and because
        ``request_preparation`` is an idempotent join keyed by content
        version the replay starts no duplicate work. The resume VALUE
        is ignored — the host resumer (or a process restart) drives
        re-evaluation, not an operator pick.
        """
        # The node is added to the graph only under the
        # ``attachment_service is not None`` guard at the ``add_node``
        # site in the composition root; this pins that invariant for the
        # checker without adding a runtime branch.
        assert deps.attachment_service is not None
        items = [dict(item) for item in (state.get("attachments") or [])]
        if not any(str(item.get("attachment_id") or "") for item in items):
            # EXIT: a turn that names no eligible attachment never
            # touches the service (spec: preparation starts only for an
            # attachment eligible to a grounded turn).
            return {}
        configurable = dict((config or {}).get("configurable") or {})
        thread_id = str(configurable.get("thread_id") or "")
        while True:
            pending: list[str] = []
            for item in items:
                attachment_id = str(item.get("attachment_id") or "")
                if not attachment_id:
                    continue
                outcome = deps.attachment_service.request_preparation(
                    thread_id, attachment_id
                )
                # Direct attribute reads per the consumer-side Protocol:
                # a service violating the pinned outcome shape raises
                # ``AttributeError`` loudly instead of degrading into
                # the generic fallback refusal.
                outcome_state = str(outcome.state or "")
                code = outcome.code
                if outcome_state == "ready":
                    continue
                if outcome_state == "pending":
                    pending.append(attachment_id)
                    continue
                # EXIT failed / unknown / any out-of-vocabulary state:
                # a terminal failure outranks a sibling's pending —
                # waiting on a partially-failed eligible set would let
                # the model answer without a source the operator named.
                # The service's code rides ``status_reason`` verbatim
                # (spec: named failure); ``unknown`` with no code is the
                # unknown/stale-identity exit. T2f: a preparation the
                # service reports ``failed`` with no code (or an
                # out-of-vocabulary state) is still a typed exit —
                # ``ATTACHMENT_PREPARATION_FAILED`` (imported from the
                # leaf module shared with the search-surface fallback)
                # names the gate's own failure and never degrades to an
                # empty successful turn.
                if outcome_state == "failed":
                    reason = str(code) if code else ATTACHMENT_PREPARATION_FAILED
                elif outcome_state == "unknown":
                    reason = str(code) if code else "unknown_locator"
                else:
                    reason = ATTACHMENT_PREPARATION_FAILED
                return {
                    "messages": [],
                    "answer": "",
                    "citations": (),
                    "status": "refuse",
                    "status_reason": reason,
                }
            if not pending:
                # All ready. Rows that were ``uploaded`` at send time
                # carry ids without frozen text; (re)hydrate each
                # item's text through the service — the same
                # ``resolve_for_turn`` mechanism the send door uses
                # today — before the leaf reads the channel.
                hydrated: list[dict[str, Any]] = []
                changed = False
                for item in items:
                    attachment_id = str(item.get("attachment_id") or "")
                    if attachment_id and not str(item.get("text") or ""):
                        try:
                            resolved = deps.attachment_service.resolve_for_turn(
                                thread_id=thread_id,
                                attachment_id=attachment_id,
                            )
                        except Exception as exc:
                            # The send door's race guard moved into
                            # the node: derived data can go unreadable
                            # between the ready verdict and this read,
                            # and the resolver answers its typed
                            # not-ready pair. Re-ask and wait (the row
                            # joins the pending set; the loop head's
                            # idempotent ``request_preparation`` makes
                            # the next routing decision on resume) —
                            # never a bare crash, never an empty
                            # success. Any other error is a real bug
                            # and propagates: typed, never swallowed.
                            if not isinstance(
                                exc, _attachment_resolve_race_errors()
                            ):
                                raise
                            pending.append(attachment_id)
                            break
                        item = {
                            **item,
                            "filename": str(
                                resolved.filename
                                or item.get("filename")
                                or ""
                            ),
                            "text": str(resolved.text or ""),
                        }
                        changed = True
                    hydrated.append(item)
                if not pending:
                    # EXIT passthrough: with every row already carrying
                    # its text the channel needs no rewrite; omitting the
                    # key leaves it untouched.
                    return {"attachments": hydrated} if changed else {}
            interrupt(
                {
                    "kind": deps.interrupt_kind,
                    "attachment_ids": list(pending),
                }
            )

    return ensure_attachment_ready_node
