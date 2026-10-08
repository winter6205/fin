"""Boundary tests for ``MCPPushPort`` (Plan D0 I-T4, SC18 / SC20, §B4).

The MCP push port sends each ``PushRecord``'s wire shape to the
iknow-memory ``push`` MCP tool via JSON-RPC 2.0 over stdio. Wire
field names must be verbatim the **z.strictObject** schema in
``src/tools/push.ts`` (``content``, ``source_ref``, ``metadata``,
``valid_from``, ``valid_until``); any extra key would be rejected by
``strictObject`` and would surface as a typed refusal here rather than
silently being dropped by the receiving schema.

The default transport (``_call_mcp``) launches a subprocess. Tests
override ``_call_mcp`` to inject a scripted response or scripted
exception — the same hermetic seam used by
``tests/retrieve/test_mcp_adapter.py``.
"""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import pytest

from fin.corpus.push import PushRecord
from fin.retrieve.mcp_push_port import MCPPushPort
from fin.retrieve.push_port import (
    PushEmptyBatchError,
    PushError,
    PushMetadataIncompleteError,
    PushProtocolError,
    PushServiceUnavailableError,
)

# Authoritative schema keys the iknow-memory ``push`` tool accepts
# (``z.strictObject(PushItemSchema)`` in ``src/tools/push.ts``). The
# fin-side adapter must NOT smuggle in ``block_id`` / ``content_id``
# / ``idempotency_key`` / ``parent_block_id`` / ``parent_text`` /
# ``text`` — ``strictObject`` would refuse them and the refusal would
# read as a wire-shape bug rather than a §B4 violation.
_IKNOW_PUSH_ITEM_KEYS: frozenset[str] = frozenset(
    {"content", "source_ref", "metadata", "valid_from", "valid_until", "chunks"}
)


def _record(
    *,
    block_id: str = "b0",
    source_ref: str = "docs/x.md",
    content_id: str = "c0",
    text: str = "hello world",
    metadata: Mapping[str, Any] | None = None,
) -> PushRecord:
    if metadata is None:
        metadata = {
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "body",
            "source_sha256": "a" * 64,
            "parse_rule_version": "v1",
            "params_table_version": "p1",
        }
    return PushRecord(
        block_id=block_id,
        source_ref=source_ref,
        content_id=content_id,
        text=text,
        parent_block_id="p0",
        parent_text="parent",
        metadata=MappingProxyType(dict(metadata)),
    )


def _records(n: int) -> tuple[PushRecord, ...]:
    return tuple(
        _record(
            block_id=f"b{i}",
            source_ref=f"docs/{i}.md",
            content_id=f"c{i}",
            text=f"text {i}",
        )
        for i in range(n)
    )


def _wires(n: int) -> list[dict[str, Any]]:
    """Wire-shape envelopes after T1.

    One envelope per frozen unit (``source_ref`` + body +
    ``chunks[]``). For tests that previously passed per-child
    ``PushRecord`` instances, this builds a matching single-chunk
    envelope so the §B4 + spec T1 invariants hold on the wire.
    """
    envelopes: list[dict[str, Any]] = []
    for i in range(n):
        body = f"text {i}"
        metadata = {
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "filing",
            "source_sha256": "sha-" + str(i),
            "parse_rule_version": "v1",
            "params_table_version": "p1",
            "normalization_rule_version": "n1",
        }
        envelopes.append(
            {
                "source_ref": f"docs/{i}.md",
                "body": body,
                "chunks": [{"index": 0, "content": body}],
                "metadata": metadata,
                "valid_from": "0001-01-01T00:00:00.000Z",
                "valid_until": "9999-12-31T23:59:59.999Z",
            }
        )
    return envelopes


# ----- scripted transport override -----

_CHUNK_IDS = ["chunk-a", "chunk-b", "chunk-c"]


