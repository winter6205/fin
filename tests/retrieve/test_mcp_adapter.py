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
* the read seam is ``read_slice({document_id, version, unit}, position)``:
  exactly one bounded slice per round trip, answered as
  ``{source_ref, text, next_position}`` or one typed failure — never a
  raise and never a page window

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
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    INVALID_POSITION,
    SOURCE_BACKEND_CONTRACT_ERROR,
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    SOURCE_UNAVAILABLE,
    UNKNOWN_LOCATOR,
    is_slice_result,
    serialized_result_bytes,
)


def _loc(document_id: str, unit: int = 1) -> Locator:
    return Locator(
        document_id=document_id,
        version="v-" + "a" * 60,
        unit=unit,
    )


def _chunk(text: str, document_id: str, unit: int = 1, score: float = 1.0) -> Chunk:
    return Chunk(
        text=text,
        source_ref=_loc(document_id, unit=unit),
        score=score,
    )


def _canonical_source_ref(document_id: str, unit: int = 1) -> str:
    """The B4 canonical fin ``source_ref`` for a (document_id, unit) pair.

    Plan D0 I-T2: the wire envelope sends ``source_ref`` as the fin
    locator serialization; tests build the canonical string via the
    single authority for that serialization, ``fin.corpus.push``.
    """
    return serialize_source_ref(
        UnitLocator(
            document_id=document_id,
            version="v-" + "a" * 60,
            unit=unit,
        )
    )


# ----- wire envelope builders: the shape the real server drives -----


def _wire_slice(
    source_ref: str, content: str, next_position: int | None
) -> dict:
    """One ``read_source`` success envelope: a text block whose JSON is
    ``{id, source_ref, content, next_position}``."""
    inner = {
        "id": source_ref,
        "source_ref": source_ref,
        "content": content,
        "next_position": next_position,
    }
    return {"result": {"content": [{"type": "text", "text": json.dumps(inner)}]}}


def _wire_refusal(
    code: str, source_ref: str, *, flag_error: bool = True, **fields: object
) -> dict:
    """One named backend refusal, sent the way the real server sends it:
    an ``isError`` result whose text block carries ``{error, source_ref}``.
    """
    result: dict = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"error": code, "source_ref": source_ref, **fields}
                ),
            }
        ]
    }
    if flag_error:
        result["isError"] = True
    return {"result": result}


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
    """Scripted ``_call_mcp`` for ``read_slice()``.

    The fake returns the **real** MCP wire shape — a text content block
    carrying a JSON envelope with ``content`` and ``next_position`` — so
    the scripted suite exercises the same decode path the real
    iknow-memory server drives. ``bodies`` maps the canonical
    ``source_ref`` → the FULL stored text of that frozen unit, and the
    fake slices it at the requested cursor the way the bounded wire
    does. A ``source_ref`` with no stored unit answers the same named
    ``read_source_not_found`` refusal the real server sends. ``errors``
    injects scripted envelopes ahead of that lookup.
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
        arguments = payload["params"]["arguments"]
        source_ref = arguments["source_ref"]
        position = arguments["position"]
        text = self.bodies.get(source_ref)
        if text is None:
            return _wire_refusal("read_source_not_found", source_ref)
        if position > len(text):
            return _wire_refusal(
                "read_source_invalid_position",
                source_ref,
                code_points=len(text),
            )
        end = min(position + SOURCE_READ_MAX_TEXT_CODE_POINTS, len(text))
        return _wire_slice(source_ref, text[position:end], end if end < len(text) else None)


def test_mcp_read_slice_addresses_exactly_the_locator_it_is_given() -> None:
    """One read resolves exactly the unit its locator names.

    The read never fans out to a neighbouring unit and never merges two
    units into one body: each call carries its own canonical
    ``source_ref``, answers with the request identity echoed back, and
    returns only that unit's text.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    first_ref = _canonical_source_ref(document_id, unit=1)
    second_ref = _canonical_source_ref(document_id, unit=2)
    adapter = _ReadCapturingAdapter(
        bodies={first_ref: "FIRST PARAGRAPH", second_ref: "SECOND PARAGRAPH"},
        known_document_ids={document_id},
    )
    second = {"document_id": document_id, "version": version, "unit": 2}
    first = {"document_id": document_id, "version": version, "unit": 1}
    assert adapter.read_slice(second) == {
        "source_ref": second,
        "text": "SECOND PARAGRAPH",
        "next_position": None,
    }
    assert adapter.read_slice(first) == {
        "source_ref": first,
        "text": "FIRST PARAGRAPH",
        "next_position": None,
    }
    assert [payload["params"]["arguments"] for payload in adapter.payloads] == [
        {"source_ref": second_ref, "position": 0},
        {"source_ref": first_ref, "position": 0},
    ]


