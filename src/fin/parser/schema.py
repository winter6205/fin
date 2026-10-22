"""Idempotent DDL for the parser-owned PostgreSQL schema (contract section 3).

The parser service is a separate bounded context with its own durable
authority, so its state lives in the schema ``fin_parser`` and nowhere
else. It never reads or writes ``public.*``.

The DDL below is frozen by the foundation contract: the same table
names, columns and constraints. ``ensure_parser_schema`` runs it at
store construction, so no separate migration step is needed and every
store may call it without coordinating.

Two properties of the execution matter for a multi-process service:

* A statement runs only when its table is missing. ``CREATE TABLE IF
  NOT EXISTS`` still takes an exclusive lock on the table it inspects,
  so running the whole DDL at every construction would serialise the
  API process against every worker process for no reason.
* A restricted role cannot create schemas, so the schema statement runs
  only when the schema is missing; and a role that owns the tables but
  not the schema may not run DDL at all, so an already-complete schema
  is accepted as-is instead of failing the construction.

One property needs more than the "create if missing" rule: the job
status vocabulary is closed, and a schema created before the vocabulary
change still carries the old constraint. The constraint is therefore
aligned with ``fin.parser.ports.JOB_STATUSES`` on every construction,
which needs the role that owns the table.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, LiteralString

import psycopg
from psycopg import sql

from fin.parser.ports import JOB_STATUSES

logger = logging.getLogger(__name__)

PARSER_SCHEMA_NAME: Final[str] = "fin_parser"

JOB_STATUS_CONSTRAINT_NAME: Final[str] = "fin_parser_job_status_ck"

# The tables the parser service owns, in the order the frozen DDL
# creates them. A restricted role needs rights on exactly these and on
# nothing else.
_TABLE_DDL: Final[tuple[tuple[str, LiteralString], ...]] = (
    (
        "job",
        """
CREATE TABLE IF NOT EXISTS fin_parser.job (
  job_id            UUID PRIMARY KEY,
  idempotency_key   TEXT        NOT NULL UNIQUE,
  input_sha256      TEXT        NOT NULL,
  source_ref        JSONB,
  declared_format   TEXT,
  detected_format   TEXT,
  status            TEXT        NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fin_parser_job_status_ck CHECK
    (status IN ('queued','running','cancel_requested','succeeded','partial','failed','cancelled','timed_out'))
);
""",
    ),
    (
        "attempt",
        """
CREATE TABLE IF NOT EXISTS fin_parser.attempt (
  job_id          UUID        NOT NULL REFERENCES fin_parser.job(job_id) ON DELETE CASCADE,
  attempt_no      BIGINT      NOT NULL,
  fencing_token   BIGINT      NOT NULL,
  worker_id       TEXT        NOT NULL,
  state           TEXT        NOT NULL,   -- claimed|running|succeeded|failed|expired
  lease_expires_at TIMESTAMPTZ NOT NULL,
  claimed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at    TIMESTAMPTZ,
  PRIMARY KEY (job_id, attempt_no),
  CONSTRAINT fin_parser_attempt_state_ck CHECK
    (state IN ('claimed','running','succeeded','failed','expired'))
);
""",
    ),
    (
        "result",
        """
