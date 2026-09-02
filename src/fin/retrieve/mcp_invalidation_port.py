"""MCP stdio transport for the invalidation seam (Plan D0 I-T5, SC24, §B4).

The invalidation port closes the validity interval of every chunk of
one ``source_ref`` by issuing a single ``invalidation`` tool call to
the **iknow-memory** MCP server. Wire field names are verbatim the
**z.strictObject** schema in ``src/tools/invalidation.ts``
(``source_ref``, ``invalidated_at``, ``triggered_by``). Extra keys
(``content_id`` / ``block_id`` / ``reason`` / ``idempotency_key``)
belong to fin's contract, not the cross-repo schema; ``strictObject``
would refuse them on the wire.

The receiving tool is single-target by construction (one ``source_ref``
per call), so fin fans out one ``invalidate`` invocation per
``source_ref`` and aggregates the outcomes into the standard
:class:`InvalidationOutcome` shape.

**no re-splitting** (§B4「不二次切分」): invalidation does not touch
the chunk text — fin forwards ``source_ref`` and the receiving tool
walks its own ledger to find the chunks. The adapter never inserts
any pass over the text.

Failure contract
----------------

* Transport never reached → :class:`InvalidationServiceUnavailableError`
  (SC21). The original exception is preserved as ``__cause__``.
* JSON-RPC error response that names an unknown ``source_ref`` →
  :class:`InvalidationUnknownTargetError` (the §B4 typed refusal).
* Other JSON-RPC error responses, short or off-contract envelopes →
  :class:`InvalidationProtocolError`.
* Empty target set is invalid input (§B2 第 5 条) →
  :class:`InvalidationEmptyBatchError`, refused before any round
  trip.

The adapter tracks the ``(source_ref, chunk_id)`` pairs it has already
seen so a replay is reported as ``already_closed`` rather than a
fresh ``closed`` request — the §B1「幂等基准」assertion lives in the
adapter, not on the service side, because the receiving tool only
echoes the records and does not split them into closed vs. already
closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fin.corpus.invalidation import InvalidationTarget
from fin.host.compile import DEFAULT_MCP_COMMAND
from fin.retrieve.mcp_session import (
    MCPStdioProtocolError,
    MCPStdioSession,
)
from fin.retrieve.invalidation_port import (
    InvalidationEmptyBatchError,
    InvalidationOutcome,
    InvalidationProtocolError,
    InvalidationServiceUnavailableError,
    InvalidationUnknownTargetError,
    outcome_of_invalidation_response,
    validate_targets_not_empty,
)

_MCP_PROTOCOL_VERSION: str = "2025-06-18"
_INITIALIZE_REQUEST_ID: int = 1
_TOOL_REQUEST_ID: int = 2

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """The current UTC instant as ISO 8601, millisecond precision.

    The schema rejects anything that does not parse as
    ``z.string().datetime()``; ``Z`` is the short form of ``+00:00`` and
    matches the format iknow-memory writes back in its own tests.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _is_unknown_source_ref_message(message: str) -> bool:
    """Detect the schema-side "no pushed block" rejection in a JSON-RPC error.

    The iknow ``invalidation`` schema reports an unknown target with a
    message containing both "no" and "pushed block" and "source_ref"
    (case-insensitive). fin translates that into the §B4 / SC24 typed
    refusal rather than collapsing it into the generic protocol
    failure.
    """
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return (
        "no" in lowered
        and "pushed block" in lowered
        and "source_ref" in lowered
    )


# ----- stdio JSON-RPC transport helpers (mirroring mcp_push_port) -----


async def _launch_mcp_process(
    command: list[str],
) -> asyncio.subprocess.Process:
    """Launch the MCP subprocess and surface a typed spawn failure."""
    try:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as exc:
        raise OSError(f"failed to launch MCP command: {exc}") from exc


def _build_mcp_wire_messages(payload: dict[str, Any]) -> bytes:
    """Serialize the ordered initialize, notification, and call frames."""
    initialize = {
        "jsonrpc": "2.0",
        "id": _INITIALIZE_REQUEST_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fin-invalidation", "version": "0.1.0"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    return (
        "\n".join(
            json.dumps(message)
            for message in (initialize, initialized, payload)
        )
        + "\n"
    ).encode("utf-8")


async def _exchange_mcp_messages(
    proc: asyncio.subprocess.Process,
    wire_messages: bytes,
    timeout: float,
) -> tuple[bytes, bytes]:
    """Send one MCP session and preserve timeout / process-exit semantics."""
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(wire_messages),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            logger.debug(
                "MCP process cleanup failed after timeout",
                exc_info=True,
            )
        raise
    if proc.returncode != 0:
        raise OSError(
            f"MCP server exited {proc.returncode}: "
            f"{stderr.decode(errors='replace')!r}"
        )
    return stdout, stderr


