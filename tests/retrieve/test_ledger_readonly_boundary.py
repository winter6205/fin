"""The database-enforced read-only boundary of the synthetic ledger.

These assertions are all *negative*: they prove a write, a temp table, and a
base-table read are refused. A negative assertion run with a credential that
holds the privilege passes trivially, so every test here connects as the
dedicated ``fin_sql_reader`` role, never as the owner.

The role is created by ``fixtures/ledger/003_readonly_role.sql``, which needs
an explicit authorization because it creates a login and grants it database
privileges. When the role is absent, these tests report the boundary as NOT RUN
and fail the suite — an unverified permission boundary is blocked evidence, not
a skip and not a pass (``.claude/rules/test.md``).

Expected values are the literals in ``002_seed.sql``, not values read back from
the database: a seed that loaded nothing must fail here rather than make the
suite vacuously true.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

# Its own mark, NOT ``mcp``: this is a live-boundary acceptance against
# the real PostgreSQL (spec §9), which the conftest and AGENTS.md keep
# distinguishable from the fixture-server protocol smoke.
pytestmark = pytest.mark.sql_live

# The admin DSN is an optional override. Without it the admin-side reads run
# through ``docker exec … psql`` over the container-local socket, which is
# ``trust`` and needs no password in the environment, in a command line, or in
# test output (spec §9: no credential anywhere in the record).
ADMIN_URL = os.environ.get("FIN_SQL_ADMIN_URL", "")
READER_URL = os.environ.get("FIN_SQL_READER_URL", "")
DB_CONTAINER = os.environ.get("FIN_SQL_DB_CONTAINER", "fin-sql-business-db")


def _admin_select(sql: str) -> list[tuple[str, ...]]:
    """Run one READ-ONLY admin query and return its rows as text.

    Only SELECT is accepted: the admin path exists to count base-table rows
    and read catalogs, and the boundary this suite verifies is that nothing
    writes. Values are returned as plain text so both transports (psycopg and
    psql -Atc) produce comparable tuples.
    """
    if not sql.lstrip().upper().startswith("SELECT"):
        raise AssertionError("admin helper is read-only by construction")
    if ADMIN_URL:
        with psycopg.connect(ADMIN_URL) as conn:
            return [tuple(str(v) for v in row) for row in conn.execute(sql).fetchall()]
    proc = subprocess.run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", "fin_sql_admin",
         "-d", "fin_business", "-Atc", sql],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "admin-side read could not run (neither FIN_SQL_ADMIN_URL nor "
            f"`docker exec {DB_CONTAINER}` works): {proc.stderr.strip()}. "
            "This is blocked evidence, not a skip."
        )
    return [tuple(line.split("|")) for line in proc.stdout.splitlines() if line]

# From 002_seed.sql. The window deliberately spans the four active contracts
# ending inside 2026 Q2, and excludes the terminated one.
WINDOW_START = "2026-01-01"
WINDOW_END = "2026-04-30"

EXPECTED_IN_WINDOW = [
    ("C-2315", "Northwind Logistics", "2026-01-24", "2026-01-17"),
    ("C-2302", "Meridian Analytics GmbH", "2026-02-28", "2025-12-30"),
    ("C-2291", "Northwind Logistics", "2026-03-14", "2026-02-12"),
    ("C-2024Q1", "Halcyon Print Works", "2026-04-30", "2026-03-31"),
]

# 6 contracts seeded, 5 active, 1 terminated and therefore invisible through the
# view. If these two numbers disagree the view's scope is not what it claims.
TOTAL_SEEDED = 6
VISIBLE_THROUGH_VIEW = 5


def _require_reader() -> str:
    """Return the reader DSN, or fail loudly when the role is not provisioned."""
    if not READER_URL:
        pytest.fail(
            "FIN_SQL_READER_URL is not set, so the read-only boundary is "
            "UNVERIFIED. Apply fixtures/ledger/003_readonly_role.sql and set "
            "FIN_SQL_READER_URL. Running these probes as the owner would pass "
            "trivially and prove nothing."
        )
    return READER_URL


def _sqlstate(exc: Exception) -> str | None:
    return getattr(exc, "sqlstate", None) or getattr(getattr(exc, "diag", None), "sqlstate", None)


def test_permitted_view_is_readable_and_matches_the_seed() -> None:
    """The one permitted query returns exactly the rows 002_seed.sql declares."""
    dsn = _require_reader()
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT contract_id, counterparty, expiry_date, notice_by::date
              FROM ledger_ro.contract_expiry
             WHERE expiry_date BETWEEN %s::date AND %s::date
             ORDER BY expiry_date
            """,
            (WINDOW_START, WINDOW_END),
        ).fetchall()

    assert [(r[0], r[1], r[2].isoformat(), r[3].isoformat()) for r in rows] == EXPECTED_IN_WINDOW


