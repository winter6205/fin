"""Local advisory-memory buffer for an enabled memory adapter.

The T3 :mod:`memory_port` remains the product seam and stays OFF by default.
This module owns local extract, recall, and soft garbage collection. Memory
rows are deliberately not filing evidence: result rows carry an explicit
advisory marker and no citation, evidence, or ``source_ref``.

The behavior is kept behind this small backend so a later Postgres store can
replace the process-local storage without changing the port or host graph.
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping

from fin.host.memory_port import (
    DEFAULT_MEMORY_LIMIT,
    MEMORY_LIMIT_CAP,
    MemoryPort,
    OffMemoryAdapter,
)


MemoryStatus = Literal["active", "disabled"]


class MemoryBufferError(Exception):
    """Base class for typed local-buffer failures."""


class MemoryBufferInputError(MemoryBufferError):
    """The caller supplied malformed memory text, metadata, or limit."""


MemoryInputError = MemoryBufferInputError


@dataclass
class _MemoryRow:
    """Authority row kept internally, including soft-disabled records."""

    id: str
    text: str
    metadata: dict[str, object]
    created_at: datetime
    expires_at: datetime | None
    status: MemoryStatus = "active"
    disabled_reason: str | None = None
    disabled_at: datetime | None = None


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise MemoryBufferInputError(
                f"{field} must be an ISO-8601 datetime"
            ) from exc
    else:
        raise MemoryBufferInputError(
            f"{field} must be a datetime or ISO-8601 string"
        )

    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _resolve_limit(limit: int) -> int:
    """Match the bounded limit behavior of the T3 memory port."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise MemoryBufferInputError("limit must be an integer")
    if limit <= 0:
        return DEFAULT_MEMORY_LIMIT
    return min(limit, MEMORY_LIMIT_CAP)


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value)}


class LocalMemoryBuffer:
    """Thread-safe local buffer for advisory dialogue memories.

    One non-empty extraction produces one row. GC changes row status instead
    of deleting it, preserving the authority record for auditing or a later
    Postgres persistence adapter. ``dream`` is a deferred no-op: T6 owns MCP
    transport and this class never sends memory outside the process.
    """

    enabled = True

    def __init__(self) -> None:
        self._rows: dict[str, _MemoryRow] = {}
        self._lock = threading.RLock()

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Store one non-empty dialogue memory as advisory context."""
        if not isinstance(text, str):
            raise MemoryBufferInputError("text must be a string")
        cleaned = text.strip()
        if not cleaned:
            return {"status": "empty", "memories": []}
        if metadata is not None and not isinstance(metadata, Mapping):
            raise MemoryBufferInputError("metadata must be a mapping")

        copied_metadata = copy.deepcopy(dict(metadata or {}))
        expires_at = self._expiry_from_metadata(copied_metadata)
        supersedes = copied_metadata.get("supersedes")
        if supersedes is not None and (
            not isinstance(supersedes, str) or not supersedes.strip()
        ):
            raise MemoryBufferInputError("supersedes must be a non-empty id")

        row = _MemoryRow(
            id=str(uuid.uuid4()),
            text=cleaned,
            metadata=copied_metadata,
            created_at=_utc_now(),
            expires_at=expires_at,
        )
        with self._lock:
            self._rows[row.id] = row
        return {"status": "ok", "memories": [self._as_memory(row)]}

    def recall(
        self,
        query: str,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
    ) -> dict[str, object]:
        """Return active memories ranked by lexical token overlap.

        This is advisory recall, not retrieval evidence. Returned rows
        intentionally cannot be passed to the citation gate as filing
        locators.
        """
        if not isinstance(query, str):
            raise MemoryBufferInputError("query must be a string")
        resolved_limit = _resolve_limit(limit)
        query_tokens = _tokens(query.strip())
        if not query_tokens:
            return {"status": "empty", "memories": []}

        with self._lock:
            ranked = [
                (len(query_tokens & _tokens(row.text)), row)
                for row in self._rows.values()
                if row.status == "active"
                and query_tokens & _tokens(row.text)
            ]
        ranked.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return {
            "status": "ok",
            "memories": [
                self._as_memory(row, score=score)
                for score, row in ranked[:resolved_limit]
            ],
        }

    def gc(
        self,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Soft-disable expired or superseded active rows.

        ``now`` is injectable for deterministic boundary tests. Disabled rows
        remain in :meth:`entries`, so GC never deletes authority data.
        """
        resolved_limit = _resolve_limit(limit)
        gc_time = (
            _utc_now() if now is None else _normalise_datetime(now, field="now")
        )

        with self._lock:
            superseded_ids = {
                str(row.metadata["supersedes"])
                for row in self._rows.values()
                if row.status == "active" and row.metadata.get("supersedes")
            }
            candidates = [
                row
                for row in self._rows.values()
                if row.status == "active"
                and (
                    (row.expires_at is not None and row.expires_at <= gc_time)
                    or row.id in superseded_ids
                )
            ]
            candidates.sort(key=lambda row: row.created_at)
            removed = 0
            for row in candidates[:resolved_limit]:
                reason = (
                    "expired"
                    if row.expires_at is not None and row.expires_at <= gc_time
                    else "superseded"
                )
                row.status = "disabled"
                row.disabled_reason = reason
                row.disabled_at = gc_time
                removed += 1
        return {"status": "ok", "removed": removed}

    def entries(self, *, include_disabled: bool = True) -> list[dict[str, object]]:
        """Return copies of authority rows for inspection and persistence."""
        with self._lock:
            rows = list(self._rows.values())
        if not include_disabled:
            rows = [row for row in rows if row.status == "active"]
        return [self._as_memory(row) for row in rows]

    def dream(self, *, limit: int = DEFAULT_MEMORY_LIMIT) -> dict[str, object]:
        """Defer durable transport to the T6 memory-MCP adapter."""
        _resolve_limit(limit)
        return {"status": "deferred", "transported": 0}

    @staticmethod
    def _expiry_from_metadata(
        metadata: dict[str, object],
    ) -> datetime | None:
        value = metadata.get("expires_at")
        if value is None:
            return None
        return _normalise_datetime(value, field="expires_at")

    @staticmethod
    def _as_memory(
        row: _MemoryRow,
        *,
        score: int | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "id": row.id,
            "text": row.text,
            "metadata": copy.deepcopy(row.metadata),
            "created_at": row.created_at.isoformat(),
            "expires_at": (
                row.expires_at.isoformat() if row.expires_at is not None else None
            ),
            "status": row.status,
            "advisory": True,
            "citation_eligible": False,
        }
        if row.disabled_reason is not None:
            result["disabled_reason"] = row.disabled_reason
        if row.disabled_at is not None:
            result["disabled_at"] = row.disabled_at.isoformat()
        if score is not None:
            result["score"] = score
        return result


def build_local_memory_port(*, enabled: bool = True) -> MemoryPort:
    """Build an explicit local/off port without changing the default port."""
    return LocalMemoryBuffer() if enabled else OffMemoryAdapter()


MemoryBuffer = LocalMemoryBuffer
LocalMemoryAdapter = LocalMemoryBuffer


__all__ = [
    "LocalMemoryAdapter",
    "LocalMemoryBuffer",
    "MemoryBuffer",
    "MemoryBufferError",
    "MemoryBufferInputError",
    "MemoryInputError",
    "build_local_memory_port",
]
