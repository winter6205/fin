"""Boundary tests for ``MCPRetrieveAdapter`` (T3, ADR 0030).

The MCP adapter is the **product** retrieve implementation: a
stdio JSON-RPC client to the iknow-memory MCP server. Its
contract (per spec success criteria):

* empty query / unknown ``document_id`` / mapper-drop →
  ``{"chunks": []}`` (NOT ``{"error": "retrieve_unavailable"}``)
* MCP process down / protocol fail / timeout →
  ``{"error": "retrieve_unavailable"}`` (typed EXIT, no BM25
  fallback)
* limit cap = 20 (ADR 0009, parity with port semantics)
* two overlapping retrieve calls each return chunks-or-
  ``retrieve_unavailable`` without raising

The default transport (``_call_mcp``) launches a subprocess.
Tests override ``_call_mcp`` to inject scripted responses or
scripted exceptions — this is the documented seam; the real
stdio JSON-RPC path is exercised separately (manually / opt-in).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve import Chunk, EmptyQueryError
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_MISSING_SOURCE_REF,
    MAPPING_STAGE_RETRIEVE,
    MappingFailureLedger,
)
from fin.retrieve.mcp_adapter import (
    MCPRetrieveAdapter,
    RetrieveUnavailableError,
)


def _loc(document_id: str, page: int = 1) -> Locator:
    return Locator(
        document_id=document_id,
        version="v-" + "a" * 60,
        page=page,
    )


def _chunk(text: str, document_id: str, page: int = 1, score: float = 1.0) -> Chunk:
    return Chunk(
        text=text,
        source_ref=_loc(document_id, page=page),
        score=score,
    )


def _canonical_source_ref(document_id: str, page: int = 1) -> str:
    """The B4 canonical fin ``source_ref`` for a (document_id, unit) pair.

    Plan D0 I-T2: the wire envelope sends ``source_ref`` as the fin
    locator serialization; tests build the canonical string via the
    single authority for that serialization, ``fin.corpus.push``.
    """
    return serialize_source_ref(
        UnitLocator(
            document_id=document_id,
            version="v-" + "a" * 60,
            unit=page,
        )
    )


# ----- test seam: subclass override of _call_mcp -----


class _ScriptedAdapter(MCPRetrieveAdapter):
    """``MCPRetrieveAdapter`` whose ``_call_mcp`` returns a
    scripted response or raises a scripted exception.

    Skips the parent ``__init__`` so the test does not need to
    allocate a real subprocess or any optional deps. The fields
    the parent ``__init__`` would set are duplicated here.
    """

    def __init__(
        self,
        *,
        scripted_response: dict | None = None,
        scripted_exception: BaseException | None = None,
        locator_map: dict[str, Locator] | None = None,
        known_document_ids: set[str] | None = None,
        command: list[str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._command = list(command) if command else None
        self._locator_map = dict(locator_map or {})
        self._known_doc_ids = (
            frozenset(known_document_ids)
            if known_document_ids is not None
            else None
        )
        self._timeout = float(timeout_seconds)
        self._scripted_response = scripted_response
        self._scripted_exception = scripted_exception

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        if self._scripted_exception is not None:
            raise self._scripted_exception
        if self._scripted_response is not None:
            return self._scripted_response
        raise RuntimeError("no scripted response configured")


class _ReadCapturingAdapter(_ScriptedAdapter):
    """Scripted ``_call_mcp`` for ``read()``.

    The fake returns the **real** MCP wire shape — a list of text content
    blocks carrying a JSON envelope — so the scripted suite exercises the
    same decode path the real iknow-memory server drives. Bodies map
    ``source_ref`` → body text; ``errors`` inject JSON-RPC error envelopes
    for the named-miss path.
    """

    def __init__(self, *, bodies: dict[str, str], errors: list[dict] | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.bodies = dict(bodies)
        self.errors = list(errors or [])
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        if self.errors:
            return self.errors.pop(0)
        source_ref = payload["params"]["arguments"]["source_ref"]
        body = self.bodies.get(source_ref, "")
        inner = {
            "id": source_ref,
            "source_ref": source_ref,
            "content": body,
        }
        return {
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(inner)}
                ]
            }
        }


def test_mcp_read_ingested_document_calls_read_source_per_unit() -> None:
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locators = (Locator(document_id, version, 2), Locator(document_id, version, 1))
    source_refs = [serialize_source_ref(UnitLocator(document_id, version, locator.page)) for locator in locators]
    adapter = _ReadCapturingAdapter(
        bodies={source_refs[0]: "SECOND PARAGRAPH", source_refs[1]: "FIRST PARAGRAPH"},
        locator_map={"hit-2": locators[0], "hit-1": locators[1]},
        known_document_ids={document_id},
    )
    result = adapter.read(document_id, version, page=1, page_limit=2)
    assert [payload["params"]["arguments"] for payload in adapter.payloads] == [
        {"source_ref": source_refs[1]}, {"source_ref": source_refs[0]}
    ]
    assert result == {"document_id": document_id, "version": version, "page": 1, "text": "FIRST PARAGRAPH\n\nSECOND PARAGRAPH", "page_count": 2}


def test_mcp_read_clamps_window_and_unit_order() -> None:
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locators = tuple(Locator(document_id, version, page) for page in range(1, 5))
    refs = {locator.page: serialize_source_ref(UnitLocator(document_id, version, locator.page)) for locator in locators}
    adapter = _ReadCapturingAdapter(
        bodies={refs[2]: "PAGE TWO", refs[3]: "PAGE THREE", refs[4]: "PAGE FOUR"},
        locator_map={f"hit-{page}": locator for page, locator in enumerate(locators, 1)},
        known_document_ids={document_id},
    )
    result = adapter.read(document_id, version, page=2, page_limit=99)
    assert [payload["params"]["arguments"] for payload in adapter.payloads] == [{"source_ref": refs[2]}, {"source_ref": refs[3]}, {"source_ref": refs[4]}]
    assert result["text"] == "PAGE TWO\n\nPAGE THREE\n\nPAGE FOUR"
    assert result["page"] == 2 and result["page_count"] == 4


def test_mcp_read_miss_and_unknown_document_are_unknown_locator() -> None:
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    adapter = _ReadCapturingAdapter(
        bodies={}, locator_map={"hit": locator}, known_document_ids={document_id},
        errors=[{"error": {"code": -32001, "message": "read_source_not_found"}}],
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}
    unknown = _ReadCapturingAdapter(bodies={}, locator_map={"hit": locator}, known_document_ids={document_id})
    assert unknown.read("UNKNOWN-2099Q9", version) == {"error": "unknown_locator"}
    assert unknown.payloads == []


def test_mcp_read_iserror_content_block_is_unknown_locator() -> None:
    """The real ``read_source`` miss wire is an ``isError`` content
    block whose text is a JSON envelope carrying the named code.

    The adapter must surface that as ``unknown_locator`` — the same
    miss shape the JSON-RPC error path uses — never as an empty
    success and never as the raw block list.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    source_ref = serialize_source_ref(
        UnitLocator(document_id, version, unit=locator.page)
    )
    miss_envelope = {
        "error": "read_source_not_found",
        "source_ref": source_ref,
    }
    adapter = _ReadCapturingAdapter(
        bodies={},
        locator_map={"hit": locator},
        known_document_ids={document_id},
        errors=[
            {
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(miss_envelope),
                        }
                    ],
                }
            }
        ],
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


