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
from typing import Any, Iterator

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
# Both worktree roots have collapsed back to the main checkout: the surfaces
# they once carried ahead of main are now merged and rebuilt there. The
# operator-console ``read_source`` tool, the corpus-ready push/资料 surface,
# and the search-mode retrieve stack (the tracked change/the tracked change/the tracked change/#245) all live in
# main's ``dist`` now, so the live acceptance runs against the single
# authoritative main build (issue fin the tracked change task 1: "对合并后的 iknow main
# build"). The aliases are kept because the opt-in tests import them by name.
IKNOW_WORKTREE_ROOT = IKNOW_ROOT
IKNOW_CORPUS_READY_ROOT = IKNOW_ROOT
EMBED_DIMENSIONS = 8

# Live backing services for the ``pg_es`` storage branch (fin the tracked change task 2).
# These are the docker-compose dev endpoints of the iknow checkout above —
# local test infra, not production credentials — and each is env-overridable
# so the operator can point the live run at another cluster without an edit.
IKNOW_PG_URL = os.environ.get(
    "FIN_LIVE_IKNOW_DB_URL",
    "postgresql://iknow_memory:iknow_memory@localhost:5432/iknow_memory",
)
IKNOW_ES_URL = os.environ.get("FIN_LIVE_IKNOW_ES_URL", "http://127.0.0.1:9200")
IKNOW_NEO4J_URI = os.environ.get(
    "FIN_LIVE_IKNOW_NEO4J_URI", "bolt://localhost:7687"
)
IKNOW_NEO4J_USER = os.environ.get("FIN_LIVE_IKNOW_NEO4J_USER", "neo4j")
IKNOW_NEO4J_PASSWORD = os.environ.get(
    "FIN_LIVE_IKNOW_NEO4J_PASSWORD", "iknow-memory-dev"
)


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


@dataclass(frozen=True)
class _LiveChildRecord(PushRecord):
    """A per-child record shaped for the live MCP tests' assertions.

    Inherits :class:`PushRecord` so the batching driver's
    ``_coerce_envelope`` recognizes the per-child shape (the T3 batch
    test asserts ``plans[0].records[0].block_id``, which only works if
    the plan row carries the per-child ``block_id``). ``to_wire()``
    is overridden to return the **one envelope per 资料** wire shape
    declared by T1 — a single-chunk envelope carrying the per-child
    ``id`` / ``content_id`` — so the receiving server indexes the
    slice as one chunk per envelope.
    """

    def to_wire(self) -> dict[str, Any]:
        """The one-envelope wire shape, projected per child.

        ``metadata`` is a copy of the underlying mapping so a caller's
        mutation of the returned wire cannot reach back into the
        record.
        """
        return {
            "source_ref": self.source_ref,
            "body": self.text,
            "chunks": [
                {
                    "index": 0,
                    "content": self.text,
                    "id": self.block_id,
                    "content_id": self.content_id,
                }
            ],
            "metadata": dict(self.metadata),
            "valid_from": "0001-01-01T00:00:00.000Z",
            "valid_until": "9999-12-31T23:59:59.999Z",
            "body_content_id": self.content_id,
        }


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


def child_records() -> tuple[_LiveChildRecord, ...]:
    """Build the live MCP tests' push records.

    Returns one :class:`_LiveChildRecord` per distinct frozen unit
    (4 units → 4 records), each as a one-chunk envelope on the wire
    (the ``_LiveChildRecord.to_wire()`` projection).

    Why one record per unit and not many children per unit
    -------------------------------------------------------

    The T1 wire contract is **one envelope per 资料** (one per unit;
    spec ``operator-console-host-contract``). ``source_ref`` is
    unit-granular — its locator triple has no block component, so
    every block of one unit necessarily serializes to the same
    ``source_ref``. The catalog side keys invalidation and
    reconstruction off that unit-level string; pushing N children of
    one unit as N one-chunk envelopes that share the unit's
    ``source_ref`` is a legacy per-block model that no production
    path emits.

    The earlier fixture built one unit with 48 sentences and pushed
    every child as a separate envelope against the same
    ``source_ref``. The real process correctly treats that as the
    ``rebuild-on-repush`` class: each envelope triggers
    ``deleteChunksForSourceDoc`` for the unit, so a multi-envelope
    push over the same unit collapses to the last envelope's chunks.
    That is the contract-correct outcome — the bug was the fixture,
    not the server.

    Modeling the live tests after the contract: 4 distinct units
    (unit numbers 1..4, all sharing the same document/version) →
    4 distinct ``source_ref`` values. Each unit's text is one short
    sentence (so ``split_unit`` produces one child per unit and the
    one-envelope-per-unit shape yields exactly one ``PushRecord``);
    the per-unit ``source_ref`` keeps the catalog-side
    ``deleteChunksForSourceDoc`` from trampling sibling units on a
    sequential per-batch push (T3's resume flow). Every sentence
    carries the phrase ``operating revenue`` so BM25 matches all 4
    regardless of unit.
    """

    version = "f" * 64
    documents_units: tuple[int, ...] = (1, 2, 3, 4)
    out: list[_LiveChildRecord] = []
    for unit_number in documents_units:
        unit_text = (
            f"Live child unit {unit_number:03d} reports operating revenue "
            "and keeps punctuation - byte identity matters."
        )
        unit = build_unit(
            fmt="txt",
            source_sha256=version,
            document_id="LIVE-2026Q3",
            version=version,
            unit=unit_number,
            text=unit_text,
        )
        records = unit_push_records(split_unit(unit, doc_type="narrative"))
        # Contract: one envelope per unit. With one short sentence
        # of unit text ``split_unit`` produces exactly one child
        # block, so the per-unit ``unit_push_records`` tuple is
        # length 1. Assert it so a future chunker change cannot
        # silently violate the fixture's one-record-per-unit
        # invariant.
        assert len(records) == 1, (
            f"unit {unit_number} split into {len(records)} children; "
            "the live MCP fixture requires one child per unit so the "
            "T1 one-envelope-per-unit wire shape stays in sync with "
            "the contract (no per-block envelopes against a shared "
            "source_ref)."
        )
        record = records[0]
        out.append(
            _LiveChildRecord(
                block_id=record.block_id,
                source_ref=record.source_ref,
                content_id=record.content_id,
                text=record.text,
                parent_block_id=record.parent_block_id,
                parent_text=record.parent_text,
                metadata=dict(record.metadata),
            )
        )
    return tuple(out)


