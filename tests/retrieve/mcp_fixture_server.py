#!/usr/bin/env python3
"""A compliant MCP 2.0 stdio fixture server for the opt-in retrieve smoke.

ADR 0032 makes the product retrieve adapter an MCP 2.0 client
(``initialize`` → ``tools/call``) that is "protocol-testable against a
compliant public or fixture server". This is that fixture server: a
dependency-free stdio JSON-RPC 2.0 server exposing a single
``retrieve`` tool over a three-passage in-memory corpus. It is spawned
as a subprocess by ``test_mcp_smoke.py``; it is never imported, never
collected by pytest (the filename does not match ``test_*.py``), and
it opens no socket.

Wire format
-----------

One JSON object per line on stdin, one JSON object per line on stdout,
until stdin reaches EOF — the framing the adapter's ``_call_mcp``
already speaks. Nothing is ever written to stdout except responses, so
a client may read the first line as the answer to its first request.

Handshake
---------

Strict by default: ``tools/list`` and ``tools/call`` before
``initialize`` answer JSON-RPC ``-32002``. The product adapter opens a
fresh subprocess per call and completes the handshake before the
business request. ``--allow-uninitialized`` remains available for
negative transport-shape tests. Keeping strict as the default is
deliberate: the ordering requirement is what makes this a protocol
test rather than a JSON echo.

Usage
-----

``python tests/retrieve/mcp_fixture_server.py [--allow-uninitialized] [--fail-initialize] [--record-methods PATH] [--crash]``

``--crash`` exits non-zero on the first request, standing in for a
server that dies mid-call.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "fin-retrieve-fixture"
SERVER_VERSION = "0.1.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
NOT_INITIALIZED = -32002

_TOOL_NAME = "retrieve"

# Three passages across two documents. ``source_ref`` ids are opaque to
# fin — the adapter's locator map is what turns them into citable
# locators, and an id missing from that map is dropped.
_CORPUS: tuple[dict[str, Any], ...] = (
    {
        "source_ref": "fixture-1",
        "document_id": "DOC-2023Q1",
        "metadata": {"cik": "0000320193", "form": "10-Q"},
        "text": "Total net revenue for the quarter was 394.3 million dollars.",
    },
    {
        "source_ref": "fixture-2",
        "document_id": "DOC-2023Q1",
        "metadata": {"cik": "0000320193", "form": "10-Q"},
        "text": "Operating expenses for the quarter rose to 120.0 million dollars.",
    },
    {
        "source_ref": "fixture-3",
        "document_id": "DOC-2023Q2",
        "metadata": {"cik": "0000320193", "form": "10-Q"},
        "text": "Revenue guidance for the following period is unchanged.",
    },
)

_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": _TOOL_NAME,
        "description": "Rank corpus passages against a lexical query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1},
                "document_id": {"type": "string"},
                "filter": {"type": "object"},
            },
            "required": ["query"],
        },
    },
)

_WORD = re.compile(r"[a-z0-9.]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def search(
    query: str,
    *,
    top_k: int = 8,
    document_id: str | None = None,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Score the corpus against ``query`` and return MCP-shaped hits.

    Scoring is token-overlap count, not BM25 — the smoke asserts the
    protocol and the locator mapping, and a real ranker here would only
    make the expected hit list harder to reason about.
    """
    wanted = set(_tokens(query))
    if not wanted:
        return []
    hits: list[dict[str, Any]] = []
    for entry in _CORPUS:
        if document_id is not None and entry["document_id"] != document_id:
            continue
        if filters and any(
            entry["metadata"].get(key) != value for key, value in filters.items()
        ):
            continue
        overlap = len(wanted & set(_tokens(entry["text"])))
        if not overlap:
            continue
        hits.append(
            {
                "text": entry["text"],
                "source_ref": entry["source_ref"],
                "score": float(overlap),
            }
        )
    hits.sort(key=lambda hit: hit["score"], reverse=True)
    return hits[: max(top_k, 0)]


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class FixtureServer:
    """Session state for one stdio connection."""

    def __init__(
        self,
        *,
        allow_uninitialized: bool = False,
        fail_initialize: bool = False,
    ) -> None:
        self.allow_uninitialized = allow_uninitialized
        self.fail_initialize = fail_initialize
        self.initialized = False

    def handle(self, request: Any) -> dict[str, Any] | None:
        """Return the response object, or ``None`` for a notification."""
        if not isinstance(request, dict):
            return _error(None, INVALID_REQUEST, "request must be an object")
        request_id = request.get("id")
        method = request.get("method")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")

        if method == "initialize" and self.fail_initialize:
            return _error(request_id, INVALID_REQUEST, "initialize failed")
        if method == "initialize":
            return _result(request_id, self._initialize(params))
        if method.startswith("notifications/"):
            if method == "notifications/initialized":
                self.initialized = True
            # Notifications carry no id and draw no response, even the
            # ones this server does not recognize.
            return None
        if method == "ping":
            return _result(request_id, {})

        not_ready = self._require_initialized(request_id)
        if not_ready is not None:
            return not_ready

        if method == "tools/list":
            return _result(request_id, {"tools": list(_TOOLS)})
        if method == "tools/call":
            return self._tools_call(request_id, params)
        return _error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # MCP version negotiation: echo the client's version when this
        # server speaks it, otherwise answer with the version it does
        # speak and let the client decide whether to continue.
        requested = params.get("protocolVersion")
        negotiated = (
            requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _require_initialized(self, request_id: Any) -> dict[str, Any] | None:
        if self.initialized or self.allow_uninitialized:
            return None
        return _error(
            request_id,
            NOT_INITIALIZED,
            "server not initialized: send initialize before tools/*",
        )

    def _tools_call(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        name = params.get("name")
        if name != _TOOL_NAME:
            return _error(request_id, INVALID_PARAMS, f"unknown tool: {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, INVALID_PARAMS, "arguments must be an object")
        query = arguments.get("query")
        if not isinstance(query, str):
            return _error(request_id, INVALID_PARAMS, "query must be a string")
        top_k = arguments.get("top_k", 8)
        if not isinstance(top_k, int) or isinstance(top_k, bool):
            return _error(request_id, INVALID_PARAMS, "top_k must be an integer")
        document_id = arguments.get("document_id")
        if document_id is not None and not isinstance(document_id, str):
            return _error(request_id, INVALID_PARAMS, "document_id must be a string")
        filters = arguments.get("filter")
        if filters is not None and not isinstance(filters, dict):
            return _error(request_id, INVALID_PARAMS, "filter must be an object")
        hits = search(
            query,
            top_k=top_k,
            document_id=document_id,
            filters=filters,
        )
        return _result(
            request_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps({"hits": hits})}
                ],
                "isError": False,
            },
        )


def main(argv: list[str]) -> int:
    record_path: Path | None = None
    if "--record-methods" in argv:
        record_index = argv.index("--record-methods") + 1
        if record_index >= len(argv):
            raise SystemExit("--record-methods requires a path")
        record_path = Path(argv[record_index])

    server = FixtureServer(
        allow_uninitialized="--allow-uninitialized" in argv,
        fail_initialize="--fail-initialize" in argv,
    )
    recorded_methods: list[str] = []
    crash = "--crash" in argv
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if crash:
            sys.stderr.write("fixture server crashed on purpose\n")
            return 3
        try:
            request = json.loads(line)
        except ValueError as exc:
            _emit(_error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
            continue
        if isinstance(request, dict) and isinstance(request.get("method"), str):
            recorded_methods.append(request["method"])
        response = server.handle(request)
        if response is not None:
            _emit(response)
    if record_path is not None:
        record_path.write_text(json.dumps(recorded_methods), encoding="utf-8")
    return 0


def _emit(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
