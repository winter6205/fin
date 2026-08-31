"""MCP adapter for the product retrieve path (T3, ADR 0032).

The adapter is the v1 retrieve implementation behind
``RetrievePort``: a stdio JSON-RPC client to the
**iknow-memory** MCP server (separate TypeScript repo). It
supersedes the in-process BM25 adapter for the **product**
compile path; BM25 is retained as an offline / test adapter
only (ADR 0007 archived by 0030).

Wire-level protocol
-------------------

MCP uses JSON-RPC 2.0 over stdio. The adapter sends a
newline-delimited ``initialize`` request, the
``notifications/initialized`` notification, and then the business
request on each new connection:

.. code-block:: json

    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "retrieve",
                "arguments": {"query": "...", "top_k": 8,
                              "document_id": "...",
                              "filter": {"cik": "...", ...}}}}

The server replies with one newline-delimited JSON object per
request on stdout. The adapter validates the initialize response
before parsing the ``tools/call`` response.

The adapter completes the MCP session handshake
(``initialize`` → ``notifications/initialized``) on the same
connection before it sends ``tools/call``. It DOES NOT inline OCR /
XBRL / parse / locator
mapping into the ranking call (complexity-anti-drift). The
The returned ``source_ref`` is the canonical fin locator serialization
defined by ``fin.corpus.push`` and is parsed as a separate post-rank
step. A retrieval service's internal id, if present for a transitional
integration, uses ``retrieval_id`` and is resolved through the
explicitly supplied locator map; it is never carried in ``source_ref``
or passed to the citation gate.

Mapper-drop is not silent: every dropped hit is counted on the
adapter's :attr:`MCPRetrieveAdapter.mapping_failures` ledger with a
stable reason code (T17 / SC22 — see
:mod:`fin.retrieve.hit_mapping`), and the ledger persists the count
when the operator configures a path. The chunks answer is still
``{"chunks": []}``, so an adapter built with an empty map looks like
a miss on the wire; the counter is what tells the operator it is not
one. Wiring the map remains the caller's job, and
``fin.host.compile_graph`` refuses to build a product adapter
without one: it derives canonical ``source_ref`` keys with
``fin.corpus.push.serialize_source_ref`` from the extracted tree
unless the operator supplies an explicit compatibility map.

Error contract
--------------

* An empty / whitespace ``query`` is **invalid input** (T21 /
  §B2 第 5 条): it raises ``EmptyQueryError`` and is counted on
  :attr:`MCPRetrieveAdapter.input_validation_failures`, without
  touching MCP.
* Unknown ``document_id`` and empty filter values (``cik=""``,
  etc.) are legal empty results and return ``{"chunks": []}``
  without touching MCP.
* MCP subprocess spawn failure, protocol timeout, malformed
  response, or JSON-RPC error response → typed exception
  ``RetrieveUnavailableError`` internally; ``retrieve()``
  translates that to ``{"error": "retrieve_unavailable"}`` —
  the product EXIT shape (spec success-criteria item 6).
* The adapter NEVER falls back to BM25 on MCP failure (ADR
  0030 / error-handling-enforcer).

Concurrency
-----------

The public ``retrieve`` is sync and bridges to ``aretrieve``
via ``asyncio.run``. Each call uses its own JSON-RPC request ids
and per-call transport (subprocess lifetime is per-call here for
simplicity — a long-lived connection is a later optimization).
Concurrent calls share the read-only locator map; there is no
shared mutable state.

Test seam
---------

The default transport (``_call_mcp``) launches a subprocess and
completes one MCP session per call. Tests subclass and override
``_call_mcp`` to inject a scripted response or scripted exception.
The default is NEVER invoked during the default pytest run — the
test surface is hermetic (no live MCP server).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fin.corpus.extracted import Locator
from fin.retrieve.degradation import (
    NOT_DEGRADED,
    DegradationError,
    DegradationSignal,
    read_degradation_signal,
)
from fin.retrieve.hit_mapping import MappingFailureLedger, map_hits_to_chunks
from fin.retrieve.input_validation import (
    InputValidationLedger,
    validate_query,
)


_DEFAULT_LIMIT = 8
_LIMIT_CAP = 20
_MCP_PROTOCOL_VERSION = "2025-06-18"
_INITIALIZE_REQUEST_ID = 1
_TOOL_REQUEST_ID = 2

logger = logging.getLogger(__name__)


# ----- typed exception -----


class RetrieveUnavailableError(Exception):
    """The MCP retrieve transport failed (subprocess down,
    protocol timeout, malformed response, or JSON-RPC error).

    This is a TYPED EXIT (ADR 0030 / error-handling-enforcer).
    It is raised internally by ``aretrieve`` and translated to
    ``{"error": "retrieve_unavailable"}`` by the sync
    ``retrieve`` wrapper. The host tool reads the structured
    error dict and surfaces it to the citation gate as a typed
    stop kind.

    The adapter does NOT swallow MCP failures as empty chunks
    and does NOT fall back to BM25.

    :attr:`code` is the token ``retrieve`` puts on the product
    error dict, and :attr:`signal` carries the response's §B3
    degradation state so a failed *and* degraded request reaches
    the host with its reasons attached (T20).
    """

    code: str = "retrieve_unavailable"
    signal: DegradationSignal = NOT_DEGRADED

    def error_dict(self) -> dict[str, Any]:
        """The product EXIT shape for this failure.

        Undegraded failures keep the historical single-key dict; a
        degraded one adds the two verbatim §B3 fields, because a
        named failure that drops the reasons is exactly the
        「静默」 SC39 counts.
        """
        return {"error": self.code, **self.signal.as_envelope_fields()}


class RetrieveDegradedFailureError(RetrieveUnavailableError):
    """The service answered degraded AND did not answer with results.

    §B2 第 2 条: when the request itself failed, the degradation is a
    named failure — never ``{"chunks": []}``, which would tell the
    caller the corpus has nothing while the arms were down. Kept in
    the :class:`RetrieveUnavailableError` family so callers that
    already branch on "retrieve did not answer" need no change, with
    a distinct :attr:`code` for callers that must tell the two apart.
    """

    code = "retrieve_degraded_failure"

    def __init__(self, message: str, *, signal: DegradationSignal) -> None:
        super().__init__(message)
        self.signal = signal


# ----- limit resolution -----


def _resolve_limit(limit: int) -> int:
    """Apply the port's limit rules: default 8, cap 20 (ADR 0009)."""
    if limit <= 0:
        return _DEFAULT_LIMIT
    if limit > _LIMIT_CAP:
        return _LIMIT_CAP
    return limit


