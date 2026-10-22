"""Full fin-to-iknow SQL retrieval against a disposable ``_test`` database.

This opt-in acceptance starts its own local PostgreSQL container, loads only
the committed synthetic ledger, and drives the real iknow MCP process through
fin's retrieve adapter. It does not depend on a developer business database or
iknow's document-index PostgreSQL.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from fin.host.evidence.evidence_contract import seed_context_from_rows, validate_evidence
from fin.retrieve.port import DATABASE_ROWS_KEY
from live_iknow_support import IKNOW_ROOT, live_iknow

pytestmark = pytest.mark.sql_live

HERE = Path(__file__).parent
LEDGER_FIXTURES = HERE / "fixtures" / "ledger"
SCHEMA_SQL = (LEDGER_FIXTURES / "001_schema.sql").read_text()
SEED_SQL = (LEDGER_FIXTURES / "002_seed.sql").read_text()
TEST_DATABASE = "fin_business_test"
POSTGRES_IMAGE = "postgres:16-alpine"
EXPECTED_IDS = ["C-2315", "C-2302", "C-2291", "C-2024Q1"]
_PORT_MAPPING = re.compile(r"^127\.0\.0\.1:(\d+)$")

_READER_SQL = f"""
CREATE ROLE fin_sql_reader LOGIN;
GRANT CONNECT ON DATABASE {TEST_DATABASE} TO fin_sql_reader;
GRANT USAGE ON SCHEMA ledger_ro TO fin_sql_reader;
GRANT SELECT ON ledger_ro.contract_expiry TO fin_sql_reader;
ALTER ROLE fin_sql_reader SET default_transaction_read_only = on;
ALTER ROLE fin_sql_reader SET statement_timeout = '3s';
ALTER ROLE fin_sql_reader SET lock_timeout = '1s';
ALTER ROLE fin_sql_reader SET idle_in_transaction_session_timeout = '10s';
"""


def _database_name_from_dsn(dsn: str) -> str:
    """Fail closed unless the DSN names a database whose name ends in _test."""
    try:
        database = conninfo_to_dict(dsn).get("dbname", "")
    except Exception as exc:
        raise ValueError("business test DSN is invalid") from None
    if not database.endswith("_test"):
        raise ValueError("business tests may connect only to a database ending in _test")
    return database


def _verify_database_identity(dsn: str) -> None:
    """Check the DSN name before connect and the server identity after connect."""
    expected_database = _database_name_from_dsn(dsn)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            row = connection.execute(
                "SELECT current_database(), current_user, "
                "current_setting('default_transaction_read_only')"
            ).fetchone()
    except psycopg.Error as exc:
        pytest.fail(
            "could not verify the isolated business test database "
            f"({type(exc).__name__})"
        )
    if row is None or row[0] != expected_database or not row[0].endswith("_test"):
        pytest.fail("the connected business database is not the requested _test database")
    if row[1] != "fin_sql_reader" or row[2] != "on":
        pytest.fail("the business test connection is not the read-only ledger reader")


def _docker_run(
    args: list[str], *, timeout: float = 30, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        pytest.fail("Docker is required for the isolated SQL acceptance database")
    except subprocess.TimeoutExpired:
        pytest.fail("Docker timed out while preparing the isolated SQL acceptance database")


def _psql_script(container: str, sql: str) -> str:
    result = _docker_run(
        [
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            TEST_DATABASE,
            "-f",
            "-",
        ],
        timeout=30,
        input_text=sql,
    )
    if result.returncode != 0:
        pytest.fail("could not prepare the synthetic business test database")
    return result.stdout.strip()


@contextmanager
def _isolated_ledger_database() -> Iterator[str]:
    """Start, verify, seed, and remove one disposable PostgreSQL _test DB."""
    container = f"fin-sql-acceptance-{uuid.uuid4().hex[:12]}"
    try:
        result = _docker_run(
            [
                "run",
                "--rm",
                "--detach",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                f"POSTGRES_DB={TEST_DATABASE}",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--name",
                container,
                POSTGRES_IMAGE,
            ],
            timeout=90,
        )
        if result.returncode != 0:
            pytest.fail("could not start the isolated PostgreSQL test container")
        port_output = _docker_run(["port", container, "5432/tcp"])
        mapping = _PORT_MAPPING.fullmatch(port_output.stdout.strip())
        if port_output.returncode != 0 or mapping is None:
            pytest.fail("could not determine the isolated PostgreSQL test port")
        dsn = (
            f"postgresql://fin_sql_reader@127.0.0.1:{mapping.group(1)}"
            f"/{TEST_DATABASE}"
        )
        _database_name_from_dsn(dsn)

        deadline = time.monotonic() + 30
        while True:
            ready = _docker_run(
                ["exec", container, "pg_isready", "-U", "postgres", "-d", TEST_DATABASE]
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                pytest.fail("isolated PostgreSQL test database did not become ready")
            time.sleep(0.25)

        # Verify the server's actual target before loading even synthetic rows.
        observed_database = _psql_script(container, "SELECT current_database();")
        if observed_database != TEST_DATABASE:
            pytest.fail(
                "the isolated PostgreSQL container opened an unexpected database "
                f"({observed_database!r})"
            )
        _psql_script(container, SCHEMA_SQL + "\n" + SEED_SQL)
        _psql_script(container, _READER_SQL)
        _verify_database_identity(dsn)
        yield dsn
    finally:
        _docker_run(["rm", "--force", container])


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader@localhost/fin_business",
        "postgresql://reader@localhost/",
        "not-a-postgresql-dsn",
    ],
)
def test_business_database_guard_rejects_non_test_targets_before_connect(
    dsn: str,
) -> None:
    with pytest.raises(ValueError):
        _database_name_from_dsn(dsn)


def test_fin_retrieve_returns_synthetic_ledger_rows_from_test_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The real MCP producer answers fin's sql retrieve from an isolated DB."""
    if shutil.which("node") is None:
        pytest.fail("Node.js is required for the fin-to-iknow SQL acceptance")
    if not (IKNOW_ROOT / "dist" / "index.js").is_file():
        pytest.fail("the built iknow-memory MCP server is required for SQL acceptance")

    with _isolated_ledger_database() as business_dsn:
        # Keep iknow away from the operator's main settings home. Its document
        # store is the test harness's in-memory backend; only the business query
        # opens PostgreSQL, and the DSN above has already passed both guards.
        monkeypatch.setenv("IKNOW_MEMORY_HOME", str(tmp_path / "iknow-memory-home"))
        monkeypatch.setenv("FIN_BUSINESS_LEDGER_URL", business_dsn)
        with live_iknow(monkeypatch, storage="memory") as live:
            result = live.retrieve.retrieve(
                "Which contracts expire and when is notice due?",
                search_mode="sql",
                sql_window_start="2026-01-01",
                sql_window_end="2026-04-30",
            )

    assert "error" not in result, result
    assert result.get("chunks") == [], result
    envelope = result.get(DATABASE_ROWS_KEY)
    assert isinstance(envelope, dict), result
    assert envelope["schema"] == "fin.evidence.retrieve_rows"
    assert envelope["schema_version"] == 1
    assert envelope["result_count"] == len(envelope["rows"]) == 4
    rows = envelope["rows"]
    assert [row["record_key"]["contract_id"] for row in rows] == EXPECTED_IDS
    assert all(row["source"] == "ledger_ro.contract_expiry" for row in rows)
    assert all(set(row["record_key"]) == {"contract_id"} for row in rows)

    context = seed_context_from_rows(database_row_values=rows)
    validation = validate_evidence(result, context)
    assert validation.refusal is None, validation
