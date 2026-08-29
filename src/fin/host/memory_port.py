"""Switchable memory seam and the default OFF adapter.

Memory is a host capability separate from filing retrieve.  This module
only defines the transport-neutral port and its safe default; local-buffer
and MCP-backed implementations belong to later memory tickets.

The default is deliberately OFF.  An explicit ON request fails loudly
until an enabled backend is supplied, rather than silently changing the
operator's requested mode back to OFF.
"""

from __future__ import annotations

import os
from typing import Mapping, Protocol, runtime_checkable


MEMORY_ENABLED_ENV_VAR = "FIN_MEMORY_ENABLED"
DEFAULT_MEMORY_LIMIT = 8
MEMORY_LIMIT_CAP = 20

_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class MemoryPortError(Exception):
    """Base class for typed memory-port failures."""


class MemoryConfigError(MemoryPortError):
    """The memory enablement setting is not a supported boolean."""


class MemoryBackendUnavailableError(MemoryPortError):
    """Memory was explicitly enabled but no backend is wired yet."""


@runtime_checkable
class MemoryPort(Protocol):
    """Port for extract, recall, garbage collection, and dream transport.

    Implementations return structured dictionaries instead of ``None``.
    Memory results are advisory data and have no citation/evidence field;
    filing citations remain owned by the separate retrieve/read path.
    """

    @property
    def enabled(self) -> bool:
        """Whether memory operations are active for the host."""
        ...

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Extract candidate memories from dialogue text."""
        ...

    def recall(
        self,
        query: str,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
    ) -> dict[str, object]:
        """Return advisory memories relevant to ``query``."""
        ...

    def gc(self, *, limit: int = DEFAULT_MEMORY_LIMIT) -> dict[str, object]:
        """Soft-disable expired or superseded memories."""
        ...

    def dream(self, *, limit: int = DEFAULT_MEMORY_LIMIT) -> dict[str, object]:
        """Consolidate and transport useful memories to a backend."""
        ...


class OffMemoryAdapter:
    """No-op memory adapter used by the default product configuration.

    The adapter is intentionally stateless and does not create tools,
    call a backend, or transport dream entries.  Structured disabled
    results keep direct callers typed without pretending that memory
    recall was attempted.
    """

    enabled = False

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        del text, metadata
        return {"status": "disabled", "memories": []}

    def recall(
        self,
        query: str,
        *,
        limit: int = DEFAULT_MEMORY_LIMIT,
    ) -> dict[str, object]:
        del query
        _resolve_limit(limit)
        return {"status": "disabled", "memories": []}

    def gc(self, *, limit: int = DEFAULT_MEMORY_LIMIT) -> dict[str, object]:
        _resolve_limit(limit)
        return {"status": "disabled", "removed": 0}

    def dream(self, *, limit: int = DEFAULT_MEMORY_LIMIT) -> dict[str, object]:
        _resolve_limit(limit)
        return {"status": "disabled", "transported": 0}


def _resolve_limit(limit: int) -> int:
    """Keep the OFF adapter compatible with the future bounded port."""
    if limit <= 0:
        return DEFAULT_MEMORY_LIMIT
    return min(limit, MEMORY_LIMIT_CAP)


def _read_enabled(env: Mapping[str, str]) -> bool:
    raw = env.get(MEMORY_ENABLED_ENV_VAR)
    if raw is None:
        return False

    value = raw.strip().lower()
    if value in _FALSE_VALUES:
        return False
    if value in _TRUE_VALUES:
        return True
    raise MemoryConfigError(
        f"{MEMORY_ENABLED_ENV_VAR} must be one of "
        "true/false, yes/no, on/off, or 1/0"
    )


def build_memory_port(
    *, env: Mapping[str, str] | None = None
) -> MemoryPort:
    """Build the configured memory port, defaulting to OFF.

    The ON implementation is intentionally deferred to the local-buffer
    ticket.  Raising here prevents an explicit ON request from becoming
    an unobservable OFF deployment.
    """
    source = os.environ if env is None else env
    if not _read_enabled(source):
        return OffMemoryAdapter()
    raise MemoryBackendUnavailableError(
        "memory is enabled, but no memory backend is wired"
    )


# Friendly names for callers that describe this seam as an env-backed port.
OffMemory = OffMemoryAdapter
memory_port_from_env = build_memory_port


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "MEMORY_ENABLED_ENV_VAR",
    "MEMORY_LIMIT_CAP",
    "MemoryBackendUnavailableError",
    "MemoryConfigError",
    "MemoryPort",
    "MemoryPortError",
    "OffMemory",
    "OffMemoryAdapter",
    "build_memory_port",
    "memory_port_from_env",
]
