from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.host.evidence.evidence_session_registry import (
    EvidenceSessionBinding,
    EvidenceSessionRegistry,
)
from fin.host.graph import _identity_bound_adapter
from fin.host.run_call_supervisor import run_call_context
from fin.host.run_control import StopToken
from fin.retrieve.evidence_route import EvidenceIdentityBinding, deadline_within
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_session import (
    MCPStdioCancelledError,
    MCPStdioSession,
)


_QUEUED_OWNER_SERVER = r"""
import json
import pathlib
import sys
import time

active = pathlib.Path(sys.argv[1])
release = pathlib.Path(sys.argv[2])
for raw in sys.stdin:
    request = json.loads(raw)
    if request.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
        continue
    if request.get("method") != "tools/call":
        continue
    if request["id"] == 10:
        active.write_text("owner", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
"""


_SOURCE_READ_SERVER = r"""
import json
import pathlib
import sys
import time

counter = pathlib.Path(sys.argv[1])
blocked = pathlib.Path(sys.argv[2])
for raw in sys.stdin:
    request = json.loads(raw)
    if request.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
        continue
    if request.get("method") != "tools/call":
        continue
    call_number = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
    counter.write_text(str(call_number), encoding="utf-8")
    if call_number == 1:
        blocked.write_text("read_source", encoding="utf-8")
        while True:
            time.sleep(0.01)
    result = {
        "content": [{
            "type": "text",
            "text": json.dumps({"content": "recovered source", "next_position": None}),
        }]
    }
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def _wait_for(path: Path, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return
        time.sleep(0.01)
    pytest.fail("the local MCP child did not reach the requested barrier")


def _result(future: Future[Any], error_type: type[BaseException]) -> BaseException:
    with pytest.raises(error_type) as raised:
        future.result(timeout=2)
    return raised.value


def test_cancelling_a_queued_call_preserves_the_active_real_child(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active-owner"
    release = tmp_path / "release-owner"
    session = MCPStdioSession(
        [sys.executable, "-u", "-c", _QUEUED_OWNER_SERVER, str(active), str(release)],
        timeout_seconds=4,
        env={},
    )
    cancel_queued = threading.Event()
    queued_started = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            owner = executor.submit(
                session.call,
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {},
                },
            )
            _wait_for(active)
            process = session._process
            assert process is not None and process.poll() is None

            def queued_call() -> dict[str, Any]:
                queued_started.set()
                return session.call(
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {},
                    },
                    cancel_event=cancel_queued,
                )

            queued = executor.submit(queued_call)
            assert queued_started.wait(1)
            time.sleep(0.1)
            cancel_queued.set()
            _result(queued, MCPStdioCancelledError)

            assert process.poll() is None
            release.write_text("continue", encoding="utf-8")
            assert owner.result(timeout=2)["id"] == 10
            assert not session.is_spent

            # The same child remains useful after the cancelled waiter leaves.
            assert session.call(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {},
                }
            )["id"] == 12
            assert session._process is process
            assert [item["id"] for item in session.requests] == [10, 12]
    finally:
        release.write_text("continue", encoding="utf-8")
        session.close()


def test_disconnect_cancellation_is_not_stalled_by_identity_binding(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active-owner"
    release = tmp_path / "release-owner"
    registry = EvidenceSessionRegistry(
        [sys.executable, "-u", "-c", _QUEUED_OWNER_SERVER, str(active), str(release)],
        transport_env={"LOCAL_TEST_TRANSPORT": "enabled"},
        timeout_seconds=4,
    )
    binding = EvidenceIdentityBinding(
        actor_id="recovery-reader",
        owner_scope_binding="recovery-scope",
        reader_binding="recovery-reader",
        source_refs=("synthetic-source",),
        authorization_revision="recovery-revision",
        deadline_at=deadline_within(60),
    )
    evidence = EvidenceSessionBinding(
        sessions=registry,
        resolve=lambda _config: binding,
    )
    token = StopToken(thread_id="recovery-thread", run_id="second-run")
    adapter = MCPRetrieveAdapter()
    source_ref = {
        "document_id": "DOC-2023Q1",
        "version": "v-" + "a" * 60,
        "unit": 1,
    }
    bind_started = threading.Event()
    blocked_by_active_owner = False
    bound_adapter: MCPRetrieveAdapter | None = None

    try:
        first = registry.bind(binding)
        with ThreadPoolExecutor(max_workers=2) as executor:
            owner = executor.submit(
                first.session.call,
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {},
                },
            )
            _wait_for(active)
            process = first.session._process
            assert process is not None and process.poll() is None

            def bind_second_run() -> MCPRetrieveAdapter:
                with run_call_context(token, None):
                    bind_started.set()
                    return _identity_bound_adapter(
                        adapter,
                        evidence,
                        {"configurable": {"thread_id": "recovery-thread"}},
                    )  # type: ignore[return-value]

            bind_started_at = time.monotonic()
            second = executor.submit(bind_second_run)
            assert bind_started.wait(1)
            token.stop(reason="client_disconnect")
            try:
                bound_adapter = second.result(timeout=0.25)
            except TimeoutError:
                blocked_by_active_owner = True

            if bound_adapter is not None:
                with pytest.raises(MCPStdioCancelledError):
                    bound_adapter.read_slice(source_ref)
                assert process.poll() is None

            release.write_text("continue", encoding="utf-8")
            assert owner.result(timeout=2)["id"] == 10
            if bound_adapter is None:
                bound_adapter = second.result(timeout=2)

        assert bound_adapter is not None
        assert first.session is registry.bind(binding).session
    finally:
        release.write_text("continue", encoding="utf-8")
        registry.close_all()

    elapsed_at_bound = time.monotonic() - bind_started_at
    assert not blocked_by_active_owner, (
        "identity binding waited at least 250ms on the active owner's MCP lock "
        "after disconnect; run cancellation could not reach the adapter during "
        f"that wait (total observed bind time {elapsed_at_bound:.3f}s)"
    )


def test_cancelled_source_read_replaces_spent_registry_session_and_recovers(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "source-call-count"
    blocked = tmp_path / "source-read-blocked"
    command = [
        sys.executable,
        "-u",
        "-c",
        _SOURCE_READ_SERVER,
        str(counter),
        str(blocked),
    ]
    registry = EvidenceSessionRegistry(
        command,
        transport_env={"LOCAL_TEST_TRANSPORT": "enabled"},
        timeout_seconds=4,
    )
    binding = EvidenceIdentityBinding(
        actor_id="recovery-reader",
        owner_scope_binding="recovery-scope",
        reader_binding="recovery-reader",
        source_refs=("synthetic-source",),
        authorization_revision="recovery-revision",
        deadline_at=deadline_within(60),
    )
    source_ref = {
        "document_id": "DOC-2023Q1",
        "version": "v-" + "a" * 60,
        "unit": 1,
    }
    adapter = MCPRetrieveAdapter()
    cancel_read = threading.Event()
    first_process = None

    try:
        first = registry.bind(binding)
        first_process = first.session._process
        bound_adapter = first.adapter_for(adapter).with_run_control(
            deadline=time.monotonic() + 5,
            cancel_event=cancel_read,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            read = executor.submit(bound_adapter.read_slice, source_ref)
            _wait_for(blocked)
            first_process = first.session._process
            assert first_process is not None and first_process.poll() is None
            cancel_read.set()
            _result(read, MCPStdioCancelledError)

        assert first.session.is_spent
        assert first_process.poll() is not None

        replacement = registry.bind(binding)
        assert replacement.session is not first.session
        assert registry.bound_identities == 1
        recovered = replacement.adapter_for(adapter).with_run_control(
            deadline=time.monotonic() + 5,
            cancel_event=threading.Event(),
        )
        result = recovered.read_slice(source_ref)
        assert result == {
            "source_ref": source_ref,
            "text": "recovered source",
            "next_position": None,
        }
        replacement_process = replacement.session._process
        assert replacement_process is not None and replacement_process.poll() is None
    finally:
        registry.close_all()

    assert first_process is not None and first_process.poll() is not None
    if "replacement_process" in locals():
        assert replacement_process.poll() is not None