def _is_blank(value: str | None) -> bool:
    """Return ``True`` when ``value`` is an empty string or
    whitespace-only.

    ``None`` is **not** blank — ``None`` means the caller did
    not pass this filter, while ``""`` / ``"   "`` means the
    caller explicitly passed an empty filter value (a miss
    shape per spec). The two cases must be treated differently.
    """
    return value is not None and not value.strip()


async def _launch_mcp_process(
    command: list[str],
) -> asyncio.subprocess.Process:
    """Launch the MCP subprocess and preserve its typed spawn failure."""
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
            "clientInfo": {"name": "fin-retrieve", "version": "0.1.0"},
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
    """Send one MCP session and preserve timeout/process exit semantics."""
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
            raise RetrieveUnavailableError(
                f"malformed JSON-RPC response: {exc}"
            ) from exc
    return responses


def _validate_initialize_response(response: Any) -> None:
    """Validate the initialize response before accepting tool output."""
    if (
        not isinstance(response, dict)
        or response.get("id") != _INITIALIZE_REQUEST_ID
    ):
        raise RetrieveUnavailableError(
            f"invalid MCP initialize response: {response!r}"
        )
    if "error" in response:
        raise RetrieveUnavailableError(
            f"MCP initialize returned JSON-RPC error: "
            f"{response['error']!r}"
        )
    if not isinstance(response.get("result"), dict):
        raise RetrieveUnavailableError(
            "MCP initialize response has no result"
        )


