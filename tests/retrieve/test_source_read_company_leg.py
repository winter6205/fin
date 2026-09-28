"""Company-leg tests for the bounded ``read_source`` read (T4, ADR 0088).

Spec ``thread-attachment-source-read`` SC7/SC8/SC9: one
``read_slice({document_id, version, unit}, position)`` round trip on
the MCP adapter answers either the success slice
``{source_ref, text, next_position}`` — the REQUEST identity echoed,
the backend slice passed through — or exactly one typed failure dict,
never an exception, never text on a failure.

All hermetic: every transport answer is scripted through the
documented ``_call_mcp`` seam, so no MCP server or local tree exists.

Boundary classes covered: happy pass-through (incl. the empty
terminal slice), every backend refusal code mapped, malformed /
oversized / non-advancing wire → ``source_backend_contract_error``,
transport raise → ``source_unavailable``, locator and cursor shapes
named BEFORE any round trip, and the legacy whole-body walk following
the cursor to a concatenation.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve import MCPRetrieveAdapter
from fin.retrieve.mcp_adapter import RetrieveUnavailableError
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    INVALID_POSITION,
    SOURCE_BACKEND_CONTRACT_ERROR,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    SOURCE_UNAVAILABLE,
    UNKNOWN_LOCATOR,
    SourceReader,
    is_slice_result,
)

DOCUMENT_ID = "DOC-2023Q1"
VERSION = "v-" + "a" * 60
SOURCE_REF = {"document_id": DOCUMENT_ID, "version": VERSION, "unit": 1}
CANONICAL = serialize_source_ref(UnitLocator(DOCUMENT_ID, VERSION, 1))


# ----- test seam: scripted _call_mcp with payload capture -----


class _SliceAdapter(MCPRetrieveAdapter):
    """``MCPRetrieveAdapter`` whose ``_call_mcp`` replays scripted
    responses (or raises scripted exceptions) and records each payload.
    """

    def __init__(self, responses: list[object], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._responses = list(responses)
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        if not self._responses:
            raise AssertionError("transport called more times than scripted")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def _slice_response(content: str, next_position: int | None) -> dict:
    """The real wire shape: a text block whose JSON is the backend
    envelope ``{id, source_ref, content, next_position}``."""
    inner = {
        "id": CANONICAL,
        "source_ref": CANONICAL,
        "content": content,
        "next_position": next_position,
    }
    return {
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}
    }


def _refusal(code: str, *, flag_error: bool = True, **fields: object) -> dict:
    body = {"error": code, **fields}
    result: dict = {
        "content": [{"type": "text", "text": json.dumps(body)}]
    }
    if flag_error:
        result["isError"] = True
    return {"result": result}


def _adapter(responses: list[object]) -> _SliceAdapter:
    return _SliceAdapter(responses, command=["iknow-memory"])


def _arguments(adapter: _SliceAdapter, index: int = 0) -> dict:
    payload = adapter.payloads[index]
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "read_source"
    return payload["params"]["arguments"]


# ----- protocol membership -----


def test_adapter_satisfies_the_source_reader_protocol() -> None:
    assert isinstance(MCPRetrieveAdapter(command=None), SourceReader)


# ----- happy path: pass-through with the request identity echoed -----


def test_read_slice_passes_the_backend_slice_through() -> None:
    adapter = _adapter([_slice_response("REVENUE ROSE", 6)])
    result = adapter.read_slice(SOURCE_REF, position=0)
    assert result == {
        "source_ref": SOURCE_REF,
        "text": "REVENUE ROSE",
        "next_position": 6,
    }
    assert is_slice_result(result)
    assert _arguments(adapter) == {"source_ref": CANONICAL, "position": 0}


def test_read_slice_echoes_the_request_mapping_not_the_backend_string() -> None:
    """The backend's own ``source_ref`` field is its serialization; the
    result must carry the typed dict the caller handed fin."""
    adapter = _adapter(
        [
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "id": CANONICAL,
                                    "source_ref": "NOT-WHAT-WAS-ASKED",
                                    "content": "OK",
                                    "next_position": None,
                                }
                            ),
                        }
                    ]
                }
            }
        ]
    )
    result = adapter.read_slice(SOURCE_REF)
    assert result["source_ref"] == SOURCE_REF
    assert result["next_position"] is None


def test_read_slice_continuation_sends_the_cursor() -> None:
    adapter = _adapter([_slice_response("TAIL", None)])
    result = adapter.read_slice(SOURCE_REF, position=12)
    assert _arguments(adapter) == {"source_ref": CANONICAL, "position": 12}
    assert result["text"] == "TAIL"


def test_empty_terminal_slice_is_success_not_an_error() -> None:
    """``position == len(text)`` is the legal end: an empty final slice
    with a null cursor completes a walk."""
    adapter = _adapter([_slice_response("", None)])
    result = adapter.read_slice(SOURCE_REF, position=42)
    assert result == {
        "source_ref": SOURCE_REF,
        "text": "",
        "next_position": None,
    }


# ----- named failures before any round trip -----


def test_fat_locator_is_invalid_locator_without_a_round_trip() -> None:
    fat = {
        "document_id": "D" * 1100,
        "version": VERSION,
        "unit": 1,
    }
    adapter = _adapter([])
    assert adapter.read_slice(fat) == {
        "error": INVALID_LOCATOR,
        "reason": "request_locator_oversized",
    }
    assert adapter.payloads == []


def test_unknown_document_is_a_miss_without_a_round_trip() -> None:
    """The adapter's known set is the set ingested on MCP; a document
    outside it is the named miss, mirroring ``retrieve``'s short
    circuit."""
    adapter = _SliceAdapter(
        [], command=["iknow-memory"], known_document_ids={DOCUMENT_ID}
    )
    result = adapter.read_slice(
        {"document_id": "OTHER-2024Q2", "version": VERSION, "unit": 1}
    )
    assert result == {"error": UNKNOWN_LOCATOR}
    assert adapter.payloads == []


@pytest.mark.parametrize(
    "bad_ref",
    [
        pytest.param("just a serialized string", id="not-a-record"),
        pytest.param(None, id="none"),
        pytest.param({"document_id": DOCUMENT_ID, "version": VERSION}, id="missing-unit"),
        pytest.param({**SOURCE_REF, "page": 1}, id="extra-key"),
        pytest.param({"document_id": "", "version": VERSION, "unit": 1}, id="empty-doc-id"),
        pytest.param({"document_id": DOCUMENT_ID, "version": "", "unit": 1}, id="empty-version"),
        pytest.param({"document_id": DOCUMENT_ID, "version": VERSION, "unit": 0}, id="zero-unit"),
        pytest.param({"document_id": DOCUMENT_ID, "version": VERSION, "unit": True}, id="bool-unit"),
        pytest.param({"document_id": DOCUMENT_ID, "version": VERSION, "unit": "1"}, id="string-unit"),
    ],
)
def test_malformed_locator_is_invalid_locator_without_a_round_trip(
    bad_ref: object,
) -> None:
    adapter = _adapter([])
    result = adapter.read_slice(bad_ref)  # type: ignore[arg-type]
    assert result["error"] == INVALID_LOCATOR
    assert adapter.payloads == []


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        pytest.param(-1, {"error": INVALID_POSITION, "position": -1}, id="negative"),
        pytest.param(-100, {"error": INVALID_POSITION, "position": -100}, id="very-negative"),
        pytest.param(
            True,
            {"error": INVALID_POSITION, "reason": "position_not_an_integer"},
            id="bool",
        ),
        pytest.param(
            "0",
            {"error": INVALID_POSITION, "reason": "position_not_an_integer"},
            id="string",
        ),
        pytest.param(
            1.5,
            {"error": INVALID_POSITION, "reason": "position_not_an_integer"},
            id="float",
        ),
    ],
)
def test_inexpressible_cursor_is_named_before_the_wire(
    position: object, expected: dict
) -> None:
    adapter = _adapter([])
    assert adapter.read_slice(SOURCE_REF, position) == expected  # type: ignore[arg-type]
    assert adapter.payloads == []


# ----- backend refusal mapping (spec SC9) -----


def test_not_found_maps_to_unknown_locator() -> None:
    adapter = _adapter([_refusal("read_source_not_found")])
    assert adapter.read_slice(SOURCE_REF) == {"error": UNKNOWN_LOCATOR}


def test_invalidated_maps_to_unknown_locator_and_hides_valid_until() -> None:
    """A closed validity window is a locator the model cannot read; the
    expiry date would only invite a retry it cannot act on."""
    adapter = _adapter(
        [_refusal("read_source_invalidated", valid_until="2026-01-01T00:00:00Z")]
    )
    result = adapter.read_slice(SOURCE_REF)
    assert result == {"error": UNKNOWN_LOCATOR}
    assert "2026" not in json.dumps(result)


def test_backend_invalid_position_echoes_the_cursor_and_length() -> None:
    """Only the backend knows the unit length, so its ``code_points``
    travels; fin's own echo is the REQUESTED position."""
    adapter = _adapter(
        [_refusal("read_source_invalid_position", code_points=77)]
    )
    result = adapter.read_slice(SOURCE_REF, position=90)
    assert result == {
        "error": INVALID_POSITION,
        "position": 90,
        "code_points": 77,
    }


