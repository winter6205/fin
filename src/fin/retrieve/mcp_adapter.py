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
                "arguments": {"query": "...", "limit": 8,
                              "filters": {"document_id": "...",
                                          "cik": "...", ...}}}}

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
one. Canonical ``source_ref`` hits need no map, so
``fin.host.compile_graph`` auto-builds the product adapter with an
empty map and never reads the extracted tree (ADR 0046 / 0047); an
operator integrating via ``retrieval_id`` supplies the explicit
compatibility map.

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

Bounded source read
-------------------

``read_slice`` is the company leg of the shared ``read_source`` contract
(:mod:`fin.retrieve.source_read`, ADR 0087 / ADR 0088): one round trip
resolves one frozen unit at a code-point ``position``, passes through the
backend's slice with the request's own typed ``source_ref``, and answers
a malformed, non-advancing, or oversized wire with
``source_backend_contract_error`` and a transport failure with
``source_unavailable`` — always a typed dict, never an exception, never
text on a failure. It is the adapter's only read seam: the legacy
whole-body walk over a generic ``page`` ordinal was removed with the
``page`` contract, no compatibility path.

Concurrency
-----------

The public ``retrieve`` is sync and bridges to ``aretrieve``
via ``asyncio.run``. Each call uses its own JSON-RPC request ids
and per-call transport (subprocess lifetime is per-call here for
simplicity — a long-lived connection is a later optimization).
Concurrent calls share the read-only locator map; there is no
shared mutable state.

Each ``asyncio.run`` boundary re-attaches the caller's OpenTelemetry
context explicitly (see ``_run_with_trace_context``), because the
spec requires the trace context to be passed across the retrieval
boundary rather than relied on to propagate through a fresh loop.

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
import copy
import json
import logging
import threading
from typing import Any, Final, Mapping

from opentelemetry.context import Context
from opentelemetry.context import attach as _otel_attach
from opentelemetry.context import detach as _otel_detach
from opentelemetry.context import get_current as _otel_get_current

from fin.corpus.extracted import Locator
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve.degradation import (
    NOT_DEGRADED,
    DegradationError,
    DegradationSignal,
    DegradedEnvelopeInvalidError,
    DegradedFieldAliasError,
    read_degradation_signal,
)
from fin.retrieve.hit_mapping import (
    MappingFailureLedger,
    map_chunks_to_fin_chunks,
    map_hits_to_chunks,
)
from fin.retrieve.metadata_filters import (
    InvalidFilterError,
    invalid_filter,
    normalized_metadata_filters,
)
from fin.retrieve.input_validation import (
    InputValidationLedger,
    validate_query,
)
from fin.retrieve.mcp_session import (
    MCPStdioCancelledError,
    MCPStdioDeadlineExceededError,
    MCPStdioProtocolError,
    MCPStdioSession,
    default_mcp_timeout_seconds,
    exchange_oneshot_mcp,
    launch_oneshot_mcp_process,
)
from fin.retrieve.port import (
    DATABASE_ROWS_KEY,
    DATABASE_ROWS_ROWS_FIELD,
    DocumentScope,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS,
    SEARCH_MODE_FILTER_KEY,
    SQL_WINDOW_END_FILTER_KEY,
    SQL_WINDOW_START_FILTER_KEY,
    STRUCTURED_RESULT_KEY,
    STRUCTURED_SEARCH_UNAVAILABLE_CODE,
    UNSUPPORTED_FILTER_MODE_CODE,
    UNSUPPORTED_SOURCE_SCOPE_CODE,
    TierUnavailableError,
    _structured_query_refusal_code,
    database_rows_of,
    distribution_of,
    new_database_rows,
    prepare_document_scope,
    resolve_search_mode,
    resolve_sql_window,
    structured_result_of,
)
from fin.retrieve.read_port import validate_company_source_ref
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    INVALID_POSITION,
    SOURCE_BACKEND_CONTRACT_ERROR,
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    SOURCE_UNAVAILABLE,
    UNKNOWN_LOCATOR,
    position_error,
    serialized_result_bytes,
    source_error,
)
from fin.retrieve.tiers import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    SQL_NO_MATCHING_ROWS,
    SQL_PRODUCER_FAILURE_CODES,
)


_DEFAULT_LIMIT = 8


# The named failure vocabulary of the upstream lexical target (TUF/TUR),
# spelled off the producer's own tokens (upstream
# ``lexical-target-tokens.ts``). The projection to fin's vocabulary lives
# in :func:`_project_upstream_failure` below — one map, so a producer
# token can never be recoded at a second call site.
UPSTREAM_NOT_READY: Final[str] = "lexical_not_ready"
UPSTREAM_AUTHORIZATION_DENIED: Final[str] = "lexical_authorization_denied"
UPSTREAM_TRANSPORT_UNAVAILABLE: Final[str] = "lexical_transport_unavailable"
UPSTREAM_STALE_REVISION: Final[str] = "lexical_stale_revision"
UPSTREAM_PROTOCOL_ERROR: Final[str] = "lexical_protocol_error"
UPSTREAM_UNFILTERABLE_GENERATION: Final[str] = "lexical_unfilterable_generation"

NOT_READY_CODE: Final[str] = "not_ready"
RETRIEVE_PROTOCOL_ERROR_CODE: Final[str] = "retrieve_protocol_error"

# The upstream structured lane's own scope refusal: a request that
# combines ``structured_query`` with ``source_ref`` / ``metadata.*`` is
# refused with this code because a structured read has no exact-match
# filter channel, so the trusted scope is exactly what cannot be honored.
UPSTREAM_UNSUPPORTED_SCOPE_FIELD: Final[str] = "unsupported_field"


