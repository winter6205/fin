"""Opt-in MCP 2.0 protocol smoke against a fixture server (T4 / SC6).

Every test in this module is marked ``mcp`` and is deselected by the
default ``uv run pytest -q`` (see ``tests/retrieve/conftest.py``).
``uv run pytest -q -m mcp`` runs them.

What the smoke covers
---------------------

Two layers, both against ``mcp_fixture_server.py`` — a real stdio
JSON-RPC MCP 2.0 server spawned as a subprocess. No live memory
server, no network, no SEC.

1. **Protocol layer.** ``_StdioClient`` drives the full MCP 2.0
   handshake by hand: ``initialize`` → ``notifications/initialized``
   → ``tools/list`` → ``tools/call``. This is the handshake ADR 0032
   names, asserted on the wire rather than through the adapter.
2. **Adapter layer.** ``MCPRetrieveAdapter`` talks to the same server
   over its REAL ``_call_mcp`` transport — no scripted override, an
   actual subprocess spawn — and its content-block envelope parsing
   is exercised against bytes a server really produced.

The two layers are separate because the product adapter opens a fresh
subprocess per call and completes a full handshake on that connection.
The fixture server is strict by default (``tools/call`` before the
handshake is a JSON-RPC ``-32002`` error), which makes the ordering
observable, and ``--allow-uninitialized`` remains available for
single-shot transport tests.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from fin.corpus.extracted import Locator
from fin.retrieve import Chunk
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter

pytestmark = pytest.mark.mcp

_SERVER_PATH = Path(__file__).with_name("mcp_fixture_server.py")

# The protocol version the fixture server advertises. Kept in one
# place so a version bump is a single edit on both sides.
_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC error code the fixture server uses for "handshake not
# done yet" (MCP servers conventionally reuse this range).
_NOT_INITIALIZED = -32002

# Generous enough that a loaded CI box does not flake, short enough
# that a wedged server fails the test instead of hanging the suite.
_READ_TIMEOUT_SECONDS = 15.0


# ----- stdio JSON-RPC driver -----


class _StdioClient:
    """Newline-delimited JSON-RPC 2.0 client over a child's stdio.

    Deliberately hand-rolled rather than pulled from an SDK: the point
    of the smoke is to assert what goes over the wire, so the wire
    format has to be visible in the test.
    """

    def __init__(self, *server_args: str) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, str(_SERVER_PATH), *server_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send a request and return the matching response object."""
        self._next_id += 1
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._send(message)
        response = self._read()
        assert response.get("id") == self._next_id, (
            f"response id {response.get('id')!r} does not match request id "
            f"{self._next_id!r}: {response!r}"
        )
        return response

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no ``id``, no response expected)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _send(self, message: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict:
        assert self._proc.stdout is not None
        ready, _, _ = select.select(
            [self._proc.stdout], [], [], _READ_TIMEOUT_SECONDS
        )
        if not ready:
            raise AssertionError(
                f"fixture server sent nothing within {_READ_TIMEOUT_SECONDS}s"
            )
        line = self._proc.stdout.readline()
        if not line:
            raise AssertionError(
                "fixture server closed stdout without answering; stderr: "
                f"{self._drain_stderr()!r}"
            )
        return json.loads(line)

    def expect_no_response(self, timeout: float = 0.5) -> None:
        """Assert the server stays silent (the contract for a notification)."""
        assert self._proc.stdout is not None
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        assert not ready, (
            "fixture server answered a notification: "
            f"{self._proc.stdout.readline()!r}"
        )

    def _drain_stderr(self) -> str:
        assert self._proc.stderr is not None
        return self._proc.stderr.read()

    def close(self) -> int:
        """Close stdin and reap the child; returns its exit code."""
        stdout, stderr = self._proc.communicate(timeout=10)
        assert self._proc.returncode == 0, (
            f"fixture server exited {self._proc.returncode}; "
            f"stdout={stdout!r} stderr={stderr!r}"
        )
        return self._proc.returncode


@pytest.fixture
def strict_client() -> Iterator[_StdioClient]:
    """A fixture server that requires the MCP handshake."""
    client = _StdioClient()
    try:
        yield client
    finally:
        if client._proc.poll() is None:
            client.close()


def _handshake(client: _StdioClient) -> dict:
    """Run ``initialize`` + ``notifications/initialized``; return the result."""
    response = client.request(
        "initialize",
        {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fin-smoke", "version": "0"},
        },
    )
    result = response["result"]
    client.notify("notifications/initialized")
    return result


# ----- protocol layer: initialize -> tools/call -----


def test_initialize_returns_a_conformant_jsonrpc_result(
    strict_client: _StdioClient,
) -> None:
    """``initialize`` answers JSON-RPC 2.0 with a negotiated protocol
    version, a tools capability, and server identity.

    This is the first half of the ADR 0032 handshake. Without a tools
    capability in the reply, a real MCP client would never go on to
    call ``tools/call`` at all.
    """
    response = strict_client.request(
        "initialize",
        {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fin-smoke", "version": "0"},
        },
    )
    assert response["jsonrpc"] == "2.0"
    assert "error" not in response
    result = response["result"]
    assert result["protocolVersion"] == _PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]


