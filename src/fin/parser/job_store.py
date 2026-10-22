"""The parser-owned PostgreSQL job store (contract sections 3-5).

PostgreSQL is the parser service's authority for job status, attempts,
cancellation and results. This module is the only writer of the
``fin_parser`` schema; it reads and writes nothing else, and it never
imports another ``fin.*`` package.

Four properties are enforced here rather than left to the callers:

* **Idempotent acceptance.** A submission is keyed by
  ``idempotency_key``. The same key with the same input digest replays
  the SAME job; the same key with a different digest conflicts; a key
  whose job was deleted is refused by its tombstone.
* **Fenced ownership.** Every claim advances the job's fencing token
  under a row lock, so two workers can never hold the same effective
  attempt, and only the current effective token may commit a result.
* **Typed failure.** Every PostgreSQL failure becomes a
  ``ParserPersistenceError``; a raw driver exception never reaches a
  caller, and a refused operation never leaves half-written state.
* **Bounded work.** The pending queue is capped, and a job that waited
  or ran longer than its deadline is not claimable any more: it becomes
  ``timed_out`` with a typed error recorded on it. The service never
  accepts unbounded work.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Final

import psycopg

from fin.parser.db import parser_connection
from fin.parser.errors import (
    ParserEnvelopeError,
    ParserExecutionTimeoutError,
    ParserInvalidRequestError,
    ParserJobCancelledError,
    ParserJobCapacityError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserJobTombstonedError,
    ParserNotificationError,
    ParserPersistenceError,
    ParserQueueFullError,
    ParserQueueWaitTimeoutError,
    ParserStaleFenceError,
)
from fin.parser.ports import (
    Attempt,
    JobRecord,
    JobSubmission,
    Notification,
    OutboxEvent,
    ResultRecord,
    StagingArtifact,
    write_outbox_row,
)
from fin.parser.schema import ensure_parser_schema

DSN_ENV_VAR: Final[str] = "FIN_PARSER_DSN"
MAX_OPEN_JOBS_ENV_VAR: Final[str] = "FIN_PARSER_MAX_OPEN_JOBS"
MAX_PENDING_JOBS_ENV_VAR: Final[str] = "FIN_PARSER_MAX_PENDING_JOBS"
QUEUE_WAIT_SECONDS_ENV_VAR: Final[str] = "FIN_PARSER_QUEUE_WAIT_SECONDS"
EXECUTION_SECONDS_ENV_VAR: Final[str] = "FIN_PARSER_EXECUTION_SECONDS"

DEFAULT_MAX_OPEN_JOBS: Final[int] = 1000
DEFAULT_MAX_PENDING_JOBS: Final[int] = 20
DEFAULT_QUEUE_WAIT_SECONDS: Final[float] = 600.0
DEFAULT_EXECUTION_SECONDS: Final[float] = 900.0

# One advisory lock for every submission: the open-job limit and the
# pending cap are counts over the whole table, so the count and the insert
# that follows it must not interleave with another submission's count. The
# lock is transaction-scoped, so it is released by commit or rollback.
_SUBMIT_LOCK_KEY: Final[int] = 1_179_643_216

# The statuses that still represent open work. A cancelled job and a
# timed-out job are terminal: they may never regain worker ownership.
# A query passes them as a list, because the driver dumps a tuple as a
# record and a list as an array.
_OPEN_STATUSES: Final[tuple[str, ...]] = ("queued", "running", "cancel_requested")

# The statuses a worker may claim. A job that asked to be cancelled is
# durable, but no new attempt may take it.
_CLAIMABLE_STATUSES: Final[tuple[str, ...]] = ("queued", "running")

# The status of a job that waits in the queue.
_PENDING_STATUS: Final[str] = "queued"

# The statuses that refuse any later commit.
_CANCELLED_STATUSES: Final[tuple[str, ...]] = ("cancelled", "cancel_requested")

# The stable, versioned lifecycle events the outbox carries.
_EVENT_ACCEPTED: Final[str] = "parser.job.accepted.v1"
_EVENT_COMPLETED: Final[str] = "parser.job.completed.v1"
_EVENT_CANCEL_REQUESTED: Final[str] = "parser.job.cancel_requested.v1"
_EVENT_CANCELLED: Final[str] = "parser.job.cancelled.v1"
_EVENT_TIMED_OUT: Final[str] = "parser.job.timed_out.v1"

# The clock the deadlines are measured with. It is injected, so a test
# can cross a deadline without waiting for it.
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ParserLimits:
    """The bounds the parser service puts on the work it accepts.

    They are the operating guardrails of the parser service: an open
    job ceiling, a pending queue cap, a queue-wait deadline and an
    execution deadline. Each of them is a typed refusal, never a silent
    acceptance of unbounded work.
    """

    max_open_jobs: int = DEFAULT_MAX_OPEN_JOBS
    max_pending_jobs: int = DEFAULT_MAX_PENDING_JOBS
    queue_wait_seconds: float = DEFAULT_QUEUE_WAIT_SECONDS
    execution_seconds: float = DEFAULT_EXECUTION_SECONDS


def resolve_parser_limits(
    *,
    max_open_jobs: int | None = None,
    max_pending_jobs: int | None = None,
    queue_wait_seconds: float | None = None,
    execution_seconds: float | None = None,
) -> ParserLimits:
    """The limits, from the explicit values or from the environment.

    Every limit is read through this one helper, so the environment
    names of the parser service live in a single place.
    """
    return ParserLimits(
        max_open_jobs=_limit_int(
            max_open_jobs, MAX_OPEN_JOBS_ENV_VAR, DEFAULT_MAX_OPEN_JOBS, minimum=1
        ),
        max_pending_jobs=_limit_int(
            max_pending_jobs,
            MAX_PENDING_JOBS_ENV_VAR,
            DEFAULT_MAX_PENDING_JOBS,
            minimum=0,
        ),
        queue_wait_seconds=_limit_float(
            queue_wait_seconds, QUEUE_WAIT_SECONDS_ENV_VAR, DEFAULT_QUEUE_WAIT_SECONDS
        ),
        execution_seconds=_limit_float(
            execution_seconds, EXECUTION_SECONDS_ENV_VAR, DEFAULT_EXECUTION_SECONDS
        ),
    )


@dataclass(frozen=True)
class ParserStoreConfig:
    """The settings the parser store runs with.

    The settings travel together: the store takes one argument, so the
    configuration of the store is stated in one place instead of being
    spread over a constructor that grows with every new setting.

    A setting left at None is read from the environment when the store is
    built, so a caller that overrides one setting never restates the
    others. ``dataclasses.replace`` states the same thing for a config
    that already exists.
    """

    dsn: str | None = None
    max_open_jobs: int | None = None
    max_pending_jobs: int | None = None
    queue_wait_seconds: float | None = None
    execution_seconds: float | None = None
    clock: Clock | None = None

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, **overrides: Any
    ) -> "ParserStoreConfig":
        """The DSN the environment names, with the given overrides applied.

        Only the DSN is read here: every other setting left unset is
        read from the environment when the store is built.
        """
        source = os.environ if environ is None else environ
        dsn = (source.get(DSN_ENV_VAR) or "").strip() or None
        return _settings_with(cls(dsn=dsn), overrides)


# The settings a caller may state. An unknown name is refused rather
# than ignored, so a typo never looks like a setting that was applied.
_STORE_SETTING_NAMES: Final[frozenset[str]] = frozenset(
    field.name for field in fields(ParserStoreConfig)
)


@dataclass(frozen=True)
class JobInsert:
    """The job row one submission inserts.

    The values travel together so the insert is one argument: the
    idempotency key, the input digest, the serialised source reference
    and the declared format.
    """

    key: str
    digest: str
    payload: str | None
    declared_format: str | None


@dataclass(frozen=True)
class ResultCommit:
    """The result one attempt commits.

    The values travel together so the commit is one argument: the job it
    belongs to, the token that proves ownership, the serialised envelope
    and the reference to the derived output.
    """

    job_id: str
    fencing_token: int
    payload: str
    reference: str


class PostgresParserJobStore:
    """The parser service's durable job authority.

    Each public call opens its own short-lived connection: the parser
    service is a separate process from the rest of fin, and a short
    connection keeps the ownership transaction (row lock plus fencing
    token) exactly as long as it needs to.
    """

    def __init__(
        self,
        config: ParserStoreConfig | str | None = None,
        **overrides: Any,
    ) -> None:
        """Build the store from one settings object.

        The string and the keyword shims: the settings used to be
        constructor arguments, and callers in other modules still hand
        over a bare DSN or a limit keyword. Both are folded into one
        ``ParserStoreConfig`` here, so the configuration of the store
        lives in one place while those call sites keep working.
        """
        requested = _requested_config(config, overrides)
        # EXIT: a config that names no DSN falls back to the one the
        # environment names; a store without either is refused below.
        resolved = (
            requested.dsn
            if requested.dsn is not None
            else os.environ.get(DSN_ENV_VAR, "")
        ).strip()
        if not resolved:
            raise ParserPersistenceError(
                f"the parser store is not configured: set {DSN_ENV_VAR}"
            )
        self._dsn = resolved
        self._limits = resolve_parser_limits(
            max_open_jobs=requested.max_open_jobs,
            max_pending_jobs=requested.max_pending_jobs,
            queue_wait_seconds=requested.queue_wait_seconds,
            execution_seconds=requested.execution_seconds,
        )
        self._clock: Clock = (
            requested.clock if requested.clock is not None else _system_clock
        )
        try:
            ensure_parser_schema(resolved)
        except psycopg.Error as exc:
            raise ParserPersistenceError(
                "the parser store could not prepare its schema"
            ) from exc

    @property
    def connection_dsn(self) -> str:
        """The DSN this store writes to."""
        return self._dsn

    @property
    def limits(self) -> ParserLimits:
        """The bounds this store enforces."""
        return self._limits

    @property
    def max_open_jobs(self) -> int:
        """The open-job limit this store enforces."""
        return self._limits.max_open_jobs

    @property
    def max_pending_jobs(self) -> int:
        """The pending-queue cap this store enforces."""
        return self._limits.max_pending_jobs

    @property
    def queue_wait_seconds(self) -> float:
        """The queue-wait deadline this store enforces."""
        return self._limits.queue_wait_seconds

    @property
    def execution_seconds(self) -> float:
        """The execution deadline this store enforces."""
        return self._limits.execution_seconds

    @contextmanager
    def _guarded(self, action: str) -> Iterator[Any]:
        """Every public call's driver work runs through here.

        A typed parser error passes through untouched; anything the
        driver raises becomes ``ParserPersistenceError``. The connection
        context rolls the transaction back when the body raises, so a
        refused operation writes nothing.
        """
        with parser_connection(
            self._dsn,
            message=f"{action}: the parser store could not be reached",
        ) as conn:
            yield conn

    # -- acceptance --

    def submit_job(
        self,
        *,
        idempotency_key: str,
        input_sha256: str,
        source_ref: Mapping[str, object] | None,
        declared_format: str | None,
    ) -> JobSubmission:
        key = _require_text(idempotency_key, "idempotency_key")
        digest = _require_text(input_sha256, "input_sha256")
        payload = _jsonb_text(source_ref, "source_ref") if source_ref is not None else None
        with self._guarded("submit job") as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SUBMIT_LOCK_KEY,))
            existing = self._resolve_existing(conn, key, digest)
            if existing is not None:
                conn.commit()
                return existing
            self._refuse_over_the_limits(conn)
            try:
                with conn.transaction():
                    row = self._insert_job(
                        conn,
                        JobInsert(
                            key=key,
                            digest=digest,
                            payload=payload,
                            declared_format=declared_format,
                        ),
                    )
            except psycopg.errors.UniqueViolation:
                # Another writer took the key between the lookup and the
                # insert. The winning row decides the outcome.
                resolved = self._resolve_existing(conn, key, digest)
                if resolved is None:
                    raise ParserPersistenceError(
                        "submit job: the idempotency key was taken by another writer"
                    ) from None
                conn.commit()
                return resolved
            conn.commit()
            return JobSubmission(
                job_id=str(row[0]), status=str(row[1]), replayed=False
            )

    def _insert_job(self, conn: Any, job: JobInsert) -> Any:
        """Queue the job and write its acceptance event in one transaction."""
        moment = self._now()
        row = conn.execute(
            "INSERT INTO fin_parser.job "
            "(job_id, idempotency_key, input_sha256, source_ref, "
            " declared_format, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, 'queued', %s, %s) "
            "RETURNING job_id, status",
            (
                str(uuid.uuid4()),
                job.key,
                job.digest,
                job.payload,
                job.declared_format,
                moment,
                moment,
            ),
        ).fetchone()
        write_outbox_row(
            conn,
            OutboxEvent(
                event=_EVENT_ACCEPTED,
                job_id=str(row[0]),
                job_version=0,
                status=_PENDING_STATUS,
            ),
        )
        return row

    def _resolve_existing(
        self, conn: Any, key: str, digest: str
    ) -> JobSubmission | None:
        """The job already accepted under this key, if there is one."""
        if self._is_tombstoned(conn, key=key):
            raise ParserJobTombstonedError(
                "submit job: the idempotency key belongs to a deleted job"
            )
        row = conn.execute(
            "SELECT job_id, status, input_sha256 FROM fin_parser.job "
            "WHERE idempotency_key = %s",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if str(row[2]) != digest:
            raise ParserJobConflictError(
                "submit job: the idempotency key was already used with a "
                "different input digest"
            )
        return JobSubmission(job_id=str(row[0]), status=str(row[1]), replayed=True)

    def _refuse_over_the_limits(self, conn: Any) -> None:
        """Refuse work the open-job ceiling or the pending cap forbids."""
        # A queued job that waited past its deadline is no longer
        # pending, so it neither holds queue capacity nor blocks the
        # count below.
        self._expire_stale_queued(conn)
        open_jobs = self._count_open_jobs(conn)
        if open_jobs >= self._limits.max_open_jobs:
            raise ParserJobCapacityError(
                f"submit job: {open_jobs} open jobs already reach the limit "
                f"of {self._limits.max_open_jobs}"
            )
        pending = self._count_pending_jobs(conn)
        if pending >= self._limits.max_pending_jobs:
            raise ParserQueueFullError(
                f"submit job: {pending} pending jobs already reach the cap of "
                f"{self._limits.max_pending_jobs}",
                max_pending_jobs=self._limits.max_pending_jobs,
                pending_count=pending,
            )

    def _count_open_jobs(self, conn: Any) -> int:
        row = conn.execute(
            "SELECT count(*) FROM fin_parser.job WHERE status = ANY(%s)",
            (list(_OPEN_STATUSES),),
        ).fetchone()
        # EXIT: a count that answers no row counts nothing, so the open
        # count is zero rather than an error.
        return int(row[0]) if row is not None else 0

    def _count_pending_jobs(self, conn: Any) -> int:
        row = conn.execute(
            "SELECT count(*) FROM fin_parser.job WHERE status = 'queued'"
        ).fetchone()
        # EXIT: a count that answers no row counts nothing, so the pending
        # count is zero rather than an error.
        return int(row[0]) if row is not None else 0

    # -- reads --

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._guarded("read job") as conn:
            row = conn.execute(
                "SELECT job_id, idempotency_key, input_sha256, status, "
                "created_at, updated_at FROM fin_parser.job WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            conn.commit()
        # EXIT: no job carries that id, so the read answers None -- the one
        # meaning of None in this package.
        return None if row is None else _job_record(row)

    def list_open_jobs(self, *, limit: int) -> list[JobRecord]:
        if limit < 0:
            raise ParserInvalidRequestError("list open jobs: limit must not be negative")
        with self._guarded("list open jobs") as conn:
            self._expire_stale_queued(conn)
            rows = conn.execute(
                "SELECT job_id, idempotency_key, input_sha256, status, "
                "created_at, updated_at FROM fin_parser.job "
                "WHERE status = ANY(%s) "
                "ORDER BY created_at, job_id LIMIT %s",
                (list(_OPEN_STATUSES), limit),
            ).fetchall()
            conn.commit()
        return [_job_record(row) for row in rows]

    # -- attempts --

    def claim_attempt(
        self, *, job_id: str, worker_id: str, lease_seconds: float
    ) -> Attempt | None:
        worker = _require_text(worker_id, "worker_id")
        if lease_seconds <= 0:
            raise ParserInvalidRequestError(
                "claim attempt: lease_seconds must be positive"
            )
        with self._guarded("claim attempt") as conn:
            # The job row is locked first: the fencing token is derived from
            # the job's attempts, so two workers must not compute it at the
            # same time. The lock also orders the claim against a
            # concurrent cancel.
            row = conn.execute(
                "SELECT status, created_at FROM fin_parser.job WHERE job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                if self._is_tombstoned(conn, job_id=job_id):
                    raise ParserJobTombstonedError(
                        "claim attempt: the job was deleted"
                    )
                conn.commit()
                # EXIT: no job carries that id, so there is no work to
                # claim -- None answers "no such row", never a failure.
                return None
            status = str(row[0])
            if status in _CANCELLED_STATUSES:
                raise ParserJobCancelledError("claim attempt: the job was cancelled")
            if status not in _CLAIMABLE_STATUSES:
                conn.commit()
                # EXIT: a terminal job has no claimable work, so the claim
                # answers None instead of handing out an attempt.
                return None
            waited = self._elapsed(row[1])
            if waited > self._limits.queue_wait_seconds:
                self._time_out(
                    conn,
                    job_id=job_id,
                    error_code=ParserQueueWaitTimeoutError.code,
                )
                conn.commit()
                # The transaction is committed, so the durable timeout
                # stays; the rollback a raised exception triggers does
                # nothing when no transaction is open.
                raise ParserQueueWaitTimeoutError(
                    "claim attempt: the job waited longer than the queue deadline",
                    job_id=job_id,
                    waited_seconds=waited,
                    queue_wait_seconds=self._limits.queue_wait_seconds,
                )
            attempt = self._start_attempt(
                conn, job_id=job_id, worker=worker, lease_seconds=lease_seconds
            )
            conn.commit()
        return attempt

    def _start_attempt(
        self, conn: Any, *, job_id: str, worker: str, lease_seconds: float
    ) -> Attempt:
        """Take the attempt and move the job to ``running``."""
        counters = conn.execute(
            "SELECT COALESCE(MAX(fencing_token), 0), COALESCE(MAX(attempt_no), 0) "
            "FROM fin_parser.attempt WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        fencing_token = int(counters[0]) + 1
        attempt_no = int(counters[1]) + 1
        claimed = conn.execute(
            "INSERT INTO fin_parser.attempt "
            "(job_id, attempt_no, fencing_token, worker_id, state, lease_expires_at, claimed_at) "
            "VALUES (%s, %s, %s, %s, 'claimed', now() + make_interval(secs => %s), %s) "
            "RETURNING lease_expires_at",
            (job_id, attempt_no, fencing_token, worker, float(lease_seconds), self._now()),
        ).fetchone()
        conn.execute(
            "UPDATE fin_parser.job SET status = 'running', updated_at = now() "
            "WHERE job_id = %s",
            (job_id,),
        )
        return Attempt(
            job_id=job_id,
            attempt_no=attempt_no,
            fencing_token=fencing_token,
            worker_id=worker,
            lease_expires_at=claimed[0],
        )

    def renew_attempt(
        self, *, job_id: str, fencing_token: int, lease_seconds: float
    ) -> bool:
        if lease_seconds <= 0:
            raise ParserInvalidRequestError(
                "renew attempt: lease_seconds must be positive"
            )
        with self._guarded("renew attempt") as conn:
            # The job row is locked before the attempt row, in the same
            # order as the claim, so the two cannot deadlock.
            job = conn.execute(
                "SELECT status FROM fin_parser.job WHERE job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if job is None or str(job[0]) in _CANCELLED_STATUSES or str(job[0]) == "timed_out":
                conn.commit()
                # EXIT: a job that is gone, cancelled or timed out owns no
                # lease, so the renewal answers False.
                return False
            current = conn.execute(
                "SELECT fencing_token, (lease_expires_at > now()) FROM fin_parser.attempt "
                "WHERE job_id = %s ORDER BY fencing_token DESC LIMIT 1 FOR UPDATE",
                (job_id,),
            ).fetchone()
            if current is None or int(current[0]) != fencing_token or not bool(current[1]):
                conn.commit()
                # EXIT: the token is not the current effective one, or its
                # lease has expired, so the renewal answers False.
                return False
            conn.execute(
                "UPDATE fin_parser.attempt "
                "SET lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE job_id = %s AND fencing_token = %s",
                (float(lease_seconds), job_id, fencing_token),
            )
            conn.commit()
            return True

    def live_attempt_tokens(self, *, now: datetime) -> set[tuple[str, int]]:
        """The attempts that still hold their job's effective fencing token.

        The effective attempt of a job is the one with its highest token;
        it is live while its attempt row is open and its lease has not
        run out. A lease that expired frees its output, because the next
        claim carries a higher token.
        """
        with self._guarded("list live attempt tokens") as conn:
            rows = conn.execute(
                "SELECT job_id, fencing_token FROM ("
                " SELECT DISTINCT ON (job_id) job_id, fencing_token, state,"
                " lease_expires_at FROM fin_parser.attempt"
                " ORDER BY job_id, fencing_token DESC"
                ") AS effective"
                " WHERE state IN ('claimed', 'running') AND lease_expires_at > %s",
                (now,),
            ).fetchall()
            conn.commit()
        return {(str(row[0]), int(row[1])) for row in rows}

    def committed_result_refs(self) -> set[str]:
        """Every derived-output reference a committed result names."""
        with self._guarded("read committed result refs") as conn:
            rows = conn.execute("SELECT result_ref FROM fin_parser.result").fetchall()
            conn.commit()
        return {str(row[0]) for row in rows}

    def committed_result_tokens(self) -> set[tuple[str, int]]:
        """The attempt each committed result was written by."""
        with self._guarded("read committed result tokens") as conn:
            rows = conn.execute(
                "SELECT job_id, fencing_token FROM fin_parser.result"
            ).fetchall()
            conn.commit()
        return {(str(row[0]), int(row[1])) for row in rows}

    # -- cancellation --

    def cancel_job(self, job_id: str) -> JobRecord:
        with self._guarded("cancel job") as conn:
            row = conn.execute(
                "SELECT job_id, idempotency_key, input_sha256, status, "
                "created_at, updated_at FROM fin_parser.job WHERE job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                if self._is_tombstoned(conn, job_id=job_id):
                    raise ParserJobTombstonedError("cancel job: the job was deleted")
                raise ParserJobNotFoundError("cancel job: no job carries that id")
            if str(row[3]) != "cancelled":
                self._request_cancellation(conn, job_id=job_id)
                row = conn.execute(
                    "SELECT job_id, idempotency_key, input_sha256, status, "
                    "created_at, updated_at FROM fin_parser.job WHERE job_id = %s",
                    (job_id,),
                ).fetchone()
            conn.commit()
        return _job_record(row)

    def _request_cancellation(self, conn: Any, *, job_id: str) -> str:
        """Record the cancellation request and fence the live attempt.

        A job with a live attempt becomes ``cancel_requested``: the
        request is durable and visible, and the attempt can no longer
        commit. A job with no live attempt is ``cancelled`` at once.
        """
        live = conn.execute(
            "SELECT 1 FROM fin_parser.attempt WHERE job_id = %s "
            "AND state IN ('claimed', 'running') LIMIT 1",
            (job_id,),
        ).fetchone()
        target = "cancel_requested" if live is not None else "cancelled"
        conn.execute(
            "UPDATE fin_parser.job SET status = %s, updated_at = now() WHERE job_id = %s",
            (target, job_id),
        )
        if live is not None:
            self._fence_attempts(conn, job_id=job_id)
        write_outbox_row(
            conn,
            OutboxEvent(
                event=_EVENT_CANCEL_REQUESTED if live is not None else _EVENT_CANCELLED,
                job_id=job_id,
                job_version=self._job_version(conn, job_id=job_id),
                status=target,
            ),
        )
        return target

    def _fence_attempts(self, conn: Any, *, job_id: str) -> None:
        """End the live attempts of a job that asked to be cancelled.

        The attempt leaves the claimable states and its lease ends now,
        so the worker stops and can no longer commit.
        """
        conn.execute(
            "UPDATE fin_parser.attempt SET state = 'expired', completed_at = now(), "
            "lease_expires_at = now() WHERE job_id = %s AND state IN ('claimed', 'running')",
            (job_id,),
        )

    # -- results --

    def commit_result(
        self,
        *,
        job_id: str,
        fencing_token: int,
        envelope: Mapping[str, object],
        result_ref: str,
    ) -> ResultRecord:
        payload = _jsonb_text(envelope, "envelope")
        reference = _require_text(result_ref, "result_ref")
        with self._guarded("commit result") as conn:
            self._refuse_uncommittable(conn, job_id=job_id, fencing_token=fencing_token)
            running = self._running_seconds(
                conn, job_id=job_id, fencing_token=fencing_token
            )
            if running is not None and running > self._limits.execution_seconds:
                self._time_out(
                    conn,
                    job_id=job_id,
                    error_code=ParserExecutionTimeoutError.code,
                )
                conn.commit()
                # The transaction is committed, so the durable timeout
                # stays; the rollback a raised exception triggers does
                # nothing when no transaction is open.
                raise ParserExecutionTimeoutError(
                    "commit result: the attempt ran longer than the execution deadline",
                    job_id=job_id,
                    running_seconds=running,
                    execution_seconds=self._limits.execution_seconds,
                )
            record = self._store_result(
                conn,
                ResultCommit(
                    job_id=job_id,
                    fencing_token=fencing_token,
                    payload=payload,
                    reference=reference,
                ),
            )
            conn.commit()
        return record

    def _refuse_uncommittable(
        self, conn: Any, *, job_id: str, fencing_token: int
    ) -> None:
        """Refuse a commit the job no longer authorizes.

        The job is locked here, so a cancellation that commits first is
        seen, and the fencing token is compared with the job's current
        effective one: a superseded attempt writes nothing.
        """
        job = conn.execute(
            "SELECT status FROM fin_parser.job WHERE job_id = %s FOR UPDATE",
            (job_id,),
        ).fetchone()
        if job is None:
            if self._is_tombstoned(conn, job_id=job_id):
                raise ParserJobTombstonedError("commit result: the job was deleted")
            raise ParserJobNotFoundError("commit result: no job carries that id")
        if str(job[0]) in _CANCELLED_STATUSES:
            raise ParserJobCancelledError("commit result: the job was cancelled")
        if str(job[0]) == "timed_out":
            # The job already gave up: a late result may not revive it.
            raise ParserExecutionTimeoutError(
                "commit result: the job already timed out"
            )
        effective = conn.execute(
            "SELECT COALESCE(MAX(fencing_token), 0), count(*) "
            "FROM fin_parser.attempt WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        # A job with no attempt has no effective token at all, so no
        # token can be the effective one.
        if int(effective[1]) == 0 or int(effective[0]) != fencing_token:
            raise ParserStaleFenceError(
                "commit result: the fencing token is no longer the effective one"
            )

    def _running_seconds(
        self, conn: Any, *, job_id: str, fencing_token: int
    ) -> float | None:
        """How long the attempt has been running, or None when it is not one."""
        row = conn.execute(
            "SELECT claimed_at FROM fin_parser.attempt "
            "WHERE job_id = %s AND fencing_token = %s",
            (job_id, fencing_token),
        ).fetchone()
        if row is None:
            # EXIT: no attempt carries that token, so there is no runtime to
            # measure -- the deadline cannot be exceeded by a run that never
            # started.
            return None
        return self._elapsed(row[0])

    def _store_result(self, conn: Any, commit: ResultCommit) -> ResultRecord:
        """Store the result, close the attempt and the job, and announce it.

        The result, the terminal status and the completion event are one
        transaction, so a crash can never leave a durable result without
        its completion event.
        """
        existing = conn.execute(
            "SELECT fencing_token, envelope, result_ref, created_at "
            "FROM fin_parser.result WHERE job_id = %s",
            (commit.job_id,),
        ).fetchone()
        if existing is not None:
            if int(existing[0]) != commit.fencing_token:
                raise ParserStaleFenceError(
                    "commit result: a result was already committed for another token"
                )
            return _result_record(commit.job_id, existing)
        # The payload is read before the first write: a result the store
        # cannot read back never reaches a durable row.
        document = _committed_document(commit.payload)
        outcome = _terminal_outcome(document)
        created = conn.execute(
            "INSERT INTO fin_parser.result "
            "(job_id, fencing_token, envelope, result_ref) "
            "VALUES (%s, %s, %s::jsonb, %s) RETURNING created_at",
            (commit.job_id, commit.fencing_token, commit.payload, commit.reference),
        ).fetchone()
        conn.execute(
            "UPDATE fin_parser.attempt SET state = 'succeeded', completed_at = now() "
            "WHERE job_id = %s AND fencing_token = %s",
            (commit.job_id, commit.fencing_token),
        )
        conn.execute(
            "UPDATE fin_parser.job SET status = %s, updated_at = now() WHERE job_id = %s",
            (outcome, commit.job_id),
        )
        write_outbox_row(
            conn,
            OutboxEvent(
                event=_EVENT_COMPLETED,
                job_id=commit.job_id,
                job_version=commit.fencing_token,
                status=outcome,
                result_ref=commit.reference,
            ),
        )
        return ResultRecord(
            job_id=commit.job_id,
            fencing_token=commit.fencing_token,
            envelope=document,
            result_ref=commit.reference,
            created_at=created[0],
        )

    def read_result(self, job_id: str) -> ResultRecord | None:
        with self._guarded("read result") as conn:
            row = conn.execute(
                "SELECT job_id, fencing_token, envelope, result_ref, created_at "
                "FROM fin_parser.result WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            conn.commit()
        if row is None:
            # EXIT: the job owns no result, so the read answers None -- the
            # one meaning of None in this package.
            return None
        return ResultRecord(
            job_id=str(row[0]),
            fencing_token=int(row[1]),
            # EXIT: a stored payload that is not an object carries no
            # readable envelope, so the record answers an empty mapping.
            envelope=dict(row[2]) if isinstance(row[2], Mapping) else {},
            result_ref=str(row[3]),
            created_at=row[4],
        )

    # -- deletion --

    def delete_job(self, job_id: str) -> None:
        with self._guarded("delete job") as conn:
            row = conn.execute(
                "SELECT job_id, idempotency_key, input_sha256 FROM fin_parser.job "
                "WHERE job_id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row is None:
                if self._is_tombstoned(conn, job_id=job_id):
                    # EXIT: the job was already deleted, so the delete is
                    # idempotent -- there is nothing left to delete, and
                    # the one tombstone still protects the key.
                    conn.commit()
                    return
                raise ParserJobNotFoundError("delete job: no job carries that id")
            # The tombstone is written in the same transaction as the
            # delete, so a replay can never observe a window in which the
            # job is gone and the tombstone is not yet there.
            conn.execute(
                "INSERT INTO fin_parser.tombstone "
                "(job_id, idempotency_key, input_sha256) VALUES (%s, %s, %s) "
                "ON CONFLICT (job_id) DO NOTHING",
                (row[0], row[1], row[2]),
            )
            conn.execute("DELETE FROM fin_parser.job WHERE job_id = %s", (job_id,))
            conn.commit()

    # -- notifications --

    def enqueue_notification(
        self, *, topic: str, payload: Mapping[str, object]
    ) -> str:
        name = _require_text(topic, "topic")
        body = _jsonb_text(payload, "payload")
        with self._guarded("enqueue notification") as conn:
            notification_id = write_outbox_row(
                conn,
                OutboxEvent(
                    event=name,
                    job_id=_payload_text(body, "job_id"),
                    job_version=_payload_int(body, "job_version"),
                    status=_payload_text(body, "status"),
                    result_ref=_payload_optional_text(body, "result_ref"),
                ),
            )
            conn.commit()
        return notification_id

    def claim_unpublished_notifications(self, *, limit: int) -> list[Notification]:
        if limit < 0:
            raise ParserInvalidRequestError(
                "claim unpublished notifications: limit must not be negative"
            )
        with self._guarded("claim unpublished notifications") as conn:
            rows = conn.execute(
                "SELECT notification_id, topic, payload, created_at "
                "FROM fin_parser.outbox WHERE published_at IS NULL "
                "ORDER BY created_at, notification_id LIMIT %s",
                (limit,),
            ).fetchall()
            conn.commit()
        return [
            Notification(
                notification_id=str(row[0]),
                topic=str(row[1]),
                # EXIT: a stored payload that is not an object carries no
                # readable body, so the notification answers an empty map.
                payload=dict(row[2]) if isinstance(row[2], Mapping) else {},
                created_at=row[3],
            )
            for row in rows
        ]

    def mark_notification_published(self, notification_id: str) -> None:
        with self._guarded("mark notification published") as conn:
            row = conn.execute(
                "UPDATE fin_parser.outbox SET published_at = now() "
                "WHERE notification_id = %s RETURNING notification_id",
                (notification_id,),
            ).fetchone()
            conn.commit()
        if row is None:
            raise ParserNotificationError(
                "mark notification published: no such notification"
            )

    def record_notification_received(self, notification_id: str) -> bool:
        with self._guarded("record notification received") as conn:
            row = conn.execute(
                "INSERT INTO fin_parser.inbox (notification_id) VALUES (%s) "
                "ON CONFLICT (notification_id) DO NOTHING RETURNING notification_id",
                (notification_id,),
            ).fetchone()
            conn.commit()
        return row is not None

    def unreconciled_notification_ids(self) -> list[str]:
        with self._guarded("list unreconciled notifications") as conn:
            rows = conn.execute(
                "SELECT notification_id FROM fin_parser.outbox AS o "
                "WHERE o.published_at IS NULL "
                "   OR NOT EXISTS (SELECT 1 FROM fin_parser.inbox AS i "
                "                  WHERE i.notification_id = o.notification_id) "
                "ORDER BY created_at, notification_id"
            ).fetchall()
            conn.commit()
        return [str(row[0]) for row in rows]

    # -- staging --

    def register_staging_artifact(
        self, *, job_id: str, fencing_token: int, path: str
    ) -> str:
        location = _require_text(path, "path")
        with self._guarded("register staging artifact") as conn:
            row = conn.execute(
                "INSERT INTO fin_parser.staging_artifact "
                "(artifact_id, job_id, fencing_token, path) "
                "VALUES (%s, %s, %s, %s) RETURNING artifact_id",
                (str(uuid.uuid4()), job_id, fencing_token, location),
            ).fetchone()
            conn.commit()
        return str(row[0])

    def list_staging_artifacts(
        self, *, job_id: str | None = None
    ) -> list[StagingArtifact]:
        with self._guarded("list staging artifacts") as conn:
            if job_id is None:
                rows = conn.execute(
                    "SELECT artifact_id, job_id, fencing_token, path, referenced, "
                    "created_at FROM fin_parser.staging_artifact "
                    "ORDER BY created_at, artifact_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT artifact_id, job_id, fencing_token, path, referenced, "
                    "created_at FROM fin_parser.staging_artifact WHERE job_id = %s "
                    "ORDER BY created_at, artifact_id",
                    (job_id,),
                ).fetchall()
            conn.commit()
        return [
            StagingArtifact(
                artifact_id=str(row[0]),
                job_id=str(row[1]),
                fencing_token=int(row[2]),
                path=str(row[3]),
                referenced=bool(row[4]),
                created_at=row[5],
            )
            for row in rows
        ]

    def mark_staging_referenced(self, artifact_id: str) -> None:
        with self._guarded("mark staging referenced") as conn:
            row = conn.execute(
                "UPDATE fin_parser.staging_artifact SET referenced = TRUE "
                "WHERE artifact_id = %s RETURNING artifact_id",
                (artifact_id,),
            ).fetchone()
            conn.commit()
        if row is None:
            raise ParserJobNotFoundError(
                "mark staging referenced: no such artifact"
            )

    def drop_staging_artifact(self, artifact_id: str) -> None:
        with self._guarded("drop staging artifact") as conn:
            conn.execute(
                "DELETE FROM fin_parser.staging_artifact WHERE artifact_id = %s",
                (artifact_id,),
            )
            conn.commit()

    # -- helpers --

    def _is_tombstoned(
        self, conn: Any, *, key: str | None = None, job_id: str | None = None
    ) -> bool:
        if key is not None:
            row = conn.execute(
                "SELECT 1 FROM fin_parser.tombstone WHERE idempotency_key = %s LIMIT 1",
                (key,),
            ).fetchone()
            return row is not None
        row = conn.execute(
            "SELECT 1 FROM fin_parser.tombstone WHERE job_id = %s LIMIT 1", (job_id,)
        ).fetchone()
        return row is not None

    def _now(self) -> datetime:
        """The store's clock, in UTC."""
        moment = self._clock()
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)

    def _elapsed(self, since: datetime) -> float:
        """The seconds between a stored moment and the store's clock."""
        start = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        return (self._now() - start).total_seconds()

    def _job_version(self, conn: Any, *, job_id: str) -> int:
        """The job's version counter: its current effective fencing token."""
        row = conn.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) FROM fin_parser.attempt WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        # EXIT: a job with no attempt has no version yet, so the counter is
        # zero -- the version the acceptance event carries.
        return int(row[0]) if row is not None else 0

    def _time_out(self, conn: Any, *, job_id: str, error_code: str) -> None:
        """Make the job ``timed_out`` and record the typed error on it.

        The attempt of the job keeps its own lifecycle: the worker owns
        the end of its attempt, and the store only refuses the result.
        """
        conn.execute(
            "UPDATE fin_parser.job SET status = 'timed_out', updated_at = now() "
            "WHERE job_id = %s",
            (job_id,),
        )
        write_outbox_row(
            conn,
            OutboxEvent(
                event=_EVENT_TIMED_OUT,
                job_id=job_id,
                job_version=self._job_version(conn, job_id=job_id),
                status="timed_out",
                error_code=error_code,
            ),
        )

    def _expire_stale_queued(self, conn: Any) -> None:
        """Make the queued jobs that waited too long ``timed_out``."""
        rows = conn.execute(
            "SELECT job_id, created_at FROM fin_parser.job WHERE status = 'queued'"
        ).fetchall()
        for row in rows:
            if self._elapsed(row[1]) > self._limits.queue_wait_seconds:
                self._time_out(
                    conn,
                    job_id=str(row[0]),
                    error_code=ParserQueueWaitTimeoutError.code,
                )


