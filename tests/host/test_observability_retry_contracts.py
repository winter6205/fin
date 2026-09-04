"""T9 contracts for observability and empty-hit rewrite retry.

The first half pins the helper contracts (``ObservabilityCollector``,
``retrieve_with_rewrite_retry``). The second half pins the TOOL-level
wiring: the ``retrieve`` host tool must retry the port exactly once on
the exact empty-hit shape, never rewrite a hit or a typed error, and
record every performed rewrite on the process default collector.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.host.graph import _make_retrieve_tool
from fin.host.observability import (
    MAX_OBSERVATION_SAMPLES,
    InvalidLatencyError,
    InvalidObservationStatusError,
    ObservationClockError,
    ObservabilityCollector,
    default_collector,
)
from fin.host.rewrite_retry import (
    MAX_REWRITE_RETRIES,
    RewriteConfigurationError,
    RewriteRetryError,
    RewriteRetryResult,
    retrieve_with_rewrite_retry,
)
from fin.retrieve import Chunk
from fin.retrieve.input_validation import EmptyQueryError


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


# ----- retry counters on the collector (T9) -----


def test_collector_records_rewrite_retry_counters_without_touching_snapshot() -> None:
    collector = ObservabilityCollector()

    collector.record_rewrite_retry(exhausted=False)
    collector.record_rewrite_retry(exhausted=False)
    collector.record_rewrite_retry(exhausted=True)

    assert collector.retry_snapshot() == {"count": 3, "exhausted": 1}
    # The terminal-status snapshot contract stays exactly as pinned above:
    # retry counters ride a separate method so the two vocabularies
    # cannot drift into each other.
    assert collector.snapshot() == {
        "counts": {},
        "latency_ms": {"count": 0, "total": 0.0, "average": 0.0},
    }


def test_collector_rejects_non_boolean_exhausted_flag() -> None:
    collector = ObservabilityCollector()

    with pytest.raises(ValueError):
        collector.record_rewrite_retry(exhausted="yes")  # type: ignore[arg-type]


# ----- tool-level retry wiring (graph.py _make_retrieve_tool) -----


def _locator(page: int = 1) -> Locator:
    return Locator(
        document_id="DOC-2023Q1", version="v-" + "a" * 60, page=page
    )


def _hit(text: str = "grounded chunk") -> Chunk:
    return Chunk(text=text, source_ref=_locator(), score=1.0)


class _ScriptedRetryPort:
    """Retrieve port scripted per call, recording every query it sees."""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self._results = list(results or [])
        self.queries: list[str] = []

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(query)
        if self._results:
            return self._results.pop(0)
        return {"chunks": []}


def _invoke_retrieve(adapter: Any, **kwargs: Any) -> Any:
    tool = _make_retrieve_tool(adapter, **kwargs)
    return tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "annual sales"},
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


def test_tool_retries_empty_hit_once_and_returns_the_rewritten_hit() -> None:
    port = _ScriptedRetryPort(
        results=[{"chunks": []}, {"chunks": [_hit()]}]
    )
    rewrites: list[str] = []

    def rewriter(query: str) -> str:
        rewrites.append(query)
        return f"{query} for AAPL"

    command = _invoke_retrieve(
        port, rewriter=rewriter, mapping_ledger=_ledger(), degradation_ledger=_ledger()
    )

    # The non-empty result wins; the retry is visible in the port's
    # query log, not in a new envelope key.
    assert [item.locator for item in command.update["evidence"]] == [_locator()]
    envelope = json.loads(command.update["messages"][0].content)
    assert envelope["chunks"][0]["text"] == "grounded chunk"
    assert port.queries == ["annual sales", "annual sales for AAPL"]
    assert rewrites == ["annual sales"]


def test_tool_never_rewrites_a_non_empty_first_hit() -> None:
    port = _ScriptedRetryPort(results=[{"chunks": [_hit()]}])

    def rewriter(_query: str) -> str:
        pytest.fail("a non-empty first hit must not be rewritten")

    command = _invoke_retrieve(port, rewriter=rewriter, mapping_ledger=_ledger())

    assert command.update["evidence"] != []
    assert port.queries == ["annual sales"]


def test_tool_exhausts_on_always_empty_without_looping() -> None:
    port = _ScriptedRetryPort()
    observations: list[RewriteRetryResult] = []

    command = _invoke_retrieve(
        port,
        rewriter=lambda query: f"{query} revised",
        mapping_ledger=_ledger(),
        retry_observer=observations.append,
    )

    envelope = json.loads(command.update["messages"][0].content)
    assert envelope == {"chunks": []}
    # Exactly one rewrite: bounded, no infinite loop.
    assert port.queries == ["annual sales", "annual sales revised"]
    assert len(observations) == 1
    assert observations[0].retry_count == 1
    assert observations[0].exhausted is True


def test_tool_keeps_input_validation_error_passthrough_without_rewrite() -> None:
    class _EmptyQueryPort:
        def __init__(self) -> None:
            self.calls = 0

        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise EmptyQueryError()

    port = _EmptyQueryPort()

    def rewriter(_query: str) -> str:
        pytest.fail("an input-validation error is not an empty hit")

    command = _invoke_retrieve(port, rewriter=rewriter, mapping_ledger=_ledger())

    envelope = json.loads(command.update["messages"][0].content)
    assert envelope == {"error": "empty_query"}
    assert port.calls == 1


def test_tool_keeps_typed_error_dict_passthrough_without_rewrite() -> None:
    port = _ScriptedRetryPort(results=[{"error": "retrieve_unavailable"}])

    def rewriter(_query: str) -> str:
        pytest.fail("a typed retrieve error is not an empty hit")

    command = _invoke_retrieve(port, rewriter=rewriter, mapping_ledger=_ledger())

    envelope = json.loads(command.update["messages"][0].content)
    assert envelope == {"error": "retrieve_unavailable"}
    assert port.queries == ["annual sales"]


def test_tool_preserves_unexpected_port_exception_surface() -> None:
    class _BrokenPort:
        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("transport exploded")

    with pytest.raises(RuntimeError, match="transport exploded"):
        _invoke_retrieve(_BrokenPort(), rewriter=lambda q: q + " x", mapping_ledger=_ledger())


def test_tool_records_the_performed_rewrite_even_when_the_retry_call_fails() -> None:
    # Finding (T9 review): the rewrite HAPPENED (the port saw the
    # rewritten query) before the follow-up call exploded. The retry
    # counter must still move — the recording is not allowed to sit
    # after a raise that skips it.
    before = default_collector().retry_snapshot()

    class _EmptyThenBrokenPort:
        def __init__(self) -> None:
            self.calls = 0

        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return {"chunks": []}
            raise RuntimeError("retry transport exploded")

    with pytest.raises(RuntimeError, match="retry transport exploded"):
        _invoke_retrieve(
            _EmptyThenBrokenPort(),
            rewriter=lambda query: query + " x",
            mapping_ledger=_ledger(),
        )

    after = default_collector().retry_snapshot()
    assert after["count"] == before["count"] + 1
    # The rewritten attempt produced no hit, so the retry counts as
    # exhausted: it definitively did not turn the miss into a hit.
    assert after["exhausted"] == before["exhausted"] + 1


def test_tool_degrades_a_throwing_rewriter_to_the_empty_hit_envelope() -> None:
    # Finding (T9 review): a typed retry failure (here: the injected
    # rewriter raising) must NOT escape the tool as a run-level
    # refuse. The tool keeps its envelope contract and degrades to
    # exactly the shape it would have returned for ``{"chunks": []}``.
    before = default_collector().retry_snapshot()

    class _AlwaysEmptyPort:
        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            return {"chunks": []}

    def broken_rewriter(_query: str) -> str:
        raise RuntimeError("rewriter exploded")

    command = _invoke_retrieve(
        _AlwaysEmptyPort(),
        rewriter=broken_rewriter,
        mapping_ledger=_ledger(),
    )

    envelope = json.loads(command.update["messages"][0].content)
    assert envelope == {"chunks": []}
    assert command.update["evidence"] == []
    # The rewriter threw before any rewritten retrieve happened, so
    # no rewrite was PERFORMED and the retry counter stays put.
    after = default_collector().retry_snapshot()
    assert after["count"] == before["count"]


def test_tool_degrades_a_malformed_retry_result_and_still_counts_the_rewrite() -> None:
    # The degrade path and the retry counter meet here: the rewrite
    # WAS performed (port called twice), then the follow-up result
    # was malformed. Degrade to the empty-hit envelope AND count the
    # performed rewrite.
    before = default_collector().retry_snapshot()

    port = _ScriptedRetryPort(results=[{"chunks": []}, ["not-a-mapping"]])

    command = _invoke_retrieve(
        port,
        rewriter=lambda query: query + " x",
        mapping_ledger=_ledger(),
    )

    envelope = json.loads(command.update["messages"][0].content)
    assert envelope == {"chunks": []}
    assert port.queries == ["annual sales", "annual sales x"]
    after = default_collector().retry_snapshot()
    assert after["count"] == before["count"] + 1


def test_tool_records_performed_retries_on_the_process_default_collector() -> None:
    before = default_collector().retry_snapshot()

    port = _ScriptedRetryPort(
        results=[{"chunks": []}, {"chunks": [_hit()]}]
    )
    _invoke_retrieve(port, mapping_ledger=_ledger())

    after = default_collector().retry_snapshot()
    assert after["count"] == before["count"] + 1
    assert after["exhausted"] == before["exhausted"]


def _ledger() -> Any:
    from fin.retrieve.hit_mapping import MappingFailureLedger

    return MappingFailureLedger()
