"""T9 contracts for observability and empty-hit rewrite retry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from fin.host.observability import (
    MAX_OBSERVATION_SAMPLES,
    InvalidLatencyError,
    InvalidObservationStatusError,
    ObservationClockError,
    ObservabilityCollector,
)
from fin.host.rewrite_retry import (
    MAX_REWRITE_RETRIES,
    RewriteConfigurationError,
    RewriteRetryError,
    retrieve_with_rewrite_retry,
)


def test_observability_counts_terminal_statuses_and_latency() -> None:
    collector = ObservabilityCollector(clock=lambda: 0.0)

    collector.record("done", latency_ms=125.5)
    collector.record("refuse", latency_ms=40)

    assert collector.snapshot() == {
        "counts": {"done": 1, "refuse": 1},
        "latency_ms": {
            "count": 2,
            "total": 165.5,
            "average": 82.75,
        },
    }


def test_observability_measure_uses_injected_clock() -> None:
    ticks = iter((10.0, 10.25))
    collector = ObservabilityCollector(clock=lambda: next(ticks))

    with collector.measure("done"):
        pass

    assert collector.snapshot()["latency_ms"]["total"] == 250.0


def test_observability_rejects_negative_latency_and_unknown_status() -> None:
    collector = ObservabilityCollector()

    with pytest.raises(InvalidLatencyError):
        collector.record("done", latency_ms=-0.1)
    with pytest.raises(InvalidObservationStatusError):
        collector.record("mystery", latency_ms=1)


def test_observability_rejects_an_unbounded_sample_cap() -> None:
    with pytest.raises(ValueError):
        ObservabilityCollector(max_samples=MAX_OBSERVATION_SAMPLES + 1)


def test_observability_is_thread_safe_and_keeps_counts_bounded() -> None:
    collector = ObservabilityCollector(max_samples=4)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _: collector.record("done", latency_ms=1),
                range(32),
            )
        )

    snapshot = collector.snapshot()
    assert snapshot["counts"] == {"done": 32}
    assert snapshot["latency_ms"]["count"] == 32
    assert collector.sample_count == 4


def test_observability_wraps_clock_failures_as_typed_errors() -> None:
    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    collector = ObservabilityCollector(clock=broken_clock)

    with pytest.raises(ObservationClockError):
        with collector.measure("done"):
            pass


def test_empty_hit_is_rewritten_once_then_returns_the_hit() -> None:
    calls: list[str] = []

    def retrieve(query: str) -> dict[str, object]:
        calls.append(query)
        if len(calls) == 1:
            return {"chunks": []}
        return {"chunks": [{"text": "grounded chunk"}]}

    result = retrieve_with_rewrite_retry(
        "annual sales",
        retrieve=retrieve,
        rewrite=lambda query: f"{query} for AAPL",
        max_retries=1,
    )

    assert result.result == {"chunks": [{"text": "grounded chunk"}]}
    assert [attempt.query for attempt in result.attempts] == [
        "annual sales",
        "annual sales for AAPL",
    ]
    assert result.retry_count == 1
    assert result.exhausted is False


def test_empty_query_is_not_rewritten_into_a_fixed_pipeline() -> None:
    calls: list[str] = []

    result = retrieve_with_rewrite_retry(
        "   ",
        retrieve=lambda query: calls.append(query) or {"chunks": []},
        rewrite=lambda _: pytest.fail("empty query must not be rewritten"),
        max_retries=MAX_REWRITE_RETRIES,
    )

    assert calls == ["   "]
    assert result.result == {"chunks": []}
    assert result.retry_count == 0


def test_typed_retrieve_error_is_preserved_without_rewrite() -> None:
    calls: list[str] = []

    result = retrieve_with_rewrite_retry(
        "annual sales",
        retrieve=lambda query: calls.append(query)
        or {"error": "retrieve_unavailable"},
        rewrite=lambda _: pytest.fail("typed retrieve errors are not empty hits"),
        max_retries=1,
    )

    assert result.result == {"error": "retrieve_unavailable"}
    assert calls == ["annual sales"]


def test_memory_recall_shape_is_not_an_empty_retrieve_hit_or_evidence() -> None:
    result = retrieve_with_rewrite_retry(
        "what did I say?",
        retrieve=lambda _: {"memories": ["operator memory"]},
        rewrite=lambda _: pytest.fail("memory recall must not be rewritten"),
        max_retries=1,
    )

    assert result.result == {"memories": ["operator memory"]}
    assert not hasattr(result, "evidence")


def test_empty_hits_stop_after_retry_cap() -> None:
    result = retrieve_with_rewrite_retry(
        "annual sales",
        retrieve=lambda _: {"chunks": []},
        rewrite=lambda query: f"{query} revised",
        max_retries=2,
    )

    assert result.result == {"chunks": []}
    assert result.retry_count == 2
    assert result.exhausted is True
    assert [attempt.query for attempt in result.attempts] == [
        "annual sales",
        "annual sales revised",
        "annual sales revised revised",
    ]


def test_retry_configuration_rejects_negative_and_overflow_values() -> None:
    with pytest.raises(RewriteConfigurationError):
        retrieve_with_rewrite_retry(
            "query",
            retrieve=lambda _: {"chunks": []},
            rewrite=lambda query: query + " revised",
            max_retries=-1,
        )
    with pytest.raises(RewriteConfigurationError):
        retrieve_with_rewrite_retry(
            "query",
            retrieve=lambda _: {"chunks": []},
            rewrite=lambda query: query + " revised",
            max_retries=MAX_REWRITE_RETRIES + 1,
        )


def test_rewrite_exception_is_typed_and_not_swallowed() -> None:
    with pytest.raises(RewriteRetryError) as caught:
        retrieve_with_rewrite_retry(
            "query",
            retrieve=lambda _: {"chunks": []},
            rewrite=lambda _: (_ for _ in ()).throw(RuntimeError("bad rewrite")),
            max_retries=1,
        )

    assert caught.value.reason == "rewrite_failed"