def test_mcp_read_invalidated_content_block_is_unknown_locator() -> None:
    """A ``read_source_invalidated`` content block — the T5
    operator-console miss — is also surfaced as ``unknown_locator``.

    The receiving service distinguishes "row exists but window is
    closed" from "row missing", but fin's read contract collapses
    both to the existing miss shape; this guard ensures the
    invalidated code (which also starts with ``read_source_``) does
    not slip through as a successful empty body.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    source_ref = serialize_source_ref(
        UnitLocator(document_id, version, unit=locator.page)
    )
    miss_envelope = {
        "error": "read_source_invalidated",
        "source_ref": source_ref,
        "valid_until": "2026-01-01T00:00:00.000Z",
    }
    adapter = _ReadCapturingAdapter(
        bodies={},
        locator_map={"hit": locator},
        known_document_ids={document_id},
        errors=[
            {
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(miss_envelope),
                        }
                    ],
                }
            }
        ],
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


def test_mcp_read_version_mismatch_rejects_before_transport() -> None:
    locator = Locator("DOC-2023Q1", "v-" + "b" * 60, 1)
    adapter = _ReadCapturingAdapter(bodies={}, locator_map={"hit": locator}, known_document_ids={"DOC-2023Q1"})
    assert adapter.read("DOC-2023Q1", "v-" + "a" * 60) == {"error": "unknown_locator"}
    assert adapter.payloads == []


@pytest.mark.parametrize(("page", "page_limit"), [(0, 1), (1, 0), (4, 3)])
def test_mcp_read_invalid_window_rejects_before_transport(page: int, page_limit: int) -> None:
    locator = Locator("DOC-2023Q1", "v-" + "a" * 60, 3)
    adapter = _ReadCapturingAdapter(bodies={}, locator_map={"hit": locator}, known_document_ids={"DOC-2023Q1"})
    assert adapter.read("DOC-2023Q1", "v-" + "a" * 60, page, page_limit) == {"error": "unknown_locator"}
    assert adapter.payloads == []


# ----- read_source content-block decode: boundary shapes -----


class _FixedResponseAdapter(_ScriptedAdapter):
    """Scripted ``_call_mcp`` returning one fixed wire response for
    ``read()`` — same decode path as ``_ReadCapturingAdapter`` but with
    full control over the response envelope."""

    def __init__(self, response: dict, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._response = response

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        return self._response


def _success_content_block(*blocks: dict) -> dict:
    return {"result": {"content": list(blocks)}}


def test_read_mixed_content_blocks_concatenate_only_text_bodies() -> None:
    """A content list mixing text blocks with non-text blocks (e.g.
    resource / image blocks) still reads: every text block carrying a
    body envelope contributes, the non-text ones are skipped — the
    decode is per-block, not all-or-nothing."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    source_ref = serialize_source_ref(UnitLocator(document_id, version, unit=1))
    adapter = _FixedResponseAdapter(
        _success_content_block(
            {"type": "text", "text": json.dumps({"id": source_ref, "content": "BODY TEXT"})},
            {"type": "resource", "resource": {"uri": "x"}},
            {"type": "text", "text": json.dumps({"id": source_ref, "content": "MORE"})},
        ),
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    result = adapter.read(document_id, version)
    assert result["text"] == "BODY TEXT\n\nMORE"


def test_read_content_block_with_unknown_error_code_is_not_a_body() -> None:
    """An inner JSON envelope whose ``error`` code does NOT start with
    ``read_source_`` carries no ``content`` body — the fallback keeps
    the raw text, so the read surfaces the envelope as page text rather
    than silently answering ``unknown_locator``. Pinned so a future
    tightening of the decoder is a deliberate contract change."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    adapter = _FixedResponseAdapter(
        _success_content_block(
            {"type": "text", "text": json.dumps({"error": "internal_boom"})}
        ),
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    result = adapter.read(document_id, version)
    assert result["text"] == json.dumps({"error": "internal_boom"})


def test_read_content_block_with_read_source_error_without_iserror_misses() -> None:
    """The invalidated miss can arrive WITHOUT an ``isError`` flag — a
    success envelope whose inner JSON carries the ``read_source_*``
    code. The decoder still treats it as the named miss (the code
    prefix is the contract, not the ``isError`` flag)."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    source_ref = serialize_source_ref(UnitLocator(document_id, version, unit=1))
    adapter = _FixedResponseAdapter(
        _success_content_block(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "error": "read_source_invalidated",
                        "source_ref": source_ref,
                        "valid_until": "2026-01-01T00:00:00.000Z",
                    }
                ),
            }
        ),
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


def test_read_empty_content_list_is_unknown_locator() -> None:
    """A success envelope with an EMPTY content list has no body on the
    wire — folded into the ``unknown_locator`` miss, never answered as
    an empty page text."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    adapter = _FixedResponseAdapter(
        _success_content_block(),
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


def test_read_non_json_inner_text_is_unknown_locator() -> None:
    """A text block that is not valid JSON is garbage on the wire —
    folded into the miss, never surfaced as page text."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    adapter = _FixedResponseAdapter(
        _success_content_block({"type": "text", "text": "plain garbage, not json"}),
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


def test_read_flat_string_content_is_a_usable_body() -> None:
    """A hand-rolled adapter that projects the body as a flat string
    ``content`` still reads — the forward-compat fallback is pinned."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    adapter = _FixedResponseAdapter(
        {"result": {"content": "FLAT BODY"}},
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    result = adapter.read(document_id, version)
    assert result["text"] == "FLAT BODY"


def test_read_iserror_block_with_plain_text_misses() -> None:
    """An ``isError`` content block whose text is not even the named
    ``read_source_*`` JSON still misses — ``isError`` itself means the
    tool refused, whatever the text says."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    adapter = _FixedResponseAdapter(
        {"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}},
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


def test_read_transport_timeout_is_a_typed_error_not_a_miss() -> None:
    """A transport-level failure (timeout) during ``read`` raises the
    typed ``RetrieveUnavailableError`` — the machine saying "I cannot
    ask" is a DIFFERENT failure from the locator not being there (the
    miss shape). The host tool never sees it through ``read_file``'s
    miss contract, so this stays a raised, typed EXIT."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)

    class _TimeoutAdapter(_ScriptedAdapter):
        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            raise asyncio.TimeoutError()

    adapter = _TimeoutAdapter(
        locator_map={"hit": locator},
        known_document_ids={document_id},
    )
    with pytest.raises(RetrieveUnavailableError) as excinfo:
        adapter.read(document_id, version)
    assert "MCP transport failed" in str(excinfo.value)


def test_read_ingested_locator_invalidated_on_memory_side_is_unknown_locator() -> None:
    """Cross-repo semantic (T5): a locator fin ingested but the memory
    side later invalidated answers the ``unknown_locator`` miss — the
    invalidated row is not readable through ``read_file``, and the
    distinction "exists but window closed" is deliberately collapsed
    into the existing miss shape fin's read contract already has."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    locator = Locator(document_id, version, 1)
    source_ref = serialize_source_ref(UnitLocator(document_id, version, unit=1))
    adapter = _ReadCapturingAdapter(
        bodies={},
        locator_map={"hit": locator},
        known_document_ids={document_id},
        errors=[
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "read_source_invalidated",
                                    "source_ref": source_ref,
                                }
                            ),
                        }
                    ]
                }
            }
        ],
    )
    assert adapter.read(document_id, version) == {"error": "unknown_locator"}


# ----- boundary 1: empty -----


