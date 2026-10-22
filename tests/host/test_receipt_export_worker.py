from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from fin.host.receipt_export import (
    ConsoleSpanTransport,
    OTLPHTTPTransport,
    ReceiptExportConfig,
    ReceiptExportConfigError,
    load_receipt_export_config,
)
from fin.host.receipt_export_worker import (
    MAX_QUEUED_TURNS,
    ReceiptExportWorker,
    start_application_receipt_export_worker,
)
from fin.host.application.lifetime import ApplicationResources, RuntimeHandle, StartupSteps
from fin.host.thread_store import InMemoryThreadStore, PostgresThreadStore
from fin.host.trace_span_store import PostgresSpanTransport
from fin.host.turn_receipts import default_receipts
from fin.host.span_seams import current_segment, publish_turn_segment, seam_span
from fin.host.turn_receipt_store import (
    InMemoryReceiptStore,
    PostgresReceiptStore,
    TurnReceipt,
)


@dataclass
class _ReceiverState:
    response_statuses: list[int] = field(default_factory=lambda: [200])
    response_body: bytes = b""
    hold_first_response: bool = False
    requests: list[bytes] = field(default_factory=list)
    arrived: threading.Event = field(default_factory=threading.Event)
    release_first_response: threading.Event = field(default_factory=threading.Event)
    request_lock: threading.Lock = field(default_factory=threading.Lock)
    port: int = 0


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        state: _ReceiverState = self.server.state  # type: ignore[attr-defined]
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with state.request_lock:
            state.requests.append(body)
            request_index = len(state.requests)
        state.arrived.set()
        if request_index == 1 and state.hold_first_response:
            state.release_first_response.wait(timeout=4)
        response_index = min(request_index - 1, len(state.response_statuses) - 1)
        self.send_response(state.response_statuses[response_index])
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()
        try:
            self.wfile.write(state.response_body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = _ReceiverState()
    server = _Server(("127.0.0.1", 0), _Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.port = server.server_address[1]
    # The endpoint is the SDK's own resolution; the transport reads it.
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"http://127.0.0.1:{state.port}/v1/traces",
    )
    try:
        yield state
    finally:
        state.release_first_response.set()
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()


def _span_names(body: bytes) -> list[str]:
    """The span names in one serialized OTLP request."""
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    return [
        span.name
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]


def _configured_store(turn_ids: tuple[str, ...] = ("turn-1",)) -> InMemoryReceiptStore:
    threads = InMemoryThreadStore()
    threads.create_thread(id="thread-1", title="receipt export")
    store = InMemoryReceiptStore(threads)
    for turn_id in turn_ids:
        store.begin_turn(
            thread_id="thread-1",
            turn_id=turn_id,
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            accepted_input_version=0,
        )
    return store


def _transport_for(port: int):
    from fin.host.receipt_export import OTLPHTTPTransport

    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "otlp"})
    assert config is not None
    return OTLPHTTPTransport(config)


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_worker_scans_and_acknowledges_from_loopback_otlp_receiver(receiver: Any) -> None:
    store = _configured_store()
    worker = ReceiptExportWorker(
        store,
        _transport_for(receiver.port),
        scan_interval=0.02,
    )
    stop = worker.start()

    try:
        assert receiver.arrived.wait(timeout=2)
        assert _wait_until(
            lambda: store.read_receipt("thread-1", "turn-1").exported_revision == 1
        )
        assert _span_names(receiver.requests[0]) == ["fin.turn.receipt"]
        assert worker.diagnostics.attempt_count == 1
    finally:
        stop()


def test_console_exporter_runs_through_the_bounded_worker(capsys: Any) -> None:
    store = _configured_store()
    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "console"})
    assert config is not None
    worker = ReceiptExportWorker.configured(store, config)
    stop = worker.start()

    try:
        assert _wait_until(
            lambda: store.read_receipt("thread-1", "turn-1").exported_revision == 1
        )
    finally:
        stop()

    assert "fin.turn.receipt" in capsys.readouterr().out