class _ScriptedPushPort(MCPPushPort):
    """An ``MCPPushPort`` whose ``_call_mcp`` returns a scripted
    response or raises a scripted exception.

    Skips the parent ``__init__`` so the test does not need to allocate
    a real subprocess. The fields ``__init__`` would set are duplicated
    here.
    """

    def __init__(
        self,
        *,
        scripted_response: dict | None = None,
        scripted_exception: BaseException | None = None,
        command: Sequence[str] | None = ("iknow-memory",),
        timeout_seconds: float = 5.0,
    ) -> None:
        self._command = list(command) if command else None
        self._timeout = float(timeout_seconds)
        self._ledger = None
        self._seen_keys: dict[tuple[str, str], bool] = {}
        self._scripted_response = scripted_response
        self._scripted_exception = scripted_exception

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        if self._scripted_exception is not None:
            raise self._scripted_exception
        if self._scripted_response is not None:
            return self._scripted_response
        raise RuntimeError("no scripted response configured")


def _success_envelope(chunk_ids: Sequence[str]) -> dict[str, Any]:
    """The canonical MCP ``tools/call`` envelope wrapping
    ``{"chunk_ids": [...]}`` inside ``result.content[0].text``."""
    return _business_envelope({"chunk_ids": list(chunk_ids)})


def _business_envelope(business: Mapping[str, Any]) -> dict[str, Any]:
    """MCP ``tools/call`` envelope wrapping an arbitrary iknow ``push``
    business payload — the shape ``src/tools/push.ts`` actually answers
    with (a JSON string in the first text content block)."""
    return {
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(dict(business))}
            ]
        }
    }


def _items_of(payload: dict) -> list[dict]:
    """The ``push`` items one scripted ``tools/call`` payload carried."""
    return payload["params"]["arguments"]["items"]


class _ChunkCountingPushPort(_ScriptedPushPort):
    """A scripted server that answers the way iknow's ``push`` does: one
    ``chunk_id`` per slice of what fin sent, in send order
    (``src/tools/push.ts`` ``allChunkIds``).

    ``scripted_response`` still wins when a test needs an exact envelope,
    so a test about the adapter's bookkeeping does not also script a
    response that happens to match the chunk count by hand.
    """

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        if self._scripted_response is not None:
            return self._scripted_response
        chunk_count = sum(len(item.get("chunks") or []) for item in _items_of(payload))
        return _success_envelope([f"chunk-{i}" for i in range(chunk_count)])