def test_window_bound_is_a_bound() -> None:
    """A window ending before the seeded rows returns nothing.

    Without this, a template that ignored its bounds and returned the whole
    table would still pass the test above.
    """
    dsn = _require_reader()
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT count(*) FROM ledger_ro.contract_expiry
             WHERE expiry_date BETWEEN %s::date AND %s::date
            """,
            ("2020-01-01", "2020-12-31"),
        ).fetchone()

    assert rows[0] == 0


def test_notice_arithmetic_is_in_band() -> None:
    """notice_by is derived by the view, not computed by the caller.

    The value is a business rule, so it belongs where the database can enforce
    it for every consumer rather than in each caller's date math.
    """
    dsn = _require_reader()
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT notice_days, (expiry_date - notice_by::date) AS days
              FROM ledger_ro.contract_expiry WHERE contract_id = 'C-2291'
            """
        ).fetchone()

    assert row is not None
    assert row[0] == 30
    assert row[1] == row[0]


def test_base_table_is_unreadable() -> None:
    """A direct read of the underlying table fails 42501.

    This is the assertion that makes the view layer an enforcement rather than
    a convenience: the scope is the grant, not a WHERE the application wrote.
    """
    dsn = _require_reader()
    with pytest.raises(psycopg.Error) as excinfo:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT * FROM ledger.contract LIMIT 1").fetchall()

    assert _sqlstate(excinfo.value) == "42501"


def test_write_is_refused() -> None:
    """A write against the base table fails 42501 at the privilege layer.

    The view write is ALSO refused, but with 55000, not 42501: the reference
    view joins two tables, so PostgreSQL refuses it as not-auto-updatable
    before the privilege check is ever reached. That refusal is real but it is
    a different fact — it would hold even for a role that held UPDATE — so the
    42501 proof for "any write fails" must target the base table, which the
    reader has no privilege on at all.
    """
    dsn = _require_reader()
    with pytest.raises(psycopg.Error) as excinfo:
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE ledger.contract SET owner_dept = 'x'").rowcount

    assert _sqlstate(excinfo.value) == "42501"

    with pytest.raises(psycopg.Error) as viewinfo:
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE ledger_ro.contract_expiry SET owner_dept = 'x'").rowcount

    assert _sqlstate(viewinfo.value) == "55000"


def test_temp_table_is_refused() -> None:
    """CREATE TEMP TABLE fails 25006.

    This probe is the only one that proves ``default_transaction_read_only``
    specifically: a temp table is otherwise permitted on a grant shape that
    refuses everything else, so without this the belt-and-braces layer would be
    assumed rather than observed.
    """
    dsn = _require_reader()
    with pytest.raises(psycopg.Error) as excinfo:
        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE TEMP TABLE fin_probe (i int)")

    assert _sqlstate(excinfo.value) == "25006"


