"""Worker attempt authority: claim, lease, fence, commit, cancel (T6/T7).

(contract §5 -- docs/decisions/p2-parser-service-contract.md §5)

The worker side of the parser service owns ONE thing: which attempt may act
on a job right now. Everything about a parse is injected, so this module
knows nothing about adapters, formats or providers -- it only knows how to
take a lease, keep it, give it up, and refuse a commit that belongs to a
superseded attempt.

The fencing rules live in the frozen store, and this module codes AGAINST
that protocol (``fin.parser.ports``) rather than defining a second one:

* ``fencing_token`` is a per-JOB counter that starts at 1 and only increases.
* A claim runs in ONE transaction that locks the job row, so two workers can
  never hold the same effective attempt: every claim advances the token, and
  the earlier token is stale from that moment on. A claim on a job whose
  latest lease has expired is therefore allowed and produces a HIGHER token;
  the lease bounds the RENEWAL, and the token decides who may still commit.
* A renewal succeeds only while the token is still the job's current
  effective token and its lease has not expired.
* A commit succeeds only when the supplied token equals the job's current
  effective token and the job is neither cancelled nor tombstoned. A stale
  claim writes NOTHING.
* A cancellation is durable: the job stops being claimable, the worker
  finalizes the recorded request as ``cancelled``, and no later action can
  make the job regain worker ownership.
* A run is bounded: an attempt that outruns its execution deadline commits
  nothing and ends as a typed failure.

What this module adds on top of the frozen store is the WORKER's own
bookkeeping on the attempt row: marking the attempt running, giving it up,
and recording that it ended without a result. The frozen protocol names
claiming, renewing, committing and cancelling, but not those transitions, so
they are declared here as a narrow seam and implemented against the
parser-owned schema.

The producer is injected for the same reason: the worker knows about leases
and fencing tokens, and nothing at all about how a document is parsed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

import psycopg

from fin.parser.db import parser_connection
from fin.parser.errors import (
    ParserError,
    ParserExecutionTimeoutError,
    ParserInvalidRequestError,
    ParserJobCancelledError,
    ParserJobConflictError,
    ParserJobNotFoundError,
    ParserPersistenceError,
    ParserStaleFenceError,
)
from fin.parser.ports import Attempt, JobRecord, ParserJobStore, ResultRecord
from fin.parser.schema import ensure_parser_schema

logger = logging.getLogger(__name__)

# The durable status vocabulary this worker acts on.  A cancellation is first
# REQUESTED, so the attempt that is still running can be fenced off, and only
# then does the job become CANCELLED.
CANCEL_REQUESTED: Final[str] = "cancel_requested"
CANCELLED: Final[str] = "cancelled"

__all__ = [
    "AttemptResult",
    "CANCELLED",
    "CANCEL_REQUESTED",
    "LeaseHeartbeat",
    "ParserWorker",
    "ParserWorkerConfig",
    "PostgresWorkerAttemptStates",
    "WorkerAttemptStates",
]


def _failure_label(exc: BaseException) -> str:
    """The stable code of a typed failure, or the class name of an unknown one."""
    if isinstance(exc, ParserError):
        return exc.code
    return type(exc).__name__


@dataclass(frozen=True)
class AttemptResult:
    """What an injected producer returns.

    The worker does not interpret the envelope: it hands the produced
    envelope and its derived-output reference to the commit, and the fence
    decides whether they may be stored.
    """

    envelope: Mapping[str, object]
    result_ref: str


@runtime_checkable
class WorkerAttemptStates(Protocol):
    """The worker-side attempt transitions the frozen store does not name.

    These transitions touch ONE attempt row -- the caller's own -- and never
    decide ownership: the fence belongs to the store.
    """

    def mark_running(self, *, job_id: str, fencing_token: int) -> bool:
        """Record that the attempt started working."""
        ...

    def release_attempt(self, *, job_id: str, fencing_token: int) -> bool:
        """Give the attempt up, ending its lease now."""
        ...

    def fail_attempt(self, *, job_id: str, fencing_token: int) -> bool:
        """Record that the attempt ended without a result."""
        ...


class PostgresWorkerAttemptStates:
    """The worker-side attempt transitions, in PostgreSQL.

    The schema owner is ``fin.parser.schema``; this class asks it for the
    DDL instead of carrying a second copy of it.
    """

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ParserInvalidRequestError("a PostgreSQL DSN is required")
        self._dsn = dsn
        try:
            ensure_parser_schema(dsn)
        except psycopg.Error as exc:
            raise ParserPersistenceError(
                "the worker could not prepare the parser schema"
            ) from exc

    @property
    def dsn(self) -> str:
        """The durable connection string these transitions write to."""
        return self._dsn

    @contextmanager
    def _guarded(self, action: str) -> Iterator[Any]:
        with parser_connection(
            self._dsn,
            message=f"{action}: the parser store could not be reached",
        ) as conn:
            yield conn

    def mark_running(self, *, job_id: str, fencing_token: int) -> bool:
        """Record that the attempt started working."""
        with self._guarded("mark attempt running") as conn:
            row = conn.execute(
                "UPDATE fin_parser.attempt SET state = 'running'"
                " WHERE job_id = %s AND fencing_token = %s AND state = 'claimed'"
                " RETURNING fencing_token",
                (job_id, fencing_token),
            ).fetchone()
            conn.commit()
        return row is not None

    def release_attempt(self, *, job_id: str, fencing_token: int) -> bool:
        """Give the attempt up.

        The lease ends now instead of waiting for its deadline: the worker
        says it is done, so the next worker may take the job over at once.
        """
        return self._end(job_id=job_id, fencing_token=fencing_token, state="expired")

    def fail_attempt(self, *, job_id: str, fencing_token: int) -> bool:
        """Record that the attempt ended without a result."""
        return self._end(job_id=job_id, fencing_token=fencing_token, state="failed")

    def _end(self, *, job_id: str, fencing_token: int, state: str) -> bool:
        with self._guarded("end attempt") as conn:
            row = conn.execute(
                "UPDATE fin_parser.attempt"
                " SET state = %s, completed_at = now(), lease_expires_at = now()"
                " WHERE job_id = %s AND fencing_token = %s"
                " AND state IN ('claimed', 'running') RETURNING fencing_token",
                (state, job_id, fencing_token),
            ).fetchone()
            conn.commit()
        return row is not None


class LeaseHeartbeat:
    """Renews one attempt's lease while its producer runs.

    The producer is opaque to the worker, so the worker cannot know when the
    lease would otherwise lapse. The renewal runs on its own thread; the
    commit still decides whether the result may be stored, and a lease that
    was lost stops the worker before it commits anything.
    """

    def __init__(
        self,
        authority: ParserJobStore,
        attempt: Attempt,
        *,
        interval_seconds: float,
        lease_seconds: float,
    ) -> None:
        self._authority = authority
        self._attempt = attempt
        self._interval = interval_seconds
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lost = False
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        """Whether the lease was lost while the producer was running."""
        return self._lost

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._loop, name="fin-parser-lease", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self._confirm_lease()

    def _confirm_lease(self) -> None:
        """Renew once more, so the lease state is known when the block ends.

        The loop polls on an interval, so the producer can finish between
        two renewals and the loop would never see the loss. One renewal at
        exit answers the question the loop answers: may this attempt still
        act on the job?
        """
        self._renew_lease()

    def _renew_lease(self) -> bool:
        """Renew the lease once and report the outcome.

        True while the attempt may still act on the job, False once it may
        not. The caller decides whether that ends its own work, so the loop
        stops and the exit confirmation does not.
        """
        try:
            renewed = self._authority.renew_attempt(
                job_id=self._attempt.job_id,
                fencing_token=self._attempt.fencing_token,
                lease_seconds=self._lease_seconds,
            )
        except ParserError as exc:
            # EXIT: a typed store failure is reported with its stable code, so
            # an unreachable store is never read as a superseded lease — and
            # the attempt stops acting on the job either way.
            self._report_failure(exc, exc.code)
            return False
        except Exception as exc:
            # EXIT: an unknown fault is reported by its class name, and the
            # attempt stops acting on the job either way.
            self._report_failure(exc, type(exc).__name__)
            return False
        if not renewed:
            # EXIT: the lease is no longer the effective one, so the attempt
            # may not act on the job any more and the loss is recorded.
            logger.warning(
                "the lease of parser job %s (token %s) is no longer the effective one",
                self._attempt.job_id,
                self._attempt.fencing_token,
            )
            self._lost = True
            return False
        return True

    def _report_failure(self, exc: BaseException, kind: str) -> None:
        """Record a renewal failure, with the connection string removed.

        A lost lease must be visible: it is the difference between an
        attempt that may still act and one that must stop.  The kind is the
        stable code of a typed failure, and the class name of an unknown
        fault.
        """
        logger.warning(
            "the lease of parser job %s (token %s) could not be renewed (%s): %s",
            self._attempt.job_id,
            self._attempt.fencing_token,
            kind,
            self._safe_message(exc),
        )
        self._lost = True

    def _safe_message(self, exc: BaseException) -> str:
        """The failure text without the store's connection string."""
        message = str(exc)
        dsn = getattr(self._authority, "connection_dsn", None)
        if isinstance(dsn, str) and dsn:
            message = message.replace(dsn, "<redacted>")
        return message

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._renew_lease():
                # EXIT: the lease is gone or could not be renewed, so this
                # thread stops renewing.
                return


