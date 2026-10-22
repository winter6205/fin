"""The one-shot MCP ports against a LONG-LIVED stdio server.

Why this module exists
----------------------

``mcp_adapter``, ``mcp_push_port``, ``mcp_invalidation_port`` and
``mcp_index_job_port`` open a fresh child per call, write the whole
handshake, and read the frames the protocol owes. Each of those four
transports once used ``Process.communicate()`` — the three async
ports through ``asyncio.subprocess``, the index-job port through
blocking ``subprocess.Popen`` — and ``communicate()`` does not return
when the response arrives: it returns when the child EXITS.

A conforming MCP stdio server answers and then keeps serving, so
``communicate()`` never returned and every real call raised
``TimeoutError``. No code path through those four ports could succeed
against a real server.

The defect survived because every existing test overrode the
``_call_mcp`` seam, so the transport underneath was never executed —
and the ``mcp``-marked smoke spawns a fixture that exits at stdin EOF,
which is precisely the one shape where ``communicate()`` does return.

This module drives the real transports against
``mcp_fixture_server.py --stay-alive``, the exact real-world shape: a
server that answers and then runs on. Every test here is opt-in ``mcp``
(no network, no live iknow-memory, no database — just a local
subprocess), consistent with ``tests/retrieve/conftest.py``.

What is pinned
--------------

* The positive case: a real round trip over a still-running server
  parses and returns, without timing out.
* A live child is NOT a failure: the frames are accepted while the
  child is still up, and the transport does not invent a non-zero exit.
* Cleanup: no child is leaked per call, on success or on timeout.
* A genuine non-zero exit is still an error.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.invalidation import (
    REASON_UNIT_VERSION_CHANGED,
    InvalidationTarget,
)
from fin.retrieve.invalidation_port import InvalidationServiceUnavailableError
from fin.retrieve.mcp_adapter import (
    MCPRetrieveAdapter,
    _build_mcp_wire_messages,
    _exchange_mcp_messages,
    _launch_mcp_process,
    _parse_mcp_responses,
)
from fin.retrieve.mcp_invalidation_port import MCPInvalidationPort
from fin.retrieve.mcp_index_job_port import MCPIndexJobPort
from fin.retrieve.mcp_push_port import MCPPushPort
from fin.retrieve.mcp_session import (
    exchange_oneshot_mcp,
    launch_oneshot_mcp_process,
)
from fin.retrieve.index_job_port import IndexJobUnavailableError
from fin.retrieve.push_port import PushServiceUnavailableError

pytestmark = pytest.mark.mcp


_SERVER_PATH = Path(__file__).with_name("mcp_fixture_server.py")

#: Generous enough for a loaded box, short enough that a transport that
#: waits for exit fails the test instead of hanging the suite. It is
#: deliberately far above the real cost of a fixture round trip (~50ms),
#: so a passing test cannot be explained by the budget being generous.
_TIMEOUT_SECONDS = 20.0


def _command(*extra: str) -> list[str]:
    """The launch command for a long-lived fixture server."""

    return [sys.executable, str(_SERVER_PATH), "--stay-alive", *extra]


# ----- retrieve port -----

def _loc(document_id: str, unit: int = 1) -> Locator:
    return Locator(document_id=document_id, version="v-" + "a" * 60, unit=unit)


def _retrieve_adapter(**kwargs: Any) -> MCPRetrieveAdapter:
    return MCPRetrieveAdapter(
        command=_command(),
        locator_map={
            "fixture-1": _loc("DOC-2023Q1", unit=1),
            "fixture-2": _loc("DOC-2023Q1", unit=2),
            "fixture-3": _loc("DOC-2023Q2", unit=1),
        },
        timeout_seconds=_TIMEOUT_SECONDS,
        **kwargs,
    )


def test_retrieve_answers_from_a_server_that_keeps_running() -> None:
    """The retrieve port completes a real round trip against a child
    that is still serving when the response lands.

    Under the old ``communicate()`` transport this raised
    ``TimeoutError`` after ``_TIMEOUT_SECONDS`` and surfaced as
    ``{"error": "retrieve_unavailable"}``.
    """
    adapter = _retrieve_adapter()

    result = adapter.retrieve("revenue", limit=5)

    assert "chunks" in result, result
    chunks = result["chunks"]
    assert chunks, "the fixture corpus matches 'revenue'"
    assert all(isinstance(chunk.source_ref, Locator) for chunk in chunks)


def test_retrieve_does_not_treat_a_running_child_as_a_failure() -> None:
    """A live child that answered correctly is the NORMAL case.

    The transport used to read ``proc.returncode != 0``, which is
    ``None`` for any child still running — so this assertion pins that
    "still running" is no longer on the failure path at all, by proving
    the round trip succeeds against a server that never exits.
    """
    adapter = _retrieve_adapter()

    # Two calls in a row: each spawns its own child, and neither may
    # depend on the previous one having exited.
    first = adapter.retrieve("revenue")
    second = adapter.retrieve("operating expenses")

    assert "chunks" in first, first
    assert "chunks" in second, second


def test_retrieve_reads_only_the_two_frames_the_call_owes() -> None:
    """Exactly two frames are owed — initialize, then tools/call — and
    both are parsed.

    The notification draws no reply, so reading a third frame would
    block forever against a server that has answered everything. The
    exchange completing at all is the assertion; the frame ids pin that
    the two owed frames are the ones it read.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "retrieve", "arguments": {"query": "revenue"}},
    }

    async def _exchange() -> list[Any]:
        proc = await _launch_mcp_process(_command())
        wire = _build_mcp_wire_messages(payload)
        stdout = await _exchange_mcp_messages(proc, wire, _TIMEOUT_SECONDS)
        return _parse_mcp_responses(stdout)

    responses = asyncio.run(_exchange())

    assert len(responses) == 2, responses
    assert responses[0]["id"] == 1, responses[0]
    assert responses[1]["id"] == 2, responses[1]


