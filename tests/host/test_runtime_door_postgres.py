"""The workspace door against the real durable Runtime.

These cases are the live half of ``test_runtime_door.py``: the same door,
but backed by ``RuntimeCoordinator`` over an isolated schema in the ``_test``
database. They pin the ordering that only PostgreSQL can prove — the
operation is durable before the run starts, a replay is recognized before a
full queue is refused, and the accepted operation/terminal survive into a
fresh OS process.

A missing ``FIN_TEST_DSN`` is a named failure, never a skip.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from fin.host import workspace_http
from fin.host.runtime import PostgresRuntimeStore, RuntimeCoordinator
from fin.host.runtime.schema import (
    OPERATION_OPEN,
    RUN_COMPLETED,
    RUN_WAITING,
)
from fin.host.task.task_runtime_types import PendingQuestion, TaskRuntimeState
from fin.host.thread_store import InMemoryThreadStore
from fin.host.workspace_http import build_router

import runtime_pg_support

pytestmark = pytest.mark.runtime_pg

ANSWER = "AAPL net sales were 81.8 billion dollars."

CHILD_READ = """
import json, os
from fin.host.runtime import PostgresRuntimeStore
store = PostgresRuntimeStore(os.environ["FIN_TEST_DSN"])
record = store.read_operation(os.environ["FIN_RUNTIME_OPERATION"])
terminal = store.read_terminal(os.environ["FIN_RUNTIME_RUN"])
print(json.dumps({
    "pid": os.getpid(),
    "operation": None if record is None else {
        "operation_id": record.operation_id,
        "thread_id": record.thread_id,
        "run_id": record.run_id,
        "fifo_position": record.fifo_position,
        "state": record.state,
    },
    "terminal": None if terminal is None else {
        "technical_result": terminal.technical_result,
        "semantic_result": terminal.semantic_result,
        "status": terminal.payload.get("status"),
    },
}))
"""


# ----- the door's non-runtime collaborators --------------------------------


class _RecordingReceipts:
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self._log = log

    def begin(self, **kwargs: Any) -> None:
        self._log.append(("receipts.begin", kwargs["turn_id"]))

    def accepted_reply(self, **kwargs: Any) -> None:
        self._log.append(("receipts.accepted_reply", kwargs["turn_id"]))

    def resume_started(self, **kwargs: Any) -> None:
        return None

    def reconcile_accepted_inputs(self, **kwargs: Any) -> None:
        return None

    def paused(self, **kwargs: Any) -> None:
        return None

    def commit_terminal(self, **kwargs: Any) -> None:
        self._log.append(("turn_row", kwargs["run"].id))

    def stream_abandoned(self, **kwargs: Any) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        return None


class _FakeAttachments:
    def status(self, attachment_id: str) -> None:
        return None

    def unresolved_publication_count(self, thread_id: str) -> int:
        return 0

    def delete_thread(self, thread_id: str) -> None:
        return None


class _OffMemoryPort:
    enabled = False


class _DoorGraph:
    """A mutable graph double: pause, then serve the resumed remainder."""

    def __init__(self, runtime: RuntimeCoordinator, log: list[tuple[Any, ...]]) -> None:
        self.runtime = runtime
        self._log = log
        self.stream_calls = 0
        self.paused = False
        #: Streams that still end on a choice window, decremented per stream.
        self.pauses_remaining = 0
        #: The window ``interrupt_pending`` reports while one is open.
        self.pending_choice: Any = None
        self.state: Any = None
        self.pending: tuple[str, ...] = ()
        self.checkpoint_run_id_value: str | None = None

    def stream(
        self, input_value: Any, *, config: dict[str, Any], stream_mode: Any
    ):
        self.stream_calls += 1
        return self._drive()

    def _drive(self):
        if self.pauses_remaining > 0 or self.paused:
            if self.pauses_remaining > 0:
                self.pauses_remaining -= 1
            interrupt = type(
                "_Interrupt",
                (),
                {"value": {"kind": "choice_window", "options": []}},
            )()
            yield ("values", {"__interrupt__": [interrupt]})
            return
        yield (
            "values",
            {
                "messages": [AIMessage(content=ANSWER)],
                "status": "done",
                "answer": ANSWER,
                "citations": [],
                "evidence": [],
            },
        )

    def is_locked(self, thread_id: str) -> bool:
        return False

    def interrupt_pending(self, thread_id: str) -> Any:
        return self.pending_choice

    def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
        return self.pending

    def checkpoint_run_id(self, thread_id: str) -> str | None:
        return self.checkpoint_run_id_value

    def checkpoint_message_ids(self, thread_id: str) -> set[str]:
        return set()

    def task_runtime_state(self, thread_id: str) -> Any:
        from fin.host.task.task_runtime_types import TaskRuntimeState

        return self.state or TaskRuntimeState()

    def finalize_abandoned_run(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        return None


# ----- harness -------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime_url() -> Iterator[str]:
    dsn = runtime_pg_support.test_dsn()
    runtime_pg_support.validate_test_database(dsn)
    schema = runtime_pg_support.create_schema(dsn, "fin_runtime_door")
    try:
        yield runtime_pg_support.scoped_url(dsn, schema)
    finally:
        runtime_pg_support.drop_schema(dsn, schema)


@pytest.fixture(scope="module")
def runtime_store(runtime_url: str) -> PostgresRuntimeStore:
    store = PostgresRuntimeStore(runtime_url)
    store.setup()
    return store


def _client(
    *,
    runtime: RuntimeCoordinator,
    graph: _DoorGraph,
    store: InMemoryThreadStore,
    log: list[tuple[Any, ...]],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    receipts = _RecordingReceipts(log)
    monkeypatch.setattr(
        workspace_http, "default_receipts", lambda _store: receipts
    )
    app = FastAPI()
    app.include_router(
        build_router(
            get_graph=lambda: graph,
            get_thread_store=lambda: store,
            get_attachment_service=lambda: _FakeAttachments(),
            get_memory_port=lambda: _OffMemoryPort(),
            get_workspace_root=lambda: tmp_path,
        )
    )
    return TestClient(app)


def _thread() -> tuple[InMemoryThreadStore, str]:
    thread_id = f"thread-{uuid.uuid4()}"
    store = InMemoryThreadStore()
    store.create_thread(id=thread_id, title="runtime door live")
    return store, thread_id


# ----- cases ---------------------------------------------------------------


def test_the_accepted_operation_and_its_terminal_survive_a_fresh_process(
    runtime_url: str,
    runtime_store: PostgresRuntimeStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = f"op-{uuid.uuid4()}"
    log: list[tuple[Any, ...]] = []
    runtime = RuntimeCoordinator(runtime_store)
    store, thread_id = _thread()
    graph = _DoorGraph(runtime, log)
    client = _client(
        runtime=runtime,
        graph=graph,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    response = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "What was AAPL net sales?", "operation_id": operation},
    )

    assert response.status_code == 200
    kinds = [entry[0] for entry in log]
    assert kinds.index("receipts.begin") < kinds.index("turn_row")
    record = runtime_store.read_operation(operation)
    assert record is not None
    assert record.thread_id == thread_id
    assert record.state == RUN_COMPLETED
    assert graph.stream_calls == 1
    terminal = runtime_store.read_terminal(record.run_id)
    assert terminal is not None
    assert terminal.technical_result == "done"
    assert terminal.semantic_result == "answered"
    assert terminal.payload["status"] == "done"

    child = runtime_pg_support.run_child(
        CHILD_READ,
        dsn=runtime_url,
        env={
            "FIN_RUNTIME_OPERATION": operation,
            "FIN_RUNTIME_RUN": record.run_id,
        },
    )
    assert child["pid"] != os.getpid()
    assert child["operation"] == {
        "operation_id": operation,
        "thread_id": thread_id,
        "run_id": record.run_id,
        "fifo_position": record.fifo_position,
        "state": RUN_COMPLETED,
    }
    assert child["terminal"] == {
        "technical_result": "done",
        "semantic_result": "answered",
        "status": "done",
    }


def test_a_duplicate_after_completion_replays_and_a_changed_payload_conflicts(
    runtime_store: PostgresRuntimeStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = f"op-{uuid.uuid4()}"
    log: list[tuple[Any, ...]] = []
    runtime = RuntimeCoordinator(runtime_store)
    store, thread_id = _thread()
    graph = _DoorGraph(runtime, log)
    client = _client(
        runtime=runtime,
        graph=graph,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    body = {"question": "What was AAPL net sales?", "operation_id": operation}

    assert (
        client.post(f"/api/threads/{thread_id}/messages", json=body).status_code
        == 200
    )
    replayed = client.post(f"/api/threads/{thread_id}/messages", json=body)

    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("application/json")
    payload = replayed.json()
    assert payload["status"] == "replayed"
    assert payload["state"] == RUN_COMPLETED
    assert payload["run_id"]
    assert payload["result"]["state"] == RUN_COMPLETED
    # One run, one receipt: the replay started nothing.
    assert graph.stream_calls == 1
    assert [entry[0] for entry in log].count("receipts.begin") == 1

    conflicted = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "a different question", "operation_id": operation},
    )

    assert conflicted.status_code == 409
    assert conflicted.json() == {"error": "operation_conflict"}
    assert graph.stream_calls == 1


def test_a_duplicate_is_recognized_before_a_full_queue_is_refused(
    runtime_store: PostgresRuntimeStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = f"op-{uuid.uuid4()}"
    second = f"op-{uuid.uuid4()}"
    log: list[tuple[Any, ...]] = []
    runtime = RuntimeCoordinator(runtime_store, queue_capacity=1)
    store, thread_id = _thread()
    graph = _DoorGraph(runtime, log)
    graph.paused = True
    client = _client(
        runtime=runtime,
        graph=graph,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    body = {"question": "Pick a filing", "operation_id": first}

    assert (
        client.post(f"/api/threads/{thread_id}/messages", json=body).status_code
        == 200
    )
    # The paused run keeps its reservation and its operation open.
    accepted = runtime_store.read_operation(first)
    assert accepted is not None
    assert accepted.state == OPERATION_OPEN
    assert accepted.fifo_position == 1

    refused = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "another question", "operation_id": second},
    )

    assert refused.status_code == 429
    assert refused.json() == {"error": "queue_capacity_exceeded"}

    replayed = client.post(f"/api/threads/{thread_id}/messages", json=body)

    assert replayed.status_code == 200
    assert replayed.json()["status"] == "replayed"
    assert replayed.json()["state"] == OPERATION_OPEN
    assert replayed.json()["run_id"] == accepted.run_id
    snapshot = runtime_store.read_recovery_snapshot(accepted.run_id)
    assert snapshot is not None
    assert snapshot.run_state == RUN_WAITING
    assert graph.stream_calls == 1


# ----- an existing run: continuation and resume -----------------------------


def _pending_state() -> TaskRuntimeState:
    return TaskRuntimeState(
        pending_question=PendingQuestion(
            question_id="q-1",
            task_id="",
            task_version=1,
            prompt="Which filing?",
            choices=("a", "b"),
            input_version=0,
        )
    )


def _row_counts(dsn: str, thread_id: str) -> tuple[int, int]:
    """Runs and operations this thread holds, read straight from PostgreSQL."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        runs = conn.execute(
            "SELECT count(*) FROM fin_runtime_run WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
        operations = conn.execute(
            "SELECT count(*) FROM fin_runtime_operation WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
    return int(runs), int(operations)


def _operation_rows(dsn: str, thread_id: str) -> list[tuple[str, str]]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT operation_id, run_id FROM fin_runtime_operation "
            "WHERE thread_id = %s ORDER BY fifo_position",
            (thread_id,),
        ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def test_a_continuation_adds_one_operation_and_no_second_run_or_receipt(
    runtime_url: str,
    runtime_store: PostgresRuntimeStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = RuntimeCoordinator(runtime_store)
    store, thread_id = _thread()
    graph = _DoorGraph(runtime, log)
    graph.paused = True
    client = _client(
        runtime=runtime,
        graph=graph,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    # The pre-P1 UI sends no operation id: the send's key is the run id.
    assert (
        client.post(
            f"/api/threads/{thread_id}/messages",
            json={"question": "Pick a filing"},
        ).status_code
        == 200
    )
    (send_key, run_id), = _operation_rows(runtime_url, thread_id)
    assert send_key == run_id
    original = runtime_store.read_operation(send_key)
    assert original is not None
    assert original.state == OPERATION_OPEN

    # The paused run is now answerable: the graph serves the resumed remainder
    # under the SAME run identity, and the reply needs no operation id.
    graph.paused = False
    graph.state = _pending_state()
    graph.pending = ("clarify",)
    graph.checkpoint_run_id_value = run_id
    reply_body = {
        "question": "a",
        "question_id": "q-1",
        "choice_id": "a",
    }

    answered = client.post(
        f"/api/threads/{thread_id}/messages", json=reply_body
    )

    assert answered.status_code == 200
    rows = _operation_rows(runtime_url, thread_id)
    assert len(rows) == 2
    assert rows[0] == (run_id, run_id)
    reply_key, reply_run = rows[1]
    # The reply's key carries the accepted input's digest.
    assert reply_key.startswith(f"{run_id}:reply:")
    assert reply_run == run_id
    reply = runtime_store.read_operation(reply_key)
    assert reply is not None
    assert reply.run_id == run_id
    assert _row_counts(runtime_url, thread_id) == (1, 2)
    assert graph.stream_calls == 2
    kinds = [entry[0] for entry in log]
    # The continuation reports on the same receipt; it never begins a new one.
    assert kinds.count("receipts.begin") == 1
    assert kinds.count("receipts.accepted_reply") == 1
    assert runtime_store.read_terminal(run_id) is not None

    replayed = client.post(
        f"/api/threads/{thread_id}/messages", json=reply_body
    )

    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("application/json")
    assert replayed.json()["status"] == "replayed"
    assert graph.stream_calls == 2
    assert _row_counts(runtime_url, thread_id) == (1, 2)


def test_a_resume_adds_one_operation_and_a_late_resume_is_a_terminal_conflict(
    runtime_url: str,
    runtime_store: PostgresRuntimeStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = f"run-{uuid.uuid4()}"
    log: list[tuple[Any, ...]] = []
    runtime = RuntimeCoordinator(runtime_store)
    store, thread_id = _thread()
    # The original send defaulted its operation key to the run id.
    runtime.accept(
        operation_id=run_id,
        thread_id=thread_id,
        accepted_input={"question": "original"},
        run_id=run_id,
    )
    lease = runtime.claim(
        thread_id=thread_id, worker_id="setup", run_id=run_id
    ).lease
    assert lease is not None
    runtime.wait(attempt_id=lease.attempt_id)
    graph = _DoorGraph(runtime, log)
    graph.pending = ("tools",)
    graph.checkpoint_run_id_value = run_id
    client = _client(
        runtime=runtime,
        graph=graph,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    # No client operation id: the resume keys itself on the run id.
    resumed = client.post(f"/api/threads/{thread_id}/resume")

    assert resumed.status_code == 200
    rows = _operation_rows(runtime_url, thread_id)
    assert len(rows) == 2
    assert rows[0] == (run_id, run_id)
    resume_key, resume_run = rows[1]
    # The resume's key carries the accepted input's digest.
    assert resume_key.startswith(f"{run_id}:resume:")
    assert resume_run == run_id
    record = runtime_store.read_operation(resume_key)
    assert record is not None
    assert record.run_id == run_id
    assert _row_counts(runtime_url, thread_id) == (1, 2)
    assert graph.stream_calls == 1
    assert runtime_store.read_terminal(run_id) is not None

    replayed = client.post(f"/api/threads/{thread_id}/resume")

    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("application/json")
    assert replayed.json()["status"] == "replayed"
    assert graph.stream_calls == 1
    assert _row_counts(runtime_url, thread_id) == (1, 2)

    late = client.post(
        f"/api/threads/{thread_id}/resume",
        json={"operation_id": f"op-late-{uuid.uuid4()}"},
    )

    assert late.status_code == 409
    assert late.json() == {"error": "run_terminal"}
    assert graph.stream_calls == 1


def test_a_second_resume_on_the_same_run_is_a_new_intent(
    runtime_url: str,
    runtime_store: PostgresRuntimeStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused run takes more than one resume; each pick is its own intent."""
    run_id = f"run-{uuid.uuid4()}"
    log: list[tuple[Any, ...]] = []
    runtime = RuntimeCoordinator(runtime_store)
    store, thread_id = _thread()
    runtime.accept(
        operation_id=run_id,
        thread_id=thread_id,
        accepted_input={"question": "original"},
        run_id=run_id,
    )
    lease = runtime.claim(thread_id=thread_id, worker_id="setup", run_id=run_id).lease
    assert lease is not None
    runtime.wait(attempt_id=lease.attempt_id)
    graph = _DoorGraph(runtime, log)
    graph.pending = ("tools",)
    graph.checkpoint_run_id_value = run_id
    graph.pending_choice = {"kind": "choice_window", "options": ["a", "b"]}
    # The first resume pauses again; the second one finishes the run.
    graph.pauses_remaining = 1
    client = _client(
        runtime=runtime,
        graph=graph,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    first = client.post(f"/api/threads/{thread_id}/resume", json={"pick": "a"})

    assert first.status_code == 200
    assert graph.stream_calls == 1
    assert runtime_store.read_terminal(run_id) is None
    snapshot = runtime_store.read_recovery_snapshot(run_id)
    assert snapshot is not None
    assert snapshot.run_state == RUN_WAITING

    second = client.post(f"/api/threads/{thread_id}/resume", json={"pick": "b"})

    assert second.status_code == 200
    assert graph.stream_calls == 2
    assert runtime_store.read_terminal(run_id) is not None

    # Both intents were accepted on the SAME run, each under its own key.
    rows = _operation_rows(runtime_url, thread_id)
    assert len(rows) == 3
    assert rows[0] == (run_id, run_id)
    assert all(run == run_id for _, run in rows)
    resume_keys = [key for key, _ in rows[1:]]
    assert all(key.startswith(f"{run_id}:resume:") for key in resume_keys)
    assert len(set(resume_keys)) == 2
    assert _row_counts(runtime_url, thread_id) == (1, 3)

    # The same body again is a replay: no stream, no fourth operation.
    replayed = client.post(f"/api/threads/{thread_id}/resume", json={"pick": "b"})

    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("application/json")
    assert replayed.json()["status"] == "replayed"
    assert graph.stream_calls == 2
    assert _row_counts(runtime_url, thread_id) == (1, 3)