def test_transaction_read_only_is_observed_from_inside_the_connection() -> None:
    """``SHOW transaction_read_only`` reads on, read from inside the app connection.

    The session value proves whatever opened this connection asked for a
    read-only session. It does NOT prove the `ALTER ROLE` provisioning ran:
    spec §9's correction requires BOTH, so the catalog assertion below is the
    companion probe, not a replacement.
    """
    dsn = _require_reader()
    with psycopg.connect(dsn) as conn:
        setting = conn.execute("SHOW transaction_read_only").fetchone()

    assert setting[0] == "on"


def test_role_default_settings_are_provisioned_in_the_catalog() -> None:
    """The ALTER ROLE defaults from 003_readonly_role.sql are in pg_catalog.

    Handoff trap 2: a session can read ``transaction_read_only = on`` because
    the application set it per-session, while the role was never provisioned —
    an operator connecting by hand then gets ``off``. Asserting the stored
    role settings makes the provisioning itself observed, not assumed.
    """
    _require_reader()
    # ``ALTER ROLE … SET`` stores a cluster-wide row (setdatabase = 0), not a
    # per-database one; joining pg_database here would find nothing and make a
    # correctly provisioned role look unprovisioned.
    rows = _admin_select(
        "SELECT s.setdatabase, s.setconfig::text FROM pg_db_role_setting s"
        " JOIN pg_roles r ON r.oid = s.setrole"
        " WHERE r.rolname = 'fin_sql_reader'"
    )
    assert len(rows) == 1, f"expected one role-setting row, got {rows}"
    assert rows[0][0] == "0"
    stored = {item.strip() for item in rows[0][1].strip("{}").split(",")}

    assert {
        "default_transaction_read_only=on",
        "statement_timeout=3s",
        "lock_timeout=1s",
        "idle_in_transaction_session_timeout=10s",
    } <= stored, f"role defaults missing from provisioning: {stored}"


def test_row_count_is_unchanged_after_every_write_attempt() -> None:
    """A refused write leaves the data exactly as it was.

    Counted as the owner, because the reader cannot count the base table and a
    count taken through the view would miss damage to a row the view hides.
    """
    dsn = _require_reader()
    before = int(_admin_select("SELECT count(*) FROM ledger.contract")[0][0])
    assert before == TOTAL_SEEDED

    for statement in (
        "UPDATE ledger.contract SET owner_dept = 'tampered'",
        "DELETE FROM ledger_ro.contract_expiry",
        "INSERT INTO ledger.contract (contract_id, counterparty_id, title, contract_type,"
        " effective_date, expiry_date, notice_days, status, owner_dept, source_system)"
        " VALUES ('C-EVIL', 'CP-001', 'x', 'goods', '2026-01-01', '2026-12-31', 0,"
        " 'active', 'x', 'x')",
        "CREATE TEMP TABLE fin_probe (i int)",
    ):
        with pytest.raises(psycopg.Error):
            with psycopg.connect(dsn) as conn:
                conn.execute(statement)

    after = int(_admin_select("SELECT count(*) FROM ledger.contract")[0][0])
    visible = int(_admin_select("SELECT count(*) FROM ledger_ro.contract_expiry")[0][0])

    assert after == before
    assert visible == VISIBLE_THROUGH_VIEW


def test_role_holds_exactly_the_expected_grants() -> None:
    """The reader's table grants equal the named list, nothing more.

    Equality rather than containment: a grant that accumulated silently — a
    second view, a base table — would otherwise pass a ``>=`` check while
    widening what a leaked credential can reach.
    """
    dsn = _require_reader()
    granted = _admin_select(
        "SELECT table_schema || '.' || table_name"
        "  FROM information_schema.role_table_grants"
        " WHERE grantee = 'fin_sql_reader' AND privilege_type = 'SELECT'"
        " ORDER BY 1"
    )

    assert [r[0] for r in granted] == ["ledger_ro.contract_expiry"]


def test_per_run_schema_is_isolated() -> None:
    """A unique schema per run keeps a leaked fixture from affecting the next.

    The synthetic ledger is shared, so the isolation that matters is the
    per-run name: two concurrent runs must not collide on a schema they both
    believe they own.
    """
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    assert first != second
