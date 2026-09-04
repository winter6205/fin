"""Token-occupancy based session compaction (ADR 0044 / plan T1).

Replaces plan-default message-count windows: ``count_tokens_approximately``
on the model-facing payload vs a single operator-overridable session
budget (default ``256_000`` estimated tokens). At >= 90% occupancy the
host summarizes the OLDEST COMPLETE tool pairs (never splitting a
pair), writes the summary plus the latest **status snapshot** slots,
``RemoveMessage``s the evicted ids from the checkpointer, and stops
near 40% occupancy. Trim-without-delete is NOT compaction.

Why a separate module from ``fin.host.compaction``: the existing
``SessionCompactor`` ships a sliding-window message-count API that
ADR 0044 explicitly rejects ("looks unlike a context budget"). Its
helpers (``render_summary_message``, ``copy_snapshot``,
``extract_existing_compaction``, ``snapshot_text``,
``default_summary`` — public names, Std-M3) are reused via import —
same render, same marker, same snapshot extraction — so a
compaction written by the gate is structurally indistinguishable
from one written by the sliding-window API.

The summarizer is an injectable seam. ``None`` means "use the v1
chat model" — the same ``model`` object ``compile_graph`` received.
This mirrors the ``SummaryModel`` Protocol DI shape in
``fin.host.compaction``. Summary failure is typed
(:class:`SessionSummaryError`) and the gate node catches it so the
operator turn continues un-compacted; this module raises (the graph
layer decides to swallow).
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from fin.host.compaction import (
    DEFAULT_MAX_SUMMARY_CHARS,
    copy_snapshot,
    default_summary,
    extract_existing_compaction,
    render_summary_message,
    response_text,
    snapshot_text,
)

# Session-compaction summary bound: the same default cap the
# sliding-window API applies (Spec-M2b: repeated compactions cannot
# grow the summary unboundedly). ``DEFAULT_MAX_SUMMARY_CHARS`` is
# the practical per-compaction cap; ``MAX_SUMMARY_CHARS`` (1M) is
# only the sliding-window config ceiling and would not bound
# anything in practice.
SUMMARY_MAX_CHARS: int = DEFAULT_MAX_SUMMARY_CHARS


def summary_max_chars() -> int:
    """Return the summary text bound applied by this module.

    Exposed as a function so tests can import the bound without
    reaching into the constant's module (and so a future operator
    seam has one reader to change).
    """
    return SUMMARY_MAX_CHARS


def _bounded_text(text: str, limit: int) -> str:
    """Module-local summary bound (Spec-M2b). Same truncation shape
    as ``fin.host.compaction._bounded_text`` but kept local so this
    module does not import an underscore-private helper."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ----- the single session budget (ADR 0044: one constant) -----


SESSION_TOKEN_BUDGET: int = 256_000
"""One canonical session budget, in estimated tokens.

ADR 0044: the budget is a SINGLE constant, and the operator may
override it later (the override seam is the :func:`session_token_budget`
context manager below — NOT this attribute; tests assert the constant
itself stays put). Characters per token is 4, the v1 estimator's
default — see :func:`count_tokens_approximately`.
"""

CHARS_PER_TOKEN: int = 4

# Occupancy thresholds. They are private constants (no UI surface;
# plan T1 contract is just "at >= 90%" and "near 40%"). One copy,
# named clearly, so the gate has exactly two control points.
_OCCUPANCY_TRIGGER: float = 0.90
_OCCUPANCY_FLOOR: float = 0.40


# ----- operator-facing override seam -----


_SESSION_BUDGET: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_fin_session_budget", default=None
)


def session_budget_tokens() -> int:
    """Read the active session budget (override aware).

    Default is the LIVE module attribute :data:`SESSION_TOKEN_BUDGET`
    (Spec-M1: single source of truth is the constant — an operator
    editing it takes effect without restarting the ``ContextVar``).
    Inside a :func:`session_token_budget` block this returns the
    override instead.
    """
    override = _SESSION_BUDGET.get()
    if override is not None:
        return override
    return SESSION_TOKEN_BUDGET


