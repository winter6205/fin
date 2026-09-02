"""Boundary tests for ``MCPInvalidationPort`` (Plan D0 I-T5, SC24, §B4).

The MCP invalidation port closes the validity interval of a
``source_ref``'s chunks by issuing a single ``invalidation`` tool
call to the iknow-memory MCP server. Wire field names must be
verbatim the **z.strictObject** schema in ``src/tools/invalidation.ts``
(``source_ref``, ``invalidated_at``, ``triggered_by``); the fin side
forwards one ``invalidate`` invocation per ``source_ref`` because
the receiving schema is single-target by construction.

The default transport (``_call_mcp``) launches a subprocess. Tests
override ``_call_mcp`` to inject a scripted response or scripted
exception — the same hermetic seam used by
``tests/retrieve/test_mcp_adapter.py``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import pytest

from fin.corpus.invalidation import (
    REASON_UNIT_VERSION_CHANGED,
    InvalidationTarget,
)
from fin.retrieve.invalidation_port import (
    InvalidationEmptyBatchError,
    InvalidationError,
    InvalidationPort,
    InvalidationProtocolError,
    InvalidationServiceUnavailableError,
    InvalidationUnknownTargetError,
)
from fin.retrieve.invalidation_port.errors import InvalidationOutcome
from fin.retrieve.mcp_invalidation_port import MCPInvalidationPort

# Authoritative schema keys the iknow-memory ``invalidation`` tool
# accepts (``z.strictObject(InvalidationInputSchema)`` in
# ``src/tools/invalidation.ts``). The fin-side adapter must NOT smuggle
# in ``content_id`` / ``block_id`` / ``reason`` / ``idempotency_key``
# — ``strictObject`` would refuse them on the wire.
_IKNOW_INVALIDATION_INPUT_KEYS: frozenset[str] = frozenset(
    {"source_ref", "invalidated_at", "triggered_by"}
)


def _target(
    *,
    source_ref: str = "docs/x.md",
    content_id: str = "c0",
    block_id: str = "b0",
    document_id: str = "DOC-1",
    version: str = "v-aaaaaaaaaaaaaaaa",
    unit: int = 1,
    reason: str = REASON_UNIT_VERSION_CHANGED,
) -> InvalidationTarget:
    return InvalidationTarget(
        source_ref=source_ref,
        content_id=content_id,
        block_id=block_id,
        document_id=document_id,
        version=version,
        unit=unit,
        reason=reason,
    )


def _targets(n: int = 1) -> tuple[InvalidationTarget, ...]:
    return tuple(
        _target(
            source_ref=f"docs/{i}.md",
            content_id=f"c{i}",
            block_id=f"b{i}",
            document_id=f"DOC-{i}",
        )
        for i in range(n)
    )


# ----- scripted transport override -----


class _ScriptedInvalidationPort(MCPInvalidationPort):
    """An ``MCPInvalidationPort`` whose ``_call_mcp`` returns a
    scripted response or raises a scripted exception.

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
        self._scripted_response = scripted_response
        self._scripted_exception = scripted_exception

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        if self._scripted_exception is not None:
            raise self._scripted_exception
        if self._scripted_response is not None:
            return self._scripted_response
        raise RuntimeError("no scripted response configured")


