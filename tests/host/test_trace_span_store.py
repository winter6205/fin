"""The ``pg`` trace exporter, its table and the operator read seam.

The contract this file holds to (ADR 0102 amendment 2026-10-20):

* the exporter is OFF by default: with no selection a turn writes no
  span row, opens no connection and still completes;
* ``pg`` persists spans — structure, timing and allowlisted attributes
  only, never the raw question or answer;
* a retried export is idempotent, a database fault is the worker's
  diagnostics rather than the turn's, and retention prunes old rows;
* the read seam answers ``[]`` only for the genuine off states — no DSN,
  no table yet — and raises a typed error when a configured store cannot
  be read, so a view never reports a fault as "no spans".

Database cases read ``FIN_CHECKPOINT_URL`` (bridged from
``FIN_TEST_DSN`` by ``tests/conftest.py``) and skip when it is unset,
which is the repo's convention for durable suites. They write only
``fin_trace_span``, the table this feature owns.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

import fin.host.trace_span_store as trace_span_store
from fin.corpus.extracted import Locator
from fin.host.graph import compile_graph
from fin.host.receipt_export import (
    RECEIPT_SPAN_ATTRIBUTE_NAMES,
    SEAM_SPAN_NAMES,
    SUPPORTED_EXPORTERS,
    TRACE_SPAN_DSN_ENV,
    ReceiptExportConfig,
    ReceiptExportConfigError,
    ReceiptExportTransportError,
    build_receipt_span,
    build_seam_span,
    load_receipt_export_config,
    receipt_span_id_for,
    seam_span_id_for,
    segment_contexts,
    segment_span_id_for,
    trace_id_for,
)
from fin.host.receipt_export_worker import (
    ReceiptExportWorker,
    start_application_receipt_export_worker,
)
from fin.host.scripted_model import ScriptedChatModel
from fin.host.span_seams import (
    SEAM_SPAN_ATTRIBUTES,
    SEGMENT_ROOT_SPAN_NAME,
    current_segment,
    publish_turn_segment,
    seam_span,
)
from fin.host.thread_store import InMemoryThreadStore
from fin.host.trace_span_store import (
    DEFAULT_TRACE_SPAN_RETENTION_DAYS,
    SPAN_ATTRIBUTE_ALLOWLIST,
    TRACE_SPAN_READ_TIMEOUT_SECONDS,
    TRACE_SPAN_RETENTION_ENV,
    PostgresSpanTransport,
    TraceSpanStoreError,
    ensure_trace_span_schema,
    list_turn_spans,
    prune_spans,
    record_spans,
    retention_days_from_env,
    span_row,
)
from fin.host.turn_receipts import default_receipts
from fin.host.turn_receipt_store import TurnReceipt, zero_stage_metrics
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import Chunk

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

THREAD = "opaque-thread-id"
TURN = "opaque-turn-id"

#: Nothing listens on port 1, so a connect is refused immediately and no
#: network egress is involved. Used to pin "a database fault is not a
#: turn fault" without needing a database that is down.
UNREACHABLE_DSN = "postgresql://fin@127.0.0.1:1/fin_trace_span_test"

RAW_QUESTION = "what was AAPL net sales in 2023 Q3?"
RAW_ANSWER = "Per the filings: revenue grew."

#: The five seams ADR 0102 closes, spelled out rather than derived from
#: ``SEAM_SPAN_NAMES``: an assertion that restates this file's own loop
#: input pins nothing about the contract.
CONTRACT_SPAN_NAMES = (
    "classify_node",
    "retrieve_tool",
    "model_node",
    "citation_gate_node",
    "admit_decision",
)


# ----- fixtures and helpers -----


@pytest.fixture(name="dsn")
def _dsn() -> Iterator[str]:
    dsn = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the span table is untested in "
            "this environment"
        )
    import psycopg

    with psycopg.connect(dsn) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
    assert database.endswith("_test")
    yield dsn


def _receipt(**changes: Any) -> TurnReceipt:
    base = TurnReceipt(
        thread_id=THREAD,
        turn_id=TURN,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
        stage_metrics=zero_stage_metrics(),
    )
    return replace(base, **changes)


def _pg_config(dsn: str) -> ReceiptExportConfig:
    config = load_receipt_export_config(
        {"OTEL_TRACES_EXPORTER": "pg", TRACE_SPAN_DSN_ENV: dsn}
    )
    assert config is not None
    return config


def _identities() -> tuple[str, str]:
    return f"trace-thread-{uuid4().hex}", f"trace-turn-{uuid4().hex}"


#: The end-to-end case's turn: one grounded run over a scripted model and a
#: fake retrieval port, so no external provider is involved. The trace
#: assertion is what this case is about; the answer is the product's own.
KNOWN_DOCUMENT_IDS = {"AAPL-2022Q3"}
GROUNDED_ANSWER = "Per the filings: revenue grew."


def _scripted_graph() -> Any:
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue"},
                    "id": "c-span",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=GROUNDED_ANSWER),
    ]
    hit = Chunk(
        text="Apple net sales for fiscal 2023 third quarter grew.",
        source_ref=Locator(
            document_id="AAPL-2022Q3", version="a" * 64, unit=1
        ),
        score=1.0,
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_DOCUMENT_IDS,
        adapter=FakeRetrievePort(
            hits_by_query={"revenue": [hit]},
            known_document_ids=KNOWN_DOCUMENT_IDS,
        ),
    )


def _turn_input() -> dict[str, Any]:
    return {
        "messages": [HumanMessage("what was AAPL net sales in 2023 Q3?")],
        "evidence": [],
        "repair_count": 0,
        "step_count": 0,
    }


def _run_config(thread_id: str, turn_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "run_id": turn_id}}


def _forget(dsn: str, *thread_ids: str) -> None:
    """Delete only this suite's rows, after checking the table is there."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        present = conn.execute(
            "SELECT to_regclass('public.fin_trace_span') IS NOT NULL"
        ).fetchone()[0]
        if not present:
            return
        conn.execute(
            "DELETE FROM fin_trace_span WHERE thread_id = ANY(%s)",
            (list(thread_ids),),
        )


