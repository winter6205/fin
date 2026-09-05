"""Dream: the dual-gated local merge of ``CONTEXT.md:45``.

Dream picks the *useful* rows out of the local working buffer and
hands them to a :class:`MemoryDreamBackend`. The gate has two halves
(``specs/cross-session-operator-memory.md`` SC17-SC22, ADR 0048):

* a 24h cooldown from the last success **or** skip (``DREAM_MIN_INTERVAL``);
* a threshold (active facts ≥ 2 AND (distinct ``thread_id`` ≥ 5 OR
  completed turns since last dream ≥ 6 OR new facts ≥ 3)).

Three deliberate absences (``ADR 0048`` 2026-09-06, supersedes the
earlier ``specs/memory-dream.md`` SC17/SC18):

* dream is **not** a graph node (``CONTEXT.md:46``) — the HTTP layer
  runs it after the SSE stream ends;
* with no backend configured (default ``null``) dream performs
  **zero** memory-MCP writes and stops at the buffer;
* the SSE routes do not unconditionally enqueue dream; the
  per-turn hook counts a turn, evaluates the gate, and only fires
  the merge when both halves open.

Failures are typed (:class:`MemoryDreamError` and its subclasses)
and never leave the foreground turn without a result: a backend
or summarizer raise is logged and swallowed by the per-turn
hook (``SC21``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, Sequence

from fin.host.memory_port import (
    DEFAULT_MEMORY_LIMIT,
    MEMORY_LIMIT_CAP,
    DreamInputError,
    DreamTransportError,
    MemoryDreamError,
    MemoryPort,
)
from fin.host.memory_store import (
    DreamCursor,
    DreamCursorStore,
    MemoryRow,
    MemoryStore,
)

logger = logging.getLogger(__name__)


DREAM_MIN_INTERVAL = timedelta(hours=24)
DREAM_MIN_THREADS = 5

LONGEVITY_DAYS = 7
TRANSPORT_RETENTION_DAYS = 7

SCORE_REPEATED_HITS = 3
SCORE_SINGLE_HIT = 1
SCORE_LONGEVITY = 2
SELECTION_THRESHOLD = 3

# Dual-gate thresholds (spec SC19). The 24h cooldown lives on the
# existing :class:`DreamCursor`; the per-thread / per-turn / new-fact
# counters live on :class:`DreamGateState`.
DREAM_MIN_ACTIVE_FACTS = 2
DREAM_MIN_TURNS_SINCE_DREAM = 6
DREAM_MIN_NEW_FACTS = 3


@dataclass(frozen=True)
class ScoredMemory:
    """One row plus the score and the reasons that produced it."""

    row: MemoryRow
    score: int
    reasons: tuple[str, ...]


class MemoryDreamScorer:
    """Rule-based usefulness scoring. Pure: no IO, no clock, no randomness.

    The weights are starting values, not calibrated ones — OQ2 exists
    because nobody has run these against real dialogue volume yet.
    """

    def score(
        self,
        rows: Sequence[MemoryRow],
        now: datetime,
    ) -> list[ScoredMemory]:
        """Score every row that is not superseded by a newer one."""
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise DreamInputError("rows must be a sequence of MemoryRow")

        superseded: set[str] = set()
        for row in rows:
            if not isinstance(row, MemoryRow):
                raise DreamInputError(
                    f"expected MemoryRow, got {type(row).__name__}"
                )
            if isinstance(row.hit_count, bool) or not isinstance(
                row.hit_count, int
            ):
                raise DreamInputError(
                    f"row {row.id!r} has a non-integer hit_count: "
                    f"{row.hit_count!r}"
                )
            target = row.metadata.get("supersedes")
            if target:
                superseded.add(str(target))

        scored: list[ScoredMemory] = []
        for row in rows:
            if row.id in superseded:
                continue
            score = 0
            reasons: list[str] = []
            if row.hit_count >= 2:
                score += SCORE_REPEATED_HITS
                reasons.append("repeated_hits")
            elif row.hit_count == 1:
                score += SCORE_SINGLE_HIT
                reasons.append("single_hit")
            if now - row.created_at >= timedelta(days=LONGEVITY_DAYS):
                score += SCORE_LONGEVITY
                reasons.append("longevity")
            scored.append(ScoredMemory(row, score, tuple(reasons)))
        return scored

    def select(
        self, scored: Sequence[ScoredMemory]
    ) -> list[ScoredMemory]:
        """Return the rows at or above the threshold, best first."""
        eligible = [item for item in scored if item.score >= SELECTION_THRESHOLD]
        eligible.sort(key=lambda item: (-item.score, item.row.created_at, item.row.id))
        return eligible


class DreamSummarizer(Protocol):
    """Optional condensation step. Off unless one is injected (SC13)."""

    def summarize(
        self, memories: Sequence[ScoredMemory]
    ) -> Sequence[ScoredMemory]:
        """Return the batch to transport, condensed or merged."""
        ...


class NullDreamSummarizer:
    """An explicit no-op summarizer: no model call, the batch passes through.

    Not the default — :class:`MemoryDream` defaults to no summarizer at
    all, which is the same outcome by a shorter path. This exists so a
    caller can name "condensation is off" instead of relying on absence.
    """

    def summarize(
        self, memories: Sequence[ScoredMemory]
    ) -> Sequence[ScoredMemory]:
        return list(memories)


class MemoryDreamBackend(Protocol):
    """Where dream transports the batch. Swapping MCP changes only this.

    With the null backend (the only wired kind after ADR 0048 2026-09-06),
    ``persist`` returns 0 — meaning "stops at the buffer, no MCP write"
    (SC22).
    """

    def persist(self, memories: Sequence[dict[str, object]]) -> int:
        """Store ``memories`` and report how many were stored."""
        ...


class NullDreamBackend:
    """The default backend: no durable target, so dream stops at the buffer.

    Returning ``0`` is the contract for "no MCP write happened"; the
    merge still records the window so the cursor advances and the
    per-turn counters reset.
    """

    def persist(self, memories: Sequence[dict[str, object]]) -> int:
        return 0


@dataclass(frozen=True)
class DreamGate:
    """The dual gate that decides whether a dream window opens.

    Spec SC17–SC19 / ADR 0048. The cooldown (``min_interval``) lives on
    the existing :class:`DreamCursor`; the per-fact / per-thread /
    per-turn / new-fact thresholds live here. SC18 short-circuits the
    fact floor BEFORE the cooldown check so ``facts < 2`` never
    refreshes the 24h basis.
    """

    min_interval: timedelta = DREAM_MIN_INTERVAL
    min_active_facts: int = DREAM_MIN_ACTIVE_FACTS
    min_distinct_threads: int = DREAM_MIN_THREADS
    min_turns_since_dream: int = DREAM_MIN_TURNS_SINCE_DREAM
    min_new_facts: int = DREAM_MIN_NEW_FACTS


@dataclass(frozen=True)
class DreamGateState:
    """The per-turn counter + active-facts watermark.

    The cursor's ``last_dream_at`` is the cooldown basis; this state is
    the threshold basis. Lives in process memory for tests and in the
    ``PostgresDreamGateStateStore`` (out of scope for this slice — the
    in-process store keeps the contract injectable).
    """

    turns_since_dream: int
    last_facts_watermark: int


class DreamGateStateStore(Protocol):
    """The threshold counters behind the per-turn dream hook."""

    def load(self) -> DreamGateState:
        """Return the current threshold state."""
        ...

    def record_turn(self, *, active_facts: int) -> DreamGateState:
        """Increment the turn counter; leave the watermark alone.

        ``active_facts`` is observed so a caller can recompute the
        new-fact delta at evaluation time without re-reading the
        store on every hook.
        """
        ...

    def reset(self, *, active_facts: int) -> None:
        """Clear the turn counter and pin the watermark to ``active_facts``.

        Called by :class:`MemoryDream` after a successful merge so
        the next window starts a fresh cycle.
        """
        ...


class InProcessDreamGateStateStore:
    """Process-local gate state. Loses its counters on restart."""

    def __init__(self) -> None:
        self._state = DreamGateState(
            turns_since_dream=0, last_facts_watermark=0
        )
        self._lock = threading.RLock()

    def load(self) -> DreamGateState:
        with self._lock:
            return self._state

    def record_turn(self, *, active_facts: int) -> DreamGateState:
        if isinstance(active_facts, bool) or not isinstance(active_facts, int):
            raise DreamInputError("active_facts must be an integer")
        if active_facts < 0:
            raise DreamInputError("active_facts must be non-negative")
        with self._lock:
            self._state = DreamGateState(
                turns_since_dream=self._state.turns_since_dream + 1,
                last_facts_watermark=self._state.last_facts_watermark,
            )
            return self._state

    def reset(self, *, active_facts: int) -> None:
        if isinstance(active_facts, bool) or not isinstance(active_facts, int):
            raise DreamInputError("active_facts must be an integer")
        if active_facts < 0:
            raise DreamInputError("active_facts must be non-negative")
        with self._lock:
            self._state = DreamGateState(
                turns_since_dream=0, last_facts_watermark=active_facts
            )

    def _reset_for_test(self, *, turns: int, watermark: int) -> None:
        """Test-only seeding. Production code uses ``reset`` instead."""
        with self._lock:
            self._state = DreamGateState(
                turns_since_dream=turns, last_facts_watermark=watermark
            )


def _new_facts_since_dream(
    state: DreamGateState, *, active_facts: int
) -> int:
    """Return ``max(0, active_facts - watermark)``.

    The watermark is pinned at the end of the previous successful
    merge (or 0 initially), so this is the number of active facts
    that have appeared since the last dream — exactly what SC19's
    "new facts ≥ 3" branch counts.
    """
    return max(0, active_facts - state.last_facts_watermark)


def evaluate_dream_gate(
    *,
    gate: DreamGate,
    cursor: DreamCursor,
    state: DreamGateState,
    active_facts: int,
    distinct_threads: int,
    now: datetime,
) -> tuple[bool, str | None]:
    """Return ``(open?, skip_reason)`` for the dual gate (pure function).

    SC18 labels the result ``facts_floor`` whenever active facts are
    below the floor, even if we are also inside the cooldown window:
    the label drives the caller to skip the cursor advance, which is
    how "facts<2 does not refresh 24h" is enforced. SC17's
    ``cooldown`` label fires only when facts are at the floor and
    the cooldown check would otherwise have advanced the basis —
    here both labels mean "no cursor advance", so the caller treats
    them uniformly. ``threshold`` advances ``last_dream_at`` because
    a threshold-not-met evaluation still consumes the 24h basis.
    """
    # SC18: facts < 2 → 0 LLM calls and the 24h basis is not refreshed.
    # The label always wins so the caller knows not to advance.
    if active_facts < gate.min_active_facts:
        return False, "facts_floor"
    # SC17: within 24h of the last success/skip → cooldown skip.
    if cursor.last_dream_at is not None and (
        now - cursor.last_dream_at < gate.min_interval
    ):
        return False, "cooldown"
    # SC19: threshold — facts ≥ 2 AND (threads ≥ 5 OR turns ≥ 6
    # OR new facts ≥ 3).
    threshold_open = (
        distinct_threads >= gate.min_distinct_threads
        or state.turns_since_dream >= gate.min_turns_since_dream
        or _new_facts_since_dream(state, active_facts=active_facts)
        >= gate.min_new_facts
    )
    if not threshold_open:
        return False, "threshold"
    return True, None


def _resolve_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise DreamInputError("limit must be an integer")
    if limit <= 0:
        return DEFAULT_MEMORY_LIMIT
    return min(limit, MEMORY_LIMIT_CAP)


class MemoryDream:
    """Runs one dream window over a buffer's rows and cursor.

    The constructor takes an optional ``gate_state`` so callers that
    want to enforce the per-turn counters (the HTTP wiring does, via
    :func:`run_dream_after_turn`) can plug the in-process store in.
    Callers that drive :meth:`run` directly without a per-turn hook
    (legacy tests) still work — ``gate_state=None`` means "no
    per-turn counts yet" and the threshold branch degenerates to
    threads-only.
    """

    def __init__(
        self,
        store: MemoryStore,
        cursors: DreamCursorStore,
        gate_state: DreamGateStateStore | None = None,
        *,
        backend: MemoryDreamBackend | None = None,
        summarizer: DreamSummarizer | None = None,
        gate: DreamGate | None = None,
        scorer: MemoryDreamScorer | None = None,
    ) -> None:
        self._store = store
        self._cursors = cursors
        self._gate_state = gate_state
        self._backend: MemoryDreamBackend = (
            backend if backend is not None else NullDreamBackend()
        )
        self._summarizer = summarizer
        self._gate = gate if gate is not None else DreamGate()
        self._scorer = scorer if scorer is not None else MemoryDreamScorer()

    def run(
        self,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Evaluate the dual gate; on open, run the local merge.

        Raises a :class:`MemoryDreamError` subclass on failure. The
        caller that owns the foreground turn is responsible for
        catching it — :func:`run_dream_after_turn` does, so a broken
        backend can never fail an operator's turn (SC21).
        """
        at = now if now is not None else datetime.now(timezone.utc)
        resolved_limit = _resolve_limit(limit)

        cursor = self._cursors.load()
        state = (
            self._gate_state.load()
            if self._gate_state is not None
            else DreamGateState(turns_since_dream=0, last_facts_watermark=0)
        )

        rows = self._store.rows()
        seen = frozenset(
            row.thread_id for row in rows if row.thread_id
        )
        new_threads = seen - cursor.seen_thread_ids
        missing = sum(1 for row in rows if row.thread_id is None)
        if missing:
            logger.warning(
                "memory dream gate: %d row(s) carry no thread_id and are "
                "not counted toward the conversation gate",
                missing,
            )

        active_facts = sum(1 for row in rows if row.status == "active")

        open_, skip_reason = evaluate_dream_gate(
            gate=self._gate,
            cursor=cursor,
            state=state,
            active_facts=active_facts,
            distinct_threads=len(new_threads),
            now=at,
        )

        if not open_:
            if skip_reason == "facts_floor":
                # SC18: facts < 2 → no LLM, no cursor advance.
                return {
                    "status": "skipped",
                    "reason": "facts_floor",
                    "transported": 0,
                }
            # cooldown / threshold: the basis of the 24h gate is
            # "last success or skip" — claim advances last_dream_at.
            if not self._cursors.claim(
                at=at,
                min_interval=self._gate.min_interval,
                seen_thread_ids=seen,
                last_status="skipped",
            ):
                return {
                    "status": "skipped",
                    "reason": "claimed",
                    "transported": 0,
                }
            return {
                "status": "skipped",
                "reason": skip_reason,
                "transported": 0,
            }

        # Gate open: claim the window, then merge. Concurrent callers
        # lose the race via the CAS in ``claim`` (SC22/concurrent).
        if not self._cursors.claim(
            at=at,
            min_interval=self._gate.min_interval,
            seen_thread_ids=seen,
            last_status="running",
        ):
            return {
                "status": "skipped",
                "reason": "claimed",
                "transported": 0,
            }

        try:
            return self._consolidate(
                rows,
                at=at,
                limit=resolved_limit,
                seen=seen,
                active_facts=active_facts,
            )
        except Exception:
            # ``claim`` already consumed the window, so the cursor must
            # stop claiming to be mid-run. A pool outage or an unusable
            # row raises OUTSIDE the MemoryDreamError family, and a
            # cursor stuck at "running" would misreport why dream went
            # quiet for the next interval. Choice: failure does NOT
            # count as success — the next evaluation within 24h stays
            # in cooldown.
            self._record_failure(at=at, seen=seen)
            raise

    def _record_failure(
        self, *, at: datetime, seen: frozenset[str]
    ) -> None:
        """Mark the claimed window failed without masking the cause."""
        try:
            self._cursors.advance(
                at=at, seen_thread_ids=seen, last_status="error"
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "memory dream failed and the cursor could not record it"
            )

    def _consolidate(
        self,
        rows: Sequence[MemoryRow],
        *,
        at: datetime,
        limit: int,
        seen: frozenset[str],
        active_facts: int,
    ) -> dict[str, object]:
        active = [row for row in rows if row.status == "active"]
        selected = self._scorer.select(self._scorer.score(active, at))[:limit]

        if not selected:
            self._cursors.advance(
                at=at, seen_thread_ids=seen, last_status="empty"
            )
            # Empty-selection still closed the window; the per-turn
            # counters reset for the same reason the buffered / ok
            # branches do (SC22 — window still closes).
            if self._gate_state is not None:
                self._gate_state.reset(active_facts=active_facts)
            return {"status": "empty", "transported": 0, "selected": 0}

        batch = self._summarize(selected)
        payload = [self._as_payload(item) for item in batch]

        # SC22: with the null backend (the only wired kind), the merge
        # performs 0 memory-MCP writes. The window still closes and the
        # gate state resets so the next window starts a fresh cycle.
        stored = self._persist(payload)
        if stored < 1:
            self._cursors.advance(
                at=at, seen_thread_ids=seen, last_status="buffered"
            )
            if self._gate_state is not None:
                self._gate_state.reset(active_facts=active_facts)
            return {
                "status": "buffered",
                "transported": 0,
                "selected": len(batch),
            }

        # A non-null backend would retire rows here. ADR 0048 2026-09-06
        # says there is no wired non-null backend today, so this branch
        # is unreachable in practice; keep it typed so a future wired
        # backend has the contract in one place.
        self._cursors.advance(
            at=at, seen_thread_ids=seen, last_status="ok"
        )
        if self._gate_state is not None:
            self._gate_state.reset(active_facts=active_facts)
        return {
            "status": "ok",
            "transported": stored,
            "selected": len(batch),
        }

    def _summarize(
        self, selected: Sequence[ScoredMemory]
    ) -> list[ScoredMemory]:
        if self._summarizer is None:
            return list(selected)
        try:
            result = self._summarizer.summarize(selected)
        except DreamInputError:
            raise
        except Exception as exc:
            raise DreamInputError(
                "dream summarization failed; the batch stays in the buffer"
            ) from exc

        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            raise DreamInputError(
                "the dream summarizer must return a sequence of ScoredMemory"
            )
        items = list(result)
        if not all(isinstance(item, ScoredMemory) for item in items):
            raise DreamInputError(
                "the dream summarizer returned something that is not a "
                "ScoredMemory sequence"
            )
        return items

    def _persist(self, payload: list[dict[str, object]]) -> int:
        try:
            stored = self._backend.persist(payload)
        except MemoryDreamError:
            raise
        except Exception as exc:
            raise DreamTransportError(
                "the memory backend could not persist the dream batch"
            ) from exc

        if isinstance(stored, bool) or not isinstance(stored, int):
            raise DreamTransportError(
                "the memory backend returned a non-integer stored count"
            )
        return stored

    @staticmethod
    def _as_payload(item: ScoredMemory) -> dict[str, object]:
        return {
            "id": item.row.id,
            "text": item.row.text,
            "title": item.row.title,
            "body": item.row.body,
            "workspace": item.row.workspace,
            "hook": item.row.hook,
            "metadata": dict(item.row.metadata),
            "created_at": item.row.created_at.isoformat(),
            "hit_count": item.row.hit_count,
            "score": item.score,
            "reasons": list(item.reasons),
            "advisory": True,
            "citation_eligible": False,
        }


