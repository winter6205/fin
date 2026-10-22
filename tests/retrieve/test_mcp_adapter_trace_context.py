"""The trace context crosses the ``asyncio.run`` retrieval boundary.

The spec is explicit about this and not incidental about how:
``specs/qa-chain-server-runtime.md`` §"Trace transport and spans" says
"Retrieval runs in a worker thread through ``asyncio.run`` in the MCP
adapter, so the trace context is passed in explicitly rather than
inherited." These tests pin the passing half — the ``retrieve`` and
``read_slice`` seams attach the caller's OpenTelemetry context around
the loop rather than depending on whether the loop happens to run on
the caller's thread.

They also pin the two things that could be damaged by making the
context explicit: cancellation still travels OUT of the transport
ladder instead of being flattened into a failure token, and the
caller's own context is left exactly as it was found.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from fin.retrieve import mcp_adapter
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_session import (
    MCPStdioCancelledError,
    MCPStdioDeadlineExceededError,
)


_REAL_ASYNCIO_RUN = asyncio.run
_DOCUMENT_ID = "DOC-2023Q1"
_VERSION = "v-" + "a" * 60
_SOURCE_REF = {"document_id": _DOCUMENT_ID, "version": _VERSION, "unit": 1}


class _TracingAdapter(MCPRetrieveAdapter):
    """Adapter that records the trace it is called under.

    The recording happens inside ``_call_mcp`` — that is, INSIDE the
    ``asyncio.run`` boundary — so the recorded span is what the loop
    actually saw, not what the caller happened to have attached.
    """

    def __init__(self, *, scripted_response: dict | None = None) -> None:
        self._command = None
        self._locator_map = {}
        self._known_doc_ids = frozenset({_DOCUMENT_ID})
        self._timeout = 5.0
        self._mapping_ledger = None
        self._input_validation_ledger = None
        self._scripted_response = (
            scripted_response
            if scripted_response is not None
            else {"result": {"hits": []}}
        )
        self.observed_spans: list[int] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        current = trace.get_current_span().get_span_context()
        self.observed_spans.append(current.span_id if current.is_valid else 0)
        return self._scripted_response


class _RaisingAdapter(_TracingAdapter):
    """Adapter whose transport raises, for the cancellation ladder."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        raise self._error


def _read_slice_wire(text: str) -> dict:
    return {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "source_ref": _SOURCE_REF,
                            "content": text,
                            "next_position": None,
                        }
                    ),
                }
            ]
        }
    }


@pytest.fixture(name="tracer")
def _tracer() -> Iterator[trace.Tracer]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("fin.test.retrieve.trace")
    provider.shutdown()