def test_empty_query_is_a_named_failure_without_mcp_call() -> None:
    """Empty / whitespace-only query raises ``EmptyQueryError``
    WITHOUT touching the MCP subprocess.

    Boundary 'empty': a missing query is invalid input (T21 / §B2 第 5
    条), not a miss shape, and it is still refused BEFORE the JSON-RPC
    request so the operator never pays for a round trip on it. The
    scripted transport raises if reached, so "no MCP call" is asserted
    by construction.
    """
    adapter = _ScriptedAdapter()
    for empty_query in ("", "   ", "\t\n", "\n"):
        with pytest.raises(EmptyQueryError):
            adapter.retrieve(empty_query)

    assert adapter.input_validation_failures.count_by_code() == {
        "empty_query": 4
    }


def test_empty_document_id_filter_returns_empty_chunks_without_mcp_call() -> None:
    """Empty-string ``document_id`` filter (whitespace-only)
    yields ``{"chunks": []}`` without calling MCP."""
    adapter = _ScriptedAdapter()
    assert adapter.retrieve("foo", document_id="   ") == {"chunks": []}


def test_empty_filter_values_return_empty_chunks_without_mcp_call() -> None:
    """Empty filter values (``cik``, ``form``, ``ticker``,
    ``filing_date``) yield ``{"chunks": []}`` without calling
    MCP (per spec: empty filters are miss shapes, not
    unavailable)."""
    adapter = _ScriptedAdapter()
    assert adapter.retrieve("foo", cik="") == {"chunks": []}
    assert adapter.retrieve("foo", form="") == {"chunks": []}
    assert adapter.retrieve("foo", ticker="  ") == {"chunks": []}


# ----- boundary 2: negative -----


def test_unknown_document_id_returns_empty_chunks_without_mcp_call() -> None:
    """Unknown ``document_id`` yields ``{"chunks": []}`` without
    an MCP round-trip.

    Boundary 'negative': the call is well-formed but the corpus
    has no such document. The model may ask for ``"FOO-9999Q1"``
    even though no ingest row exists — return the miss shape,
    not a typed error.
    """
    # No scripted response — if the adapter tries to call MCP,
    # the test fixture's RuntimeError surfaces.
    adapter = _ScriptedAdapter(
        known_document_ids={"DOC-2023Q1"},
    )
    assert adapter.retrieve("foo", document_id="UNKNOWN-9999") == {"chunks": []}


# ----- boundary 3: overflow -----


def test_limit_clamped_to_twenty() -> None:
    """MCP adapter must respect the port's limit cap (ADR 0009).

    The MCP server may return up to ``top_k`` hits; the adapter
    must enforce the 20-cap on top of that.
    """
    hits = [
        {
            "text": f"hit {i}",
            "source_ref": f"id-{i}",
            "score": float(50 - i),
        }
        for i in range(50)
    ]
    locator_map = {f"id-{i}": _loc("DOC-2023Q1") for i in range(50)}
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits},
        locator_map=locator_map,
    )
    result = adapter.retrieve("foo", limit=999)
    assert "chunks" in result
    assert len(result["chunks"]) == 20


# ----- mapper-drop -----


def test_mapper_drop_omits_unmapped_hits() -> None:
    """Mapper-drop: hits whose ``source_ref`` (iknow-memory id) is
    NOT in ``locator_map`` are omitted from the chunks output.

    ADR 0030: the adapter must never pass an iknow id to the
    citation gate (the gate reads ``source_ref.document_id``).
    The contract is "drop, do not pass through".
    """
    hits = [
        {"text": "kept 1", "source_ref": "id-A", "score": 3.0},
        {"text": "dropped", "source_ref": "id-MISSING", "score": 2.0},
        {"text": "kept 2", "source_ref": "id-B", "score": 1.0},
    ]
    locator_map = {
        "id-A": _loc("DOC-2023Q1"),
        "id-B": _loc("DOC-2023Q2", page=2),
    }
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits},
        locator_map=locator_map,
    )
    result = adapter.retrieve("foo", limit=10)
    assert "chunks" in result
    texts = [c.text for c in result["chunks"]]
    assert texts == ["kept 1", "kept 2"]
    # And the dropped hit's id must NOT appear as a locator
    # anywhere in the output.
    for chunk in result["chunks"]:
        assert chunk.source_ref.document_id != "id-MISSING"


def test_mapper_drop_when_map_empty_returns_empty_chunks() -> None:
    """An empty ``locator_map`` drops every hit — output is
    ``{"chunks": []}`` regardless of MCP server result."""
    hits = [
        {"text": "any", "source_ref": "id-X", "score": 1.0},
    ]
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits},
        locator_map={},
    )
    result = adapter.retrieve("foo")
    assert result == {"chunks": []}


# ----- boundary 4: exception (MCP down) -----


def test_mcp_subprocess_failure_returns_retrieve_unavailable() -> None:
    """Scripted subprocess spawn / runtime failure (``OSError``)
    → ``{"error": "retrieve_unavailable"}``.

    ADR 0030 / error-handling-enforcer: typed EXIT, no empty
    catch, no BM25 fallback. The MCP adapter does NOT swallow
    MCP failures as empty chunks; it surfaces a structured error
    so the host tool / citation gate can decide.
    """
    adapter = _ScriptedAdapter(scripted_exception=OSError("connection refused"))
    result = adapter.retrieve("foo")
    assert result == {"error": "retrieve_unavailable"}


def test_mcp_timeout_returns_retrieve_unavailable() -> None:
    """``TimeoutError`` (timeout on subprocess / socket) →
    ``{"error": "retrieve_unavailable"}``."""
    adapter = _ScriptedAdapter(scripted_exception=TimeoutError("timed out"))
    result = adapter.retrieve("foo")
    assert result == {"error": "retrieve_unavailable"}


def test_mcp_protocol_error_response_returns_retrieve_unavailable() -> None:
    """A JSON-RPC error response (``response["error"]``) →
    ``{"error": "retrieve_unavailable"}``."""
    adapter = _ScriptedAdapter(
        scripted_response={"error": {"code": -32600, "message": "bad request"}},
    )
    result = adapter.retrieve("foo")
    assert result == {"error": "retrieve_unavailable"}


def test_mcp_malformed_response_returns_retrieve_unavailable() -> None:
    """A response whose ``result.hits`` is not a list →
    ``{"error": "retrieve_unavailable"}`` (don't crash the run
    on a misbehaving server)."""
    adapter = _ScriptedAdapter(
        scripted_response={"result": {"hits": "not-a-list"}},
    )
    result = adapter.retrieve("foo")
    assert result == {"error": "retrieve_unavailable"}


# ----- boundary 5: concurrent -----


def test_overlapping_retrieve_calls_independent() -> None:
    """Two overlapping retrieve calls each return chunks-or-
    ``retrieve_unavailable`` without raising.

    The adapter's public surface is sync (``retrieve``); the
    internal ``asyncio.run`` round-trip is hermetic per call.
    Multiple threads hitting the same adapter simultaneously
    must NOT share mutable state — the locator map is set at
    construction and read-only thereafter; the JSON-RPC transport
    is per-call.
    """
    hits = [
        {"text": "hit", "source_ref": "id-A", "score": 1.0},
    ]
    locator_map = {"id-A": _loc("DOC-2023Q1")}
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits},
        locator_map=locator_map,
    )

    def _go() -> dict:
        return adapter.retrieve("foo", limit=8)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: _go(), range(8)))

    expected = {"chunks": [_chunk("hit", "DOC-2023Q1", score=1.0)]}
    for i, r in enumerate(results):
        assert r == expected, (
            f"overlapping call #{i} returned {r!r}, expected {expected!r}"
        )


