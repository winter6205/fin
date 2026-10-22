"""P1: the workspace door durably accepts before it starts any work.

The door is where an intent becomes a run. These cases pin the order and the
outcomes a lost response depends on: the operation is accepted BEFORE the
receipt and before any claim or graph work, a replay answers with the prior
record and starts nothing, a typed admission failure is a JSON status the
SPA can act on, and a refused claim leaves the acceptance intact for a later
worker. The coordinator here is a recorder, so no PostgreSQL and no model is
involved; the live-store ordering lives in ``test_runtime_door_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from fin.host import workspace_http
from fin.host.runtime.schema import (
    AcceptanceRecord,
    AttemptLease,
    ClaimOutcome,
    InvalidOperation,
    OperationConflict,
    QueueCapacityExceeded,
    RenewalOutcome,
    RunEndRecord,
    RuntimeUnavailable,
    ThreadFenceRecord,
    normalize_identity,
)
from fin.host.task.task_runtime_types import (
    PendingQuestion,
    TaskRuntimeState,
)
from fin.host.thread_store import InMemoryThreadStore
from fin.host.workspace_http import build_router

THREAD_ID = "thread-runtime-door"
PAUSED_RUN_ID = "run-paused-door"
ANSWER = "AAPL net sales were 81.8 billion dollars."


# ----- doubles -------------------------------------------------------------


class _FakeRuntimeStore:
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self._log = log
        self.terminals: dict[str, dict[str, Any]] = {}

    def commit_terminal(self, **kwargs: Any) -> None:
        self._log.append(("commit_terminal", kwargs["run_id"]))
        self.terminals[kwargs["run_id"]] = kwargs

    def read_terminal(self, run_id: str) -> dict[str, Any] | None:
        return self.terminals.get(run_id)


class _RecordingRuntime:
    """The coordinator surface the door uses, recording every decision."""

    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self.log = log
        self.store = _FakeRuntimeStore(log)
        self.accept_calls: list[dict[str, Any]] = []
        self.claim_calls: list[dict[str, Any]] = []
        self.accept_error: Exception | None = None
        self.accept_replay = False
        self.accept_result: dict[str, Any] | None = None
        self.claim_outcome: ClaimOutcome | None = None
        self.tombstones: list[str] = []

    def accept(self, **kwargs: Any) -> AcceptanceRecord:
        # The production coordinator normalizes the identity before anything
        # else, so an empty operation id is refused the same way here.
        operation_id = normalize_identity("operation_id", kwargs["operation_id"])
        self.log.append(("accept", operation_id, kwargs["thread_id"]))
        self.accept_calls.append(kwargs)
        if self.accept_error is not None:
            raise self.accept_error
        return AcceptanceRecord(
            operation_id=operation_id,
            thread_id=kwargs["thread_id"],
            run_id=kwargs["run_id"],
            fifo_position=1,
            payload_digest="digest",
            accepted_input=kwargs["accepted_input"],
            notification_intent=kwargs["notification_intent"] or {},
            result=self.accept_result,
            state="completed" if self.accept_replay else "accepted",
            created_at=datetime.now(timezone.utc),
            replay=self.accept_replay,
        )

    def claim(self, **kwargs: Any) -> ClaimOutcome:
        self.log.append(("claim", kwargs["run_id"]))
        self.claim_calls.append(kwargs)
        if self.claim_outcome is not None:
            return self.claim_outcome
        return ClaimOutcome(
            claimed=True,
            lease=AttemptLease(
                attempt_id=f"attempt-{kwargs['run_id']}",
                run_id=kwargs["run_id"],
                thread_id=kwargs["thread_id"],
                worker_id=kwargs["worker_id"],
                attempt_generation=1,
                thread_generation=1,
                lease_expires_at=datetime.now(timezone.utc)
                + timedelta(hours=1),
                state="active",
            ),
            reason=None,
        )

    def renew(self, *, attempt_id: str) -> RenewalOutcome:
        return RenewalOutcome(renewed=True, lease=None, reason=None)

    def wait(self, *, attempt_id: str) -> bool:
        self.log.append(("wait", attempt_id))
        return True

    def end(self, *, run_id: str, outcome: str) -> RunEndRecord:
        self.log.append(("end", run_id, outcome))
        return RunEndRecord(
            run_id=run_id,
            thread_id=THREAD_ID,
            state=outcome,
            reservation_released=True,
            attempts_released=1,
        )

    def tombstone_thread(self, *, thread_id: str) -> ThreadFenceRecord:
        self.log.append(("tombstone", thread_id))
        self.tombstones.append(thread_id)
        return ThreadFenceRecord(
            thread_id=thread_id,
            tombstoned=True,
            cancellation_reason=None,
            reservation_released=True,
            attempts_released=0,
        )


class _RecordingReceipts:
    def __init__(self, log: list[tuple[Any, ...]], store: InMemoryThreadStore) -> None:
        self._log = log
        self._store = store

    def begin(self, **kwargs: Any) -> None:
        self._log.append(("receipts.begin", kwargs["turn_id"]))

    def accepted_reply(self, **kwargs: Any) -> None:
        self._log.append(("receipts.accepted_reply", kwargs["turn_id"]))

    def resume_started(self, **kwargs: Any) -> None:
        self._log.append(("receipts.resume_started", kwargs["turn_id"]))

    def reconcile_accepted_inputs(self, **kwargs: Any) -> None:
        return None

    def paused(self, **kwargs: Any) -> None:
        self._log.append(("receipts.paused", kwargs["turn_id"]))

    def commit_terminal(self, **kwargs: Any) -> None:
        self._log.append(("turn_row", kwargs["run"].id))

    def stream_abandoned(self, **kwargs: Any) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self._log.append(("receipts.delete_thread", thread_id))
        self._store.delete_thread(thread_id)


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
    def __init__(
        self,
        runtime: _RecordingRuntime,
        *,
        log: list[tuple[Any, ...]],
        state: TaskRuntimeState | None = None,
        pending_nodes: tuple[str, ...] = (),
        checkpoint_run_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self._log = log
        self._state = state
        self._pending_nodes = pending_nodes
        self._checkpoint_run_id = checkpoint_run_id
        self.configs: list[dict[str, Any]] = []
        self.stream_calls = 0

    def stream(
        self, input_value: Any, *, config: dict[str, Any], stream_mode: Any
    ):
        self.configs.append(config)
        self.stream_calls += 1
        return self._drive()

    def _drive(self):
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
        return None

    def pending_nodes(self, thread_id: str) -> tuple[str, ...]:
        return self._pending_nodes

    def checkpoint_run_id(self, thread_id: str) -> str | None:
        return self._checkpoint_run_id

    def checkpoint_message_ids(self, thread_id: str) -> set[str]:
        return set()

    def task_runtime_state(self, thread_id: str) -> TaskRuntimeState:
        return self._state or TaskRuntimeState()

    def finalize_abandoned_run(
        self, thread_id: str, *, status: str, status_reason: str
    ) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self._log.append(("graph.delete_thread", thread_id))


# ----- harness -------------------------------------------------------------


def _client(
    *,
    runtime: _RecordingRuntime,
    store: InMemoryThreadStore,
    log: list[tuple[Any, ...]],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    graph: _DoorGraph | None = None,
) -> TestClient:
    receipts = _RecordingReceipts(log, store)
    monkeypatch.setattr(
        workspace_http, "default_receipts", lambda _store: receipts
    )
    app = FastAPI()
    app.include_router(
        build_router(
            get_graph=lambda: graph or _DoorGraph(runtime, log=log),
            get_thread_store=lambda: store,
            get_attachment_service=lambda: _FakeAttachments(),
            get_memory_port=lambda: _OffMemoryPort(),
            get_workspace_root=lambda: tmp_path,
        )
    )
    return TestClient(app)


def _store_with_thread() -> InMemoryThreadStore:
    store = InMemoryThreadStore()
    store.create_thread(id=THREAD_ID, title="runtime door")
    return store


def _kinds(log: list[tuple[Any, ...]]) -> list[str]:
    return [entry[0] for entry in log]


# ----- ordinary send -------------------------------------------------------


def test_the_operation_is_accepted_before_the_receipt_and_the_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(runtime, log=log)
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    kinds = _kinds(log)
    assert kinds.index("accept") < kinds.index("claim")
    assert kinds.index("accept") < kinds.index("receipts.begin")
    assert kinds.index("claim") < kinds.index("receipts.begin")
    accepted = runtime.accept_calls[0]
    assert accepted["operation_id"] == accepted["run_id"]
    assert accepted["accepted_input"] == {
        "question": "What was AAPL net sales in 2023 Q3?",
        "free_text": None,
        "choice_id": None,
        "question_id": None,
        "origin_run_id": None,
        "attachment_ids": [],
        "tiers": [],
        "thinking": True,
        "restore_archive_id": None,
        "propose_extract_method": None,
        "propose_document_id": None,
        "propose_version": None,
    }
    assert accepted["notification_intent"] == {"kind": "assistant_stream"}
    claimed = runtime.claim_calls[0]
    assert claimed["run_id"] == accepted["run_id"]
    assert claimed["worker_id"].startswith("workspace-http-")
    # The claimed attempt's identity reached the graph config.
    run_id = accepted["run_id"]
    injected = graph.configs[0]["configurable"]
    assert injected["runtime_run_id"] == run_id
    assert injected["runtime_attempt_id"] == f"attempt-{run_id}"
    assert injected["runtime_attempt_generation"] == 1
    assert injected["thread_generation"] == 1
    assert run_id in runtime.store.terminals
    assert store.list_runs(THREAD_ID)


def test_a_supplied_operation_id_keys_the_durable_operation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"question": "hello", "operation_id": "op-client-1"},
    )

    assert runtime.accept_calls[0]["operation_id"] == "op-client-1"


def test_an_empty_operation_id_is_refused_and_never_replaced(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty identity is invalid input, not a missing one."""
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(runtime, log=log)
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"question": "hello", "operation_id": ""},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_operation",
        "field": "operation_id",
    }
    # Nothing was accepted under a substituted key and no run started.
    assert runtime.accept_calls == []
    assert graph.stream_calls == 0
    assert "claim" not in _kinds(log)