def test_initialize_negotiates_down_an_unsupported_client_version(
    strict_client: _StdioClient,
) -> None:
    """A client asking for a version the server does not speak gets the
    server's own version back, not an error.

    Version negotiation is the one place where a mismatch must not read
    as a transport failure — the client is then free to accept or hang up.
    """
    response = strict_client.request(
        "initialize",
        {
            "protocolVersion": "1999-01-01",
            "capabilities": {},
            "clientInfo": {"name": "fin-smoke", "version": "0"},
        },
    )
    assert "error" not in response
    assert response["result"]["protocolVersion"] == _PROTOCOL_VERSION


def test_initialized_notification_gets_no_reply(
    strict_client: _StdioClient,
) -> None:
    """``notifications/initialized`` has no ``id``, so it must draw no
    response — a server that answers it desynchronizes every later
    request/response pairing on the stream."""
    strict_client.request(
        "initialize",
        {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {}},
    )
    strict_client.notify("notifications/initialized")
    strict_client.expect_no_response()


def test_tools_list_advertises_the_retrieve_tool(
    strict_client: _StdioClient,
) -> None:
    """After the handshake, ``tools/list`` advertises ``retrieve`` with an
    input schema covering the retrieve port's query and filters."""
    _handshake(strict_client)
    result = strict_client.request("tools/list")["result"]
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert "retrieve" in tools, tools
    properties = tools["retrieve"]["inputSchema"]["properties"]
    assert "query" in properties
    assert "top_k" in properties
    assert "document_id" in properties


def test_initialize_then_tools_call_returns_content_block_hits(
    strict_client: _StdioClient,
) -> None:
    """The full ADR 0032 handshake: ``initialize`` → ``tools/call``
    returns the canonical MCP content-block envelope whose text decodes
    to hits carrying ``text`` / ``source_ref`` / ``score``.

    ``source_ref`` matters most: it is the id the adapter's locator map
    is keyed on, so a server that omits it produces chunks the citation
    gate can never resolve.
    """
    _handshake(strict_client)
    response = strict_client.request(
        "tools/call",
        {"name": "retrieve", "arguments": {"query": "revenue", "top_k": 5}},
    )
    assert "error" not in response, response
    content = response["result"]["content"]
    assert content[0]["type"] == "text"
    hits = json.loads(content[0]["text"])["hits"]
    assert hits, "fixture corpus should match the query 'revenue'"
    for hit in hits:
        assert isinstance(hit["text"], str) and hit["text"]
        assert isinstance(hit["source_ref"], str) and hit["source_ref"]
        assert isinstance(hit["score"], (int, float))
    assert response["result"]["isError"] is False


def test_tools_call_honours_top_k(strict_client: _StdioClient) -> None:
    """``top_k`` bounds the hit list server-side (overflow boundary)."""
    _handshake(strict_client)
    response = strict_client.request(
        "tools/call",
        {"name": "retrieve", "arguments": {"query": "revenue quarter", "top_k": 1}},
    )
    hits = json.loads(response["result"]["content"][0]["text"])["hits"]
    assert len(hits) == 1