@contextmanager
def live_iknow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path = IKNOW_ROOT,
    storage: str = "memory",
    es_chunk_index: str | None = None,
    es_entity_index: str | None = None,
) -> Iterator[LiveIknow]:
    """Run the real compiled iknow stdio server over one shared process.

    ``root`` selects the iknow-memory checkout whose ``dist/index.js``
    is spawned; it defaults to the single authoritative main build
    (``IKNOW_WORKTREE_ROOT`` / ``IKNOW_CORPUS_READY_ROOT`` are aliases
    of that same root now, kept only for import compatibility).

    ``storage`` selects the backing services the spawned process is
    wired to: ``"memory"`` (the default — in-process store, the
    original harness shape) or ``"pg_es"`` for the real PG (pgvector) +
    Elasticsearch + Neo4j stack the task-2 search-mode driver needs.
    ``es_chunk_index`` / ``es_entity_index`` apply only to ``pg_es`` and
    are **required** there with no default: the cluster is shared with
    iknow's own live suites, so every pg_es caller must pass explicit
    isolated (ideally per-run unique) index names it owns and cleans up.
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

    if storage == "memory":
        env = {
            "GRAPHRAG_MEMORY_STORAGE": "memory",
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
            "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
            "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-live-test",
            "GRAPHRAG_MEMORY_EMBED_BASE_URL": (
                f"http://127.0.0.1:{embedding_server.server_port}/v1"
            ),
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-live-test",
            # Vector-only on purpose. This harness runs STORAGE=memory with no
            # Elasticsearch; iknow ADR-0007 makes the BM25/precision layer
            # default-on-with-no-address an out-of-contract config (the in-process
            # memory BM25 is a test seam, not a sanctioned fallback), so a B-tier
            # retrieve escalates to retrieve_degraded_failure
            # (keyword_bm25:attempted-and-failed). Disabling BM25 is the
            # ADR-sanctioned vector-only escape hatch; A-tier lexical is still
            # exercised because mode-a-arm sources lexical from the memory backend
            # independent of this flag. Real-ES lexical is covered by the task-2
            # PG+ES driver, not this memory harness.
            "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
            "GRAPHRAG_MEMORY_NEO4J_URI": "",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
    elif storage == "pg_es":
        if es_chunk_index is None or es_entity_index is None:
            raise ValueError(
                "storage='pg_es' requires explicit es_chunk_index and "
                "es_entity_index: the cluster is shared with iknow's own "
                "live suites, so a built-in default name would cross their "
                "queries — pass per-run isolated fin-owned indices instead"
            )
        env = {
            "GRAPHRAG_MEMORY_STORAGE": "pgvector",
            "GRAPHRAG_MEMORY_DB_URL": IKNOW_PG_URL,
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
            "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
            "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-live-test",
            "GRAPHRAG_MEMORY_EMBED_BASE_URL": (
                f"http://127.0.0.1:{embedding_server.server_port}/v1"
            ),
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-live-test",
            "GRAPHRAG_MEMORY_ES_URL": IKNOW_ES_URL,
            # fin-owned indices (the driver passes per-run unique names and
            # deletes them at teardown): iknow's own live suites share this
            # cluster, and a shared index would cross their queries.
            "GRAPHRAG_MEMORY_ES_CHUNK_INDEX": es_chunk_index,
            "GRAPHRAG_MEMORY_ES_ENTITY_INDEX": es_entity_index,
            "GRAPHRAG_MEMORY_NEO4J_URI": IKNOW_NEO4J_URI,
            "GRAPHRAG_MEMORY_NEO4J_USER": IKNOW_NEO4J_USER,
            "GRAPHRAG_MEMORY_NEO4J_PASSWORD": IKNOW_NEO4J_PASSWORD,
            # Unlike the memory branch, BM25 stays ON: with a real ES
            # precision layer configured, ADR-0007's default-on-with-address
            # contract is satisfied, and that layer is exactly what the
            # task-2 A tier must lexically hit (and B fuses with vectors).
            # Rerank stays OFF so the fused order is observable end to end.
            "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "true",
            "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "true",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
    else:
        raise ValueError(
            f"live_iknow supports storage='memory' or 'pg_es', got {storage!r}"
        )
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
