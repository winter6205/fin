#!/usr/bin/env python3
"""Release the demo checkpoint database's instance claim.

Every browser-acceptance run gets a throwaway ``FIN_HOME``. The demo
checkpoint database is a dedicated ``_test`` target that outlives that home,
so the claim a previous run wrote into it would block the next one: the
product refuses to serve a database whose recorded instance has no home
("file missing database <id>"), and the run would die before the browser
ever opened.

This script releases that claim, and only that claim, so the fresh home can
take it. The database is addressed only through the environment, only after
it has been verified to be a ``_test`` target, and no DSN is ever printed.
"""

from __future__ import annotations

import os
import sys


_TARGET_ENV = "FIN_CHECKPOINT_URL"
_CLAIM_TABLE = "fin_instance"


def _fail(message: str) -> int:
    print(f"demo acceptance: {message}", file=sys.stderr)
    return 2


def _verified_database(dsn: str) -> str:
    """Return the database name after proving it is a ``_test`` target."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        connection_settings = conninfo_to_dict(dsn)
    except Exception:
        raise ValueError(f"{_TARGET_ENV} is not a valid PostgreSQL connection setting")
    if "service" in connection_settings or "passfile" in connection_settings:
        raise ValueError(f"{_TARGET_ENV} must provide its connection settings through the environment")
    database = connection_settings.get("dbname", "")
    if not isinstance(database, str) or not database.endswith("_test"):
        raise ValueError(f"{_TARGET_ENV} must target a database ending in _test")
    return database


def release_claim(dsn: str) -> int:
    """Delete the recorded instance claim and return the number of rows removed."""
    import psycopg

    database = _verified_database(dsn)
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            row = cursor.execute("SELECT current_database()").fetchone()
            if row is None or row[0] != database:
                raise ValueError("the connected database identity is not the requested _test target")
            cursor.execute(f"DELETE FROM {_CLAIM_TABLE}")
            removed = cursor.rowcount
        connection.commit()
    return removed


def main() -> int:
    dsn = os.environ.get(_TARGET_ENV, "").strip()
    if not dsn:
        return _fail(f"missing required environment setting {_TARGET_ENV}")

    try:
        import psycopg  # noqa: F401
    except ImportError:
        return _fail("PostgreSQL client support is unavailable in the current uv environment")

    try:
        removed = release_claim(dsn)
    except ValueError as error:
        return _fail(str(error))
    except Exception:
        return _fail("could not release the demo instance claim")

    print(f"demo acceptance: released the instance claim ({removed} row(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