@pytest.mark.parametrize(
    ("error", "status", "body"),
    [
        (OperationConflict("op-1"), 409, {"error": "operation_conflict"}),
        (
            QueueCapacityExceeded(THREAD_ID, 1),
            429,
            {"error": "queue_capacity_exceeded"},
        ),
        (
            InvalidOperation("operation_id", "empty"),
            400,
            {"error": "invalid_operation", "field": "operation_id"},
        ),
        (
            InvalidOperation("run_id", "terminal"),
            409,
            {"error": "run_terminal"},
        ),
        (RuntimeUnavailable("accept"), 503, {"error": "runtime_unavailable"}),
    ],
)
def test_a_typed_admission_failure_answers_json_before_any_stream(
    error: Exception,
    status: int,
    body: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    runtime.accept_error = error
    store = _store_with_thread()
    graph = _DoorGraph(runtime, log=log)
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/messages", json={"question": "hello"}
    )

    assert response.status_code == status
    assert response.json() == body
    assert response.headers["content-type"].startswith("application/json")
    assert "claim" not in _kinds(log)
    assert "receipts.begin" not in _kinds(log)
    assert graph.stream_calls == 0


def test_a_replay_answers_with_the_prior_record_and_starts_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    runtime.accept_replay = True
    runtime.accept_result = {"status": "done", "answer": ANSWER}
    store = _store_with_thread()
    graph = _DoorGraph(runtime, log=log)
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"question": "hello", "operation_id": "op-replay"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "replayed"
    assert body["state"] == "completed"
    assert body["result"] == {"status": "done", "answer": ANSWER}
    assert body["fifo_position"] == 1
    assert "claim" not in _kinds(log)
    assert "receipts.begin" not in _kinds(log)
    assert graph.stream_calls == 0
    assert store.list_runs(THREAD_ID) == []