# ----- module helpers -----

def _system_clock() -> datetime:
    """The wall clock, in UTC."""
    return datetime.now(timezone.utc)


def _requested_config(
    config: ParserStoreConfig | str | None, overrides: Mapping[str, Any]
) -> ParserStoreConfig:
    """The settings the caller asked for.

    A bare DSN string and the legacy limit keywords are folded into one
    settings object, so the store reads one configuration.
    """
    if config is None:
        base = ParserStoreConfig()
    elif isinstance(config, ParserStoreConfig):
        base = config
    elif isinstance(config, str):
        base = ParserStoreConfig(dsn=config)
    else:
        raise ParserInvalidRequestError(
            "the parser store takes a ParserStoreConfig or a DSN string"
        )
    return _settings_with(base, overrides)


def _settings_with(
    base: ParserStoreConfig, overrides: Mapping[str, Any]
) -> ParserStoreConfig:
    """The settings with the given ones replaced.

    An unknown setting is a typed refusal, not a silent no-op: a typo in
    a setting name must never look like a setting that was applied.
    """
    unknown = sorted(set(overrides) - _STORE_SETTING_NAMES)
    if unknown:
        raise ParserInvalidRequestError(
            f"unknown parser store setting: {', '.join(unknown)}"
        )
    return replace(base, **overrides)


