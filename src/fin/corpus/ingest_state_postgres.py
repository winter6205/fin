"""The durable ingest job store: the same port, rows that outlive the process.

Transition legality, busy detection and audit-row shape all come from
:mod:`fin.corpus.ingest_state_model` so this store and the process-local
memory store cannot diverge on the rules; only the SQL mechanics (row
locks, one serialized connection, committed rejections) live here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final, LiteralString, NoReturn

import psycopg
from psycopg import errors as psycopg_errors

from fin.corpus.ingest_state_errors import (
    DuplicateJobError,
    IngestStateError,
    IngestStateStoreConfigError,
    IngestStateStoreUnavailableError,
    IngestStepError,
    UnknownJobError,
)
from fin.corpus.ingest_state_model import (
    ALL_DOCUMENTS,
    AllDocuments,
    IngestAuditRow,
    IngestJob,
    IngestState,
    StepFailure,
    TransitionRequest,
    coerce_request,
    coerce_state,
    coerce_state_filter,
    event_audit,
    failure_guard,
    job_created_audit,
    job_superseded_audit,
    rejection_audit,
    replacement_guard,
    step_failed_audit,
    transition_audit,
    transition_guard,
    utc_now,
    validate_document_id,
)

# ``LiteralString`` is what psycopg's ``Query`` accepts for inline SQL: it is
# the driver's own guard that a query was written out, not built from a
# caller-supplied value. Only the annotation changes; these are still the
# same ``str`` objects at runtime.
_SCHEMA_STATEMENTS: tuple[LiteralString, ...] = (
    """
CREATE TABLE IF NOT EXISTS fin_ingest_job (
    document_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    error_type TEXT,
    error_message TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS fin_ingest_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    document_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    from_state TEXT,
    to_state TEXT,
    step TEXT,
    error_type TEXT,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
)
""",
    """
CREATE INDEX IF NOT EXISTS fin_ingest_audit_document_idx
    ON fin_ingest_audit (document_id, audit_id)