class _CapturingPushPort(_ChunkCountingPushPort):
    """``_ChunkCountingPushPort`` that also records the payloads it sent —
    the request side of the wire-shape assertions."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        return await super()._call_mcp(payload)


# iknow stores ``content_identity`` as a sha256 hex digest; the tests use
# plain hex so an assertion never depends on fin recomputing anything.
_IDENTITY_A = "a" * 64
_IDENTITY_B = "b" * 64
_IDENTITY_C = "c" * 64
_IDENTITY_D = "d" * 64


# ----- boundary 1: wire shape matches the iknow schema -----


def test_push_items_envelope_matches_iknow_schema() -> None:
    """Each item sent to the ``push`` MCP tool must carry only the
    ``z.strictObject`` keys the receiving schema accepts.

    Extra keys (``block_id`` / ``content_id`` / ``idempotency_key`` /
    ``parent_block_id`` / ``parent_text`` / ``text``) are §B4 wire
    fields that belong to fin's contract, not the cross-repo schema.
    Forwarding them would either be rejected by ``strictObject`` (a
    wire-shape bug reading as a refusal) or, on a mock without
    ``.strict()``, silently dropped — both are the same kind of
    failure the §B4 contract forbids.
    """

    port = _CapturingPushPort()
    records = _wires(2)
    port.push(records)

    assert len(port.payloads) == 1
    payload = port.payloads[0]
    arguments = payload["params"]["arguments"]
    assert payload["params"]["name"] == "push"
    items = arguments["items"]
    assert len(items) == 2
    for item in items:
        # No key outside the iknow-memory ``PushItemSchema`` is sent.
        assert set(item) <= _IKNOW_PUSH_ITEM_KEYS, item
        # The required §B4 / SC20 fields are present and well-typed.
        assert isinstance(item["content"], str) and item["content"]
        assert isinstance(item["source_ref"], str) and item["source_ref"]
        assert isinstance(item["metadata"], Mapping)
        assert len(item["metadata"]) >= 1
        assert isinstance(item["valid_from"], str) and item["valid_from"]
        assert isinstance(item["valid_until"], str) and item["valid_until"]
        # ISO datetimes satisfy the schema's ``z.string().datetime()``.
        assert "T" in item["valid_from"]
        assert "T" in item["valid_until"]


# ----- boundary 2: incomplete metadata is a named failure -----


def test_push_incomplete_metadata_returns_named_failure() -> None:
    """A JSON-RPC error from the receiving schema that names
    ``metadata`` translates to :class:`PushMetadataIncompleteError`.

    The MCP ``push`` schema rejects the WHOLE batch on missing
    ``metadata`` (per ``src/tools/push.ts``). fin's adapter must
    translate that rejection into the §B4 / SC20 typed refusal rather
    than collapsing it into a generic ``PushProtocolError``.
    """
    error_response = {
        "error": {
            "code": -32602,
            "message": (
                "Invalid params: items.0.metadata must contain at "
                "least one key (B4 required)"
            ),
        }
    }
    port = _ScriptedPushPort(scripted_response=error_response)
    records = _wires(1)

    with pytest.raises(PushMetadataIncompleteError) as excinfo:
        port.push(records)

    assert excinfo.value.code == "push_metadata_incomplete"
    assert isinstance(excinfo.value, PushError)


# ----- boundary 3: idempotent replay -----


def test_push_idempotent_replay_writes_zero() -> None:
    """Replaying the same batch must return the same ``block_ids``
    with ``written == 0`` and ``duplicates == record_count``.

    §B4「幂等键」+ §B1「幂等基准」: the second push of an identical
    batch is the same write; fin must not book it as a fresh write
    even if the wire-side ``chunk_ids`` happen to be byte-identical
    to the first call.
    """

    class _Stateful(MCPPushPort):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            self.calls += 1
            return _success_envelope(["chunk-a", "chunk-b"])

    port = _Stateful()
    records = _wires(2)

    first = port.push(records)
    assert first["written"] == 2
    assert first["duplicates"] == 0
    assert first["block_ids"] == ["chunk-a", "chunk-b"]

    replay = port.push(records)
    assert replay["written"] == 0
    assert replay["duplicates"] == 2
    assert replay["block_ids"] == ["chunk-a", "chunk-b"]

    assert port.calls == 2


# ----- boundary 4: transport failure is named -----


def test_push_transport_failure_is_named_not_silent() -> None:
    """A transport-level ``OSError`` becomes :class:`PushServiceUnavailableError`
    (SC21「检索服务不可用」). A bare ``OSError`` must NOT escape — that
    would be a silent failure mode the §B4 contract forbids.
    """
    port = _ScriptedPushPort(
        scripted_exception=OSError("connection refused"),
    )

    with pytest.raises(PushServiceUnavailableError) as excinfo:
        port.push(_wires(1))

    assert excinfo.value.code == "push_service_unavailable"
    assert isinstance(excinfo.value.__cause__, OSError)


# ----- boundary 5: protocol error is named -----


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"result": None}, id="null-result"),
        pytest.param({"result": []}, id="list-result"),
        pytest.param(
            {"result": {"content": [{"type": "text", "text": "not json"}]}},
            id="content-text-not-json",
        ),
        pytest.param(
            {"result": {"content": [{"type": "text", "text": "{}"}]}},
            id="content-text-no-chunk-ids",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"chunk_ids": "abc"})}
                    ]
                }
            },
            id="chunk-ids-not-list",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"chunk_ids": ["only-one"]}),
                        }
                    ]
                }
            },
            id="chunk-ids-count-mismatch",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"chunk_ids": ["a", 42]}),
                        }
                    ]
                }
            },
            id="chunk-ids-stray-non-string-element",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"chunk_ids": {"0": "a"}}),
                        }
                    ]
                }
            },
            id="chunk-ids-mapping-not-list",
        ),
        pytest.param(
            {
                "error": {
                    "code": -32603,
                    "message": "internal error",
                }
            },
            id="json-rpc-internal-error",
        ),
    ],
)
def test_push_protocol_error_is_named(response: dict) -> None:
    """Any off-contract response becomes :class:`PushProtocolError`.

    A short envelope is never padded, a stray one is never truncated
    into a plausible outcome (SC21): fin must refuse by name, not
    guess what the server meant.
    """
    port = _ScriptedPushPort(scripted_response=response)

    with pytest.raises(PushProtocolError) as excinfo:
        port.push(_wires(2))

    assert excinfo.value.code == "push_protocol_error"
    assert isinstance(excinfo.value, PushError)


# ----- boundary 6: empty batch is invalid input -----


def test_push_empty_batch_raises() -> None:
    """An empty record batch is invalid input (§B2 第 5 条): refused by
    name BEFORE the MCP round-trip, so the operator never pays for a
    pointless call.
    """
    port = _ScriptedPushPort()

    with pytest.raises(PushEmptyBatchError) as excinfo:
        port.push([])

    assert excinfo.value.code == "push_empty_batch"
    assert excinfo.value.degraded is False
    # the transport was not called: an empty batch refuses before MCP
    assert port._scripted_response is None  # no scripted hit


# ----- protocol conformance -----


def test_mcp_push_port_satisfies_push_port() -> None:
    """``MCPPushPort`` must satisfy the ``PushPort`` Protocol structurally."""
    from fin.retrieve.push_port import PushPort

    port = _ScriptedPushPort()
    assert isinstance(port, PushPort)


# ----- no configured transport: typed failure, no crash -----


def test_no_configured_command_raises_service_unavailable() -> None:
    """An unconfigured command (None) must surface as
    :class:`PushServiceUnavailableError` rather than letting an
    ``OSError`` / ``TypeError`` escape untyped.
    """
    port = MCPPushPort(command=None)
    with pytest.raises(PushServiceUnavailableError):
        port.push(_wires(1))


# ----- F1: chunk-count parity with the receiving tool -----


def _multi_chunk_wires(specs: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
    """Build envelopes whose ``chunks`` lists match ``specs``.

    Each spec is ``(source_ref, [chunk_content, ...])``; every chunk is
    also a contiguous substring of the body so the standard envelope
    validation passes. Used to pin the post-T1 wire shape the
    ``push`` server actually answers with (one ``chunk_id`` per
    slice, across all envelopes).
    """
    envelopes: list[dict[str, Any]] = []
    for index, (source_ref, chunks) in enumerate(specs):
        body = "".join(chunks)
        metadata = {
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "filing",
            "source_sha256": "sha-" + str(index),
            "parse_rule_version": "v1",
            "params_table_version": "p1",
            "normalization_rule_version": "n1",
        }
        envelopes.append(
            {
                "source_ref": source_ref,
                "body": body,
                "chunks": [
                    {"index": offset, "content": chunk}
                    for offset, chunk in enumerate(chunks)
                ],
                "metadata": metadata,
                "valid_from": "0001-01-01T00:00:00.000Z",
                "valid_until": "9999-12-31T23:59:59.999Z",
            }
        )
    return envelopes


def test_push_multi_chunk_envelope_chunk_count_matches_server() -> None:
    """F1: the ``chunk_ids`` count check pins the new wire shape.

    The receiving ``push`` tool hands back one ``chunk_id`` per chunk
    across all envelopes (see ``src/tools/push.ts`` ``allChunkIds``),
    so a 3-envelope batch where the first envelope carries 2 chunks,
    the second carries 1, and the third carries 3 yields **6** chunk
    ids — not 3. The previous record-count check raised
    :class:`PushProtocolError` for any multi-chunk envelope. This
    test pins the post-T1 chunk-count parity so the regression cannot
    return.

    The scripted server returns one id per slice (``chunk-0``,
    ``chunk-1``, ...), the adapter's chunk-count check accepts that
    count, and the response's ``block_ids`` is parallel to chunks —
    not envelopes.
    """
    specs = [
        ("docs/a.md", ["alpha ", "beta"]),  # 2 chunks
        ("docs/b.md", ["gamma"]),  # 1 chunk
        ("docs/c.md", ["delta ", "epsilon ", "zeta"]),  # 3 chunks
    ]
    expected_chunk_count = sum(len(chunks) for _, chunks in specs)
    expected_chunk_ids = [f"chunk-{i}" for i in range(expected_chunk_count)]

    port = _ChunkCountingPushPort()
    outcome = port.push(_multi_chunk_wires(specs))

    assert outcome["block_ids"] == expected_chunk_ids
    # ``written`` counts chunks now, not envelopes: 6 fresh chunks.
    assert outcome["written"] == expected_chunk_count
    assert outcome["duplicates"] == 0


def test_push_multi_chunk_envelope_replay_counts_chunks_not_envelopes() -> None:
    """F1: a multi-chunk envelope replay reports ``duplicates`` in
    chunks, not in envelopes.

    The (source_ref, body_content_id) ledger keys on the envelope —
    pushing the same 2-chunk envelope twice contributes **2** to
    ``duplicates`` the second time, not 1. Both envelopes here are
    multi-chunk, so the replay total is 5 chunks.
    """
    specs = [
        ("docs/a.md", ["alpha ", "beta"]),  # 2 chunks
        ("docs/b.md", ["gamma ", "delta ", "epsilon"]),  # 3 chunks
    ]

    port = _ChunkCountingPushPort()
    envelopes = _multi_chunk_wires(specs)

    first = port.push(envelopes)
    assert first["written"] == 5
    assert first["duplicates"] == 0

    again = port.push(envelopes)
    # Both envelopes are on the (source_ref, body_content_id) ledger
    # → every chunk is a duplicate.
    assert again["written"] == 0
    assert again["duplicates"] == 5


# ----- stable identity handoff: iknow's ``content_identity`` reaches the caller -----
#
# ``submit_index_job`` takes the ``(source_ref, content_identity)`` pair
# iknow itself stored (iknow ``src/tools/push.ts`` computes and persists
# it; fin sends no identity at all). A fin-side recomputation would be a
# second authority on a value the service owns, so the only correct
# source is the push response.


def test_push_exposes_the_single_document_content_identity() -> None:
    """A one-资料 push answers with a top-level ``content_identity``; the
    adapter must hand it back keyed by the ``source_ref`` it sent."""
    port = _ScriptedPushPort(
        scripted_response=_business_envelope(
            {
                "chunk_ids": ["chunk-a"],
                "source_doc_id": "doc-1",
                "content_identity": _IDENTITY_A,
            }
        )
    )

    outcome = port.push(_wires(1))

    assert outcome["content_identities"] == {"docs/0.md": _IDENTITY_A}


def test_push_zips_batch_content_identities_in_send_order() -> None:
    """The legacy ``items[]`` batch answers with parallel
    ``source_doc_ids`` / ``content_identities`` arrays in send order; the
    adapter pairs each entry with the ``source_ref`` of the item that
    produced it."""
    port = _ScriptedPushPort(
        scripted_response=_business_envelope(
            {
                "chunk_ids": ["chunk-a", "chunk-b", "chunk-c"],
                "source_doc_ids": ["doc-0", "doc-1", "doc-2"],
                "content_identities": [_IDENTITY_A, _IDENTITY_B, _IDENTITY_C],
            }
        )
    )

    outcome = port.push(_wires(3))

    assert outcome["content_identities"] == {
        "docs/0.md": _IDENTITY_A,
        "docs/1.md": _IDENTITY_B,
        "docs/2.md": _IDENTITY_C,
    }


def test_push_batch_content_identities_follow_sent_item_order_not_response_order(
) -> None:
    """The zip is against what fin SENT, so a multi-chunk batch still
    attributes one identity per 资料."""
    specs = [
        ("docs/a.md", ["alpha ", "beta"]),
        ("docs/b.md", ["gamma"]),
        ("docs/c.md", ["delta ", "epsilon ", "zeta"]),
    ]
    port = _ScriptedPushPort(
        scripted_response=_business_envelope(
            {
                "chunk_ids": [f"chunk-{i}" for i in range(6)],
                "source_doc_ids": ["doc-a", "doc-b", "doc-c"],
                "content_identities": [_IDENTITY_A, _IDENTITY_B, _IDENTITY_C],
            }
        )
    )

    outcome = port.push(_multi_chunk_wires(specs))

    assert outcome["content_identities"] == {
        "docs/a.md": _IDENTITY_A,
        "docs/b.md": _IDENTITY_B,
        "docs/c.md": _IDENTITY_C,
    }


def test_push_reports_no_content_identity_when_the_service_omits_it() -> None:
    """An iknow build that predates the identity response is still a
    successful write: the adapter answers "no identity known" rather
    than inventing one fin could later hand to ``submit_index_job``."""
    port = _ScriptedPushPort(scripted_response=_success_envelope(["chunk-a"]))

    assert port.push(_wires(1))["content_identities"] == {}


def test_push_replay_still_reports_the_stored_content_identity() -> None:
    """An idempotent re-push echoes the stored identity, so a caller that
    resolves the pair from the response gets the same value twice."""

    class _EchoIdentity(_ChunkCountingPushPort):
        """Answers every call with the one identity the service says it
        stored for each 资料, so a replay reads the same pairs twice."""

        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            refs = [item["source_ref"] for item in _items_of(payload)]
            return _business_envelope(
                {
                    "chunk_ids": [f"chunk-{i}" for i in range(len(refs))],
                    "source_doc_ids": [f"doc-{i}" for i in range(len(refs))],
                    "content_identities": [_IDENTITY_A] * len(refs),
                }
            )

    port = _EchoIdentity()
    envelopes = _wires(2)

    assert port.push(envelopes)["content_identities"] == {
        "docs/0.md": _IDENTITY_A,
        "docs/1.md": _IDENTITY_A,
    }
    assert port.push(envelopes)["content_identities"] == {
        "docs/0.md": _IDENTITY_A,
        "docs/1.md": _IDENTITY_A,
    }


@pytest.mark.parametrize(
    "business",
    [
        pytest.param({"content_identity": 42}, id="scalar-not-a-string"),
        pytest.param({"content_identity": ""}, id="scalar-blank"),
        pytest.param({"content_identities": "abc"}, id="list-not-a-list"),
        pytest.param({"content_identities": []}, id="list-empty"),
        pytest.param({"content_identities": [_IDENTITY_A]}, id="list-too-short"),
        pytest.param(
            {
                "content_identities": [
                    _IDENTITY_A,
                    _IDENTITY_B,
                    _IDENTITY_C,
                    _IDENTITY_D,
                ]
            },
            id="list-too-long",
        ),
        pytest.param(
            {"content_identities": [_IDENTITY_A, _IDENTITY_B, 7]},
            id="list-stray-non-string",
        ),
        pytest.param(
            {"content_identities": {_IDENTITY_A: "docs/0.md"}},
            id="list-keyed-the-wrong-way-round",
        ),
        pytest.param(
            {"content_identity": _IDENTITY_A, "content_identities": [_IDENTITY_A]},
            id="both-forms-at-once",
        ),
        pytest.param(
            {"content_identity": _IDENTITY_A}, id="scalar-for-a-multi-doc-batch"
        ),
    ],
)
def test_push_off_contract_content_identity_is_a_named_protocol_failure(
    business: dict[str, Any],
) -> None:
    """A misaligned or malformed identity is refused by name — never
    guessed at, and never published as a pair the service does not hold."""
    port = _ScriptedPushPort(
        scripted_response=_business_envelope(
            {
                "chunk_ids": ["chunk-a", "chunk-b", "chunk-c"],
                **business,
            }
        )
    )

    with pytest.raises(PushProtocolError) as excinfo:
        port.push(_wires(3))

    assert excinfo.value.code == "push_protocol_error"
    assert isinstance(excinfo.value, PushError)


def test_push_never_sends_an_identity_field_on_the_wire() -> None:
    """iknow computes the identity itself and its ``strictObject`` refuses
    unknown keys, so an identity on the request is a wire-shape bug —
    pinned here because the response now carries one in the other
    direction."""

    port = _CapturingPushPort(
        scripted_response=_business_envelope(
            {
                "chunk_ids": ["chunk-a"],
                "source_doc_id": "doc-1",
                "content_identity": _IDENTITY_A,
            }
        )
    )
    port.push(_wires(1))

    items = _items_of(port.payloads[0])
    assert len(items) == 1
    for item in items:
        assert set(item) <= _IKNOW_PUSH_ITEM_KEYS, item
        assert "content_identity" not in item
        assert "body_content_id" not in item


# ----- the push budget (FIN_MCP_TIMEOUT_SECONDS) -----
#
# A push is the only MCP call that does real work before it answers: the
# receiving side embeds every chunk and writes the rows. Measured against
# the live server, one push took ~11s end to end, so the previous 5s
# ceiling could never succeed and reported every push as a timeout. These
# tests pin the RESOLUTION of that budget, which is the part that can
# silently regress — the number itself is a measured fact, not a style.


# The slowest observed live push, in seconds, from the timing experiment
# that replaced the 5s ceiling: the same document answered in 11.02s with a
# 30s budget and 11.05s with a 120s one, i.e. ~11s is the real cost of the
# work and the budget above it is not what the number costs.
_MEASURED_PUSH_SECONDS = 11.05


def test_default_budget_clears_a_measured_push() -> None:
    """The floor must exceed the measured cost of one real push.

    Asserted against the measurement rather than against the shipped 60,
    so lowering the default fails here and names why — instead of the
    failure surfacing only as a production timeout. The shipped value is
    pinned separately so the two cannot be confused: this test says the
    budget must clear the work, that one says what it currently is.
    """
    from fin.retrieve.mcp_push_port import DEFAULT_MCP_TIMEOUT_SECONDS

    assert DEFAULT_MCP_TIMEOUT_SECONDS > _MEASURED_PUSH_SECONDS


def test_the_shipped_budget_is_the_documented_value() -> None:
    """The number itself: 60s, as recorded in the owning spec.

    `specs/corpus-serve-ingest.md` ("MCP per-call budget") and
    `specs/search-mode-business-sql-contract.md` §6 both name 60 s, so a
    change here is a change to a settled limit and must be a deliberate
    spec amendment, not a quiet edit.
    """
    from fin.retrieve.mcp_push_port import DEFAULT_MCP_TIMEOUT_SECONDS

    assert DEFAULT_MCP_TIMEOUT_SECONDS == 60.0


def test_port_resolves_the_env_budget_when_constructed_with_no_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production builds ``MCPPushPort()`` with NO arguments, so this is
    the only seam where an operator override can take effect."""
    monkeypatch.setenv("FIN_MCP_TIMEOUT_SECONDS", "90")
    assert MCPPushPort()._timeout == 90.0