def _one_seam_span(
    thread_id: str,
    turn_id: str,
    *,
    seam: str,
    attributes: dict[str, Any] | None = None,
):
    return build_seam_span(
        segment_contexts(thread_id, turn_id, 0),
        thread_id=thread_id,
        turn_id=turn_id,
        segment_seq=0,
        seam=seam,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        attributes=attributes,
    )


def _wait_until(predicate: Any, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ----- the selection -----


def test_pg_is_a_choice_and_every_exporter_stays_off_until_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TRACE_SPAN_DSN_ENV, raising=False)
    assert load_receipt_export_config({}) is None
    assert load_receipt_export_config({"OTEL_TRACES_EXPORTER": ""}) is None
    assert load_receipt_export_config({"OTEL_TRACES_EXPORTER": "none"}) is None

    for exporter in ("console", "otlp"):
        config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": exporter})
        assert config is not None
        assert config.exporter == exporter
        # Neither existing exporter gains a target it did not have.
        assert config.span_dsn is None

    assert "pg" in SUPPORTED_EXPORTERS
    config = _pg_config("postgresql://fin@127.0.0.1/fin_trace_span_test")
    assert config.exporter == "pg"
    assert config.span_dsn == "postgresql://fin@127.0.0.1/fin_trace_span_test"


def test_pg_without_a_target_is_refused_and_the_error_names_no_value() -> None:
    for environ in (
        {"OTEL_TRACES_EXPORTER": "pg"},
        {"OTEL_TRACES_EXPORTER": "pg", TRACE_SPAN_DSN_ENV: "   "},
    ):
        with pytest.raises(ReceiptExportConfigError) as refused:
            load_receipt_export_config(environ)
        message = str(refused.value)
        assert TRACE_SPAN_DSN_ENV in message
        # A connection string, or anything shaped like one, is never echoed.
        assert "postgresql://" not in message
        assert "://" not in message