@dataclass(frozen=True)
class ParserWorkerConfig:
    """One worker's authority, its attempt bookkeeping, and its timing.

    The settings travel together: the worker takes one argument, so the
    configuration of a worker is stated in one place instead of being spread
    over a call that grows with every new setting.
    """

    authority: ParserJobStore
    states: WorkerAttemptStates
    worker_id: str
    lease_seconds: float = 60.0
    producer: Callable[[Attempt], AttemptResult] | None = None
    renew_interval: float | None = None


class ParserWorker:
    """Claims an attempt, runs an injected producer, commits under the fence.

    The producer is injected on purpose: this worker knows about leases and
    fencing tokens, and nothing at all about how a document is parsed.
    """

    def __init__(self, config: ParserWorkerConfig) -> None:
        if not config.worker_id.strip():
            raise ParserInvalidRequestError("a worker id is required")
        if config.lease_seconds <= 0:
            raise ParserInvalidRequestError("a lease must last longer than zero seconds")
        if config.renew_interval is not None and config.renew_interval <= 0:
            raise ParserInvalidRequestError("a renewal interval must be positive")
        self._config = config

    @property
    def worker_id(self) -> str:
        return self._config.worker_id

    @property
    def lease_seconds(self) -> float:
        return self._config.lease_seconds

    def claim(self, job_id: str) -> Attempt:
        """Claim the job, refusing it when it has no claimable work."""
        try:
            attempt = self._config.authority.claim_attempt(
                job_id=job_id,
                worker_id=self._config.worker_id,
                lease_seconds=self._config.lease_seconds,
            )
        except ParserJobCancelledError:
            # The cancellation is already durable, so the job is finished;
            # the worker only has to make that terminal state explicit, and
            # a failure to do so never replaces the cancellation.
            self._finalize_quietly(job_id)
            raise
        if attempt is None:
            # EXIT: the job has no claimable work — it is unknown or already
            # terminal — and those are different failures.
            job = self._config.authority.get_job(job_id)
            if job is None:
                raise ParserJobNotFoundError(f"no parser job {job_id}")
            if job.status == CANCEL_REQUESTED:
                # The cancellation is durable; making its terminal state
                # explicit must not replace the cancellation itself.
                self._finalize_quietly(job_id)
                raise ParserJobCancelledError(f"parser job {job_id} was cancelled")
            raise ParserJobConflictError(
                f"parser job {job_id} has no claimable work (status {job.status})"
            )
        return attempt

    def finalize_cancelled(self, job_id: str) -> JobRecord:
        """Turn a recorded cancellation request into the cancelled status.

        A cancellation is requested first, so an attempt that is still running
        can be fenced off, and the job is CANCELLED once nobody may act on it
        any more.  This is the durable end of that request.
        """
        job = self._config.authority.get_job(job_id)
        if job is None:
            raise ParserJobNotFoundError(f"no parser job {job_id}")
        if job.status == CANCELLED:
            return job
        if job.status != CANCEL_REQUESTED:
            raise ParserJobConflictError(
                f"parser job {job_id} awaits no cancellation (status {job.status})"
            )
        record = self._config.authority.cancel_job(job_id)
        if record.status != CANCELLED:
            raise ParserJobConflictError(
                f"parser job {job_id} did not reach the cancelled status "
                f"(status {record.status})"
            )
        return record

    def renew(self, attempt: Attempt) -> bool:
        """Extend the attempt's lease while it is still the effective one."""
        return self._config.authority.renew_attempt(
            job_id=attempt.job_id,
            fencing_token=attempt.fencing_token,
            lease_seconds=self._config.lease_seconds,
        )

    def release(self, attempt: Attempt) -> bool:
        """Give the attempt up so another worker may take the job over."""
        return self._config.states.release_attempt(
            job_id=attempt.job_id, fencing_token=attempt.fencing_token
        )

    def commit(self, attempt: Attempt, result: AttemptResult) -> ResultRecord:
        """Store the produced result under the attempt's fencing token."""
        return self._config.authority.commit_result(
            job_id=attempt.job_id,
            fencing_token=attempt.fencing_token,
            envelope=result.envelope,
            result_ref=result.result_ref,
        )

    def process(
        self,
        job_id: str,
        produce: Callable[[Attempt], AttemptResult] | None = None,
    ) -> ResultRecord:
        """Claim, produce, and commit -- or leave nothing behind.

        A producer that raises ends the attempt as failed; the job keeps no
        result, so another worker may take it over once the lease is over.
        """
        producer = produce if produce is not None else self._config.producer
        if producer is None:
            raise ParserInvalidRequestError("no producer is configured for this worker")

        attempt = self.claim(job_id)
        self._config.states.mark_running(
            job_id=attempt.job_id, fencing_token=attempt.fencing_token
        )
        try:
            result = self._produce(attempt, producer)
        except BaseException as exc:
            # EXIT: the producer's error is the error the caller sees; the
            # attempt is marked failed, and a failure to mark it is attached
            # to that error instead of replacing it.
            self._record_failed_attempt(attempt, exc)
            raise
        try:
            return self.commit(attempt, result)
        except ParserJobCancelledError:
            # The job was cancelled while the producer ran: the commit is
            # refused, and the cancellation becomes the job's terminal state.
            self._finalize_quietly(attempt.job_id)
            raise
        except ParserExecutionTimeoutError as exc:
            # The attempt outran its execution deadline. The store recorded
            # the timeout, so the job is timed out and the attempt ends
            # without a result; a failure to record that never replaces the
            # timeout the caller must see.
            self._record_failed_attempt(attempt, exc)
            raise

    def _produce(
        self, attempt: Attempt, producer: Callable[[Attempt], AttemptResult]
    ) -> AttemptResult:
        """Run the producer, keeping the lease alive while it runs."""
        heartbeat = (
            LeaseHeartbeat(
                self._config.authority,
                attempt,
                interval_seconds=self._config.renew_interval,
                lease_seconds=self._config.lease_seconds,
            )
            if self._config.renew_interval is not None
            else None
        )
        if heartbeat is None:
            # EXIT: no renewal thread runs, so the lease is not renewed while
            # the producer works and the attempt keeps the lease it claimed.
            return producer(attempt)
        with heartbeat:
            result = producer(attempt)
        if heartbeat.lost:
            # EXIT: the lease is gone, so this attempt may no longer act on the
            # job: the result is not committed, and the attempt ends without
            # one.
            raise ParserStaleFenceError(
                "the attempt lost its lease while the producer ran"
            )
        return result

    def _record_failed_attempt(self, attempt: Attempt, primary: BaseException) -> None:
        """Record the failed attempt without replacing the error that caused it.

        Recording the failure can fail too: the store may be unreachable.  The
        error that ended the attempt stays the error the caller sees, and the
        failure to record it is logged and attached to that error, so neither
        is swallowed.
        """
        try:
            self._config.states.fail_attempt(
                job_id=attempt.job_id, fencing_token=attempt.fencing_token
            )
        except BaseException as recording:
            # EXIT: the failed attempt is not written, and the recording
            # failure is logged and attached to the caller's error — it never
            # replaces it.
            logger.error(
                "the failed attempt of parser job %s (token %s) could not be "
                "recorded: %s",
                attempt.job_id,
                attempt.fencing_token,
                _failure_label(recording),
            )
            primary.add_note(
                "the failed attempt could not be recorded: "
                f"{_failure_label(recording)}: {recording}"
            )

    def _finalize_quietly(self, job_id: str) -> None:
        """Finalize a cancellation without hiding the failure that caused it."""
        try:
            self.finalize_cancelled(job_id)
        except ParserError as exc:
            # EXIT: the cancellation stays unfinalized, and its failure is
            # logged while the error that caused it is re-raised.
            logger.warning(
                "the cancellation of parser job %s could not be finalized: %s",
                job_id,
                exc.code,
            )