def test_backend_invalid_position_without_code_points_stays_bare() -> None:
    adapter = _adapter([_refusal("read_source_invalid_position")])
    assert adapter.read_slice(SOURCE_REF, position=5) == {
        "error": INVALID_POSITION,
        "position": 5,
    }


def test_backend_invalid_locator_reason_passes_through() -> None:
    adapter = _adapter(
        [_refusal("read_source_invalid_locator", reason="resolved_identity_oversized")]
    )
    assert adapter.read_slice(SOURCE_REF) == {
        "error": INVALID_LOCATOR,
        "reason": "resolved_identity_oversized",
    }


def test_non_string_reason_cannot_vary_the_failure_shape() -> None:
    """A reason passes through only as the plain token the backend names
    it with; prose-shaped junk keeps the bare code."""
    adapter = _adapter(
        [_refusal("read_source_invalid_locator", reason={"nested": 1})]
    )
    assert adapter.read_slice(SOURCE_REF) == {"error": INVALID_LOCATOR}


def test_backend_unavailable_maps_to_source_unavailable() -> None:
    adapter = _adapter([_refusal("read_source_unavailable")])
    assert adapter.read_slice(SOURCE_REF) == {"error": SOURCE_UNAVAILABLE}


def test_refusal_is_mapped_with_or_without_the_isError_flag() -> None:
    """The code is in the body: the flag is the backend's courtesy, not
    the signal fin reads."""
    flagged = _adapter([_refusal("read_source_not_found")])
    bare = _adapter([_refusal("read_source_not_found", flag_error=False)])
    assert flagged.read_slice(SOURCE_REF) == bare.read_slice(SOURCE_REF) == {
        "error": UNKNOWN_LOCATOR
    }