def test_mcp_read_slice_cursor_walk_is_bounded_and_terminates() -> None:
    """The cursor walk replaces the old page window: every slice is
    within the code-point cap, every cursor strictly advances, the walk
    reassembles exactly the stored unit text, and it stops — no extra
    round trip can happen once the cursor is ``None``.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    stored = "A" * SOURCE_READ_MAX_TEXT_CODE_POINTS + "TAIL OF THE UNIT"
    adapter = _ReadCapturingAdapter(
        bodies={_canonical_source_ref(document_id, unit=1): stored},
        known_document_ids={document_id},
    )
    source_ref = {"document_id": document_id, "version": version, "unit": 1}
    position: int | None = 0
    collected = ""
    slices = 0
    while position is not None:
        result = adapter.read_slice(source_ref, position)
        assert is_slice_result(result), result
        assert len(result["text"]) <= SOURCE_READ_MAX_TEXT_CODE_POINTS
        assert serialized_result_bytes(result) <= SOURCE_READ_MAX_RESULT_BYTES
        assert result["next_position"] is None or (
            result["next_position"] > position
        ), result
        collected += result["text"]
        position = result["next_position"]
        slices += 1
    assert collected == stored
    assert slices == 2
    assert [
        payload["params"]["arguments"]["position"] for payload in adapter.payloads
    ] == [0, SOURCE_READ_MAX_TEXT_CODE_POINTS]



def test_mcp_read_miss_and_unknown_document_are_unknown_locator() -> None:
    """A locator the backend cannot find and a document outside the
    ingested set are the one named miss — never an empty success, never
    a body."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    adapter = _ReadCapturingAdapter(
        bodies={}, known_document_ids={document_id}
    )
    unread = {"document_id": document_id, "version": version, "unit": 7}
    miss = adapter.read_slice(unread)
    assert miss == {"error": UNKNOWN_LOCATOR}
    assert "text" not in miss
    unknown = _ReadCapturingAdapter(
        bodies={}, known_document_ids={document_id}
    )
    assert unknown.read_slice(
        {"document_id": "UNKNOWN-2099Q9", "version": version, "unit": 1}
    ) == {"error": UNKNOWN_LOCATOR}
    assert unknown.payloads == []


def test_mcp_read_jsonrpc_error_is_an_outage_not_a_miss() -> None:
    """A machine that refuses to answer at the JSON-RPC level is the
    outage, not a locator that does not exist — the distinction the old
    read contract already drew and the typed read keeps.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    adapter = _ReadCapturingAdapter(
        bodies={_canonical_source_ref(document_id): "BODY"},
        known_document_ids={document_id},
        errors=[{"error": {"code": -32001, "message": "internal error"}}],
    )
    outage = adapter.read_slice(
        {"document_id": document_id, "version": version, "unit": 1}
    )
    assert outage == {"error": SOURCE_UNAVAILABLE}
    assert "text" not in outage


def test_mcp_read_iserror_content_block_is_unknown_locator() -> None:
    """The real ``read_source`` miss wire is an ``isError`` content
    block whose text is a JSON envelope carrying the named code.

    The adapter must surface that as ``unknown_locator`` — the same
    miss shape a locator lookup uses — never as an empty success and
    never as the raw block list.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    canonical = _canonical_source_ref(document_id)
    adapter = _ReadCapturingAdapter(
        bodies={},
        known_document_ids={document_id},
        errors=[_wire_refusal("read_source_not_found", canonical)],
    )
    assert adapter.read_slice(
        {"document_id": document_id, "version": version, "unit": 1}
    ) == {"error": UNKNOWN_LOCATOR}


