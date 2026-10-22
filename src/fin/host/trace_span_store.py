"""The local ``fin_trace_span`` table: fin's own span sink and read seam.

WHY a table rather than only an OTLP endpoint: a local operator should be
able to read a turn's trace without standing up a collector, and the
trace view the console renders reads exactly this table. It is the third
``OTEL_TRACES_EXPORTER`` choice — ``pg`` — and it is OFF unless that
token is selected together with the database it writes to.

WHAT is stored: one row per span — its identifiers, its parent, the turn
it belongs to, its name, its instants, its duration and the allowlisted
attributes. Nothing else. ``allowlisted_span_attributes`` applies the
same closed name set the exporters already send, so the ``attributes``
column can never become a copy of whatever a caller had to hand: no
prompt, no answer, no excerpt, no source identity, no exception text,
no connection string.

WHEN the schema appears: on the first successful export, not at boot. A
store that is unreachable at boot must not fail the boot, and a process
that never selected ``pg`` must never create the table. The DDL is
idempotent (``CREATE ... IF NOT EXISTS`` throughout), which is how this
repo manages host-side SQL: inline DDL executed by the store that owns
the table, with an ``ALTER`` beside it when a column has to reach an
already-created installation. A brand-new table needs only the
``CREATE``; the dedupe index is stated separately so it also reaches a
table an earlier build created.

RETENTION: ``FIN_TRACE_SPAN_RETENTION_DAYS`` bounds the table by
``created_at``; the default is 14 days and ``0`` disables pruning. The
export worker prunes at most once an hour, in its own thread, and a
prune that fails is logged and retried on the next window — it never
turns a delivered batch into a failure and never reaches a turn.

The read seam (:func:`list_turn_spans`) answers ``[]`` when no DSN is
configured or the table does not exist, because "trace disabled" is a
normal state and not an error for the caller rendering a turn. Every
OTHER store fault raises :class:`TraceSpanStoreError`: an unreachable or
refusing database is not the same fact as "this turn has no spans", and
a reader that reported the two alike would let the developer view claim
tracing was on while showing nothing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Iterator
import psycopg
from opentelemetry.sdk.trace import ReadableSpan
from psycopg.types.json import Jsonb

from fin.host.receipt_export import (
    RECEIPT_SPAN_ATTRIBUTE_NAMES,
    TRACE_SPAN_DSN_ENV,
    ReceiptExportConfig,
    ReceiptExportConfigError,
    ReceiptExportTransportError,
)
from fin.host.span_seams import SEAM_SPAN_ATTRIBUTES

logger = logging.getLogger(__name__)

#: The one table this module owns.
TRACE_SPAN_TABLE: Final[str] = "fin_trace_span"

#: How long a span row is kept. Conservative on purpose: the trace layer
#: is diagnostics, and an unbounded diagnostic table turns a local
#: deployment into a storage problem.
TRACE_SPAN_RETENTION_ENV: Final[str] = "FIN_TRACE_SPAN_RETENTION_DAYS"
DEFAULT_TRACE_SPAN_RETENTION_DAYS: Final[int] = 14

#: A read seam never streams an unbounded turn; a turn's trace is a
#: handful of spans and the console draws all of them.
MAX_LIST_LIMIT: Final[int] = 1000

#: The operator read's own bound. It is the same budget the export path
#: uses, and it is stated because the view answers a request: a store
#: that cannot answer within it is reported as unreadable rather than
#: holding the request open.
TRACE_SPAN_READ_TIMEOUT_SECONDS: Final[float] = 2.0

#: Pruning is maintenance, not export: once an hour is plenty for a
#: 14-day window and keeps the sender from running a DELETE per batch.
PRUNE_INTERVAL_SECONDS: Final[float] = 3600.0

#: The three correlation names every span carries, plus the two closed
#: owners of everything else: a seam span's names and a receipt
#: projection's names. The union is the whole vocabulary the table
#: accepts.
SPAN_ATTRIBUTE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "fin.thread_id",
        "fin.turn_id",
        "fin.segment_seq",
        *SEAM_SPAN_ATTRIBUTES,
        *RECEIPT_SPAN_ATTRIBUTE_NAMES,
    }
)

#: The row columns in read order. ``id`` and ``created_at`` are the
#: database's; every other value comes from the span.
_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "thread_id",
    "turn_id",
    "name",
    "start_time",
    "end_time",
    "duration_ms",
    "attributes",
    "created_at",
)

#: The columns an export writes. ``id`` and ``created_at`` are left to
#: the table so a retried export carries the same values the first one
#: would have.
_INSERT_COLUMNS: Final[tuple[str, ...]] = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "thread_id",
    "turn_id",
    "name",
    "start_time",
    "end_time",
    "duration_ms",
    "attributes",
)

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)

_TRACE_SPAN_SCHEMA_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TRACE_SPAN_TABLE} (
    id             BIGSERIAL PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT,
    thread_id      TEXT NOT NULL,
    turn_id        TEXT NOT NULL,
    name           TEXT NOT NULL,
    start_time     TIMESTAMPTZ NOT NULL,
    end_time       TIMESTAMPTZ NOT NULL,
    duration_ms    DOUBLE PRECISION NOT NULL,
    attributes     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One row per span. The export worker retries a batch it could not
-- acknowledge, so the same span arrives more than once by design; the
-- unique index is what makes that retry a no-op instead of a duplicate.
-- Stated as its own statement so it also reaches a table an earlier
-- build created.
CREATE UNIQUE INDEX IF NOT EXISTS fin_trace_span_identity_idx
    ON {TRACE_SPAN_TABLE} (trace_id, span_id);
CREATE INDEX IF NOT EXISTS fin_trace_span_turn_idx
    ON {TRACE_SPAN_TABLE} (thread_id, turn_id, start_time);
CREATE INDEX IF NOT EXISTS fin_trace_span_created_at_idx
    ON {TRACE_SPAN_TABLE} (created_at);
"""