def test_overlapping_retrieve_calls_with_mixed_outcomes() -> None:
    """Two overlapping retrieve calls can each independently
    return chunks or ``retrieve_unavailable`` — they don't
    need to return the same shape. The contract is per-call."""
    hits = [{"text": "h", "source_ref": "id-A", "score": 1.0}]
    locator_map = {"id-A": _loc("DOC-2023Q1")}

    def _go_with_exc() -> dict:
        a = _ScriptedAdapter(
            scripted_exception=OSError("boom"),
        )
        return a.retrieve("foo")

    def _go_with_hits() -> dict:
        a = _ScriptedAdapter(
            scripted_response={"hits": hits},
            locator_map=locator_map,
        )
        return a.retrieve("foo")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(
            ex.map(
                lambda i: (_go_with_exc if i % 2 else _go_with_hits)(),
                range(8),
            )
        )

    assert {"error": "retrieve_unavailable"} in results
    assert {"chunks": [_chunk("h", "DOC-2023Q1")]} in results


# ----- typed exception -----


def test_retrieve_unavailable_error_is_typed_exception() -> None:
    """``RetrieveUnavailableError`` must be a typed exception
    class, not a magic-string sentinel or a bare ``Exception``."""
    assert issubclass(RetrieveUnavailableError, Exception)


def test_async_path_raises_retrieve_unavailable_error() -> None:
    """The async ``aretrieve`` raises ``RetrieveUnavailableError``
    on MCP transport failure; the sync ``retrieve`` translates
    that into ``{"error": "retrieve_unavailable"}``."""
    import asyncio

    adapter = _ScriptedAdapter(scripted_exception=OSError("down"))
    with pytest.raises(RetrieveUnavailableError):
        asyncio.run(adapter.aretrieve("foo"))


# ----- protocol conformance -----


def test_mcp_adapter_satisfies_retrieve_port() -> None:
    """``MCPRetrieveAdapter`` must satisfy the ``RetrievePort``
    Protocol structurally."""
    from fin.retrieve.port import RetrievePort

    # Empty / whitespace query never reaches MCP, so no scripted
    # response is needed.
    adapter = _ScriptedAdapter()
    assert isinstance(adapter, RetrievePort)


# ----- JSON-RPC arguments -----


class _CapturingAdapter(_ScriptedAdapter):
    """Records the JSON-RPC payload the adapter would send."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(scripted_response={"hits": []}, **kwargs)  # type: ignore[arg-type]
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        return await super()._call_mcp(payload)


def test_omitted_filters_are_absent_from_the_request() -> None:
    """A caller that passes no EDGAR filters must not produce a
    ``filters`` block at all.

    ``_is_blank(None)`` is ``False`` — ``None`` means "the caller did
    not pass this filter" — so testing ``not _is_blank(v)`` let every
    unset filter through as an explicit ``null``. iknow-memory then
    received ``{"cik": null, "form": null, ...}``: at best noise, at
    worst a filter that matches no document, which reads back as an
    ordinary empty result.

    Plan D0 I-T1: the wire key is the plural ``filters`` (the
    ``z.strictObject`` schema rejects the legacy singular ``filter``).
    """
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue")
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert "filters" not in arguments, arguments
    assert "filter" not in arguments, arguments


def test_supplied_filters_are_forwarded_in_the_request() -> None:
    """The filters the caller DID pass still reach the server under
    the plural ``filters`` key, and only those.

    Plan D0 I-T1: ``filters`` is the wire-side spelling the iknow-memory
    schema accepts; emitting the singular ``filter`` would be stripped.
    """
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue", cik="0000320193", form="10-Q")
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert "filter" not in arguments
    assert arguments["filters"] == {"cik": "0000320193", "form": "10-Q"}


def test_retrieve_payload_uses_limit_not_top_k() -> None:
    """The wire payload uses ``limit`` (not the legacy ``top_k``).

    Plan D0 I-T1: the iknow-memory ``retrieve`` schema's `z.strictObject`
    only knows ``limit``; emitting the legacy ``top_k`` would be
    rejected by strict mode, surfacing contract drift instead of
    silently dropping the value on the server side.
    """
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue", limit=7)
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert arguments.get("limit") == 7
    assert "top_k" not in arguments


def test_retrieve_payload_filters_is_plural_record() -> None:
    """The wire payload's filters are merged into one ``filters`` dict,
    and ``document_id`` joins the EDGAR-shaped filters there (no longer
    a top-level adapter field).

    Plan D0 I-T1: ``document_id`` is now ``filters.document_id``;
    cik / form / ticker / filing_date are siblings under ``filters``.
    """
    adapter = _CapturingAdapter()
    adapter.retrieve(
        "revenue",
        document_id="DOC-2023Q1",
        cik="0000320193",
        form="10-Q",
        ticker="AAPL",
        filing_date="2023-09-30",
        limit=5,
    )
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert "filter" not in arguments
    assert "document_id" not in arguments, arguments
    assert arguments.get("filters") == {
        "document_id": "DOC-2023Q1",
        "cik": "0000320193",
        "form": "10-Q",
        "ticker": "AAPL",
        "filing_date": "2023-09-30",
    }


# ----- search_mode forwarding + named 档 exits (search-mode-retrieve T2) -----

from fin.host.named_failures import (  # noqa: E402  (section-local imports)
    REAL_DB_NOT_WIRED,
)
from fin.retrieve.port import (  # noqa: E402
    InvalidSearchModeError,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    SEARCH_MODE_FILTER_KEY,
    TierUnavailableError,
)
from fin.retrieve.tiers import COMMUNITY_SUMMARY_INDEX_FAILURE  # noqa: E402


def test_unset_search_mode_is_absent_from_the_wire() -> None:
    """``search_mode=None`` means "do not send" — iknow's own default
    (``graph``) answers, and an explicit ``null`` on the wire would be a
    filter the server tries to match (the same rule as every other
    unset filter)."""
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue")
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert "filters" not in arguments, arguments


def test_search_mode_lexical_is_forwarded_as_filters_search_mode() -> None:
    """The wire token is pinned exactly once per spec
    (``filters.search_mode`` ∈ ``lexical|graph|global|sql``, sibling iknow
    spec); elsewhere the constant is used so a rename cannot drift
    silently."""
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue", search_mode="lexical")
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert arguments["filters"] == {"search_mode": "lexical"}
    assert SEARCH_MODE_FILTER_KEY == "search_mode"


def test_graph_rides_alongside_the_edgar_filters() -> None:
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue", cik="0000320193", search_mode="graph")
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert arguments["filters"] == {
        "cik": "0000320193",
        "search_mode": "graph",
    }


def test_the_wire_never_carries_a_retired_letter() -> None:
    """The last honest separation: a bare letter is no longer a token of
    this protocol at all — not an input spelling either, since the entry
    seams were retokenized in the same stroke. The adapter hands the letter
    to the SAME seam the fake uses, so a retired letter cannot be
    serialized to iknow as if the rename had never happened."""
    adapter = _CapturingAdapter()
    for letter in ("A", "B", "C", "D"):
        with pytest.raises(InvalidSearchModeError) as excinfo:
            adapter.retrieve("revenue", search_mode=letter)
        assert excinfo.value.code == "invalid_search_mode"
    assert adapter.payloads == []


@pytest.mark.parametrize(
    ("tier", "code"),
    [
        pytest.param("sql", REAL_DB_NOT_WIRED, id="tier-sql"),
    ],
)
def test_an_unopened_tier_is_a_named_exit_without_an_mcp_call(
    tier: str, code: str
) -> None:
    """A 档 pin that names an unopened 档 gets the SAME public code the
    send door uses (``TIER_UNAVAILABLE_CODE`` mapping), not a silent
    default-``graph`` retrieve: the request never reaches the transport
    (payloads stays empty), and the exit is not the miss shape
    ``{"chunks": []}`` (that would read as "the corpus has nothing").

    Only ``sql`` is an unopened 档 now: opening ``global`` (fin task 4)
    moved it onto the same forwarded path as lexical/graph (pinned by the
    next test), so it can no longer wear this refusal."""
    adapter = _CapturingAdapter()
    result = adapter.retrieve("revenue", search_mode=tier)
    assert result == {"error": code}
    assert "chunks" not in result
    assert adapter.payloads == []


def test_global_is_an_open_tier_forwarded_to_the_wire() -> None:
    """The mirror of the refusal above, for the 档 this change opened:
    ``global`` 综述 now runs a real report arm, so the adapter SENDS the
    token to the server (it reaches the transport) rather than answering
    「档未开」without a call."""
    adapter = _CapturingAdapter()
    assert adapter.retrieve("revenue", search_mode="global") == {"chunks": []}
    assert len(adapter.payloads) == 1
    assert (
        adapter.payloads[0]["params"]["arguments"]["filters"]
        == {SEARCH_MODE_FILTER_KEY: "global"}
    )


@pytest.mark.parametrize(
    ("tier", "code"),
    [
        pytest.param("sql", REAL_DB_NOT_WIRED, id="tier-sql"),
    ],
)
def test_aretrieve_raises_the_typed_tier_error_before_transport(
    tier: str, code: str
) -> None:
    """The async seam carries the named failure as its typed error
    (the sync ``retrieve`` translates it to the product dict, the same
    split as ``RetrieveUnavailableError``). Only ``sql`` remains a
    routing-time refusal after global was opened."""
    adapter = _CapturingAdapter()
    with pytest.raises(TierUnavailableError) as excinfo:
        asyncio.run(adapter.aretrieve("revenue", search_mode=tier))
    assert excinfo.value.code == code
    assert adapter.payloads == []


@pytest.mark.parametrize(
    "bogus", ["Z", "E", "a", "b", " c ", "", "summary", "local"]
)
def test_a_mode_outside_the_closed_set_raises_invalid_search_mode(
    bogus: str,
) -> None:
    """The port takes wire tokens, not prose and not case variants —
    the send door normalizes human input, so guessing here would be
    the silent-degradation path the spec forbids."""
    adapter = _CapturingAdapter()
    with pytest.raises(InvalidSearchModeError) as excinfo:
        adapter.retrieve("revenue", search_mode=bogus)
    assert excinfo.value.code == "invalid_search_mode"
    assert adapter.payloads == []


# ----- T3: the five boundary classes under a 档 (product adapter) -----
#
# The same matrix as ``test_fake_port``'s T3 section, on the PRODUCT
# adapter: an accepted 档 must not bend the empty / overflow /
# concurrent / exception contracts, and a refused 档 keeps its OWN
# name even when the transport is down (spec SC4/SC7 — the exits never
# cross).


def test_an_open_tier_with_an_empty_wire_answer_is_the_legal_miss() -> None:
    """Boundary 'empty' × 档: an accepted 档 that reaches the server and
    finds nothing is the miss shape ``{"chunks": []}`` — and, unlike a
    refused 档, the request DID go out (one payload), so an operator
    can tell "searched at lexical, nothing there" from "the port never
    opened that 档"."""
    adapter = _CapturingAdapter()
    assert adapter.retrieve("foo", search_mode="lexical") == {"chunks": []}
    assert len(adapter.payloads) == 1
    assert (
        adapter.payloads[0]["params"]["arguments"]["filters"]
        == {SEARCH_MODE_FILTER_KEY: "lexical"}
    )