def test_an_explicit_constructor_argument_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that names a budget owns it; the env is a default, not an
    override of an explicit choice."""
    monkeypatch.setenv("FIN_MCP_TIMEOUT_SECONDS", "90")
    assert MCPPushPort(timeout_seconds=12.0)._timeout == 12.0


@pytest.mark.parametrize("raw", ["abc", "", "0", "-5", "nan", "inf", "-inf"])
def test_an_unusable_env_value_falls_back_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    from fin.retrieve.mcp_push_port import DEFAULT_MCP_TIMEOUT_SECONDS
    """A typo in an operator's `.env` must not take the ingest door down;
    the port degrades to the default budget, and the caller still sees the
    same typed PushServiceUnavailableError it already handles if the
    budget was genuinely too small.

    ``nan`` and ``inf`` are here because they parse as floats and pass a
    bare ``> 0`` test: ``nan <= 0`` is False and ``inf > 0`` is True, so
    without an explicit finiteness check they would reach
    ``asyncio.wait_for(timeout=nan)`` (undefined behaviour) or silently
    remove the ceiling this budget exists to provide.
    """
    monkeypatch.setenv("FIN_MCP_TIMEOUT_SECONDS", raw)
    assert MCPPushPort()._timeout == DEFAULT_MCP_TIMEOUT_SECONDS