#: The export worker retries, so a conflict is the ordinary shape of a
#: second delivery rather than an error. ``DO NOTHING`` rather than an
#: upsert on purpose: a span id is a function of (thread_id, turn_id,
#: segment_seq, name), so every receipt revision of one segment shares
#: its projection's id. The stored row is therefore the FIRST delivered
#: revision, and the receipt layer — not this table — stays the
#: authority on a turn's current state. An out-of-order late delivery
#: can then never rewrite evidence the table already holds.
_INSERT_SQL: Final[str] = (
    "INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
    "ON CONFLICT (trace_id, span_id) DO NOTHING"
).format(
    table=TRACE_SPAN_TABLE,
    columns=", ".join(_INSERT_COLUMNS),
    placeholders=", ".join(["%s"] * len(_INSERT_COLUMNS)),
)


class TraceSpanStoreError(Exception):
    """The span table could not be read or written as requested."""


def _select_sql() -> str:
    """The read statement, built per call so the table name stays one
    constant (a test substitutes a missing table to prove the seam
    answers ``[]`` rather than raising)."""
    return (
        f"SELECT {', '.join(_COLUMNS)} FROM {TRACE_SPAN_TABLE} "
        "WHERE thread_id = %s AND turn_id = %s "
        "ORDER BY start_time, id LIMIT %s"
    )


def _prune_sql() -> str:
    return (
        f"DELETE FROM {TRACE_SPAN_TABLE} "
        "WHERE created_at < now() - make_interval(days => %s)"
    )


@contextmanager
def _connection(dsn: str, timeout: float) -> Iterator[Any]:
    """One short-lived connection with a bounded connect and statement.

    Both bounds are derived from the caller's export timeout, so a
    wedged database cannot hold the export worker past the budget the
    shutdown drain is sized against. The ``options`` override belongs to
    a dedicated diagnostics DSN; nothing else is read from it.
    """
    connect_seconds = max(1, int(round(timeout)))
    statement_ms = max(1, int(round(timeout * 1000)))
    with psycopg.connect(
        dsn,
        connect_timeout=connect_seconds,
        options=f"-c statement_timeout={statement_ms}",
    ) as conn:
        yield conn


