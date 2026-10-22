"""P3 T4 — the tool outcome ledger against real PostgreSQL (``runtime_pg``).

Each case runs in a throwaway schema of the isolated ``_test`` database.
Facts commit only under a live worker attempt: a stale worker is rejected
by the same ownership assertion the fenced checkpointer runs, and the
append-only trigger rejects UPDATE and DELETE. Ordering is explicit (a row
update expires the lease, then a takeover claim mints the next
generation); no case infers a race from a sleep. Persistence is read back
by a fresh OS process.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.runtime import PostgresRuntimeStore, RuntimeCoordinator
from fin.host.runtime.schema import (
    OWNERSHIP_ATTEMPT_GENERATION,
    CheckpointOwnershipLost,
)
from fin.host.state import reset_input_state
from fin.host.tools.ledger import (
    PostgresToolOutcomeLedger,
    ToolOutcomeObservation,
    fact_id,
)
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileStore,
)
from fin.retrieve import Chunk, FakeRetrievePort

import runtime_pg_support

pytestmark = pytest.mark.runtime_pg

QUESTION = "What was AAPL net sales in 2023 Q3?"
QUERY = "net sales"
COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."

CHILD_READBACK = """
import json, os
from fin.host.tools.ledger import PostgresToolOutcomeLedger