def test_a_refused_claim_leaves_the_acceptance_and_starts_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    runtime.claim_outcome = ClaimOutcome(
        claimed=False, lease=None, reason="reserved_by_other_run"
    )
    store = _store_with_thread()
    graph = _DoorGraph(runtime, log=log)
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/messages", json={"question": "hello"}
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "run_not_claimable",
        "reason": "reserved_by_other_run",
    }
    assert len(runtime.accept_calls) == 1
    assert "receipts.begin" not in _kinds(log)
    assert graph.stream_calls == 0
    assert store.list_runs(THREAD_ID) == []


# ----- continuation and resume --------------------------------------------


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


def test_a_continued_reply_is_accepted_and_claims_the_paused_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(
        runtime,
        log=log,
        state=_pending_state(),
        pending_nodes=("clarify",),
        checkpoint_run_id=PAUSED_RUN_ID,
    )
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={
            "question": "a",
            "question_id": "q-1",
            "choice_id": "a",
            "operation_id": "op-reply",
        },
    )

    assert response.status_code == 200
    kinds = _kinds(log)
    assert kinds.index("accept") < kinds.index("receipts.accepted_reply")
    accepted = runtime.accept_calls[0]
    assert accepted["operation_id"] == "op-reply"
    assert accepted["run_id"] == PAUSED_RUN_ID
    assert accepted["accepted_input"]["choice_id"] == "a"
    assert runtime.claim_calls[0]["run_id"] == PAUSED_RUN_ID
    injected = graph.configs[0]["configurable"]
    assert injected["runtime_attempt_id"] == f"attempt-{PAUSED_RUN_ID}"
    assert PAUSED_RUN_ID in runtime.store.terminals


