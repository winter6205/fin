"""Turn-receipt spans over the OpenTelemetry SDK, configured by ``OTEL_*``.

The exporter is selected through the standard environment variables, so
any OTLP/HTTP backend is reached by configuration and none by code. The
SDK owns the envelope, the endpoint resolution and the retry policy;
this module owns the deterministic correlation and the attribute
allowlist, which are fin's, and the request bound the shutdown drain
depends on.

``OTEL_TRACES_EXPORTER`` selects the exporter and every choice is OFF
unless it is named:

* ``none`` (the default) selects no exporter at all — no worker, no
  transport, no span built, no connection;
* ``console`` writes span batches to the process output;
* ``otlp`` posts them to whatever ``OTEL_EXPORTER_OTLP_*`` names;
* ``pg`` writes them to the local ``fin_trace_span`` table, whose
  database is named by ``FIN_TRACE_SPAN_DSN``. That DSN is the one
  local-PG setting this module reads; it is required by ``pg`` and
  ignored by every other choice, and its value is never echoed into a
  message or a log line.
"""

from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final
from urllib.parse import urlsplit

from opentelemetry.exporter.http.transport._base import (
    BaseHTTPResult,
    BaseHTTPTransport,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags

from fin.host.turn_receipt_store import (
    STAGE_METRIC_KEYS,
    TOKEN_USAGE_KEYS,
    TurnReceipt,
    _validate_receipt,
)

TRACES_EXPORTER_ENV: Final[str] = "OTEL_TRACES_EXPORTER"
#: The one local-PG setting: the database the ``pg`` exporter writes its
#: spans to. It is read only when ``pg`` is selected.
TRACE_SPAN_DSN_ENV: Final[str] = "FIN_TRACE_SPAN_DSN"
#: The supported selections. ``none`` is the default and owns no
#: transport at all; ``otlp`` is whatever the SDK's endpoint env points
#: at, so no backend is named here. ``pg`` is fin's own local sink and
#: is not vendor-shaped.
SUPPORTED_EXPORTERS: Final[tuple[str, ...]] = ("none", "console", "otlp", "pg")

SERVICE_NAME: Final[str] = "fin.turn_receipt"

#: The five hand-written execution seams. The set is closed on purpose:
#: growing it reopens the tracing contract rather than adding coverage.
SEAM_SPAN_NAMES: Final[tuple[str, ...]] = (
    "classify_node",
    "retrieve_tool",
    "model_node",
    "citation_gate_node",
    "admit_decision",
)

_SAMPLED: Final[TraceFlags] = TraceFlags(TraceFlags.SAMPLED)


class ReceiptExportError(Exception):
    """Base error for a receipt export that did not complete safely."""


class ReceiptExportConfigError(ReceiptExportError):
    """The requested trace exporter is unknown or incomplete."""


class ReceiptExportTransportError(ReceiptExportError):
    """The OTLP/HTTP request failed before a valid acknowledgement."""


class ReceiptExportResponseError(ReceiptExportError):
    """The collector answered, but not with a full acknowledgement."""


@dataclass(frozen=True, repr=False)
class ReceiptExportConfig:
    """Which trace exporter the process selected, if any.

    ``span_dsn`` is populated only for ``pg`` and is deliberately kept
    out of the rendered form: it is a connection string.
    """

    exporter: str
    span_dsn: str | None = None


def load_receipt_export_config(
    environ: Mapping[str, str] | None = None,
) -> ReceiptExportConfig | None:
    """Read the standard exporter selection; absent means no exporter.

    Everything else the exporter needs — endpoint, headers, TLS — is the
    SDK's own ``OTEL_EXPORTER_OTLP_*`` resolution, so this reads one
    variable and validates nothing vendor-shaped about it. The local PG
    sink is the single fin-owned exception: it needs a database, so
    ``pg`` without ``FIN_TRACE_SPAN_DSN`` is refused here rather than
    discovered later in a worker where the turn has already been
    accepted.
    """
    import os

    values = environ if environ is not None else os.environ
    exporter = values.get(TRACES_EXPORTER_ENV, "").strip().lower() or "none"
    if exporter not in SUPPORTED_EXPORTERS:
        raise ReceiptExportConfigError(
            f"{TRACES_EXPORTER_ENV} must be one of "
            f"{'|'.join(SUPPORTED_EXPORTERS)}; got {exporter!r}"
        )
    if exporter == "none":
        return None
    if exporter == "pg":
        span_dsn = values.get(TRACE_SPAN_DSN_ENV, "").strip()
        if not span_dsn:
            # EXIT: the variable NAME is the message; the value, had
            # there been one, never is.
            raise ReceiptExportConfigError(
                f"the {exporter!r} exporter needs {TRACE_SPAN_DSN_ENV}"
            )
        return ReceiptExportConfig(exporter, span_dsn)
    return ReceiptExportConfig(exporter)


# ----- deterministic correlation -----


def _correlation(thread_id: str, turn_id: str) -> bytes:
    thread = thread_id.encode("utf-8")
    turn = turn_id.encode("utf-8")
    return (
        b"fin-turn-receipt:v1\0"
        + len(thread).to_bytes(2, "big")
        + thread
        + len(turn).to_bytes(2, "big")
        + turn
    )


def _stable_id(prefix: bytes, value: bytes, size: int) -> bytes:
    result = bytearray(hashlib.sha256(prefix + b"\0" + value).digest()[:size])
    if not any(result):
        result[-1] = 1
    return bytes(result)


def trace_id_for(thread_id: str, turn_id: str) -> int:
    """The one trace a turn's receipts and all of its spans share."""
    return int.from_bytes(
        _stable_id(b"trace", _correlation(thread_id, turn_id), 16), "big"
    )


def _segment_key(thread_id: str, turn_id: str, segment_seq: int) -> bytes:
    return _correlation(thread_id, turn_id) + segment_seq.to_bytes(8, "big")


def segment_span_id_for(thread_id: str, turn_id: str, segment_seq: int) -> int:
    """The segment ROOT's span id.

    WHY the unsalted three-part derivation is the root's alone: one
    segment carries one root, five seam children and one receipt
    projection, and every one of them shares (thread_id, turn_id,
    segment_seq). The root takes this unsalted id; each seam and the
    projection salt the same key with its own name, so nothing collides,
    every id stays a function of the documented inputs, and every span
    has a parent that exists in the same trace.
    """
    return int.from_bytes(
        _stable_id(b"segment-root", _segment_key(thread_id, turn_id, segment_seq), 8),
        "big",
    )


def seam_span_id_for(
    thread_id: str, turn_id: str, segment_seq: int, seam: str
) -> int:
    """One execution-seam span id: a child of the segment root."""
    if seam not in SEAM_SPAN_NAMES:
        raise ReceiptExportError(f"unknown execution seam {seam!r}")
    return int.from_bytes(
        _stable_id(
            b"seam:" + seam.encode("utf-8"),
            _segment_key(thread_id, turn_id, segment_seq),
            8,
        ),
        "big",
    )


def receipt_span_id_for(thread_id: str, turn_id: str, segment_seq: int) -> int:
    """The receipt projection's span id: a SIBLING of the segment root.

    The projection reports durable state rather than executing inside a
    segment, so it hangs off the trace directly instead of pretending to
    be a child of work that may never have run.
    """
    return int.from_bytes(
        _stable_id(
            b"projection:receipt", _segment_key(thread_id, turn_id, segment_seq), 8
        ),
        "big",
    )


def _context(trace_id: int, span_id: int) -> SpanContext:
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=_SAMPLED,
    )