def test_mcp_read_invalidated_content_block_is_unknown_locator() -> None:
    """A ``read_source_invalidated`` content block — the operator-console
    miss — is also surfaced as ``unknown_locator``.

    The receiving service distinguishes "row exists but window is
    closed" from "row missing", but fin's read contract collapses
    both to the existing miss shape; this guard ensures the
    invalidated code (which also starts with ``read_source_``) does
    not slip through as a successful empty body.
    """
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    canonical = _canonical_source_ref(document_id)
    adapter = _ReadCapturingAdapter(
        bodies={},
        known_document_ids={document_id},
        errors=[
            _wire_refusal(
                "read_source_invalidated",
                canonical,
                valid_until="2026-01-01T00:00:00.000Z",
            )
        ],
    )
    result = adapter.read_slice(
        {"document_id": document_id, "version": version, "unit": 1}
    )
    assert result == {"error": UNKNOWN_LOCATOR}


def test_mcp_read_stale_version_is_the_named_miss_without_text() -> None:
    """A ``version`` the backend does not hold for that document is the
    miss named by the wire, and the failure never carries text — the
    invariant the old pre-transport version check guarded, now enforced
    by the one authority that knows which versions exist.
    """
    document_id = "DOC-2023Q1"
    stored = _canonical_source_ref(document_id)
    adapter = _ReadCapturingAdapter(
        bodies={stored: "REVENUE ROSE"}, known_document_ids={document_id}
    )
    result = adapter.read_slice(
        {"document_id": document_id, "version": "v-" + "b" * 60, "unit": 1}
    )
    assert result == {"error": UNKNOWN_LOCATOR}
    assert "text" not in result
    assert adapter.payloads[0]["params"]["arguments"]["source_ref"] != stored


@pytest.mark.parametrize(
    ("bad_ref", "expected"),
    [
        pytest.param(
            {"document_id": "DOC-2023Q1", "version": "v-" + "a" * 60, "unit": 0},
            {"error": INVALID_LOCATOR, "reason": "locator_fields_malformed"},
            id="unit-below-one",
        ),
        pytest.param(
            {
                "document_id": "DOC-2023Q1",
                "version": "v-" + "a" * 60,
                "unit": True,
            },
            {"error": INVALID_LOCATOR, "reason": "locator_fields_malformed"},
            id="bool-unit",
        ),
        pytest.param(
            {
                "document_id": "DOC-2023Q1",
                "version": "v-" + "a" * 60,
                "unit": 1,
                "page": 1,
            },
            {"error": INVALID_LOCATOR, "reason": "locator_keys_malformed"},
            id="extra-legacy-page-key",
        ),
        pytest.param(
            {
                "document_id": "DOC-2023Q1",
                "version": "v-" + "a" * 60,
            },
            {"error": INVALID_LOCATOR, "reason": "locator_keys_malformed"},
            id="missing-unit",
        ),
    ],
)
def test_mcp_read_invalid_locator_rejects_before_transport(
    bad_ref: dict, expected: dict
) -> None:
    """A locator that is not a typed triple is refused without asking the
    backend anything — the old window validation's short circuit, now on
    the locator shape itself.
    """
    adapter = _ReadCapturingAdapter(bodies={}, known_document_ids={"DOC-2023Q1"})
    assert adapter.read_slice(bad_ref) == expected
    assert adapter.payloads == []


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        pytest.param(-1, {"error": INVALID_POSITION, "position": -1}, id="negative"),
        pytest.param(
            "0",
            {"error": INVALID_POSITION, "reason": "position_not_an_integer"},
            id="string-cursor",
        ),
    ],
)
def test_mcp_read_invalid_cursor_rejects_before_transport(
    position: object, expected: dict
) -> None:
    adapter = _ReadCapturingAdapter(
        bodies={}, known_document_ids={"DOC-2023Q1"}
    )
    assert (
        adapter.read_slice(
            {"document_id": "DOC-2023Q1", "version": "v-" + "a" * 60, "unit": 3},
            position,  # type: ignore[arg-type]
        )
        == expected
    )
    assert adapter.payloads == []