@contextmanager
def session_token_budget(budget: int):
    """Override the session budget for the scope of this block.

    Used by tests (``session_token_budget(1_600)``) so a small seeded
    fixture crosses the 90% trigger. The constant
    :data:`SESSION_TOKEN_BUDGET` is unchanged; only the
    ``ContextVar``-backed reader sees the override. Propagates through
    ``graph.invoke`` (verified for langgraph 1.2.x — sync nodes run on
    the invoking thread).

    ``budget`` must be a positive integer.
    """
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise ValueError(f"session_token_budget must be a positive int, got {budget!r}")
    token = _SESSION_BUDGET.set(budget)
    try:
        yield
    finally:
        _SESSION_BUDGET.reset(token)


# ----- typed failure -----


class SessionSummaryError(Exception):
    """Typed session-summary failure surfaced by the gate.

    Raised by :meth:`OccupancyCompactor.compact_update` when the
    injected summarizer fails (returns empty, raises, or is
    malformed). The graph layer catches this and lets the operator
    turn continue un-compacted — typed so callers cannot accidentally
    ``except Exception:`` over the seam.
    """


# ----- summarizer seam -----


class SummaryModel(Protocol):
    """Synchronous injected summarizer.

    The default is the v1 chat model — the same ``model`` object
    ``compile_graph`` received. Tests inject a stub.
    """

    def invoke(self, messages: list[BaseMessage]) -> Any:
        """Return the summary response for the supplied evicted messages."""


def _response_text(response: Any) -> str:
    return response_text(response, error=SessionSummaryError)


# ----- token estimator -----


def count_tokens_approximately(
    messages: Iterable[BaseMessage] | None,
) -> int:
    """Sum of ``len(text) // CHARS_PER_TOKEN`` over each message.

    Plain chars/4 heuristic — the v1 ADR spec is "occupancy vs the
    session budget", not "occupancy vs the vendor's tokenizer".
    LangChain ships its own ``count_tokens_approximately`` with a
    per-message floor and tool-call overhead; that is the wrong
    estimator here (the contract pins an exact chars/4 number — the
    unit tests assert ``[HumanMessage("a"*400)]`` -> 100, no floor).
    """
    total = 0
    for message in messages or []:
        content = getattr(message, "content", "")
        text = content if isinstance(content, str) else str(content)
        total += len(text) // CHARS_PER_TOKEN
    return total


# ----- the compactor -----