def _project_upstream_failure(
    body: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project one upstream named failure to fin's envelope, or ``None``
    when the body names no upstream token this adapter maps.

    Spec ``qa-chain-corpus-retrieval`` §"Result, error, readiness, and
    degradation wire": the named failure envelopes keep their own
    meaning and are never recoded into each other or into a successful
    empty. ``lexical_not_ready`` is the exact ``not_ready`` passthrough
    ONLY beside the producer's own proven cause/stage pair — a bare
    not-ready label stays the operational ``retrieve_unavailable``
    reading, because a generic label is not evidence of index absence.
    """
    token = body.get("error")
    if token == UPSTREAM_NOT_READY:
        if body.get("cause") == "index" and body.get("stage") == "index":
            return {"error": NOT_READY_CODE, "cause": "index", "stage": "index"}
        return None
    if token == UPSTREAM_AUTHORIZATION_DENIED:
        return {"error": "authorization_denied"}
    if token in (UPSTREAM_TRANSPORT_UNAVAILABLE, UPSTREAM_STALE_REVISION):
        return {"error": "retrieve_unavailable"}
    if token == UPSTREAM_PROTOCOL_ERROR:
        return {"error": RETRIEVE_PROTOCOL_ERROR_CODE}
    if token == UPSTREAM_UNFILTERABLE_GENERATION:
        return {"error": UNSUPPORTED_FILTER_MODE_CODE}
    if token == UPSTREAM_UNSUPPORTED_SCOPE_FIELD:
        return {"error": UNSUPPORTED_SOURCE_SCOPE_CODE}
    return None


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
    already branch on "retrieve did not answer" need no change.

    :attr:`code` IS the spec's wire token (spec qa-chain-corpus-retrieval
    §"Result, error, readiness, and degradation wire": the all-arms-failed
    envelope is ``retrieve_degraded``). It is deliberately one token, not
    an internal ``*_failure`` name projected at a separate mapping site:
    the exception family here is one-to-one with the wire contract, so a
    second vocabulary would only be a place for the two to drift.
    """

    code = "retrieve_degraded"

    def __init__(self, message: str, *, signal: DegradationSignal) -> None:
        super().__init__(message)
        self.signal = signal


class RetrieveBudgetExhaustedError(RetrieveUnavailableError):
    """The queue-inclusive budget expired on the session transport.

    The ONE fin-side ``cause=busy`` minting point (spec §"Result,
    error, readiness, and degradation wire"): the backend owns no
    queue and no budget, so queue saturation or total-budget
    exhaustion is observed here and reported with the stage it expired
    in — exactly ``queue``, ``startup``, or ``response``. The wire
    shape is the exact second ``not_ready`` form; it is never
    recoded into index work and never into a successful empty.
    """

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        # The stage vocabulary is validated by the ONE error that owns
        # it; this error carries the same wire stage, so it delegates.
        self.stage = MCPStdioDeadlineExceededError.validate_stage(stage)

    def error_dict(self) -> dict[str, Any]:
        return {"error": NOT_READY_CODE, "cause": "busy", "stage": self.stage}


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
    return await launch_oneshot_mcp_process(command)


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
) -> bytes:
    """Send one MCP session and return the two owed response frames.

    A conforming MCP stdio server answers and keeps serving, so the
    exchange reads the frames the protocol owes rather than waiting for
    the child to exit. The child is stopped and reaped here.
    """
    return await exchange_oneshot_mcp(
        proc, wire_messages, timeout=timeout
    )


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