def _validate_tool_response(
    response: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate and return the tools/call response object."""
    if (
        not isinstance(response, dict)
        or response.get("id") != payload.get("id")
    ):
        raise RetrieveUnavailableError(
            f"invalid MCP tools/call response: {response!r}"
        )
    return response


def _degradation_of_response(
    response: Any,
    *,
    business_result: Any | None = None,
) -> DegradationSignal:
    """Read the §B3 signal from the relevant response envelope.

    Servers put it on the tool result; a JSON-RPC error envelope has no
    result to put it on, so the top level is read too. Once the canonical
    content block has been decoded, ``business_result`` is the actual
    business envelope and is read last. All layers are read strictly —
    an unreadable signal raises rather than passing as 未降级.
    """
    top_level = read_degradation_signal(response)
    if top_level.degraded:
        return top_level
    if isinstance(response, dict):
        result_signal = read_degradation_signal(response.get("result"))
        if result_signal.degraded:
            return result_signal
    if business_result is not None:
        return read_degradation_signal(business_result)
    return NOT_DEGRADED


def _unavailable(
    message: str, signal: DegradationSignal
) -> RetrieveUnavailableError:
    """The typed EXIT for a request that produced no usable result.

    Degraded responses take the named degraded failure so the caller can
    tell「能力降级且这次没答上」from a plain transport outage — and so the
    reasons survive to the user-facing notice (SC39).
    """
    if signal.degraded:
        return RetrieveDegradedFailureError(message, signal=signal)
    return RetrieveUnavailableError(message)


def _validate_mcp_responses(
    responses: list[Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate handshake ordering and return the business response."""
    if len(responses) < 2:
        raise RetrieveUnavailableError(
            "MCP handshake or tools/call response is missing"
        )
    _validate_initialize_response(responses[0])
    return _validate_tool_response(responses[1], payload)


# ----- adapter -----


class MCPRetrieveAdapter:
    """stdio JSON-RPC client to the iknow-memory MCP server.

    Parameters
    ----------
    command
        The MCP server launch command (e.g. ``["iknow-memory",
        "serve"]``). When unset (``None`` or empty),
        ``_call_mcp`` raises
        ``OSError("no MCP command configured")`` so the caller
        sees a typed ``retrieve_unavailable`` rather than a
        silent fall-through.
    locator_map
        Explicit transitional map from ``retrieval_id`` to a fin
        ``Locator``. Canonical ``source_ref`` values do not need this
        map. Non-canonical hits using an old ``source_ref`` key remain
        supported only through this explicit compatibility seam. An
        empty map therefore drops only non-canonical hits;
        ``compile_graph`` still requires a non-empty map for compatibility
        with integrations that use retrieval ids.
    timeout_seconds
        Per-call timeout on the JSON-RPC exchange. Default 5s
        (operator override via constructor).
    mapping_ledger
        Where mapper-drops are counted (T17 / SC22). ``None``
        (the default) means an in-memory ledger this adapter
        owns — counting is never off, only unpersisted. Pass a
        ledger with a ``path`` (or share the host's) to make the
        count outlive the run.
    input_validation_ledger
        Where invalid input is counted (T21 / SC42). Same
        default: an in-memory ledger this adapter owns.
    """

    # Class-level defaults so the counters exist even for a subclass
    # that skips ``__init__`` (the documented ``_call_mcp`` test seam
    # does exactly that). The properties fill them lazily.
    _mapping_ledger: MappingFailureLedger | None = None
    _input_validation_ledger: InputValidationLedger | None = None

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        locator_map: dict[str, Locator] | None = None,
        known_document_ids: set[str] | None = None,
        timeout_seconds: float = 5.0,
        mapping_ledger: MappingFailureLedger | None = None,
        input_validation_ledger: InputValidationLedger | None = None,
    ) -> None:
        self._command: list[str] | None = (
            list(command) if command is not None else None
        )
        # Defensive copy: a caller mutating the source dict
        # after construction must not affect this adapter.
        self._locator_map: dict[str, Locator] = dict(locator_map or {})
        # ``None`` means "no client-side membership test —
        # defer to the MCP server". ``set()`` means "no
        # document is known — every document_id filter short-
        # circuits to a miss". ``{"DOC-..."}`` enables the
        # membership test.
        self._known_doc_ids: frozenset[str] | None = (
            frozenset(known_document_ids)
            if known_document_ids is not None
            else None
        )
        self._timeout: float = float(timeout_seconds)
        self._mapping_ledger = mapping_ledger
        self._input_validation_ledger = input_validation_ledger

    # ----- public surface -----

    @property
    def input_validation_failures(self) -> InputValidationLedger:
        """The「输入校验失败」count for this adapter (T21 / SC42).

        Like :attr:`mapping_failures`, an adapter built without one
        still counts: the default is an in-memory ledger created on
        first access.
        """
        ledger = self._input_validation_ledger
        if ledger is None:
            ledger = InputValidationLedger()
            self._input_validation_ledger = ledger
        return ledger

    @property
    def mapping_failures(self) -> MappingFailureLedger:
        """The ledger counting this adapter's mapper-drops (SC22).

        An adapter built without one still counts: the default is an
        in-memory ledger, created on first access, so a map-less
        adapter's silence is diagnosable from its own surface rather
        than only from the absence of chunks.
        """
        ledger = self._mapping_ledger
        if ledger is None:
            ledger = MappingFailureLedger()
            self._mapping_ledger = ledger
        return ledger

    def retrieve(
        self,
        query: str,
        *,
        document_id: str | None = None,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Sync retrieve: returns ``{"chunks": [...]}`` or
        ``{"error": "retrieve_unavailable"}``.

        Unknown filters short-circuit to the miss shape BEFORE the
        MCP round-trip; an empty query raises ``EmptyQueryError``
        there instead (T21 — invalid input is not a miss, and the
        typed error travels out of this seam rather than being
        flattened into an envelope the model reads as "no hits").
        Any MCP transport / protocol failure surfaces as
        ``{"error": "retrieve_unavailable"}`` (no BM25
        fallback).
        """
        try:
            return asyncio.run(
                self.aretrieve(
                    query,
                    document_id=document_id,
                    cik=cik,
                    form=form,
                    ticker=ticker,
                    filing_date=filing_date,
                    limit=limit,
                )
            )
        except RetrieveUnavailableError as exc:
            # Typed EXIT — the product shape is the error dict. A
            # degraded failure carries its §B3 reasons along (T20).
            return exc.error_dict()
        except DegradationError as exc:
            # The service answered with an unreadable / renamed
            # degradation signal (§B3). Named EXIT rather than a
            # coerced result: neither「未降级」nor an empty hit list
            # would be a true reading of that response.
            return {"error": exc.code}

    async def aretrieve(
        self,
        query: str,
        *,
        document_id: str | None = None,
        cik: str | None = None,
        form: str | None = None,
        ticker: str | None = None,
        filing_date: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Async retrieve: raises ``RetrieveUnavailableError``
        on transport failure; ``retrieve`` translates that to
        the structured error dict.

        Tests subclass and override ``_call_mcp`` for scripted
        transport. The default transport launches a subprocess.
        """
        # Empty / whitespace query: invalid input (T21 / §B2 第 5 条),
        # named and counted before any round trip. The other
        # short-circuits below stay miss shapes — they are legal empty
        # results, and the split is the whole point of the ticket.
        validate_query(query, ledger=self.input_validation_failures)
        # Empty / whitespace document_id filter: miss shape.
        if document_id is not None and not document_id.strip():
            return {"chunks": []}
        # Unknown document_id filter (when ``known_document_ids``
        # is configured): miss shape. We do this BEFORE the
        # MCP round-trip so the operator never pays for an
        # expensive retrieve call on a guaranteed miss. When
        # ``known_document_ids`` is ``None`` the adapter defers
        # to the MCP server's own membership logic.
        if (
            document_id is not None
            and self._known_doc_ids is not None
            and document_id not in self._known_doc_ids
        ):
            return {"chunks": []}
        # Empty filter values (cik / form / ticker /
        # filing_date) are miss shapes — short-circuit before
        # MCP per spec.
        if any(_is_blank(v) for v in (cik, form, ticker, filing_date)):
            return {"chunks": []}

        effective_limit = _resolve_limit(limit)

        # Build the JSON-RPC payload. The MCP ``tools/call``
        # method takes ``arguments`` matching the tool's input
        # schema; we forward the lexical query, the optional
        # ``top_k`` (limit), and the optional ``filter`` block.
        arguments: dict[str, Any] = {
            "query": query,
            "top_k": effective_limit,
        }
        if document_id is not None:
            arguments["document_id"] = document_id
        # Only filters the caller actually passed. Blank values
        # already short-circuited above, so a non-``None`` value
        # here is a real filter; a ``None`` means "unset" and must
        # NOT be sent as an explicit ``null`` the server would try
        # to match on.
        for key, value in (
            ("cik", cik),
            ("form", form),
            ("ticker", ticker),
            ("filing_date", filing_date),
        ):
            if value is not None:
                arguments.setdefault("filter", {})[key] = value

        payload = {
            "jsonrpc": "2.0",
            "id": _TOOL_REQUEST_ID,
            "method": "tools/call",
            "params": {
                "name": "retrieve",
                "arguments": arguments,
            },
        }

        # Transport. The default ``_call_mcp`` launches a
        # subprocess; tests override it.
        try:
            response = await self._call_mcp(payload)
        except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
            # EXIT: typed MCP-transport failure. Translate to
            # the product error dict at the sync seam.
            raise RetrieveUnavailableError(
                f"MCP transport failed: {exc!r}"
            ) from exc

        # §B3 signal, read once for this response: the envelope may
        # carry it beside the JSON-RPC error or inside ``result``.
        # Read BEFORE the failure branches so a degraded failure keeps
        # its reasons instead of collapsing into a bare unavailable.
        envelope_signal = _degradation_of_response(response)

        # JSON-RPC error response: surface as unavailable.
        if isinstance(response, dict) and "error" in response:
            raise _unavailable(
                f"MCP returned JSON-RPC error: {response['error']!r}",
                envelope_signal,
            )

        # Result shape: accept three envelope variants for
        # forward-compatibility with different MCP server
        # implementations:
        #
        # 1. ``{"result": {"hits": [...]}}`` — the standard
        #    JSON-RPC success envelope with the raw tool output
        #    as ``result``.
        # 2. ``{"hits": [...]}`` — server returns the raw tool
        #    output as the JSON-RPC result (some MCP servers
        #    unwrap automatically).
        # 3. ``{"result": {"content": [{"type": "text",
        #    "text": "<json with hits>"}]}}`` — the canonical
        #    MCP ``tools/call`` envelope with content blocks.
        if not isinstance(response, dict):
            raise RetrieveUnavailableError(
                f"MCP returned non-dict response: {response!r}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            # Variant 2: ``{"hits": [...]}`` at top level.
            if isinstance(response.get("hits"), list):
                result = response
            else:
                raise _unavailable(
                    f"MCP returned non-dict result: {result!r}",
                    envelope_signal,
                )
        # Variant 3: content-block envelope.
        if "hits" not in result and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and isinstance(
                    first.get("text"), str
                ):
                    try:
                        inner = json.loads(first["text"])
                    except ValueError as exc:
                        raise RetrieveUnavailableError(
                            f"MCP content text is not JSON: {exc}"
                        ) from exc
                    if isinstance(inner, dict):
                        result = inner
        signal = _degradation_of_response(
            response,
            business_result=result,
        )
        hits = result.get("hits")
        if not isinstance(hits, list):
            raise _unavailable(
                f"MCP returned non-list hits: {hits!r}", signal
            )

        # Map iknow ids → fin locators; drop unmapped, and count
        # every drop on the ledger (T17 / SC22: the drop must not be
        # silent). The cap is enforced AFTER mapping so the returned
        # list is the locatable subset, not a slice of unmapped ids.
        chunks = map_hits_to_chunks(
            hits,
            locator_map=self._locator_map,
            limit=effective_limit,
            ledger=self.mapping_failures,
        )
        # A degraded response with results is 放行 (§B2 第 1 条) — but it
        # travels with its flag, so the host can record the reasons and
        # tell the user this answer is not a complete one.
        return {"chunks": list(chunks), **signal.as_envelope_fields()}

    # ----- transport (test seam) -----

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send ``payload`` (a JSON-RPC request) to the MCP
        server and return the parsed response dict.

        The default implementation:

        1. Launches ``self._command`` via asyncio subprocess.
        2. Sends ``initialize`` and
           ``notifications/initialized`` on the new connection.
        3. Writes the JSON-serialized ``tools/call`` payload.
        4. Reads the initialize and business responses.
        5. Times out after ``self._timeout`` seconds.
        6. Returns the parsed business response dict.

        Tests subclass and override this method with a
        scripted response / exception. The override is the
        documented seam — modifying the default transport
        requires the override to honor the same contract
        (return dict on success; raise ``OSError`` /
        ``TimeoutError`` on failure).
        """
        if not self._command:
            # ``None`` (unset) and ``[]`` (configured to nothing)
            # are the same no-transport condition: there is no
            # program to exec, so spawning would raise TypeError
            # instead of the typed EXIT the caller contracts on.
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
    "MCPRetrieveAdapter",
    "RetrieveDegradedFailureError",
    "RetrieveUnavailableError",
]