ledger = PostgresToolOutcomeLedger(os.environ["FIN_TEST_DSN"])
facts = ledger.read_run(os.environ["FIN_LEDGER_RUN"])
print(json.dumps({
    "pid": os.getpid(),
    "facts": [
        {
            "fact_id": fact.fact_id,
            "kind": fact.kind,
            "logical_call_id": fact.logical_call_id,
            "tool_name": fact.tool_name,
            "model_attempt": fact.model_attempt,
            "execution_attempt": fact.execution_attempt,
            "provider_tool_call_id": fact.provider_tool_call_id,
            "runtime_attempt_id": fact.runtime_attempt_id,
            "runtime_attempt_generation": fact.runtime_attempt_generation,
            "thread_generation": fact.thread_generation,
            "outcome_code": fact.outcome_code,
            "field_paths": list(fact.field_paths),
        }
        for fact in facts
    ],
}))
"""


# ----- fixtures -------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_tool_ledger")
    try:
        yield runtime_pg_support.scoped_url(dsn, schema)
    finally:
        runtime_pg_support.drop_schema(dsn, schema)


@pytest.fixture(scope="module")
def store(runtime_url: str) -> PostgresRuntimeStore:
    runtime_store = PostgresRuntimeStore(runtime_url)
    runtime_store.setup()
    return runtime_store


@pytest.fixture(scope="module")
def ledger(runtime_url: str) -> PostgresToolOutcomeLedger:
    outcome_ledger = PostgresToolOutcomeLedger(runtime_url)
    outcome_ledger.setup()
    return outcome_ledger


@pytest.fixture()
def coordinator(store: PostgresRuntimeStore) -> RuntimeCoordinator:
    return RuntimeCoordinator(store, queue_capacity=8, lease_seconds=30.0)


# ----- helpers --------------------------------------------------------------


def _claim(
    coordinator: RuntimeCoordinator, *, thread_id: str | None = None
):
    thread = thread_id or f"thread-{uuid.uuid4()}"
    coordinator.accept(
        operation_id=f"op-{uuid.uuid4()}",
        thread_id=thread,
        accepted_input={"message": "ledger me"},
    )
    outcome = coordinator.claim(thread_id=thread, worker_id="worker-a")
    assert outcome.claimed is True, outcome.reason
    assert outcome.lease is not None
    return outcome.lease


def _expire(url: str, attempt_id: str) -> None:
    """Expire one lease by an explicit row update, never by sleeping."""
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(
            "UPDATE fin_runtime_worker_attempt SET "
            "lease_expires_at = now() - interval '1 second' "
            "WHERE attempt_id = %s",
            (attempt_id,),
        )


def _observe(
    kind: str,
    *,
    logical: str = "retrieve#1",
    call_id: str = "call-1",
    tool: str = "retrieve",
    model_attempt: int = 1,
    execution: int | None = None,
    code: str | None = None,
    paths: Sequence[str] = (),
) -> ToolOutcomeObservation:
    return ToolOutcomeObservation(
        logical_call_id=logical,
        tool_name=tool,
        model_attempt=model_attempt,
        kind=kind,  # type: ignore[arg-type]
        provider_tool_call_id=call_id,
        execution_attempt=execution,
        outcome_code=code,  # type: ignore[arg-type]
        field_paths=tuple(paths),
    )


def _record(
    ledger: PostgresToolOutcomeLedger,
    lease: Any,
    observations: Sequence[ToolOutcomeObservation],
) -> int:
    return ledger.record(
        observations,
        thread_id=lease.thread_id,
        run_id=lease.run_id,
        attempt_id=lease.attempt_id,
        attempt_generation=lease.attempt_generation,
        thread_generation=lease.thread_generation,
    )


def _kinds_by_call(
    facts: Sequence[Any],
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for fact in facts:
        grouped.setdefault(fact.provider_tool_call_id or "", []).append(fact.kind)
    return grouped


# ----- facts and identities -------------------------------------------------


def test_facts_distinguish_success_known_failure_and_unknown(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    lease = _claim(coordinator)
    observations = [
        _observe("valid_input_accepted"),
        _observe("execution_started", execution=1),
        _observe("succeeded", execution=1),
        _observe(
            "valid_input_accepted",
            logical="retrieve#2",
            call_id="call-2",
            model_attempt=2,
        ),
        _observe(
            "execution_started",
            logical="retrieve#2",
            call_id="call-2",
            model_attempt=2,
            execution=2,
        ),
        _observe(
            "known_failed",
            logical="retrieve#2",
            call_id="call-2",
            model_attempt=2,
            execution=2,
            code="tool_result_error",
        ),
        _observe(
            "valid_input_accepted",
            logical="save_memory#1",
            call_id="call-3",
            tool="save_memory",
        ),
        _observe(
            "execution_started",
            logical="save_memory#1",
            call_id="call-3",
            tool="save_memory",
            execution=1,
        ),
        _observe(
            "outcome_unknown",
            logical="save_memory#1",
            call_id="call-3",
            tool="save_memory",
            execution=1,
            code="tool_result_error",
        ),
    ]
    assert _record(ledger, lease, observations) == len(observations)

    facts = ledger.read_run(lease.run_id)
    assert [fact.kind for fact in facts] == [
        observation.kind for observation in observations
    ]
    assert [fact.fact_seq for fact in facts] == sorted(
        fact.fact_seq for fact in facts
    )
    # Every fact carries the run, the logical call and the SAME worker
    # attempt identity that committed it.
    assert {fact.thread_id for fact in facts} == {lease.thread_id}
    assert {fact.runtime_attempt_id for fact in facts} == {lease.attempt_id}
    assert {fact.runtime_attempt_generation for fact in facts} == {
        lease.attempt_generation
    }
    assert {fact.thread_generation for fact in facts} == {lease.thread_generation}
    # A provider tool-call id is never the logical identity.
    assert all(
        fact.logical_call_id != fact.provider_tool_call_id for fact in facts
    )
    assert [fact.model_attempt for fact in facts] == [
        1,
        1,
        1,
        2,
        2,
        2,
        1,
        1,
        1,
    ]
    unknown = [
        fact for fact in facts if fact.kind in ("known_failed", "outcome_unknown")
    ]
    assert {fact.outcome_code for fact in unknown} == {"tool_result_error"}


def test_duplicate_delivery_adds_no_second_row(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    lease = _claim(coordinator)
    observations = [
        _observe(
            "schema_rejected",
            call_id="call-bad",
            code="invalid_tool_arguments",
            paths=("limit",),
        ),
        _observe(
            "correction_offered",
            call_id="call-bad",
            code="invalid_tool_arguments",
            paths=("limit",),
        ),
    ]
    assert _record(ledger, lease, observations) == len(observations)
    before = ledger.read_run(lease.run_id)
    assert len(before) == len(observations)

    # The same logical result delivered twice commits nothing new.
    assert _record(ledger, lease, observations) == 0
    after = ledger.read_run(lease.run_id)
    assert [fact.fact_id for fact in after] == [fact.fact_id for fact in before]
    assert [fact.field_paths for fact in after] == [("limit",), ("limit",)]


def _fact_content(fact: Any) -> tuple[Any, ...]:
    """The delivery content of a fact, without its run or attempt identity."""
    return (
        fact.kind,
        fact.logical_call_id,
        fact.tool_name,
        fact.model_attempt,
        fact.execution_attempt,
        fact.provider_tool_call_id,
        fact.outcome_code,
        fact.field_paths,
    )


def test_concurrent_duplicate_delivery_commits_one_logical_result(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    """Barrier-ordered duplicate delivery adds no second logical result."""
    import psycopg

    observations = [_observe("succeeded", execution=1)]

    # The same observations delivered once are the reference facts.
    baseline_lease = _claim(coordinator)
    assert _record(ledger, baseline_lease, observations) == 1
    (baseline,) = ledger.read_run(baseline_lease.run_id)

    lease = _claim(coordinator)
    # Both deliveries reach the ledger together; the explicit barrier
    # orders them at the same start line instead of inferring a race.
    barrier = threading.Barrier(2)
    inserted: list[int] = []
    errors: list[BaseException] = []

    def deliver() -> None:
        try:
            barrier.wait(timeout=30)
            inserted.append(_record(ledger, lease, observations))
        except BaseException as exc:  # surfaced on the test thread below
            errors.append(exc)

    threads = [
        threading.Thread(target=deliver, name=f"delivery-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    # Exactly one delivery won the insert; the other added no row.
    assert len(inserted) == 2
    assert sorted(inserted) == [0, 1]
    assert sum(inserted) == 1

    facts = ledger.read_run(lease.run_id)
    assert len(facts) == 1
    fact = facts[0]
    expected_id = fact_id(
        run_id=lease.run_id,
        logical_call_id="retrieve#1",
        kind="succeeded",
        provider_tool_call_id="call-1",
        execution_attempt=1,
    )
    assert fact.fact_id == expected_id
    with psycopg.connect(ledger.connection_url) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM fin_tool_outcome_fact WHERE fact_id = %s",
            (expected_id,),
        ).fetchone()
    assert rows is not None and rows[0] == 1
    # The committed row is exactly the one a single delivery would write.
    assert _fact_content(fact) == _fact_content(baseline)
    assert fact.runtime_attempt_id == lease.attempt_id


def test_a_duplicate_delivery_is_logged_as_a_count_and_never_as_a_value(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lease = _claim(coordinator)
    observations = [
        _observe(
            "schema_rejected",
            call_id="call-bad",
            code="invalid_tool_arguments",
            paths=("limit",),
        )
    ]
    assert _record(ledger, lease, observations) == 1

    with caplog.at_level(logging.INFO, logger="fin.host.tools.ledger"):
        assert _record(ledger, lease, observations) == 0

    records = [
        record
        for record in caplog.records
        if record.name == "fin.host.tools.ledger"
    ]
    assert len(records) == 1, [record.getMessage() for record in records]
    message = records[0].getMessage()
    assert records[0].levelno == logging.INFO
    # The difference is named as a duplicate delivery ...
    assert "duplicate delivery" in message
    assert "1 of 1" in message
    # ... and no observation value reaches the log.
    assert "call-bad" not in message
    assert "limit" not in message
    assert "invalid_tool_arguments" not in message


def test_the_readback_maps_every_column_to_its_own_field(
    ledger: PostgresToolOutcomeLedger,
) -> None:
    """The read is by column name, so a column cannot be mis-assigned."""
    import psycopg

    run_id = f"run-{uuid.uuid4()}"
    with psycopg.connect(ledger.connection_url) as conn:
        conn.execute(
            "INSERT INTO fin_tool_outcome_fact ("
            "  kind, outcome_code, tool_name, model_attempt, execution_attempt,"
            "  field_paths, business_operation_id, provider_tool_call_id,"
            "  runtime_attempt_id, runtime_attempt_generation, thread_generation,"
            "  logical_call_id, thread_id, run_id, fact_id"
            ") VALUES ("
            "  %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s"
            ")",
            (
                "known_failed",
                "tool_result_error",
                "column_probe",
                7,
                9,
                '["limit"]',
                "op-1",
                "call-1",
                "attempt-1",
                4,
                3,
                "retrieve#1",
                "thread-1",
                run_id,
                "fact-probe-1",
            ),
        )

    facts = ledger.read_run(run_id)

    assert len(facts) == 1
    fact = facts[0]
    assert fact.kind == "known_failed"
    assert fact.outcome_code == "tool_result_error"
    assert fact.tool_name == "column_probe"
    assert fact.model_attempt == 7
    assert fact.execution_attempt == 9
    assert fact.field_paths == ("limit",)
    assert fact.business_operation_id == "op-1"
    assert fact.provider_tool_call_id == "call-1"
    assert fact.runtime_attempt_id == "attempt-1"
    assert fact.runtime_attempt_generation == 4
    assert fact.thread_generation == 3
    assert fact.logical_call_id == "retrieve#1"
    assert fact.thread_id == "thread-1"
    assert fact.run_id == run_id
    assert fact.fact_id == "fact-probe-1"
    assert isinstance(fact.created_at, datetime)


def test_setup_does_not_rewrite_the_outcome_code_constraint(
    ledger: PostgresToolOutcomeLedger,
) -> None:
    """A steady-state boot must not re-validate the append-only table."""
    import psycopg

    def _constraint_oid() -> int:
        with psycopg.connect(ledger.connection_url) as conn:
            row = conn.execute(
                "SELECT oid FROM pg_constraint "
                "WHERE conrelid = 'fin_tool_outcome_fact'::regclass "
                "AND conname = 'fin_tool_outcome_fact_outcome_code_check'"
            ).fetchone()
        assert row is not None
        return int(row[0])

    before = _constraint_oid()
    ledger.setup()
    after = _constraint_oid()

    # A drop-and-re-add would mint a new constraint identity.
    assert after == before


def test_the_guarded_outcome_code_constraint_still_rejects_an_unknown_code(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    """The catalog guard adds a working check, not just a name."""
    import psycopg

    lease = _claim(coordinator)
    with pytest.raises(psycopg.Error) as rejected:
        with psycopg.connect(ledger.connection_url) as conn:
            conn.execute(
                "INSERT INTO fin_tool_outcome_fact ("
                "  fact_id, run_id, thread_id, logical_call_id, tool_name,"
                "  model_attempt, runtime_attempt_id, runtime_attempt_generation,"
                "  thread_generation, kind, outcome_code"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "fact-unknown-code",
                    lease.run_id,
                    lease.thread_id,
                    "retrieve#1",
                    "retrieve",
                    1,
                    lease.attempt_id,
                    lease.attempt_generation,
                    lease.thread_generation,
                    "known_failed",
                    "a free-text reason",
                ),
            )
    assert "fin_tool_outcome_fact_outcome_code_check" in str(rejected.value)


# ----- immutability ---------------------------------------------------------


def test_update_and_delete_are_rejected_by_the_database(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    import psycopg

    lease = _claim(coordinator)
    assert _record(ledger, lease, [_observe("succeeded", execution=1)]) == 1

    with pytest.raises(psycopg.Error) as update:
        with psycopg.connect(ledger.connection_url) as conn:
            conn.execute(
                "UPDATE fin_tool_outcome_fact SET kind = 'known_failed' "
                "WHERE run_id = %s",
                (lease.run_id,),
            )
    assert "append-only" in str(update.value)

    with pytest.raises(psycopg.Error) as delete:
        with psycopg.connect(ledger.connection_url) as conn:
            conn.execute(
                "DELETE FROM fin_tool_outcome_fact WHERE run_id = %s",
                (lease.run_id,),
            )
    assert "append-only" in str(delete.value)

    assert [fact.kind for fact in ledger.read_run(lease.run_id)] == ["succeeded"]


# ----- fencing --------------------------------------------------------------


def test_a_stale_worker_commits_nothing(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    lease = _claim(coordinator)
    stale = [
        _observe("valid_input_accepted"),
        _observe("execution_started", execution=1),
        _observe("succeeded", execution=1),
    ]

    # Explicit ordering: the old lease is expired by a row update, then the
    # takeover claim mints generation + 1. No sleep infers the race.
    _expire(ledger.connection_url, lease.attempt_id)
    takeover = coordinator.claim(
        thread_id=lease.thread_id, worker_id="worker-b", run_id=lease.run_id
    )
    assert takeover.claimed is True, takeover.reason
    assert takeover.lease is not None
    assert takeover.lease.attempt_generation == lease.attempt_generation + 1

    with pytest.raises(CheckpointOwnershipLost) as rejected:
        _record(ledger, lease, stale)
    assert rejected.value.reason == OWNERSHIP_ATTEMPT_GENERATION
    # Nothing from the fenced step committed.
    assert ledger.read_run(lease.run_id) == ()

    # The current owner still records.
    assert _record(ledger, takeover.lease, stale) == len(stale)
    assert [fact.kind for fact in ledger.read_run(lease.run_id)] == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]


# ----- fresh-process readback ----------------------------------------------


def test_a_fresh_process_reads_the_facts_and_their_identities(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
) -> None:
    lease = _claim(coordinator)
    observations = [
        _observe("valid_input_accepted"),
        _observe("execution_started", execution=1),
        _observe("succeeded", execution=1),
    ]
    _record(ledger, lease, observations)

    child = runtime_pg_support.run_child(
        CHILD_READBACK,
        dsn=ledger.connection_url,
        env={"FIN_LEDGER_RUN": lease.run_id},
    )

    assert child["pid"] != os.getpid()
    facts = child["facts"]
    assert [fact["kind"] for fact in facts] == [
        observation.kind for observation in observations
    ]
    assert {fact["runtime_attempt_id"] for fact in facts} == {lease.attempt_id}
    assert {fact["runtime_attempt_generation"] for fact in facts} == {
        lease.attempt_generation
    }
    assert {fact["thread_generation"] for fact in facts} == {
        lease.thread_generation
    }


# ----- the graph writes its own step ----------------------------------------


def _chunk() -> Chunk:
    return Chunk(
        text=COMPANY_TEXT,
        source_ref=Locator(document_id="AAPL-2023Q3", version="d" * 64, unit=3),
        score=1.5,
    )


def _profile_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / "ws" / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def test_the_production_boot_wires_the_ledger_on_the_runtime_store(
    runtime_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin.host import compile as compile_module

    monkeypatch.setattr(
        compile_module,
        "_build_default_model",
        lambda **_kwargs: ScriptedChatModel(script=[]),
    )

    handle = compile_module.compile_runtime_handle(
        checkpointer_url=runtime_url, env={}
    )
    try:
        compiled = handle.graph
        assert isinstance(compiled.tool_ledger, PostgresToolOutcomeLedger)
        assert compiled.tool_ledger.connection_url == runtime_url
    finally:
        handle.close()


def test_the_graph_records_valid_and_rejected_calls_in_one_attempt(
    ledger: PostgresToolOutcomeLedger,
    coordinator: RuntimeCoordinator,
    tmp_path: Path,
) -> None:
    lease = _claim(coordinator)
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": QUERY},
                        "id": "call-good",
                        "type": "tool_call",
                    },
                    {
                        "name": "retrieve",
                        "args": {"query": 424242},
                        "id": "call-bad",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="Answer."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(hits_by_query={QUERY: [_chunk()]}),
        user_profile_store=_profile_store(tmp_path),
        tool_ledger=ledger,
    )
    out = graph.invoke(
        reset_input_state(QUESTION),
        config={
            "configurable": {
                "thread_id": lease.thread_id,
                "run_id": lease.run_id,
                "runtime_run_id": lease.run_id,
                "runtime_attempt_id": lease.attempt_id,
                "runtime_attempt_generation": lease.attempt_generation,
                "thread_generation": lease.thread_generation,
            }
        },
    )
    assert out["retrieve_attempted"] is True

    facts = ledger.read_run(lease.run_id)
    kinds = _kinds_by_call(facts)
    assert kinds["call-good"] == [
        "valid_input_accepted",
        "execution_started",
        "succeeded",
    ]
    assert kinds["call-bad"] == ["schema_rejected", "correction_offered"]
    # The rejected call never started an execution, and both calls belong to
    # the one worker attempt that recorded them.
    assert all(
        fact.execution_attempt is None
        for fact in facts
        if fact.provider_tool_call_id == "call-bad"
    )
    assert {fact.runtime_attempt_id for fact in facts} == {lease.attempt_id}
    assert {fact.thread_id for fact in facts} == {lease.thread_id}
    assert {fact.logical_call_id for fact in facts} == {
        "retrieve#1",
        "retrieve#2",
    }