def test_unrecognized_backend_code_is_a_contract_error() -> None:
    """A code fin was not taught cannot fold into a known miss — that
    would hide wire drift."""
    adapter = _adapter([_refusal("read_source_drdift_detected")])
    assert adapter.read_slice(SOURCE_REF) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "unrecognized_backend_code",
    }


# ----- wire breaches: contract error, never salvage, never text -----


def test_content_over_the_code_point_cap_is_refused() -> None:
    adapter = _adapter(
        [_slice_response("X" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 1), None)]
    )
    assert adapter.read_slice(SOURCE_REF) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "content_over_code_point_cap",
    }


def test_result_over_the_byte_cap_is_refused_even_within_the_code_point_cap() -> None:
    """8,000 astral code points are 32,000 UTF-8 bytes; with a long
    locator the complete serialized result crosses 32 KiB while the
    text alone looks compliant. The cap measures the whole result."""
    long_ref = {
        "document_id": "D" * 700,
        "version": VERSION,
        "unit": 1,
    }
    canonical = serialize_source_ref(
        UnitLocator(long_ref["document_id"], VERSION, 1)
    )
    inner = {
        "id": canonical,
        "source_ref": canonical,
        "content": "\U0001F600" * SOURCE_READ_MAX_TEXT_CODE_POINTS,
        "next_position": None,
    }
    adapter = _adapter(
        [{"result": {"content": [{"type": "text", "text": json.dumps(inner)}]}}]
    )
    result = adapter.read_slice(long_ref)
    assert result == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": "result_over_byte_cap",
    }


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param({"result": {}}, "envelope_undecodable", id="no-content"),
        pytest.param(
            {"result": {"content": []}}, "envelope_undecodable", id="empty-blocks"
        ),
        pytest.param(
            {"result": {"content": [{"type": "text", "text": "not json"}]}},
            "envelope_undecodable",
            id="non-json-text",
        ),
        pytest.param(
            {
                "result": {
                    "content": [{"type": "text", "text": json.dumps(42)}]
                }
            },
            "envelope_undecodable",
            id="inner-not-a-record",
        ),
        pytest.param(
            {"result": {"content": "flat string"}},
            "envelope_undecodable",
            id="flat-string-content",
        ),
        pytest.param("bare string", "response_not_an_object", id="non-dict-response"),
    ],
)
def test_undecodable_wire_is_a_contract_error(
    response: object, reason: str
) -> None:
    adapter = _adapter([response])
    assert adapter.read_slice(SOURCE_REF) == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": reason,
    }


def _breach(inner: dict) -> dict:
    return {
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}
    }


