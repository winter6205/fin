"""Optional Redis runtime plane for rate limiting and queue fan-out.

Redis is deliberately not imported by this module.  Postgres remains the
authority store for checkpoints, turns, memory bodies, and citations; this
client is only an optional runtime optimization (ADR 0036).

The intended host API is small:

* :meth:`RedisClient.from_environment` reads ``FIN_REDIS_URL``.
* :meth:`RedisClient.health` returns ``disabled``, ``available``, or
  ``degraded`` without exposing the configured URL.
* :meth:`RedisClient.allow` is the future rate-limit gate.  The skeleton
  allows work when Redis is absent or down, which is the documented
  single-process fallback.
* :meth:`RedisClient.publish` is the future Pub/Sub seam and is a no-op
  until a Redis package is deliberately added.
* :meth:`RedisClient.require_available` is for an explicitly Redis-required
  path and raises :class:`RedisUnavailableError`; product serving must not
  use this as a boot gate.

The stdlib socket probe makes a configured down endpoint observable in unit
tests and in diagnostics.  It does not implement Redis commands or make
Redis an accidental dependency of the product.
"""

from __future__ import annotations

import os
import socket
import math
from dataclasses import dataclass
from typing import Final, Literal, Mapping, Protocol
from urllib.parse import urlsplit

REDIS_DISABLED: Final[str] = "disabled"
REDIS_AVAILABLE: Final[str] = "available"
REDIS_DEGRADED: Final[str] = "degraded"

REASON_REDIS_URL_UNSET: Final[str] = "redis_url_unset"
REASON_REDIS_UNREACHABLE: Final[str] = "redis_unreachable"

DEFAULT_REDIS_PORT: Final[int] = 6379
DEFAULT_TIMEOUT_SECONDS: Final[float] = 1.0
MAX_TIMEOUT_SECONDS: Final[float] = 30.0

RedisState = Literal["disabled", "available", "degraded"]


class RedisConfigurationError(ValueError):
    """The optional Redis configuration cannot be used."""


class RedisUnavailableError(RuntimeError):
    """A caller explicitly required Redis, but it is not available."""

    def __init__(self, health: RedisHealth) -> None:
        self.health = health
        super().__init__(f"Redis is unavailable ({health.reason})")


class RedisTransport(Protocol):
    """Minimal transport seam; a Redis package can implement this later."""

    def ping(self) -> None:
        """Raise when the configured Redis endpoint cannot answer."""


@dataclass(frozen=True)
class RedisHealth:
    """A URL-safe health snapshot for the optional runtime plane."""

    state: RedisState
    reason: str

    @property
    def detail(self) -> str:
        """Return operator copy that never includes URL or driver text."""
        if self.state == REDIS_DISABLED:
            return (
                "FIN_REDIS_URL is not set; Redis features are disabled and "
                "the process uses its single-process policy."
            )
        if self.state == REDIS_DEGRADED:
            return (
                "FIN_REDIS_URL is set but Redis did not answer; the process "
                "continues with its single-process policy."
            )
        return "Redis answered the optional runtime-plane probe."


class _SocketTransport:
    """Stdlib-only PING transport used when a URL is configured."""

    def __init__(self, host: str, port: int, timeout_seconds: float) -> None:
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds

    def ping(self) -> None:
        with socket.create_connection(
            (self._host, self._port), timeout=self._timeout_seconds
        ) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(128)
        if not response.startswith((b"+PONG", b"$4\r\nPONG")):
            raise OSError("Redis did not return PONG")


def _validate_timeout(timeout_seconds: float) -> float:
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise RedisConfigurationError("Redis timeout must be a finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise RedisConfigurationError(
            f"Redis timeout must be between 0 and {MAX_TIMEOUT_SECONDS} seconds"
        )
    return timeout


def _transport_for_url(url: str, timeout_seconds: float) -> RedisTransport:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "redis" or not parsed.hostname:
            raise ValueError
        port = parsed.port or DEFAULT_REDIS_PORT
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise RedisConfigurationError(
            "FIN_REDIS_URL must be a redis:// URL with a valid host and port"
        ) from None
    return _SocketTransport(parsed.hostname, port, timeout_seconds)


class RedisClient:
    """Optional Redis client with a typed, non-blocking degraded policy."""

    def __init__(
        self,
        *,
        url: str | None,
        transport: RedisTransport | None,
        timeout_seconds: float,
    ) -> None:
        self._url = url
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        transport: RedisTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> RedisClient:
        """Build a client from ``FIN_REDIS_URL`` without requiring Redis."""
        source = os.environ if environ is None else environ
        return cls.from_url(
            source.get("FIN_REDIS_URL"),
            transport=transport,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def from_url(
        cls,
        url: str | None,
        *,
        transport: RedisTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> RedisClient:
        """Build a disabled client for blank URLs or a configured client."""
        timeout = _validate_timeout(timeout_seconds)
        normalized_url = (url or "").strip()
        if not normalized_url:
            return cls(url=None, transport=None, timeout_seconds=timeout)
        configured_transport = transport or _transport_for_url(
            normalized_url, timeout
        )
        return cls(
            url=normalized_url,
            transport=configured_transport,
            timeout_seconds=timeout,
        )

    @property
    def configured(self) -> bool:
        """Whether an operator supplied ``FIN_REDIS_URL``."""
        return self._url is not None

    def health(self) -> RedisHealth:
        """Probe configured Redis and convert failures to typed degradation."""
        if self._transport is None:
            return RedisHealth(REDIS_DISABLED, REASON_REDIS_URL_UNSET)
        try:
            self._transport.ping()
        except Exception:
            return RedisHealth(REDIS_DEGRADED, REASON_REDIS_UNREACHABLE)
        return RedisHealth(REDIS_AVAILABLE, "")

    def require_available(self) -> None:
        """Raise a typed error for callers that explicitly require Redis."""
        health = self.health()
        if health.state != REDIS_AVAILABLE:
            raise RedisUnavailableError(health)

    def allow(self, key: str) -> bool:
        """Allow work under the skeleton's single-process fallback policy.

        ``key`` is reserved for the future rate-limit implementation.  The
        current skeleton intentionally does not reject or queue work based on
        Redis health, so Redis cannot block product serving.
        """
        del key
        return True

    def publish(self, channel: str, payload: str) -> None:
        """Future Pub/Sub seam; currently succeeds as an explicit no-op."""
        del channel, payload


__all__ = [
    "DEFAULT_REDIS_PORT",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "REASON_REDIS_UNREACHABLE",
    "REASON_REDIS_URL_UNSET",
    "REDIS_AVAILABLE",
    "REDIS_DEGRADED",
    "REDIS_DISABLED",
    "RedisClient",
    "RedisConfigurationError",
    "RedisHealth",
    "RedisTransport",
    "RedisUnavailableError",
]