class OccupancyCompactor:
    """Apply occupancy-based compaction to a model-facing history.

    Stateless across calls (no lock, no buffered summary) — the
    summary state lives in the checkpointer (the leading
    ``SystemMessage`` carries ``fin_compaction_summary`` plus
    ``fin_compaction_summary_text`` and ``status_snapshot``), so the
    compactor rehydrates it via ``_extract_existing_compaction``.

    Greedy newest-first unit inclusion vs the 40% floor:
    ``compact_update`` slices the history into whole units (a
    standalone message, or an AIMessage + its matching ToolMessages —
    never split), then admits units NEWEST-first until the next unit
    would push the kept tail past the floor. The remaining OLDEST
    units are evicted and summarized.

    A summary model failure (raise or empty result) is wrapped in
    :class:`SessionSummaryError`; the graph layer is the one that
    catches and decides to continue un-compacted.
    """

    def __init__(
        self,
        *,
        summarizer: SummaryModel | Callable[[list[BaseMessage]], Any] | None = None,
        trigger_ratio: float = _OCCUPANCY_TRIGGER,
        floor_ratio: float = _OCCUPANCY_FLOOR,
    ) -> None:
        if not (0.0 < trigger_ratio <= 1.0):
            raise ValueError(f"trigger_ratio must be in (0, 1], got {trigger_ratio!r}")
        if not (0.0 < floor_ratio <= trigger_ratio):
            raise ValueError(
                f"floor_ratio must be in (0, trigger], got {floor_ratio!r}"
            )
        self.summarizer = summarizer
        self.trigger_ratio = trigger_ratio
        self.floor_ratio = floor_ratio

    # ----- occupancy -----

    def occupancy(
        self,
        messages: Iterable[BaseMessage] | None,
        *,
        overhead_tokens: int = 0,
    ) -> float:
        """Fraction of the session budget the payload would occupy.

        Includes ``overhead_tokens`` (host prompt, tool schemas, ...)
        so the caller can assert "model-facing occupancy" not just
        "message-occupancy". Always in [0, 1] given a positive
        budget.
        """
        budget = max(1, session_budget_tokens())
        tokens = count_tokens_approximately(messages) + max(0, int(overhead_tokens))
        return min(1.0, tokens / budget)

    # ----- eviction unit walker -----

    @staticmethod
    def _pair_units(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
        """Slice the history into eviction units preserving pair integrity.

        Oldest-first ordering. A unit is either:

        * an AIMessage with tool_calls plus the immediately following
          ``ToolMessage`` s for those calls (one unit, never split);
        * or a single message that is not an AIMessage with tool_calls.

        Walks forward: AIMessage at index ``i``, its ToolMessages at
        indices ``i + 1`` onward (langgraph's ``add_messages`` order
        puts the AIMessage BEFORE its tool results).
        """
        units: list[list[BaseMessage]] = []
        index = 0
        while index < len(messages):
            current = messages[index]
            if isinstance(current, AIMessage) and current.tool_calls:
                call_ids = {call["id"] for call in current.tool_calls}
                unit: list[BaseMessage] = [current]
                index += 1
                while (
                    index < len(messages)
                    and isinstance(messages[index], ToolMessage)
                    and messages[index].tool_call_id in call_ids
                ):
                    unit.append(messages[index])
                    index += 1
                units.append(unit)
            else:
                units.append([current])
                index += 1
        return units

    def _greedy_keep(
        self,
        units: Sequence[Sequence[BaseMessage]],
        floor_tokens: int,
    ) -> tuple[Sequence[BaseMessage], Sequence[BaseMessage]]:
        """Pick the newest suffix of units whose total tokens <= floor_tokens.

        Walks units NEWEST-FIRST, accumulating tokens; the kept suffix
        is the prefix of that walk (the oldest unit not yet admitted
        is where the split happens). The FIRST unit is always admitted
        even if it alone exceeds the floor — the newest exchange must
        survive compaction, or the current turn loses its own context.

        Returns ``(evicted, kept)`` as the original-order slices.
        """
        total = 0
        kept_count = 0  # number of NEWEST units admitted
        for unit in reversed(units):
            unit_tokens = count_tokens_approximately(unit)
            if kept_count > 0 and total + unit_tokens > floor_tokens:
                break
            total += unit_tokens
            kept_count += 1
        kept: Sequence[BaseMessage] = [
            message
            for unit in units[-kept_count:]
            for message in unit
        ]
        evicted: Sequence[BaseMessage] = [
            message
            for unit in units[: len(units) - kept_count]
            for message in unit
        ]
        return evicted, kept

    # ----- summarizer wiring -----

    def _summarize(
        self,
        evicted: Sequence[BaseMessage],
        previous_summary: str,
        snapshot: Mapping[str, Any],
    ) -> str:
        """Run the injected summarizer over the evicted prefix.

        Mirrors ``fin.host.compaction.SessionCompactor._make_summary``:
        the prompt names the previous summary (incremental
        summarization) and the active snapshot slots. Failures are
        wrapped in :class:`SessionSummaryError`; empty output is a
        failure too (no silent empty summary).
        """
        if self.summarizer is None:
            return default_summary(evicted, previous_summary)
        prompt: list[BaseMessage] = [
            SystemMessage(
                content=(
                    "Create a concise incremental conversation summary. "
                    "The messages after this instruction are the newly "
                    "evicted history; do not summarize recent messages. "
                    f"Previous summary: {previous_summary or '(none)'}."
                )
            )
        ]
        if any(snapshot.get(key) for key in ("company", "period", "document_ids")):
            prompt[0] = SystemMessage(
                content=(
                    f"{prompt[0].content} Active status snapshot: "
                    f"{snapshot_text(snapshot)}"
                )
            )
        prompt.extend(evicted)
        try:
            if hasattr(self.summarizer, "invoke"):
                response = self.summarizer.invoke(prompt)  # type: ignore[union-attr]
            elif callable(self.summarizer):
                response = self.summarizer(prompt)
            else:
                raise SessionSummaryError(
                    "summarizer must be callable or provide invoke"
                )
        except SessionSummaryError:
            raise
        except Exception as exc:  # noqa: BLE001 - typed boundary
            raise SessionSummaryError("could not create session summary") from exc
        text = response_text(response, error=SessionSummaryError)
        if not text:
            raise SessionSummaryError("summary model returned empty text")
        # Spec-M2b: bound the summary text — repeated compactions
        # must not grow the summary unboundedly (same pattern as
        # ``fin.host.compaction``'s per-compactor ``_bounded_text``).
        return _bounded_text(text, SUMMARY_MAX_CHARS)

    # ----- public entry point -----

    def compact_update(
        self,
        messages: Sequence[BaseMessage] | None,
        *,
        status_snapshots: Sequence[Mapping[str, Any]] | None = None,
        force: bool = False,
    ) -> list[BaseMessage]:
        """Compact ``messages`` if and only if occupancy crosses the trigger.

        ``force=True`` (plan T2: the ``/compact`` command) runs the
        SAME eviction / summary / write-back path WITHOUT waiting for
        the 90% trigger — the operator asked, so a thread already
        under 90% may still compact. The 40% floor and the pair-unit
        walker are unchanged: a payload with nothing evictable still
        returns ``[]`` (the caller turns that into a no-op receipt).

        Returns the ``messages`` update to feed to LangGraph's
        ``add_messages`` reducer — ``[]`` means "no compaction". When
        compaction fires, the update is::

            [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *kept]

        so the reducer shrinks the checkpointer to ``[summary, *kept]``
        (see ADR 0044: trim-without-delete is NOT compaction).

        The summary carries the latest status-snapshot slots via
        :func:`render_summary_message` and :func:`copy_snapshot`. A
        leading prior summary is carried forward
        (:func:`extract_existing_compaction`) so repeated compactions
        summarize incrementally, never re-summarizing kept messages.
        """
        model_facing = list(messages or [])
        history, previous_summary, embedded_snapshot = (
            extract_existing_compaction(model_facing)
        )
        snapshot = copy_snapshot(status_snapshots)
        if status_snapshots is None and embedded_snapshot:
            snapshot = embedded_snapshot

        budget = max(1, session_budget_tokens())
        trigger_tokens = budget * self.trigger_ratio
        floor_tokens = budget * self.floor_ratio

        # Spec-M2a: the trigger measurement counts the FULL
        # model-facing payload INCLUDING the existing summary — the
        # model will actually see it, so a huge prior summary is
        # real occupancy (measuring only the stripped history would
        # hide the weight and never trigger).
        current_tokens = count_tokens_approximately(model_facing)
        if not force and current_tokens < trigger_tokens:
            return []

        # Spec-M2a (floor side): the previous summary rides INTO the
        # next summary (incremental summarization) and stays in the
        # model-facing payload after compaction, so the kept-tail
        # floor must reserve its weight. Without this, a thread whose
        # history is mostly one huge summary keeps every (tiny)
        # history unit, evicts nothing, and the compaction silently
        # no-ops while the summary keeps its weight.
        summary_tokens = len(previous_summary) // CHARS_PER_TOKEN
        floor_tokens = max(0, floor_tokens - summary_tokens)

        units = self._pair_units(history)
        evicted, kept = self._greedy_keep(units, floor_tokens)
        if not evicted:
            # Nothing to evict (every unit is past the floor) — leave
            # the history alone. This is rare (a long single unit
            # dominates) but keeps the gate from corrupting a valid
            # tool-pair history.
            return []
        summary_text = self._summarize(evicted, previous_summary, snapshot)
        summary_message = render_summary_message(summary_text, snapshot)
        return [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_message, *kept]


__all__ = [
    "CHARS_PER_TOKEN",
    "SUMMARY_MAX_CHARS",
    "OccupancyCompactor",
    "SESSION_TOKEN_BUDGET",
    "SessionSummaryError",
    "SummaryModel",
    "count_tokens_approximately",
    "session_budget_tokens",
    "session_token_budget",
    "summary_max_chars",
]
