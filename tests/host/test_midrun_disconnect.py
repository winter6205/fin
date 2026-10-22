"""T11 characterization of a client disconnect during blocked sync graph work."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, ToolMessage

from fin.host.run_control import RunAdmissionError
from fin.host.task.task_runtime_types import TaskRuntimeState
from fin.host.thread_store import InMemoryThreadStore


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


class _BlockedGraph:
    """Graph protocol stand-in with a real synchronous blocked iterator."""

    def __init__(self) -> None:
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self._lock = threading.Lock()
        self._locked = False

    def stream(self, input_value: Any, *, config: dict, stream_mode: Any):
        with self._lock:
            self._locked = True
        try:
            yield (
                "values",
                {
                    "messages": [
                        ToolMessage(
                            content="working",
                            name="read_source",
                            tool_call_id="blocked-read",
                            id="blocked-result",
                        )
                    ]
                },
            )
            self.blocked.set()
            self.release.wait(timeout=10)
            yield (
                "values",
                {
                    "messages": [
                        AIMessage(content="late answer", id="late-answer")
                    ],
                    "status": "done",
                    "status_reason": "done",
                    "answer": "late answer",
                    "citations": [],
                },
            )
        finally:
            with self._lock:
                self._locked = False
            self.closed.set()

    def is_locked(self, thread_id: str) -> bool:
        with self._lock:
            return self._locked

    def task_runtime_state(self, thread_id: str) -> TaskRuntimeState:
        return TaskRuntimeState()

    def checkpoint_message_ids(self, thread_id: str) -> set[str]:
        return set()

    def interrupt_pending(self, thread_id: str) -> None:
        return None


def test_midrun_disconnect_stops_blocked_work_without_releasing_its_owner(
    http_app: FastAPI,
    tmp_path,
) -> None:
    """A disconnected client stops the run, while its sync graph call still owns the thread."""
    from fin.host.http import get_graph, get_thread_store, get_workspace_root

    graph = _BlockedGraph()
    fresh_store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: fresh_store
    http_app.dependency_overrides[get_graph] = lambda: graph
    http_app.dependency_overrides[get_workspace_root] = lambda: tmp_path
    thread = fresh_store.create_thread(title="disconnect")
    control = http_app.state.fin_resources.run_control

    async def exercise_disconnect() -> tuple[bool, bool, bool]:
        disconnected = asyncio.Event()
        received_disconnect = asyncio.Event()
        body_sent = asyncio.Event()
        request_message = {
            "type": "http.request",
            "body": json.dumps({"question": "synthetic question"}).encode(),
            "more_body": False,
        }

        async def receive() -> dict[str, Any]:
            nonlocal request_message
            if request_message is not None:
                message = request_message
                request_message = None
                return message
            await disconnected.wait()
            received_disconnect.set()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                body_sent.set()

        path = f"/api/threads/{thread.id}/messages"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        request_task = asyncio.create_task(http_app(scope, receive, send))
        stop_reached_run = False
        ownership_retained = False
        try:
            assert await asyncio.to_thread(graph.blocked.wait, 5), "graph did not block"
            await asyncio.wait_for(body_sent.wait(), timeout=5)
            disconnected.set()
            await asyncio.wait_for(received_disconnect.wait(), timeout=5)

            with control._lock:
                token = control._tokens.get(thread.id)
            assert token is not None, "the run was deregistered before blocked work unwound"
            stop_reached_run = token.is_set()
            try:
                claim = control.admit(thread.id)
            except RunAdmissionError:
                ownership_retained = True
            else:
                ownership_retained = False
                claim.release()
        finally:
            graph.release.set()
            try:
                await asyncio.wait_for(request_task, timeout=10)
            except asyncio.CancelledError:
                pass
        late_answer_written = any(
            run.answer == "late answer" for run in fresh_store.list_runs(thread.id)
        )
        return stop_reached_run, ownership_retained, late_answer_written

    stop_reached_run, ownership_retained, late_answer_written = asyncio.run(
        exercise_disconnect()
    )
    assert graph.closed.is_set(), "the blocked worker was not released during test cleanup"
    assert ownership_retained, "the thread admission was released while graph work was blocked"
    assert not late_answer_written, "the disconnected run wrote its answer after the client left"
    assert stop_reached_run, "http.disconnect did not set the active run's stop signal"


def test_delayed_disconnect_does_not_stop_a_replacement_token_with_same_run_id() -> None:
    from fin.host.run_control import RunControl
    from fin.host.workspace_http import _disconnect_stop

    control = RunControl()
    old = control.begin("t-1", "same-run")
    on_disconnect = _disconnect_stop(old)
    control.end(old)
    current = control.begin("t-1", "same-run")

    on_disconnect()

    assert old.is_set() is True
    assert old.reason == "client_disconnect"
    assert current.is_set() is False


def test_completed_response_does_not_record_abandonment() -> None:
    from fin.host.run_control import RunControl
    from fin.host.workspace_http import _ClaimedStreamingResponse

    control = RunControl()
    admission = control.admit("t-1")
    abandoned: list[bool] = []
    response = _ClaimedStreamingResponse(
        iter([b"complete"]),
        status_code=200,
        admission=admission,
        abandonment=lambda: abandoned.append(True),
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        return None

    asyncio.run(response(scope, receive, send))

    assert abandoned == []
    next_admission = control.admit("t-1")
    next_admission.release()
