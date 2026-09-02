"""MCP stdio transport for the push seam (Plan D0 I-T4, SC18 / SC20, §B4).

The push port sends each ``PushRecord``'s wire shape to the
**iknow-memory** MCP ``push`` tool via JSON-RPC 2.0 over stdio. Wire
field names are verbatim the **z.strictObject** schema in
``src/tools/push.ts`` — ``content`` / ``source_ref`` / ``metadata`` /
``valid_from`` / ``valid_until``. Extra keys would either be rejected
by ``strictObject`` or silently dropped on a mock without ``.strict()``;
both are the same kind of wire-shape failure this module refuses to
ship.

**no re-splitting** (§B4「不二次切分」): fin owns chunking; the bytes
that leave here equal the bytes that were pushed.

**not a second chunker** — the push port forwards ``PushRecord.to_wire()``
fields unchanged and lets the receiving tool store the text verbatim.
The adapter never inserts a sliding-window pass between fin and the
service.

Failure contract
----------------

* Transport never reached → :class:`PushServiceUnavailableError` (SC21).
* JSON-RPC error response or short / off-contract envelope →
  :class:`PushProtocolError` (SC21).
* The receiving schema's ``metadata`` rejection → the §B4 / SC20 typed
  refusal :class:`PushMetadataIncompleteError`, raised by name rather
  than collapsed into the generic ``push_protocol_error``.
* Empty batch is invalid input (§B2 第 5 条) →
  :class:`PushEmptyBatchError`, refused before the round trip.

Concurrency / state
-------------------

The public ``push`` is sync and bridges to ``asyncio.run``. The
adapter tracks the ``(source_ref, content_id)`` keys it has already
seen so a replay is reported as ``duplicates`` rather than a fresh
``written`` batch — the §B1「幂等基准」assertion lives in fin's
adapter, not on the service side, because the service only echoes
``chunk_ids`` and does not split them into new vs. duplicate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fin.host.compile import DEFAULT_MCP_COMMAND
from fin.retrieve.input_validation import InputValidationLedger
from fin.retrieve.push_port import (
    PushEmptyBatchError,
    PushMetadataIncompleteError,
    PushProtocolError,
    PushServiceUnavailableError,
    validate_batch_not_empty,
)

# The iknow ``push`` schema requires ``valid_until`` to be a strict ISO
# datetime (no ``.optional()``), so "null" / "open-ended" has to
# serialize as a real but unreachable future instant. ``9999-12-31`` is
# the standard "no end" sentinel: it is strictly after any plausible
# ``valid_from`` and never collides with a real closing instant.
_VALID_UNTIL_SENTINEL: str = "9999-12-31T23:59:59.999Z"

_MCP_PROTOCOL_VERSION: str = "2025-06-18"
_INITIALIZE_REQUEST_ID: int = 1
_TOOL_REQUEST_ID: int = 2

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """The current UTC instant as ISO 8601, millisecond precision.

    The schema rejects anything that does not parse as
    ``z.string().datetime()`` (an ISO 8601 instant); ``Z`` is the
    short form of ``+00:00`` and matches the format iknow-memory
    writes back in its own tests.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _is_blank(value: str | None) -> bool:
    """A whitespace-only ``value`` is blank; ``None`` is not."""
    return value is not None and not value.strip()


# ----- stdio JSON-RPC transport helpers (copied from mcp_adapter to
# avoid coupling to its private API) -----


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
            "clientInfo": {"name": "fin-push", "version": "0.1.0"},
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
            raise PushProtocolError(
                f"malformed JSON-RPC response: {exc}"
            ) from exc
    return responses