def test_the_pg_transport_refuses_another_exporter() -> None:
    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "console"})
    assert config is not None
    with pytest.raises(ReceiptExportConfigError):
        PostgresSpanTransport(config)
    with pytest.raises(ReceiptExportConfigError):
        PostgresSpanTransport(ReceiptExportConfig("pg"))


def test_retention_is_configurable_with_a_conservative_default() -> None:
    assert DEFAULT_TRACE_SPAN_RETENTION_DAYS == 14
    assert retention_days_from_env({}) == DEFAULT_TRACE_SPAN_RETENTION_DAYS
    assert retention_days_from_env({TRACE_SPAN_RETENTION_ENV: "3"}) == 3
    # Zero keeps rows: an operator can disable pruning on purpose.
    assert retention_days_from_env({TRACE_SPAN_RETENTION_ENV: "0"}) == 0
    # Nonsense keeps the safe default rather than widening retention.
    assert (
        retention_days_from_env({TRACE_SPAN_RETENTION_ENV: "soon"})
        == DEFAULT_TRACE_SPAN_RETENTION_DAYS
    )
    assert (
        retention_days_from_env({TRACE_SPAN_RETENTION_ENV: "-5"})
        == DEFAULT_TRACE_SPAN_RETENTION_DAYS
    )


# ----- the attribute lock -----


