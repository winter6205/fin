"""Contract tests for the T1 PostgreSQL connection-pool API."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fin.host.db_pool import (
    MAX_POOL_SIZE,
    PostgresPool,
    PostgresPoolAcquireError,
    PostgresPoolClosedError,
    PostgresPoolConfigError,
)


def _configured_postgres_url() -> str:
    url = os.environ.get("FIN_CHECKPOINT_URL", "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; live PostgreSQL pool tests are "
            "untested in this environment"
        )
    return url


def test_empty_connection_url_is_rejected() -> None:
    """An empty DSN must fail as configuration, not become a local fallback."""
    with pytest.raises(PostgresPoolConfigError, match="connection URL"):
        PostgresPool("")


def test_negative_pool_size_is_rejected() -> None:
    """Negative capacity cannot be passed to the driver."""
    with pytest.raises(PostgresPoolConfigError, match="min_size"):
        PostgresPool("postgresql://placeholder", min_size=-1)


def test_overflow_pool_size_is_rejected() -> None:
    """An unbounded capacity request must fail before pool creation."""
    with pytest.raises(PostgresPoolConfigError, match="max_size"):
        PostgresPool(
            "postgresql://placeholder",
            min_size=1,
            max_size=MAX_POOL_SIZE + 1,
        )


def test_closed_pool_rejects_acquisition() -> None:
    """Acquisition before opening or after closing has a typed lifecycle error."""
    pool = PostgresPool("postgresql://placeholder")

    with pytest.raises(PostgresPoolClosedError):
        with pool.connection():
            pass


def test_pool_supports_concurrent_connection_contexts() -> None:
    """Several workers can acquire and release connections concurrently."""
    url = _configured_postgres_url()
    pool = PostgresPool(url, min_size=2, max_size=4, timeout=5.0)
    barrier = Barrier(4)

    def query_one() -> int:
        barrier.wait(timeout=5)
        with pool.connection() as connection:
            return connection.execute("SELECT 1").fetchone()[0]

    with pool:
        pool.wait(timeout=5)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _item: query_one(), range(4)))

    assert results == [1, 1, 1, 1]


def test_pool_exhaustion_raises_typed_acquire_error() -> None:
    """A saturated pool must report a typed timeout instead of hanging."""
    url = _configured_postgres_url()
    pool = PostgresPool(url, min_size=1, max_size=1, timeout=0.1)

    with pool:
        pool.wait(timeout=5)
        held = pool.connection()
        connection = held.__enter__()
        try:
            with pytest.raises(PostgresPoolAcquireError):
                with pool.connection(timeout=0.01):
                    pass
        finally:
            held.__exit__(None, None, None)
            assert connection is not None
