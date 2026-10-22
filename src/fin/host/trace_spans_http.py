"""``GET /api/threads/{thread}/turns/{turn}/spans`` — one turn's trace spans.

The trace layer is spans, and the ``pg`` exporter stores them in the same
PostgreSQL the host already reads. This route is the operator's read door:
it returns the spans of ONE turn, oldest first, for the developer
TaskPanel waterfall.

The payload is a normal 200 even when tracing was never enabled, because
"this turn has no spans" is an ordinary state, not a refusal. What the
view may claim is stated by ``trace_state`` rather than inferred from an
empty list, and every state has exactly one cause:

* ``off`` — the boot selected no exporter, so nothing is written;
* ``local`` — the boot selected the local ``pg`` sink and the read
  succeeded; an empty list then really is "this turn has no span";
* ``external`` — the boot selected ``console`` or ``otlp``, so tracing is
  on but its spans are not written where this view reads;
* ``unreadable`` — the local sink is selected and the store could not be
  read, so nothing is claimed about this turn's spans.

The exporter selection is the config the application boot resolved ONCE
(``ApplicationResources``) and injected here: this module never reads
``OTEL_TRACES_EXPORTER`` itself, because two readers of one setting can
disagree with the worker that is actually running.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends, FastAPI, Request

from fin.host.receipt_export import ReceiptExportConfig, ReceiptExportConfigError
from fin.host.trace_span_store import TraceSpanStoreError, list_turn_spans

#: The exporter token that writes spans to the local PostgreSQL store.
TRACE_BACKEND_TOKEN: Final[str] = "pg"

#: The exporters whose spans leave the process instead of landing in the
#: local table. Both are "tracing is on" and neither is readable here.
REMOTE_EXPORTERS: Final[frozenset[str]] = frozenset({"console", "otlp"})

#: The four facts the view can state about one turn's trace.
TraceState = Literal["off", "local", "external", "unreadable"]

TRACE_STATE_OFF: Final[TraceState] = "off"
TRACE_STATE_LOCAL: Final[TraceState] = "local"
TRACE_STATE_EXTERNAL: Final[TraceState] = "external"
TRACE_STATE_UNREADABLE: Final[TraceState] = "unreadable"

#: The read seam this route calls.
SpanReader = Callable[..., list[dict[str, Any]]]

#: Bounded so one turn cannot flood the waterfall (and the response).
TRACE_SPAN_READ_LIMIT: Final[int] = 200


def trace_state(config: ReceiptExportConfig | None) -> TraceState:
    """What the view may claim, from the one boot-resolved selection.

    :raises ReceiptExportConfigError: the selection names an exporter
        this build does not know. Folding that into a state would make a
        misconfigured boot read as a normal one.
    """
    if config is None:
        return TRACE_STATE_OFF
    if config.exporter == TRACE_BACKEND_TOKEN:
        return TRACE_STATE_LOCAL
    if config.exporter in REMOTE_EXPORTERS:
        return TRACE_STATE_EXTERNAL
    raise ReceiptExportConfigError(
        f"the {config.exporter!r} exporter has no view state"
    )


def get_turn_span_reader() -> SpanReader:
    """The dependency tests override to keep this view hermetic."""
    return list_turn_spans


def get_receipt_export_config(request: Request) -> ReceiptExportConfig | None:
    """The exporter selection the application boot resolved, if any.

    Read off the application's own resources rather than the environment:
    the worker that is running and the view that reports on it must answer
    from ONE resolved value.
    """
    resources = getattr(request.app.state, "fin_resources", None)
    return getattr(resources, "receipt_export_config", None)


def _payload(
    thread_id: str, turn_id: str, state: TraceState, spans: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "trace_state": state,
        "spans": spans,
    }


def build_router() -> APIRouter:
    """The span view over the reader and the boot-resolved selection."""
    router = APIRouter(prefix="/api")

    @router.get("/threads/{thread_id}/turns/{turn_id}/spans")
    def turn_spans(
        thread_id: str,
        turn_id: str,
        span_reader: SpanReader = Depends(get_turn_span_reader),
        config: ReceiptExportConfig | None = Depends(get_receipt_export_config),
    ) -> dict[str, Any]:
        """This turn's spans, the state that explains them, and no error."""
        state = trace_state(config)
        if state != TRACE_STATE_LOCAL or config is None:
            # EXIT: no local sink is selected, so this door has nothing to
            # read. The state says why the list is empty.
            return _payload(thread_id, turn_id, state, [])
        try:
            spans = span_reader(
                thread_id,
                turn_id,
                dsn=config.span_dsn,
                limit=TRACE_SPAN_READ_LIMIT,
            )
        except TraceSpanStoreError:
            # EXIT: the sink is selected but the read failed. The panel
            # renders the receipt fallback under "unreadable" rather than
            # claiming the turn produced no spans.
            return _payload(thread_id, turn_id, TRACE_STATE_UNREADABLE, [])
        return _payload(thread_id, turn_id, state, spans)

    return router


def mount(app: FastAPI) -> None:
    """Register the span view before the SPA catch-all is mounted."""
    app.include_router(build_router())


__all__ = [
    "REMOTE_EXPORTERS",
    "TRACE_BACKEND_TOKEN",
    "TRACE_SPAN_READ_LIMIT",
    "TRACE_STATE_EXTERNAL",
    "TRACE_STATE_LOCAL",
    "TRACE_STATE_OFF",
    "TRACE_STATE_UNREADABLE",
    "TraceState",
    "build_router",
    "get_receipt_export_config",
    "get_turn_span_reader",
    "mount",
    "trace_state",
]