def test_mcp_read_cursor_past_the_unit_is_the_named_position_error() -> None:
    """The overflow boundary a page window used to clamp: a cursor past
    the end of the stored unit is answered by the one party that knows
    the unit's length, and the adapter passes that named position error
    through instead of inventing an empty slice."""
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    stored = "A" * 300
    adapter = _ReadCapturingAdapter(
        bodies={_canonical_source_ref(document_id): stored},
        known_document_ids={document_id},
    )
    assert adapter.read_slice(
        {"document_id": document_id, "version": version, "unit": 1},
        position=len(stored) + 1,
    ) == {
        "error": INVALID_POSITION,
        "position": len(stored) + 1,
        "code_points": len(stored),
    }



# ----- read_source content-block decode: boundary shapes -----


class _FixedResponseAdapter(_ScriptedAdapter):
    """Scripted ``_call_mcp`` returning one fixed wire response for
    ``read_slice()`` — same decode path as ``_ReadCapturingAdapter`` but
    with full control over the response envelope."""

    def __init__(self, response: dict, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._response = response

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        return self._response


def _success_content_block(*blocks: dict) -> dict:
    return {"result": {"content": list(blocks)}}


def _decoding_adapter(response: dict) -> _FixedResponseAdapter:
    """A ``_FixedResponseAdapter`` for the company unit every decode test
    reads: same document, version, and unit, so only the wire shape
    varies between cases."""
    return _FixedResponseAdapter(
        response, known_document_ids={"DOC-2023Q1"}
    )


def _company_ref(unit: int = 1) -> dict:
    document_id, version = "DOC-2023Q1", "v-" + "a" * 60
    return {"document_id": document_id, "version": version, "unit": unit}


def test_read_skips_non_text_blocks_and_reads_one_body() -> None:
    """A content list mixing text blocks with non-text blocks (e.g.
    resource / image blocks) still reads: the non-text ones are skipped
    and the first decodable text block is the answer — one bounded slice
    per call, so a second body block is never stitched onto it."""
    canonical = _canonical_source_ref("DOC-2023Q1")
    adapter = _decoding_adapter(
        _success_content_block(
            {"type": "resource", "resource": {"uri": "x"}},
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "id": canonical,
                        "content": "BODY TEXT",
                        "next_position": None,
                    }
                ),
            },
            {
                "type": "text",
                "text": json.dumps(
                    {"id": canonical, "content": "MORE", "next_position": None}
                ),
            },
        )
    )
    result = adapter.read_slice(_company_ref())
    assert result == {
        "source_ref": _company_ref(),
        "text": "BODY TEXT",
        "next_position": None,
    }


def test_read_content_block_with_unrecognized_error_code_is_a_contract_error() -> None:
    """An inner JSON envelope carrying an ``error`` code fin was not
    taught is a wire breach named as ``source_backend_contract_error``:
    the decoder no longer falls back to handing the raw envelope to the
    model as text, so an unreadable refusal can never masquerade as a
    body."""
    adapter = _decoding_adapter(
        _success_content_block(
            {"type": "text", "text": json.dumps({"error": "internal_boom"})}
        )
    )
    result = adapter.read_slice(_company_ref())
    assert result == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "unrecognized_backend_code",
    }
    assert "text" not in result


def test_read_content_block_with_read_source_error_without_iserror_misses() -> None:
    """The invalidated miss can arrive WITHOUT an ``isError`` flag — a
    success envelope whose inner JSON carries the ``read_source_*``
    code. The decoder still treats it as the named miss (the code
    itself is the contract, not the ``isError`` flag)."""
    canonical = _canonical_source_ref("DOC-2023Q1")
    adapter = _decoding_adapter(
        _wire_refusal(
            "read_source_invalidated",
            canonical,
            flag_error=False,
            valid_until="2026-01-01T00:00:00.000Z",
        )
    )
    assert adapter.read_slice(_company_ref()) == {"error": UNKNOWN_LOCATOR}


