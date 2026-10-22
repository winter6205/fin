"""The parser service's result boundary and its tombstone semantics.

The store owns the fence: it decides whether an attempt may commit a result,
and it writes the tombstone when a job is deleted. This module owns what
happens AROUND that store:

* **The envelope contract at the commit boundary.** A result is committed only
  after its envelope validates as a :class:`~fin.parser.envelope.ParsedDocument`,
  so an envelope that breaks the contract never reaches the durable row.
* **The durable READ boundary.** A stored result is read back as a validated
  ``ParsedDocument``. A stored envelope that no longer satisfies the contract
  is a typed failure, never a mapping handed to a caller as if it were a
  result.
* **The tombstone READ.** The store port names no tombstone read, and "is this
  identity durably deleted?" is a question the deletion boundary must be able
  to answer, so the read lives here.

Ownership (ADR 0103): the parser service owns RESULT EXISTENCE — whether a
parsed result exists for a job — while fin owns whether that result is usable
for readiness or publication. Nothing here decides the second question.

The result REFERENCES derived output. The envelope carries the digest of the
source and the references of the derived text, never the source bytes, so a
result row can never stand in for the document it was parsed from.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from fin.parser.db import parser_connection
from fin.parser.envelope import ParsedDocument, parse_envelope
from fin.parser.ports import ParserJobStore, ResultRecord

__all__ = [
    "ResultBoundary",
    "TombstoneRecord",
    "find_tombstone_by_idempotency_key",
    "parser_read_connection",
    "read_tombstone",
]


@contextmanager
def parser_read_connection(connection_url: str) -> Iterator[psycopg.Connection[Any]]:
    """A read-only connection whose driver failures are typed.

    The parser context has ONE way to read durable state, so a read that
    cannot reach PostgreSQL is always ``ParserPersistenceError`` and never a
    bare driver exception. The transaction is READ ONLY, so a write through
    this connection is refused by PostgreSQL instead of applied.
    """
    with parser_connection(
        connection_url,
        message="the parser store could not be reached",
        read_only=True,
    ) as conn:
        yield conn


@dataclass(frozen=True, slots=True)
class TombstoneRecord:
    """The durable record of a deleted job identity."""

    job_id: str
    idempotency_key: str
    input_sha256: str
    created_at: datetime


def _tombstone_from_row(row: Any) -> TombstoneRecord | None:
    if row is None:
        return None
    return TombstoneRecord(
        job_id=str(row[0]),
        idempotency_key=str(row[1]),
        input_sha256=str(row[2]),
        created_at=row[3],
    )


class ResultBoundary:
    """The commit and durable-read boundary over the parser's durable store.

    The boundary adds the ENVELOPE CONTRACT to the store's fence. The fence
    itself stays with the store: a stale attempt, a cancelled job and a
    deleted job are refused there, and this class does not re-decide them.
    """

    def __init__(self, store: ParserJobStore) -> None:
        self._store = store

    def commit_result(
        self,
        *,
        job_id: str,
        fencing_token: int,
        envelope: Mapping[str, object],
        result_ref: str,
    ) -> ResultRecord:
        """Commit a result whose envelope satisfies the envelope contract.

        The envelope is validated BEFORE the store writes anything, so a
        contract violation is refused without touching durable state. What is
        stored is the validated envelope, not the caller's mapping.
        """
        document = parse_envelope(envelope)
        return self._store.commit_result(
            job_id=job_id,
            fencing_token=fencing_token,
            envelope=document.model_dump(mode="json"),
            result_ref=result_ref,
        )

    def read_result(self, job_id: str) -> ResultRecord | None:
        """The durable result of a job, or ``None`` when it has none.

        Absence is not a failure: the question is about durable state, and
        "no result yet" is one of its answers.
        """
        return self._store.read_result(job_id)

    def read_document(self, job_id: str) -> ParsedDocument | None:
        """The durable result as a validated envelope, or ``None``.

        A stored envelope that no longer satisfies the contract raises
        ``ParserEnvelopeError``: the row exists, but it is not a result a
        caller may act on.
        """
        record = self._store.read_result(job_id)
        if record is None:
            return None
        return parse_envelope(record.envelope)

    def delete_job(self, job_id: str) -> None:
        """Delete a job. The store leaves the replay-protection tombstone."""
        self._store.delete_job(job_id)


def read_tombstone(connection_url: str, job_id: str) -> TombstoneRecord | None:
    """The tombstone of a deleted job, when it has one.

    A tombstone is the durable answer to "was this identity deleted?", and it
    outlives the process that wrote it.
    """
    with parser_read_connection(connection_url) as conn:
        row = conn.execute(
            "SELECT job_id, idempotency_key, input_sha256, created_at"
            " FROM fin_parser.tombstone WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return _tombstone_from_row(row)


def find_tombstone_by_idempotency_key(
    connection_url: str, idempotency_key: str
) -> TombstoneRecord | None:
    """The tombstone that protects an idempotency key, when there is one."""
    with parser_read_connection(connection_url) as conn:
        row = conn.execute(
            "SELECT job_id, idempotency_key, input_sha256, created_at"
            " FROM fin_parser.tombstone WHERE idempotency_key = %s"
            " ORDER BY created_at LIMIT 1",
            (idempotency_key,),
        ).fetchone()
    return _tombstone_from_row(row)