def test_configured_builds_the_transport_the_selection_names() -> None:
    """Every supported exporter has an explicit arm, including ``pg``."""
    store = _configured_store()
    pg = ReceiptExportWorker.configured(
        store, ReceiptExportConfig("pg", "postgresql://fin@127.0.0.1:1/fin_trace_span_test")
    )
    console = ReceiptExportWorker.configured(store, ReceiptExportConfig("console"))
    otlp = ReceiptExportWorker.configured(store, ReceiptExportConfig("otlp"))

    assert isinstance(pg._transport, PostgresSpanTransport)
    assert isinstance(console._transport, ConsoleSpanTransport)
    assert isinstance(otlp._transport, OTLPHTTPTransport)


@pytest.mark.parametrize("exporter", ["none", "jaeger-udp", ""])
def test_configured_refuses_a_selection_with_no_transport(exporter: str) -> None:
    """No silent catch-all: an unknown selection cannot borrow a transport."""
    store = _configured_store()

    with pytest.raises(ReceiptExportConfigError):
        ReceiptExportWorker.configured(store, ReceiptExportConfig(exporter))


def test_application_worker_queues_one_finished_segment_off_the_calling_thread(
    receiver: Any,
) -> None:
    receiver.hold_first_response = True
    threads = InMemoryThreadStore()
    threads.create_thread(id="segment-thread", title="segment export")
    projector = default_receipts(threads)
    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "otlp"})
    assert config is not None
    stop = start_application_receipt_export_worker(
        store=projector.store,
        config=config,
        projector=projector,
    )

    try:
        started = time.monotonic()
        with publish_turn_segment(projector, "segment-thread", "segment-turn"):
            with seam_span("classify_node", current_segment()):
                pass
        elapsed = time.monotonic() - started

        assert elapsed < 0.2
        assert receiver.arrived.wait(timeout=2)
        assert _span_names(receiver.requests[0]) == [
            "fin.turn.segment",
            "classify_node",
        ]
    finally:
        receiver.release_first_response.set()
        stop()


def test_console_segment_sinks_stay_with_their_own_projectors(capsys: Any) -> None:
    previous_provider = getattr(trace_api, "_TRACER_PROVIDER", None)
    global_exporter = InMemorySpanExporter()
    global_provider = TracerProvider()
    global_provider.add_span_processor(SimpleSpanProcessor(global_exporter))
    trace_api._TRACER_PROVIDER = global_provider
    projects: list[tuple[Any, str]] = []
    stops: list[Any] = []
    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "console"})
    assert config is not None

    for _index in (1, 2):
        threads = InMemoryThreadStore()
        thread_id = "isolated-thread"
        threads.create_thread(id=thread_id, title="isolated segment")
        projector = default_receipts(threads)
        projects.append((projector, thread_id))
        stops.append(
            start_application_receipt_export_worker(
                store=projector.store,
                config=config,
                projector=projector,
            )
        )

    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def publish(projector: Any, thread_id: str) -> None:
        try:
            with publish_turn_segment(projector, thread_id, "isolated-turn"):
                with seam_span("classify_node", current_segment()):
                    barrier.wait(timeout=2)
        except BaseException as exc:
            failures.append(exc)

    publishers = [
        threading.Thread(target=publish, args=project, daemon=True)
        for project in projects
    ]
    try:
        for publisher in publishers:
            publisher.start()
        for publisher in publishers:
            publisher.join(timeout=3)
        assert not any(publisher.is_alive() for publisher in publishers)
        assert failures == []

        disabled_threads = InMemoryThreadStore()
        disabled_threads.create_thread(id="disabled-thread", title="no exporter")
        disabled_projector = default_receipts(disabled_threads)
        with publish_turn_segment(
            disabled_projector, "disabled-thread", "disabled-turn"
        ):
            with seam_span("classify_node", current_segment()):
                pass
        assert global_exporter.get_finished_spans() == ()
    finally:
        barrier.abort()
        for stop in reversed(stops):
            stop()
        assert all(projector.finished_span_sink is None for projector, _ in projects)
        trace_api._TRACER_PROVIDER = previous_provider

    output = capsys.readouterr().out
    assert output.count("fin.turn.segment") == 2
    assert output.count("classify_node") == 2
    assert output.count("isolated-thread") == 4