def test_a_reply_without_a_client_operation_id_gets_its_own_key_and_replays(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-P1 UI sends no operation id; a reply must still be accepted."""
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(
        runtime,
        log=log,
        state=_pending_state(),
        pending_nodes=("clarify",),
        checkpoint_run_id=PAUSED_RUN_ID,
    )
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )
    body = {"question": "a", "question_id": "q-1", "choice_id": "a"}

    assert (
        client.post(f"/api/threads/{THREAD_ID}/messages", json=body).status_code
        == 200
    )

    runtime.accept_replay = True
    runtime.accept_result = {"state": "completed"}
    replayed = client.post(f"/api/threads/{THREAD_ID}/messages", json=body)

    assert replayed.status_code == 200
    assert replayed.json()["status"] == "replayed"
    keys = {call["operation_id"] for call in runtime.accept_calls}
    assert len(keys) == 1
    key = keys.pop()
    # The reply's key carries the accepted input's digest.
    assert key.startswith(f"{PAUSED_RUN_ID}:reply:")
    assert graph.stream_calls == 1


def test_a_continued_reply_replay_starts_no_second_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(
        runtime,
        log=log,
        state=_pending_state(),
        pending_nodes=("clarify",),
        checkpoint_run_id=PAUSED_RUN_ID,
    )
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )
    body = {
        "question": "a",
        "question_id": "q-1",
        "choice_id": "a",
        "operation_id": "op-reply",
    }
    assert (
        client.post(f"/api/threads/{THREAD_ID}/messages", json=body).status_code
        == 200
    )

    runtime.accept_replay = True
    runtime.accept_result = {"state": "completed"}
    replayed = client.post(f"/api/threads/{THREAD_ID}/messages", json=body)

    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("application/json")
    assert replayed.json()["status"] == "replayed"
    assert graph.stream_calls == 1
    kinds = _kinds(log)
    assert kinds.count("receipts.accepted_reply") == 1
    assert "receipts.begin" not in kinds
    assert len(runtime.accept_calls) == 2
    assert {call["run_id"] for call in runtime.accept_calls} == {PAUSED_RUN_ID}


def test_a_supplied_operation_id_wins_on_resume(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(
        runtime,
        log=log,
        pending_nodes=("tools",),
        checkpoint_run_id=PAUSED_RUN_ID,
    )
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(
        f"/api/threads/{THREAD_ID}/resume",
        json={"operation_id": "op-resume-client"},
    )

    assert response.status_code == 200
    assert runtime.accept_calls[0]["operation_id"] == "op-resume-client"


def test_a_resume_replay_starts_no_second_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(
        runtime,
        log=log,
        pending_nodes=("tools",),
        checkpoint_run_id=PAUSED_RUN_ID,
    )
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )
    assert client.post(f"/api/threads/{THREAD_ID}/resume").status_code == 200

    runtime.accept_replay = True
    runtime.accept_result = {"state": "completed"}
    replayed = client.post(f"/api/threads/{THREAD_ID}/resume")

    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("application/json")
    assert replayed.json()["status"] == "replayed"
    assert graph.stream_calls == 1
    assert _kinds(log).count("receipts.resume_started") == 1
    assert len(runtime.accept_calls) == 2
    assert {call["run_id"] for call in runtime.accept_calls} == {PAUSED_RUN_ID}


def test_the_background_resume_keys_its_own_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-resumer must not collide with the operator resume's key."""
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(runtime, log=log)
    monkeypatch.setattr(
        workspace_http, "default_receipts", lambda _store: _RecordingReceipts(log, store)
    )

    frames = list(
        workspace_http._resume_run_stream(
            graph,
            store,
            run_control=workspace_http.RunControl(),
            thread_id=THREAD_ID,
            run_id=PAUSED_RUN_ID,
            resume_input=None,
            resume_config={"configurable": {"thread_id": THREAD_ID}},
        )
    )

    assert frames
    accepted = runtime.accept_calls[0]
    # The key carries the digest of the input the resumer accepted.
    assert accepted["operation_id"].startswith(
        f"{PAUSED_RUN_ID}:auto-resume:"
    )
    assert accepted["accepted_input"] == {"kind": "resume", "resume": "None"}
    assert runtime.claim_calls[0]["run_id"] == PAUSED_RUN_ID


def test_a_resume_is_accepted_and_claimed_under_the_checkpoint_run_id(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    graph = _DoorGraph(
        runtime,
        log=log,
        pending_nodes=("tools",),
        checkpoint_run_id=PAUSED_RUN_ID,
    )
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        graph=graph,
    )

    response = client.post(f"/api/threads/{THREAD_ID}/resume")

    assert response.status_code == 200
    accepted = runtime.accept_calls[0]
    assert accepted["operation_id"].startswith(f"{PAUSED_RUN_ID}:resume:")
    assert accepted["run_id"] == PAUSED_RUN_ID
    assert accepted["accepted_input"] == {"pick": None, "free_text": None}
    assert runtime.claim_calls[0]["run_id"] == PAUSED_RUN_ID
    assert graph.configs[0]["configurable"]["runtime_run_id"] == PAUSED_RUN_ID
    assert PAUSED_RUN_ID in runtime.store.terminals


# ----- delete --------------------------------------------------------------


def test_delete_tombstones_the_thread_before_removing_history(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)
    store = _store_with_thread()
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    response = client.delete(f"/api/threads/{THREAD_ID}")

    assert response.status_code == 200
    kinds = _kinds(log)
    assert kinds.index("tombstone") < kinds.index("receipts.delete_thread")
    assert kinds.index("tombstone") < kinds.index("graph.delete_thread")
    assert runtime.tombstones == [THREAD_ID]


def test_a_tombstone_failure_refuses_the_delete(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    runtime = _RecordingRuntime(log)

    def _fail(*, thread_id: str) -> ThreadFenceRecord:
        raise RuntimeUnavailable("tombstone_thread")

    runtime.tombstone_thread = _fail  # type: ignore[method-assign]
    store = _store_with_thread()
    client = _client(
        runtime=runtime,
        store=store,
        log=log,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    response = client.delete(f"/api/threads/{THREAD_ID}")

    assert response.status_code == 500
    assert response.json() == {"error": "thread_delete_failed"}
    assert "receipts.delete_thread" not in _kinds(log)
    assert store.get_thread(THREAD_ID) is not None


def test_the_accepted_input_mirrors_every_send_field() -> None:
    """A send field that never reaches the digest cannot be distinguished.

    ``accept_operation`` compares the accepted input's digest, so a field
    the door forgets here silently turns a changed intent into a replay.
    Pin the mirror against the request model instead of a hand-kept list.
    """
    from fin.host.workspace_http import SendMessageRequest, _runtime_send_input

    body = SendMessageRequest(question="q")
    accepted = _runtime_send_input(body)
    declared = set(SendMessageRequest.model_fields) - {"operation_id"}

    assert set(accepted) == declared
