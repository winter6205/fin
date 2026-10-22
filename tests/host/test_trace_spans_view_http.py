"""The developer span view: one turn's trace, its state, and no refusal.

The trace layer is spans, so the TaskPanel needs a read door that answers
for ONE turn. It is a normal 200 even when tracing was never enabled: a
turn without spans is the ordinary state the receipt fallback exists for,
not a refusal.

The response states WHY a list is empty instead of letting the panel
guess. Four states, four causes:

* ``off`` — the boot selected no exporter;
* ``local`` — the boot selected the local ``pg`` sink and the read worked;
* ``external`` — the boot selected ``console`` or ``otlp``, so tracing is
  on but nothing is written where this view reads;
* ``unreadable`` — the sink is selected and the store could not be read.

The exporter selection is a DEPENDENCY here, fed by the application boot:
the environment is never read by this route, so a case that sets
``OTEL_TRACES_EXPORTER`` proves the route ignores it.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.host.receipt_export import ReceiptExportConfig, ReceiptExportConfigError
from fin.host.trace_span_store import TraceSpanStoreError
from fin.host.trace_spans_http import (
    TRACE_BACKEND_TOKEN,
    TRACE_SPAN_READ_LIMIT,
    TRACE_STATE_EXTERNAL,
    TRACE_STATE_LOCAL,
    TRACE_STATE_OFF,
    TRACE_STATE_UNREADABLE,
    get_receipt_export_config,
    get_turn_span_reader,
    trace_state,
)

THREAD_ID = "thread-1"
TURN_ID = "turn-1"
EXPORTER_ENV = "OTEL_TRACES_EXPORTER"
SPAN_DSN_ENV = "FIN_TRACE_SPAN_DSN"
SPAN_DSN = "postgresql://fin@127.0.0.1:1/fin_trace_span_test"


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture(autouse=True)
def _no_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambient shell cannot decide these cases."""
    monkeypatch.delenv(EXPORTER_ENV, raising=False)
    monkeypatch.delenv(SPAN_DSN_ENV, raising=False)


def _span_row(name: str = "retrieve_tool") -> dict[str, Any]:
    """One row in the shape ``trace_span_store._span_payload`` publishes.

    The keys and their types are the store's own: an integer ``id``, ISO
    8601 instants including ``end_time`` and ``created_at``, a float
    duration and decoded attributes. The route must pass rows through
    verbatim, so a fixture that renamed or retyped one would hide a wire
    break.
    """
    return {
        "id": 41,
        "trace_id": "0" * 31 + "1",
        "span_id": "0" * 15 + "1",
        "parent_span_id": None,
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "name": name,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:00.012500+00:00",
        "duration_ms": 12.5,
        "attributes": {"fin.thread_id": THREAD_ID, "fin.turn_id": TURN_ID},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _pg_config() -> ReceiptExportConfig:
    return ReceiptExportConfig(TRACE_BACKEND_TOKEN, SPAN_DSN)


def _select(
    http_app: FastAPI, config: ReceiptExportConfig | None
) -> None:
    """Pin the exporter selection the boot would have resolved."""
    http_app.dependency_overrides[get_receipt_export_config] = lambda: config


def _read_spans(
    http_app: FastAPI, rows: list[dict[str, Any]]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Override the reader and record every call it receives."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def reader(thread_id: str, turn_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append((thread_id, turn_id, kwargs))
        return rows

    http_app.dependency_overrides[get_turn_span_reader] = lambda: reader
    return calls


def _failing_reader(http_app: FastAPI) -> None:
    def reader(thread_id: str, turn_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise TraceSpanStoreError("the trace span store could not be read")

    http_app.dependency_overrides[get_turn_span_reader] = lambda: reader


def _url(thread_id: str = THREAD_ID, turn_id: str = TURN_ID) -> str:
    return f"/api/threads/{thread_id}/turns/{turn_id}/spans"


def test_the_view_returns_the_turn_s_spans_and_the_local_state(
    client: TestClient,
    http_app: FastAPI,
) -> None:
    rows = [_span_row("classify_node"), _span_row("model_node")]
    calls = _read_spans(http_app, rows)
    _select(http_app, _pg_config())

    response = client.get(_url())

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "trace_state": TRACE_STATE_LOCAL,
        "spans": rows,
    }
    # The read is driven by the boot-resolved selection, DSN included: the
    # store never has to re-read ``FIN_TRACE_SPAN_DSN`` behind this route.
    assert calls == [
        (THREAD_ID, TURN_ID, {"dsn": SPAN_DSN, "limit": TRACE_SPAN_READ_LIMIT}),
    ]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, TRACE_STATE_OFF),
        (ReceiptExportConfig("console"), TRACE_STATE_EXTERNAL),
        (ReceiptExportConfig("otlp"), TRACE_STATE_EXTERNAL),
        (ReceiptExportConfig("pg", SPAN_DSN), TRACE_STATE_LOCAL),
    ],
)
def test_trace_state_names_the_selected_exporter(
    config: ReceiptExportConfig | None, expected: str
) -> None:
    assert trace_state(config) == expected