def test_application_lifetime_exports_the_shared_projector_receipt(
    receiver: Any,
) -> None:
    threads = InMemoryThreadStore()
    threads.create_thread(id="composition-thread", title="receipt export")
    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "otlp"})
    assert config is not None
    inert_release = lambda: None
    steps = StartupSteps(
        require_single_process=lambda: None,
        apply_operator_settings=lambda: None,
        attachment_budgets=lambda: None,
        receipt_export_config=lambda: config,
        require_checkpoint_url=lambda: None,
        preflight_ingest_storage=lambda: None,
        publish_knowledge_mcp=lambda: None,
        # ``**_kwargs`` so this composition case is about the exporter
        # seam, not about which arguments the runtime compile takes.
        compile_runtime=lambda **_kwargs: RuntimeHandle(object()),
        start_ingest_worker=lambda: inert_release,
        thread_store=lambda: threads,
        attachment_service=lambda: object(),
        start_attachment_prep_worker=lambda _service: inert_release,
        start_attachment_resume_worker=lambda **_kwargs: inert_release,
        start_invalidation_drain_worker=lambda **_kwargs: inert_release,
        start_receipt_export_worker=start_application_receipt_export_worker,
        stop_ingest_worker=inert_release,
        stop_attachment_prep_worker=inert_release,
        stop_attachment_resume_worker=inert_release,
        stop_invalidation_drain_worker=inert_release,
        stop_receipt_export_worker=inert_release,
        workspace_root=lambda: Path("unused"),
    )
    resources = ApplicationResources(build_steps=lambda: steps)
    resources.startup()

    try:
        receipt = default_receipts(threads).begin(
            thread_id="composition-thread",
            turn_id="composition-turn",
            accepted_input_version=0,
        )
        assert receiver.arrived.wait(timeout=2.5)
        assert _wait_until(
            lambda: default_receipts(threads)
            .store.read_receipt(receipt.thread_id, receipt.turn_id)
            .exported_revision
            == receipt.revision
        )
        assert _span_names(receiver.requests[0]) == ["fin.turn.receipt"]
    finally:
        resources.shutdown()


def test_worker_retries_the_captured_revision_while_scan_continues(receiver: Any) -> None:
    receiver.hold_first_response = True
    receiver.response_statuses = [500, 200]
    store = _configured_store()
    worker = ReceiptExportWorker(
        store,
        _transport_for(receiver.port),
        scan_interval=0.02,
    )
    stop = worker.start()

    try:
        assert receiver.arrived.wait(timeout=2)
        store.write_receipt(
            TurnReceipt(
                thread_id="thread-1",
                turn_id="turn-1",
                revision=1,
                execution_state="running",
                semantic_result="overview",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            ),
            expected_revision=1,
        )
        receiver.release_first_response.set()
        assert _wait_until(lambda: len(receiver.requests) >= 2)
        assert receiver.requests[0] == receiver.requests[1]
        assert _wait_until(
            lambda: store.read_receipt("thread-1", "turn-1").exported_revision == 2
        )
    finally:
        receiver.release_first_response.set()
        stop()


