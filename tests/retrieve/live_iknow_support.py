"""Opt-in helpers for acceptance tests against a real iknow-memory process.

The default pytest suite never imports this module's process fixture.  The
tests that use it are marked ``mcp`` and therefore stay outside the default
run.  A local embedding HTTP endpoint supplies deterministic vectors so the
acceptance exercises the real MCP server without credentials or an external
service.
"""

from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import PushRecord, unit_push_records
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_invalidation_port import MCPInvalidationPort
from fin.retrieve.mcp_push_port import MCPPushPort
from fin.retrieve.mcp_session import MCPStdioSession


IKNOW_ROOT = Path("/home/winner/projects/iknow-memory")
IKNOW_ENTRY = IKNOW_ROOT / "dist" / "index.js"
# A worktree checkout whose build carries a newer tool surface (e.g. the
# operator-console worktree's ``read_source``). Pass ``root=`` to
# :func:`live_iknow` to test against it; the default keeps the main
# checkout so existing opt-in tests are unaffected.
IKNOW_WORKTREE_ROOT = Path(
    "/home/winner/projects/iknow-memory-wt-operator-console"
)
EMBED_DIMENSIONS = 8


class _EmbeddingHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible deterministic embedding endpoint for opt-in tests."""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        try:
            import json

            request = json.loads(body)
            inputs = request.get("input", [])
            count = 1 if isinstance(inputs, str) else len(inputs)
            response = {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": [1.0, *([0.0] * (EMBED_DIMENSIONS - 1))],
                    }
                    for index in range(count)
                ],
                "model": request.get("model", "live-test"),
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
            encoded = json.dumps(response).encode("utf-8")
        except (ValueError, TypeError, AttributeError):
            self.send_error(400, "invalid embedding request")
            return

        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@dataclass
class LiveIknow:
    """The shared real stdio session and fin-side ports."""

    session: MCPStdioSession
    push: MCPPushPort
    retrieve: MCPRetrieveAdapter
    invalidation: MCPInvalidationPort


def _node_executable() -> str:
    """Resolve the WSL-visible Node executable used by the live process."""

    configured = os.environ.get("IKNOW_NODE")
    candidates = [
        configured,
        shutil.which("node"),
        "/home/winner/.bun/bin/bun",
        shutil.which("node.exe"),
        "/mnt/f/software/nodejs/node.exe",
    ]
    for candidate in candidates:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    raise AssertionError(
        "live iknow acceptance needs Node; set IKNOW_NODE to node.exe"
    )


def child_records() -> tuple[PushRecord, ...]:
    """Build existing fin child blocks, with enough text for multiple blocks."""

    version = "f" * 64
    text = "".join(
        f"Live child sentence {index:03d} records operating revenue and "
        "keeps punctuation - byte identity matters.\n"
        for index in range(48)
    )
    unit = build_unit(
        fmt="txt",
        source_sha256=version,
        document_id="LIVE-2026Q3",
        version=version,
        unit=1,
        text=text,
    )
    records = unit_push_records(split_unit(unit, doc_type="narrative"))
    assert len(records) >= 2
    return records


@contextmanager
def live_iknow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path = IKNOW_ROOT,
) -> Iterator[LiveIknow]:
    """Run the real compiled iknow stdio server over one shared process.

    ``root`` selects the iknow-memory checkout whose ``dist/index.js``
    is spawned; pass ``IKNOW_WORKTREE_ROOT`` for a worktree build that
    carries tools the main checkout does not have yet.
    """

    entry = root / "dist" / "index.js"
    if not entry.is_file():
        raise AssertionError(
            f"{entry} is missing; run `npm run build` in {root}"
        )

    embedding_server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(
        target=embedding_server.serve_forever,
        name="fin-live-embedding",
        daemon=True,
    )
    thread.start()

    env = {
        "GRAPHRAG_MEMORY_STORAGE": "memory",
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
        "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
        "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-live-test",
        "GRAPHRAG_MEMORY_EMBED_BASE_URL": (
            f"http://127.0.0.1:{embedding_server.server_port}/v1"
        ),
        "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-live-test",
        "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "true",
        "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
        "GRAPHRAG_MEMORY_NEO4J_URI": "",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
        "ANTHROPIC_AUTH_TOKEN": "",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    session = MCPStdioSession(
        [_node_executable(), str(entry)],
        cwd=str(root),
        timeout_seconds=20.0,
    )
    live = LiveIknow(
        session=session,
        push=MCPPushPort(session=session, timeout_seconds=20.0),
        retrieve=MCPRetrieveAdapter(session=session, timeout_seconds=20.0),
        invalidation=MCPInvalidationPort(session=session, timeout_seconds=20.0),
    )
    try:
        yield live
    finally:
        session.close()
        embedding_server.shutdown()
        embedding_server.server_close()
        thread.join(timeout=5)
