"""Shared PostgreSQL plumbing for the durable Runtime store suites.

Each suite owns a throwaway schema in the shared ``_test`` database and
drops it at teardown, so suites may run in any order and in parallel
processes without seeing each other's rows. A missing ``FIN_TEST_DSN`` is a
named failure, never a skip: these suites exist to exercise real PostgreSQL.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_SCHEMA_RE = re.compile(r"^fin_runtime_[a-z0-9_]+$")


def test_dsn() -> str:
    """The isolated test database, or a named failure."""
    dsn = (os.environ.get("FIN_TEST_DSN") or "").strip()
    if not dsn:
        pytest.fail(
            "FIN_TEST_DSN is not set. The durable Runtime suites need the "
            "isolated PostgreSQL _test database. A missing DSN is a failure, "
            "never a skip."
        )
    return dsn


def create_schema(dsn: str, prefix: str) -> str:
    import psycopg
    from psycopg import sql

    schema = f"{prefix}_{uuid.uuid4().hex[:12]}"
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    return schema


def scoped_url(dsn: str, schema: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, options=f"-c search_path={schema}")


def drop_schema(dsn: str, schema: str) -> None:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn) as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )


def run_child(
    code: str,
    *,
    dsn: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``code`` in a fresh OS process against the same schema.

    The child gets the scoped DSN through ``FIN_TEST_DSN`` and the worktree's
    ``src`` on ``PYTHONPATH``. Its stdout must be one JSON object; the DSN
    never appears in a failure message.
    """
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(SRC_ROOT)
    child_env["FIN_TEST_DSN"] = dsn
    child_env.pop("FIN_CHECKPOINT_URL", None)
    if env:
        child_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.replace(dsn, "<redacted>")[-4000:]
        pytest.fail(f"fresh runtime process failed: {stderr}")
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        pytest.fail(
            "fresh runtime process did not print one JSON object: "
            f"{completed.stdout[:1000]!r}"
        )
    if not isinstance(payload, dict):
        pytest.fail("fresh runtime process printed a non-object JSON value")
    return payload


def validate_test_database(dsn: str) -> None:
    """Guard the suites that connect without the pytest conftest gate."""
    from psycopg.conninfo import conninfo_to_dict

    settings = conninfo_to_dict(dsn)
    database = settings.get("dbname", "")
    if not isinstance(database, str) or not database.endswith("_test"):
        pytest.fail("FIN_TEST_DSN must name a database ending in _test")
