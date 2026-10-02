"""§9 leg-2 acceptance — the fixed `contracts_expiring` template on a real PostgreSQL.

Opt-in live driver (``sql_live`` mark — its own mark, kept distinct from
the ``mcp`` fixture-server protocol smoke; ``tests/retrieve/conftest.py``
deselects it from the default hermetic run). Opt-in command::

    set -a; source ~/.fin_dev/sql_reader.env; set +a
    uv run pytest -q -m sql_live tests/retrieve/test_live_sql_business_db.py -v

What this proves, and where (spec §9 leg 2, plan T3/T8 acceptance):

* the REAL iknow ``BusinessQueryExecutor`` build (Node subprocess, covered in
  ``live_sql_business_driver.mjs``) runs the fixed named template against the
  live dev instance on 5434 as ``fin_sql_reader`` and returns exactly the rows
  the committed fixture DDL independently declares — expected values are
  derived from ``fixtures/ledger/002_seed.sql`` text in this file, never read
  back out of the database;
* a zero-row window is the legitimate empty outcome (``sql_no_matching_rows``),
  observed as a different token from a denial (``sql_query_denied``, covered by
  the privilege probes in ``test_ledger_readonly_boundary.py``) and from an
  unavailability (``retrieve_unavailable``, observed here against a dead port);
* a template name outside the closed set is refused **before a connection is
  opened** — proven by asking for it against an unreachable instance and
  receiving the refusal token, not an unavailability;
* every row carries a ``queried_at`` from this round (timestamp recorded);
* re-applying 001+002 into two fresh scratch schemas reproduces the same rows,
  so the fixture, not the incidentally-loaded database, is the source of truth.

What this driver does NOT cover (recorded per spec §9's mandatory rule):

* seam NOT covered: fin's host / retrieve port / MCP transport above iknow —
  the fin-side wiring is the concurrent T7 task, and the full stack stays for
  the combined review run. This file stops at iknow's executor and the
  PostgreSQL boundary.
* leg 1 (the operator's real ledger) cannot run until a source is identified;
  blocked evidence, not a pass.
* the browser journey (B3) and the dashboard ``GET /api/query`` path.

No credential or DSN with a password appears in this file, in its output, or
in the JSON the Node driver prints: the reader DSN travels only through the
subprocess environment.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Same directory (pytest inserts the test dir on sys.path); the boundary
# probes share these constants, and importing rather than re-declaring them
# keeps one source of truth for the expected rows.
from test_ledger_readonly_boundary import (
    EXPECTED_IN_WINDOW,
    TOTAL_SEEDED,
    VISIBLE_THROUGH_VIEW,
)

pytestmark = pytest.mark.sql_live

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "ledger"
SCHEMA_SQL = (FIXTURES / "001_schema.sql").read_text()
SEED_SQL = (FIXTURES / "002_seed.sql").read_text()

READER_URL = os.environ.get("FIN_SQL_READER_URL", "")
IKNOW_ROOT = Path(os.environ.get("FIN_LIVE_IKNOW_ROOT")
                  or "/home/winner/projects/iknow-memory")
IKNOW_DIST = IKNOW_ROOT / "dist"
DB_CONTAINER = os.environ.get("FIN_SQL_DB_CONTAINER", "fin-sql-business-db")

# The deliberate-unreachable probe endpoint: no secret (no password), a port
# nothing listens on. Used to distinguish "refused before connecting" from
# "could not connect".
PROBE_URL = "postgresql://fin_sql_reader@127.0.0.1:5999/nosuchdb"

WINDOW_START, WINDOW_END = "2026-01-01", "2026-04-30"

# The executor's field projection for `contracts_expiring`, restated here from
# spec §2.3 / the template definition so a silent projection change breaks a
# test rather than the operator's answer.
TEMPLATE_FIELDS = [
    "title", "counterparty", "expiry_date", "notice_by",
    "total_amount", "currency", "owner_dept",
]


def _require_live_prereqs() -> None:
    if not READER_URL:
        pytest.fail(
            "FIN_SQL_READER_URL is unset: the live leg-2 run cannot execute. "
            "Blocked evidence, not a skip (.claude/rules/test.md)."
        )
    executor_js = IKNOW_DIST / "core" / "business-query" / "sql-query-executor.js"
    if not executor_js.is_file():
        pytest.fail(
            f"iknow business-query executor build not found at {executor_js}; "
            "build the branch checkout (npm run build) or point "
            "FIN_LIVE_IKNOW_ROOT at it. Blocked evidence, not a skip."
        )
    proc = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if proc.returncode != 0 or int(proc.stdout.lstrip("v").split(".")[0]) < 20:
        pytest.fail("node >= 20 is required to drive the iknow executor. "
                    "Blocked evidence, not a skip.")


def _run_driver(action: str) -> dict:
    _require_live_prereqs()
    proc = subprocess.run(
        ["node", str(HERE / "live_sql_business_driver.mjs"), action],
        capture_output=True,
        text=True,
        env={**os.environ,
             "FIN_SQL_READER_URL": READER_URL,
             "FIN_SQL_PROBE_URL": PROBE_URL,
             "IKNOW_DIST_ROOT": str(IKNOW_DIST)},
    )
    if proc.returncode != 0:
        pytest.fail(f"driver action {action!r} exited {proc.returncode}: "
                    f"{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _admin_psql(sql: str | None = None, stdin_script: str | None = None) -> str:
    """Admin access via the container-local trust socket — no password in the
    environment or on any visible command line. ``sql`` must be a read-only
    statement or DDL against scratch schemas this test itself created; a
    script goes through ``-f -`` so psql actually reads it (``-c`` ignores
    stdin)."""
    args = ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", "fin_sql_admin",
            "-d", "fin_business", "-v", "ON_ERROR_STOP=1"]
    if stdin_script is not None:
        args += ["-At", "-f", "-"]
    else:
        assert sql is not None and sql.lstrip().upper().startswith(("SELECT", "CREATE", "DROP"))
        args += ["-Atc", sql]
    proc = subprocess.run(args, input=stdin_script or "", capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"admin psql failed: {proc.stderr.strip()}. Blocked "
                    "evidence, not a skip.")
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# Expected values derived from the fixture DDL text — independently authored.
# --------------------------------------------------------------------------

_CONTRACT_ROW = re.compile(
    r"\('(C-[A-Z0-9]+)',\s*'(CP-\d+)',\s*'[^']*',\s*'[^']*',\s*"
    r"'(\d{4}-\d{2}-\d{2})',\s*'(\d{4}-\d{2}-\d{2})',\s*"
    r"(?:true|false),\s*(\d+),.*?'(active|terminated|expired|renewed)'",
    re.DOTALL,
)
_COUNTERPARTY_ROW = re.compile(r"\('(CP-\d+)',\s*'([^']*)'")


def expected_from_ddl(
    window_start: str = WINDOW_START, window_end: str = WINDOW_END
) -> list[tuple[str, str, str, str]]:
    """Compute the permitted view's window rows from 001/002 TEXT.

    Mirrors the view definition (join counterparty, ``status = 'active'`` only,
    ``notice_by = expiry_date - notice_days``) and the template's ORDER BY
    (expiry_date, contract_id). The literals come from the committed seed; the
    database is never consulted, so a broken seed fails instead of passing.
    """
    names = dict(_COUNTERPARTY_ROW.findall(SEED_SQL.split("ledger.contract")[0]))
    rows = []
    for match in _CONTRACT_ROW.finditer(SEED_SQL):
        contract_id, cp_id, effective, expiry, notice_days, status = match.groups()
        if status != "active":  # the view's predicate, restated from 001
            continue
        expiry_d = _dt.date.fromisoformat(expiry)
        notice_by = (expiry_d - _dt.timedelta(days=int(notice_days))).isoformat()
        if _dt.date.fromisoformat(window_start) <= expiry_d <= _dt.date.fromisoformat(window_end):
            rows.append((contract_id, names[cp_id], expiry, notice_by))
    return sorted(rows, key=lambda r: (r[2], r[0]))


def test_expected_rows_are_derived_from_ddl_not_the_database() -> None:
    """The driver's DDL-derived expectation equals the committed boundary
    expectation; both are text-derived. If the seed literals ever change,
    this fails until the constants move with them."""
    assert expected_from_ddl() == EXPECTED_IN_WINDOW
    # Spot facts the plan's T3 progress and the task name independently.
    assert ("C-2291", "Northwind Logistics", "2026-03-14", "2026-02-12") in EXPECTED_IN_WINDOW
    ddl_all = _CONTRACT_ROW.findall(SEED_SQL)
    assert len(ddl_all) == TOTAL_SEEDED
    assert sum(1 for r in ddl_all if r[5] == "active") == VISIBLE_THROUGH_VIEW
    # The terminated row would be inside the window on its date; only the
    # view's status predicate hides it — that is the DB-side assertion below.
    terminated = [r for r in ddl_all if r[5] == "terminated"]
    assert len(terminated) == 1 and terminated[0][0] == "C-2320"
    assert _dt.date.fromisoformat(WINDOW_START) <= _dt.date.fromisoformat(terminated[0][3]) \
        <= _dt.date.fromisoformat(WINDOW_END)


# --------------------------------------------------------------------------
# Live executor against 5434 (real boundary).
# --------------------------------------------------------------------------

def test_executor_returns_the_ddl_declared_window_rows() -> None:
    payload = _run_driver("window")
    assert payload["kind"] == "result"
    assert payload["source"] == "ledger_ro.contract_expiry"
    assert payload["truncated"] is False
    rows = payload["rows"]
    observed = [
        (r["record_key"]["contract_id"], r["fields"]["counterparty"],
         r["fields"]["expiry_date"], r["fields"]["notice_by"])
        for r in rows
    ]
    assert observed == expected_from_ddl()
    assert not any(r["record_key"]["contract_id"] == "C-2320" for r in rows)
    for row in rows:
        assert list(row["fields"]) == TEMPLATE_FIELDS
        # One queried_at for the round, from this run.
        stamp = _dt.datetime.fromisoformat(row["queried_at"].replace("Z", "+00:00"))
        age = (_dt.datetime.now(_dt.timezone.utc) - stamp).total_seconds()
        assert 0 <= age < 300, f"queried_at {row['queried_at']} is not from this round"


def test_zero_row_window_is_the_legitimate_empty_outcome() -> None:
    """Empty names the database and the window — it is not a denial, not an
    unavailability, and not advice to ingest documents (plan T2 decision)."""
    payload = _run_driver("empty")
    assert payload["kind"] == "error"
    assert payload["token"] == "sql_no_matching_rows"
    assert "ledger_ro.contract_expiry" in payload["message"]
    assert "2020-01-01..2020-12-31" in payload["message"]
    assert "doc" not in payload["message"].lower()  # no ingest advice
    # Distinct from the other classes, all four observed separately:
    denied = "sql_query_denied"       # boundary probes hold 42501/25006 evidence
    timeout = "sql_query_timeout"
    unavailable = _run_driver("unreachable")
    assert unavailable["token"] == "retrieve_unavailable"
    assert len({payload["token"], denied, timeout, unavailable["token"]}) == 4


def test_out_of_template_query_is_refused_before_a_connection() -> None:
    """The template name is refused while the instance is unreachable: if the
    executor had opened a connection first, the token would have been
    ``retrieve_unavailable``. The observed token therefore proves refusal
    happens pre-connection (§5c), for a name outside the closed set."""
    payload = _run_driver("unknown-template")
    assert payload["kind"] == "error"
    assert payload["token"] == "invalid_business_query"
    assert "free_form_probe" in payload["message"]


# --------------------------------------------------------------------------
# Rebuild determinism — non-destructive (scratch schemas, then dropped).
# --------------------------------------------------------------------------

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _rebuild(scratch: str) -> None:
    """Apply 001+002 into freshly-named scratch schemas. The shared seeded
    instance is never dropped or rewritten; these schemas are created by this
    test and dropped by the caller."""
    for name in (f"ledger_{scratch}", f"ledger_ro_{scratch}"):
        assert _IDENT.match(name)
    base = f"ledger_{scratch}"
    ro = f"ledger_ro_{scratch}"
    ddl = (SCHEMA_SQL + "\n" + SEED_SQL).replace("ledger_ro", ro)
    ddl = re.sub(r"\bledger\b", base, ddl)
    _admin_psql(f"CREATE SCHEMA {base}; CREATE SCHEMA {ro};")
    _admin_psql(stdin_script=ddl)


def test_rebuilding_the_fixture_produces_the_same_rows() -> None:
    """Two scratch rebuilds from the committed fixture each reproduce the
    DDL-declared counts and window rows — rebuilding produces the same rows
    (plan T3 acceptance), and the expectation still comes from the DDL text."""
    suffixes = [uuid.uuid4().hex[:8] for _ in range(2)]
    schemas = [f"det_a3a_{s}" for s in suffixes]
    try:
        for scratch in schemas:
            _rebuild(scratch)
            base = f"ledger_{scratch}"
            ro = f"ledger_ro_{scratch}"
            assert int(_admin_psql(f"SELECT count(*) FROM {base}.contract")) == TOTAL_SEEDED
            assert int(_admin_psql(f"SELECT count(*) FROM {ro}.contract_expiry")) == VISIBLE_THROUGH_VIEW
            rows = _admin_psql(
                f"SELECT contract_id, counterparty, expiry_date, notice_by::date"
                f" FROM {ro}.contract_expiry"
                f" WHERE expiry_date BETWEEN '{WINDOW_START}' AND '{WINDOW_END}'"
                f" ORDER BY expiry_date, contract_id"
            )
            observed = [tuple(line.split("|")) for line in rows.splitlines()]
            assert observed == expected_from_ddl()
    finally:
        # Only the scratch schemas this test created are dropped; the shared
        # seeded instance and its grants are untouched.
        for scratch in schemas:
            _admin_psql(f"DROP SCHEMA IF EXISTS ledger_{scratch} CASCADE;"
                        f" DROP SCHEMA IF EXISTS ledger_ro_{scratch} CASCADE;")


# --------------------------------------------------------------------------
# Record support: environment versions (sanitized) for the leg-2 record.
# --------------------------------------------------------------------------

def test_record_versions(capsys: pytest.CaptureFixture) -> None:
    """Print the sanitized versions block the §9 record requires: fin HEAD,
    iknow HEAD, docker image — never the DSN. Asserted, not just printed, so a
    silent environment drift fails the run that cites this record."""
    fin_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE.parents[1],
                              capture_output=True, text=True).stdout.strip()
    iknow_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=IKNOW_ROOT,
                                capture_output=True, text=True).stdout.strip()
    image = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}",
                            DB_CONTAINER], capture_output=True, text=True).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", fin_head), fin_head
    assert re.fullmatch(r"[0-9a-f]{40}", iknow_head), iknow_head
    assert image == "postgres:16-alpine", image
    print(f"\n[leg-2 record] fin HEAD={fin_head} iknow HEAD={iknow_head} "
          f"postgres image={image}")