def _time_ns(value: datetime) -> int:
    utc = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    nanoseconds = (
        (delta.days * 86400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1000
    )
    if nanoseconds < 0:
        raise ReceiptExportError("receipt timestamp predates the OTLP epoch")
    return nanoseconds


# ----- the attribute allowlist -----

#: The receipt fields and the attribute name each publishes, in the
#: contract's own order. A closed literal rather than a filter over the
#: dataclass: a new receipt field is absent here until the contract
#: admits it, which is what keeps prompts, answers and source
#: identifiers out. The retrieval rerank pass-through is deliberately
#: absent: the receipt carries no rerank fields, so that row emits
#: nothing.
_RECEIPT_FIELD_ATTRIBUTES: Final[tuple[tuple[str, str], ...]] = (
    ("thread_id", "fin.thread_id"),
    ("turn_id", "fin.turn_id"),
    ("segment_seq", "fin.segment_seq"),
    ("schema_version", "fin.receipt.schema_version"),
    ("revision", "fin.receipt.revision"),
    ("execution_state", "fin.receipt.execution_state"),
    ("technical_result", "fin.receipt.technical_result"),
    ("semantic_result", "fin.receipt.semantic_result"),
    ("accepted_input_count", "fin.receipt.accepted_input_count"),
    ("accepted_input_version", "fin.receipt.accepted_input_version"),
    ("route_source", "fin.route.source"),
    ("route_intent", "fin.route.intent"),
    ("grounded_decision", "fin.grounded.decision"),
    ("decision_parse", "fin.grounded.decision_parse"),
    ("not_ready_cause", "fin.not_ready.cause"),
    ("not_ready_stage", "fin.not_ready.stage"),
    ("failure_kind", "fin.failure.kind"),
    ("cancellation_reason", "fin.cancellation.reason"),
)

#: The four receipt metrics that are always measured, so they are always
#: present rather than null-omitted.
_RECEIPT_METRIC_ATTRIBUTES: Final[tuple[tuple[str, str], ...]] = (
    ("work_elapsed_ms", "fin.metrics.work_elapsed_ms"),
    ("user_wait_ms", "fin.metrics.user_wait_ms"),
    ("catalog_warning_count", "fin.metrics.catalog_warning_count"),
    ("accounting_complete", "fin.metrics.accounting_complete"),
)

#: A stage's three fields, published as ``fin.metrics.<stage>.<field>``.
_STAGE_METRIC_FIELDS: Final[tuple[str, ...]] = ("calls", "elapsed_ms", "complete")

#: Provider usage, published with the three short names.
_TOKEN_ATTRIBUTES: Final[tuple[tuple[str, str], ...]] = tuple(
    zip(TOKEN_USAGE_KEYS, ("fin.tokens.input", "fin.tokens.output", "fin.tokens.total"))
)

#: Every name a receipt projection may carry, whether or not one revision
#: has a value for it. The local PG sink applies this closed set to any
#: span it persists, so a name outside it cannot reach the table even if
#: a future builder forgot to filter.
RECEIPT_SPAN_ATTRIBUTE_NAMES: Final[frozenset[str]] = frozenset(
    [name for _field, name in _RECEIPT_FIELD_ATTRIBUTES]
    + ["fin.receipt.terminal", "fin.citation.verified"]
    + [name for _field, name in _RECEIPT_METRIC_ATTRIBUTES]
    + [
        f"fin.metrics.{stage}.{field}"
        for stage in STAGE_METRIC_KEYS
        for field in _STAGE_METRIC_FIELDS
    ]
    + [name for _key, name in _TOKEN_ATTRIBUTES]
)


def allowlisted_attributes(receipt: TurnReceipt) -> dict[str, str | bool | int]:
    """Exactly the contract's attribute names, with nulls omitted."""
    attributes: dict[str, str | bool | int] = {}
    for field_name, name in _RECEIPT_FIELD_ATTRIBUTES:
        value = getattr(receipt, field_name)
        if value is None:
            continue
        attributes[name] = value
    # Present as true only for a terminal receipt. Absent says nothing,
    # which is not the same claim as false.
    if receipt.execution_state == "terminal":
        attributes["fin.receipt.terminal"] = True
    if receipt.citation_verified is not None:
        attributes["fin.citation.verified"] = receipt.citation_verified
    for field_name, name in _RECEIPT_METRIC_ATTRIBUTES:
        attributes[name] = getattr(receipt, field_name)
    for stage in STAGE_METRIC_KEYS:
        values = receipt.stage_metrics[stage]
        for field_name in _STAGE_METRIC_FIELDS:
            attributes[f"fin.metrics.{stage}.{field_name}"] = values[field_name]
    if receipt.token_usage is not None:
        for key, name in _TOKEN_ATTRIBUTES:
            if key in receipt.token_usage:
                attributes[name] = receipt.token_usage[key]
    return attributes


def build_receipt_span(receipt: TurnReceipt) -> ReadableSpan:
    """Project one validated receipt revision into an immutable span."""
    _validate_receipt(receipt)
    trace_id = trace_id_for(receipt.thread_id, receipt.turn_id)
    return ReadableSpan(
        name="fin.turn.receipt",
        context=_context(
            trace_id,
            receipt_span_id_for(
                receipt.thread_id, receipt.turn_id, receipt.segment_seq
            ),
        ),
        parent=None,
        resource=Resource.create({"service.name": SERVICE_NAME}),
        attributes=allowlisted_attributes(receipt),
        kind=SpanKind.INTERNAL,
        start_time=_time_ns(receipt.started_at),
        end_time=_time_ns(receipt.terminal_at or receipt.updated_at),
    )


def segment_contexts(
    thread_id: str, turn_id: str, segment_seq: int
) -> SegmentContexts:
    """The segment root's and the receipt projection's contexts.

    Handed to the five execution seams so each is a child of the root
    rather than of whatever ambient context the caller happened to be
    in. Retrieval leaves the event loop for a worker thread, so this is
    passed explicitly rather than relied on to propagate.
    """
    trace_id = trace_id_for(thread_id, turn_id)
    return SegmentContexts(
        trace_id=trace_id,
        segment_root=_context(
            trace_id, segment_span_id_for(thread_id, turn_id, segment_seq)
        ),
        receipt_projection=_context(
            trace_id, receipt_span_id_for(thread_id, turn_id, segment_seq)
        ),
    )


@dataclass(frozen=True)
class SegmentContexts:
    """The two spans at one level of a trace; the seams hang off the root."""

    trace_id: int
    segment_root: SpanContext
    receipt_projection: SpanContext


def build_seam_span(
    contexts: SegmentContexts,
    *,
    thread_id: str,
    turn_id: str,
    segment_seq: int,
    seam: str,
    started_at: datetime,
    ended_at: datetime,
    attributes: Mapping[str, str | bool | int] | None = None,
) -> ReadableSpan:
    """One execution-seam span, parented to its segment root."""
    return ReadableSpan(
        name=seam,
        context=_context(
            contexts.trace_id,
            seam_span_id_for(thread_id, turn_id, segment_seq, seam),
        ),
        parent=contexts.segment_root,
        resource=Resource.create({"service.name": SERVICE_NAME}),
        attributes={
            "fin.thread_id": thread_id,
            "fin.turn_id": turn_id,
            "fin.segment_seq": segment_seq,
            **(attributes or {}),
        },
        kind=SpanKind.INTERNAL,
        start_time=_time_ns(started_at),
        end_time=_time_ns(ended_at),
    )


# ----- the exporter seam -----


@dataclass(frozen=True)
class _Answer(BaseHTTPResult):
    """One HTTP outcome plus the bytes the collector answered with."""

    body: bytes = b""
    reply_headers: Mapping[str, str] = field(default_factory=dict)

    def content(self) -> bytes:
        return self.body

    def headers(self) -> Mapping[str, str]:
        return self.reply_headers


class _BoundedTransport(BaseHTTPTransport):
    """A TOTAL request bound, an abort, and no retries of its own.

    WHY this stands in for the SDK's default transport: that one hands
    ``requests`` a socket-level timeout, which a peer dribbling bytes
    outruns, and neither ``Session.close()`` nor ``exporter.shutdown()``
    interrupts a read already in flight. A drain that cannot interrupt a
    blocked export is not a drain, and this one is capped at two
    seconds, so both properties are established here rather than
    assumed: a watchdog fires at the total bound, and ``close()`` takes
    the same path.

    WHY :meth:`is_connection_error` answers ``False``: the SDK retries
    what it is told is a connection error, and the export worker's three
    attempts with its own waits are the only retry accounting this
    contract permits. A hidden retry would send the same immutable
    payload more than three times and count as one attempt.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._socket: socket.socket | None = None
        self._aborted = False
        self._budget = 0.0
        self.close_error_count = 0
        self.answer: _Answer | None = None

    def budget(self, seconds: float) -> None:
        """The total bound for the next request, in seconds."""
        with self._lock:
            self._budget = max(seconds, 0.0)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        data: bytes | None = None,
    ) -> BaseHTTPResult:
        # The SDK hands down its own remaining-time estimate; the
        # caller's bound is the tighter of the two and the one that
        # answers the drain.
        with self._lock:
            budget = min(self._budget, float(timeout or 0.0) or self._budget)
            aborted = self._aborted
        if budget <= 0:
            return _Answer(error=TimeoutError("no request time remains"))
        if aborted:
            return _Answer(error=ConnectionError("OTLP transport is closed"))
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.scheme not in ("http", "https"):
            return _Answer(error=ValueError(f"unsupported collector URL {url!r}"))
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection: http.client.HTTPConnection = (
            http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port,
                timeout=budget,
                context=ssl.create_default_context(),
            )
            if parsed.scheme == "https"
            else http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=budget
            )
        )
        with self._lock:
            if self._aborted:
                return _Answer(error=ConnectionError("OTLP transport is closed"))
            self._connection = connection
            # Per request, so a stale answer cannot classify a request
            # that never reached the collector.
            self.answer = None
        # Armed once per request rather than per socket operation, so a
        # peer that keeps sending bytes cannot push the deadline out.
        watchdog = threading.Timer(budget, self._break_socket)
        watchdog.daemon = True
        watchdog.start()
        try:
            connection.request(method, target, body=data, headers=headers or {})
            # ``http.client`` owns a socket only once ``connect()`` has
            # run (inside ``request``), and it drops its reference again
            # as soon as the response head is parsed. The read that can
            # hang is the body after that, so the socket is held here,
            # across the whole exchange, for an abort to break.
            with self._lock:
                self._socket = getattr(connection, "sock", None)
            response = connection.getresponse()
            body = response.read()
            answer = _Answer(
                status_code=response.status,
                reason=response.reason,
                body=body,
                reply_headers={k.lower(): v for k, v in response.getheaders()},
            )
            self.answer = answer
            return answer
        except Exception as exc:
            return _Answer(error=exc)
        finally:
            watchdog.cancel()
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                    self._socket = None
            connection.close()

    def close(self) -> None:
        with self._lock:
            self._aborted = True
        self._break_socket()

    def _break_socket(self) -> None:
        """Take down the socket of the request in flight.

        ``shutdown`` rather than ``close``: a closed socket is not
        enough, because a blocked ``recv`` can outlast the close.
        """
        with self._lock:
            sock = self._socket
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            # EXIT: a socket already closed or never connected raises here and
            # there is nothing further to tear down; the export is being
            # aborted either way, so this cannot leave a live handle behind.
            self.close_error_count += 1

    def is_connection_error(self, exception: Exception | None) -> bool:
        return False


def _rejected_spans(body: bytes) -> int:
    """How many spans the collector said it rejected, per OTLP.

    The SDK reads the status code and drops the body, so a partial
    success would otherwise be acknowledged as a delivered trace. A body
    this build cannot parse is not a rejection claim: the status code
    already decided that outcome.
    """
    if not body:
        return 0
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceResponse,
        )

        response = ExportTraceServiceResponse()
        response.ParseFromString(body)
        partial = response.partial_success
    except Exception:
        return 0
    return int(partial.rejected_spans)


class OTLPHTTPTransport:
    """Export immutable spans synchronously, one captured batch at a time.

    The seam the export worker drives: ``send`` reports whether the
    collector fully acknowledged the batch, and ``close_active`` is what
    a shutdown calls to stop waiting on one.
    """

    def __init__(self, config: ReceiptExportConfig) -> None:
        if config.exporter != "otlp":
            raise ReceiptExportConfigError(
                f"the {config.exporter!r} exporter has no request transport"
            )
        self._config = config
        self._lock = threading.Lock()
        self._transport = _BoundedTransport()
        self._exporter = OTLPSpanExporter(_transport=self._transport)
        self._closed = False

    def send(self, spans: Sequence[ReadableSpan], *, timeout: float) -> None:
        """Send one captured batch and say whether it was acknowledged.

        :raises ReceiptExportResponseError: the collector answered, but
            redirected or rejected part of the batch, so the revision
            stays pending.
        :raises ReceiptExportTransportError: no acknowledgement arrived
            at all.
        """
        if not spans:
            raise ReceiptExportTransportError("no spans to export")
        if timeout <= 0:
            raise ReceiptExportTransportError("OTLP request timeout expired")
        with self._lock:
            if self._closed:
                raise ReceiptExportTransportError("OTLP transport is closed")
        self._transport.budget(timeout)
        result = self._exporter.export(list(spans))
        answer = self._transport.answer
        if answer is None or answer.status_code is None:
            raise ReceiptExportTransportError("OTLP export was not acknowledged")
        # A redirect is not a delivery. The SDK treats any 2xx-3xx as
        # success, so the status is judged here as well.
        if not 200 <= answer.status_code < 300:
            raise ReceiptExportResponseError(
                "OTLP endpoint returned a non-success status"
            )
        if result is not SpanExportResult.SUCCESS:
            raise ReceiptExportTransportError("OTLP export was not acknowledged")
        if _rejected_spans(answer.body):
            raise ReceiptExportResponseError("OTLP endpoint reported partial success")

    def close_active(self) -> None:
        """Stop accepting requests and interrupt the one in flight."""
        with self._lock:
            self._closed = True
        self._transport.close()


class ConsoleSpanTransport:
    """Write span batches through the SDK console exporter."""

    def __init__(self) -> None:
        self._exporter = ConsoleSpanExporter(out=sys.stdout)

    def send(self, spans: Sequence[ReadableSpan], *, timeout: float) -> None:
        del timeout
        result = self._exporter.export(list(spans))
        if result is not SpanExportResult.SUCCESS:
            raise ReceiptExportTransportError("console span export failed")

    def close_active(self) -> None:
        self._exporter.shutdown()