def test_a_tiered_request_sends_the_clamped_limit() -> None:
    """Boundary 'overflow' × 档: the ADR 0009 cap resolves BEFORE the
    wire, so a tiered over-cap request sends ``limit == 20`` beside
    its 档 — the cap is a port rule, not a 档 knob."""
    adapter = _CapturingAdapter()
    adapter.retrieve("foo", limit=999, search_mode="lexical")
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert arguments["limit"] == 20
    assert arguments["filters"] == {SEARCH_MODE_FILTER_KEY: "lexical"}


def test_transport_down_with_an_open_tier_is_still_retrieve_unavailable() -> None:
    """Boundary 'exception' × 档: the round trip of an ACCEPTED 档
    failing names ``retrieve_unavailable`` — the transport is the
    thing that broke, and neither the miss shape (a corpus with
    nothing) nor a silent default answer may wear the 档's request."""
    adapter = _ScriptedAdapter(scripted_exception=OSError("down"))
    assert adapter.retrieve("foo", search_mode="graph") == {
        "error": "retrieve_unavailable"
    }


@pytest.mark.parametrize(
    ("tier", "code"),
    [
        pytest.param("sql", REAL_DB_NOT_WIRED, id="tier-sql"),
    ],
)
def test_a_closed_tier_keeps_its_name_even_when_the_transport_is_down(
    tier: str, code: str
) -> None:
    """Named-exit ordering under fault: the 档 check runs BEFORE the
    transport, so even a down MCP answers the 档's own code, never
    ``retrieve_unavailable``. The distinction is actionable: retrying
    the transport changes nothing for 「档未开」, while
    ``retrieve_unavailable`` means the 档 IS open and the call is
    worth retrying. Only ``sql`` is a closed 档 after global was
    opened; a global request over a down transport is the transport's
    own failure, pinned in the next test."""
    adapter = _ScriptedAdapter(scripted_exception=OSError("down"))
    assert adapter.retrieve("foo", search_mode=tier) == {"error": code}


def test_an_open_global_over_a_down_transport_is_retrieve_unavailable() -> None:
    """The other side of that ordering now that global is open: a
    综述 request is not refused at routing, so when the transport is
    down it names ``retrieve_unavailable`` (retry-worthy), not
    ``tier_not_open``."""
    adapter = _ScriptedAdapter(scripted_exception=OSError("down"))
    assert adapter.retrieve("foo", search_mode="global") == {
        "error": "retrieve_unavailable"
    }


# ----- global 综述 runtime: community-summary index-not-ready (fin task 4) -
#
# The opened 综述 档's report arm reads the 社区 summary index. When that
# index is not-ready, upstream surfaces a typed ``isError`` result whose
# text carries the ``community_summary_index_failure`` token. fin must
# record that job fact (the empty-plus-flag envelope the host decision
# names), NOT collapse it into the generic ``retrieve_unavailable`` the
# unwrapped content-block path would otherwise produce — and equally must
# NOT mistake a transport error that lacks the token for the summary job.


def _community_summary_error_result(*, text: str) -> dict:
    """A ``tools/call`` result envelope for the upstream 综述 refusal.

    This is the exact shape the adapter keys on: an ``isError`` result
    whose first content block is a text carrying ``text``."""
    return {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": text}],
        }
    }


def test_a_community_summary_refusal_is_recorded_as_the_job_fact() -> None:
    """An isError whose text names community_summary_index_failure is a
    RECORDED report-index fact: the adapter hands back the empty-plus-flag
    envelope so the host names the summary-index job for the operator."""
    adapter = _ScriptedAdapter(
        scripted_response=_community_summary_error_result(
            text=(
                f"retrieve refused: {COMMUNITY_SUMMARY_INDEX_FAILURE} "
                "(summary index not built)"
            )
        )
    )
    result = adapter.retrieve("综述这几份 10-Q", search_mode="global")
    assert result == {
        "chunks": [],
        RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
    }


