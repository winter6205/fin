#!/usr/bin/env python3
"""Verify the demo acceptance PostgreSQL targets are isolated ``_test`` DBs.

DSNs are accepted only from the environment. They are never included in
arguments, output, or exception text. Both requested names are checked before
opening either connection, then each server identity is verified with one
read-only identity query.
"""

from __future__ import annotations

import os
import sys


_TARGETS = ("FIN_CHECKPOINT_URL", "GRAPHRAG_MEMORY_DB_URL")


def _fail(message: str) -> int:
    print(f"demo acceptance: {message}", file=sys.stderr)
    return 2


def main() -> int:
    values = {name: os.environ.get(name, "").strip() for name in _TARGETS}
    for name, dsn in values.items():
        if not dsn:
            return _fail(f"missing required environment setting {name}")

    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        return _fail("PostgreSQL client support is unavailable in the current uv environment")

    expected: dict[str, str] = {}
    for name, dsn in values.items():
        try:
            connection_settings = conninfo_to_dict(dsn)
        except Exception:
            return _fail(f"{name} is not a valid PostgreSQL connection setting")
        if "service" in connection_settings or "passfile" in connection_settings:
            return _fail(f"{name} must provide its connection settings through the environment")
        database = connection_settings.get("dbname", "")
        if not isinstance(database, str) or not database.endswith("_test"):
            return _fail(f"{name} must target a database ending in _test")
        expected[name] = database

    for name, dsn in values.items():
        try:
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                row = connection.execute("SELECT current_database()").fetchone()
        except Exception:
            return _fail(f"could not verify the {name} PostgreSQL target")
        if (
            row is None
            or not isinstance(row[0], str)
            or row[0] != expected[name]
            or not row[0].endswith("_test")
        ):
            return _fail(f"the connected {name} database identity is not the requested _test target")

    print("demo acceptance: both PostgreSQL targets verified as _test databases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
