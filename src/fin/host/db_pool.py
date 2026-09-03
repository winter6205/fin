"""Explicit PostgreSQL connection-pool lifecycle for host integrations.

The pool is intentionally not wired into the existing checkpointer or
thread-store paths in T1. Later host wiring can construct one pool, keep it
open for the process lifetime, and pass ``connection`` contexts to its
PostgreSQL-backed components.
"""

from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout


DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 10
DEFAULT_TIMEOUT = 30.0
MAX_POOL_SIZE = 1_000


class PostgresPoolError(Exception):
    """Base class for typed pool configuration and lifecycle failures."""


class PostgresPoolConfigError(PostgresPoolError):
    """The pool configuration cannot be safely passed to psycopg."""


class PostgresPoolClosedError(PostgresPoolError):
    """A connection was requested from a pool that is not open."""


class PostgresPoolAcquireError(PostgresPoolError):
    """The pool could not provide a connection before the timeout."""


class PostgresPoolOpenError(PostgresPoolError):
    """The pool could not establish its initial connections."""


def _validate_configuration(
    connection_url: str,
    min_size: int,
    max_size: int,
    timeout: float,
) -> str:
    """Validate caller input before constructing the third-party pool."""
    if not isinstance(connection_url, str) or not connection_url.strip():
        raise PostgresPoolConfigError("connection URL must be non-empty")
    if isinstance(min_size, bool) or min_size < 0:
        raise PostgresPoolConfigError("min_size must be a non-negative integer")
    if isinstance(max_size, bool) or max_size < 1:
        raise PostgresPoolConfigError("max_size must be a positive integer")
    if min_size > max_size:
        raise PostgresPoolConfigError("min_size cannot exceed max_size")
    if max_size > MAX_POOL_SIZE:
        raise PostgresPoolConfigError(
            f"max_size cannot exceed {MAX_POOL_SIZE}"
        )
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise PostgresPoolConfigError("timeout must be a finite positive number")
    return connection_url.strip()


class PostgresPool:
    """A typed, explicitly managed wrapper around ``psycopg_pool``.

    ``open()`` starts the worker and connection machinery; ``close()`` stops
    it. The wrapper starts closed so a caller cannot accidentally create
    background connection work merely by constructing an object.
    """

    def __init__(
        self,
        connection_url: str,
        *,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        url = _validate_configuration(connection_url, min_size, max_size, timeout)
        self._pool = ConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
        )
        self._timeout = timeout
        self._state_lock = threading.Lock()
        self._opened = False
        self._closed = False

    @property
    def is_open(self) -> bool:
        """Return whether the pool is currently available for acquisition."""
        with self._state_lock:
            return self._opened and not self._closed

    def open(self, *, wait: bool = False, timeout: float | None = None) -> None:
        """Open the pool, optionally waiting for its minimum capacity.

        ``wait=True`` is useful at service startup when a database outage
        should stop boot rather than surface on the first request.
        """
        wait_timeout = self._timeout if timeout is None else timeout
        if isinstance(wait_timeout, bool) or not math.isfinite(wait_timeout):
            raise PostgresPoolConfigError(
                "open timeout must be a finite number"
            )
        if wait_timeout <= 0:
            raise PostgresPoolConfigError("open timeout must be positive")

        with self._state_lock:
            if self._closed:
                raise PostgresPoolClosedError("PostgreSQL pool is closed")
            if self._opened:
                return
            try:
                self._pool.open()
            except (PoolClosed, psycopg.Error) as exc:
                raise PostgresPoolOpenError(
                    "could not open the PostgreSQL connection pool"
                ) from exc
            self._opened = True

        if wait:
            try:
                self._pool.wait(timeout=wait_timeout)
            except (PoolTimeout, PoolClosed, psycopg.Error) as exc:
                self.close()
                raise PostgresPoolOpenError(
                    "could not establish PostgreSQL pool connections"
                ) from exc

    def close(self) -> None:
        """Close the pool and make subsequent acquisition fail loudly."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._opened = False
            self._pool.close()

    def wait(self, *, timeout: float | None = None) -> None:
        """Wait until the configured minimum number of connections is ready."""
        if not self.is_open:
            raise PostgresPoolClosedError("PostgreSQL pool is not open")
        wait_timeout = self._timeout if timeout is None else timeout
        if isinstance(wait_timeout, bool) or not math.isfinite(wait_timeout):
            raise PostgresPoolConfigError(
                "wait timeout must be a finite number"
            )
        if wait_timeout <= 0:
            raise PostgresPoolConfigError("wait timeout must be positive")
        try:
            self._pool.wait(timeout=wait_timeout)
        except (PoolTimeout, PoolClosed, psycopg.Error) as exc:
            raise PostgresPoolOpenError(
                "could not establish PostgreSQL pool connections"
            ) from exc

    @contextmanager
    def connection(self, *, timeout: float | None = None) -> Iterator[psycopg.Connection]:
        """Borrow one connection and always return it to the pool."""
        if not self.is_open:
            raise PostgresPoolClosedError("PostgreSQL pool is not open")
        acquire_timeout = self._timeout if timeout is None else timeout
        if isinstance(acquire_timeout, bool) or not math.isfinite(acquire_timeout):
            raise PostgresPoolConfigError(
                "connection timeout must be a finite number"
            )
        if acquire_timeout <= 0:
            raise PostgresPoolConfigError("connection timeout must be positive")

        try:
            connection = self._pool.getconn(timeout=acquire_timeout)
        except PoolClosed as exc:
            raise PostgresPoolClosedError(
                "PostgreSQL pool closed during acquisition"
            ) from exc
        except PoolTimeout as exc:
            raise PostgresPoolAcquireError(
                "timed out acquiring a PostgreSQL connection"
            ) from exc
        except psycopg.Error as exc:
            raise PostgresPoolAcquireError(
                "could not acquire a PostgreSQL connection"
            ) from exc

        try:
            yield connection
        finally:
            self._pool.putconn(connection)

    def __enter__(self) -> PostgresPool:
        self.open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def create_postgres_pool(
    connection_url: str,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
) -> PostgresPool:
    """Construct a closed pool for an explicit lifecycle owner."""
    return PostgresPool(
        connection_url,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
    )


__all__ = [
    "DEFAULT_MAX_SIZE",
    "DEFAULT_MIN_SIZE",
    "DEFAULT_TIMEOUT",
    "MAX_POOL_SIZE",
    "PostgresPool",
    "PostgresPoolAcquireError",
    "PostgresPoolClosedError",
    "PostgresPoolConfigError",
    "PostgresPoolError",
    "PostgresPoolOpenError",
    "create_postgres_pool",
]