def _success_envelope(
    chunk_ids: Sequence[str],
    *,
    invalidations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The canonical MCP ``tools/call`` envelope wrapping
    ``{"invalidated_chunk_ids": [...], "invalidations": [...]}``
    inside ``result.content[0].text`` (per
    ``src/tools/invalidation.ts``)."""
    body: dict[str, Any] = {"invalidated_chunk_ids": list(chunk_ids)}
    if invalidations is not None:
        body["invalidations"] = list(invalidations)
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(body)}]
        }
    }


# ----- boundary 1: wire shape matches the iknow schema -----


def test_invalidate_envelope_matches_iknow_schema() -> None:
    """The ``invalidation`` tool receives one ``invalidate`` call per
    ``source_ref``, with the **z.strictObject** envelope
    (``source_ref``, ``invalidated_at``, ``triggered_by``).

    Extra keys (``content_id`` / ``block_id`` / ``reason``) are
    §B4 wire fields that belong to fin's contract, not the
    cross-repo schema. ``strictObject`` would refuse them on the
    wire; even on a mock without ``.strict()`` they would be
    silently dropped — both are the same kind of failure the §B4
    contract forbids.
    """

    class _Capturing(MCPInvalidationPort):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.payloads: list[dict] = []

        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            self.payloads.append(payload)
            return _success_envelope(["chunk-a", "chunk-b"])

    port = _Capturing()
    targets = _targets(2)
    port.invalidate(targets)

    # Two distinct source_refs → two MCP calls.
    assert len(port.payloads) == 2
    for payload, target in zip(port.payloads, targets):
        arguments = payload["params"]["arguments"]
        assert payload["params"]["name"] == "invalidation"
        # No key outside the iknow-memory schema is sent.
        assert set(arguments) <= _IKNOW_INVALIDATION_INPUT_KEYS, arguments
        assert arguments["source_ref"] == target.source_ref
        assert arguments["triggered_by"] == "fin:corpus"
        # ``invalidated_at`` is an ISO datetime when present.
        assert "invalidated_at" in arguments
        assert isinstance(arguments["invalidated_at"], str)
        assert "T" in arguments["invalidated_at"]


# ----- boundary 2: unknown source_ref is a named failure -----


def test_invalidate_unknown_source_ref_is_named_failure() -> None:
    """A JSON-RPC error from the receiving service that names the
    unknown source_ref translates to :class:`InvalidationUnknownTargetError`.

    The receiving service answers「no pushed block with source_ref…」
    for a target it never accepted; fin's adapter must surface the
    §B4 typed refusal rather than collapsing it into the generic
    protocol failure or treating the unknown target as a settled
    no-op (a silently skipped target is exactly the silence §B4
    forbids).
    """
    error_response = {
        "error": {
            "code": -32602,
            "message": (
                "no pushed block with source_ref 'docs/unknown.md'"
            ),
        }
    }
    port = _ScriptedInvalidationPort(scripted_response=error_response)
    targets = _targets(1)

    with pytest.raises(InvalidationUnknownTargetError) as excinfo:
        port.invalidate(targets)

    assert excinfo.value.code == "invalidation_unknown_target"
    assert excinfo.value.degraded is False
    assert isinstance(excinfo.value, InvalidationError)


# ----- boundary 3: idempotent replay -----


def test_invalidate_replay_is_idempotent() -> None:
    """Issuing the same ``invalidate`` twice for a closed target must
    return ``already_closed == 1`` and ``closed == 0`` on the second
    call.

    §B4「幂等」+ §B1「幂等基准」: the second request is the same
    request, so the service answers the same closing instant and
    counts the repeat under ``already_closed``.
    """

    class _Stateful(MCPInvalidationPort):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.first_seen_at: int | None = None

        async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
            self.calls += 1
            if self.first_seen_at is None:
                self.first_seen_at = 1_000_000
            return _success_envelope(
                ["chunk-a"],
                invalidations=[
                    {
                        "chunk_id": "chunk-a",
                        "source_ref": payload["params"]["arguments"]["source_ref"],
                        "invalidated_at": self.first_seen_at,
                        "triggered_by": "fin:corpus",
                    }
                ],
            )

    port = _Stateful()
    targets = _targets(1)

    first = port.invalidate(targets)
    assert isinstance(first, InvalidationOutcome)
    assert first.closed == 1
    assert first.already_closed == 0
    assert first.block_ids == ("chunk-a",)

    replay = port.invalidate(targets)
    assert replay.closed == 0
    assert replay.already_closed == 1
    assert replay.block_ids == ("chunk-a",)
    # The closing instant survives the replay — same answer twice.
    assert replay.closed_at == first.closed_at
    assert port.calls == 2


# ----- boundary 4: empty target list is invalid input -----


def test_invalidate_empty_raises() -> None:
    """An empty target set is invalid input (§B2 第 5 条): refused by
    name BEFORE the MCP round-trip, so the operator never pays for
    a pointless call.
    """
    port = _ScriptedInvalidationPort()

    with pytest.raises(InvalidationEmptyBatchError) as excinfo:
        port.invalidate(())

    assert excinfo.value.code == "invalidation_empty_batch"
    assert excinfo.value.degraded is False


# ----- boundary 5: transport failure is named -----


def test_invalidate_transport_failure_is_named_not_silent() -> None:
    """A transport-level ``OSError`` becomes :class:`InvalidationServiceUnavailableError`
    (SC21「检索服务不可用」). A bare ``OSError`` must NOT escape — that
    would be a silent failure mode the §B4 contract forbids.
    """
    port = _ScriptedInvalidationPort(
        scripted_exception=OSError("connection refused"),
    )

    with pytest.raises(InvalidationServiceUnavailableError) as excinfo:
        port.invalidate(_targets(1))

    assert excinfo.value.code == "invalidation_service_unavailable"
    assert isinstance(excinfo.value.__cause__, OSError)


# ----- boundary 6: protocol error is named -----


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
            id="content-text-no-invalidated-chunk-ids",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"invalidated_chunk_ids": "abc"}),
                        }
                    ]
                }
            },
            id="invalidated-chunk-ids-not-list",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"invalidated_chunk_ids": []}),
                        }
                    ]
                }
            },
            id="invalidated-chunk-ids-count-mismatch",
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
def test_invalidate_protocol_error_is_named(response: dict) -> None:
    """Any off-contract response becomes :class:`InvalidationProtocolError`.

    A short envelope is never padded, a stray one is never
    truncated into a plausible outcome (SC21): fin must refuse by
    name, not guess what the server meant.
    """
    port = _ScriptedInvalidationPort(scripted_response=response)

    with pytest.raises(InvalidationProtocolError) as excinfo:
        port.invalidate(_targets(1))

    assert excinfo.value.code == "invalidation_protocol_error"
    assert isinstance(excinfo.value, InvalidationError)


# ----- protocol conformance -----


def test_mcp_invalidation_port_satisfies_invalidation_port() -> None:
    """``MCPInvalidationPort`` must satisfy the ``InvalidationPort``
    Protocol structurally."""
    port = _ScriptedInvalidationPort()
    assert isinstance(port, InvalidationPort)


# ----- no configured transport: typed failure, no crash -----


def test_no_configured_command_raises_service_unavailable() -> None:
    """An unconfigured command (None) must surface as
    :class:`InvalidationServiceUnavailableError` rather than letting
    an ``OSError`` / ``TypeError`` escape untyped.
    """
    port = MCPInvalidationPort(command=None)
    with pytest.raises(InvalidationServiceUnavailableError):
        port.invalidate(_targets(1))