def test_tools_call_with_no_match_returns_an_empty_hit_list(
    strict_client: _StdioClient,
) -> None:
    """A search that ran and found nothing is an empty ``hits`` list, not
    a JSON-RPC error — the wire-level source of the ``{chunks: []}``
    miss shape."""
    _handshake(strict_client)
    response = strict_client.request(
        "tools/call",
        {"name": "retrieve", "arguments": {"query": "zzzz-no-such-term"}},
    )
    assert "error" not in response
    assert json.loads(response["result"]["content"][0]["text"])["hits"] == []


# ----- protocol layer: negative paths -----


def test_tools_call_before_initialize_is_a_protocol_error(
    strict_client: _StdioClient,
) -> None:
    """A strict MCP server rejects ``tools/call`` before the handshake.

    This is the negative half of "initialize → tools/call": the ordering
    is a protocol requirement, not decoration.
    """
    response = strict_client.request(
        "tools/call",
        {"name": "retrieve", "arguments": {"query": "revenue"}},
    )
    assert response["error"]["code"] == _NOT_INITIALIZED
    assert "result" not in response


def test_unknown_method_is_method_not_found(
    strict_client: _StdioClient,
) -> None:
    """An unimplemented method answers JSON-RPC ``-32601``."""
    _handshake(strict_client)
    response = strict_client.request("resources/list")
    assert response["error"]["code"] == -32601


def test_unknown_tool_name_is_invalid_params(
    strict_client: _StdioClient,
) -> None:
    """``tools/call`` for a tool the server does not expose answers
    JSON-RPC ``-32602`` rather than an empty result that would read as
    a legitimate miss."""
    _handshake(strict_client)
    response = strict_client.request(
        "tools/call", {"name": "no_such_tool", "arguments": {}}
    )
    assert response["error"]["code"] == -32602


def test_malformed_line_is_a_parse_error_and_the_server_survives(
    strict_client: _StdioClient,
) -> None:
    """Garbage on stdin answers ``-32700`` and leaves the session usable
    (exception boundary: one bad frame must not kill the server)."""
    assert strict_client._proc.stdin is not None
    strict_client._proc.stdin.write("{not json\n")
    strict_client._proc.stdin.flush()
    assert strict_client._read()["error"]["code"] == -32700
    response = strict_client.request(
        "initialize",
        {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {}},
    )
    assert "error" not in response


# ----- adapter layer: real subprocess transport -----


def _loc(document_id: str, unit: int = 1) -> Locator:
    return Locator(document_id=document_id, version="v-" + "a" * 60, unit=unit)


def _lenient_command() -> list[str]:
    """Launch command for a server that skips the handshake requirement.

    The product adapter still handshakes, but this option keeps the
    fixture useful for testing the single-shot tools-call shape.
    """
    return [sys.executable, str(_SERVER_PATH), "--allow-uninitialized"]


def test_adapter_over_real_stdio_transport_maps_hits_to_chunks() -> None:
    """End to end over the REAL transport: ``MCPRetrieveAdapter`` spawns
    the fixture server, exchanges JSON-RPC on stdio, and maps the
    content-block reply to ``Chunk``s carrying fin locators.

    Nothing here is scripted — ``_call_mcp`` is the shipped subprocess
    implementation, so this is the first test that proves the adapter's
    envelope parsing survives contact with bytes a server actually wrote.
    """
    adapter = MCPRetrieveAdapter(
        command=_lenient_command(),
        locator_map={
            "fixture-1": _loc("DOC-2023Q1", unit=1),
            "fixture-2": _loc("DOC-2023Q1", unit=2),
            "fixture-3": _loc("DOC-2023Q2", unit=1),
        },
        timeout_seconds=20.0,
    )
    result = adapter.retrieve("revenue", limit=5)
    assert "chunks" in result, result
    chunks = result["chunks"]
    assert chunks, "the fixture corpus matches 'revenue'"
    for chunk in chunks:
        assert isinstance(chunk, Chunk)
        assert isinstance(chunk.source_ref, Locator)
        assert chunk.source_ref.document_id.startswith("DOC-")
    assert chunks == sorted(chunks, key=lambda c: c.score, reverse=True)


