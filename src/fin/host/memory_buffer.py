"""Advisory-memory buffer behind the T3 :mod:`memory_port`.

The T3 :mod:`memory_port` remains the product seam and stays OFF by default.
This module owns local extract, recall, and soft garbage collection. Memory
rows are deliberately not filing evidence: result rows carry an explicit
advisory marker and no citation, evidence, or ``source_ref``.

``MemoryBuffer`` holds the behaviour once and delegates persistence to a
:class:`~fin.host.memory_store.MemoryStore`. Two subclasses pick the store:
:class:`LocalMemoryBuffer` keeps rows in the process, and
:class:`PostgresMemoryBuffer` keeps them in ``fin_memory`` per ADR 0035.
Swapping stores never changes the port or the host graph.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from fin.host.db_pool import PostgresPool
from fin.host.memory_dream import (
    TRANSPORT_RETENTION_DAYS,
    DreamSummarizer,
    MemoryDream,
    MemoryDreamBackend,
    MemoryDreamError,
)
from fin.host.memory_port import (
    DEFAULT_MEMORY_LIMIT,
    MEMORY_LIMIT_CAP,
    MemoryPort,
    OffMemoryAdapter,
)
from fin.host.memory_store import (
    DreamCursorStore,
    InProcessDreamCursorStore,
    InProcessMemoryStore,
    MemoryRow,
    MemoryStore,
    PostgresDreamCursorStore,
    PostgresMemoryStore,
)

logger = logging.getLogger(__name__)


class MemoryBufferError(Exception):
    """Base class for typed local-buffer failures."""


class MemoryBufferInputError(MemoryBufferError):
    """The caller supplied malformed memory text, metadata, or limit."""


MemoryInputError = MemoryBufferInputError


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


def _json_safe_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Normalise metadata to what a JSONB column can hold.

    ``fin_memory.metadata`` is JSONB, so a ``datetime`` there would be
    coerced to a string by Postgres while the process-local store kept the
    object. Normalising at this boundary keeps the two implementations
    behaviourally identical instead of drifting (SC10).
    """
    return json.loads(json.dumps(metadata, default=str))