def test_read_content_without_a_cursor_is_a_contract_error() -> None:
    """A body the wire never gave a cursor for cannot be told apart from
    a truncated one, so the bounded promise is unverifiable — named as a
    contract error rather than answered as a complete read."""
    canonical = _canonical_source_ref("DOC-2023Q1")
    adapter = _decoding_adapter(
        _success_content_block(
            {"type": "text", "text": json.dumps({"id": canonical, "content": "BODY"})}
        )
    )
    assert adapter.read_slice(_company_ref()) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "next_position_absent",
    }


def test_read_non_advancing_cursor_is_a_contract_error() -> None:
    """A cursor that repeats the requested position can never terminate a
    walk, so it is refused instead of handed over as a loop the model
    would have to break itself."""
    canonical = _canonical_source_ref("DOC-2023Q1")
    adapter = _decoding_adapter(_wire_slice(canonical, "BODY", 0))
    assert adapter.read_slice(_company_ref()) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "non_advancing_cursor",
    }


def test_read_empty_content_list_is_a_contract_error() -> None:
    """A success envelope with an EMPTY content list has no body on the
    wire — named as an undecodable envelope, never answered as an empty
    slice that a caller would read as a complete unit."""
    adapter = _decoding_adapter(_success_content_block())
    assert adapter.read_slice(_company_ref()) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "envelope_undecodable",
    }


def test_read_non_json_inner_text_is_a_contract_error() -> None:
    """A text block that is not valid JSON is garbage on the wire —
    named, never surfaced as unit text."""
    adapter = _decoding_adapter(
        _success_content_block({"type": "text", "text": "plain garbage, not json"})
    )
    result = adapter.read_slice(_company_ref())
    assert result == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "envelope_undecodable",
    }
    assert "text" not in result


def test_read_flat_string_content_is_a_contract_error() -> None:
    """A hand-rolled adapter that projects the body as a flat string
    ``content`` is not the bounded wire: the strict decoder names the
    breach instead of reading an unvalidated body."""
    adapter = _decoding_adapter({"result": {"content": "FLAT BODY"}})
    assert adapter.read_slice(_company_ref()) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "envelope_undecodable",
    }


def test_read_iserror_block_with_plain_text_is_a_contract_error() -> None:
    """An ``isError`` content block whose text is not even the named
    ``read_source_*`` JSON carries no code to map, so it is the wire
    breach it reads as — the refusal is named, never turned into a
    body."""
    adapter = _decoding_adapter(
        {"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}}
    )
    assert adapter.read_slice(_company_ref()) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "envelope_undecodable",
    }


def test_read_transport_timeout_is_a_typed_error_not_a_miss() -> None:
    """A transport-level failure (timeout) during a read answers the
    outage code — the machine saying "I cannot ask" is a DIFFERENT
    failure from the locator not being there (the miss shape), and
    neither one raises at the model or carries text."""
    document_id = "DOC-2023Q1"
    canonical = _canonical_source_ref(document_id)

    class _TimeoutAdapter(_ScriptedAdapter):
        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            raise asyncio.TimeoutError()

    adapter = _TimeoutAdapter(known_document_ids={document_id})
    outage = adapter.read_slice(_company_ref())
    assert outage == {"error": SOURCE_UNAVAILABLE}

    missing = _ReadCapturingAdapter(
        bodies={}, known_document_ids={document_id}
    )
    assert missing.read_slice(_company_ref()) == {"error": UNKNOWN_LOCATOR}
    assert missing.payloads[0]["params"]["arguments"] == {
        "source_ref": canonical,
        "position": 0,
    }