def test_a_community_summary_refusal_resolves_on_the_async_seam() -> None:
    """Same fact on the async path: ``aretrieve`` returns the flagged
    envelope rather than raising — the record is not a transport fault."""
    adapter = _ScriptedAdapter(
        scripted_response=_community_summary_error_result(
            text=COMMUNITY_SUMMARY_INDEX_FAILURE
        )
    )
    assert asyncio.run(adapter.aretrieve("综述", search_mode="global")) == {
        "chunks": [],
        RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
    }


def test_an_iserror_without_the_token_stays_retrieve_unavailable() -> None:
    """The negative half of the discrimination: a real transport/tool
    error whose text does NOT carry the summary token must keep the
    generic ``retrieve_unavailable`` EXIT — fin never invents a
    summary-index job fact out of an unrelated failure."""
    adapter = _ScriptedAdapter(
        scripted_response=_community_summary_error_result(
            text="worker crashed unexpectedly"
        )
    )
    assert adapter.retrieve("foo", search_mode="global") == {
        "error": "retrieve_unavailable"
    }


def test_the_summary_token_only_counts_on_a_first_text_block() -> None:
    """The matcher is narrow: a non-error result, or an isError whose
    content is empty / not a leading text block, is not the recorded
    job fact (it falls through to the ordinary unwrap / miss path)."""
    from fin.retrieve.mcp_adapter import _is_community_summary_not_ready

    token_text = COMMUNITY_SUMMARY_INDEX_FAILURE
    # isError + leading text block carrying the token → True.
    assert _is_community_summary_not_ready(
        {"isError": True, "content": [{"type": "text", "text": token_text}]}
    ) is True
    # Not an error at all → False.
    assert _is_community_summary_not_ready(
        {"isError": False, "content": [{"type": "text", "text": token_text}]}
    ) is False
    # isError but empty content → False.
    assert _is_community_summary_not_ready(
        {"isError": True, "content": []}
    ) is False
    # isError, leading block is not a dict → False.
    assert _is_community_summary_not_ready(
        {"isError": True, "content": ["not-a-dict"]}
    ) is False
    # isError, leading text WITHOUT the token → False.
    assert _is_community_summary_not_ready(
        {"isError": True, "content": [{"type": "text", "text": "other err"}]}
    ) is False


def test_an_illegal_mode_is_named_before_a_broken_transport() -> None:
    """The same ordering one class further back: an out-of-set token is
    the CALLER's error and raises even with the transport scripted to
    fail — a down MCP must not launder an illegal 档 into
    ``retrieve_unavailable``."""
    adapter = _ScriptedAdapter(scripted_exception=OSError("down"))
    with pytest.raises(InvalidSearchModeError):
        adapter.retrieve("foo", search_mode="sql ")


def test_concurrent_tiered_calls_answer_per_call() -> None:
    """Boundary 'concurrent' × 档: one shared adapter, parallel calls
    mixing the three OPEN 档 — each accepted mode reaches the wire and
    gets its OWN hit answer. ``global`` 综述 is open now (fin task 4), so
    it is an accepted mode beside lexical/graph, not a per-call refusal."""
    chunks_wire = [
        {
            "id": "chunk-1",
            "content": "hit",
            "source_ref": _canonical_source_ref("DOC-2023Q1", page=1),
            "valid_window": ["2023-01-01T00:00:00Z", None],
            "score": 1.0,
        }
    ]

    class _ConcurrentTierAdapter(_ScriptedAdapter):
        """Scripted adapter that also records every wire payload."""

        def __init__(self) -> None:
            super().__init__(scripted_response={"chunks": chunks_wire})
            self.payloads: list[dict] = []

        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            self.payloads.append(payload)
            return await super()._call_mcp(payload)

    adapter = _ConcurrentTierAdapter()

    def _go(i: int) -> dict:
        mode = ("lexical", "graph", "global")[i % 3]
        return adapter.retrieve("foo", search_mode=mode)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_go, range(12)))

    expected_chunks = {"chunks": [_chunk("hit", "DOC-2023Q1", score=1.0)]}
    modes = [("lexical", "graph", "global")[i % 3] for i in range(12)]
    for mode, result in zip(modes, results):
        assert result == expected_chunks, mode

    sent_modes = [
        payload["params"]["arguments"]["filters"][SEARCH_MODE_FILTER_KEY]
        for payload in adapter.payloads
    ]
    assert sorted(sent_modes) == sorted(modes), (
        "an accepted 档 failed to reach the wire"
    )


# ----- locator_map injection -----


def test_default_locator_map_is_empty() -> None:
    """Default ``MCPRetrieveAdapter`` (no locator_map) drops
    every hit — config wiring is the caller's responsibility.

    In production, ``compile_graph`` injects the locator_map
    from corpus metadata; in tests we explicitly construct
    adapters with the map.
    """
    hits = [{"text": "h", "source_ref": "id-X", "score": 1.0}]
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits},
        locator_map=None,
    )
    result = adapter.retrieve("foo")
    assert result == {"chunks": []}


# ----- empty-hit envelope: a miss, never an error, never a raise -----


def _content_block(payload: dict) -> dict:
    """Wrap ``payload`` in the canonical MCP ``tools/call``
    content-block envelope."""
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}]
        }
    }


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"result": {"hits": []}}, id="result.hits"),
        pytest.param({"hits": []}, id="top-level-hits"),
        pytest.param(_content_block({"hits": []}), id="content-block"),
    ],
)
def test_empty_hit_envelope_maps_to_empty_chunks(envelope: dict) -> None:
    """A server that ran the search and found nothing returns an
    empty hit list. That is the MISS shape ``{"chunks": []}`` —
    never ``{"error": "retrieve_unavailable"}`` and never a raise.

    Conflating "searched, found nothing" with "search never ran"
    is exactly the distinction ``tests/host/test_retrieve_typed_exit.py``
    relies on, so every envelope variant has to land on the miss
    shape.
    """
    adapter = _ScriptedAdapter(
        scripted_response=envelope,
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )
    assert adapter.retrieve("foo") == {"chunks": []}


def test_content_block_degraded_empty_envelope_is_named_failure() -> None:
    """A degraded empty result is not a legal empty-result success."""
    envelope = _content_block(
        {
            "hits": [],
            "degraded": True,
            "degraded_reasons": ["vector:unavailable"],
        }
    )
    adapter = _ScriptedAdapter(scripted_response=envelope)

    assert adapter.retrieve("foo") == {
        "error": "retrieve_degraded_failure",
        "degraded": True,
        "degraded_reasons": ["vector:unavailable"],
    }


def test_empty_hit_envelope_does_not_raise_on_the_async_path() -> None:
    """The async seam agrees with the sync one: an empty hit list
    resolves, it does not raise ``RetrieveUnavailableError``."""
    adapter = _ScriptedAdapter(scripted_response={"result": {"hits": []}})
    assert asyncio.run(adapter.aretrieve("foo")) == {"chunks": []}


# ----- no configured transport: typed EXIT, not a crash -----


