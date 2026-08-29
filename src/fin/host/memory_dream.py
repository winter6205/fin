"""Dream: the rule-gated consolidate step of ``CONTEXT.md:45``.

Dream picks the *useful* rows out of the local working buffer and hands
them to a :class:`MemoryDreamBackend` for durable persistence. It is
rule-gated — 24 hours and five new conversations must have passed — so a
chat that never accumulates either never pays for a transport.

Two things here are deliberate absences:

* dream is **not** a graph node (``CONTEXT.md:46``). The HTTP layer runs it
  after the SSE stream ends, which T6c wires.
* with no backend configured dream **stops at the buffer**, returning
  ``buffered`` and leaving every row active. That is the shape
  ``CONTEXT.md:45`` allows while the memory MCP is still unproven, and it
  is the default until T6d lands a real transport.

Failures are typed (:class:`MemoryDreamError` and its subclasses) and never
leave half a batch behind: rows are retired only after the backend reports
it stored them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, Sequence

from fin.host.memory_port import (
    DEFAULT_MEMORY_LIMIT,
    MEMORY_LIMIT_CAP,
    DreamCursorError,
    DreamInputError,
    DreamTransportError,
    MemoryDreamError,
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
    """The default summarizer: no model call, the batch passes through."""

    def summarize(
        self, memories: Sequence[ScoredMemory]
    ) -> Sequence[ScoredMemory]:
        return list(memories)


class MemoryDreamBackend(Protocol):
    """Where dream transports the batch. Swapping MCP changes only this."""

    def persist(self, memories: Sequence[dict[str, object]]) -> int:
        """Store ``memories`` and report how many were stored."""
        ...


class NullDreamBackend:
    """The default backend: no durable target, so dream stops at the buffer."""

    def persist(self, memories: Sequence[dict[str, object]]) -> int:
        return 0


@dataclass(frozen=True)
class DreamGate:
    """The two conditions that must both hold before dream runs."""

    min_interval: timedelta = DREAM_MIN_INTERVAL
    min_threads: int = DREAM_MIN_THREADS


def _resolve_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise DreamInputError("limit must be an integer")
    if limit <= 0:
        return DEFAULT_MEMORY_LIMIT
    return min(limit, MEMORY_LIMIT_CAP)


class MemoryDream:
    """Runs one dream window over a buffer's rows and cursor."""

    def __init__(
        self,
        store: MemoryStore,
        cursors: DreamCursorStore,
        *,
        backend: MemoryDreamBackend | None = None,
        summarizer: DreamSummarizer | None = None,
        gate: DreamGate | None = None,
        scorer: MemoryDreamScorer | None = None,
    ) -> None:
        self._store = store
        self._cursors = cursors
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
        """Consolidate and transport one batch, if the gate opens.

        Raises a :class:`MemoryDreamError` subclass on failure. The caller
        that owns the foreground turn is responsible for catching it —
        ``MemoryBuffer.dream`` does, so a broken backend can never fail an
        operator's turn (SC21).
        """
        at = now if now is not None else datetime.now(timezone.utc)
        resolved_limit = _resolve_limit(limit)

        cursor = self._cursors.load()
        rows = self._store.rows()

        new_threads, missing = self._new_thread_ids(rows, cursor)
        if missing:
            logger.warning(
                "memory dream gate: %d row(s) carry no thread_id and are not "
                "counted toward the conversation gate",
                missing,
            )

        if not self._time_gate_open(cursor, at):
            return {"status": "skipped", "reason": "interval", "transported": 0}
        if len(new_threads) < self._gate.min_threads:
            return {"status": "skipped", "reason": "threads", "transported": 0}

        seen = {row.thread_id for row in rows if row.thread_id}
        if not self._cursors.claim(
            at=at,
            min_interval=self._gate.min_interval,
            seen_thread_ids=frozenset(seen),
            last_status="running",
        ):
            return {"status": "skipped", "reason": "claimed", "transported": 0}

        try:
            return self._consolidate(
                rows, at=at, limit=resolved_limit, seen=frozenset(seen)
            )
        except MemoryDreamError:
            self._cursors.advance(
                at=at, seen_thread_ids=frozenset(seen), last_status="error"
            )
            raise

    def _consolidate(
        self,
        rows: Sequence[MemoryRow],
        *,
        at: datetime,
        limit: int,
        seen: frozenset[str],
    ) -> dict[str, object]:
        active = [row for row in rows if row.status == "active"]
        selected = self._scorer.select(self._scorer.score(active, at))[:limit]

        if not selected:
            self._cursors.advance(
                at=at, seen_thread_ids=seen, last_status="empty"
            )
            return {"status": "empty", "transported": 0, "selected": 0}

        batch = self._summarize(selected)
        payload = [self._as_payload(item) for item in batch]

        stored = self._persist(payload)
        if stored < 1:
            self._cursors.advance(
                at=at, seen_thread_ids=seen, last_status="buffered"
            )
            return {
                "status": "buffered",
                "transported": 0,
                "selected": len(batch),
            }

        self._store.mark_transported([item.row.id for item in batch], at=at)
        self._cursors.advance(at=at, seen_thread_ids=seen, last_status="ok")
        return {"status": "ok", "transported": stored, "selected": len(batch)}

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

    def _time_gate_open(self, cursor: DreamCursor, at: datetime) -> bool:
        if cursor.last_dream_at is None:
            return True
        return at - cursor.last_dream_at >= self._gate.min_interval

    @staticmethod
    def _new_thread_ids(
        rows: Sequence[MemoryRow], cursor: DreamCursor
    ) -> tuple[set[str], int]:
        """Return the threads new since the cursor, and the uncountable rows.

        ``thread_id`` is the gate's only data source (SC8). Rows without
        one are real memories that simply cannot be counted, so they are
        reported rather than dropped.
        """
        threads = {row.thread_id for row in rows if row.thread_id}
        missing = sum(1 for row in rows if row.thread_id is None)
        return threads - cursor.seen_thread_ids, missing

    @staticmethod
    def _as_payload(item: ScoredMemory) -> dict[str, object]:
        return {
            "id": item.row.id,
            "text": item.row.text,
            "metadata": dict(item.row.metadata),
            "created_at": item.row.created_at.isoformat(),
            "hit_count": item.row.hit_count,
            "score": item.score,
            "reasons": list(item.reasons),
            "advisory": True,
            "citation_eligible": False,
        }


__all__ = [
    "DREAM_MIN_INTERVAL",
    "DREAM_MIN_THREADS",
    "LONGEVITY_DAYS",
    "SCORE_LONGEVITY",
    "SCORE_REPEATED_HITS",
    "SCORE_SINGLE_HIT",
    "SELECTION_THRESHOLD",
    "TRANSPORT_RETENTION_DAYS",
    "DreamCursorError",
    "DreamGate",
    "DreamInputError",
    "DreamSummarizer",
    "DreamTransportError",
    "MemoryDream",
    "MemoryDreamBackend",
    "MemoryDreamError",
    "MemoryDreamScorer",
    "NullDreamBackend",
    "NullDreamSummarizer",
    "ScoredMemory",
]
