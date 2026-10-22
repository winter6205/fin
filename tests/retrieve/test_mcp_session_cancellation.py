from __future__ import annotations

import asyncio
import math
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from fin.retrieve.mcp_session import (
    MCPStdioCancelledError,
    MCPStdioDeadlineExceededError,
    MCPStdioSession,
    MCPStdioTransportError,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


_SERVER = r"""
import json
import pathlib
import sys
import time

marker = None if sys.argv[1] == "-" else pathlib.Path(sys.argv[1])
mode = sys.argv[2]
for raw in sys.stdin:
    request = json.loads(raw)
    if request.get("method") == "initialize":
        if mode == "slow_initialize":
            time.sleep(30)
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
    elif request.get("method") == "tools/call":
        if marker is not None:
            marker.write_text("called", encoding="utf-8")
        if mode == "hold_response":
            time.sleep(30)
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
"""


def _session(tmp_path: Path, mode: str = "respond") -> MCPStdioSession:
    marker = tmp_path / "called"
    return MCPStdioSession(
        [sys.executable, "-u", "-c", _SERVER, str(marker), mode],
        timeout_seconds=10,
        env={},
    )


def _payload(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {},
    }


def _wait_for(path: Path, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return
        time.sleep(0.01)
    pytest.fail("child did not receive the tools/call request")


def _future_error(future: Future[Any], error_type: type[BaseException]) -> BaseException:
    with pytest.raises(error_type) as raised:
        future.result(timeout=2)
    return raised.value


def test_deadline_expiring_while_queued_does_not_start_child(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    session._io_lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            started = threading.Event()

            def call() -> dict[str, Any]:
                started.set()
                return session.call(
                    _payload(2),
                    deadline=time.monotonic() + 0.1,
                )

            future = executor.submit(call)
            assert started.wait(1)
            _future_error(future, MCPStdioDeadlineExceededError)
            assert session._process is None
    finally:
        session._io_lock.release()
        session.close()


def test_cancellation_while_queued_does_not_start_child(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    cancel_event = threading.Event()
    session._io_lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            started = threading.Event()

            def call() -> dict[str, Any]:
                started.set()
                return session.call(
                    _payload(2),
                    cancel_event=cancel_event,
                )

            future = executor.submit(call)
            assert started.wait(1)
            time.sleep(0.05)
            cancel_event.set()
            _future_error(future, MCPStdioCancelledError)
            assert session._process is None
            assert not session.is_spent
    finally:
        session._io_lock.release()
        session.close()


def test_inflight_cancellation_kills_owned_child_and_spends_session(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "called"
    session = _session(tmp_path, "hold_response")
    cancel_event = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                session.call,
                _payload(2),
                cancel_event=cancel_event,
            )
            _wait_for(marker)
            process = session._process
            assert process is not None

            cancel_event.set()
            _future_error(future, MCPStdioCancelledError)

        assert process.poll() is not None
        assert session.is_spent
        with pytest.raises(MCPStdioTransportError, match="failed"):
            session.call(_payload(3))
    finally:
        session.close()


def test_deadline_covers_initialize_handshake_and_stops_child(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, "slow_initialize")
    try:
        started = time.monotonic()
        with pytest.raises(MCPStdioDeadlineExceededError):
            session.call(
                _payload(2),
                deadline=started + 0.15,
            )
        assert time.monotonic() - started < 2
        assert session.is_spent
    finally:
        session.close()


@pytest.mark.parametrize("deadline", [math.nan, math.inf, -math.inf])
def test_nonfinite_deadline_is_rejected_before_starting_child(
    tmp_path: Path, deadline: float
) -> None:
    session = _session(tmp_path)
    try:
        with pytest.raises(ValueError, match="finite"):
            session.call(_payload(2), deadline=deadline)
        assert session._process is None
    finally:
        session.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [("deadline", True), ("deadline", "1.0"), ("cancel_event", object())],
)
def test_invalid_run_control_types_are_rejected_before_starting_child(
    tmp_path: Path, name: str, value: object
) -> None:
    session = _session(tmp_path)
    try:
        with pytest.raises(TypeError):
            session.call(_payload(2), **{name: value})
        assert session._process is None
    finally:
        session.close()


def test_expired_deadline_does_not_start_child(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        with pytest.raises(MCPStdioDeadlineExceededError):
            session.call(
                _payload(2),
                deadline=time.monotonic() - 1,
            )
        assert session._process is None
    finally:
        session.close()


def test_call_without_run_control_keeps_existing_session_behavior(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    try:
        response = session.call(_payload(2), 2)
        assert response == {"jsonrpc": "2.0", "id": 2, "result": {}}
    finally:
        session.close()


def test_is_spent_probe_does_not_wait_for_an_active_io_owner(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    owner_ready = threading.Event()
    release_owner = threading.Event()

    def hold_io_lock() -> None:
        with session._io_lock:
            owner_ready.set()
            assert release_owner.wait(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(hold_io_lock)
        assert owner_ready.wait(1)
        probe = executor.submit(lambda: session.is_spent)
        try:
            assert probe.result(timeout=0.25) is False
        finally:
            release_owner.set()
            owner.result(timeout=2)
    session.close()


def test_is_spent_still_detects_terminal_flags_and_exited_child(
    tmp_path: Path,
) -> None:
    closed = _session(tmp_path)
    closed.close()
    assert closed.is_spent

    failed = _session(tmp_path)
    failed._failed = True
    assert failed.is_spent

    exited = _session(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=2)
    exited._process = process
    try:
        assert exited.is_spent
    finally:
        exited.close()


class _RecordingSession(MCPStdioSession):
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[float | None, threading.Event | None]] = []
        self.error = error

    def call(
        self,
        payload: dict[str, Any],
        timeout_seconds: float | None = None,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        self.calls.append((deadline, cancel_event))
        if self.error is not None:
            raise self.error
        return {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {"chunks": []},
        }


def test_run_control_is_bound_to_adapter_copy_and_reaches_mcp_session() -> None:
    session = _RecordingSession()
    adapter = MCPRetrieveAdapter(session=session)
    cancel_event = threading.Event()
    deadline = time.monotonic() + 5

    bound = adapter.with_run_control(
        deadline=deadline, cancel_event=cancel_event
    )
    # A successful result carries the spec's ``distribution`` key beside
    # the chunks (spec qa-chain-corpus-retrieval §"Result, error,
    # readiness, and degradation wire"): no usable chunk is ``zero``.
    assert bound.retrieve("query") == {"chunks": [], "distribution": "zero"}

    assert session.calls == [(deadline, cancel_event)]
    assert adapter._run_deadline is None
    assert adapter._run_cancel_event is None


def test_read_slice_propagates_run_cancellation_instead_of_source_miss() -> None:
    session = _RecordingSession(MCPStdioCancelledError("cancelled"))
    adapter = MCPRetrieveAdapter(session=session).with_run_control(
        cancel_event=threading.Event()
    )

    with pytest.raises(MCPStdioCancelledError):
        adapter.read_slice(
            {"document_id": "DOC-2023Q1", "version": "v1", "unit": 1}
        )


def test_run_control_cancels_the_default_adapter_transport(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "called"
    adapter = MCPRetrieveAdapter(
        command=[
            sys.executable,
            "-u",
            "-c",
            _SERVER,
            str(marker),
            "hold_response",
        ],
        timeout_seconds=10,
    )
    cancel_event = threading.Event()
    bound = adapter.with_run_control(cancel_event=cancel_event)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(bound.retrieve, "query")
        _wait_for(marker)
        cancel_event.set()
        _future_error(future, MCPStdioCancelledError)
