"""MCP stdio transport for the push seam (Plan D0 I-T4, SC18 / SC20, §B4;
spec `operator-console-host-contract` T1).

The push port sends each envelope's wire shape to the **iknow-memory**
MCP ``push`` tool via JSON-RPC 2.0 over stdio. After T1 the wire shape
is the **one envelope per 资料** declared by the spec — ``source_ref``
+ the unit's extracted body + ``chunks[]`` of ``{content, index}`` +
``metadata`` + ``valid_from`` / ``valid_until``. Wire field names are
verbatim the schema in ``src/tools/push.ts``. Only the single-envelope
arm of that union is a ``z.strictObject``; fin sends the ``items[]``
arm, whose item objects are a plain ``z.object``, so an extra key here
would be silently ignored by the service rather than refused. That
asymmetry is why this module validates its own wire shape and refuses
to ship it.

**no re-splitting** (§B4「不二次切分」): fin owns chunking; the bytes
that leave here equal the bytes that were pushed. The chunks list is
forwarded verbatim — the receiving tool stores it as-is, never
re-normalizes the slice text and never re-runs the chunker.

**not a second chunker** — the push port forwards the envelope wire
fields unchanged and lets the receiving tool store the body + slices
verbatim. The adapter never inserts a sliding-window pass between fin
and the service.

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
* Envelope body missing → :class:`PushEmptyDocumentError`, refused
  before the round trip (the same T21 invalid-input refusal the
  receiving side names).

Concurrency / state
-------------------

The public ``push`` is sync and bridges to ``asyncio.run``. The
adapter tracks the ``(source_ref, body_content_id)`` keys it has
already seen so a replay is reported as ``duplicates`` rather than a
fresh ``written`` batch — the §B1「幂等基准」assertion lives in fin's
adapter, not on the service side, because the service only echoes
``chunk_ids`` and does not split them into new vs. duplicate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fin.corpus.push import body_content_id as _body_content_id
from fin.host.compile import DEFAULT_MCP_COMMAND
from fin.retrieve.input_validation import InputValidationLedger
from fin.retrieve.mcp_session import (
    MCPStdioProtocolError,
    MCPStdioSession,
)
from fin.retrieve.push_port import (
    PushEmptyBatchError,
    PushEmptyDocumentError,
    PushMetadataIncompleteError,
    PushProtocolError,
    PushServiceUnavailableError,
    content_identities_of,
    validate_batch_not_empty,
    validate_envelope_chunks,
    validate_wire_record,
    validate_wire_record_text,
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

    The iknow ``push`` schema (``PushItemSchema``, required on every
    item of the batch fin sends) rejects the WHOLE batch on missing
    ``metadata``. fin must
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


def _chunk_ids_of(envelope: dict[str, Any], *, chunk_count: int) -> tuple[str, ...]:
    """Pull ``chunk_ids`` out of the business envelope and validate it.

    ``chunk_count`` is the number of chunks across **all** envelopes the
    adapter sent — the receiving ``push`` tool hands back one
    ``chunk_id`` per slice, regardless of how many envelopes those
    slices were packed into. A multi-chunk envelope therefore
    contributes ``len(envelope.chunks)`` ids to the response. The
    length check pins that 1:1 correspondence: a short envelope would
    hide a lost chunk; a stray one would pretend a chunk we never
    sent had landed.
    """
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
    if len(chunk_ids) != chunk_count:
        raise PushProtocolError(
            f"push response carries {len(chunk_ids)} chunk ids for "
            f"{chunk_count} pushed chunks"
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
        session: MCPStdioSession | None = None,
    ) -> None:
        self._command: list[str] | None = list(command) if command else None
        self._timeout: float = float(timeout_seconds)
        self._ledger: InputValidationLedger | None = ledger
        self._session = session
        # ``(source_ref, body_content_id)`` membership set for envelopes
        # the adapter has already seen, so a replay is reported as
        # ``duplicates`` instead of a fresh ``written`` batch. The value
        # is a constant sentinel — the membership test is the only
        # invariant the bookkeeping needs (the chunk ids come back from
        # the receiver's own idempotency baseline).
        self._seen_keys: dict[tuple[str, str], bool] = {}

    @property
    def input_validation_failures(self) -> InputValidationLedger:
        """The「输入校验失败」count for refusals taken on this side (T21)."""
        if self._ledger is None:
            self._ledger = InputValidationLedger()
        return self._ledger

    # ----- PushPort surface -----

    def push(self, envelopes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Send ``envelopes`` to the MCP ``push`` tool and report the
        §B4 outcome.

        Each envelope is the **one envelope per 资料** shape declared by
        the spec: ``source_ref`` + the unit's extracted body + the
        child ``chunks`` with monotonic ``index`` from 0. Empty batches
        are refused by name before the round trip (§B2 第 5 条);
        metadata completeness is the receiving side's judgement, so the
        adapter does NOT pre-screen the batch but DOES translate the
        receiving schema's ``metadata`` rejection into
        :class:`PushMetadataIncompleteError`. A transport-level
        ``OSError`` becomes :class:`PushServiceUnavailableError`; a
        short or off-contract envelope becomes
        :class:`PushProtocolError`.

        ``block_ids`` is parallel to **chunks across all envelopes**
        (the receiving ``push`` tool hands back one ``chunk_id`` per
        slice — see ``src/tools/push.ts`` ``allChunkIds``). ``written``
        and ``duplicates`` count chunks so the three fields agree; an
        envelope that was already on this adapter's ``(source_ref,
        body_content_id)`` ledger contributes its chunk count to
        ``duplicates`` rather than to ``written``. The
        ``_seen_keys`` ledger keys on the envelope-level idempotency
        pair, not on chunk ids, so a multi-chunk envelope is one
        idempotency row.

        ``content_identities`` maps each sent ``source_ref`` to the
        ``content_identity`` the service says it stored beside it — the
        only value fin may later pair with that `source_ref` at
        ``submit_index_job``. A service that reports none yields an empty
        mapping; this adapter never substitutes its own hash. The pairing
        rule itself is :func:`fin.retrieve.push_port.content_identities_of`
        's, shared with every other push reader: it attributes a batch
        answer against the first-seen distinct ``source_ref`` values fin
        SENT, so the ``source_doc_ids[]`` the service reports alongside
        them is never a fin-side attribution input.
        """
        ledger = self.input_validation_failures
        validate_batch_not_empty(envelopes, ledger=ledger)
        # Pre-screen the envelope shape on the client side too — the
        # receiving schema's strictObject will refuse it, but failing
        # before the round trip is the typed refusal the suite asserts
        # on (T21: invalid input is counted, not silently dropped).
        for envelope in envelopes:
            validate_wire_record(envelope)
            validate_wire_record_text(envelope, ledger=ledger)
            validate_envelope_chunks(envelope)

        items = [self._to_push_item(envelope) for envelope in envelopes]
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
        chunk_count = sum(
            len(item.get("chunks") or []) for item in items
        )
        chunk_ids = _chunk_ids_of(envelope, chunk_count=chunk_count)
        content_identities = content_identities_of(
            envelope,
            source_refs=[str(wire["source_ref"]) for wire in envelopes],
        )

        written, duplicates = 0, 0
        for wire in envelopes:
            chunk_total = len(wire.get("chunks") or [])
            key = (
                str(wire["source_ref"]),
                str(_body_content_id(wire)),
            )
            if key in self._seen_keys:
                # The adapter's ledger remembers the envelope as a
                # duplicate before the round trip; the server is
                # expected to echo the same chunk ids (its own
                # idempotency baseline — §B1 / §B4), so the chunk ids
                # returned this round are exactly the ones the original
                # write produced.
                duplicates += chunk_total
            else:
                self._seen_keys[key] = True
                written += chunk_total

        return {
            "block_ids": list(chunk_ids),
            "written": written,
            "duplicates": duplicates,
            "content_identities": content_identities,
        }

    # ----- helpers -----

    @staticmethod
    def _to_push_item(envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Project one fin envelope into the iknow ``PushItem`` shape.

        The wire shape (verbatim the ``PushItemSchema`` in
        ``src/tools/push.ts``) is ``{content, source_ref, chunks,
        metadata, valid_from, valid_until}``. Every envelope field is
        forwarded verbatim — fin owns chunking (§B4「不二次切分」),
        so the receiving tool stores the body and slices as-is.
        """
        source_ref = envelope.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise PushProtocolError(
                f"push envelope {envelope.get('source_ref')!r} carries no "
                "source_ref"
            )
        body = envelope.get("content")
        if body is None:
            body = envelope.get("body")
        if not isinstance(body, str) or not body.strip():
            raise PushEmptyDocumentError(
                f"push envelope {source_ref!r} carries no body; "
                "an empty document is invalid input, not an empty result"
            )
        return {
            "content": body,
            "source_ref": source_ref,
            "chunks": [
                {"index": int(chunk["index"]), "content": str(chunk["content"])}
                for chunk in envelope.get("chunks", [])
            ],
            "metadata": dict(envelope.get("metadata") or {}),
            "valid_from": _now_iso(),
            "valid_until": _VALID_UNTIL_SENTINEL,
        }

    @staticmethod
    def _build_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
        """The JSON-RPC ``tools/call`` payload for ``push``.

        One envelope per ``items[i]``; the receiving schema indexes by
        position so ``chunk_ids`` from the response is parallel to
        ``items``.
        """
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
        session = getattr(self, "_session", None)
        if session is not None:
            try:
                return await asyncio.to_thread(
                    session.call, payload, self._timeout
                )
            except MCPStdioProtocolError as exc:
                raise PushProtocolError(
                    f"MCP session protocol failed: {exc}"
                ) from exc

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
    "PushEmptyDocumentError",
    "PushMetadataIncompleteError",
    "PushProtocolError",
    "PushServiceUnavailableError",
]
