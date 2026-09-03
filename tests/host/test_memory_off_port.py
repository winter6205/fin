"""T3 contracts for the default-off memory port."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from fin.host.memory_port import (
    DEFAULT_MEMORY_LIMIT,
    MEMORY_LIMIT_CAP,
    MemoryBackendUnavailableError,
    MemoryConfigError,
    MemoryPort,
    OffMemoryAdapter,
    build_memory_port,
)


def test_default_memory_port_is_off_and_structurally_implements_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIN_MEMORY_ENABLED", raising=False)

    port = build_memory_port()

    assert isinstance(port, MemoryPort)
    assert isinstance(port, OffMemoryAdapter)
    assert port.enabled is False


def test_off_memory_returns_empty_structured_results_for_empty_inputs() -> None:
    port = OffMemoryAdapter()

    assert port.extract("") == {"status": "disabled", "memories": []}
    assert port.recall("   ") == {"status": "disabled", "memories": []}
    assert port.gc() == {"status": "disabled", "removed": 0}
    assert port.dream() == {"status": "disabled", "transported": 0}


def test_off_memory_never_uses_invalid_or_overflow_limits() -> None:
    port = OffMemoryAdapter()

    assert port.recall("anything", limit=-1) == {
        "status": "disabled",
        "memories": [],
    }
    assert port.recall("anything", limit=MEMORY_LIMIT_CAP + 1) == {
        "status": "disabled",
        "memories": [],
    }
    assert port.gc(limit=-1) == {"status": "disabled", "removed": 0}
    assert port.dream(limit=MEMORY_LIMIT_CAP + 1) == {
        "status": "disabled",
        "transported": 0,
    }
    assert DEFAULT_MEMORY_LIMIT > 0


def test_off_memory_is_stateless_under_concurrent_calls() -> None:
    port = OffMemoryAdapter()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: port.recall("same query", limit=4),
                range(32),
            )
        )

    assert results == [{"status": "disabled", "memories": []}] * 32


def test_explicit_on_does_not_silently_fall_back_to_off() -> None:
    with pytest.raises(MemoryBackendUnavailableError):
        build_memory_port(env={"FIN_MEMORY_ENABLED": "true"})


def test_invalid_memory_enabled_value_is_a_typed_configuration_error() -> None:
    with pytest.raises(MemoryConfigError):
        build_memory_port(env={"FIN_MEMORY_ENABLED": "sometimes"})