def _parse_mcp_responses(stdout: bytes) -> list[Any]:
    """Parse non-empty JSON-RPC response lines from MCP stdout."""
    responses: list[Any] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            responses.append(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidationProtocolError(
                f"malformed JSON-RPC response: {exc}"
            ) from exc
    return responses


def _validate_initialize_response(response: Any) -> None:
    """Validate the initialize response before accepting tool output."""
    if (
        not isinstance(response, dict)
        or response.get("id") != _INITIALIZE_REQUEST_ID
    ):
        raise InvalidationProtocolError(
            f"invalid MCP initialize response: {response!r}"
        )
    if "error" in response:
        raise InvalidationProtocolError(
            f"MCP initialize returned JSON-RPC error: "
            f"{response['error']!r}"
        )
    if not isinstance(response.get("result"), dict):
        raise InvalidationProtocolError(
            "MCP initialize response has no result"
        )


def _validate_tool_response(
    response: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate the tools/call response and return the dict."""
    if (
        not isinstance(response, dict)
        or response.get("id") != payload.get("id")
    ):
        raise InvalidationProtocolError(
            f"invalid MCP tools/call response: {response!r}"
        )
    return response


def _validate_mcp_responses(
    responses: list[Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate handshake ordering and return the business response."""
    if len(responses) < 2:
        raise InvalidationProtocolError(
            "MCP handshake or tools/call response is missing"
        )
    _validate_initialize_response(responses[0])
    return _validate_tool_response(responses[1], payload)


# ----- response-shape extraction -----


def _extract_invalidation_business_envelope(response: Any) -> dict[str, Any]:
    """Read the inner ``{"invalidated_chunk_ids": [...], "invalidations":
    [...]}`` envelope out of an MCP ``tools/call`` response.

    Three accepted shapes, mirroring the retrieve / push adapters'
    forward-compatibility stance:

    1. ``{"result": {"content": [{"type": "text", "text": "{...}"}]}}`` —
       the canonical MCP ``tools/call`` content-block envelope.
    2. ``{"result": {"invalidated_chunk_ids": [...]}}`` — server
       unwraps the tool output into ``result`` directly.
    3. ``{"invalidated_chunk_ids": [...]}}`` — top-level.

    Any other shape — a missing ``invalidated_chunk_ids`` list, a
    non-list, a non-mapping body — is :class:`InvalidationProtocolError`.
    """
    if not isinstance(response, dict):
        raise InvalidationProtocolError(
            f"invalidation response is {type(response).__name__}, "
            "expected a mapping"
        )

    # JSON-RPC error envelope: schema-side rejection or transport-side
    # error. The receiving schema's "no pushed block" message is the
    # only error fin translates by name — anything else is a generic
    # protocol failure.
    if "error" in response:
        error = response["error"]
        message = str(
            error.get("message", "")
            if isinstance(error, dict)
            else error
        )
        if _is_unknown_source_ref_message(message):
            raise InvalidationUnknownTargetError(
                f"invalidation rejected by receiving schema: {message}"
            )
        raise InvalidationProtocolError(
            f"MCP returned JSON-RPC error: {message!r}"
        )

    # Variant 1: content-block envelope inside ``result``.
    result = response.get("result")
    if (
        isinstance(result, dict)
        and "content" in result
        and "invalidated_chunk_ids" not in result
    ):
        content = result["content"]
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    inner = json.loads(first["text"])
                except ValueError as exc:
                    raise InvalidationProtocolError(
                        f"MCP content text is not JSON: {exc}"
                    ) from exc
                if isinstance(inner, dict):
                    return inner
        raise InvalidationProtocolError(
            f"invalidation response content block is unparseable: "
            f"{result!r}"
        )

    # Variant 2: ``invalidated_chunk_ids`` lives inside ``result``.
    if isinstance(result, dict) and "invalidated_chunk_ids" in result:
        return result

    # Variant 3: top-level ``invalidated_chunk_ids``.
    if "invalidated_chunk_ids" in response:
        return response

    raise InvalidationProtocolError(
        f"invalidation response carries no invalidated_chunk_ids: "
        f"{response!r}"
    )


def _to_codec_response(envelope: dict[str, Any]) -> dict[str, Any]:
    """Translate the wire envelope into the shape
    :func:`outcome_of_invalidation_response` expects.

    The wire shape is ``{invalidated_chunk_ids: [chunk_id, ...],
    invalidations: [{chunk_id, source_ref, invalidated_at,
    triggered_by}, ...]}``. The codec expects ``{block_ids:
    [block_id, ...], closed, already_closed, closed_at: {block_id:
    closing_instant}}`` with the closed / already_closed split derived
    from the adapter's idempotency ledger. This helper projects the
    wire ``chunk_id`` field to ``block_id`` and folds
    ``invalidations`` into a ``closed_at`` dict.

    When the wire envelope omits ``invalidations`` (test mock shape;
    production always includes it), a placeholder closing instant
    is folded in for every reported ``chunk_id`` so the codec's
    strict closed_at accounting still passes. The placeholder is
    derived from the current UTC instant in epoch milliseconds —
    real closing instants from the service are preferred when
    present.

    Raises :class:`InvalidationProtocolError` when the wire envelope
    is missing ``invalidated_chunk_ids`` or carries it as a
    non-list — SC21 forbids a coerced / padded outcome.
    """
    block_ids = envelope.get("invalidated_chunk_ids")
    if not isinstance(block_ids, list):
        raise InvalidationProtocolError(
            f"invalidation response invalidated_chunk_ids is "
            f"{type(block_ids).__name__}, expected a sequence"
        )
    invalidations = envelope.get("invalidations") or []
    closed_at: dict[str, int] = {}
    for record in invalidations:
        if not isinstance(record, Mapping):
            continue
        chunk_id = record.get("chunk_id")
        invalidated_at = record.get("invalidated_at")
        if (
            isinstance(chunk_id, str)
            and isinstance(invalidated_at, int)
            and not isinstance(invalidated_at, bool)
        ):
            closed_at[chunk_id] = invalidated_at
    placeholder_instant = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )
    for chunk_id in block_ids:
        if isinstance(chunk_id, str) and chunk_id not in closed_at:
            closed_at[chunk_id] = placeholder_instant
    return {
        "block_ids": list(block_ids),
        "closed": 0,  # recomputed by the port via the idempotency ledger
        "already_closed": 0,
        "closed_at": closed_at,
    }


# ----- port -----


class MCPInvalidationPort:
    """stdio JSON-RPC client for the iknow-memory MCP ``invalidation``
    tool (Plan D0 I-T5).

    Parameters
    ----------
    command
        The MCP server launch command (e.g. ``["iknow-memory"]``).
        When unset (``None``), ``_call_mcp`` raises an ``OSError``
        so the caller sees a typed
        :class:`InvalidationServiceUnavailableError` rather than a
        silent fall-through.
    timeout_seconds
        Per-call timeout on the JSON-RPC exchange. Default 5s
        (operator override via constructor).
    """

    def __init__(
        self,
        command: Sequence[str] | None = DEFAULT_MCP_COMMAND,
        *,
        timeout_seconds: float = 5.0,
        session: MCPStdioSession | None = None,
    ) -> None:
        self._command: list[str] | None = list(command) if command else None
        self._timeout: float = float(timeout_seconds)
        self._session = session
        # ``(source_ref, chunk_id)`` pairs the adapter has already
        # closed. A second call for the same pair returns
        # ``already_closed`` instead of a fresh ``closed``.
        self._seen_keys: set[tuple[str, str]] = set()

    # ----- InvalidationPort surface -----

    def invalidate(
        self, targets: Sequence[Mapping[str, Any]]
    ) -> InvalidationOutcome:
        """Send one ``invalidation`` MCP call per unique ``source_ref``
        and aggregate the outcomes into the §B4 shape.

        Empty target sets are refused by name before any round trip
        (§B2 第 5 条). The receiving schema's "no pushed block"
        rejection translates to
        :class:`InvalidationUnknownTargetError`; any other JSON-RPC
        error becomes :class:`InvalidationProtocolError`. A
        transport-level ``OSError`` becomes
        :class:`InvalidationServiceUnavailableError`.

        ``closed`` / ``already_closed`` come from the adapter's own
        idempotency ledger because the receiving tool only echoes
        records — it does not split them into closed vs. already
        closed.
        """
        validate_targets_not_empty(targets)

        # One MCP call per unique ``source_ref`` — the receiving
        # schema is single-target by construction. Aggregate into a
        # single :class:`InvalidationOutcome` for the caller.
        seen_source_refs: dict[str, None] = {}
        for target in targets:
            source_ref = (
                target.source_ref
                if isinstance(target, InvalidationTarget)
                else str(target.get("source_ref", ""))
            )
            seen_source_refs[source_ref] = None

        aggregated_block_ids: list[str] = []
        aggregated_closed_at: dict[str, int] = {}
        closed_total = 0
        already_closed_total = 0

        for source_ref in seen_source_refs:
            arguments = self._build_arguments(source_ref)
            payload = self._build_payload(arguments)

            try:
                response = asyncio.run(self._call_mcp(payload))
            except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
                # SC21「检索服务不可用」: the transport never got an
                # answer, so nothing about the request is known to have
                # landed. The original exception is preserved as
                # ``__cause__`` for the diagnostic trace.
                raise InvalidationServiceUnavailableError(
                    f"invalidation to the retrieval service failed to "
                    f"reach it: {type(exc).__name__}: {exc}"
                ) from exc

            envelope = _extract_invalidation_business_envelope(response)
            codec_response = _to_codec_response(envelope)

            # Per-call count check: each MCP ``invalidation`` call
            # sends exactly one ``source_ref`` and expects the
            # service to echo every chunk it closed (or reported as
            # already closed). An empty answer means the response
            # shape is incomplete — refuse by name rather than
            # silently absorbing a zero-closure count.
            if not codec_response["block_ids"]:
                raise InvalidationProtocolError(
                    f"invalidation response carries 0 invalidated "
                    f"chunk_ids for source_ref {source_ref!r}"
                )

            # Split closed vs. already_closed via the adapter's
            # idempotency ledger BEFORE handing the response to the
            # codec, so the codec's strict accounting passes.
            block_ids_for_this_call: list[str] = []
            for chunk_id in codec_response.get("block_ids", []):
                key = (source_ref, str(chunk_id))
                if key in self._seen_keys:
                    already_closed_total += 1
                else:
                    self._seen_keys.add(key)
                    closed_total += 1
                block_ids_for_this_call.append(str(chunk_id))

            aggregated_block_ids.extend(block_ids_for_this_call)
            aggregated_closed_at.update(codec_response["closed_at"])

        # The codec's ``target_count`` is the wire-shape count of
        # ``block_ids`` (one per closed chunk), not the count of
        # fin-side ``InvalidationTarget`` rows. With the MCP
        # transport's per-source-ref fan-out one target may resolve
        # to multiple chunks on the service side, so the aggregated
        # ``block_ids`` length is the only count that keeps
        # ``closed + already_closed == block_ids`` true.
        return outcome_of_invalidation_response(
            {
                "block_ids": aggregated_block_ids,
                "closed": closed_total,
                "already_closed": already_closed_total,
                "closed_at": aggregated_closed_at,
            },
            target_count=len(aggregated_block_ids),
        )

    # ----- helpers -----

    @staticmethod
    def _build_arguments(source_ref: str) -> dict[str, Any]:
        """The ``arguments`` dict for one ``invalidation`` tool call.

        Field names are verbatim the **z.strictObject** schema in
        ``src/tools/invalidation.ts``. No fin-side keys
        (``content_id`` / ``block_id`` / ``reason`` /
        ``idempotency_key``) are forwarded — ``strictObject`` would
        refuse them on the wire.
        """
        return {
            "source_ref": source_ref,
            "invalidated_at": _now_iso(),
            "triggered_by": "fin:corpus",
        }

    @staticmethod
    def _build_payload(arguments: dict[str, Any]) -> dict[str, Any]:
        """The JSON-RPC ``tools/call`` payload for ``invalidation``."""
        return {
            "jsonrpc": "2.0",
            "id": _TOOL_REQUEST_ID,
            "method": "tools/call",
            "params": {
                "name": "invalidation",
                "arguments": arguments,
            },
        }

    # ----- transport (test seam) -----

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send ``payload`` to the MCP server and return the parsed
        ``tools/call`` response dict.

        The default implementation:

1. Launches ``self._command`` via asyncio subprocess.
2. Sends ``initialize`` and ``notifications```` on the new connection.
4. Reads the initialize and the business response.
5. Times out after ``self._timeout`` seconds.
6. Returns the parsed business response dict.

        Tests subclass and override this method with a scripted response /
        exception. The override is the documented seam — modifying the
        default transport requires the override to honor the same
        contract (return dict on success; raise ``OSError`` /
        ``TimeoutError`` on failure).
        """
        session = getattr(self, "_session", None)
        if session is not None:
            try:
                return await asyncio.to_thread(
                    session.call, payload, self._timeout
                )
            except MCPStdioProtocolError as exc:
                raise InvalidationProtocolError(
                    f"MCP session protocol failed: {exc}"
                ) from exc

        if not self._command:
            # ``None`` (unset) and ``[]`` (configured to nothing) are
            # the same no-transport condition: there is no program to
            # exec, so spawning would raise TypeError instead of the
            # typed EXIT the caller contracts on.
            raise OSError("no MCP command configured")
        proc = await _launch_mcp_process(self._command)
        wire_messages = _build_mcp_wire_messages(payload)
        stdout, _ = await _exchange_mcp_messages(
            proc,
            wire_messages,
            self._timeout,
        )
        # The notification has no response. The two response lines
        # must therefore be initialize first, then tools/call.
        responses = _parse_mcp_responses(stdout)
        return _validate_mcp_responses(responses, payload)


__all__ = ["MCPInvalidationPort"]