def test_read_ingested_locator_invalidated_on_memory_side_is_unknown_locator() -> None:
    """Cross-repo semantic: a locator fin ingested but the memory side
    later invalidated answers the ``unknown_locator`` miss — the
    invalidated row is not readable through ``read_source``, and the
    distinction "exists but window closed" is deliberately collapsed
    into the existing miss shape fin's read contract already has."""
    canonical = _canonical_source_ref("DOC-2023Q1")
    adapter = _ReadCapturingAdapter(
        bodies={},
        known_document_ids={"DOC-2023Q1"},
        errors=[
            _wire_refusal("read_source_invalidated", canonical, flag_error=False)
        ],
    )
    assert adapter.read_slice(_company_ref()) == {"error": UNKNOWN_LOCATOR}



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
        "id-B": _loc("DOC-2023Q2", unit=2),
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
    INVALID_BUSINESS_QUERY,
    REAL_DB_NOT_WIRED,
)
from fin.retrieve.port import (  # noqa: E402
    InvalidBusinessQueryError,
    InvalidSearchModeError,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    SEARCH_MODE_FILTER_KEY,
    SQL_WINDOW_END_FILTER_KEY,
    SQL_WINDOW_START_FILTER_KEY,
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


_SQL_WINDOW = {"sql_window_start": "2026-01-01", "sql_window_end": "2026-12-31"}


def test_sql_is_an_open_tier_forwarded_to_the_wire() -> None:
    """A2 (issue #322 T7) retired the routing-time sql refusal: a ledger
    request with a bindable window now RUNS — it reaches the transport
    and carries its 档 token beside the bound window pair. This is the
    old 「档未开」pin turned into its acceptance mirror (spec §4: opening a
    档 is one table row and nowhere else)."""
    adapter = _CapturingAdapter()
    result = adapter.retrieve("revenue", search_mode="sql", **_SQL_WINDOW)
    assert "error" not in result, result
    assert len(adapter.payloads) == 1
    filters = adapter.payloads[0]["params"]["arguments"]["filters"]
    assert filters[SEARCH_MODE_FILTER_KEY] == "sql"
    assert filters[SQL_WINDOW_START_FILTER_KEY] == "2026-01-01"
    assert filters[SQL_WINDOW_END_FILTER_KEY] == "2026-12-31"


def test_a_producer_not_provisioned_failure_keeps_its_own_name() -> None:
    """The retired table row's token survived NARROWED:
    ``real_db_not_wired`` is now what the PRODUCER declares — a structured
    ``isError`` body naming it passes through under its own name, never
    collapsing into ``retrieve_unavailable`` (spec §7: not-provisioned
    asks for provisioning, an outage asks for a retry; conflating them
    sends the operator the wrong advice)."""
    adapter = _ScriptedAdapter(
        scripted_response=_an_error_result(
            text=json.dumps(
                {
                    "error": REAL_DB_NOT_WIRED,
                    "message": "no business ledger provisioned on this instance",
                }
            )
        )
    )
    assert adapter.retrieve(
        "revenue", search_mode="sql", **_SQL_WINDOW
    ) == {"error": REAL_DB_NOT_WIRED}


def test_global_is_an_open_tier_forwarded_to_the_wire() -> None:
    """The other 档 that walked this same path when it opened (fin task
    4): ``global`` 综述 runs a real report arm, so the adapter SENDS the
    token to the server (it reaches the transport) rather than answering
    「档未开」without a call."""
    adapter = _CapturingAdapter()
    assert adapter.retrieve("revenue", search_mode="global") == {"chunks": []}
    assert len(adapter.payloads) == 1
    assert (
        adapter.payloads[0]["params"]["arguments"]["filters"]
        == {SEARCH_MODE_FILTER_KEY: "global"}
    )


def test_aretrieve_raises_the_typed_sql_refusal_before_transport() -> None:
    """The async seam still raises its named typed error BEFORE any round
    trip — only the refusal's identity moved: after A2 no 档 is refused at
    routing, and what the port refuses pre-transport is a ``sql`` request
    whose window the template cannot bind (same code the producer names
    before it connects; a defaulted window would be a query the operator
    never asked)."""
    adapter = _CapturingAdapter()
    with pytest.raises(InvalidBusinessQueryError) as excinfo:
        asyncio.run(adapter.aretrieve("revenue", search_mode="sql"))
    assert excinfo.value.code == INVALID_BUSINESS_QUERY
    assert adapter.payloads == []


def test_aretrieve_resolves_the_producer_not_provisioned_exit_by_name() -> None:
    """The async path surfaces the routed sql 档's producer declaration
    under its own name (the sync seam's mirror): ``aretrieve`` answers the
    typed EXIT dict rather than raising — a recorded refusal is not a
    transport fault."""
    adapter = _ScriptedAdapter(
        scripted_response=_an_error_result(
            text=json.dumps(
                {
                    "error": REAL_DB_NOT_WIRED,
                    "message": "no business ledger provisioned",
                }
            )
        )
    )
    assert asyncio.run(
        adapter.aretrieve("revenue", search_mode="sql", **_SQL_WINDOW)
    ) == {"error": REAL_DB_NOT_WIRED}


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
# concurrent / exception contracts, and since A2 every closed-set 档 is
# accepted, so a fault under a 档 request names its OWN failure level
# (transport outage vs producer declaration — spec SC4/SC7, the exits
# never cross).


def test_an_open_tier_with_an_empty_wire_answer_is_the_legal_miss() -> None:
    """Boundary 'empty' × 档: an accepted 档 that reaches the server and
    finds nothing is the miss shape ``{"chunks": []}`` — and the request
    DID go out (one payload), so an operator can tell "searched at
    lexical, nothing there" from a named failure that never spent a
    round trip (an out-of-set token, or an unbindable sql window)."""
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


def test_sql_over_a_down_transport_is_retrieve_unavailable() -> None:
    """Named-exit ordering under fault, re-pinned for the OPEN sql 档:
    nothing is refused before the transport anymore, so a ledger request
    over a down MCP names the thing that actually broke —
    ``retrieve_unavailable`` (retry-worthy). The distinction stays two
    tests deep: that retry advice must never wear the producer's
    not-provisioned name, and the structured ``real_db_not_wired`` exit
    (pinned above on a live scripted transport) must never wear the
    outage's — spec §7 keeps 未接/未提供, miss, and outage advice apart."""
    adapter = _ScriptedAdapter(scripted_exception=OSError("down"))
    assert adapter.retrieve(
        "foo", search_mode="sql", **_SQL_WINDOW
    ) == {"error": "retrieve_unavailable"}


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
# first text block carries the JSON body
# ``{"error": "community_summary_index_failure", "message": <prose>}``.
# fin classifies on that exact structured field — never on a prose
# substring — and records the job fact (the empty-plus-flag envelope the
# host decision names), NOT collapsing it into the generic
# ``retrieve_unavailable`` the unwrapped content-block path would
# otherwise produce. Equally it must not mistake a transport message that
# merely happens to contain the token for the summary job.


def _an_error_result(*, text: str) -> dict:
    """A ``tools/call`` result envelope whose first content block is a
    text carrying ``text`` — the shape the transport boundary wraps every
    failure in, structured body or bare prose alike."""
    return {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": text}],
        }
    }