def _limit_int(
    value: int | None, env_var: str, default: int, *, minimum: int
) -> int:
    """One limit, from the explicit value or from the environment."""
    if value is not None:
        parsed = value
    else:
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            # EXIT: the environment names no value, so the documented
            # default applies.
            return default
        try:
            parsed = int(raw)
        except ValueError as exc:
            raise ParserInvalidRequestError(
                f"{env_var} must be a whole number"
            ) from exc
    if parsed < minimum:
        raise ParserInvalidRequestError(
            f"{env_var} must not be below {minimum}"
        )
    return parsed


def _limit_float(
    value: float | None, env_var: str, default: float
) -> float:
    """One deadline, from the explicit value or from the environment."""
    if value is not None:
        parsed = value
    else:
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            # EXIT: the environment names no value, so the documented
            # default applies.
            return default
        try:
            parsed = float(raw)
        except ValueError as exc:
            raise ParserInvalidRequestError(
                f"{env_var} must be a number"
            ) from exc
    if parsed <= 0:
        raise ParserInvalidRequestError(f"{env_var} must be a positive number")
    return parsed


def _require_text(value: str, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ParserInvalidRequestError(f"{field} must be a non-empty string")
    return text


def _jsonb_text(value: Mapping[str, object], field: str) -> str:
    try:
        return json.dumps(dict(value))
    except (TypeError, ValueError) as exc:
        raise ParserInvalidRequestError(
            f"{field} must be JSON-serializable"
        ) from exc


def _terminal_outcome(document: Mapping[str, object]) -> str:
    """The terminal status a committed result earns.

    A result that reports partial coverage is ``partial``; every other
    result is ``succeeded``. The store reads the coverage the envelope
    carries and does not second-guess the envelope contract.
    """
    coverage = document.get("coverage")
    if not isinstance(coverage, Mapping):
        # EXIT: a result that reports no coverage object is not partial, so
        # the terminal status is a plain success.
        return "succeeded"
    return "partial" if coverage.get("status") == "partial" else "succeeded"


def _committed_document(payload: str) -> Mapping[str, object]:
    """The committed payload read back as a JSON object.

    EXIT: a payload that cannot be read back is not a valid result, so the
    commit raises instead of recording a success it cannot justify.
    """
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise ParserEnvelopeError(
            "commit result: the committed payload is not valid JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise ParserEnvelopeError(
            "commit result: the committed payload is not a JSON object"
        )
    return document


def _payload_text(body: str, field: str) -> str:
    """One text field of a notification body, empty when it carries none."""
    value = _payload_value(body, field)
    if value is None:
        # EXIT: the body carries no such field, so the row records the
        # empty value -- the absence is the caller's, not a parse failure.
        return ""
    if not isinstance(value, str):
        raise ParserInvalidRequestError(
            f"the notification payload field {field} must be a string"
        )
    return value


def _payload_optional_text(body: str, field: str) -> str | None:
    """One optional text field of a notification body."""
    value = _payload_value(body, field)
    if value is None:
        # EXIT: an absent or null optional field carries no value, which is
        # the documented meaning of "this event carries no result".
        return None
    if not isinstance(value, str):
        raise ParserInvalidRequestError(
            f"the notification payload field {field} must be a string"
        )
    return value


def _payload_int(body: str, field: str) -> int:
    """One whole-number field of a notification body."""
    value = _payload_value(body, field)
    if value is None:
        # EXIT: the body carries no such field, so the row records zero --
        # the absence is the caller's, not a parse failure.
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise ParserInvalidRequestError(
            f"the notification payload field {field} must be a whole number"
        )
    return value


def _payload_value(body: str, field: str) -> object:
    """One field of a notification body.

    EXIT: a body that is not a JSON object is malformed input, so the
    store raises instead of writing an empty field into a durable row.
    """
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise ParserInvalidRequestError(
            "the notification payload is not valid JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise ParserInvalidRequestError(
            "the notification payload is not a JSON object"
        )
    return document.get(field)


def _job_record(row: Any) -> JobRecord:
    return JobRecord(
        job_id=str(row[0]),
        idempotency_key=str(row[1]),
        input_sha256=str(row[2]),
        status=str(row[3]),
        created_at=row[4],
        updated_at=row[5],
    )


def _result_record(job_id: str, row: Any) -> ResultRecord:
    return ResultRecord(
        job_id=job_id,
        fencing_token=int(row[0]),
        # EXIT: a stored envelope that is not an object carries no readable
        # envelope, so the record answers an empty mapping.
        envelope=dict(row[1]) if isinstance(row[1], Mapping) else {},
        result_ref=str(row[2]),
        created_at=row[3],
    )


__all__ = [
    "DEFAULT_EXECUTION_SECONDS",
    "DEFAULT_MAX_OPEN_JOBS",
    "DEFAULT_MAX_PENDING_JOBS",
    "DEFAULT_QUEUE_WAIT_SECONDS",
    "DSN_ENV_VAR",
    "EXECUTION_SECONDS_ENV_VAR",
    "MAX_OPEN_JOBS_ENV_VAR",
    "MAX_PENDING_JOBS_ENV_VAR",
    "QUEUE_WAIT_SECONDS_ENV_VAR",
    "Clock",
    "JobInsert",
    "ParserLimits",
    "ParserStoreConfig",
    "PostgresParserJobStore",
    "ResultCommit",
    "resolve_parser_limits",
]
