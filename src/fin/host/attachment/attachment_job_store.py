"""Durable preparation jobs for thread attachments (ADR 0089, plan T2f).

One job is the unit of preparation work, keyed by
``(attachment_id, content_sha256)``: the pair is what "this exact content
of this exact attachment" means, so a re-upload that changed the bytes is
a different job and a retried request for the same bytes joins the one
already in flight. The graph gate's start-or-join promise — concurrent
triggers never create a second active job — is decided here, under the
primary key, not in the caller.

Lifecycle: ``pending`` → ``running`` (exactly one winner per claim) →
one of the terminals ``succeeded`` / ``failed`` / ``voided``. ``voided``
is the tombstone answer: the attachment was deleted while its job was
queued or running, so the job is retired without publishing anything.

The reopen rule (chosen deliberately, pinned by tests): a fresh
``enqueue`` reopens a ``failed`` job to ``pending`` — its caller only
reaches ``enqueue`` when the attachment row itself asks for preparation
again — while ``succeeded`` and ``voided`` are terminal and are never
re-run by an enqueue. A repair of a published-but-damaged attachment
goes through the explicit :meth:`reopen` instead, so "we are re-running
content that already succeeded" is always a named decision at the call
site, never a side effect of a join.

Two implementations satisfy :class:`ThreadAttachmentJobStore`, matching
the twins in :mod:`fin.host.attachment.attachment_store`:
:class:`InMemoryThreadAttachmentJobStore` (dicts behind one lock; the
default test and no-Postgres boot) and :class:`PostgresThreadAttachmentJobStore`
(the same rules on ``fin_thread_attachment_job`` rows, with the claim CAS
as one ``FOR UPDATE SKIP LOCKED`` statement so two processes cannot both
win).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Collection, Final, Literal, Protocol

#: The persisted states of one job. ``pending``/``running`` are the active
#: work a claim or a boot sweep can act on; the rest are terminals.
JobState = Literal["pending", "running", "succeeded", "failed", "voided"]

#: The states :meth:`incomplete` reports by default: unfinished work.
ACTIVE_STATES: Final[tuple[JobState, ...]] = ("pending", "running")

#: The states a publication is UNRESOLVED in — not yet published, and
#: not published successfully either. ``failed`` belongs here and
#: ``succeeded`` does not: a failed attempt leaves the document as
#: unsearchable as one that was never attempted, which is exactly what
#: a warning about this turn's evidence has to count.
UNRESOLVED_STATES: Final[tuple[JobState, ...]] = ("pending", "running", "failed")

_STATES: tuple[JobState, ...] = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "voided",
)


@dataclass(frozen=True)
class ThreadAttachmentJob:
    """One durable preparation job."""

    attachment_id: str
    content_sha256: str
    target_version: int
    state: JobState
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class ThreadAttachmentJobStore(Protocol):
    """The durability contract for attachment preparation work."""

    def enqueue(
        self, *, attachment_id: str, content_sha256: str, target_version: int
    ) -> ThreadAttachmentJob:
        """Start or join THE one job for this content key.

        A missing job row is created ``pending``; an active (``pending``
        or ``running``) row is joined unchanged; a ``failed`` row is
        reopened to ``pending`` under the new target version (see the
        module docstring for why); ``succeeded`` and ``voided`` rows are
        returned as the terminals they are.
        """
        ...

    def reopen(
        self, *, attachment_id: str, content_sha256: str, target_version: int
    ) -> ThreadAttachmentJob:
        """Move any job for the key back to ``pending`` explicitly.

        The repair seam for content whose published result went unreadable:
        unlike :meth:`enqueue`, it reopens a ``succeeded`` job too, so a
        re-run of already-succeeded content only happens where a caller
        named the reason.
        """
        ...

    def claim_next(self) -> ThreadAttachmentJob | None:
        """Claim the oldest active job, or ``None`` when nothing is pending.

        Single winner: racing claimants see exactly one non-``None``
        result for one job.
        """
        ...

    def mark_succeeded(self, *, attachment_id: str, content_sha256: str) -> None:
        """Finish a claimed job successfully."""
        ...

    def mark_failed(
        self, *, attachment_id: str, content_sha256: str, reason_code: str
    ) -> None:
        """Finish a claimed job with the typed failure code."""
        ...

    def mark_voided(self, *, attachment_id: str, content_sha256: str) -> None:
        """Retire a job because its attachment was deleted.

        Callable from any state: the tombstone always wins, and a late
        publication must land on nothing.
        """
        ...

    def incomplete(
        self, *, states: Collection[JobState] = ACTIVE_STATES
    ) -> tuple[ThreadAttachmentJob, ...]:
        """Jobs in the given (default: active) states, oldest first."""
        ...

    def get(
        self, *, attachment_id: str, content_sha256: str
    ) -> ThreadAttachmentJob | None:
        """The job row for one content key, or ``None`` when there is none."""
        ...

    def requeue_running(self) -> int:
        """Boot sweep: move every ``running`` job back to ``pending``.

        Safe against a single-process executor: a ``running`` row at boot
        belongs to an executor that died with the previous process, and
        only the sweep may adopt it. Returns how many rows were reset.
        """
        ...

    def forget(self, *, attachment_id: str) -> int:
        """Delete every job row of one attachment (confirmed-cleanup cleanup).

        Returns how many rows were removed. Unfinished work of a deleted
        attachment is also voided by the drain's liveness re-check;
        ``forget`` is what a confirmed deletion does so retired jobs do
        not pile up.
        """
        ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_target_version(version: object) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(
            f"target_version must be a positive int (a preparation mints "
            f"version >= 1), got {version!r}"
        )
    return version


def _require_states(states: Collection[JobState]) -> tuple[JobState, ...]:
    wanted = tuple(dict.fromkeys(states))
    unknown = [state for state in wanted if state not in _STATES]
    if unknown:
        raise ValueError(f"unknown job states: {unknown!r}; must be one of {_STATES}")
    return wanted


def _job(
    record: ThreadAttachmentJob, **changes: object
) -> ThreadAttachmentJob:
    if "state" in changes:
        changes["state"] = _validate_state(str(changes["state"]))
    return replace(record, **changes)  # type: ignore[arg-type]


def _validate_state(value: str) -> JobState:
    if value in _STATES:
        return value  # type: ignore[return-value]
    raise ValueError(f"job state must be one of {_STATES}, got {value!r}")


class InMemoryThreadAttachmentJobStore:
    """Dict-backed twin of the job store, guarded by one lock.

    THE join and the claim are each one critical section: that is what
    makes "concurrent duplicate requests join one job" and "exactly one
    claim winner" true for the default boot, mirroring the row lock the
    Postgres twin gets from ``FOR UPDATE SKIP LOCKED``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[tuple[str, str], ThreadAttachmentJob] = {}

    def enqueue(
        self, *, attachment_id: str, content_sha256: str, target_version: int
    ) -> ThreadAttachmentJob:
        attachment_id = _require_text("attachment_id", attachment_id)
        content_sha256 = _require_text("content_sha256", content_sha256)
        target_version = _require_target_version(target_version)
        with self._lock:
            existing = self._jobs.get((attachment_id, content_sha256))
            if existing is None:
                stamp = _now()
                job = ThreadAttachmentJob(
                    attachment_id=attachment_id,
                    content_sha256=content_sha256,
                    target_version=target_version,
                    state="pending",
                    failure_reason=None,
                    created_at=stamp,
                    updated_at=stamp,
                )
                self._jobs[(attachment_id, content_sha256)] = job
                return job
            if existing.state == "failed":
                reopened = _job(
                    existing,
                    state="pending",
                    failure_reason=None,
                    target_version=target_version,
                    updated_at=_now(),
                )
                self._jobs[(attachment_id, content_sha256)] = reopened
                return reopened
            return existing

    def reopen(
        self, *, attachment_id: str, content_sha256: str, target_version: int
    ) -> ThreadAttachmentJob:
        attachment_id = _require_text("attachment_id", attachment_id)
        content_sha256 = _require_text("content_sha256", content_sha256)
        target_version = _require_target_version(target_version)
        with self._lock:
            existing = self._jobs.get((attachment_id, content_sha256))
            if existing is None:
                return self.enqueue(
                    attachment_id=attachment_id,
                    content_sha256=content_sha256,
                    target_version=target_version,
                )
            reopened = _job(
                existing,
                state="pending",
                failure_reason=None,
                target_version=target_version,
                updated_at=_now(),
            )
            self._jobs[(attachment_id, content_sha256)] = reopened
            return reopened

    def claim_next(self) -> ThreadAttachmentJob | None:
        with self._lock:
            for key, job in self._jobs.items():
                if job.state == "pending":
                    claimed = _job(job, state="running", updated_at=_now())
                    self._jobs[key] = claimed
                    return claimed
            return None

    def mark_succeeded(self, *, attachment_id: str, content_sha256: str) -> None:
        with self._lock:
            job = self._jobs.get((attachment_id, content_sha256))
            # WHY only a claimed job may succeed: a finish without a claim
            # would bless work no executor ran; a missing row means the
            # attachment was confirmed-deleted mid-run, so there is nothing
            # left to finish.
            if job is not None and job.state == "running":
                self._jobs[(attachment_id, content_sha256)] = _job(
                    job, state="succeeded", updated_at=_now()
                )

    def mark_failed(
        self, *, attachment_id: str, content_sha256: str, reason_code: str
    ) -> None:
        reason_code = _require_text("reason_code", reason_code)
        with self._lock:
            job = self._jobs.get((attachment_id, content_sha256))
            if job is not None and job.state == "running":
                self._jobs[(attachment_id, content_sha256)] = _job(
                    job,
                    state="failed",
                    failure_reason=reason_code,
                    updated_at=_now(),
                )

    def mark_voided(self, *, attachment_id: str, content_sha256: str) -> None:
        with self._lock:
            job = self._jobs.get((attachment_id, content_sha256))
            if job is not None and job.state != "voided":
                self._jobs[(attachment_id, content_sha256)] = _job(
                    job, state="voided", failure_reason=None, updated_at=_now()
                )

    def incomplete(
        self, *, states: Collection[JobState] = ACTIVE_STATES
    ) -> tuple[ThreadAttachmentJob, ...]:
        wanted = _require_states(states)
        with self._lock:
            return tuple(
                job
                for job in self._jobs.values()
                if job.state in wanted
            )

    def get(
        self, *, attachment_id: str, content_sha256: str
    ) -> ThreadAttachmentJob | None:
        with self._lock:
            return self._jobs.get((attachment_id, content_sha256))

    def requeue_running(self) -> int:
        reset = 0
        with self._lock:
            for key, job in list(self._jobs.items()):
                if job.state == "running":
                    self._jobs[key] = _job(job, state="pending", updated_at=_now())
                    reset += 1
        return reset

    def forget(self, *, attachment_id: str) -> int:
        attachment_id = _require_text("attachment_id", attachment_id)
        with self._lock:
            keys = [
                key for key in self._jobs if key[0] == attachment_id
            ]
            for key in keys:
                del self._jobs[key]
            return len(keys)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_thread_attachment_job (
    attachment_id    TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    target_version   INTEGER NOT NULL,
    state            TEXT NOT NULL CHECK (state IN
                       ('pending','running','succeeded','failed','voided')),
    failure_reason   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (attachment_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_job_claim_idx
    ON fin_thread_attachment_job (state, created_at, attachment_id);
"""

_COLUMNS = (
    "attachment_id, content_sha256, target_version, state, failure_reason, "
    "created_at, updated_at"
)


class PostgresThreadAttachmentJobStore:
    """psycopg-backed twin on ``fin_thread_attachment_job``.

    Same surface and rules as the in-memory store; the mechanics differ
    only where durability demands it. The enqueue join is an
    ``INSERT ... ON CONFLICT DO NOTHING`` plus a ``FOR UPDATE`` read of
    whichever row now owns the key, and the claim is ONE
    ``UPDATE ... WHERE (pk) = (SELECT ... FOR UPDATE SKIP LOCKED)``
    statement: the row lock is what gives two processes the single-winner
    guarantee, and ``SKIP LOCKED`` is what stops a second claimant from
    queueing behind the first one's lock to re-run the same row.

    Each public call opens its own short-lived connection, matching
    :class:`~fin.host.attachment.attachment_store.PostgresAttachmentStore`. Schema
    creation is idempotent and runs once per construction. There is no
    foreign key to ``fin_thread``: a job outlives its attachment row by
    design (it is retired by ``mark_voided`` or ``forget``), and the
    attachment door is the only enqueuer.
    """

    def __init__(self, connection_url: str) -> None:
        if not isinstance(connection_url, str) or not connection_url.strip():
            raise ValueError(
                "the durable attachment job store needs a non-empty Postgres "
                "URL; nothing is configured"
            )
        self._url = connection_url.strip()
        with self._connect() as conn:
            conn.execute(_SCHEMA_SQL)
            conn.commit()

    def _connect(self) -> Any:
        """A connection that closes when the caller's block ends.

        WHY ``closing``: the same reason the attachment store gives —
        psycopg's own context manager only commits or rolls back, and an
        unclosed connection would hold a server slot until collection.
        """
        import contextlib

        import psycopg

        return contextlib.closing(psycopg.connect(self._url))

    @staticmethod
    def _row_to_job(row: Any) -> ThreadAttachmentJob:
        return ThreadAttachmentJob(
            attachment_id=str(row[0]),
            content_sha256=str(row[1]),
            target_version=int(row[2]),
            state=_validate_state(str(row[3])),
            failure_reason=None if row[4] is None else str(row[4]),
            created_at=row[5],
            updated_at=row[6],
        )

    def enqueue(
        self, *, attachment_id: str, content_sha256: str, target_version: int
    ) -> ThreadAttachmentJob:
        attachment_id = _require_text("attachment_id", attachment_id)
        content_sha256 = _require_text("content_sha256", content_sha256)
        target_version = _require_target_version(target_version)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO fin_thread_attachment_job "
                "(attachment_id, content_sha256, target_version, state) "
                "VALUES (%s, %s, %s, 'pending') "
                "ON CONFLICT (attachment_id, content_sha256) DO NOTHING",
                (attachment_id, content_sha256, target_version),
            )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM fin_thread_attachment_job "
                "WHERE attachment_id = %s AND content_sha256 = %s FOR UPDATE",
                (attachment_id, content_sha256),
            ).fetchone()
            assert row is not None
            job = self._row_to_job(row)
            if job.state == "failed":
                # WHY a separate guarded UPDATE instead of an upsert
                # ``DO UPDATE``: the DO UPDATE would also bump target_version
                # on every duplicate request and could steal a row that a
                # claim transitioned in between, exactly what the failed-only
                # reopen rule excludes.
                row = conn.execute(
                    "UPDATE fin_thread_attachment_job SET state = 'pending', "
                    "failure_reason = NULL, target_version = %s, "
                    "updated_at = now() WHERE attachment_id = %s "
                    "AND content_sha256 = %s AND state = 'failed' "
                    f"RETURNING {_COLUMNS}",
                    (target_version, attachment_id, content_sha256),
                ).fetchone()
                job = self._row_to_job(row)
            conn.commit()
        return job

    def reopen(
        self, *, attachment_id: str, content_sha256: str, target_version: int
    ) -> ThreadAttachmentJob:
        attachment_id = _require_text("attachment_id", attachment_id)
        content_sha256 = _require_text("content_sha256", content_sha256)
        target_version = _require_target_version(target_version)
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE fin_thread_attachment_job SET state = 'pending', "
                "failure_reason = NULL, target_version = %s, updated_at = now() "
                "WHERE attachment_id = %s AND content_sha256 = %s "
                f"RETURNING {_COLUMNS}",
                (target_version, attachment_id, content_sha256),
            ).fetchone()
            if row is None:
                conn.rollback()
            else:
                conn.commit()
        if row is None:
            return self.enqueue(
                attachment_id=attachment_id,
                content_sha256=content_sha256,
                target_version=target_version,
            )
        return self._row_to_job(row)

    def claim_next(self) -> ThreadAttachmentJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "WITH picked AS ("
                "  SELECT attachment_id, content_sha256"
                "  FROM fin_thread_attachment_job"
                "  WHERE state = 'pending'"
                "  ORDER BY created_at, attachment_id, content_sha256"
                "  LIMIT 1"
                "  FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE fin_thread_attachment_job j "
                "SET state = 'running', updated_at = now() "
                "FROM picked "
                "WHERE j.attachment_id = picked.attachment_id "
                "AND j.content_sha256 = picked.content_sha256 "
                "RETURNING j.attachment_id, j.content_sha256, "
                "j.target_version, j.state, j.failure_reason, "
                "j.created_at, j.updated_at"
            ).fetchone()
            conn.commit()
        return None if row is None else self._row_to_job(row)

    def _finish(
        self,
        *,
        attachment_id: str,
        content_sha256: str,
        sets: str,
        params: tuple[Any, ...],
        from_running_only: bool,
    ) -> None:
        with self._connect() as conn:
            guard = " AND state = 'running'" if from_running_only else ""
            conn.execute(
                f"UPDATE fin_thread_attachment_job SET {sets} "
                "WHERE attachment_id = %s AND content_sha256 = %s" + guard,
                params + (attachment_id, content_sha256),
            )
            conn.commit()

    def mark_succeeded(self, *, attachment_id: str, content_sha256: str) -> None:
        # WHY the ``running`` guard: a finish without a claim would bless
        # work no executor ran; a row already voided by the delete race
        # stays voided.
        self._finish(
            attachment_id=attachment_id,
            content_sha256=content_sha256,
            sets="state = 'succeeded', failure_reason = NULL, updated_at = now()",
            params=(),
            from_running_only=True,
        )

    def mark_failed(
        self, *, attachment_id: str, content_sha256: str, reason_code: str
    ) -> None:
        reason_code = _require_text("reason_code", reason_code)
        self._finish(
            attachment_id=attachment_id,
            content_sha256=content_sha256,
            sets=(
                "state = 'failed', failure_reason = %s, updated_at = now()"
            ),
            params=(reason_code,),
            from_running_only=True,
        )

    def mark_voided(self, *, attachment_id: str, content_sha256: str) -> None:
        # The tombstone answer wins from any state: late publication must
        # be impossible, and the executor may still believe its job is
        # running.
        self._finish(
            attachment_id=attachment_id,
            content_sha256=content_sha256,
            sets="state = 'voided', failure_reason = NULL, updated_at = now()",
            params=(),
            from_running_only=False,
        )

    def incomplete(
        self, *, states: Collection[JobState] = ACTIVE_STATES
    ) -> tuple[ThreadAttachmentJob, ...]:
        wanted = _require_states(states)
        if not wanted:
            return ()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM fin_thread_attachment_job "
                "WHERE state = ANY(%s) "
                "ORDER BY created_at, attachment_id, content_sha256",
                ([state for state in wanted],),
            ).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def get(
        self, *, attachment_id: str, content_sha256: str
    ) -> ThreadAttachmentJob | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM fin_thread_attachment_job "
                "WHERE attachment_id = %s AND content_sha256 = %s",
                (attachment_id, content_sha256),
            ).fetchone()
        return None if row is None else self._row_to_job(row)

    def requeue_running(self) -> int:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE fin_thread_attachment_job "
                "SET state = 'pending', updated_at = now() "
                "WHERE state = 'running'"
            )
            conn.commit()
        return int(result.rowcount or 0)

    def forget(self, *, attachment_id: str) -> int:
        attachment_id = _require_text("attachment_id", attachment_id)
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM fin_thread_attachment_job WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.commit()
        return int(result.rowcount or 0)


__all__ = [
    "ACTIVE_STATES",
    "InMemoryThreadAttachmentJobStore",
    "JobState",
    "PostgresThreadAttachmentJobStore",
    "ThreadAttachmentJob",
    "ThreadAttachmentJobStore",
]