CREATE TABLE IF NOT EXISTS fin_parser.result (
  job_id        UUID PRIMARY KEY REFERENCES fin_parser.job(job_id) ON DELETE CASCADE,
  fencing_token BIGINT      NOT NULL,
  envelope      JSONB       NOT NULL,
  result_ref    TEXT        NOT NULL,   -- derived-output REFERENCE, never bytes
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
    (
        "tombstone",
        """
CREATE TABLE IF NOT EXISTS fin_parser.tombstone (
  job_id          UUID PRIMARY KEY,
  idempotency_key TEXT        NOT NULL,
  input_sha256    TEXT        NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
    (
        "outbox",
        """
CREATE TABLE IF NOT EXISTS fin_parser.outbox (
  notification_id UUID PRIMARY KEY,
  topic           TEXT        NOT NULL,
  payload         JSONB       NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at    TIMESTAMPTZ
);
""",
    ),
    (
        "inbox",
        """
CREATE TABLE IF NOT EXISTS fin_parser.inbox (
  notification_id UUID PRIMARY KEY,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
    (
        "staging_artifact",
        """
CREATE TABLE IF NOT EXISTS fin_parser.staging_artifact (
  artifact_id   UUID PRIMARY KEY,
  job_id        UUID        NOT NULL,
  fencing_token BIGINT      NOT NULL,
  path          TEXT        NOT NULL,
  referenced    BOOLEAN     NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
)

PARSER_TABLES: Final[tuple[str, ...]] = tuple(name for name, _ in _TABLE_DDL)


def ensure_parser_schema(dsn: str) -> None:
    """Create the parser-owned schema and its tables once per store.

    The DDL is idempotent, so every store that writes one of these
    tables may call it without coordinating. A role that may not create
    objects still gets a usable store as long as the tables are already
    there, which is what a restricted deployment looks like.
    """
    with psycopg.connect(dsn) as conn:
        if not _schema_exists(conn):
            conn.execute("CREATE SCHEMA IF NOT EXISTS fin_parser")
            conn.commit()
        missing = _missing_tables(conn)
        if missing:
            for name, statement in _TABLE_DDL:
                if name in missing:
                    conn.execute(statement)
            conn.commit()
        _align_job_status_constraint(conn)
        conn.commit()


def _align_job_status_constraint(conn: psycopg.Connection[Any]) -> None:
    """Make the job status constraint carry the closed vocabulary.

    A schema that predates the vocabulary change still carries the old
    constraint, so the rows are migrated and the constraint replaced. The
    role must own the table to do that; a role that may not run DDL keeps
    the schema it finds and is told why.
    """
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = %s AND conrelid = 'fin_parser.job'::regclass",
        (JOB_STATUS_CONSTRAINT_NAME,),
    ).fetchone()
    if row is None:
        return
    if _constraint_statuses(str(row[0])) == set(JOB_STATUSES):
        return
    try:
        with conn.transaction():
            # The old constraint is dropped first: it does not know the
            # new vocabulary yet, so the rows could not be migrated
            # under it.
            conn.execute(
                sql.SQL("ALTER TABLE fin_parser.job DROP CONSTRAINT {}").format(
                    sql.Identifier(JOB_STATUS_CONSTRAINT_NAME)
                )
            )
            conn.execute(
                "UPDATE fin_parser.job SET status = 'queued' WHERE status = 'accepted'"
            )
            conn.execute(
                sql.SQL(
                    "ALTER TABLE fin_parser.job ADD CONSTRAINT {} CHECK (status IN ({}))"
                ).format(
                    sql.Identifier(JOB_STATUS_CONSTRAINT_NAME),
                    sql.SQL(", ").join(sql.Literal(status) for status in JOB_STATUSES),
                )
            )
    except psycopg.errors.InsufficientPrivilege as exc:
        # EXIT: the role may not run DDL, so the constraint keeps the old
        # vocabulary and the store keeps the schema it finds — a restricted
        # deployment stays usable, and the warning says what was not aligned.
        logger.warning(
            "the parser job status constraint could not be aligned to the closed "
            "vocabulary: %s",
            exc,
        )


def _constraint_statuses(definition: str) -> set[str]:
    """The status values a rendered CHECK constraint allows."""
    return set(re.findall(r"'([^']*)'", definition))


def _schema_exists(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname = 'fin_parser'"
    ).fetchone()
    return row is not None


def _missing_tables(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'fin_parser'"
    ).fetchall()
    present = {str(row[0]) for row in rows}
    return [table for table in PARSER_TABLES if table not in present]


__all__ = [
    "JOB_STATUS_CONSTRAINT_NAME",
    "PARSER_SCHEMA_NAME",
    "PARSER_TABLES",
    "ensure_parser_schema",
]