# ----- the row -----


def allowlisted_span_attributes(
    attributes: Mapping[str, Any],
) -> dict[str, str | bool | int | float]:
    """Only the contract's names, and only JSON scalars.

    The transport is handed spans the two allowlist owners already
    built, so this is a second lock rather than the first. It is what
    makes the table's ``attributes`` column a closed shape: a span that
    somehow carried a prompt, an answer or a source id loses it here,
    and a structured value (a list, a mapping) has no path into a
    scalar-only contract at all.
    """
    allowed: dict[str, str | bool | int | float] = {}
    for name, value in attributes.items():
        if name not in SPAN_ATTRIBUTE_ALLOWLIST:
            continue
        # ``bool`` is an ``int`` subclass, so this admits the three
        # scalar shapes the contract names and refuses every container.
        if isinstance(value, (str, int, float)):
            allowed[name] = value
    return allowed


def _instant(nanoseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=nanoseconds // 1000)


def span_row(span: ReadableSpan) -> dict[str, Any]:
    """One span as its table row, with the allowlist applied.

    :raises TraceSpanStoreError: the span carries no turn correlation or
        no instants, so it cannot be addressed or ordered.
    """
    attributes = allowlisted_span_attributes(span.attributes or {})
    thread_id = attributes.get("fin.thread_id")
    turn_id = attributes.get("fin.turn_id")
    if not isinstance(thread_id, str) or not isinstance(turn_id, str):
        raise TraceSpanStoreError("a persisted span must carry its turn correlation")
    if span.start_time is None or span.end_time is None:
        raise TraceSpanStoreError("a persisted span must carry its instants")
    context = span.context
    if context is None:
        raise TraceSpanStoreError("a persisted span must carry its context")
    parent = span.parent
    return {
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "name": span.name,
        "start_time": _instant(span.start_time),
        "end_time": _instant(span.end_time),
        "duration_ms": (span.end_time - span.start_time) / 1_000_000,
        "attributes": attributes,
    }


def _json_cell(value: object) -> dict[str, Any]:
    """A JSONB column reads back as a decoded object or as its text."""
    if value is None:
        return {}
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _span_payload(row: Sequence[Any]) -> dict[str, Any]:
    """One row of the agreed schema, JSON-ready for its reader.

    Instants become ISO-8601 strings because the consumer is a JSON
    reader, which is the same projection the receipt view already
    publishes.
    """
    payload: dict[str, Any] = dict(zip(_COLUMNS, row))
    payload["attributes"] = _json_cell(payload["attributes"])
    for name in ("start_time", "end_time", "created_at"):
        value = payload[name]
        if isinstance(value, datetime):
            payload[name] = value.isoformat()
    payload["duration_ms"] = float(payload["duration_ms"])
    return payload


# ----- the store -----


def ensure_trace_span_schema(dsn: str, *, timeout: float = 2.0) -> None:
    """Create the table and its indexes if this database lacks them.

    Idempotent, so the export worker may call it on the first write of a
    process without coordinating with anyone.
    """
    with _connection(dsn, timeout) as conn:
        conn.execute(_TRACE_SPAN_SCHEMA_SQL)
        conn.commit()


def record_spans(
    dsn: str, spans: Sequence[ReadableSpan], *, timeout: float = 2.0
) -> None:
    """Persist one span batch; a span already stored is a no-op.

    :raises TraceSpanStoreError: no span in the batch could be addressed
        as a row, so nothing was written.
    :raises psycopg.Error: the store could not be reached or refused the
        batch. The caller maps that to its own export failure.
    """
    rows: list[dict[str, Any]] = []
    for span in spans:
        try:
            rows.append(span_row(span))
        except TraceSpanStoreError as exc:
            logger.warning("dropping a span that cannot be stored: %s", exc)
    if not rows:
        raise TraceSpanStoreError("no persistable span in the batch")
    parameters = [
        tuple(
            Jsonb(row[column]) if column == "attributes" else row[column]
            for column in _INSERT_COLUMNS
        )
        for row in rows
    ]
    with _connection(dsn, timeout) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(_INSERT_SQL, parameters)
        conn.commit()


def prune_spans(dsn: str, *, retention_days: int, timeout: float = 2.0) -> int:
    """Delete rows older than ``retention_days`` and report how many.

    ``retention_days <= 0`` keeps everything and deletes nothing.
    """
    if retention_days <= 0:
        return 0
    with _connection(dsn, timeout) as conn:
        cursor = conn.execute(_prune_sql(), (retention_days,))
        deleted = cursor.rowcount
        conn.commit()
    return max(deleted, 0)


def retention_days_from_env(environ: Mapping[str, str] | None = None) -> int:
    """The configured retention, or the conservative default.

    A value that is not a non-negative integer keeps the default rather
    than guessing: retention is maintenance, and the one thing it must
    not do on a bad value is widen itself without bound.
    """
    values = environ if environ is not None else os.environ
    raw = (values.get(TRACE_SPAN_RETENTION_ENV) or "").strip()
    if not raw:
        return DEFAULT_TRACE_SPAN_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "%s is not an integer; keeping %d days",
            TRACE_SPAN_RETENTION_ENV,
            DEFAULT_TRACE_SPAN_RETENTION_DAYS,
        )
        return DEFAULT_TRACE_SPAN_RETENTION_DAYS
    if days < 0:
        logger.warning(
            "%s must not be negative; keeping %d days",
            TRACE_SPAN_RETENTION_ENV,
            DEFAULT_TRACE_SPAN_RETENTION_DAYS,
        )
        return DEFAULT_TRACE_SPAN_RETENTION_DAYS
    return days