@pytest.mark.parametrize("exporter", ["none", "jaeger", ""])
def test_an_unknown_exporter_is_refused_rather_than_named_a_state(
    exporter: str,
) -> None:
    with pytest.raises(ReceiptExportConfigError):
        trace_state(ReceiptExportConfig(exporter))


@pytest.mark.parametrize(
    ("config", "expected_state"),
    [
        (None, TRACE_STATE_OFF),
        (ReceiptExportConfig("console"), TRACE_STATE_EXTERNAL),
        (ReceiptExportConfig("otlp"), TRACE_STATE_EXTERNAL),
    ],
)
def test_an_empty_trace_names_why_it_is_empty_and_reads_no_store(
    client: TestClient,
    http_app: FastAPI,
    config: ReceiptExportConfig | None,
    expected_state: str,
) -> None:
    calls = _read_spans(http_app, [])
    _select(http_app, config)

    response = client.get(_url())

    assert response.status_code == 200
    body = response.json()
    assert body["spans"] == []
    assert body["trace_state"] == expected_state
    assert "error" not in body
    # No local sink is selected, so the route opens no read at all.
    assert calls == []


def test_a_local_sink_with_no_span_is_still_a_200(
    client: TestClient, http_app: FastAPI
) -> None:
    _read_spans(http_app, [])
    _select(http_app, _pg_config())

    body = client.get(_url()).json()

    assert body["trace_state"] == TRACE_STATE_LOCAL
    assert body["spans"] == []
    assert "error" not in body


def test_an_unreadable_store_is_its_own_state_not_an_empty_trace(
    client: TestClient, http_app: FastAPI
) -> None:
    """A store fault must never read as "this turn produced no span"."""
    _failing_reader(http_app)
    _select(http_app, _pg_config())

    response = client.get(_url())

    assert response.status_code == 200
    body = response.json()
    assert body["trace_state"] == TRACE_STATE_UNREADABLE
    assert body["spans"] == []
    assert "error" not in body


def test_the_view_keeps_the_store_s_rows_verbatim(
    client: TestClient,
    http_app: FastAPI,
) -> None:
    """A row's own facts — including its attributes — are not rewritten."""
    row = _span_row("citation_gate_node")
    row["attributes"] = {"fin.citation.verified": True, "fin.segment_seq": 0}
    _read_spans(http_app, [row])
    _select(http_app, _pg_config())

    body = client.get(_url()).json()

    assert body["spans"] == [row]
    assert body["spans"][0]["attributes"] == row["attributes"]
    assert body["spans"][0]["id"] == 41


def test_the_route_never_reads_the_exporter_environment(
    client: TestClient,
    http_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One configuration authority: the boot's resolved selection.

    The environment says ``pg`` in one case and ``none`` in the other, and
    the answer follows the injected selection both times. A route that
    re-read ``OTEL_TRACES_EXPORTER`` would report the opposite.
    """
    calls = _read_spans(http_app, [])
    monkeypatch.setenv(EXPORTER_ENV, TRACE_BACKEND_TOKEN)
    _select(http_app, None)

    assert client.get(_url()).json()["trace_state"] == TRACE_STATE_OFF
    assert calls == []

    monkeypatch.setenv(EXPORTER_ENV, "none")
    _select(http_app, _pg_config())

    assert client.get(_url()).json()["trace_state"] == TRACE_STATE_LOCAL
    assert len(calls) == 1


def test_the_route_reads_the_selection_the_application_boot_published(
    client: TestClient,
    http_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default dependency is the application's own boot-resolved value."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        http_app.state,
        "fin_resources",
        SimpleNamespace(
            receipt_export_config=ReceiptExportConfig(TRACE_BACKEND_TOKEN, SPAN_DSN)
        ),
        raising=False,
    )
    calls = _read_spans(http_app, [_span_row()])

    body = client.get(_url()).json()

    assert body["trace_state"] == TRACE_STATE_LOCAL
    assert calls == [
        (THREAD_ID, TURN_ID, {"dsn": SPAN_DSN, "limit": TRACE_SPAN_READ_LIMIT}),
    ]


def test_the_default_reader_is_the_store_seam_and_unbooted_is_off(
    client: TestClient,
) -> None:
    """No override: the route calls ``trace_span_store.list_turn_spans``.

    An application that never booted resolved no exporter, and with no
    local sink selected the view reports ``off`` without reading.
    """
    response = client.get(_url("unconfigured-thread", "unconfigured-turn"))

    assert response.status_code == 200
    body = response.json()
    assert body["spans"] == []
    assert body["trace_state"] == TRACE_STATE_OFF
    assert "error" not in body


def test_the_view_is_not_a_thread_scoped_route(client: TestClient) -> None:
    """The turn is part of the address; there is no whole-thread trace."""
    _read_spans(client.app, [])  # type: ignore[arg-type]
    response = client.get(f"/api/threads/{THREAD_ID}/spans")

    assert response.status_code == 404