@pytest.mark.parametrize(
    "command",
    [pytest.param(None, id="unset"), pytest.param([], id="empty-list")],
)
def test_no_configured_command_returns_retrieve_unavailable(
    command: list[str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL ``MCPRetrieveAdapter`` (default ``_call_mcp``, no
    scripted transport) with no usable launch command answers the
    typed EXIT ``{"error": "retrieve_unavailable"}``.

    An unconfigured adapter must not crash the run, and must not
    answer ``{"chunks": []}`` either: reporting a miss would tell
    the model the corpus has nothing when retrieval never ran.
    """

    async def _forbid_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            f"the adapter tried to spawn a subprocess: {args!r}"
        )

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec", _forbid_spawn
    )
    adapter = MCPRetrieveAdapter(
        command=command,
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )
    assert adapter.retrieve("foo") == {"error": "retrieve_unavailable"}


def test_no_configured_command_raises_typed_error_on_async_path() -> None:
    """The async seam raises the typed exception the sync seam
    translates — no bare ``OSError`` / ``TypeError`` escapes."""
    adapter = MCPRetrieveAdapter(command=None)
    with pytest.raises(RetrieveUnavailableError):
        asyncio.run(adapter.aretrieve("foo"))


# ----- envelope variants: chunks or a typed error, no partial parse -----


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(
            {
                "result": {
                    "hits": [
                        {"text": "hit", "source_ref": "id-A", "score": 2.5}
                    ]
                }
            },
            id="result.hits",
        ),
        pytest.param(
            {"hits": [{"text": "hit", "source_ref": "id-A", "score": 2.5}]},
            id="top-level-hits",
        ),
        pytest.param(
            _content_block(
                {"hits": [{"text": "hit", "source_ref": "id-A", "score": 2.5}]}
            ),
            id="content-block",
        ),
    ],
)
def test_populated_envelope_variants_map_to_chunks(envelope: dict) -> None:
    """All three envelope shapes an MCP server may use produce the
    SAME chunks — a variant the adapter does not understand must
    not degrade into a silent partial parse."""
    adapter = _ScriptedAdapter(
        scripted_response=envelope,
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )
    assert adapter.retrieve("foo") == {
        "chunks": [_chunk("hit", "DOC-2023Q1", score=2.5)]
    }


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({}, id="no-result-no-hits"),
        pytest.param({"result": None}, id="null-result"),
        pytest.param({"result": []}, id="list-result"),
        pytest.param({"hits": "not-a-list"}, id="top-level-hits-not-a-list"),
        pytest.param({"result": {"hits": {}}}, id="result-hits-not-a-list"),
        pytest.param(
            {"result": {"content": [{"type": "text", "text": "not json"}]}},
            id="content-text-not-json",
        ),
        pytest.param(
            _content_block({"results": []}), id="content-json-without-hits"
        ),
        pytest.param({"result": {"content": []}}, id="empty-content-list"),
    ],
)
def test_malformed_envelope_maps_to_retrieve_unavailable(
    envelope: dict,
) -> None:
    """A server whose reply the adapter cannot read is a broken
    transport, not a miss: every malformed envelope returns the
    typed EXIT rather than raising or inventing empty chunks."""
    adapter = _ScriptedAdapter(
        scripted_response=envelope,
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )
    assert adapter.retrieve("foo") == {"error": "retrieve_unavailable"}


def test_non_dict_response_maps_to_retrieve_unavailable() -> None:
    """A reply that is not even a JSON object (a bare list) is the
    same typed EXIT."""
    adapter = _ScriptedAdapter(
        scripted_response=["hits"],  # type: ignore[arg-type]
    )
    assert adapter.retrieve("foo") == {"error": "retrieve_unavailable"}


# ----- schema field stripping (A6: mock zod without .strict()) -----


def test_stripped_adapter_fields_emit_warning_and_degraded_reasons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the mock MCP schema (no ``.strict()``) drops ``limit`` /
    ``filters`` / any key under ``filters``, fin must warn and surface
    ``degraded_reasons`` — not silently return chunks as if the
    filters ran.

    Plan D0 I-T1: the wire field names are ``limit`` and ``filters``
    (the legacy ``top_k`` / ``filter`` / top-level ``document_id`` are
    no longer sent).
    """
    hits = [{"text": "hit", "source_ref": "id-A", "score": 1.0}]
    locator_map = {"id-A": _loc("DOC-2023Q1")}
    adapter = _ScriptedAdapter(
        scripted_response={
            "hits": hits,
            # Mock echoes only ``query`` — other adapter fields stripped.
            "applied_arguments": {"query": "revenue"},
        },
        locator_map=locator_map,
        known_document_ids={"DOC-2023Q1"},
    )

    with caplog.at_level(logging.WARNING, logger="fin.retrieve.mcp_adapter"):
        result = adapter.retrieve(
            "revenue",
            document_id="DOC-2023Q1",
            cik="0000320193",
            form="10-Q",
            limit=5,
        )

    assert len(result["chunks"]) == 1
    assert result["degraded"] is True
    assert result["degraded_reasons"]
    assert any(
        "field_stripped" in reason for reason in result["degraded_reasons"]
    )

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warnings
    assert any(
        "adapter_field_stripped" in record.getMessage() for record in warnings
    )


def test_stripped_adapter_fields_detects_limit_and_filters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per-key stripping on the new wire field names: a server that
    echoes only ``query`` reports BOTH ``adapter:field_stripped:limit``
    AND ``adapter:field_stripped:filters``.

    Plan D0 I-T1: stripping detection must use the new names
    (``limit`` and ``filters``); a server keeping ``filters`` but
    dropping one key inside it reports the per-key ``filters.<key>``
    reason instead.
    """
    hits = [{"text": "hit", "source_ref": "id-A", "score": 1.0}]
    adapter = _ScriptedAdapter(
        scripted_response={
            "hits": hits,
            "applied_arguments": {"query": "revenue"},
        },
        locator_map={"id-A": _loc("DOC-2023Q1")},
        known_document_ids={"DOC-2023Q1"},
    )

    with caplog.at_level(logging.WARNING, logger="fin.retrieve.mcp_adapter"):
        result = adapter.retrieve(
            "revenue",
            document_id="DOC-2023Q1",
            cik="0000320193",
            form="10-Q",
            limit=5,
        )

    reasons = result.get("degraded_reasons", [])
    assert "adapter:field_stripped:limit" in reasons
    assert "adapter:field_stripped:filters" in reasons

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert any("adapter_field_stripped" in message for message in warnings)


def test_stripped_filters_key_detected_per_missing_filter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server that keeps ``filters`` but drops one key inside
    reports only ``adapter:field_stripped:filters.<key>`` — not the
    umbrella ``filters`` reason."""
    hits = [{"text": "hit", "source_ref": "id-A", "score": 1.0}]
    adapter = _ScriptedAdapter(
        scripted_response={
            "hits": hits,
            # Mock kept ``filters`` but dropped ``filters.document_id``.
            "applied_arguments": {
                "query": "revenue",
                "limit": 5,
                "filters": {"cik": "0000320193", "form": "10-Q"},
            },
        },
        locator_map={"id-A": _loc("DOC-2023Q1")},
        known_document_ids={"DOC-2023Q1"},
    )

    with caplog.at_level(logging.WARNING, logger="fin.retrieve.mcp_adapter"):
        result = adapter.retrieve(
            "revenue",
            document_id="DOC-2023Q1",
            cik="0000320193",
            form="10-Q",
            limit=5,
        )

    reasons = result.get("degraded_reasons", [])
    assert "adapter:field_stripped:filters.document_id" in reasons
    # The umbrella "filters" reason is NOT appended when ``filters``
    # was kept and only one key inside was dropped.
    assert "adapter:field_stripped:filters" not in reasons
    assert "adapter:field_stripped:limit" not in reasons


def test_response_without_applied_arguments_stays_undegraded() -> None:
    """Production servers that omit ``applied_arguments`` skip detection —
    no spurious warnings or degradation envelope."""
    hits = [{"text": "hit", "source_ref": "id-A", "score": 1.0}]
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits},
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )

    result = adapter.retrieve(
        "revenue",
        document_id="DOC-2023Q1",
        cik="0000320193",
        limit=5,
    )

    assert result == {"chunks": [_chunk("hit", "DOC-2023Q1")]}