@pytest.fixture(name="worker_thread_loop", autouse=True)
def _worker_thread_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Make every ``asyncio.run`` in this process execute on a fresh thread.

    This is the premise the spec names — "retrieval runs in a worker
    thread through ``asyncio.run``". Reproducing it is what makes the
    tests below load-bearing: without it, ``asyncio.run`` happens to
    run on the caller's thread and ambient contextvars propagate for
    free, so a bare ``asyncio.run`` and an explicit attach are
    indistinguishable and the test would pass against the unfixed
    code.
    """

    def run_in_worker_thread(coro: Any) -> Any:
        box: dict[str, Any] = {}

        def runner() -> None:
            loop = asyncio.new_event_loop()
            try:
                box["value"] = loop.run_until_complete(coro)
            except BaseException as exc:  # noqa: BLE001 - relayed verbatim
                box["error"] = exc
            finally:
                loop.close()

        thread = threading.Thread(target=runner, name="fake-retrieval-worker")
        thread.start()
        thread.join()
        if "error" in box:
            raise box["error"]
        return box["value"]

    monkeypatch.setattr(asyncio, "run", run_in_worker_thread)
    yield


# ----- the context actually crosses -----


def test_retrieve_carries_the_caller_span_into_the_worker_loop(tracer) -> None:
    """``retrieve``'s transport runs under the caller's span.

    The span id is read inside ``_call_mcp``, so this asserts what the
    worker loop saw rather than what the caller attached.
    """
    adapter = _TracingAdapter()

    with tracer.start_as_current_span("retrieve_tool") as caller:
        caller_span_id = caller.get_span_context().span_id
        result = adapter.retrieve("quarterly revenue")

    # A successful result carries the spec's ``distribution`` key beside
    # the chunks (spec qa-chain-corpus-retrieval §"Result, error,
    # readiness, and degradation wire"): no usable chunk is ``zero``.
    assert result == {"chunks": [], "distribution": "zero"}
    assert adapter.observed_spans == [caller_span_id]
    assert caller_span_id != 0


def test_read_slice_carries_the_caller_span_into_the_worker_loop(tracer) -> None:
    """The read seam crosses the boundary the same way retrieve does."""
    adapter = _TracingAdapter(scripted_response=_read_slice_wire("slice body"))

    with tracer.start_as_current_span("retrieve_tool") as caller:
        caller_span_id = caller.get_span_context().span_id
        result = adapter.read_slice(_SOURCE_REF)

    assert result == {
        "source_ref": _SOURCE_REF,
        "text": "slice body",
        "next_position": None,
    }
    assert adapter.observed_spans == [caller_span_id]
    assert caller_span_id != 0


def test_caller_context_is_the_only_one_that_crosses(tracer) -> None:
    """The span crosses; the caller's other contextvars do not.

    ``contextvars.copy_context()`` would restore the whole caller
    stack, which would hand this module's transport a foreign run's
    ``RunCallContext``. The sentinel below stands in for that: a
    contextvar that is NOT the OTel context must not ride along.
    """
    import contextvars

    sentinel: contextvars.ContextVar[str] = contextvars.ContextVar(
        "fin.test.unrelated", default="unset"
    )
    observed: list[str] = []

    class _SentinelAdapter(_TracingAdapter):
        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            observed.append(sentinel.get())
            return {"result": {"hits": []}}

    adapter = _SentinelAdapter()
    token = sentinel.set("caller-private")
    try:
        with tracer.start_as_current_span("retrieve_tool"):
            adapter.retrieve("quarterly revenue")
    finally:
        sentinel.reset(token)

    assert observed == ["unset"]


# ----- the tests above would fail without the explicit attach -----


def test_a_bare_asyncio_run_loses_the_context_on_a_worker_thread(
    tracer,
) -> None:
    """The negative control the passing tests depend on.

    Without this, "the context crossed" could be an artifact of the
    loop running on the caller's thread. It is not: the same worker
    thread drops a bare ``asyncio.run`` and keeps the attached one.
    """
    seen: list[object] = []

    async def observe() -> None:
        seen.append(otel_context.get_value("fin.test.sentinel"))

    async def observe_attached(captured: Any) -> None:
        token = otel_context.attach(captured)
        try:
            seen.append(otel_context.get_value("fin.test.sentinel"))
        finally:
            otel_context.detach(token)

    with tracer.start_as_current_span("retrieve_tool"):
        token = otel_context.attach(otel_context.set_value("fin.test.sentinel", "carried"))
        try:
            captured = otel_context.get_current()
            asyncio.run(observe())
            asyncio.run(observe_attached(captured))
        finally:
            otel_context.detach(token)

    assert seen == [None, "carried"]


def test_an_empty_caller_context_still_answers(tracer) -> None:
    """No active context is a normal case, not an error.

    Attaching an empty ``Context`` would be a no-op anyway; this pins
    that the fast path runs and returns the real envelope rather than
    guarding the empty context with a raise.
    """
    adapter = _TracingAdapter()

    assert not otel_context.get_current()
    assert adapter.retrieve("quarterly revenue") == {
        "chunks": [],
        "distribution": "zero",
    }
    # No caller span, so nothing valid is observed — and nothing crashes.
    assert adapter.observed_spans == [0]


# ----- the caller's own context is left as found -----


def test_the_callers_context_is_not_mutated_by_a_call(tracer) -> None:
    """The attach is scoped to the loop, not left dangling afterwards.

    ``asyncio.run`` already runs the coroutine in a copied context, so
    the caller's own context is untouched. That is worth pinning: a
    leaked attach here would make every later operation in the turn
    report itself as part of this retrieve.
    """
    adapter = _TracingAdapter()
    before = otel_context.get_current()

    with tracer.start_as_current_span("retrieve_tool"):
        inside = otel_context.get_current()
        adapter.retrieve("quarterly revenue")
        assert otel_context.get_current() == inside

    assert otel_context.get_current() == before
    assert not trace.get_current_span().get_span_context().is_valid


# ----- cancellation still travels out of the transport ladder -----


@pytest.mark.parametrize(
    "error",
    [
        MCPStdioCancelledError("cancelled"),
        MCPStdioDeadlineExceededError("late", stage="response"),
    ],
)
def test_read_slice_re_raises_cancellation_under_an_attached_context(
    tracer, error: BaseException
) -> None:
    """A trace context must not change what cancellation means.

    Both errors derive from ``OSError``, so the ``except OSError``
    ladder beside them would swallow one into
    ``source_unavailable``. The context crossing must not disturb
    that ordering.
    """
    adapter = _RaisingAdapter(error)

    with tracer.start_as_current_span("retrieve_tool"):
        with pytest.raises(type(error)):
            adapter.read_slice(_SOURCE_REF)


def test_retrieve_re_raises_cancellation_under_an_attached_context(tracer) -> None:
    """Same ladder question on the retrieve seam's async transport."""
    adapter = _RaisingAdapter(MCPStdioCancelledError("cancelled"))

    with tracer.start_as_current_span("retrieve_tool"):
        with pytest.raises(MCPStdioCancelledError):
            adapter.retrieve("quarterly revenue")


