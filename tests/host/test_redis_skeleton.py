"""Contract tests for the optional Redis runtime plane.

Redis is a runtime optimization only.  These tests deliberately use a
small in-process transport fake, so the default suite never imports a
Redis package or needs a live service.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import socket

import pytest

from fin.host.redis_client import (
    REDIS_AVAILABLE,
    REDIS_DEGRADED,
    REDIS_DISABLED,
    RedisClient,
    RedisConfigurationError,
    RedisUnavailableError,
)


class _WorkingTransport:
    def ping(self) -> None:
        return None


class _DownTransport:
    def ping(self) -> None:
        raise OSError("connection refused")


def _closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_unset_redis_url_disables_client_and_allows_single_process_work() -> None:
    client = RedisClient.from_environment({})

    health = client.health()

    assert health.state == REDIS_DISABLED
    assert health.reason == "redis_url_unset"
    assert client.allow("thread-1") is True


def test_configured_client_reports_available_with_in_process_transport() -> None:
    client = RedisClient.from_url(
        "redis://localhost:6379/0",
        transport=_WorkingTransport(),
    )

    health = client.health()

    assert health.state == REDIS_AVAILABLE
    assert health.reason == ""
    assert client.require_available() is None


def test_negative_timeout_is_rejected_as_configuration_error() -> None:
    with pytest.raises(RedisConfigurationError):
        RedisClient.from_url("redis://localhost:6379/0", timeout_seconds=-1)


def test_overflow_port_is_rejected_as_configuration_error() -> None:
    with pytest.raises(RedisConfigurationError):
        RedisClient.from_url("redis://localhost:65536/0")


def test_disabled_client_is_safe_for_concurrent_single_process_calls() -> None:
    client = RedisClient.from_environment({"FIN_REDIS_URL": "   "})

    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions = list(executor.map(client.allow, range(64)))

    assert decisions == [True] * 64
    assert client.health().state == REDIS_DISABLED


def test_down_transport_degrades_and_required_access_raises_typed_error() -> None:
    client = RedisClient.from_url(
        "redis://localhost:6379/0",
        transport=_DownTransport(),
    )

    health = client.health()

    assert health.state == REDIS_DEGRADED
    assert health.reason == "redis_unreachable"
    with pytest.raises(RedisUnavailableError):
        client.require_available()


def test_down_socket_url_is_typed_degraded_without_blank_failure() -> None:
    client = RedisClient.from_url(
        f"redis://127.0.0.1:{_closed_local_port()}/0",
        timeout_seconds=0.1,
    )

    health = client.health()

    assert health.state == REDIS_DEGRADED
    assert health.detail.strip()