""",
)

_JOB_COLUMNS = "state, created_at, updated_at, error_type, error_message"

# Resolved in this order; the first non-blank one wins. ``FIN_CHECKPOINT_URL``
# is last because it names the process's general Postgres, which is a fine
# place for job state but not the one an ingest-specific deployment sets.
# Owned here (not in ``ingest_state_env``) because this store's empty-URL
# refusal names the first key, and the env module imports this one.
INGEST_STATE_DSN_KEYS: Final[tuple[str, ...]] = (
    "FIN_INGEST_STATE_DSN",
    "FIN_METADATA_DSN",
    "FIN_METADATA_URL",
    "FIN_CHECKPOINT_URL",
)


class PostgresIngestStateStore:
    """SQL-backed job state and audit trail: the same port, durable rows.

    A job row and the audit row that explains it are written in one
    transaction, so the state a poller reads after a restart is the state the
    machine actually reached. A write transaction locks its job row
    (``FOR UPDATE``) until it commits, which is what turns a second worker's
    competing transition into the typed busy error instead of a lost write.

    One connection is held per store instance and serialized under a lock: a
    job-state read costs less than the connect that would open a fresh one.
    """

    def __init__(self, connection_url: str) -> None:
        if not isinstance(connection_url, str) or not connection_url.strip():
            raise IngestStateStoreConfigError(
                "the durable ingest job store needs a non-empty Postgres "
                "URL; nothing is configured",
                key=INGEST_STATE_DSN_KEYS[0],
            )
        self._url = connection_url.strip()
        self._connection: psycopg.Connection | None = None
        self._lock = threading.RLock()
        try:
            with self._transaction() as connection:
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
        except psycopg.Error as exc:
            raise IngestStateStoreUnavailableError(
                "the configured durable ingest job store is unreachable"
            ) from exc

    def close(self) -> None:
        """Drop the held connection (process shutdown, test teardown)."""
        with self._lock:
            if self._connection is not None and not self._connection.closed:
                self._connection.close()
            self._connection = None

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        """One serialized transaction: commit on success, never on failure."""
        with self._lock:
            if self._connection is None or self._connection.closed:
                self._connection = psycopg.connect(self._url)
            connection = self._connection
            try:
                yield connection
            except psycopg.Error:
                self.close()
                raise
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _unavailable(
        document_id: str, action: str, cause: BaseException
    ) -> NoReturn:
        """One exit for every driver fault on one document: typed, retryable.

        The job state is left exactly as the machine last committed it, so
        the caller's retry starts from the real state rather than a guess.
        """
        raise IngestStateStoreUnavailableError(
            f"the ingest job {document_id!r} could not be {action} in the "
            "durable job store"
        ) from cause

    def create_job(
        self,
        document_id: str,
        *,
        actor: str | None = None,
    ) -> IngestJob:
        """Create a pending job and audit its creation."""
        validate_document_id(document_id)
        request = TransitionRequest(
            to_state=IngestState.PENDING, actor=actor
        )
        now = utc_now()
        try:
            with self._transaction() as connection:
                try:
                    connection.execute(
                        "INSERT INTO fin_ingest_job (document_id, state, "
                        "created_at, updated_at) VALUES (%s, %s, %s, %s)",
                        (document_id, IngestState.PENDING.value, now, now),
                    )
                except psycopg_errors.UniqueViolation as exc:
                    raise DuplicateJobError(
                        f"ingest job already exists for {document_id!r}"
                    ) from exc
                self._insert_audit(
                    connection,
                    job_created_audit(document_id, request, now),
                )
        except psycopg.Error as exc:
            self._unavailable(document_id, "created", exc)
        return IngestJob(
            document_id=document_id,
            state=IngestState.PENDING,
            created_at=now,
            updated_at=now,
        )

    def get_job(self, document_id: str) -> IngestJob:
        """Return a job or raise a typed missing-job error."""
        validate_document_id(document_id)
        try:
            with self._transaction() as connection:
                return self._read_job(connection, document_id)
        except psycopg.Error as exc:
            self._unavailable(document_id, "read", exc)

    def list_jobs(
        self,
        *,
        states: Collection[IngestState | str] | None = None,
    ) -> tuple[IngestJob, ...]:
        """Return a deterministic snapshot, optionally state-filtered.

        The filter is resolved and validated in SQL parameters, so a
        worker discovers exactly the states its resume edge can act on.
        """
        wanted = coerce_state_filter(states)
        if wanted is not None and not wanted:
            return ()
        statement = (
            f"SELECT document_id, {_JOB_COLUMNS} FROM fin_ingest_job"
        )
        params: tuple[Any, ...] = ()
        if wanted is not None:
            statement += " WHERE state = ANY(%s)"
            params = ([state.value for state in sorted(wanted)],)
        statement += " ORDER BY document_id"
        try:
            with self._transaction() as connection:
                rows = connection.execute(
                    statement, params
                ).fetchall()
        except psycopg.Error as exc:
            raise IngestStateStoreUnavailableError(
                "the ingest job table could not be read"
            ) from exc
        return tuple(
            IngestJob(
                document_id=str(row[0]),
                state=coerce_state(row[1]),
                created_at=row[2],
                updated_at=row[3],
                error_type=row[4],
                error_message=row[5],
            )
            for row in rows
        )

    def list_audit(
        self,
        document_id: str | None | AllDocuments = ALL_DOCUMENTS,
    ) -> tuple[IngestAuditRow, ...]:
        """Return all audit rows or only rows for one document.

        The unfiltered read is ``list_audit()``; an explicit ``None`` is
        refused by the shared identity rule, like a blank id, before any
        WHERE clause is built.
        """
        if document_id is not ALL_DOCUMENTS:
            validate_document_id(document_id)
        statement = (
            "SELECT document_id, action, actor, occurred_at, from_state, "
            "to_state, step, error_type, error_message, metadata "
            "FROM fin_ingest_audit"
        )
        params: tuple[Any, ...] = ()
        if document_id is not ALL_DOCUMENTS:
            statement += " WHERE document_id = %s"
            params = (document_id,)
        statement += " ORDER BY audit_id"
        try:
            with self._transaction() as connection:
                rows = connection.execute(statement, params).fetchall()
        except psycopg.Error as exc:
            raise IngestStateStoreUnavailableError(
                "the ingest audit trail could not be read"
            ) from exc
        return tuple(
            IngestAuditRow(
                document_id=str(row[0]),
                action=str(row[1]),
                actor=str(row[2]),
                occurred_at=row[3],
                from_state=coerce_state(row[4]) if row[4] else None,
                to_state=coerce_state(row[5]) if row[5] else None,
                step=row[6],
                error_type=row[7],
                error_message=row[8],
                metadata=dict(row[9] or {}),
            )
            for row in rows
        )

    def transition(
        self,
        document_id: str,
        to_state: IngestState | str,
        *,
        actor: str | None = None,
        expected_state: IngestState | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestState:
        """Atomically transition a job and return its new state."""
        request = coerce_request(
            TransitionRequest(
                to_state=to_state,
                actor=actor,
                expected_state=expected_state,
                metadata=metadata,
            )
        )
        target = request.to_state
        assert isinstance(target, IngestState)  # narrowed by coerce_request
        try:
            with self._transaction() as connection:
                job = self._read_job(connection, document_id, for_update=True)
                error = transition_guard(job, request)
                if error is not None:
                    self._reject(connection, job, error, request)
                now = utc_now()
                connection.execute(
                    "UPDATE fin_ingest_job SET state = %s, updated_at = %s, "
                    "error_type = NULL, error_message = NULL "
                    "WHERE document_id = %s",
                    (target.value, now, document_id),
                )
                self._insert_audit(
                    connection, transition_audit(job, request, now)
                )
        except psycopg.Error as exc:
            self._unavailable(document_id, "transitioned", exc)
        return target

    def append_audit(
        self,
        document_id: str,
        action: str,
        metadata: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> IngestAuditRow:
        """Durably append one state-machine-external event row.

        Same shape as the memory store's: no job column moves, the row
        is its own committed transaction, and the job must exist — an
        event about a document no job ever accepted is a bug, and
        persisting it would let a recovery sweep read a publication
        history that never had an owner.
        """
        validate_document_id(document_id)
        if not isinstance(action, str) or not action.strip():
            raise ValueError("action must not be empty")
        row = event_audit(document_id, action, metadata, actor, utc_now())
        try:
            with self._transaction() as connection:
                # Existence check inside the same transaction: the event
                # and the proof its job exists commit atomically.
                self._read_job(connection, document_id)
                self._insert_audit(connection, row)
        except psycopg.Error as exc:
            raise IngestStateStoreUnavailableError(
                "the ingest audit event could not be appended"
            ) from exc
        return row

    def supersede_job(
        self,
        document_id: str,
        *,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestJob:
        """Open the next ``pending`` generation of a ready document's job."""
        validate_document_id(document_id)
        request = TransitionRequest(
            to_state=IngestState.PENDING, actor=actor, metadata=metadata
        )
        now = utc_now()
        try:
            with self._transaction() as connection:
                job = self._read_job(connection, document_id, for_update=True)
                error = replacement_guard(job)
                if error is not None:
                    self._reject(connection, job, error, request)
                connection.execute(
                    "UPDATE fin_ingest_job SET state = %s, created_at = %s, "
                    "updated_at = %s, error_type = NULL, "
                    "error_message = NULL WHERE document_id = %s",
                    (IngestState.PENDING.value, now, now, document_id),
                )
                self._insert_audit(
                    connection,
                    job_superseded_audit(document_id, request, now),
                )
        except psycopg.Error as exc:
            self._unavailable(document_id, "superseded", exc)
        return IngestJob(
            document_id=document_id,
            state=IngestState.PENDING,
            created_at=now,
            updated_at=now,
        )

    def fail_step(
        self,
        document_id: str,
        *,
        step: str,
        cause: BaseException,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> NoReturn:
        """Mark a non-terminal job failed, audit it, and raise typed."""
        failure = StepFailure(step=step, cause=cause)
        validate_document_id(document_id)
        request = TransitionRequest(
            to_state=IngestState.FAILED, actor=actor, metadata=metadata
        )
        try:
            with self._transaction() as connection:
                job = self._read_job(connection, document_id, for_update=True)
                error = failure_guard(job)
                if error is not None:
                    self._reject(connection, job, error, request)
                now = utc_now()
                connection.execute(
                    "UPDATE fin_ingest_job SET state = %s, updated_at = %s, "
                    "error_type = %s, error_message = %s "
                    "WHERE document_id = %s",
                    (
                        IngestState.FAILED.value,
                        now,
                        failure.error_type,
                        failure.error_message,
                        document_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    step_failed_audit(job, failure, request, now),
                )
        except psycopg.Error as exc:
            self._unavailable(document_id, "failed", exc)
        raise IngestStepError(document_id, step, cause) from cause

    @staticmethod
    def _read_job(
        connection: psycopg.Connection,
        document_id: str,
        *,
        for_update: bool = False,
    ) -> IngestJob:
        row = connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM fin_ingest_job "
            "WHERE document_id = %s"
            + (" FOR UPDATE" if for_update else ""),
            (document_id,),
        ).fetchone()
        if row is None:
            raise UnknownJobError(document_id)
        return IngestJob(
            document_id=document_id,
            state=coerce_state(row[0]),
            created_at=row[1],
            updated_at=row[2],
            error_type=row[3],
            error_message=row[4],
        )

    @staticmethod
    def _insert_audit(
        connection: psycopg.Connection, row: IngestAuditRow
    ) -> None:
        connection.execute(
            "INSERT INTO fin_ingest_audit (document_id, action, actor, "
            "occurred_at, from_state, to_state, step, error_type, "
            "error_message, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                row.document_id,
                row.action,
                row.actor,
                row.occurred_at,
                row.from_state.value if row.from_state else None,
                row.to_state.value if row.to_state else None,
                row.step,
                row.error_type,
                row.error_message,
                json.dumps(dict(row.metadata), sort_keys=True),
            ),
        )

    def _reject(
        self,
        connection: psycopg.Connection,
        job: IngestJob,
        error: IngestStateError,
        request: TransitionRequest,
    ) -> NoReturn:
        """Audit a refused transition durably, then raise it.

        The rejection is committed on its own: a refused transition is still
        something an operator has to be able to find afterwards, and the job
        row is left untouched.
        """
        self._insert_audit(
            connection, rejection_audit(job, error, request, utc_now())
        )
        connection.commit()
        raise error