def test_worker_does_not_ack_partial_otlp_response(receiver: Any) -> None:
    """A 200 that rejected spans is not a delivery. The SDK reads only
    the status code and drops the body, so the transport has to read the
    partial success back out for the revision to stay pending."""
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = 1
    receiver.response_body = response.SerializeToString()
    store = _configured_store()
    worker = ReceiptExportWorker(
        store,
        _transport_for(receiver.port),
        scan_interval=10,
    )
    stop = worker.start()

    try:
        # Wait on the exporter's own accounting, not the receiver's tally:
        # the receiver records the THIRD request before the sender has read
        # its response, so a request-count wait can sample the diagnostics
        # while the third rejection is still in flight. The failure counter
        # is set only after the third malformed answer has been processed.
        assert _wait_until(
            lambda: worker.diagnostics.export_failure_count == 1, timeout=3.0
        )
        current = store.read_receipt("thread-1", "turn-1")
        assert current is not None
        assert current.exported_revision == 0
        assert current.revision == 1
        assert len(receiver.requests) == 3
        assert worker.diagnostics.attempt_count == 3
        assert worker.diagnostics.malformed_response_count == 3
        assert worker.diagnostics.export_failure_count == 1
    finally:
        stop()


def test_queue_coalesces_duplicates_and_never_exceeds_256_identities() -> None:
    store = _configured_store()
    worker = ReceiptExportWorker(store, _transport_for(1))
    receipts = [
        TurnReceipt(thread_id=f"thread-{index}", turn_id="turn")
        for index in range(MAX_QUEUED_TURNS)
    ]

    for receipt in receipts:
        assert worker._enqueue(receipt)
    assert not worker._enqueue(receipts[0])
    assert not worker._enqueue(TurnReceipt(thread_id="extra", turn_id="turn"))

    diagnostics = worker.diagnostics
    assert diagnostics.queued_turns == MAX_QUEUED_TURNS
    assert diagnostics.queue_saturation_count == 1


def test_stop_joins_sender_blocked_in_postgres_ack(
    receiver: Any, tmp_path: Path
) -> None:
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip("FIN_TEST_DSN is not set; durable shutdown check was not run")

    import psycopg

    with psycopg.connect(url) as identity_conn:
        database_name = identity_conn.execute("SELECT current_database()").fetchone()[0]
        assert database_name.endswith("_test")
        has_identity_table = identity_conn.execute(
            "SELECT to_regclass('public.fin_instance') IS NOT NULL"
        ).fetchone()[0]
        identity = None
        if has_identity_table:
            rows = identity_conn.execute("SELECT id::text FROM fin_instance").fetchall()
            assert len(rows) <= 1
            identity = rows[0][0] if rows else None
    if identity is not None:
        identity_file = tmp_path / "instance_id"
        identity_file.write_text(f"{identity}\n", encoding="utf-8")
        identity_file.chmod(0o600)

    thread_id = f"receipt-shutdown-{uuid4().hex}"
    turn_id = f"turn-{uuid4().hex}"
    threads = PostgresThreadStore(url, fin_home=tmp_path)
    store = PostgresReceiptStore(url, fin_home=tmp_path)
    threads.create_thread(id=thread_id, title="receipt shutdown test")
    store.begin_turn(
        thread_id=thread_id,
        turn_id=turn_id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        accepted_input_version=0,
    )
    worker = ReceiptExportWorker(
        store,
        _transport_for(receiver.port),
        scan_interval=0.02,
    )
    stop = worker.stop
    try:
        with psycopg.connect(url) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT 1 FROM fin_turn_receipt "
                    "WHERE thread_id = %s AND turn_id = %s FOR UPDATE",
                    (thread_id, turn_id),
                )
                stop = worker.start()
                assert receiver.arrived.wait(timeout=2)
                assert _wait_until(
                    lambda: _postgres_ack_is_waiting(url), timeout=2
                )
                started = time.monotonic()
                stop()
                stop_elapsed = time.monotonic() - started
                assert stop_elapsed < 1.5
                assert worker._threads is None
                current = store.read_receipt(thread_id, turn_id)
                assert current is not None
                assert current.exported_revision == 0
    finally:
        stop()
        if threads.get_thread(thread_id) is not None:
            threads.delete_thread(thread_id)


def _postgres_ack_is_waiting(url: str) -> bool:
    import psycopg

    with psycopg.connect(url) as connection:
        return connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock' "
            "AND query LIKE 'SELECT revision, exported_revision%FOR UPDATE%')"
        ).fetchone()[0]