def _stripped_adapter_field_names(
    sent_arguments: Mapping[str, Any],
    applied_arguments: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return adapter field names the MCP tool schema did not keep.

    Compares what fin sent on the wire with what the server echoes in
    ``applied_arguments``. Production servers that omit the echo field
    are handled by the caller — this function is only invoked when the
    echo is present.

    Plan D0 I-T1: the wire keys are ``limit`` and ``filters`` (legacy
    ``top_k`` / ``filter`` / top-level ``document_id`` are no longer
    sent). When the schema keeps ``filters`` but drops one key inside
    it, the per-key reason ``filters.<key>`` is appended instead of
    the umbrella ``filters``.
    """
    stripped: list[str] = []
    if "limit" in sent_arguments and "limit" not in applied_arguments:
        stripped.append("limit")

    sent_filters = sent_arguments.get("filters")
    applied_filters = applied_arguments.get("filters")
    if isinstance(sent_filters, Mapping) and sent_filters:
        if not isinstance(applied_filters, Mapping):
            stripped.append("filters")
        else:
            for filter_key in sent_filters:
                if filter_key not in applied_filters:
                    stripped.append(f"filters.{filter_key}")

    return tuple(stripped)


def _signal_for_stripped_adapter_fields(
    sent_arguments: Mapping[str, Any],
    applied_arguments: Any,
) -> DegradationSignal:
    """Detect schema stripping via ``applied_arguments`` and surface §B3.

    Mock iknow-memory schemas without ``.strict()`` drop unknown keys
    silently; when the server echoes which keys it kept, fin compares
    that echo to what it sent and refuses to treat the response as
    undegraded.
    """
    if not isinstance(applied_arguments, Mapping):
        return NOT_DEGRADED

    stripped = _stripped_adapter_field_names(sent_arguments, applied_arguments)
    if not stripped:
        return NOT_DEGRADED

    logger.warning(
        "adapter_field_stripped: MCP tool schema dropped fin adapter "
        "fields: %s",
        ", ".join(stripped),
    )
    return DegradationSignal(
        degraded=True,
        degraded_reasons=tuple(
            f"adapter:field_stripped:{name}" for name in stripped
        ),
    )


def _merge_degradation_signals(
    *signals: DegradationSignal,
) -> DegradationSignal:
    """Combine degradation from the service envelope and fin-side checks."""
    reasons: list[str] = []
    degraded = False
    for signal in signals:
        if signal.degraded:
            degraded = True
            reasons.extend(signal.degraded_reasons)
    if not degraded:
        return NOT_DEGRADED
    return DegradationSignal(degraded=True, degraded_reasons=tuple(reasons))


def _named_error_code_of_body(body: object) -> str | None:
    """The named error code a decoded ``tools/call`` body carries, if any.

    Every named refusal this adapter reads projects its code the same way
    — a JSON body with a string ``error`` field (``read_source_not_found``
    and friends, the global 综述 refusal) — so the field is read here
    once and no caller has to match a code against prose.
    """
    if not isinstance(body, Mapping):
        return None
    code = body.get("error")
    return code if isinstance(code, str) else None


def _upstream_error_fields(result: dict[str, Any]) -> dict[str, Any]:
    """The cause/stage fields the upstream refusal carried, verbatim.

    Read from the SAME first-text-block body
    :func:`_structured_is_error_code` decoded, so a projected envelope
    keeps the producer's own readiness pair instead of re-deriving one.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return {}
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            body = json.loads(text)
        except ValueError:
            return {}
        if isinstance(body, Mapping):
            fields = {
                key: body[key]
                for key in ("cause", "stage")
                if key in body
            }
            return fields
        return {}
    return {}


def _structured_is_error_code(result: dict[str, Any]) -> str | None:
    """The named code an MCP ``tools/call`` refusal carries, if any.

    Every structured refusal this adapter reads projects its code the
    same way — an ``isError`` result whose FIRST text content block
    decodes to a JSON body with a string ``error`` field (the global 综述
    report-index refusal, the sql 档's producer failure vocabulary) — so
    the field is read here once and no caller matches a code against
    prose. The producer declares its sql tokens exactly this way (spec
    search-mode-business-sql-contract §7.1: an UNTOKENED failure is a
    dead-server reading, and this reader is what keeps the two apart).

    ``None`` means "this refusal names no code": a non-error result, an
    empty / non-leading-text content list, text that will not decode, or
    a body without a string ``error`` field. Only the FIRST text block
    is judged — a body that will not decode is a decisive miss for this
    result, so the scan stops there instead of treating later blocks as
    candidate envelopes.
    """
    if result.get("isError") is not True:
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            body = json.loads(text)
        except ValueError as exc:
            logger.debug(
                "structured error text is not JSON: %s",
                exc,
            )
            return None
        return _named_error_code_of_body(body)
    return None


_STRUCTURED_QUERY_ERROR_CODES = frozenset({
    "malformed_structured_query", "unsupported_structured_schema",
    "unsupported_subject", "unsupported_operation", "unsupported_field",
    "unsupported_operator", "invalid_value", "request_too_large",
    "authorization_denied", "invalid_cursor", "cursor_expired",
    "cursor_revision_mismatch", "read_revision_mismatch", "result_too_large",
    "count_unavailable", "query_timeout", STRUCTURED_SEARCH_UNAVAILABLE_CODE,
    "maintenance_status_unavailable",
})


def _structured_query_error_body_code(body: object) -> str | None:
    if not isinstance(body, Mapping):
        return None
    version = body.get("schema_version")
    if (
        body.get("schema") != "iknow.structured_error"
        or type(version) not in (int, float)
        or version != 1
    ):
        return None
    error = body.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    if (
        not isinstance(code, str)
        or code not in _STRUCTURED_QUERY_ERROR_CODES
        or type(error.get("retryable")) is not bool
    ):
        return None
    return code


def _structured_query_error_code(result: dict[str, Any]) -> str | None:
    """Read only a schema-v1 structured-query refusal from MCP content."""
    if result.get("isError") is not True:
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            body = json.loads(text)
        except ValueError:
            return None
        return _structured_query_error_body_code(body)
    return None


def _is_community_summary_not_ready(result: dict[str, Any]) -> bool:
    """Whether an MCP ``tools/call`` result is the global 综述 report arm's
    typed community-summary not-ready refusal.

    The structured read itself is the shared :func:`_structured_is_error_code`;
    this is the one call that asks whether the named code is the summary
    index's. The code is read off the decoded body, never searched for in
    the message, because a transport fault is free to quote the token in
    its own prose. A refusal so named is a RECORDED job fact about the
    report index — distinct from a transport fault (which carries no such
    field and must stay ``retrieve_unavailable``) and from a legal empty
    hit (which is not an error at all).
    """
    return _structured_is_error_code(result) == COMMUNITY_SUMMARY_INDEX_FAILURE


def _rows_present(result: dict[str, Any]) -> bool:
    """Whether the business envelope carries at least one readable row.

    The degraded-release rule (「降级但有结果就放行」) counts rows as
    results the same way it counts chunks: a sql answer of only rows
    must not collapse into 「降级但没有可用结果」. The sibling is the E1
    row object — presence is judged on its ``rows`` list — and an entry
    that is not a mapping still passes this gate: the release decision
    does not depend on mapping succeeding first, and a sibling whose
    shape fails the validator is named as a contract failure where the
    sibling is consumed below, not silently un-released here.
    """
    rows = database_rows_of(result.get(DATABASE_ROWS_KEY))
    return bool(rows)


def _database_rows_ingress_refusal(sibling: Mapping[str, Any]) -> str | None:
    """The §6 code the evidence validator refuses this row object with,
    or ``None`` when its shape holds.

    WHY the import is local: the validator lives in ``fin.host``, whose
    package chain imports back into ``fin.retrieve.mcp_adapter``, so a
    top-level import here closes a retrieve↔host import cycle. The
    call-in itself is the contract — shape judgment has a single home
    and is never duplicated at this ingress, and the self-seed posture
    (including its never-hash rule) is the home's
    :func:`seed_context_from_rows`, not a copy of it here.
    """
    from fin.host.evidence.evidence_contract import (
        seed_context_from_rows,
        validate_evidence,
    )

    claimed = sibling.get(DATABASE_ROWS_ROWS_FIELD)
    rows = claimed if isinstance(claimed, list) else ()
    context = seed_context_from_rows(database_row_values=rows)
    validation = validate_evidence({DATABASE_ROWS_KEY: sibling}, context)
    if validation.refusal is None:
        return None
    return validation.refusal.code


# ----- adapter -----


_RETRIEVAL_WINDOW_KEYS = (
    "retrieval_fingerprint",
    "generation_id",
    "epoch",
)


def retrieval_window_of(payload: Mapping[str, Any]) -> dict[str, str]:
    """Copy iknow's window strings. A missing or non-string value is absent."""
    window: dict[str, str] = {}
    for key in _RETRIEVAL_WINDOW_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            window[key] = value
    return window


# The §B3 failure family whose WIRE token is the spec's protocol failure:
# a malformed degradation envelope (spec qa-chain-corpus-retrieval §"Result,
# error, readiness, and degradation wire") and a renamed signal field are
# both 「malformed or incompatible response contract」. Each class keeps its
# own internal ``code`` — the ledger and the logs read it to tell the two
# kinds of envelope problem apart — so this map, not a blanket rename, is
# what puts one named token on the wire. Ledger persistence problems are a
# different family (fin's own record, not the producer's envelope) and keep
# their own codes.
DEGRADATION_WIRE_CODES: Final[dict[type[Exception], str]] = {
    DegradedEnvelopeInvalidError: RETRIEVE_PROTOCOL_ERROR_CODE,
    DegradedFieldAliasError: RETRIEVE_PROTOCOL_ERROR_CODE,
}


def _degradation_wire_code(exc_type: type[Exception]) -> str:
    """The wire token for one :class:`DegradationError` subclass.

    MRO-walked so a future subclass of a mapped class inherits its
    family's wire code instead of leaking an unmapped internal token.
    """
    for cls in exc_type.__mro__:
        code = DEGRADATION_WIRE_CODES.get(cls)
        if code is not None:
            return code
    raise ValueError(
        f"{exc_type.__name__} has no wire code in DEGRADATION_WIRE_CODES; "
        "extend the map rather than emitting the internal token"
    )


def _run_with_trace_context(factory: Any) -> Any:
    """Run ``factory()`` on a private loop with the caller's OTel
    context explicitly attached.

    The spec requires the trace context to be PASSED across the
    retrieval boundary rather than relied on to propagate
    (``specs/qa-chain-server-runtime.md`` §"Trace transport and
    spans"). Propagation across a fresh event loop is an accident of
    which thread ``asyncio.run`` happens to execute on, so this
    attaches the captured context itself.

    Why ``attach``/``detach`` rather than ``contextvars.copy_context()``:
    both restore the OTel context, but ``copy_context()`` also copies
    every OTHER contextvar in the caller's stack — including the T11
    ``RunCallContext`` (``fin.host.run_call_supervisor``) — which would
    make this module depend on a host type and would hand a foreign
    run's stop token to a ``retrieve`` that outlives the caller's
    context. Attaching exactly the OpenTelemetry context is the
    narrower and dependency-free choice, and it is the only one of the
    two that expresses intent ("carry the trace, not the rest").

    The context is captured at THIS public entry point, before
    ``asyncio.run``, so the change is API-shape-neutral: the host's
    ``retrieve_tool`` span needs no signature change and this module
    learns nothing about its caller. An empty current context is left
    unattached — attaching an empty ``Context`` is a no-op and
    detaching an unrelated token is worse than doing nothing.

    ``factory`` is a zero-argument callable returning the coroutine,
    so the coroutine is CREATED inside this frame where the context is
    already current rather than in the caller's.
    """
    captured: Context = _otel_get_current()
    if not captured:
        return asyncio.run(factory())

    async def _attached() -> Any:
        token = _otel_attach(captured)
        try:
            return await factory()
        finally:
            _otel_detach(token)

    return asyncio.run(_attached())


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
        map (ADR 0047's wire contract), so an empty map — including
        the ``compile_graph`` auto-build default — drops only
        non-canonical hits, and those drops are counted on
        ``mapping_ledger``: visible, never silent.
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
        timeout_seconds: float | None = None,
        mapping_ledger: MappingFailureLedger | None = None,
        input_validation_ledger: InputValidationLedger | None = None,
        session: MCPStdioSession | None = None,
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
        # Shares the transport-wide budget with the push and index-job
        # ports: a retrieve that has to run BM25 + vector + rerank over the
        # corpus is the same order of work as a push, and a 5s ceiling made
        # this adapter the last MCP call site still reporting a timeout for
        # a request that only needed longer. Leaving it behind would have
        # been the half-fix that is worse than none — the same defect under
        # a different tool name.
        self._timeout: float = float(
            timeout_seconds
            if timeout_seconds is not None
            else default_mcp_timeout_seconds()
        )
        self._mapping_ledger = mapping_ledger
        self._input_validation_ledger = input_validation_ledger
        self._session = session
        self._run_deadline: float | None = None
        self._run_cancel_event: threading.Event | None = None

    # ----- public surface -----

    def with_session(self, session: MCPStdioSession) -> MCPRetrieveAdapter:
        """A copy of this adapter whose transport is ``session``.

        The identity that authorizes a read belongs to the *connection*, not to
        the adapter, and the host builds one adapter per process. Rebinding
        therefore shares this adapter's locator map and both ledgers — which the
        graph injected once and every identity must keep counting into — and
        changes only which child process the call rides. A copy rather than a
        rebuild is what keeps the rebind free of cross-identity ledger drift.
        """

        rebound = copy.copy(self)
        rebound._session = session
        return rebound

    def with_run_control(
        self,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> MCPRetrieveAdapter:
        """A per-run copy bound to trusted cancellation and deadline state.

        The public retrieve and source-read contracts stay unchanged; the host
        composes this copy for one run, and every transport call made through it
        carries the same control state.
        """

        rebound = copy.copy(self)
        rebound._run_deadline = deadline
        rebound._run_cancel_event = cancel_event
        return rebound

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
        search_mode: str | None = None,
        structured_request: Mapping[str, Any] | None = None,
        sql_window_start: str | None = None,
        sql_window_end: str | None = None,
        filters: Mapping[str, Any] | None = None,
        document_scope: DocumentScope | None = None,
    ) -> dict[str, Any]:
        """Sync retrieve: returns ``{"chunks": [...]}`` or
        ``{"error": "retrieve_unavailable"}``.

        Unknown filters short-circuit to the miss shape BEFORE the
        MCP round-trip; an empty query raises ``EmptyQueryError``
        there instead (T21 — invalid input is not a miss, and the
        typed error travels out of this seam rather than being
        flattened into an envelope the model reads as "no hits").
        A ``sql`` request whose expiry window is missing or malformed
        raises ``InvalidBusinessQueryError`` there too — the same exit
        the producer names before a connection exists, and it travels
        out of this seam for the host's input-error handling rather
        than starting a round trip fin cannot bind.
        Any MCP transport / protocol failure surfaces as
        ``{"error": "retrieve_unavailable"}`` (no BM25
        fallback). A 档 that is legal but not open on this port
        surfaces as its named ``{"error": <TIER_UNAVAILABLE_CODE>}``
        — the request never starts, so no silent B dressed as C.
        """
        try:
            return _run_with_trace_context(
                lambda: self.aretrieve(
                    query,
                    document_id=document_id,
                    cik=cik,
                    form=form,
                    ticker=ticker,
                    filing_date=filing_date,
                    limit=limit,
                    search_mode=search_mode,
                    structured_request=structured_request,
                    sql_window_start=sql_window_start,
                    sql_window_end=sql_window_end,
                    filters=filters,
                    document_scope=document_scope,
                )
            )
        except RetrieveUnavailableError as exc:
            # Typed EXIT — the product shape is the error dict. A
            # degraded failure carries its §B3 reasons along (T20).
            return exc.error_dict()
        except TierUnavailableError as exc:
            # Typed EXIT — the routing-time named 未开/未接 code,
            # translated at the same sync seam as the transport EXIT.
            # Distinct from the branch above: a retry answers the same
            # way, so this is NOT the 「retrieve_unavailable, try
            # later」 shape.
            return {"error": exc.code}
        except DegradationError as exc:
            # The service answered with an unreadable / renamed
            # degradation signal (§B3). Named EXIT rather than a
            # coerced result: neither「未降级」nor an empty hit list
            # would be a true reading of that response. The WIRE token
            # is the spec's protocol failure, not the exception's own
            # internal code.
            return {"error": _degradation_wire_code(type(exc))}

    def read_slice(
        self,
        source_ref: Mapping[str, Any],
        position: int = 0,
    ) -> dict[str, Any]:
        """Read one bounded slice of one company frozen unit.

        The company leg of :class:`fin.retrieve.source_read.SourceReader`
        (ADR 0087 / ADR 0088): exactly one ``read_source`` round trip
        resolves the locator's unit at ``position`` and answers
        ``{source_ref, text, next_position}`` — the request's own typed
        ``source_ref`` echoed back, never the backend's serialization —
        or one typed failure dict. It never raises at the model and
        never carries text on a failure.
        """
        located = validate_company_source_ref(source_ref)
        if not isinstance(located, tuple):
            # EXIT: a malformed or oversized identity is a shape failure
            # fin can name without asking the backend anything.
            return located
        document_id, version, unit = located
        if (
            self._known_doc_ids is not None
            and document_id not in self._known_doc_ids
        ):
            # EXIT: the adapter's known set is the set ingested on MCP,
            # so a document outside it is the named miss with no round
            # trip — the same short-circuit ``retrieve`` applies.
            return source_error(UNKNOWN_LOCATOR)
        bad_position = position_error(position)
        if bad_position is not None:
            # EXIT: a cursor fin cannot express on the wire is invalid
            # input named here; a beyond-end position stays the
            # backend's judgment because only it knows the unit length.
            return bad_position

        try:
            response = self._call_read_source(
                {
                    "source_ref": serialize_source_ref(
                        UnitLocator(document_id, version, unit)
                    ),
                    "position": position,
                }
            )
        except (
            MCPStdioCancelledError,
            MCPStdioDeadlineExceededError,
        ):
            raise
        except (
            RetrieveUnavailableError,
            OSError,
            asyncio.TimeoutError,
            TimeoutError,
        ) as exc:
            # EXIT: the machine could not answer at all. Named as an
            # outage — not as a locator that does not exist, and with no
            # text to leak.
            logger.debug("read_source transport failed: %s", exc)
            return source_error(SOURCE_UNAVAILABLE)
        return _slice_of_read_response(response, source_ref, position)

    def _call_read_source(self, arguments: Mapping[str, Any]) -> Any:
        """One ``read_source`` tools/call round trip for the read legs.

        Raises :class:`RetrieveUnavailableError` for a transport failure;
        :meth:`read_slice` names that as the ``source_unavailable``
        outage — the machine refusing to answer is a different failure
        from a locator that does not exist.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": _TOOL_REQUEST_ID,
            "method": "tools/call",
            "params": {"name": "read_source", "arguments": dict(arguments)},
        }
        try:
            return _run_with_trace_context(lambda: self._call_mcp(payload))
        except (MCPStdioCancelledError, MCPStdioDeadlineExceededError):
            raise
        except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
            raise RetrieveUnavailableError(
                f"MCP transport failed: {exc!r}"
            ) from exc

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
        search_mode: str | None = None,
        structured_request: Mapping[str, Any] | None = None,
        sql_window_start: str | None = None,
        sql_window_end: str | None = None,
        filters: Mapping[str, Any] | None = None,
        document_scope: DocumentScope | None = None,
    ) -> dict[str, Any]:
        """Async retrieve: raises ``RetrieveUnavailableError``
        on transport failure; ``retrieve`` translates that to
        the structured error dict.

        A 档 token outside the closed set raises
        ``InvalidSearchModeError`` and an unopened one raises
        ``TierUnavailableError`` — both BEFORE any round trip, so an
        illegal or unopened 档 never starts a silent default-B
        retrieve. A legacy ``sql`` request without a bindable window
        raises ``InvalidBusinessQueryError`` at the same pre-transport
        seam — the producer refuses the same name before it connects,
        and a window fin invented would be a query the operator never
        asked. A structured request instead requires the host-bound
        ``sql`` mode and refuses legacy outer filters.

        Tests subclass and override ``_call_mcp`` for scripted
        transport. The default transport launches a subprocess.
        """
        # Empty / whitespace query: invalid input (T21 / §B2 第 5 条),
        # named and counted before any round trip. The other
        # short-circuits below stay miss shapes — they are legal empty
        # results, and the split is the whole point of the ticket.
        validate_query(query, ledger=self.input_validation_failures)
        # The generic metadata filter is validated BEFORE any I/O — no
        # lock, no process, no transport (spec qa-chain-corpus-retrieval
        # §"Retrieve request and metadata filters"). An out-of-contract
        # map is the canonical refusal, not an empty hit list.
        try:
            effective_filters = normalized_metadata_filters(filters)
        except InvalidFilterError:
            return invalid_filter()
        # 档 token: validated against the closed set before the
        # round-trip like every other invalid input (the raise is the
        # named EXIT; ``retrieve`` maps the 未开 codes to error dicts).
        effective_search_mode = resolve_search_mode(search_mode)
        effective_document_scope, scope_refusal = prepare_document_scope(
            document_scope, document_id, effective_search_mode
        )
        if scope_refusal is not None:
            return {"error": scope_refusal}
        refusal = _structured_query_refusal_code(
            structured_request,
            search_mode=effective_search_mode,
            has_outer_filters=any(
                value is not None
                for value in (
                    document_id,
                    cik,
                    form,
                    ticker,
                    filing_date,
                    sql_window_start,
                    sql_window_end,
                )
            ),
            has_generic_filters=effective_filters is not None,
            has_scope_locator=document_id is not None,
            has_document_scope=effective_document_scope is not None,
        )
        if refusal is not None:
            return {"error": refusal}
        # Structured requests use their own typed predicates and never
        # carry or require the legacy business expiry window.
        effective_window = (
            None
            if structured_request is not None
            else resolve_sql_window(
                effective_search_mode, sql_window_start, sql_window_end
            )
        )
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
        # ``limit``, and the optional ``filters`` block.
        #
        # Plan D0 I-T1: the wire shape aligns to the iknow-memory
        # ``z.strictObject`` schema — ``limit`` / ``filters`` are the
        # only keys the server keeps. The legacy ``top_k`` /
        # ``filter`` / top-level ``document_id`` would be rejected by
        # strict mode, which is the symptom that exposed gap 1.
        arguments: dict[str, Any] = {
            "query": query,
            "limit": effective_limit,
        }
        if effective_document_scope is not None:
            arguments["document_scope"] = [
                {
                    "document_id": entry.document_id,
                    "version": entry.version,
                }
                for entry in effective_document_scope
            ]
        # Only filters the caller actually passed. Blank values
        # already short-circuited above, so a non-``None`` value
        # here is a real filter; a ``None`` means "unset" and must
        # NOT be sent as an explicit ``null`` the server would try
        # to match on.
        wire_filters: dict[str, Any] = {}
        if document_id is not None:
            wire_filters["document_id"] = document_id
        for key, value in (
            ("cik", cik),
            ("form", form),
            ("ticker", ticker),
            ("filing_date", filing_date),
        ):
            if value is not None:
                wire_filters[key] = value
        # The 档 rides the same record: iknow reads ``filters.search_mode``
        # over the closed set in :mod:`fin.retrieve.tiers` and defaults to
        # graph when the key is missing — so an unset mode is NOT sent as
        # null.
        if effective_search_mode is not None:
            wire_filters[SEARCH_MODE_FILTER_KEY] = effective_search_mode
        # The sql 档's bound window rides beside its 档 token (validated
        # above — reaching here means the pair is bindable). Document 档
        # resolve to ``None`` and the keys are never sent: the wire the
        # document path sees stays byte-identical.
        if effective_window is not None:
            wire_filters[SQL_WINDOW_START_FILTER_KEY] = effective_window[0]
            wire_filters[SQL_WINDOW_END_FILTER_KEY] = effective_window[1]
        if structured_request is not None:
            wire_filters["structured_query"] = dict(structured_request)
        # Both legacy financial arguments and generic metadata filters
        # use the upstream exact-match vocabulary (``metadata.<key>``),
        # so the upstream resolver applies them conjunctively.
        for key in ("cik", "form", "ticker", "filing_date"):
            if key in wire_filters:
                wire_filters[f"metadata.{key}"] = wire_filters.pop(key)
        if effective_filters is not None:
            wire_filters.update(effective_filters)
        if wire_filters:
            arguments["filters"] = wire_filters

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
        except RetrieveBudgetExhaustedError:
            # The one busy minting point travels out untouched: it is
            # already the typed ``not_ready(busy, stage)`` error, and
            # re-wrapping it as a transport failure would be the recode
            # the spec forbids.
            raise
        except MCPStdioCancelledError:
            # The owning run cancelled the exchange: a control signal,
            # not a transport failure, so it is never recoded.
            raise
        except MCPStdioDeadlineExceededError as exc:
            # The retrieval budget expired. ``cause=busy`` is minted HERE
            # and only here (spec §"Result, error, readiness, and
            # degradation wire": the backend owns no queue and no
            # budget): queue saturation or total-budget exhaustion with
            # the stage the budget expired in.
            raise RetrieveBudgetExhaustedError(
                f"MCP session budget expired in stage {exc.stage!r}: {exc}",
                stage=exc.stage,
            ) from exc
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

        # Result shape: accept four envelope variants. The canonical wire
        # shape is ``{chunks: [...]}`` per spec §B3 / §B4; the legacy
        # ``{hits: [...]}`` is retained only as a forward-compat
        # fallback for non-yet-upgraded servers (Plan D0 I-T2):
        #
        # 1. ``{"result": {"chunks": [...]}}`` — canonical JSON-RPC
        #    success envelope with the raw tool output as ``result``.
        # 2. ``{"chunks": [...]}`` — server returns the raw tool
        #    output as the JSON-RPC result (some MCP servers
        #    unwrap automatically).
        # 3. ``{"result": {"content": [{"type": "text",
        #    "text": "<json with chunks>"}]}}`` — the canonical
        #    MCP ``tools/call`` envelope with content blocks
        #    carrying the business envelope.
        # 4. ``{"result": {"hits": [...]}}`` / ``{"hits": [...]}``
        #    — legacy fallback for non-yet-upgraded servers; the
        #    ``hits`` path was the v1 contract the iknow-memory side
        #    has since replaced with ``chunks``.
        if not isinstance(response, dict):
            raise RetrieveUnavailableError(
                f"MCP returned non-dict response: {response!r}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            # Variant 2: ``{"chunks": [...]}`` or ``{"hits": [...]}``
            # at top level.
            top_level_chunks = response.get("chunks")
            top_level_hits = response.get("hits")
            if isinstance(top_level_chunks, list):
                result = response
            elif isinstance(top_level_hits, list):
                result = response
            else:
                raise _unavailable(
                    f"MCP returned non-dict result: {result!r}",
                    envelope_signal,
                )
        # A named refusal on the structured field is its own fact, read
        # once here and dispatched by name — never re-typed by prose.
        #   * the global 综述 report-index miss is a RECORDED job fact
        #     (empty chunks plus the flag) so the host names the summary
        #     index for the operator;
        #   * the sql 档's zero-row exit is the sibling RECORDED job
        #     fact: the permitted query RAN and matched no row, which is
        #     「数据库没这行」— not 「语料没东西」 and not an outage
        #     (spec §7 keeps the three advice sets apart);
        #   * the rest of the declared sql failure vocabulary (not
        #     provisioned / denied / timeout / unverified / unbound
        #     request) is a typed EXIT dict under its OWN name — retry
        #     advice for a denied read differs from an outage's, and an
        #     untokened prose error matches no set here and stays the
        #     ``retrieve_unavailable`` reading it is.
        if structured_request is not None:
            structured_error = _structured_query_error_code(result)
            if structured_error is not None:
                projected = _project_upstream_failure({"error": structured_error})
                return projected or {"error": structured_error}
            if result.get("isError") is True:
                return {"error": STRUCTURED_SEARCH_UNAVAILABLE_CODE}
            # A legacy SQL refusal cannot answer a structured query. It is
            # evidence that the structured capability is absent, not a
            # structured no-match or permission result.
            if _structured_is_error_code(result) is not None:
                return {"error": STRUCTURED_SEARCH_UNAVAILABLE_CODE}
            named_refusal = None
        else:
            named_refusal = _structured_is_error_code(result)
        if named_refusal == COMMUNITY_SUMMARY_INDEX_FAILURE:
            return {
                "chunks": [],
                RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
                **retrieval_window_of(result),
            }
        if named_refusal == SQL_NO_MATCHING_ROWS:
            return {
                "chunks": [],
                DATABASE_ROWS_KEY: new_database_rows([]),
                RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS: True,
                **retrieval_window_of(result),
            }
        if named_refusal in SQL_PRODUCER_FAILURE_CODES:
            return {"error": named_refusal}
        # The lexical target's own failure vocabulary projects to fin's
        # named envelopes (spec §"Result, error, readiness, and
        # degradation wire"): each token keeps its own meaning —
        # authorization, transport, protocol, filter-mode — and the
        # proven index fact is the exact ``not_ready`` passthrough. A
        # body naming no upstream token falls through unchanged.
        if named_refusal is not None:
            projected = _project_upstream_failure(
                {"error": named_refusal, **_upstream_error_fields(result)}
            )
            if projected is not None:
                return projected
        # Variant 3: content-block envelope. Unwrap only when the
        # block carries neither the canonical ``chunks`` key nor the
        # legacy ``hits`` key — otherwise the block's ``content`` key
        # is unrelated (degradation diagnostics, etc.).
        if (
            "chunks" not in result
            and "hits" not in result
            and "content" in result
        ):
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
        if effective_document_scope is not None and (
            DATABASE_ROWS_KEY in result
            or structured_result_of(result) is not None
        ):
            return {"error": UNSUPPORTED_SOURCE_SCOPE_CODE}
        if (
            structured_request is not None
            and structured_result_of(result) is None
        ):
            return {"error": STRUCTURED_SEARCH_UNAVAILABLE_CODE}
        if structured_request is not None and DATABASE_ROWS_KEY in result:
            return {"error": STRUCTURED_SEARCH_UNAVAILABLE_CODE}
        signal = _degradation_of_response(
            response,
            business_result=result,
        )
        signal = _merge_degradation_signals(
            signal,
            _signal_for_stripped_adapter_fields(
                arguments,
                result.get("applied_arguments"),
            ),
        )

        # Plan D0 I-T2: the canonical envelope is ``result.chunks``;
        # ``result.hits`` is retained only as a forward-compat
        # fallback. When both are present, ``chunks`` wins.
        canonical_chunks = result.get("chunks")
        legacy_hits = result.get("hits")
        raw_structured_result = structured_result_of(result)
        if isinstance(canonical_chunks, list):
            # Map canonical chunks: each carries ``source_ref`` as
            # the B4 fin locator serialization. Unmappable chunks
            # are dropped AND counted (T17 / SC22: drop must not be
            # silent). ``locator_map`` is the legacy hits-only seam;
            # the chunks path does NOT consult it.
            chunks = map_chunks_to_fin_chunks(
                canonical_chunks,
                limit=effective_limit,
                ledger=self.mapping_failures,
            )
        elif raw_structured_result is not None:
            # Structured results may have no lexical chunks. Keep the
            # normal retrieve envelope shape without invoking legacy hits.
            chunks = ()
        elif isinstance(legacy_hits, list):
            # Map iknow ids → fin locators; drop unmapped, and count
            # every drop on the ledger (T17 / SC22: the drop must
            # not be silent). The cap is enforced AFTER mapping so
            # the returned list is the locatable subset, not a slice
            # of unmapped ids.
            chunks = map_hits_to_chunks(
                legacy_hits,
                locator_map=self._locator_map,
                limit=effective_limit,
                ledger=self.mapping_failures,
            )
        else:
            raise _unavailable(
                "MCP returned neither chunks nor a list of hits: "
                f"chunks={canonical_chunks!r}, hits={legacy_hits!r}",
                signal,
            )
        if effective_document_scope is not None:
            allowed_sources = {
                (entry.document_id, entry.version)
                for entry in effective_document_scope
            }
            chunks = tuple(
                chunk
                for chunk in chunks
                if (
                    chunk.source_ref.document_id,
                    chunk.source_ref.version,
                )
                in allowed_sources
            )
        if (
            signal.degraded
            and not chunks
            and not _rows_present(result)
            and raw_structured_result is None
        ):
            raise _unavailable(
                "MCP returned a degraded response without usable chunks",
                signal,
            )
        # A degraded response with results is 放行 (§B2 第 1 条) — but it
        # travels with its flag, so the host can record the reasons and
        # tell the user this answer is not a complete one.
        envelope: dict[str, Any] = {
            "chunks": list(chunks),
            "distribution": distribution_of(chunks),
            **signal.as_envelope_fields(),
        }
        # The row sibling (spec §8.0, E1 envelope contract §4.4):
        # ``database_rows`` rides BESIDE ``chunks``, never inside it — a
        # row is not a passage and the Chunk/Locator types stay untouched.
        # The value is the E1 row OBJECT ``{schema, rows, result_count}``.
        # A PRESENT row object goes through the structural evidence
        # validator before anything is consumed: a §3/§6 violation is
        # the named contract failure on the wire, never the silent
        # absent-sibling and never a half-read result (the closed-set
        # judgment lives in the validator, not duplicated here). The
        # obsolete bare-array shape is not a row object, so it keeps
        # today's frozen reading: consumed as if the key were absent.
        # The key is ABSENT whenever the wire did not carry it (every
        # document-档 envelope stays byte-identical). A conforming
        # sibling passes through with the producer's own ``schema`` /
        # ``result_count`` intact — re-authoring the producer's numbers
        # here would hide exactly the drift the validator must be able
        # to see.
        raw_rows = result.get(DATABASE_ROWS_KEY)
        if isinstance(raw_rows, Mapping):
            refused = _database_rows_ingress_refusal(raw_rows)
            if refused is not None:
                return {"error": refused}
            envelope[DATABASE_ROWS_KEY] = dict(raw_rows)
        if raw_structured_result is not None:
            envelope[STRUCTURED_RESULT_KEY] = dict(raw_structured_result)
        envelope.update(retrieval_window_of(result))
        return envelope

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
        5. Spends ``self._timeout`` as ONE budget over the lock wait,
           the startup/handshake, and the response read.
        6. Returns the parsed business response dict.

        Tests subclass and override this method with a
        scripted response / exception. The override is the
        documented seam — modifying the default transport
        requires the override to honor the same contract
        (return dict on success; raise ``OSError`` /
        ``TimeoutError`` on failure).
        """
        session = getattr(self, "_session", None)
        deadline = getattr(self, "_run_deadline", None)
        cancel_event = getattr(self, "_run_cancel_event", None)
        if session is not None or deadline is not None or cancel_event is not None:
            owns_session = session is None
            if session is None:
                command = getattr(self, "_command", None)
                if not command:
                    raise OSError("no MCP command configured")
                session = MCPStdioSession(
                    command,
                    timeout_seconds=self._timeout,
                )
            try:
                return await asyncio.to_thread(
                    session.call,
                    payload,
                    self._timeout,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            except MCPStdioProtocolError as exc:
                raise RetrieveUnavailableError(
                    f"MCP session protocol failed: {exc}"
                ) from exc
            finally:
                if owns_session:
                    await asyncio.to_thread(session.close)

        if not self._command:
            # ``None`` (unset) and ``[]`` (configured to nothing)
            # are the same no-transport condition: there is no
            # program to exec, so spawning would raise TypeError
            # instead of the typed EXIT the caller contracts on.
            raise OSError("no MCP command configured")
        proc = await _launch_mcp_process(self._command)
        wire_messages = _build_mcp_wire_messages(payload)
        stdout = await _exchange_mcp_messages(
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


# ----- bounded read_source slice decoding (read_slice) -----


_READ_WIRE_INVALID = object()


def _read_slice_envelope_of_result(result: Any) -> Any:
    """The inner business envelope of one ``read_source`` result.

    Strict by design: the bounded wire has exactly one shape (a text
    block whose JSON is the envelope), and a flat string or undecodable
    block is a wire breach the caller must name rather than salvage into
    a body.
    """
    if not isinstance(result, Mapping) or "content" not in result:
        return _READ_WIRE_INVALID
    content = result["content"]
    if not isinstance(content, list) or not content:
        return _READ_WIRE_INVALID
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            return _READ_WIRE_INVALID
        try:
            inner = json.loads(text)
        except ValueError:
            return _READ_WIRE_INVALID
        return inner if isinstance(inner, Mapping) else _READ_WIRE_INVALID
    return _READ_WIRE_INVALID


# The closed map of the iknow-memory ``read_source`` refusal codes onto
# fin's typed read failures (spec thread-attachment-source-read SC9).
_BACKEND_SLICE_ERRORS: dict[str, str] = {
    "read_source_not_found": UNKNOWN_LOCATOR,
    "read_source_invalidated": UNKNOWN_LOCATOR,
    "read_source_invalid_position": INVALID_POSITION,
    "read_source_invalid_locator": INVALID_LOCATOR,
    "read_source_unavailable": SOURCE_UNAVAILABLE,
}


def _slice_error_of_backend_body(
    code: str, body: Mapping[str, Any], position: int
) -> dict[str, Any]:
    """Map one named backend refusal onto fin's typed read failure.

    "Row missing" (``read_source_not_found``) and "validity window
    closed" (``read_source_invalidated``) are one answer for the model —
    a locator it cannot read — so ``valid_until`` stays on the backend
    rather than inviting a retry it cannot act on.
    """
    fin_code = _BACKEND_SLICE_ERRORS.get(code)
    if fin_code is None:
        # EXIT: an unrecognized code is a wire fin was not taught to
        # read; folding it into a known miss would hide the drift.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="unrecognized_backend_code"
        )
    if fin_code == INVALID_POSITION:
        # The backend knows the unit length, fin does not: echo the
        # requested cursor and carry ``code_points`` when it reported one.
        fields: dict[str, Any] = {"position": position}
        code_points = body.get("code_points")
        if isinstance(code_points, int) and not isinstance(code_points, bool):
            fields["code_points"] = code_points
        return source_error(INVALID_POSITION, **fields)
    if fin_code == INVALID_LOCATOR:
        reason = body.get("reason")
        # A reason passes through only as the plain token the backend
        # names it with; anything else keeps the bare code so the failure
        # shape cannot vary with backend prose.
        return (
            source_error(INVALID_LOCATOR, reason=reason)
            if isinstance(reason, str)
            else source_error(INVALID_LOCATOR)
        )
    # EXIT: the miss and the outage answers carry no text.
    return source_error(fin_code)


def _slice_of_read_response(
    response: Any, source_ref: Mapping[str, Any], position: int
) -> dict[str, Any]:
    """Decode one ``read_source`` response into a slice or a typed failure.

    A success envelope must carry ``content`` as a string, a
    ``next_position`` that is ``None`` or a strictly advancing integer,
    and a complete serialized result inside the shared byte cap; any
    deviation is the backend breaking its own promise, which fin names
    ``source_backend_contract_error`` rather than pass through.
    """
    if not isinstance(response, dict):
        # EXIT: a non-object answer carries nothing to read at all.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="response_not_an_object"
        )
    if "error" in response:
        # EXIT: a JSON-RPC level error is the machine refusing to answer,
        # an outage shape, not a locator shape.
        return source_error(SOURCE_UNAVAILABLE)
    envelope = _read_slice_envelope_of_result(response.get("result"))
    if envelope is _READ_WIRE_INVALID:
        # EXIT: not a JSON text block fin could even attempt to read.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="envelope_undecodable"
        )
    named = _named_error_code_of_body(envelope)
    if named is not None:
        # EXIT: a named backend refusal maps to its own typed failure.
        return _slice_error_of_backend_body(named, envelope, position)

    content = envelope.get("content")
    if not isinstance(content, str):
        # EXIT: an unrecognised body with no known error code.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="content_not_text"
        )
    if len(content) > SOURCE_READ_MAX_TEXT_CODE_POINTS:
        # EXIT: the backend promised a bounded chunk and broke that
        # promise; handing it over would move the cap to the model.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="content_over_code_point_cap"
        )
    if "next_position" not in envelope:
        # EXIT: without a cursor a consumer cannot tell a complete read
        # from a truncated one, so the bound is unverifiable.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="next_position_absent"
        )
    next_position = envelope["next_position"]
    if next_position is not None and (
        not isinstance(next_position, int) or isinstance(next_position, bool)
    ):
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="next_position_not_an_integer"
        )
    if next_position is not None and next_position <= position:
        # EXIT: ``== position`` is a contract error per the spec: a
        # non-advancing cursor can never terminate a walk.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="non_advancing_cursor"
        )

    result: dict[str, Any] = {
        # The request identity is echoed, never the serialised string: a
        # consumer must not re-derive which unit the text came from.
        "source_ref": dict(source_ref),
        "text": content,
        "next_position": next_position,
    }
    if serialized_result_bytes(result) > SOURCE_READ_MAX_RESULT_BYTES:
        # EXIT: text that fits its own cap but not the complete
        # envelope's byte cap is still a broken backend promise.
        return source_error(
            SOURCE_BACKEND_CONTRACT_ERROR, reason="result_over_byte_cap"
        )
    return result