def test_a_persisted_row_carries_only_allowlisted_attributes() -> None:
    projection = build_receipt_span(
        _receipt(
            segment_seq=0,
            route_source="rule",
            route_intent="retrieve",
            grounded_decision="answer",
            execution_state="terminal",
            technical_result="done",
            terminal_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
    )
    row = span_row(projection)

    assert row["name"] == "fin.turn.receipt"
    assert row["trace_id"] == f"{trace_id_for(THREAD, TURN):032x}"
    assert row["span_id"] == f"{projection.context.span_id:016x}"
    assert row["parent_span_id"] is None
    assert row["thread_id"] == THREAD
    assert row["turn_id"] == TURN
    assert row["duration_ms"] == 1000.0
    assert set(row["attributes"]) <= SPAN_ATTRIBUTE_ALLOWLIST
    assert row["attributes"]["fin.route.intent"] == "retrieve"
    assert set(row["attributes"]) <= RECEIPT_SPAN_ATTRIBUTE_NAMES

    seam_row = span_row(
        _one_seam_span(
            THREAD,
            TURN,
            seam="retrieve_tool",
            attributes={
                "fin.citation.verified": True,
                "fin.prompt": RAW_QUESTION,
                "fin.answer": RAW_ANSWER,
            },
        )
    )
    contexts = segment_contexts(THREAD, TURN, 0)
    assert seam_row["parent_span_id"] == f"{contexts.segment_root.span_id:016x}"
    assert seam_row["attributes"] == {
        "fin.thread_id": THREAD,
        "fin.turn_id": TURN,
        "fin.segment_seq": 0,
        "fin.citation.verified": True,
    }
    serialized = json.dumps(seam_row, default=str)
    assert RAW_QUESTION not in serialized
    assert RAW_ANSWER not in serialized


def test_the_allowlist_union_is_the_three_owners_and_nothing_else() -> None:
    assert SPAN_ATTRIBUTE_ALLOWLIST >= {
        "fin.thread_id",
        "fin.turn_id",
        "fin.segment_seq",
        *SEAM_SPAN_ATTRIBUTES,
        *RECEIPT_SPAN_ATTRIBUTE_NAMES,
    }
    for forbidden in ("prompt", "answer", "excerpt", "source_id", "question"):
        assert not any(forbidden in name for name in SPAN_ATTRIBUTE_ALLOWLIST)


# ----- off by default -----


def test_the_default_selection_writes_nothing_and_still_completes_a_turn(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SR-5, off mode: no span row, and the turn's own record still holds.

    The table is created before the turn runs. Without it the read seam
    answers ``[]`` for a MISSING TABLE without querying, so a row-count
    assertion here would pass even if every span had been written.
    """
    monkeypatch.delenv(TRACE_SPAN_DSN_ENV, raising=False)
    ensure_trace_span_schema(dsn)
    threads = InMemoryThreadStore()
    threads.create_thread(id=THREAD, title="trace default off")
    projector = default_receipts(threads)
    config = load_receipt_export_config({})
    assert config is None
    release = start_application_receipt_export_worker(
        store=projector.store, config=config, projector=projector
    )

    real_socket = socket.socket

    class _RefusingSocket(real_socket):  # type: ignore[misc]
        def connect(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("tracing off opened a network connection")

        def connect_ex(self, *args: Any, **kwargs: Any) -> int:
            raise AssertionError("tracing off opened a network connection")

    socket.socket = _RefusingSocket  # type: ignore[misc]
    try:
        receipt = projector.begin(
            thread_id=THREAD, turn_id=TURN, accepted_input_version=0
        )
        with publish_turn_segment(projector, THREAD, TURN):
            for name in CONTRACT_SPAN_NAMES:
                with seam_span(name, current_segment()):
                    pass
        assert list_turn_spans(THREAD, TURN, dsn=dsn) == []
    finally:
        socket.socket = real_socket  # type: ignore[misc]
        release()

    # The session-record layer is always on: the turn's receipt exists and
    # is readable even though the trace layer wrote nothing.
    stored = projector.store.read_receipt(THREAD, TURN)
    assert stored is not None
    assert stored.revision == receipt.revision
    assert stored.exported_revision == 0
    assert projector.finished_span_sink is None
    assert list_turn_spans(THREAD, TURN, dsn=dsn) == []
    _forget(dsn, THREAD)


def test_the_read_seam_is_empty_when_tracing_was_never_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TRACE_SPAN_DSN_ENV, raising=False)
    assert list_turn_spans(THREAD, TURN) == []
    assert list_turn_spans(THREAD, TURN, dsn="   ") == []
    with pytest.raises(ValueError):
        list_turn_spans(THREAD, TURN, limit=0)
    with pytest.raises(ValueError):
        list_turn_spans(THREAD, TURN, limit=100_000)


def test_the_read_seam_reports_an_unreadable_store_instead_of_no_spans() -> None:
    """A configured store that cannot be reached is not "this turn has none".

    The distinction is what lets the developer view say the trace state is
    unreadable rather than claiming tracing produced nothing.
    """
    with pytest.raises(TraceSpanStoreError):
        list_turn_spans(THREAD, TURN, dsn=UNREACHABLE_DSN)


def test_the_read_is_bounded_by_the_named_timeout(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[float] = []
    real_connection = trace_span_store._connection

    def _recorded(target: str, timeout: float) -> Any:
        recorded.append(timeout)
        return real_connection(target, timeout)

    monkeypatch.setattr(trace_span_store, "_connection", _recorded)

    list_turn_spans(THREAD, TURN, dsn=dsn)

    assert recorded == [TRACE_SPAN_READ_TIMEOUT_SECONDS]


def test_the_read_seam_returns_nothing_when_the_table_is_missing(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trace_span_store, "TRACE_SPAN_TABLE", f"fin_trace_span_{uuid4().hex}"
    )
    assert list_turn_spans(THREAD, TURN, dsn=dsn) == []


# ----- the worker never blocks or fails a turn -----


def test_a_database_fault_is_worker_diagnostics_not_a_failed_turn() -> None:
    config = _pg_config(UNREACHABLE_DSN)
    threads = InMemoryThreadStore()
    threads.create_thread(id=THREAD, title="trace pg down")
    projector = default_receipts(threads)
    worker = ReceiptExportWorker(
        projector.store,
        PostgresSpanTransport(config),
        scan_interval=0.02,
    )
    unbind = projector.bind_finished_span_sink(worker.enqueue_finished_spans)
    failures: list[Any] = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: failures.append(args.exc_value)
    stop = worker.start()

    try:
        started = time.monotonic()
        with publish_turn_segment(projector, THREAD, TURN):
            for name in SEAM_SPAN_NAMES:
                with seam_span(name, current_segment()):
                    pass
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, f"a turn waited {elapsed:.3f}s on the span store"
        assert _wait_until(lambda: worker.diagnostics.export_failure_count >= 1)
        assert worker.diagnostics.attempt_count == 3
        # The turn's spans are dropped, not retried forever: the queue
        # hands the batch back as soon as the third attempt fails.
        assert _wait_until(lambda: worker.diagnostics.queued_turns == 0)
    finally:
        threading.excepthook = previous_hook
        unbind()
        stop()

    assert failures == []


def test_a_direct_transport_failure_is_a_typed_export_error() -> None:
    transport = PostgresSpanTransport(_pg_config(UNREACHABLE_DSN))
    span = build_receipt_span(_receipt())
    with pytest.raises(ReceiptExportTransportError):
        transport.send([span], timeout=1.0)
    with pytest.raises(ReceiptExportTransportError):
        transport.send([], timeout=1.0)
    with pytest.raises(ReceiptExportTransportError):
        transport.send([span], timeout=0.0)
    transport.close_active()
    with pytest.raises(ReceiptExportTransportError):
        transport.send([span], timeout=1.0)


# ----- the durable table -----


def test_pg_persists_a_turn_s_five_seam_spans_under_one_root(dsn: str) -> None:
    thread_id, turn_id = _identities()
    threads = InMemoryThreadStore()
    threads.create_thread(id=thread_id, title="trace pg")
    projector = default_receipts(threads)
    transport = PostgresSpanTransport(_pg_config(dsn), retention_days=0)
    unbind = projector.bind_finished_span_sink(
        lambda spans: transport.send(spans, timeout=5.0)
    )
    try:
        with publish_turn_segment(projector, thread_id, turn_id):
            for name in CONTRACT_SPAN_NAMES:
                with seam_span(name, current_segment()):
                    pass

        rows = list_turn_spans(thread_id, turn_id, dsn=dsn)

        assert len(rows) == 6
        names = [row["name"] for row in rows]
        assert names[0] == SEGMENT_ROOT_SPAN_NAME
        assert sorted(names[1:]) == sorted(CONTRACT_SPAN_NAMES)
        root_span_id = rows[0]["span_id"]
        assert rows[0]["parent_span_id"] is None
        for row in rows[1:]:
            # Every span names a parent that is in the table: no orphan.
            assert row["parent_span_id"] == root_span_id
            assert set(row["attributes"]) <= SPAN_ATTRIBUTE_ALLOWLIST
            assert row["attributes"]["fin.thread_id"] == thread_id
            assert row["attributes"]["fin.turn_id"] == turn_id
            assert row["trace_id"] == f"{trace_id_for(thread_id, turn_id):032x}"
    finally:
        unbind()
        _forget(dsn, thread_id)


def test_the_pg_worker_exports_a_real_turn_s_segment_and_receipt(dsn: str) -> None:
    """SR-5, pg mode, through the DEPLOYED worker and a real graph turn.

    The sink is bound by ``start_application_receipt_export_worker`` — not
    wired straight to the transport — so one case covers
    ``ReceiptExportWorker.configured``, the pg transport, the finished-
    segment queue, the SDK pipeline and the receipt scan: the turn's five
    seam spans, their root and the receipt projection land in
    ``fin_trace_span``, and ``exported_revision`` reaches ``revision``.

    The turn is a RESUMED one (segment 1), so the run has to read its
    ``segment_seq`` from the receipt: every span id and every correlation
    attribute must be stamped with 1, not the default 0.
    """
    thread_id, turn_id = _identities()
    threads = InMemoryThreadStore()
    threads.create_thread(id=thread_id, title="trace pg worker")
    projector = default_receipts(threads)
    started = projector.begin(
        thread_id=thread_id, turn_id=turn_id, accepted_input_version=0
    )
    resumed = projector.store.record_accepted_input(
        thread_id=thread_id,
        turn_id=turn_id,
        expected_revision=started.revision,
        input_version=1,
    )
    assert resumed.segment_seq == 1
    stop = start_application_receipt_export_worker(
        store=projector.store, config=_pg_config(dsn), projector=projector
    )
    try:
        with publish_turn_segment(projector, thread_id, turn_id):
            state = _scripted_graph().invoke(
                _turn_input(), config=_run_config(thread_id, turn_id)
            )

        assert state["messages"][-1].content == GROUNDED_ANSWER
        assert _wait_until(
            lambda: projector.store.read_receipt(thread_id, turn_id).exported_revision
            == resumed.revision
        )
        # Six segment rows plus the receipt projection.
        assert _wait_until(
            lambda: len(list_turn_spans(thread_id, turn_id, dsn=dsn)) == 7
        )
        rows = list_turn_spans(thread_id, turn_id, dsn=dsn)
        exported = projector.store.read_receipt(thread_id, turn_id)
    finally:
        stop()
        _forget(dsn, thread_id)

    names = sorted(row["name"] for row in rows)
    assert names == sorted(
        [*CONTRACT_SPAN_NAMES, SEGMENT_ROOT_SPAN_NAME, "fin.turn.receipt"]
    )
    by_name = {row["name"]: row for row in rows}
    assert by_name[SEGMENT_ROOT_SPAN_NAME]["parent_span_id"] is None
    root_span_id = f"{segment_span_id_for(thread_id, turn_id, 1):016x}"
    assert by_name[SEGMENT_ROOT_SPAN_NAME]["span_id"] == root_span_id
    for seam in CONTRACT_SPAN_NAMES:
        row = by_name[seam]
        assert row["span_id"] == f"{seam_span_id_for(thread_id, turn_id, 1, seam):016x}"
        assert row["parent_span_id"] == root_span_id
        assert row["attributes"]["fin.segment_seq"] == 1
    receipt_row = by_name["fin.turn.receipt"]
    assert receipt_row["span_id"] == f"{receipt_span_id_for(thread_id, turn_id, 1):016x}"
    assert receipt_row["attributes"]["fin.segment_seq"] == 1
    assert receipt_row["attributes"]["fin.receipt.revision"] == resumed.revision
    for row in rows:
        assert row["trace_id"] == f"{trace_id_for(thread_id, turn_id):032x}"
        assert row["attributes"]["fin.thread_id"] == thread_id
        assert row["attributes"]["fin.turn_id"] == turn_id
    assert exported.exported_revision == resumed.revision


def test_pg_refuses_a_span_that_carries_raw_text(dsn: str) -> None:
    thread_id, turn_id = _identities()
    ensure_trace_span_schema(dsn)
    try:
        transport = PostgresSpanTransport(_pg_config(dsn), retention_days=0)
        transport.send(
            [
                _one_seam_span(
                    thread_id,
                    turn_id,
                    seam="model_node",
                    attributes={
                        "fin.route.intent": "retrieve",
                        "fin.question": RAW_QUESTION,
                        "fin.answer": RAW_ANSWER,
                    },
                )
            ],
            timeout=5.0,
        )

        rows = list_turn_spans(thread_id, turn_id, dsn=dsn)

        assert len(rows) == 1
        assert rows[0]["attributes"] == {
            "fin.thread_id": thread_id,
            "fin.turn_id": turn_id,
            "fin.segment_seq": 0,
            "fin.route.intent": "retrieve",
        }
        serialized = json.dumps(rows)
        assert RAW_QUESTION not in serialized
        assert RAW_ANSWER not in serialized
    finally:
        _forget(dsn, thread_id)


def test_a_retried_export_does_not_duplicate_or_rewrite_a_span(dsn: str) -> None:
    thread_id, turn_id = _identities()
    transport = PostgresSpanTransport(_pg_config(dsn), retention_days=0)
    batch = [
        _one_seam_span(thread_id, turn_id, seam=name) for name in SEAM_SPAN_NAMES
    ]
    try:
        transport.send(batch, timeout=5.0)
        first = list_turn_spans(thread_id, turn_id, dsn=dsn)
        # The same span identity, exported again with a different ending:
        # one span is one row, and a delivery that already landed is not
        # rewritten by a later one.
        transport.send(
            [
                build_seam_span(
                    segment_contexts(thread_id, turn_id, 0),
                    thread_id=thread_id,
                    turn_id=turn_id,
                    segment_seq=0,
                    seam=SEAM_SPAN_NAMES[0],
                    started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ended_at=datetime(2026, 1, 1, 0, 0, 9, tzinfo=timezone.utc),
                )
            ],
            timeout=5.0,
        )
        second = list_turn_spans(thread_id, turn_id, dsn=dsn)

        assert len(first) == len(SEAM_SPAN_NAMES)
        assert [row["id"] for row in first] == [row["id"] for row in second]
        assert [row["end_time"] for row in first] == [
            row["end_time"] for row in second
        ]
    finally:
        _forget(dsn, thread_id)


def test_the_read_seam_orders_by_start_time_and_bounds_the_limit(dsn: str) -> None:
    thread_id, turn_id = _identities()
    ensure_trace_span_schema(dsn)
    try:
        record_spans(
            dsn,
            [_one_seam_span(thread_id, turn_id, seam=name) for name in SEAM_SPAN_NAMES],
            timeout=5.0,
        )

        rows = list_turn_spans(thread_id, turn_id, dsn=dsn, limit=2)

        assert len(rows) == 2
        assert [row["start_time"] for row in rows] == sorted(
            row["start_time"] for row in rows
        )
        assert all(isinstance(row["attributes"], dict) for row in rows)
    finally:
        _forget(dsn, thread_id)


def test_retention_prunes_only_rows_older_than_the_window(dsn: str) -> None:
    old_thread, old_turn = _identities()
    fresh_thread, fresh_turn = _identities()
    ensure_trace_span_schema(dsn)
    try:
        record_spans(
            dsn, [_one_seam_span(old_thread, old_turn, seam="classify_node")], timeout=5.0
        )
        record_spans(
            dsn,
            [_one_seam_span(fresh_thread, fresh_turn, seam="classify_node")],
            timeout=5.0,
        )
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE fin_trace_span SET created_at = now() - interval '30 days' "
                "WHERE thread_id = %s",
                (old_thread,),
            )

        deleted = prune_spans(dsn, retention_days=14, timeout=5.0)

        assert deleted == 1
        assert list_turn_spans(old_thread, old_turn, dsn=dsn) == []
        assert len(list_turn_spans(fresh_thread, fresh_turn, dsn=dsn)) == 1
    finally:
        _forget(dsn, old_thread, fresh_thread)


def test_a_pruning_fault_does_not_turn_a_delivered_batch_into_a_failure(
    dsn: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    thread_id, turn_id = _identities()
    transport = PostgresSpanTransport(_pg_config(dsn), retention_days=7)
    try:
        def _unavailable(*_args: Any, **_kwargs: Any) -> int:
            import psycopg

            raise psycopg.OperationalError("retention is unavailable")

        monkeypatch.setattr(trace_span_store, "prune_spans", _unavailable)
        with caplog.at_level("WARNING", logger="fin.host.trace_span_store"):
            transport.send(
                [_one_seam_span(thread_id, turn_id, seam="classify_node")],
                timeout=5.0,
            )

        # The batch was written before the prune ran, and the prune's
        # fault is a warning rather than a delivery failure.
        assert len(list_turn_spans(thread_id, turn_id, dsn=dsn)) == 1
        assert "retention prune failed" in caplog.text
    finally:
        _forget(dsn, thread_id)
