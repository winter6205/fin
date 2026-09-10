"""Operator-console T4: live iknow-memory ``read_source`` acceptance.

Runs against the **real** iknow-memory stdio server built from the
operator-console worktree (``IKNOW_WORKTREE_ROOT``), whose tool surface
carries ``read_source`` — the main-checkout build the other ``mcp``
tests use predates it. Like every live test here it is marked ``mcp``
and therefore deselected by default ``uv run pytest -q``
(``tests/retrieve/conftest.py``); run it with
``uv run pytest -q -m mcp``.

What the ticket covers, on the wire:

1. The MCP initialize handshake against the real server — the shared
   :class:`~fin.retrieve.mcp_session.MCPStdioSession` performs it once
   and refuses to answer calls when it fails, so a successful
   ``tools/list`` / ``tools/call`` below proves a completed handshake
   (a strict MCP server rejects ``tools/call`` before it).
2. ``tools/list`` advertises ``read_source``.
3. ``tools/call read_source`` returns the documented shape for a
   pushed source — ``{id, source_ref, title?, content}`` — addressed
   by ``source_ref`` **and** by the returned ``id``, both backed by a
   real :func:`~fin.corpus.push.envelope_of_unit` push through
   :class:`~fin.retrieve.mcp_push_port.MCPPushPort`.
4. An unknown key is a **named** miss — ``isError: true`` with
   ``{"error": "read_source_not_found"}`` — never an empty success.

The assertions decode the canonical MCP content-block envelope
(``result.content[0].text``) the real server writes, so a server-side
shape drift surfaces here rather than only in scripted tests.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import UnitLocator, build_unit
from fin.corpus.push import envelope_of_unit, serialize_source_ref

from live_iknow_support import IKNOW_WORKTREE_ROOT, live_iknow


pytestmark = pytest.mark.mcp

_REQUEST_TIMEOUT_SECONDS = 20.0


@pytest.fixture
def worktree_live(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """A live session against the operator-console iknow worktree build.

    The worktree checkout is machine-local (the main checkout's build
    predates ``read_source``), so an absent build skips cleanly instead
    of failing the opt-in run the way a missing main checkout would.
    """

    entry = IKNOW_WORKTREE_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in "
            f"{IKNOW_WORKTREE_ROOT} to exercise the live read_source path"
        )
    with live_iknow(monkeypatch, root=IKNOW_WORKTREE_ROOT) as live:
        yield live


def _live_envelope(document_id: str, version: str, unit: int) -> Any:
    """One real fin envelope for a single-sentence frozen unit."""

    text = (
        f"Unit {unit} of {document_id} records operating revenue for "
        "the live T4 read_source check.\n"
    )
    parsed = build_unit(
        fmt="txt",
        source_sha256=version,
        document_id=document_id,
        version=version,
        unit=unit,
        text=text,
    )
    split = split_unit(parsed, doc_type="narrative")
    return envelope_of_unit(
        split,
        valid_from="2026-01-01T00:00:00.000Z",
        valid_until="9999-12-31T23:59:59.999Z",
    )


def _tools_call(live: Any, arguments: dict[str, Any], request_id: int) -> dict:
    """One ``tools/call read_source`` frame on the shared session."""

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "read_source", "arguments": arguments},
    }
    return live.session.call(payload, _REQUEST_TIMEOUT_SECONDS)


def _decoded_inner(response: dict) -> dict:
    """Decode the canonical content-block envelope the server wrote."""

    assert "error" not in response, response
    result = response["result"]
    assert isinstance(result, dict), result
    content = result["content"]
    assert isinstance(content, list) and content, result
    first = content[0]
    assert first["type"] == "text", result
    return json.loads(first["text"])


def test_live_iknow_tools_list_includes_read_source(
    worktree_live: Any,
) -> None:
    """The real server's tool surface advertises ``read_source``.

    A ``tools/list`` answer only comes back after the session's
    initialize handshake succeeded, so this also proves the handshake
    against the real stdio process.
    """

    response = worktree_live.session.call(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/list"},
        _REQUEST_TIMEOUT_SECONDS,
    )
    assert "error" not in response, response
    tools = {
        tool["name"]: tool for tool in response["result"]["tools"]
    }
    assert "read_source" in tools, sorted(tools)
    properties = tools["read_source"]["inputSchema"]["properties"]
    assert "source_ref" in properties
    assert "id" in properties


def test_live_iknow_read_source_returns_pushed_body_by_ref_and_id(
    worktree_live: Any,
) -> None:
    """A pushed fin envelope reads back verbatim by ``source_ref`` and
    by the returned primary-key ``id`` — the documented
    ``{id, source_ref, title?, content}`` shape."""

    version = "f" * 64
    envelope = _live_envelope("T4-LIVE-READ", version, unit=1)

    pushed = worktree_live.push.push([envelope.to_wire()])
    assert pushed["written"] == 1, pushed

    by_ref = _tools_call(
        worktree_live, {"source_ref": envelope.source_ref}, request_id=11
    )
    inner = _decoded_inner(by_ref)
    assert inner["source_ref"] == envelope.source_ref
    assert inner["content"] == envelope.body
    assert isinstance(inner["id"], str) and inner["id"]

    by_id = _tools_call(
        worktree_live, {"id": inner["id"]}, request_id=12
    )
    again = _decoded_inner(by_id)
    assert again["id"] == inner["id"]
    assert again["source_ref"] == envelope.source_ref
    assert again["content"] == envelope.body


def test_live_iknow_read_source_unknown_key_is_a_named_miss(
    worktree_live: Any,
) -> None:
    """An unknown key answers ``isError`` with the named
    ``read_source_not_found`` code, never an empty success."""

    # A well-formed fin locator for a document that was never pushed:
    # the miss must be the named server-side code, not a parse refusal.
    unknown_ref = serialize_source_ref(
        UnitLocator("T4-LIVE-ABSENT", "f" * 64, unit=1)
    )
    response = _tools_call(
        worktree_live,
        {"source_ref": unknown_ref},
        request_id=13,
    )
    result = response["result"]
    assert result["isError"] is True, result
    inner = json.loads(result["content"][0]["text"])
    assert inner["error"] == "read_source_not_found"
    assert inner["source_ref"] == unknown_ref