def list_turn_spans(
    thread_id: str, turn_id: str, *, dsn: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """One turn's spans, ordered by ``start_time``.

    The seam the trace view reads. Every row is the agreed schema with
    its ``attributes`` decoded and its instants rendered as ISO-8601
    strings.

    ``dsn`` defaults to ``FIN_TRACE_SPAN_DSN``. With neither — or with
    no table yet, because the exporter never ran — the answer is an
    empty list: a disabled trace layer is a normal turn, not an error.
    A store that is configured but cannot be read is a DIFFERENT fact:
    it raises (:class:`TraceSpanStoreError`) rather than claiming the
    turn has no spans, so the view can say the state is unknown.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    target = (dsn if dsn is not None else os.environ.get(TRACE_SPAN_DSN_ENV, "")).strip()
    if not target:
        # EXIT: no store is configured, so "no spans" is the whole truth
        # and there is nothing to read.
        return []
    try:
        with _connection(target, TRACE_SPAN_READ_TIMEOUT_SECONDS) as conn:
            rows = conn.execute(_select_sql(), (thread_id, turn_id, limit)).fetchall()
    except psycopg.errors.UndefinedTable:
        # EXIT: the exporter has never run against this database, so
        # there is no trace layer to read rather than a broken one.
        return []
    except psycopg.Error as exc:
        # Not an exit: the store is named and may hold this turn's rows,
        # so a fault answers "unknown", never "empty". The detail is the
        # exception's type only — a driver error can quote a statement or
        # a connection string.
        raise TraceSpanStoreError(
            f"the trace span store could not be read ({type(exc).__name__})"
        ) from exc
    return [_span_payload(row) for row in rows]


# ----- the exporter transport -----


class PostgresSpanTransport:
    """Persist span batches to the local ``fin_trace_span`` table.

    The ``pg`` choice's transport, driven by the same bounded worker the
    other two use. A failure is typed and raised, which is exactly what
    that worker already knows how to account for: it retries within its
    own bound, records the failure and moves on. Nothing here runs on
    the turn's thread, so no database fault can fail or slow a turn.

    The table is created by the first successful export rather than by
    construction, so a database that is down at boot does not fail the
    boot; a failed attempt forgets that the schema was ensured, so the
    next attempt repairs a database that was recreated underneath us.
    """

    def __init__(
        self,
        config: ReceiptExportConfig,
        *,
        retention_days: int | None = None,
        prune_interval: float = PRUNE_INTERVAL_SECONDS,
    ) -> None:
        if config.exporter != "pg":
            raise ReceiptExportConfigError(
                f"the {config.exporter!r} exporter has no local span store"
            )
        dsn = (config.span_dsn or "").strip()
        if not dsn:
            raise ReceiptExportConfigError(
                f"the 'pg' exporter needs {TRACE_SPAN_DSN_ENV}"
            )
        self._dsn = dsn
        self._retention_days = (
            retention_days_from_env() if retention_days is None else retention_days
        )
        self._prune_interval = prune_interval
        self._lock = threading.Lock()
        self._closed = False
        self._schema_ready = False
        self._last_prune: float | None = None

    def send(self, spans: Sequence[ReadableSpan], *, timeout: float) -> None:
        """Write one captured batch, then maintain the table.

        :raises ReceiptExportTransportError: no span could be written.
            The batch stays undelivered for the worker's retry budget.
        """
        if not spans:
            raise ReceiptExportTransportError("no spans to export")
        if timeout <= 0:
            raise ReceiptExportTransportError("span store timeout expired")
        with self._lock:
            if self._closed:
                raise ReceiptExportTransportError("span store transport is closed")
        try:
            if not self._schema_ready:
                ensure_trace_span_schema(self._dsn, timeout=timeout)
                self._schema_ready = True
            record_spans(self._dsn, spans, timeout=timeout)
        except (psycopg.Error, TraceSpanStoreError) as exc:
            # WHY the detail is the exception's type and nothing else:
            # a driver error can quote a statement or a connection and
            # the log is not the place for either.
            self._schema_ready = False
            logger.warning("trace span export failed: %s", type(exc).__name__)
            raise ReceiptExportTransportError("span store write failed") from exc
        self._prune_if_due(timeout)

    def close_active(self) -> None:
        """Stop accepting batches.

        There is no request in flight to interrupt: the connection, its
        statement and its connect are bounded by the export timeout, so
        the worker's drain is bounded without anything here reaching
        into a live connection.
        """
        with self._lock:
            self._closed = True

    def _prune_if_due(self, timeout: float) -> None:
        now = time.monotonic()
        if self._last_prune is not None and now - self._last_prune < self._prune_interval:
            return
        self._last_prune = now
        try:
            prune_spans(self._dsn, retention_days=self._retention_days, timeout=timeout)
        except (psycopg.Error, TraceSpanStoreError) as exc:
            # EXIT: retention is maintenance. A prune that failed leaves
            # rows to prune on the next window, and must never turn a
            # batch that was already written into a delivery failure.
            logger.warning("trace span retention prune failed: %s", type(exc).__name__)


__all__ = [
    "DEFAULT_TRACE_SPAN_RETENTION_DAYS",
    "MAX_LIST_LIMIT",
    "PostgresSpanTransport",
    "PRUNE_INTERVAL_SECONDS",
    "SPAN_ATTRIBUTE_ALLOWLIST",
    "TRACE_SPAN_READ_TIMEOUT_SECONDS",
    "TRACE_SPAN_RETENTION_ENV",
    "TRACE_SPAN_TABLE",
    "TraceSpanStoreError",
    "allowlisted_span_attributes",
    "ensure_trace_span_schema",
    "list_turn_spans",
    "prune_spans",
    "record_spans",
    "retention_days_from_env",
    "span_row",
]