class MemoryBuffer:
    """Advisory-memory behaviour over an injectable row store.

    One non-empty extraction produces one row. Ranking, expiry, and
    supersession live here so both stores behave identically; only
    persistence differs.
    """

    enabled = True

    def __init__(
        self,
        store: MemoryStore,
        cursors: DreamCursorStore,
        *,
        backend: MemoryDreamBackend | None = None,
        summarizer: DreamSummarizer | None = None,
    ) -> None:
        self._store = store
        self._cursors = cursors
        self._backend = backend
        self._summarizer = summarizer

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
        title: str = "",
        workspace: str = "",
    ) -> dict[str, object]:
        """Store one non-empty dialogue memory as advisory context."""
        if not isinstance(text, str):
            raise MemoryBufferInputError("text must be a string")
        cleaned = text.strip()
        if not cleaned:
            return {"status": "empty", "memories": []}
        if metadata is not None and not isinstance(metadata, Mapping):
            raise MemoryBufferInputError("metadata must be a mapping")
        if not isinstance(title, str):
            raise MemoryBufferInputError("title must be a string")
        if not isinstance(workspace, str):
            raise MemoryBufferInputError("workspace must be a string")

        copied_metadata = copy.deepcopy(dict(metadata or {}))
        expires_at = self._expiry_from_metadata(copied_metadata)
        supersedes = copied_metadata.get("supersedes")
        if supersedes is not None and (
            not isinstance(supersedes, str) or not supersedes.strip()
        ):
            raise MemoryBufferInputError("supersedes must be a non-empty id")

        row = MemoryRow(
            id=str(uuid.uuid4()),
            text=cleaned,
            metadata=_json_safe_metadata(copied_metadata),
            created_at=_utc_now(),
            expires_at=expires_at,
            title=title,
            body=cleaned,
            workspace=workspace,
        )
        self._store.add(row)
        return {"status": "ok", "memories": [self._as_memory(row)]}

    def recall(
        self,
        query: str,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
        workspace: str = "",
    ) -> dict[str, object]:
        """Return active memories ranked by lexical token overlap.

        This is advisory recall, not retrieval evidence. Returned rows
        intentionally cannot be passed to the citation gate as filing
        locators. Every returned row records one hit, which is the
        recurrence signal the dream score reads (SC7).

        ``workspace`` scopes the read to one workspace (spec SC6). The
        default empty string keeps legacy callers working; production
        callers pass the operator's workspace.
        """
        if not isinstance(query, str):
            raise MemoryBufferInputError("query must be a string")
        if not isinstance(workspace, str):
            raise MemoryBufferInputError("workspace must be a string")
        resolved_limit = _resolve_limit(limit)
        query_tokens = _tokens(query.strip())
        if not query_tokens:
            return {"status": "empty", "memories": []}

        rows = self._store.rows(include_disabled=False, workspace=workspace)
        ranked = [
            (len(query_tokens & _tokens(row.text)), row)
            for row in rows
            if query_tokens & _tokens(row.text)
        ]
        ranked.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        top = ranked[:resolved_limit]

        self._store.bump_hit_counts([row.id for _score, row in top])
        # ``rows()`` hands back copies, so mirror the increment onto them
        # instead of re-reading the store for the response.
        for _score, row in top:
            row.hit_count += 1

        return {
            "status": "ok",
            "memories": [
                self._as_memory(row, score=score) for score, row in top
            ],
        }

    def gc(
        self,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Soft-disable expired or superseded active rows, then purge.

        ``now`` is injectable for deterministic boundary tests. Rows that
        were never transported are only soft-disabled; rows a backend
        already confirmed are hard-deleted once they pass the retention
        window, because a copy that exists durably elsewhere has no read
        value locally (SC19).
        """
        resolved_limit = _resolve_limit(limit)
        gc_time = (
            _utc_now() if now is None else _normalise_datetime(now, field="now")
        )

        rows = self._store.rows(include_disabled=False)
        superseded_ids = {
            str(row.metadata["supersedes"])
            for row in rows
            if row.metadata.get("supersedes")
        }
        candidates = [
            row
            for row in rows
            if (row.expires_at is not None and row.expires_at <= gc_time)
            or row.id in superseded_ids
        ]
        candidates.sort(key=lambda row: row.created_at)

        removed = 0
        for row in candidates[:resolved_limit]:
            reason = (
                "expired"
                if row.expires_at is not None and row.expires_at <= gc_time
                else "superseded"
            )
            self._store.disable(row.id, reason=reason, at=gc_time)
            removed += 1

        self._store.delete_transported_before(
            gc_time - timedelta(days=TRANSPORT_RETENTION_DAYS)
        )
        return {"status": "ok", "removed": removed}

    def entries(
        self,
        *,
        include_disabled: bool = True,
        workspace: str = "",
    ) -> list[dict[str, object]]:
        """Return copies of authority rows for inspection and persistence.

        ``workspace`` scopes the read to one workspace (spec SC6).
        """
        if not isinstance(workspace, str):
            raise MemoryBufferInputError("workspace must be a string")
        return [
            self._as_memory(row)
            for row in self._store.rows(
                include_disabled=include_disabled,
                workspace=workspace,
            )
        ]

    def dream(self, *, limit: int = DEFAULT_MEMORY_LIMIT) -> dict[str, object]:
        """Consolidate useful rows and transport them to the dream backend.

        A failed dream is recorded, never raised: CONTEXT.md:45 requires
        that dream must not fail the foreground turn, and the HTTP layer
        that triggers it runs after the response has already streamed.
        """
        try:
            return MemoryDream(
                self._store,
                self._cursors,
                backend=self._backend,
                summarizer=self._summarizer,
            ).run(limit=limit)
        except MemoryDreamError as exc:
            logger.exception("memory dream failed; the buffer is untouched")
            return {
                "status": "error",
                "error": type(exc).__name__,
                "transported": 0,
            }

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
        row: MemoryRow,
        *,
        score: int | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "id": row.id,
            "text": row.text,
            "title": row.title,
            "body": row.body,
            "workspace": row.workspace,
            "hook": row.hook,
            "metadata": copy.deepcopy(row.metadata),
            "created_at": row.created_at.isoformat(),
            "expires_at": (
                row.expires_at.isoformat() if row.expires_at is not None else None
            ),
            "hit_count": row.hit_count,
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


class LocalMemoryBuffer(MemoryBuffer):
    """Process-local buffer used by tests and by in-memory deployments.

    Rows do not survive a restart; that is the Postgres store's contract,
    not this one's.
    """

    def __init__(
        self,
        *,
        backend: MemoryDreamBackend | None = None,
        summarizer: DreamSummarizer | None = None,
    ) -> None:
        super().__init__(
            InProcessMemoryStore(),
            InProcessDreamCursorStore(),
            backend=backend,
            summarizer=summarizer,
        )


class PostgresMemoryBuffer(MemoryBuffer):
    """Buffer backed by ``fin_memory`` (ADR 0035's working store).

    The pool is opened on construction so a misconfigured URL fails at
    wiring time instead of on the first operator request.
    """

    def __init__(
        self,
        pool: PostgresPool,
        *,
        backend: MemoryDreamBackend | None = None,
        summarizer: DreamSummarizer | None = None,
    ) -> None:
        self._pool = pool
        if not pool.is_open:
            pool.open()
        super().__init__(
            PostgresMemoryStore(pool),
            PostgresDreamCursorStore(pool),
            backend=backend,
            summarizer=summarizer,
        )

    def close(self) -> None:
        """Close the pool. The buffer is unusable afterwards."""
        self._pool.close()


def build_local_memory_port(*, enabled: bool = True) -> MemoryPort:
    """Build an explicit local/off port without changing the default port."""
    return LocalMemoryBuffer() if enabled else OffMemoryAdapter()


LocalMemoryAdapter = LocalMemoryBuffer


__all__ = [
    "LocalMemoryAdapter",
    "LocalMemoryBuffer",
    "MemoryBuffer",
    "MemoryBufferError",
    "MemoryBufferInputError",
    "MemoryInputError",
    "PostgresMemoryBuffer",
    "build_local_memory_port",
]
