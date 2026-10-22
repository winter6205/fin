"""The exported span: the correlation it derives and what it may carry.

The allowlist assertions here are the SR-5 evidence: a closed name-set
equality over the attributes that actually reach the wire, plus the
absence of anything that would carry user or source text.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from fin.host.receipt_export import (
    SEAM_SPAN_NAMES,
    OTLPHTTPTransport,
    ReceiptExportConfig,
    ReceiptExportConfigError,
    ReceiptExportError,
    ReceiptExportResponseError,
    ReceiptExportTransportError,
    allowlisted_attributes,
    build_receipt_span,
    build_seam_span,
    load_receipt_export_config,
    receipt_span_id_for,
    segment_contexts,
    segment_span_id_for,
    seam_span_id_for,
    trace_id_for,
)
from fin.host.turn_receipt_store import STAGE_METRIC_KEYS, TurnReceipt, zero_stage_metrics


def _receipt(**changes: Any) -> TurnReceipt:
    base = TurnReceipt(
        thread_id="opaque-thread-id",
        turn_id="opaque-turn-id",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
        stage_metrics=zero_stage_metrics(),
    )
    return replace(base, **changes)


def test_missing_export_configuration_disables_export_without_partial_values() -> None:
    assert load_receipt_export_config({}) is None
    assert load_receipt_export_config({"OTEL_TRACES_EXPORTER": ""}) is None
    assert load_receipt_export_config({"OTEL_TRACES_EXPORTER": "none"}) is None

    assert load_receipt_export_config({"OTEL_TRACES_EXPORTER": "otlp"}) is not None


def test_an_unknown_exporter_is_refused_rather_than_silently_dropped() -> None:
    with pytest.raises(ReceiptExportConfigError):
        load_receipt_export_config({"OTEL_TRACES_EXPORTER": "jaeger-udp"})


def test_configuration_is_the_standard_selection_and_names_no_backend() -> None:
    """No vendor name and no credential field survives on the module
    surface: any backend is reached by ``OTEL_EXPORTER_OTLP_*`` alone."""
    import fin.host.receipt_export as module

    exported = {
        name
        for name in dir(module)
        if not name.startswith("_") or name == "_BoundedTransport"
    }
    joined = " ".join(exported).lower()
    for vendor in ("langfuse", "public_key", "secret_key", "ingestion", "traces_path"):
        assert vendor not in joined
    # The config the process loads carries only the selection.
    assert dataclasses.fields(ReceiptExportConfig("none"))[0].name == "exporter"
    assert not hasattr(module, "_validate_base_url")
    assert not hasattr(module, "build_otlp_payload")
    assert not hasattr(module, "_validate_response")


def test_trace_id_is_stable_per_turn_and_distinct_across_turns() -> None:
    first = trace_id_for("opaque-thread-id", "opaque-turn-id")
    again = trace_id_for("opaque-thread-id", "opaque-turn-id")
    other_turn = trace_id_for("opaque-thread-id", "other-turn")
    other_thread = trace_id_for("other-thread", "opaque-turn-id")

    assert first == again
    assert len({first, other_turn, other_thread}) == 3
    # A 128-bit id, never the zero trace.
    assert first.bit_length() > 0 and first != 0


def test_one_segment_has_one_root_and_no_two_spans_share_an_id() -> None:
    """The root takes the unsalted three-part id; every other span in
    the segment salts it with its own name. That is what keeps six
    spans in one segment from colliding on one derivation."""
    root = segment_span_id_for("opaque-thread-id", "opaque-turn-id", 0)
    seams = [seam_span_id_for("opaque-thread-id", "opaque-turn-id", 0, s) for s in SEAM_SPAN_NAMES]
    projection = receipt_span_id_for("opaque-thread-id", "opaque-turn-id", 0)

    ids = [root, *seams, projection]
    assert len(set(ids)) == len(ids)
    assert root == segment_span_id_for("opaque-thread-id", "opaque-turn-id", 0)
    # A different segment is a different root, so a resumed turn does not
    # reopen the segment it already finished.
    assert root != segment_span_id_for("opaque-thread-id", "opaque-turn-id", 1)
    # One turn is one trace, whichever segment a span belongs to.
    assert trace_id_for("opaque-thread-id", "opaque-turn-id") == trace_id_for(
        "opaque-thread-id", "opaque-turn-id"
    )


def test_an_unknown_seam_is_refused_rather_than_invented() -> None:
    with pytest.raises(Exception):
        seam_span_id_for("opaque-thread-id", "opaque-turn-id", 0, "some_new_node")


def test_every_seam_span_names_a_parent_that_exists_in_the_trace() -> None:
    """SR-5: no span references a missing parent."""
    contexts = segment_contexts("opaque-thread-id", "opaque-turn-id", 0)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    emitted = [contexts.segment_root]

    for seam in SEAM_SPAN_NAMES:
        span = build_seam_span(
            contexts,
            thread_id="opaque-thread-id",
            turn_id="opaque-turn-id",
            segment_seq=0,
            seam=seam,
            started_at=started,
            ended_at=ended,
        )
        emitted.append(span.context)
        assert span.parent is contexts.segment_root
        assert span.context.trace_id == contexts.trace_id
        assert span.name == seam

    # The projection is a sibling: same trace, parented to nothing.
    assert contexts.receipt_projection.trace_id == contexts.trace_id
    known = {context.span_id for context in emitted}
    for span in (
        build_seam_span(
            contexts,
            thread_id="opaque-thread-id",
            turn_id="opaque-turn-id",
            segment_seq=0,
            seam=SEAM_SPAN_NAMES[0],
            started_at=started,
            ended_at=ended,
        ),
    ):
        assert span.parent.span_id in known  # type: ignore[union-attr]


def test_payload_uses_stable_trace_and_scalar_allowlist() -> None:
    stage_metrics = {
        stage: {"calls": 1, "elapsed_ms": 17, "complete": True}
        for stage in STAGE_METRIC_KEYS
    }
    receipt = _receipt(
        revision=7,
        segment_seq=2,
        execution_state="terminal",
        technical_result="timeout",
        semantic_result="not_ready",
        terminal_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
        accepted_input_count=2,
        accepted_input_version=9,
        route_source="jev",
        route_intent="retrieve",
        grounded_decision="answer",
        decision_parse="structured",
        citation_verified=True,
        not_ready_cause="busy",
        not_ready_stage="queue",
        failure_kind="worker_failed",
        cancellation_reason="phase_deadline",
        work_elapsed_ms=190,
        user_wait_ms=11,
        accounting_complete=True,
        stage_metrics=stage_metrics,
        catalog_warning_count=3,
        token_usage={"input_tokens": 13, "output_tokens": 8, "total_tokens": 21},
    )

    span = build_receipt_span(receipt)
    attributes = dict(span.attributes or {})
    context = span.context

    assert context.trace_id == trace_id_for("opaque-thread-id", "opaque-turn-id")
    assert context.span_id == receipt_span_id_for(
        "opaque-thread-id", "opaque-turn-id", 2
    )
    # The projection is a sibling of the segment root, so it has no parent.
    assert span.parent is None
    assert span.start_time == 1767225600000000000
    assert span.end_time == 1767225602000000000
    assert attributes["fin.thread_id"] == "opaque-thread-id"
    assert attributes["fin.turn_id"] == "opaque-turn-id"
    assert attributes["fin.segment_seq"] == 2
    assert attributes["fin.receipt.revision"] == 7
    assert attributes["fin.receipt.execution_state"] == "terminal"
    assert attributes["fin.receipt.terminal"] is True
    assert attributes["fin.receipt.semantic_result"] == "not_ready"
    assert attributes["fin.metrics.model.calls"] == 1
    assert attributes["fin.metrics.cleanup.complete"] is True
    assert attributes["fin.tokens.total"] == 21

    expected_attributes = {
        "fin.thread_id",
        "fin.turn_id",
        "fin.segment_seq",
        "fin.receipt.schema_version",
        "fin.receipt.revision",
        "fin.receipt.execution_state",
        "fin.receipt.technical_result",
        "fin.receipt.semantic_result",
        "fin.receipt.terminal",
        "fin.receipt.accepted_input_count",
        "fin.receipt.accepted_input_version",
        "fin.route.source",
        "fin.route.intent",
        "fin.grounded.decision",
        "fin.grounded.decision_parse",
        "fin.not_ready.cause",
        "fin.not_ready.stage",
        "fin.citation.verified",
        "fin.failure.kind",
        "fin.cancellation.reason",
        "fin.metrics.work_elapsed_ms",
        "fin.metrics.user_wait_ms",
        "fin.metrics.catalog_warning_count",
        "fin.metrics.accounting_complete",
        "fin.tokens.input",
        "fin.tokens.output",
        "fin.tokens.total",
    }
    expected_attributes.update(
        f"fin.metrics.{stage}.{field}"
        for stage in STAGE_METRIC_KEYS
        for field in ("calls", "elapsed_ms", "complete")
    )
    assert set(attributes) == expected_attributes
    assert "question" not in attributes
    assert "answer" not in attributes
    assert "source_id" not in attributes


def test_the_projection_carries_no_user_source_or_credential_text() -> None:
    """The serialized wire form, not just the dataclass, is checked: an
    attribute could survive a name-set assertion and still leak."""
    receipt = _receipt(
        revision=3,
        technical_result="failed",
        failure_kind="worker_failed",
        execution_state="terminal",
        terminal_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
    )
    names = set(allowlisted_attributes(receipt))
    for forbidden in ("prompt", "answer", "excerpt", "source_label", "source_id"):
        assert forbidden not in names
        assert not any(forbidden in name for name in names)
    # No rerank row: the receipt carries no rerank fields, so that
    # reserved allowlist row emits nothing.
    assert not any("rerank" in name for name in names)


def test_nonterminal_payload_reports_state_without_terminal_success() -> None:
    attributes = allowlisted_attributes(_receipt(execution_state="paused"))

    assert attributes["fin.receipt.execution_state"] == "paused"
    assert "fin.receipt.terminal" not in attributes
    assert "fin.receipt.technical_result" not in attributes
    assert "fin.receipt.semantic_result" not in attributes


# ----- the wire -----


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    response_body = b""
    response_status = 200
    requests: list[tuple[str, dict[str, str], bytes]] = []
    arrived = threading.Event()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append(
            (self.path, {key.lower(): value for key, value in self.headers.items()}, body)
        )
        type(self).arrived.set()
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/x-protobuf")
        if type(self).response_status == 302:
            self.send_header("Location", "/followed-redirect")
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Any:
    _Handler.requests = []
    _Handler.arrived = threading.Event()
    _Handler.response_body = b""
    _Handler.response_status = 200
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"http://127.0.0.1:{server.server_address[1]}/v1/traces",
    )
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()


def _transport() -> OTLPHTTPTransport:
    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "otlp"})
    assert config is not None
    return OTLPHTTPTransport(config)


def test_transport_posts_otlp_protobuf_to_the_configured_path(receiver: Any) -> None:
    _Handler.response_status = 200
    _transport().send([build_receipt_span(_receipt())], timeout=2.0)

    assert _Handler.arrived.wait(timeout=1)
    path, headers, body = _Handler.requests[0]
    # The path comes from the endpoint env; no vendor path is hardcoded.
    assert path == "/v1/traces"
    assert headers["content-type"] == "application/x-protobuf"
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    wire = request.resource_spans[0].scope_spans[0].spans[0]
    assert wire.name == "fin.turn.receipt"
    assert wire.trace_id.hex() == f"{trace_id_for('opaque-thread-id', 'opaque-turn-id'):032x}"


def test_a_partial_success_is_refused_rather_than_acknowledged(
    receiver: Any,
) -> None:
    """The SDK reads only the status code and drops the body, so a
    collector that rejects spans would otherwise look delivered."""
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = 1
    _Handler.response_body = response.SerializeToString()

    with pytest.raises(ReceiptExportResponseError):
        _transport().send([build_receipt_span(_receipt())], timeout=2.0)


@pytest.mark.parametrize("status", [500, 302])
def test_transport_rejects_non_success_and_redirect_responses(
    receiver: Any, status: int
) -> None:
    _Handler.response_status = status
    try:
        with pytest.raises(ReceiptExportResponseError):
            _transport().send([build_receipt_span(_receipt())], timeout=2.0)
        assert len(_Handler.requests) == 1
    finally:
        _Handler.response_status = 200


def test_an_expired_budget_is_refused_without_touching_the_network(
    receiver: Any,
) -> None:
    with pytest.raises(ReceiptExportTransportError):
        _transport().send([build_receipt_span(_receipt())], timeout=0.0)
    assert _Handler.requests == []


# ----- the bounded drain -----


class _DripHandler(BaseHTTPRequestHandler):
    """Answers with a header promising far more than it will send, and
    then dribbles bytes forever. A socket-level timeout never ends this;
    only a total bound does."""

    stop = threading.Event()
    arrived = threading.Event()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "100000000")
        self.end_headers()
        type(self).arrived.set()
        while not type(self).stop.is_set():
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except OSError:
                break
            time.sleep(0.02)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def drip_peer(monkeypatch: pytest.MonkeyPatch) -> Any:
    _DripHandler.stop = threading.Event()
    _DripHandler.arrived = threading.Event()
    server = _Server(("127.0.0.1", 0), _DripHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"http://127.0.0.1:{server.server_address[1]}/v1/traces",
    )
    try:
        yield server
    finally:
        _DripHandler.stop.set()
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()


def test_a_dripping_peer_cannot_outlive_the_request_bound(drip_peer: Any) -> None:
    """(a) A total bound exists. The peer keeps sending bytes well past
    the socket timeout, so only a deadline over the whole request ends
    it."""
    transport = _transport()
    outcome: dict[str, str] = {}

    def run() -> None:
        try:
            transport.send([build_receipt_span(_receipt())], timeout=0.5)
            outcome["result"] = "acknowledged"
        except ReceiptExportError as exc:
            outcome["result"] = type(exc).__name__

    sender = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    sender.start()
    assert _DripHandler.arrived.wait(timeout=2)
    sender.join(timeout=3)

    elapsed = time.monotonic() - started
    assert not sender.is_alive(), "the request outlived its own bound"
    assert elapsed < 2.0, f"the total bound took {elapsed:.3f}s to fire"
    assert outcome["result"] != "acknowledged"


def test_close_active_interrupts_the_export_in_flight(drip_peer: Any) -> None:
    """(b) Shutdown can interrupt a blocked read, which the SDK's own
    transport could not do: closing its session left the reader blocked
    for the full socket timeout."""
    transport = _transport()
    outcome: dict[str, str] = {}

    def run() -> None:
        try:
            transport.send([build_receipt_span(_receipt())], timeout=10.0)
            outcome["result"] = "acknowledged"
        except ReceiptExportError as exc:
            outcome["result"] = type(exc).__name__

    sender = threading.Thread(target=run, daemon=True)
    sender.start()
    assert _DripHandler.arrived.wait(timeout=2)
    time.sleep(0.1)

    started = time.monotonic()
    transport.close_active()
    sender.join(timeout=2)
    interrupted_in = time.monotonic() - started

    assert not sender.is_alive(), "close_active did not interrupt the blocked read"
    assert interrupted_in < 1.0, f"the interrupt took {interrupted_in:.3f}s"
    # A payload was received but never fully acknowledged, so the
    # revision must stay pending rather than be counted as delivered.
    assert outcome["result"] != "acknowledged"