def test_retrieve_reaps_its_child_after_each_call() -> None:
    """No MCP child is leaked per call.

    The child is counted before and after three round trips against
    long-lived servers; a transport that never stopped its child would
    leave three extra processes behind, because these servers do not
    exit on their own.
    """
    before = _mcp_children()
    adapter = _retrieve_adapter()
    for _ in range(3):
        assert "chunks" in adapter.retrieve("revenue"), "round trip failed"
    assert _mcp_children() - before == 0, "an MCP child was leaked"


def _mcp_children() -> int:
    """How many fixture-server children this test process has running."""

    result = subprocess.run(
        ["pgrep", "-f", str(_SERVER_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    return len([line for line in result.stdout.split() if line.strip()])


def test_retrieve_kills_a_wedged_server_on_timeout() -> None:
    """A server that answers nothing is a timeout, and it is cleaned up.

    The fixture answers every method it knows, so a hang comes from a
    command that reads stdin and never replies: it consumes the wire
    frames without emitting anything.
    """
    before = _mcp_children()
    adapter = MCPRetrieveAdapter(
        command=[sys.executable, "-c", _SILENT_READER],
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=2.0,
    )

    assert adapter.retrieve("revenue") == {"error": "retrieve_unavailable"}

    assert _mcp_children() - before == 0, "a timed-out MCP child was leaked"


#: A child that consumes its stdin and then stays alive without ever
#: answering. Stands in for a wedged server: the frames arrive, no
#: response ever comes, and the child does not exit either — so the
#: only thing that can end the call is the budget.
_SILENT_READER = (
    "import sys, time\n"
    "sys.stdin.read()\n"
    "while True:\n"
    "    time.sleep(3600)\n"
)


# ----- push port -----


def _push_wire() -> dict[str, Any]:
    """One wire-shape push envelope with a single chunk."""

    return {
        "source_ref": "docs/x.md",
        "body": "text 0",
        "chunks": [{"index": 0, "content": "text 0"}],
        "metadata": {
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "filing",
            "source_sha256": "sha-0",
            "parse_rule_version": "v1",
            "params_table_version": "p1",
            "normalization_rule_version": "n1",
        },
        "valid_from": "0001-01-01T00:00:00.000Z",
        "valid_until": "9999-12-31T23:59:59.999Z",
    }


def test_push_answers_from_a_server_that_keeps_running() -> None:
    """The push port completes a real round trip against a still-running
    child and reports the chunk ids the server actually stored."""

    port = MCPPushPort(command=_command(), timeout_seconds=_TIMEOUT_SECONDS)

    result = port.push([_push_wire()])

    assert result["written"] == 1
    assert result["duplicates"] == 0
    assert len(result["block_ids"]) == 1


def test_push_reaps_its_child_after_each_call() -> None:
    """A still-running server must not be left behind per push."""

    before = _mcp_children()
    port = MCPPushPort(command=_command(), timeout_seconds=_TIMEOUT_SECONDS)

    port.push([_push_wire()])
    port.push([dict(_push_wire(), source_ref="docs/y.md")])

    assert _mcp_children() - before == 0, "an MCP child was leaked"


def test_push_reports_a_wedged_server_as_service_unavailable() -> None:
    """A server that answers nothing is the SC21 unavailable failure,
    not a silent success, and it is cleaned up."""

    before = _mcp_children()
    port = MCPPushPort(
        command=[sys.executable, "-c", _SILENT_READER],
        timeout_seconds=2.0,
    )

    with pytest.raises(PushServiceUnavailableError):
        port.push([_push_wire()])

    assert _mcp_children() - before == 0, "a timed-out MCP child was leaked"


# ----- invalidation port -----


def _target(source_ref: str = "docs/x.md") -> InvalidationTarget:
    return InvalidationTarget(
        source_ref=source_ref,
        content_id="c0",
        block_id="b0",
        document_id="DOC-1",
        version="v-" + "a" * 16,
        unit=1,
        reason=REASON_UNIT_VERSION_CHANGED,
    )


def test_invalidation_answers_from_a_server_that_keeps_running() -> None:
    """The invalidation port completes a real round trip against a
    still-running child and closes what it reports."""

    port = MCPInvalidationPort(
        command=_command("--pushed", "docs/x.md=chunk-0"),
        timeout_seconds=_TIMEOUT_SECONDS,
    )

    outcome = port.invalidate([_target()])

    assert outcome.closed == 1, outcome
    assert outcome.already_closed == 0, outcome
    assert outcome.block_ids == ("chunk-0",), outcome


def test_invalidation_reaps_its_child_after_each_call() -> None:
    """One call per ``source_ref``, each spawning its own child that is
    then reaped."""

    before = _mcp_children()
    port = MCPInvalidationPort(
        command=_command(
            "--pushed", "docs/x.md=chunk-0", "--pushed", "docs/y.md=chunk-1"
        ),
        timeout_seconds=_TIMEOUT_SECONDS,
    )

    port.invalidate([_target("docs/x.md")])
    port.invalidate([_target("docs/y.md")])

    assert _mcp_children() - before == 0, "an MCP child was leaked"


def test_invalidation_reports_a_wedged_server_as_service_unavailable() -> None:
    """A server that answers nothing is the SC21 unavailable failure,
    and it is cleaned up."""

    before = _mcp_children()
    port = MCPInvalidationPort(
        command=[sys.executable, "-c", _SILENT_READER],
        timeout_seconds=2.0,
    )

    with pytest.raises(InvalidationServiceUnavailableError):
        port.invalidate([_target()])

    assert _mcp_children() - before == 0, "a timed-out MCP child was leaked"


# ----- index-job port -----


def test_index_job_submit_answers_from_a_server_that_keeps_running() -> None:
    """The index-job port completes a real round trip against a child
    that is still serving.

    This port is synchronous, so it exercises the sync sibling of the
    exchange — the same owed-frame discipline, one bridge to the event
    loop away from the three async ports above.
    """
    port = MCPIndexJobPort(
        command=_command(), timeout_seconds=_TIMEOUT_SECONDS
    )

    submission = port.submit_index_job("docs/x.md", "identity-abc")

    assert submission.record.job_id == "job-1"
    assert submission.record.source_ref == "docs/x.md"
    assert submission.record.content_identity == "identity-abc"
    assert submission.reused is False


def test_index_job_poll_answers_from_a_server_that_keeps_running() -> None:
    """The poll leg answers too, and reaps its child."""

    before = _mcp_children()
    port = MCPIndexJobPort(
        command=_command(), timeout_seconds=_TIMEOUT_SECONDS
    )

    poll = port.poll_index_job("job-1")

    assert poll.record.job_id == "job-1"
    assert poll.community_index_state is None
    assert _mcp_children() - before == 0, "an MCP child was leaked"


def test_index_job_reaps_its_child_after_each_call() -> None:
    """Two spawn-per-call round trips leave nothing running."""

    before = _mcp_children()
    port = MCPIndexJobPort(
        command=_command(), timeout_seconds=_TIMEOUT_SECONDS
    )

    port.submit_index_job("docs/x.md", "identity-abc")
    port.poll_index_job("job-1")

    assert _mcp_children() - before == 0, "an MCP child was leaked"


def test_index_job_reports_a_wedged_server_as_unavailable() -> None:
    """A server that answers nothing is the unavailable failure, and it
    is cleaned up rather than left for the reaper."""

    before = _mcp_children()
    port = MCPIndexJobPort(
        command=[sys.executable, "-c", _SILENT_READER],
        timeout_seconds=2.0,
    )

    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("docs/x.md", "identity-abc")

    assert _mcp_children() - before == 0, "a timed-out MCP child was leaked"


# ----- the shared exchange itself -----


#: A child that answers ``initialize``, then dies non-zero WITHOUT
#: answering ``tools/call``. This is the reachable shape of a genuine
#: non-zero exit: the frames the protocol owed are never all delivered,
#: so stdout closes mid-exchange.
_DIES_MID_EXCHANGE = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    request = json.loads(line)\n"
    "    if request.get('id') == 1:\n"
    "        print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}))\n"
    "        sys.stdout.flush()\n"
    "        break\n"
    "sys.stderr.write('fixture server died mid-call\\n')\n"
    "sys.exit(7)\n"
)


def test_a_server_that_dies_mid_call_is_still_an_error() -> None:
    """A genuine non-zero exit stays a transport failure.

    The fix must not have relaxed the exit check into accepting any
    child. Here the child answers ``initialize`` and then dies without
    answering ``tools/call``, so the owed frames never all arrive and
    the non-zero exit is reported rather than read as a still-running
    server.
    """
    port = MCPPushPort(
        command=[sys.executable, "-c", _DIES_MID_EXCHANGE],
        timeout_seconds=_TIMEOUT_SECONDS,
    )

    with pytest.raises(PushServiceUnavailableError):
        port.push([_push_wire()])


def test_a_server_that_dies_mid_call_leaves_no_child_behind() -> None:
    """The failure path reaps too — a crashed server is not leaked."""

    before = _mcp_children()

    with pytest.raises(PushServiceUnavailableError):
        MCPPushPort(
            command=[sys.executable, "-c", _DIES_MID_EXCHANGE],
            timeout_seconds=_TIMEOUT_SECONDS,
        ).push([_push_wire()])

    assert _mcp_children() - before == 0, "a crashed MCP child was leaked"


def test_exchange_never_calls_communicate() -> None:
    """The transport must not depend on ``communicate`` at all.

    ``communicate()`` returning at process exit is the defect: against a
    long-lived server it never returns at all. A child whose
    ``communicate`` raises proves the exchange reads frames off the
    stream instead.
    """

    class _NoCommunicateProcess:
        """A real child, with ``communicate`` made poisonous."""

        def __init__(self, proc: Any) -> None:
            self._proc = proc

        def __getattr__(self, name: str) -> Any:
            if name == "communicate":
                def _poison(*_a: Any, **_k: Any) -> Any:
                    raise AssertionError(
                        "the MCP exchange must not wait for process exit"
                    )

                return _poison
            return getattr(self._proc, name)

    async def _run() -> bytes:
        proc = await launch_oneshot_mcp_process(_command())
        wrapped = _NoCommunicateProcess(proc)
        wire = (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
            b'{"name":"retrieve","arguments":{"query":"revenue"}}}\n'
        )
        return await exchange_oneshot_mcp(
            wrapped, wire, timeout=_TIMEOUT_SECONDS
        )

    stdout = asyncio.run(_run())
    assert b'"id": 1' in stdout or b'"id":1' in stdout, stdout
    assert b'"id": 2' in stdout or b'"id":2' in stdout, stdout
