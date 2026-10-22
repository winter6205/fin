"""The parser context's one way to reach PostgreSQL.

Every durable read and write of the parser service goes through
:func:`parser_connection`: it opens the connection, owns the transaction,
translates a driver failure into the typed ``ParserPersistenceError`` the
taxonomy names, and closes the connection on a best-effort basis. The
context therefore has ONE way to fail against PostgreSQL, and no call
site repeats that translation.

A ``read_only`` connection sets its transaction read-only as the first
statement, so a read path that claims to be read-only cannot write.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import psycopg

from fin.parser.errors import ParserPersistenceError

__all__ = ["parser_connection"]


@contextmanager
def parser_connection(
    dsn: str,
    *,
    message: str,
    read_only: bool = False,
) -> Iterator[psycopg.Connection[Any]]:
    """One parser connection, with the driver's failures made typed.

    The transaction commits when the body ends and rolls back when the
    body raises, so a refused operation writes nothing. ``message`` is the
    failure text of the typed error. The close is best-effort: a driver
    error on close does not change an outcome that was already decided.
    """
    try:
        conn = psycopg.connect(dsn)
    except psycopg.Error as exc:
        raise ParserPersistenceError(message) from exc
    try:
        with conn:
            if read_only:
                # The claim is enforced, not merely documented: the rest of
                # the transaction refuses every write.
                conn.execute("SET TRANSACTION READ ONLY")
            yield conn
    except psycopg.Error as exc:
        raise ParserPersistenceError(message) from exc
    finally:
        with suppress(psycopg.Error):
            # EXIT: the close is best-effort — a driver error on close does
            # not change an outcome that was already decided.
            conn.close()
