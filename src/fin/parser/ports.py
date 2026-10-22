"""Ports of the parser bounded context (contract section 4).

The parser service keeps its durable state in PostgreSQL, in its own
schema ``fin_parser``. Everything the service needs from that store is
named here, so the worker, the notification path and the HTTP surface
code against one protocol instead of against a driver.

The protocol is the frozen shape other workers code against: method
signatures and value-object fields do not drift per call site. The
implementations behind it may change (one PostgreSQL store today), but
a caller only ever sees this surface.

Two rules hold for every implementation:

* Failure is raised as a typed ``fin.parser.errors`` exception, never
  signalled by returning ``None``, ``-1`` or ``""``. ``None`` means one
  specific thing only: "there is no such row" for the read methods that
  answer with an optional record.
* ``fencing_token`` is a per-JOB counter. A caller that holds a token
  proves it still owns the job only while that token is the job's
  current effective token.

The job lifecycle is the closed vocabulary ``JOB_STATUSES``: a submitted
job is ``queued``, a claimed one is ``running``, a cancelled one is
``cancel_requested`` until the worker acknowledges it, and the terminal
states are ``succeeded``, ``partial``, ``failed``, ``cancelled`` and
``timed_out``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

JOB_STATUSES: Final[tuple[str, ...]] = (
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "timed_out",
)


@dataclass(frozen=True)
class OutboxEvent:
    """One lifecycle event to append to the parser-owned outbox.

    The event travels as one object, so the row is written from a single
    value: the event name, the job it belongs to, the job's version
    counter, the status it reports and the reference to the derived
    output.
    """

    event: str
    job_id: str
    job_version: int
    status: str
    # EXIT: an event that carries no result records None -- the one value
    # that means "this event carries no result".
    result_ref: str | None = None
    # EXIT: an event that ended in no typed failure records None, so the
    # row never carries an empty error token.
    error_code: str | None = None


def write_outbox_row(connection: Any, event: OutboxEvent) -> str:
    """Append one lifecycle event to the parser-owned outbox.

    The row is written on the caller's connection, so it commits with the
    state change that produced it: a durable job can never lack its
    completion event.

    ``event.event`` is a stable versioned event name
    (``parser.job.completed.v1`` and its siblings), ``event.job_version``
    is the job's version counter -- its current effective fencing token --
    and ``event.result_ref`` is None for an event that carries no result.
    ``event.error_code`` records the typed failure that ended the job, if
    any.
    """
    payload = {
        "event": event.event,
        "job_id": event.job_id,
        "job_version": event.job_version,
        "status": event.status,
        "result_ref": event.result_ref,
        "error_code": event.error_code,
    }
    row = connection.execute(
        "INSERT INTO fin_parser.outbox (notification_id, topic, payload) "
        "VALUES (%s, %s, %s::jsonb) RETURNING notification_id",
        (str(uuid.uuid4()), event.event, json.dumps(payload)),
    ).fetchone()
    return str(row[0])


@dataclass(frozen=True)
class JobSubmission:
    """The outcome of one job submission.

    ``replayed`` is True when the job already existed for the same
    idempotency key and the same input digest, so the caller learns
    that its retry converged on an existing job instead of a new one.
    """

    job_id: str
    status: str
    replayed: bool


@dataclass(frozen=True)
class JobRecord:
    """One durable parser job row.

    ``status`` is the closed set the job table allows:
    ``queued`` / ``running`` / ``cancel_requested`` / ``succeeded`` /
    ``partial`` / ``failed`` / ``cancelled`` / ``timed_out``.
    """

    job_id: str
    idempotency_key: str
    input_sha256: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Attempt:
    """One worker attempt on a job.

    ``fencing_token`` is the job-scoped ownership proof: only the
    attempt holding the job's current effective token may commit.
    """

    job_id: str
    attempt_no: int
    fencing_token: int
    worker_id: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class ResultRecord:
    """One durable parse result.

    ``envelope`` is the stored versioned result envelope; ``result_ref``
    is a REFERENCE to derived output, never the output bytes.
    """

    job_id: str
    fencing_token: int
    envelope: Mapping[str, object]
    result_ref: str
    created_at: datetime


@dataclass(frozen=True)
class Notification:
    """One parser-owned outbox notification."""

    notification_id: str
    topic: str
    payload: Mapping[str, object]
    created_at: datetime


@dataclass(frozen=True)
class StagingArtifact:
    """One staging output a worker produced for a job."""

    artifact_id: str
    job_id: str
    fencing_token: int
    path: str
    referenced: bool
    created_at: datetime


@runtime_checkable
class ParserJobStore(Protocol):
    """The parser service's durable authority.

    Implementations own the ``fin_parser`` schema and nothing else.
    """

    def submit_job(
        self,
        *,
        idempotency_key: str,
        input_sha256: str,
        source_ref: Mapping[str, object] | None,
        declared_format: str | None,
    ) -> JobSubmission:
        """Accept one job durably, or replay an identical submission.

        Same key and same ``input_sha256`` returns the SAME job with
        ``replayed=True``. Same key and a different digest raises
        ``ParserJobConflictError``. A key whose job was deleted raises
        ``ParserJobTombstonedError``. Exceeding the open-job limit
        raises ``ParserJobCapacityError``; exceeding the pending-queue
        cap raises ``ParserQueueFullError``.

        The accepted job is ``queued``, and its acceptance event is
        written in the same transaction as the job row.
        """
        ...

    def get_job(self, job_id: str) -> JobRecord | None:
        """The job row, or None when no job carries that id.

        EXIT: None answers "no such row" and nothing else; it never
        carries a failure.
        """
        ...

    def list_open_jobs(self, *, limit: int) -> list[JobRecord]:
        """The open jobs, oldest first, capped at ``limit``.

        An open job is ``queued``, ``running`` or ``cancel_requested``: a
        cancellation whose worker died is still open, so the job that
        finalizes it can find it.
        """
        ...

    def claim_attempt(
        self, *, job_id: str, worker_id: str, lease_seconds: float
    ) -> Attempt | None:
        """Take ownership of a job under a lease.

        The claim runs in ONE transaction that locks the job row, so
        competing workers can never hold the same effective attempt:
        each claim advances the job's fencing token. None means the job
        has no claimable work (unknown job, or already terminal).

        A job that waited longer than the queue-wait deadline is not
        claimable: it becomes ``timed_out`` and the claim raises
        ``ParserQueueWaitTimeoutError``.
        """
        ...

    def renew_attempt(
        self, *, job_id: str, fencing_token: int, lease_seconds: float
    ) -> bool:
        """Extend the lease of the job's CURRENT effective attempt.

        False when the token is no longer effective, when the lease has
        already expired, or when the job was cancelled.
        """
        ...

    def live_attempt_tokens(self, *, now: datetime) -> set[tuple[str, int]]:
        """The attempts that still hold their job's effective fencing token.

        An attempt is live when it is the job's highest token, its attempt
        row is still open, and its lease has not run out. A lease that
        expired frees its output, because the next claim carries a higher
        token.
        """
        ...

    def committed_result_refs(self) -> set[str]:
        """Every derived-output reference a committed result names.

        A committed result is the durable claim on its derived output, so
        this is the set the staging collection must not destroy.
        """
        ...

    def committed_result_tokens(self) -> set[tuple[str, int]]:
        """The attempt each committed result was written by.

        The token half of the safety rule: an artifact produced by the
        attempt that committed is committed output, whatever its path
        spelling is.
        """
        ...

    def cancel_job(self, job_id: str) -> JobRecord:
        """Cancel a job durably and return its record.

        A job with a live attempt becomes ``cancel_requested``: the
        request is durable and visible, and the attempt is fenced so it
        can no longer commit. A job with no live attempt becomes
        ``cancelled`` at once. The cancellation is committed before this
        returns, so a later commit of any token is refused.
        """
        ...

    def commit_result(
        self,
        *,
        job_id: str,
        fencing_token: int,
        envelope: Mapping[str, object],
        result_ref: str,
    ) -> ResultRecord:
        """Store the parse result of the attempt holding ``fencing_token``.

        Writes only when the token is the job's current effective token
        and the job is neither cancelled nor tombstoned. Any other
        outcome raises and writes nothing.

        The terminal state is ``succeeded``, or ``partial`` when the
        result reports partial coverage. The completion event is written
        in the same transaction as the result.
        """
        ...

    def read_result(self, job_id: str) -> ResultRecord | None:
        """The durable result of a job, or None when it has none.

        EXIT: None answers "no such result" and nothing else; it never
        carries a failure.
        """
        ...

    def delete_job(self, job_id: str) -> None:
        """Delete a job and leave a replay-protection tombstone.

        The delete is idempotent: a job that was already deleted answers
        the same way, leaves the one tombstone it left and writes no
        second one. A job id that never carried a job raises
        ``ParserJobNotFoundError``.

        The tombstone is the replay protection: a submission under the
        same idempotency key and a late commit against the deleted job
        raise ``ParserJobTombstonedError``.
        """
        ...

    def enqueue_notification(
        self, *, topic: str, payload: Mapping[str, object]
    ) -> str:
        """Append one notification to the parser-owned outbox.

        The row carries the canonical event payload: the event name, the
        job id, the job version, the status and the result reference.
        """
        ...

    def claim_unpublished_notifications(self, *, limit: int) -> list[Notification]:
        """The outbox notifications that were never published yet."""
        ...

    def mark_notification_published(self, notification_id: str) -> None:
        """Record that a notification left the durable outbox."""
        ...

    def record_notification_received(self, notification_id: str) -> bool:
        """Record a delivery. False for a duplicate id, True for the first."""
        ...

    def unreconciled_notification_ids(self) -> list[str]:
        """The outbox ids that were never published or never confirmed."""
        ...

    def register_staging_artifact(
        self, *, job_id: str, fencing_token: int, path: str
    ) -> str:
        """Register one staging output and return its artifact id."""
        ...

    def list_staging_artifacts(
        self, *, job_id: str | None = None
    ) -> list[StagingArtifact]:
        """The staging outputs, optionally narrowed to one job."""
        ...

    def mark_staging_referenced(self, artifact_id: str) -> None:
        """Mark a staging output as referenced by a committed result."""
        ...

    def drop_staging_artifact(self, artifact_id: str) -> None:
        """Remove a staging output. Repeating the drop is harmless."""
        ...


__all__ = [
    "JOB_STATUSES",
    "Attempt",
    "JobRecord",
    "JobSubmission",
    "Notification",
    "OutboxEvent",
    "ParserJobStore",
    "ResultRecord",
    "StagingArtifact",
    "write_outbox_row",
]
