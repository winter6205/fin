"""Bounded scanner and sender for durable receipt revisions."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable

from opentelemetry.sdk.trace import ReadableSpan

from fin.host.receipt_export import (
    ConsoleSpanTransport,
    OTLPHTTPTransport,
    ReceiptExportConfig,
    ReceiptExportConfigError,
    ReceiptExportError,
    ReceiptExportResponseError,
    build_receipt_span,
)
from fin.host.thread_store import PersistenceUnavailableError
from fin.host.trace_span_store import PostgresSpanTransport
from fin.host.turn_receipts import TurnReceiptProjector
from fin.host.turn_receipt_store import ReceiptError, ReceiptStore, TurnReceipt

MAX_QUEUED_TURNS = 256
SCAN_LIMIT = 64
SCAN_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 2.0
SHUTDOWN_DRAIN_SECONDS = 2.0
RETRY_WAITS_SECONDS = (0.25, 1.0)
_STORE_OPERATION_TIMEOUT_SECONDS = 0.75
_THREAD_JOIN_GRACE_SECONDS = 0.05
_IDENTITY = tuple[str, str]
_SEGMENT_IDENTITY = tuple[str, str, int]


@dataclass(frozen=True)
class _FinishedSegment:
    identity: _SEGMENT_IDENTITY
    spans: tuple[ReadableSpan, ...]


@dataclass(frozen=True)
class ReceiptExportDiagnostics:
    queued_turns: int
    queue_saturation_count: int
    scan_failure_count: int
    attempt_count: int
    export_failure_count: int
    malformed_response_count: int


class ReceiptExportWorkerError(ReceiptExportError):
    """The exporter worker could not start or keep its owned threads."""


class ReceiptExportWorker:
    """One application-owned scanner and independent HTTP sender."""

    def __init__(
        self,
        store: ReceiptStore,
        transport: OTLPHTTPTransport | ConsoleSpanTransport | PostgresSpanTransport,
        *,
        scan_interval: float = SCAN_INTERVAL_SECONDS,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        shutdown_timeout: float = SHUTDOWN_DRAIN_SECONDS,
    ) -> None:
        for name, value in (
            ("scan_interval", scan_interval),
            ("request_timeout", request_timeout),
            ("shutdown_timeout", shutdown_timeout),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ReceiptExportWorkerError(f"{name} must be a positive duration")
            if not 0 < value < float("inf"):
                raise ReceiptExportWorkerError(f"{name} must be a positive duration")
        if shutdown_timeout <= _THREAD_JOIN_GRACE_SECONDS:
            raise ReceiptExportWorkerError("shutdown_timeout is too short")
        self._store = store
        self._transport = transport
        self._scan_interval = scan_interval
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._queue: queue.Queue[TurnReceipt | _FinishedSegment] = queue.Queue(
            MAX_QUEUED_TURNS
        )
        self._queued: set[_IDENTITY] = set()
        self._queued_segments: set[_SEGMENT_IDENTITY] = set()
        self._queue_lock = threading.Lock()
        self._counts_lock = threading.Lock()
        self._counts = {
            "queue_saturation_count": 0,
            "scan_failure_count": 0,
            "attempt_count": 0,
            "export_failure_count": 0,
            "malformed_response_count": 0,
        }
        self._scan_stop = threading.Event()
        self._sender_force_stop = threading.Event()
        self._drain_requested = threading.Event()
        self._threads: tuple[threading.Thread, threading.Thread] | None = None
        self._cursor: _IDENTITY | None = None
        self._deadline: float | None = None
        self._lifecycle_lock = threading.Lock()

    @classmethod
    def configured(
        cls, store: ReceiptStore, config: ReceiptExportConfig
    ) -> "ReceiptExportWorker":
        """The worker one resolved exporter selection owns.

        :raises ReceiptExportConfigError: the selection names no transport
            this build has. A silent fallback would export one
            configuration's payloads through another's transport.
        """
        if config.exporter == "console":
            return cls(store, ConsoleSpanTransport())
        if config.exporter == "otlp":
            return cls(store, OTLPHTTPTransport(config))
        if config.exporter == "pg":
            return cls(store, PostgresSpanTransport(config))
        raise ReceiptExportConfigError(
            f"the {config.exporter!r} exporter has no transport"
        )

    @property
    def diagnostics(self) -> ReceiptExportDiagnostics:
        with self._queue_lock:
            queued_turns = len(self._queued)
        with self._counts_lock:
            return ReceiptExportDiagnostics(queued_turns, **self._counts)

    def start(self) -> Callable[[], None]:
        with self._lifecycle_lock:
            if self._threads is not None:
                raise ReceiptExportWorkerError(
                    "receipt exporter worker is already started"
                )
            scanner = threading.Thread(
                target=self._scan_loop,
                name="fin-receipt-export-scan",
                daemon=True,
            )
            sender = threading.Thread(
                target=self._send_loop,
                name="fin-receipt-export-send",
                daemon=True,
            )
            self._threads = scanner, sender
            try:
                sender.start()
            except Exception as exc:
                self._threads = None
                raise ReceiptExportWorkerError(
                    "could not start receipt export sender"
                ) from exc
            try:
                scanner.start()
            except Exception as exc:
                self._sender_force_stop.set()
                sender.join(timeout=0.5)
                self._threads = None
                raise ReceiptExportWorkerError(
                    "could not start receipt export scanner"
                ) from exc
        return self.stop

    def stop(self) -> None:
        threads = self._threads
        if threads is None:
            return
        overall_deadline = time.monotonic() + self._shutdown_timeout
        drain_deadline = overall_deadline - _THREAD_JOIN_GRACE_SECONDS
        self._deadline = drain_deadline
        self._scan_stop.set()
        scanner, sender = threads
        scanner.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        self._drain_requested.set()
        sender.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        if scanner.is_alive() or sender.is_alive():
            self._sender_force_stop.set()
            self._transport.close_active()
            scanner.join(timeout=max(0.0, overall_deadline - time.monotonic()))
            sender.join(timeout=max(0.0, overall_deadline - time.monotonic()))
        if scanner.is_alive() or sender.is_alive():
            raise ReceiptExportWorkerError(
                "receipt exporter threads did not stop before the shutdown deadline"
            )
        if not scanner.is_alive() and not sender.is_alive():
            with self._lifecycle_lock:
                if self._threads == threads:
                    self._threads = None

    def _scan_loop(self) -> None:
        while not self._scan_stop.is_set():
            self._scan_pending()
            if self._scan_stop.wait(self._scan_interval):
                return

    def _scan_pending(self) -> None:
        try:
            receipts = self._store.list_pending_receipts(
                after=self._cursor, limit=SCAN_LIMIT
            )
        except (PersistenceUnavailableError, ReceiptError):
            self._increment("scan_failure_count")
            return
        if not receipts:
            self._cursor = None
            return
        for receipt in receipts:
            if self._scan_stop.is_set():
                return
            identity = receipt.thread_id, receipt.turn_id
            self._enqueue(receipt)
            self._cursor = identity

    def _enqueue(self, receipt: TurnReceipt) -> bool:
        identity = receipt.thread_id, receipt.turn_id
        with self._queue_lock:
            if identity in self._queued:
                return False
            if (
                len(self._queued) + len(self._queued_segments)
                >= MAX_QUEUED_TURNS
            ):
                self._increment("queue_saturation_count")
                return False
            self._queued.add(identity)
            try:
                self._queue.put_nowait(receipt)
            except queue.Full:
                self._queued.remove(identity)
                self._increment("queue_saturation_count")
                return False
            return True

    def enqueue_finished_spans(self, spans: Sequence[ReadableSpan]) -> bool:
        """Queue one root-and-children batch without blocking its caller."""
        batch = tuple(spans)
        root = next(
            (span for span in batch if span.name == "fin.turn.segment"), None
        )
        if root is None:
            return False
        attributes = root.attributes or {}
        thread_id = attributes.get("fin.thread_id")
        turn_id = attributes.get("fin.turn_id")
        segment_seq = attributes.get("fin.segment_seq")
        if (
            not isinstance(thread_id, str)
            or not isinstance(turn_id, str)
            or not isinstance(segment_seq, int)
            or isinstance(segment_seq, bool)
        ):
            return False
        identity = thread_id, turn_id, segment_seq
        item = _FinishedSegment(identity, batch)
        with self._queue_lock:
            if identity in self._queued_segments:
                return False
            if len(self._queued) + len(self._queued_segments) >= MAX_QUEUED_TURNS:
                self._increment("queue_saturation_count")
                return False
            self._queued_segments.add(identity)
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._queued_segments.remove(identity)
                self._increment("queue_saturation_count")
                return False
            return True

    def _send_loop(self) -> None:
        while not self._sender_force_stop.is_set():
            if self._expired():
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._drain_requested.is_set():
                    return
                continue
            try:
                if isinstance(item, _FinishedSegment):
                    self._export_finished_segment(item)
                else:
                    self._export(item)
            finally:
                with self._queue_lock:
                    if isinstance(item, _FinishedSegment):
                        self._queued_segments.discard(item.identity)
                    else:
                        self._queued.discard((item.thread_id, item.turn_id))
                self._queue.task_done()

    def _export_finished_segment(self, segment: _FinishedSegment) -> None:
        if not self._expired():
            self._send_payload(segment.spans)

    def _export(self, receipt: TurnReceipt) -> None:
        if self._expired():
            return
        if receipt.exported_revision >= receipt.revision:
            return
        try:
            payload = build_receipt_span(receipt)
        except ReceiptError:
            self._increment("export_failure_count")
            return
        if not self._send_payload((payload,)):
            return
        if self._store_call_would_outlive_drain():
            return
        try:
            self._store.acknowledge_export(
                receipt.thread_id, receipt.turn_id, receipt.revision
            )
        except (PersistenceUnavailableError, ReceiptError):
            self._increment("export_failure_count")

    def _send_payload(self, payload: Sequence[ReadableSpan]) -> bool:
        for attempt in range(3):
            timeout = self._request_timeout
            if self._deadline is not None:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    return False
                timeout = min(timeout, remaining)
            if self._sender_force_stop.is_set():
                return False
            self._increment("attempt_count")
            try:
                self._transport.send(payload, timeout=timeout)
            except ReceiptExportResponseError:
                self._increment("malformed_response_count")
                self._retry_or_finish(attempt)
                continue
            except ReceiptExportError:
                self._retry_or_finish(attempt)
                continue
            if self._expired():
                return False
            return True
        self._increment("export_failure_count")
        return False

    def _retry_or_finish(self, attempt: int) -> None:
        if attempt >= len(RETRY_WAITS_SECONDS) or self._sender_force_stop.is_set():
            return
        wait = RETRY_WAITS_SECONDS[attempt]
        if self._deadline is not None:
            wait = min(wait, max(0.0, self._deadline - time.monotonic()))
        if wait > 0:
            self._sender_force_stop.wait(wait)

    def _expired(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _store_call_would_outlive_drain(self) -> bool:
        return self._deadline is not None and (
            self._deadline - time.monotonic() < _STORE_OPERATION_TIMEOUT_SECONDS
        )

    def _increment(self, name: str) -> None:
        with self._counts_lock:
            self._counts[name] += 1


def start_application_receipt_export_worker(
    *,
    store: ReceiptStore,
    config: ReceiptExportConfig | None,
    projector: TurnReceiptProjector | None = None,
) -> Callable[[], None]:
    """Start one configured export worker; disabled export owns no thread."""
    if config is None:
        return lambda: None
    worker = ReceiptExportWorker.configured(store, config)
    stop = worker.start()
    if projector is None:
        return stop
    unbind = projector.bind_finished_span_sink(worker.enqueue_finished_spans)

    def release() -> None:
        unbind()
        stop()

    return release


__all__ = [
    "MAX_QUEUED_TURNS",
    "ReceiptExportDiagnostics",
    "ReceiptExportWorker",
    "ReceiptExportWorkerError",
    "start_application_receipt_export_worker",
]