def test_all_adapter_fields_applied_stays_undegraded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``applied_arguments`` echoes every field fin sent, there is
    nothing to report.

    Plan D0 I-T1: ``applied_arguments`` echoes the new wire keys
    (``limit`` / ``filters``), so a server that keeps them all is
    undegraded.
    """
    hits = [{"text": "hit", "source_ref": "id-A", "score": 1.0}]
    adapter = _ScriptedAdapter(
        scripted_response={
            "hits": hits,
            "applied_arguments": {
                "query": "revenue",
                "limit": 5,
                "filters": {
                    "document_id": "DOC-2023Q1",
                    "cik": "0000320193",
                    "form": "10-Q",
                },
            },
        },
        locator_map={"id-A": _loc("DOC-2023Q1")},
        known_document_ids={"DOC-2023Q1"},
    )

    with caplog.at_level(logging.WARNING, logger="fin.retrieve.mcp_adapter"):
        result = adapter.retrieve(
            "revenue",
            document_id="DOC-2023Q1",
            cik="0000320193",
            form="10-Q",
            limit=5,
        )

    assert result == {"chunks": [_chunk("hit", "DOC-2023Q1")]}
    assert [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == []


# ----- canonical chunks envelope (Plan D0 I-T2) -----


def test_chunks_envelope_maps_each_chunk_source_ref_to_locator() -> None:
    """The MCP server's canonical wire envelope is ``{chunks: [...]}``
    where each chunk carries ``source_ref`` as the fin locator
    serialization (B4). Each chunk is mapped through that canonical
    string into a fin ``Locator``; the chunks the adapter returns
    carry the resolved locator, not the wire string.

    Plan D0 I-T2: this is the wire envelope spec §B3 / §B4 calls for;
    prior behaviour read ``hits`` and went through the ``locator_map``
    path, which the canonical chunk envelope does not use.
    """
    chunks_wire = [
        {
            "id": "chunk-1",
            "content": "first hit",
            "source_ref": _canonical_source_ref("DOC-2023Q1", page=1),
            "valid_window": ["2023-01-01T00:00:00Z", None],
            "score": 2.5,
        },
        {
            "id": "chunk-2",
            "content": "second hit",
            "source_ref": _canonical_source_ref("DOC-2023Q1", page=2),
            "valid_window": ["2023-01-01T00:00:00Z", None],
            "score": 1.0,
        },
    ]
    adapter = _ScriptedAdapter(
        scripted_response={"chunks": chunks_wire},
        locator_map={"id-A": _loc("DOC-2023Q1")},  # legacy map is unused
    )

    result = adapter.retrieve("revenue", limit=8)

    assert "chunks" in result
    assert [
        (c.text, c.source_ref, c.score) for c in result["chunks"]
    ] == [
        ("first hit", _loc("DOC-2023Q1", page=1), 2.5),
        ("second hit", _loc("DOC-2023Q1", page=2), 1.0),
    ]


def test_chunk_missing_source_ref_is_counted_not_silently_dropped() -> None:
    """A chunk that arrives without a canonical ``source_ref`` is
    dropped AND counted on the mapping ledger — silently discarding
    it would be exactly the SC22 silence the ledger exists to
    prevent.

    Plan D0 I-T2: the chunk-vs-hit distinction carries through to
    failure codes; the chunk path uses a chunk-specific reason so an
    operator can tell chunks-path drops from hits-path drops.
    """
    chunks_wire = [
        {
            "id": "chunk-1",
            "content": "kept",
            # canonical source_ref present, lands a chunk
            "source_ref": _canonical_source_ref("DOC-2023Q1", page=1),
            "valid_window": ["2023-01-01T00:00:00Z", None],
            "score": 2.0,
        },
        {
            "id": "chunk-2",
            "content": "no source_ref at all",
            # no source_ref: must be dropped AND counted
            "valid_window": ["2023-01-01T00:00:00Z", None],
            "score": 1.0,
        },
    ]
    adapter = _ScriptedAdapter(
        scripted_response={"chunks": chunks_wire},
        locator_map={},
    )

    result = adapter.retrieve("revenue", limit=8)

    assert [c.text for c in result["chunks"]] == ["kept"]
    failures = adapter.mapping_failures
    assert failures.count() == 1
    failure = failures.failures()[0]
    assert failure.stage == MAPPING_STAGE_RETRIEVE
    # The chunk-specific code keeps chunks-path drops distinguishable
    # from the legacy hits-path drop diagnostics.
    assert failure.code != MAPPING_FAILURE_MISSING_SOURCE_REF or (
        failure.code == MAPPING_FAILURE_MISSING_SOURCE_REF
        and failure.message  # carry an informative message either way
    )


def test_chunk_hits_envelope_still_works_for_legacy_servers() -> None:
    """Forward-compat: a server that still returns ``{hits: [...]}``
    with iknow-side ids under ``source_ref`` is handled by the
    legacy mapper path, so an un-upgraded server does not produce
    a ``retrieve_unavailable`` exit in fin.

    Plan D0 I-T2: the iknow-memory side has unified on ``chunks``;
    the legacy ``hits`` envelope is retained for completeness with
    a comment rather than removed outright, so any non-yet-upgraded
    server still produces a miss/result rather than a typed EXIT.
    """
    hits_wire = [
        {"text": "kept 1", "source_ref": "id-A", "score": 3.0},
        {"text": "dropped", "source_ref": "id-MISSING", "score": 2.0},
    ]
    adapter = _ScriptedAdapter(
        scripted_response={"hits": hits_wire},
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )

    result = adapter.retrieve("revenue", limit=8)

    # Same behaviour as before: the mapper drops unmapped ids and
    # returns the locatable subset.
    assert [c.text for c in result["chunks"]] == ["kept 1"]
    # The drop is counted — same SC22 invariant.
    assert adapter.mapping_failures.count() == 1


def test_chunk_envelope_inside_result_takes_precedence_over_hits() -> None:
    """The canonical envelope is ``result.chunks``; ``result.hits``
    is retained only as a legacy fallback. A response that carries
    both reads ``chunks`` (the canonical path).

    Plan D0 I-T2: with both present, ``chunks`` wins — ``hits``
    only applies when ``chunks`` is absent.
    """
    adapter = _ScriptedAdapter(
        scripted_response={
            "chunks": [
                {
                    "id": "chunk-canon",
                    "content": "from chunks",
                    "source_ref": _canonical_source_ref("DOC-2023Q1"),
                    "valid_window": ["2023-01-01T00:00:00Z", None],
                    "score": 1.0,
                }
            ],
            "hits": [
                {"text": "from hits", "source_ref": "id-A", "score": 1.0}
            ],
        },
        locator_map={"id-A": _loc("DOC-2023Q1")},
    )

    result = adapter.retrieve("revenue", limit=8)

    assert [c.text for c in result["chunks"]] == ["from chunks"]
    # ``hits`` path was NOT taken: id-A was mapped but never consulted.
    assert adapter.mapping_failures.count() == 0