def test_adapter_completes_handshake_before_tools_call(tmp_path: Path) -> None:
    """The product adapter must send the MCP session handshake before
    the first business request on the same stdio connection."""
    trace_path = tmp_path / "mcp-methods.json"
    adapter = MCPRetrieveAdapter(
        command=[
            sys.executable,
            str(_SERVER_PATH),
            "--record-methods",
            str(trace_path),
        ],
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=20.0,
    )

    result = adapter.retrieve("revenue")

    assert "chunks" in result, result
    assert trace_path.read_text(encoding="utf-8") == (
        '["initialize", "notifications/initialized", "tools/call"]'
    )


def test_adapter_handshake_failure_returns_typed_exit() -> None:
    """An initialize failure is unavailable, never a successful miss or
    a partial chunks result."""
    adapter = MCPRetrieveAdapter(
        command=[
            sys.executable,
            str(_SERVER_PATH),
            "--fail-initialize",
        ],
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=20.0,
    )

    assert adapter.retrieve("revenue") == {"error": "retrieve_unavailable"}


def test_adapter_over_real_transport_drops_unmapped_hits() -> None:
    """Hits whose ``source_ref`` is absent from the locator map are
    dropped over the real transport too — an iknow id must never reach
    the citation gate (ADR 0032)."""
    adapter = MCPRetrieveAdapter(
        command=_lenient_command(),
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=20.0,
    )
    result = adapter.retrieve("revenue", limit=5)
    assert [chunk.source_ref for chunk in result["chunks"]] == [
        _loc("DOC-2023Q1")
    ]


def test_adapter_over_real_transport_no_match_is_the_miss_shape() -> None:
    """A real round-trip that finds nothing returns ``{"chunks": []}`` —
    a miss, never ``retrieve_unavailable``."""
    adapter = MCPRetrieveAdapter(
        command=_lenient_command(),
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=20.0,
    )
    assert adapter.retrieve("zzzz-no-such-term") == {
        "chunks": [],
        "distribution": "zero",
    }


def test_adapter_over_real_transport_forwards_document_id_filter() -> None:
    """The ``document_id`` the adapter puts in ``arguments`` really filters
    server-side: querying a term present in both documents but pinned to
    one returns only that document's chunk."""
    adapter = MCPRetrieveAdapter(
        command=_lenient_command(),
        locator_map={
            "fixture-1": _loc("DOC-2023Q1", unit=1),
            "fixture-3": _loc("DOC-2023Q2", unit=1),
        },
        timeout_seconds=20.0,
    )
    result = adapter.retrieve("revenue", document_id="DOC-2023Q2", limit=5)
    document_ids = {chunk.source_ref.document_id for chunk in result["chunks"]}
    assert document_ids == {"DOC-2023Q2"}


def test_adapter_against_strict_server_completes_the_handshake() -> None:
    """A handshake-strict MCP server accepts the product adapter's
    initialized tools/call session."""
    adapter = MCPRetrieveAdapter(
        command=[sys.executable, str(_SERVER_PATH)],
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=20.0,
    )
    result = adapter.retrieve("revenue")
    assert "chunks" in result, result
    assert result["chunks"][0].source_ref == _loc("DOC-2023Q1")


def test_adapter_against_a_crashing_server_returns_the_typed_exit() -> None:
    """A server that dies mid-call is a transport failure, not a miss."""
    adapter = MCPRetrieveAdapter(
        command=[sys.executable, str(_SERVER_PATH), "--crash"],
        locator_map={"fixture-1": _loc("DOC-2023Q1")},
        timeout_seconds=20.0,
    )
    assert adapter.retrieve("revenue") == {"error": "retrieve_unavailable"}


def test_smoke_needs_no_live_memory_server_or_network() -> None:
    """The whole smoke runs against a local subprocess: no memory-server
    endpoint and no SEC host is configured for it (spec SC1)."""
    for variable in ("IKNOW_MEMORY_URL", "FIN_MCP_ENDPOINT"):
        assert not os.environ.get(variable)
    assert _SERVER_PATH.is_file()