def test_an_ordinary_transport_failure_is_still_a_named_exit(tracer) -> None:
    """The re-raise is narrow: a plain ``OSError`` is still translated.

    Pinning the other side of the ladder keeps the cancellation
    re-raise from quietly widening into "never translate".
    """
    adapter = _RaisingAdapter(OSError("transport down"))

    with tracer.start_as_current_span("retrieve_tool"):
        assert adapter.retrieve("quarterly revenue") == {
            "error": "retrieve_unavailable"
        }
        assert adapter.read_slice(_SOURCE_REF) == {
            "error": "source_unavailable",
        }


# ----- the caller's shape is untouched -----


def test_run_control_binding_still_reaches_the_worker_loop() -> None:
    """``with_run_control`` survives the new runner.

    The runner changed which call carries the deadline; the deadline
    still has to arrive, or cancellation quietly stops working.
    """
    deadline_marker = 1_234.5
    seen: list[Any] = []

    class _RecordingSession:
        def call(
            self,
            payload: dict,
            timeout_seconds: float | None = None,
            *,
            deadline: float | None = None,
            cancel_event: threading.Event | None = None,
        ) -> dict:
            seen.append((deadline, cancel_event))
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"chunks": []}}

        def close(self) -> None:
            return None

    cancel_event = threading.Event()
    adapter = MCPRetrieveAdapter(session=_RecordingSession()).with_run_control(  # type: ignore[arg-type]
        deadline=deadline_marker, cancel_event=cancel_event
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(adapter.retrieve, "quarterly revenue")
        assert future.result(timeout=2) == {
            "chunks": [],
            "distribution": "zero",
        }

    assert seen == [(deadline_marker, cancel_event)]


def test_the_helper_is_used_at_both_asyncio_run_sites() -> None:
    """Neither ``asyncio.run`` call site is left bare.

    A future call site added without the runner would pass every test
    above (they exercise the two seams) while silently dropping the
    trace. Counting the executable sites is the cheap standing guard.
    """
    import ast
    import inspect
    import textwrap

    assert mcp_adapter.__file__ is not None
    with open(mcp_adapter.__file__, encoding="utf-8") as handle:
        tree = ast.parse(textwrap.dedent(handle.read()))

    direct_runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
    ]
    # Both live inside the runner helper: the empty-context fast path
    # and the attached path. No other site calls ``asyncio.run``
    # directly.
    assert len(direct_runs) == 2

    runner_call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_with_trace_context"
    ]
    assert len(runner_call_sites) == 2
    assert inspect.getsource(mcp_adapter._run_with_trace_context)