def _community_summary_error_result(*, message: str) -> dict:
    """The upstream 综述 refusal on the wire: an ``isError`` result whose
    first text block carries the named error body, ``message`` as prose."""
    return _an_error_result(
        text=json.dumps(
            {
                "error": COMMUNITY_SUMMARY_INDEX_FAILURE,
                "message": message,
            }
        )
    )


def test_a_community_summary_refusal_is_recorded_as_the_job_fact() -> None:
    """An isError carrying the structured error field naming
    community_summary_index_failure is a RECORDED report-index fact: the
    adapter hands back the empty-plus-flag envelope so the host names the
    summary-index job for the operator."""
    adapter = _ScriptedAdapter(
        scripted_response=_community_summary_error_result(
            message="summary index not built"
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
            message="community summaries incomplete"
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
        scripted_response=_an_error_result(text="worker crashed unexpectedly")
    )
    assert adapter.retrieve("foo", search_mode="global") == {
        "error": "retrieve_unavailable"
    }


def test_the_token_in_transport_prose_alone_is_not_the_job_fact() -> None:
    """The false classification prose matching would allow: an
    unstructured transport failure whose message happens to quote the
    token (a retry log line, a stack trace, an echo of the request) has
    no structured error field, so it is still ``retrieve_unavailable``."""
    adapter = _ScriptedAdapter(
        scripted_response=_an_error_result(
            text=(
                "worker exited while replaying: "
                f"{COMMUNITY_SUMMARY_INDEX_FAILURE} "
                "appears in this unrelated transport message"
            )
        )
    )
    assert adapter.retrieve("foo", search_mode="global") == {
        "error": "retrieve_unavailable"
    }


def test_a_named_error_field_with_another_code_is_not_the_job_fact() -> None:
    """The field is matched EXACTLY: a JSON error body naming another
    code — even one spelling the summary token as a fragment — is not
    the 综述 job fact and stays ``retrieve_unavailable``."""
    adapter = _ScriptedAdapter(
        scripted_response=_an_error_result(
            text=json.dumps(
                {
                    "error": (
                        f"{COMMUNITY_SUMMARY_INDEX_FAILURE}_retry_queued"
                    ),
                    "message": "not the refusal",
                }
            )
        )
    )
    assert adapter.retrieve("foo", search_mode="global") == {
        "error": "retrieve_unavailable"
    }


def test_the_summary_token_only_counts_on_a_first_text_block() -> None:
    """The matcher is narrow: a non-error result, an isError whose
    content is empty / not a leading text block, or a leading text that is
    not the named error body is not the recorded job fact (it falls
    through to the ordinary unwrap / miss path)."""
    from fin.retrieve.mcp_adapter import _is_community_summary_not_ready

    def error_body(code: str) -> str:
        return json.dumps({"error": code, "message": "summary index absent"})

    # isError + leading text block carrying the named error field → True.
    assert _is_community_summary_not_ready(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": error_body(COMMUNITY_SUMMARY_INDEX_FAILURE),
                }
            ],
        }
    ) is True
    # Not an error at all → False.
    assert _is_community_summary_not_ready(
        {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": error_body(COMMUNITY_SUMMARY_INDEX_FAILURE),
                }
            ],
        }
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
    # isError, token only in prose (no structured field) → False.
    assert _is_community_summary_not_ready(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"retry log: {COMMUNITY_SUMMARY_INDEX_FAILURE}",
                }
            ],
        }
    ) is False
    # isError, body JSON that is not an object → False.
    assert _is_community_summary_not_ready(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps([COMMUNITY_SUMMARY_INDEX_FAILURE]),
                }
            ],
        }
    ) is False
    # isError, a JSON object that parses cleanly but carries no ``error``
    # field at all — an unrelated failure names no code → False.
    assert _is_community_summary_not_ready(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"message": "transport detail"}),
                }
            ],
        }
    ) is False
    # isError, named error field carrying another code → False.
    assert _is_community_summary_not_ready(
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": error_body("read_source_not_found"),
                }
            ],
        }
    ) is False
    # isError, non-string error field → False.
    assert _is_community_summary_not_ready(
        {
            "isError": True,
            "content": [{"type": "text", "text": '{"error": 7}'}],
        }
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
            "source_ref": _canonical_source_ref("DOC-2023Q1", unit=1),
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
            "source_ref": _canonical_source_ref("DOC-2023Q1", unit=1),
            "valid_window": ["2023-01-01T00:00:00Z", None],
            "score": 2.5,
        },
        {
            "id": "chunk-2",
            "content": "second hit",
            "source_ref": _canonical_source_ref("DOC-2023Q1", unit=2),
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
        ("first hit", _loc("DOC-2023Q1", unit=1), 2.5),
        ("second hit", _loc("DOC-2023Q1", unit=2), 1.0),
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
            "source_ref": _canonical_source_ref("DOC-2023Q1", unit=1),
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