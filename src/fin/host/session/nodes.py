"""The SESSION capability's graph-node behaviour.

The SESSION capability owns the ``compaction_gate`` node: the gate that
compacts the model-facing history before every LLM call (ADR 0044). The
host-graph composition root owns the topology and passes the gate's
dependencies in explicitly — this module owns the behaviour, not the
wiring.

The node body is a module-level function; its dependencies arrive
through the frozen :class:`SessionNodeDeps` bundle, so the composition
root never reaches into the capability's internals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langchain_core.messages import BaseMessage

from fin.host.agent_instructions import messages_for_model
from fin.host.session.compaction import CompactionInputError
from fin.host.session.session_compaction import (
    OccupancyCompactor,
    SessionSummaryError,
    count_tokens_approximately,
)
from fin.host.state import AgentState


class CompactionGateNode(Protocol):
    """The node shape LangGraph calls: ``(state)``.

    A protocol rather than a bare ``Callable``: LangGraph passes
    ``state`` by name, and a plain callable type would lose the
    parameter name, so the composition root's ``add_node`` call would
    not type-check.
    """

    def __call__(self, state: AgentState) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SessionNodeDeps:
    """The dependencies the SESSION nodes need from the composition root.

    Every field is a value or a callable the composition root already
    holds; nothing here is resolved inside the capability.
    """

    compactor: OccupancyCompactor
    enable_choice_window: bool
    memory_enabled: bool
    visual_faces_factory: Callable[[str, Any], Any] | None
    grounded_schema_tokens: int
    clarify_schema_tokens: int
    read_profile_text: Callable[[], str]
    with_turn_attachments: Callable[
        [AgentState, list[BaseMessage]], list[BaseMessage]
    ]
    clarify_missing_slots: Callable[[AgentState], list[str]]
    normalize_track: Callable[[object], str | None]
    assigned_retrieve_mode: Callable[[Any], Any]
    logger: logging.Logger


def _compaction_overhead_tokens(
    state: Mapping[str, Any], deps: SessionNodeDeps
) -> int:
    """Measure the next model call's transient messages and schemas."""
    raw_track = state.get("track", "dialogue")
    normalized_track = deps.normalize_track(raw_track)
    if normalized_track in {"dialogue", "clarify", "off_topic"}:
        track = normalized_track
    elif "track" not in state:
        track = "dialogue"
    else:
        track = "grounded"

    options: dict[str, Any] = {"user_profile": deps.read_profile_text()}
    schema_tokens = 0
    if track == "grounded":
        options.update(
            memory_on=deps.memory_enabled,
            visual_lane_on=deps.visual_faces_factory is not None,
            search_mode=deps.assigned_retrieve_mode(state),
        )
        schema_tokens = deps.grounded_schema_tokens
    elif track == "clarify":
        options["missing_slots"] = deps.clarify_missing_slots(
            cast(AgentState, state)
        )
        schema_tokens = deps.clarify_schema_tokens

    messages = list(state.get("messages") or [])
    model_messages = deps.with_turn_attachments(
        cast(AgentState, state),
        messages_for_model(messages, track=track, **options),
    )
    injected_tokens = max(
        0,
        count_tokens_approximately(model_messages)
        - count_tokens_approximately(messages),
    )
    return injected_tokens + schema_tokens


def _clarify_window_pending(state: AgentState, deps: SessionNodeDeps) -> bool:
    """True while a grounded turn still waits on the 档 choice window
    (T2; reused by the T4 post-propose route so the two window lanes
    cannot drift).

    The cascade itself no longer emits ``clarify`` (ADR 0104 — a
    routing residual takes the recorded default), so this is TRUE
    only for a LEGACY receipt: a window an earlier build left
    pending on the checkpoint.
    """
    audit = state.get("search_mode_decision") or {}
    return bool(
        deps.enable_choice_window
        and deps.normalize_track(state.get("track")) == "grounded"
        and audit.get("decision_tier") == "clarify"
        and audit.get("options")
    )


def build_compaction_overhead_tokens(
    deps: SessionNodeDeps,
) -> Callable[[Mapping[str, Any]], int]:
    """Bind the overhead measurement to the dependency bundle."""

    def compaction_overhead_tokens(state: Mapping[str, Any]) -> int:
        return _compaction_overhead_tokens(state, deps)

    return compaction_overhead_tokens


def build_clarify_window_pending(
    deps: SessionNodeDeps,
) -> Callable[[AgentState], bool]:
    """Bind the pending-window predicate to the dependency bundle."""

    def clarify_window_pending(state: AgentState) -> bool:
        return _clarify_window_pending(state, deps)

    return clarify_window_pending


def build_compaction_gate_node(
    deps: SessionNodeDeps,
) -> CompactionGateNode:
    """Build the ``compaction_gate`` node the host graph registers."""

    def compaction_gate_node(state: AgentState) -> dict[str, Any]:
        """Compact before the next LLM call (ADR 0044).

        The gate runs on EVERY entry path that would otherwise call
        the model: ``tools -> gate``, ``issuer_join -> gate``,
        ``from_classify -> gate``, ``from_issuer_fanout (fallback)
        -> gate``, and ``from_gate (bounce) -> gate``. It is NOT
        nested inside ``model_node`` (separate graph node).

        On a summary failure, the typed :class:`SessionSummaryError`
        is logged and the node returns ``{}`` so the operator turn
        continues un-compacted (ADR 0044: typed, not silent empty
        catch; must not refuse the operator turn). A malformed
        snapshot (``CompactionInputError`` from ``copy_snapshot``)
        is handled the same way — logged and skipped, never a
        blown-up run.
        """
        messages = list(state.get("messages") or [])
        snapshots = list(state.get("status_snapshots") or [])
        overhead_tokens = _compaction_overhead_tokens(state, deps)
        try:
            update = deps.compactor.compact_update(
                messages,
                status_snapshots=snapshots,
                overhead_tokens=overhead_tokens,
            )
        except (SessionSummaryError, CompactionInputError) as exc:
            deps.logger.warning("session compaction skipped: %s", exc)
            return {}
        if not update:
            return {}
        if update[1].additional_kwargs.get(
            "fin_compaction_summary_truncated"
        ):
            deps.logger.warning(
                "session compaction summary shortened to satisfy the 40%% floor"
            )
        # ``compact_update`` returns ``[RemoveMessage(REMOVE_ALL),
        # summary, *kept]`` so the ``add_messages`` reducer shrinks
        # the checkpointer. We pass the SAME ``messages`` list back
        # to the channel so the rebuild pattern actually replaces
        # history rather than appending duplicates.
        return {"messages": update}

    return compaction_gate_node