# ---------------------------------------------------------------------
# SC21: the per-turn hook is the gated entrypoint
# ---------------------------------------------------------------------


def run_dream_after_turn(
    port: MemoryPort,
    *,
    thread_id: str,
    gate_state_store: DreamGateStateStore | None = None,
    active_facts_provider: Callable[[], int] | None = None,
    dream_factory: Callable[[], MemoryDream] | None = None,
) -> dict[str, object] | None:
    """Count a turn; only fire the dream when both halves of the gate open.

    This is the reason dream is not a graph node (CONTEXT.md:46) and
    not unconditional post-SSE (ADR 0048 2026-09-06 / SC21): the
    per-turn hook is cheap — it bumps the turn counter and reads a
    couple of counters — and the LLM only runs when the gate is
    open.

    Memory OFF is a hard skip: SC2 requires the default boot to do no
    dream work at all, so an OFF port is not even asked.

    The wiring (HTTP / SSE) passes ``gate_state_store``,
    ``active_facts_provider``, and ``dream_factory`` so the hook has
    the dual-gate inputs it needs. A port that does not expose them
    (the wiring tests' minimal recording port, the OFF adapter) is a
    safe no-op — SC21's "do not unconditionally enqueue dream" is
    preserved either way.

    Nothing may raise out of here. Starlette runs background tasks
    without isolation, so an escaping exception would turn a turn
    that already succeeded into a broken response (SC21).
    """
    if not port.enabled:
        return None
    if (
        gate_state_store is None
        or active_facts_provider is None
        or dream_factory is None
    ):
        # The port did not expose the dual-gate wiring; the spec
        # forbids firing the dream unconditionally, so a missing
        # wiring is a no-op (NOT a fallback to ``port.dream()``).
        return None
    try:
        active_facts = max(0, int(active_facts_provider()))
        gate_state_store.record_turn(active_facts=active_facts)
        dream = dream_factory()
        return dream.run(now=None)
    except Exception:  # noqa: BLE001
        logger.exception(
            "memory dream failed after thread %s; the turn is unaffected",
            thread_id,
        )
        return None


__all__ = [
    "DREAM_MIN_ACTIVE_FACTS",
    "DREAM_MIN_INTERVAL",
    "DREAM_MIN_NEW_FACTS",
    "DREAM_MIN_THREADS",
    "DREAM_MIN_TURNS_SINCE_DREAM",
    "LONGEVITY_DAYS",
    "SCORE_LONGEVITY",
    "SCORE_REPEATED_HITS",
    "SCORE_SINGLE_HIT",
    "SELECTION_THRESHOLD",
    "TRANSPORT_RETENTION_DAYS",
    "DreamGate",
    "DreamGateState",
    "DreamGateStateStore",
    "DreamInputError",
    "DreamSummarizer",
    "DreamTransportError",
    "InProcessDreamGateStateStore",
    "MemoryDream",
    "MemoryDreamBackend",
    "MemoryDreamError",
    "MemoryDreamScorer",
    "NullDreamBackend",
    "NullDreamSummarizer",
    "ScoredMemory",
    "evaluate_dream_gate",
    "run_dream_after_turn",
]