def _validate_initialize_response(response: Any) -> None:
    """Validate the initialize response before accepting tool output."""
    if (
        not isinstance(response, dict)
        or response.get("id") != _INITIALIZE_REQUEST_ID
    ):
        raise PushProtocolError(
            f"invalid MCP initialize response: {response!r}"
        )
    if "error" in response:
        raise PushProtocolError(
            f"MCP initialize returned JSON-RPC error: "
            f"{response['error']!r}"
        )
    if not isinstance(response.get("result"), dict):
        raise PushProtocolError(
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
        raise PushProtocolError(
            f"invalid MCP tools/call response: {response!r}"
        )
    return response


def _validate_mcp_responses(
    responses: list[Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate handshake ordering and return the business response."""
    if len(responses) < 2:
        raise PushProtocolError(
            "MCP handshake or tools/call response is missing"
        )
    _validate_initialize_response(responses[0])
    return _validate_tool_response(responses[1], payload)


# ----- response-shape extraction -----


def _is_metadata_incomplete_message(message: str) -> bool:
    """Detect the schema-side ``metadata`` rejection in a JSON-RPC error.

    The iknow ``push`` schema (``z.strictObject(PushItemSchema)``)
    rejects the WHOLE batch on missing ``metadata``. fin must
    translate that single rule into the §B4 / SC20 typed refusal
    rather than collapsing it into the generic protocol failure.
    """
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return "metadata" in lowered and (
        "required" in lowered
        or "missing" in lowered
        or "must contain" in lowered
        or "b4 required" in lowered
    )


def _extract_push_business_envelope(response: Any) -> dict[str, Any]:
    """Read the inner ``{"chunk_ids": [...]}`` envelope out of an MCP
    ``tools/call`` response.

    Three accepted shapes, mirroring the retrieve adapter's
    forward-compatibility stance:

    1. ``{"result": {"content": [{"type": "text", "text": "{\"chunk_ids\":…}"}]}}``
       — the canonical MCP ``tools/call`` content-block envelope.
    2. ``{"result": {"chunk_ids": [...]}}`` — server unwraps the tool
       output into ``result`` directly.
    3. ``{"chunk_ids": [...]}}`` — top-level ``chunk_ids``.

    Any other shape — a missing ``chunk_ids`` list, a non-list, a
    count mismatch — is :class:`PushProtocolError`.
    """
    if not isinstance(response, dict):
        raise PushProtocolError(
            f"push response is {type(response).__name__}, expected a "
            "mapping"
        )

    # JSON-RPC error envelope: schema-side rejection or transport-side
    # error. The receiving schema's ``metadata`` rule is the only error
    # message fin reads — anything else is a generic protocol failure.
    if "error" in response:
        error = response["error"]
        message = str(error.get("message", "") if isinstance(error, dict) else error)
        if _is_metadata_incomplete_message(message):
            raise PushMetadataIncompleteError(
                f"push rejected by receiving schema: {message}"
            )
        raise PushProtocolError(
            f"MCP returned JSON-RPC error: {message!r}"
        )

    # Variant 1: content-block envelope inside ``result``.
    result = response.get("result")
    if isinstance(result, dict) and "content" in result and "chunk_ids" not in result:
        content = result["content"]
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    inner = json.loads(first["text"])
                except ValueError as exc:
                    raise PushProtocolError(
                        f"MCP content text is not JSON: {exc}"
                    ) from exc
                if isinstance(inner, dict):
                    return inner
        raise PushProtocolError(
            f"push response content block is unparseable: {result!r}"
        )

    # Variant 2: ``chunk_ids`` lives inside ``result``.
    if isinstance(result, dict) and "chunk_ids" in result:
        return result

    # Variant 3: top-level ``chunk_ids``.
    if "chunk_ids" in response:
        return response

    raise PushProtocolError(
        f"push response carries no chunk_ids: {response!r}"
    )


def _chunk_ids_of(envelope: dict[str, Any], *, record_count: int) -> tuple[str, ...]:
    """Pull ``chunk_ids`` out of the business envelope and validate it."""
    chunk_ids = envelope.get("chunk_ids")
    if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, list):
        raise PushProtocolError(
            f"push response chunk_ids is {type(chunk_ids).__name__}, "
            "expected a sequence of chunk ids"
        )
    if not all(isinstance(chunk_id, str) for chunk_id in chunk_ids):
        raise PushProtocolError(
            "push response chunk_ids contains a non-string chunk id"
        )
    if len(chunk_ids) != record_count:
        raise PushProtocolError(
            f"push response carries {len(chunk_ids)} chunk ids for "
            f"{record_count} pushed records"
        )
    return tuple(chunk_ids)


# ----- port -----


class MCPPushPort:
    """stdio JSON-RPC client for the iknow-memory MCP ``push`` tool
    (Plan D0 I-T4).

    Parameters
    ----------
    command
        The MCP server launch command (e.g. ``["iknow-memory"]``). When
        unset (``None``), ``_call_mcp`` raises an ``OSError`` so the
        caller sees a typed ``PushServiceUnavailableError`` rather than
        a silent fall-through.
    timeout_seconds
        Per-call timeout on the JSON-RPC exchange. Default 5s (operator
        override via constructor).
    ledger
        Where invalid-input refusals (e.g. :class:`PushEmptyBatchError`)
        are counted (T21 / SC42). ``None`` (the default) means an
        in-memory ledger this adapter owns — counting is never off,
        only unpersisted.
    """

    def __init__(
        self,
        command: Sequence[str] | None = DEFAULT_MCP_COMMAND,
        *,
        timeout_seconds: float = 5.0,
        ledger: InputValidationLedger | None = None,
    ) -> None:
        self._command: list[str] | None = list(command) if command else None
        self._timeout: float = float(timeout_seconds)
        self._ledger: InputValidationLedger | None = ledger
        # ``(source_ref, content_id)`` → ``block_id`` for the keys the
        # adapter has already seen, so a replay is reported as
        # ``duplicates`` instead of a fresh ``written`` batch.
        self._seen_keys: dict[tuple[str, str], str] = {}

    @property
    def input_validation_failures(self) -> InputValidationLedger:
        """The「输入校验失败」count for refusals taken on this side (T21)."""
        if self._ledger is None:
            self._ledger = InputValidationLedger()
        return self._ledger

    # ----- PushPort surface -----

    def push(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Send ``records`` to the MCP ``push`` tool and report the
        §B4 outcome.

        Empty batches are refused by name before the round trip
        (§B2 第 5 条); metadata completeness is the receiving side's
        judgement, so the adapter does NOT pre-screen the batch but
        DOES translate the receiving schema's ``metadata`` rejection
        into :class:`PushMetadataIncompleteError`. A transport-level
        ``OSError`` becomes :class:`PushServiceUnavailableError`; a
        short or off-contract envelope becomes
        :class:`PushProtocolError`.

        ``block_ids`` is parallel to ``records``: a record whose
        ``(source_ref, content_id)`` was already seen yields the
        **stored** ``chunk_id``, so a replay returns the same ids as
        the original write. ``written`` / ``duplicates`` come from the
        adapter's own idempotency ledger (the service only echoes
        ``chunk_ids`` — it does not split them into new vs.
        duplicate).
        """
        ledger = self.input_validation_failures
        validate_batch_not_empty(records, ledger=ledger)

        items = [self._to_push_item(record) for record in records]
        payload = self._build_payload(items)

        try:
            response = asyncio.run(self._call_mcp(payload))
        except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
            # SC21「检索服务不可用」: the transport never got an
            # answer, so nothing about the batch is known to have
            # landed. The original exception is preserved as
            # ``__cause__`` for the diagnostic trace.
            raise PushServiceUnavailableError(
                f"push to the retrieval service failed to reach it: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        envelope = _extract_push_business_envelope(response)
        chunk_ids = _chunk_ids_of(envelope, record_count=len(records))

        written, duplicates = 0, 0
        for record, block_id in zip(records, chunk_ids):
            key = (str(record["source_ref"]), str(record["content_id"]))
            if key in self._seen_keys:
                duplicates += 1
            else:
                self._seen_keys[key] = block_id
                written += 1

        return {
            "block_ids": list(chunk_ids),
            "written": written,
            "duplicates": duplicates,
        }

    # ----- helpers -----

    @staticmethod
    def _to_push_item(record: Mapping[str, Any]) -> dict[str, Any]:
        """Translate one fin wire record into the iknow ``PushItem`` shape.

        Field names are verbatim the **z.strictObject** schema in
        ``src/tools/push.ts``. No fin-side keys (``block_id``,
        ``content_id``, ``idempotency_key``, ``parent_block_id``,
        ``parent_text``, ``text``) are forwarded — ``strictObject``
        would refuse them on the wire.
        """
        text = record.get("text")
        source_ref = record.get("source_ref")
        metadata = record.get("metadata")
        if not isinstance(text, str) or not text.strip():
            raise PushEmptyBatchError(
                f"push of block {record.get('block_id')!r} carries no text; "
                "an empty document is invalid input, not an empty result"
            )
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise PushProtocolError(
                f"push of block {record.get('block_id')!r} carries no "
                "source_ref"
            )
        if not isinstance(metadata, Mapping) or len(metadata) == 0:
            raise PushMetadataIncompleteError(
                f"push of block {record.get('block_id')!r} carries no "
                "metadata (B4 requires at least one key)"
            )
        return {
            "content": text,
            "source_ref": source_ref,
            "metadata": dict(metadata),
            "valid_from": _now_iso(),
            "valid_until": _VALID_UNTIL_SENTINEL,
        }

    @staticmethod
    def _build_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
        """The JSON-RPC ``tools/call`` payload for ``push``."""
        return {
            "jsonrpc": "2.0",
            "id": _TOOL_REQUEST_ID,
            "method": "tools/call",
            "params": {
                "name": "push",
                "arguments": {"items": items},
            },
        }

    # ----- transport (test seam) -----

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send ``payload`` to the MCP server and return the parsed
        ``tools/call`` response dict.

        The default implementation:

        1. Launches ``self._command`` via asyncio subprocess.
        2. Sends ``initialize`` and ``notifications/initialized`` on
           the new connection.
        3. Writes the JSON-serialized ``tools/call`` payload.
        4. Reads the initialize and business responses.
        5. Times out after ``self._timeout`` seconds.
        6. Returns the parsed business response dict.

        Tests subclass and override this method with a scripted
        response / exception. The override is the documented seam —
        modifying the default transport requires the override to honor
        the same contract (return dict on success; raise ``OSError`` /
        ``TimeoutError`` on failure).
        """
        if not self._command:
            # ``None`` (unset) and ``[]`` (configured to nothing) are
            # the same no-transport condition: there is no program to
            # exec, so spawning would raise ``TypeError`` instead of
            # the typed failure the caller contracts on.
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


__all__ = [
    "MCPPushPort",
    "PushEmptyBatchError",
    "PushMetadataIncompleteError",
    "PushProtocolError",
    "PushServiceUnavailableError",
]
