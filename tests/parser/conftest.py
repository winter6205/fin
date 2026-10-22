"""Fixtures for the parser store and HTTP tests.

The parser store is durable PostgreSQL state, so the fixtures point at
``FIN_TEST_DSN`` and refuse any database whose name does not end with
``_test`` (repo hard boundary 4). Nothing here touches a development
database, and nothing writes outside the injected temporary roots.

Isolation: the test database is shared, so the fixtures never truncate a
table. Every key a case writes carries this process's run prefix, and
the teardown removes exactly those rows. A case therefore cannot see
another run's rows, and cannot delete them either.

Two roles are used: the ordinary test role, and a restricted role that
may touch only the ``fin_parser`` schema. The restricted role is what
proves the store does not need database-wide rights. It is named after
this process, so a concurrent run never shares it.

The pending-queue cap is raised for the parser cases only: the queue is
counted over the whole shared database, and the rows of earlier runs
stay there. The cap is set per case and restored afterwards, so no other
test in the session sees it. The cap itself is exercised in
``test_job_store.py`` with an explicit limit, so the behaviour is covered
without letting another run's leftovers refuse this run's work.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.parser.job_store import PostgresParserJobStore
from fin.parser.schema import ensure_parser_schema

TEST_DSN_ENV = "FIN_TEST_DSN"

# The run prefix keeps one run's rows apart from another run's rows in
# the shared test database.
RUN_PREFIX = f"test-{os.getpid()}-"

# The pending cap of the parser cases; the store reads it from here.
PENDING_CAP_ENV = "FIN_PARSER_MAX_PENDING_JOBS"
PENDING_CAP = "1000"


@pytest.fixture(autouse=True)
def parser_pending_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise the pending cap for one parser case, and restore it after.

    The setting is scoped to the case: it is never written into the
    process environment at import time, so a test outside this package
    sees the shipped default.
    """
    monkeypatch.setenv(PENDING_CAP_ENV, PENDING_CAP)


def _database_name(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    return str(row[0]) if row is not None else ""


@pytest.fixture(scope="session")
def parser_dsn() -> str:
    """The PostgreSQL DSN the parser tests may write to.

    A database whose name does not end with ``_test`` is refused, not
    skipped: connecting to a development database would destroy the
    operator's demo data.
    """
    dsn = os.environ.get(TEST_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV} is unset: the parser tests need a PostgreSQL _test database")
    database = _database_name(dsn)
    assert database.endswith("_test"), (
        f"the parser tests refuse a database that is not a _test database: {database}"
    )
    return dsn


@pytest.fixture
def parser_database(parser_dsn: str) -> Iterator[str]:
    """The parser schema, ready to write, with this run's rows removed."""
    ensure_parser_schema(parser_dsn)
    yield parser_dsn
    _remove_run_rows(parser_dsn)


@pytest.fixture
def job_key() -> str:
    """An idempotency key no other case and no other run can collide with."""
    return f"{RUN_PREFIX}{uuid.uuid4().hex}"


@pytest.fixture
def store(parser_database: str, parser_pending_cap: None) -> PostgresParserJobStore:
    """A store over the test database, with the pending cap in force."""
    return PostgresParserJobStore(parser_database)


@pytest.fixture(scope="session")
def parser_schema(parser_dsn: str) -> str:
    """The parser schema, reconciled by the role that owns it.

    The job status vocabulary is closed, and aligning a schema that
    predates it needs the table owner. The restricted role may not run
    that DDL, so the owning role aligns it once per session, before any
    restricted store is built.
    """
    ensure_parser_schema(parser_dsn)
    return parser_dsn


@pytest.fixture(scope="session")
def restricted_role(parser_dsn: str, parser_schema: str) -> Iterator[str]:
    """A role that may touch only the parser schema.

    The role owns no database-wide rights: it is not a superuser, it may
    not create databases or roles, and it holds no privilege on any
    other schema. The grants it does hold are exactly the ones the
    store needs.
    """
    role = f"fin_parser_app_{os.getpid()}"
    with psycopg.connect(parser_dsn) as conn:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            ).format(role=sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=sql.Identifier(_database_name(parser_dsn)),
                role=sql.Identifier(role),
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA fin_parser TO {role}").format(
                role=sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL(
                "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA fin_parser TO {role}"
            ).format(role=sql.Identifier(role))
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA fin_parser "
                "GRANT ALL ON TABLES TO {role}"
            ).format(role=sql.Identifier(role))
        )
        conn.commit()
    yield role
    with psycopg.connect(parser_dsn) as conn:
        owner = conn.execute("SELECT current_user").fetchone()[0]
        # REASSIGN first: a role that somehow owns an object must hand it
        # over rather than take the schema down with it.
        conn.execute(
            sql.SQL("REASSIGN OWNED BY {old} TO {new}").format(
                old=sql.Identifier(role), new=sql.Identifier(str(owner))
            )
        )
        conn.execute(
            sql.SQL("DROP OWNED BY {role}").format(role=sql.Identifier(role))
        )
        conn.execute(sql.SQL("DROP ROLE {role}").format(role=sql.Identifier(role)))
        conn.commit()


@pytest.fixture
def restricted_dsn(parser_dsn: str, restricted_role: str) -> str:
    """A DSN for the restricted role."""
    info = conninfo_to_dict(parser_dsn)
    return make_conninfo(**{**info, "user": restricted_role})


def _remove_run_rows(dsn: str) -> None:
    """Delete the rows this run wrote, and only those."""
    with psycopg.connect(dsn) as conn:
        # The lifecycle events of the run's jobs go first: they name the
        # job they belong to, and the job row is deleted below.
        conn.execute(
            "DELETE FROM fin_parser.outbox WHERE payload->>'job_id' IN ("
            "  SELECT job_id::text FROM fin_parser.job WHERE idempotency_key LIKE %s)",
            (f"{RUN_PREFIX}%",),
        )
        conn.execute(
            "DELETE FROM fin_parser.job WHERE idempotency_key LIKE %s",
            (f"{RUN_PREFIX}%",),
        )
        conn.execute(
            "DELETE FROM fin_parser.tombstone WHERE idempotency_key LIKE %s",
            (f"{RUN_PREFIX}%",),
        )
        conn.commit()


__all__ = ["RUN_PREFIX", "TEST_DSN_ENV", "parser_dsn"]