@pytest.mark.parametrize(
    ("inner", "reason"),
    [
        pytest.param(
            {"id": "x", "source_ref": CANONICAL, "next_position": None},
            "content_not_text",
            id="content-missing",
        ),
        pytest.param(
            {"content": 7, "next_position": None},
            "content_not_text",
            id="content-not-a-string",
        ),
        pytest.param(
            {"content": "PART", "id": "x"},
            "next_position_absent",
            id="cursor-missing",
        ),
        pytest.param(
            {"content": "PART", "next_position": "5"},
            "next_position_not_an_integer",
            id="cursor-string",
        ),
        pytest.param(
            {"content": "PART", "next_position": True},
            "next_position_not_an_integer",
            id="cursor-bool",
        ),
        pytest.param(
            {"content": "PART", "next_position": 0},
            "non_advancing_cursor",
            id="cursor-equal",
        ),
        pytest.param(
            {"content": "PART", "next_position": 3},
            "non_advancing_cursor",
            id="cursor-behind",
        ),
    ],
)
def test_broken_success_envelope_is_a_contract_error(
    inner: dict, reason: str
) -> None:
    adapter = _adapter([_breach(inner)])
    result = adapter.read_slice(SOURCE_REF, position=4)
    assert result == {
        "error": SOURCE_BACKEND_CONTRACT_ERROR,
        "reason": reason,
    }
    assert "text" not in result, "a contract error must never carry text"


# ----- transport and protocol failures: source_unavailable -----


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(OSError("no MCP command configured"), id="oserror"),
        pytest.param(asyncio.TimeoutError(), id="timeout"),
        pytest.param(TimeoutError(), id="timeouterror"),
        pytest.param(RetrieveUnavailableError("down"), id="unavailable-raise"),
    ],
)
def test_transport_raise_is_an_outage_not_a_miss(exc: BaseException) -> None:
    adapter = _adapter([exc])
    assert adapter.read_slice(SOURCE_REF) == {"error": SOURCE_UNAVAILABLE}


def test_jsonrpc_level_error_is_an_outage() -> None:
    adapter = _adapter(
        [{"error": {"code": -32603, "message": "internal error"}}]
    )
    assert adapter.read_slice(SOURCE_REF) == {"error": SOURCE_UNAVAILABLE}


# ----- legacy whole-body walk (read / _fetch_unit_body) -----


def _read_adapter(responses: list[object]) -> _SliceAdapter:
    return _SliceAdapter(
        responses,
        command=["iknow-memory"],
        locator_map={"hit-1": Locator(DOCUMENT_ID, VERSION, 1)},
        known_document_ids={DOCUMENT_ID},
    )


def test_read_follows_the_cursor_and_concatenates_the_slices() -> None:
    adapter = _read_adapter(
        [_slice_response("FIRST-HALF", 10), _slice_response("-tail", None)]
    )
    result = adapter.read(DOCUMENT_ID, VERSION, page=1, page_limit=1)
    assert result["text"] == "FIRST-HALF-tail"
    assert [p["params"]["arguments"] for p in adapter.payloads] == [
        {"source_ref": CANONICAL},
        {"source_ref": CANONICAL, "position": 10},
    ]


def test_read_of_a_server_without_cursors_needs_one_call() -> None:
    """A pre-bounded server answers the whole unit with no cursor: the
    walk completes on the first response."""
    inner = {"id": CANONICAL, "source_ref": CANONICAL, "content": "WHOLE BODY"}
    adapter = _read_adapter([_breach(inner)])
    result = adapter.read(DOCUMENT_ID, VERSION)
    assert result["text"] == "WHOLE BODY"
    assert len(adapter.payloads) == 1


def test_read_of_a_non_advancing_cursor_folds_to_the_named_miss() -> None:
    adapter = _read_adapter([_slice_response("STUCK", 0)])
    assert adapter.read(DOCUMENT_ID, VERSION) == {"error": "unknown_locator"}


def test_read_walk_stops_at_the_defensive_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that keeps advancing but never terminates must not
    spin the read: past the bound the walk reports the miss rather than
    a partial body."""
    monkeypatch.setattr("fin.retrieve.mcp_adapter._MAX_SLICES_PER_UNIT", 3)
    responses = [_slice_response("AAAA", start + 4) for start in range(0, 4 * 4, 4)]
    adapter = _read_adapter(responses)
    assert adapter.read(DOCUMENT_ID, VERSION) == {"error": "unknown_locator"}
    assert len(adapter.payloads) == 3


def test_read_named_miss_still_folds_to_unknown_locator() -> None:
    adapter = _read_adapter([_refusal("read_source_not_found")])
    assert adapter.read(DOCUMENT_ID, VERSION) == {"error": "unknown_locator"}
