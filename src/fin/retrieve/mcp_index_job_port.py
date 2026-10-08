"""MCP stdio transport for the index-job seam.

Calls the iknow-memory MCP tools ``submit_index_job`` and
``poll_index_job`` over JSON-RPC 2.0 (the wire contract is
``specs/mcp-tool-surface.md`` §Index-job tool contract). The arguments
are exactly the pair ``{source_ref, content_identity}`` for submit and
``{job_id}`` for poll — fin sends the identity it read back from the
push response and never recomputes one.

Failure translation
-------------------

* ``isError: true`` whose first text block decodes to the JSON body
  ``{"error": <literal>, "message": <prose>}`` → the typed
  :class:`~fin.retrieve.index_job_port.IndexJobError` named by the
  ``error`` field (read off the decoded body, never prose-matched —
  the same discipline as ``mcp_adapter._named_error_code_of_body``).
* Transport failure (spawn, timeout, exit), a JSON-RPC error frame, an
  unparseable refusal, or an off-contract success body →
  :class:`~fin.retrieve.index_job_port.IndexJobUnavailableError`:
  an answer fin cannot trust says nothing about the job, so it is
  reported as "cannot reach a trustworthy service", never coerced into
  a record.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Mapping, Sequence

from fin.host.compile import DEFAULT_MCP_COMMAND
from fin.retrieve.index_job_port import (
    INDEX_JOB_STATUSES,
    LOCATOR_CEILING_CODEPOINTS,
    CommunityIndexState,
    IndexJobError,
    IndexJobInvalidLocatorError,
    IndexJobPoll,
    IndexJobRecord,
    IndexJobSubmission,
    IndexJobUnavailableError,
    index_job_error_for_code,
)
from fin.retrieve.mcp_session import (
    MCPStdioProtocolError,
    MCPStdioSession,
    default_mcp_timeout_seconds,
    build_handshake_wire,
    build_tools_call_frame,
    parse_jsonrpc_response_lines,
    validate_initialize_response,
)

_MCP_CLIENT_NAME = "fin-index-job"


def _is_valid_locator(value: Any) -> bool:
    """One locator string inside the shared ceiling; the service names
    an over-ceiling value ``index_job_invalid_locator`` without
    echoing it, so fin refuses before the round trip and never sends
    one either."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= LOCATOR_CEILING_CODEPOINTS
    )


class MCPIndexJobPort:
    """stdio JSON-RPC client for the iknow-memory index-job tools.

    Parameters
    ----------
    command
        The MCP server launch command. ``None`` / empty with no session
        means there is no transport: every call answers
        :class:`IndexJobUnavailableError` rather than raising raw
        ``TypeError``.
    timeout_seconds
        Per-call timeout on the JSON-RPC exchange.
    session
        A shared :class:`~fin.retrieve.mcp_session.MCPStdioSession`;
        when present every call rides it via ``session.call`` (the same
        injection seam :class:`~fin.retrieve.mcp_push_port.MCPPushPort`
        uses), so several tools can share one server process.
    """

    def __init__(
        self,
        command: Sequence[str] | None = DEFAULT_MCP_COMMAND,
        *,
        timeout_seconds: float | None = None,
        session: MCPStdioSession | None = None,
    ) -> None:
        self._command: list[str] | None = list(command) if command else None
        # Shares the push port's budget: `submit_index_job` hands real work
        # to the same MCP server (the graph re-index), so a 5s ceiling fails
        # it for the same reason it failed every push — measured, not
        # assumed. `None` resolves the env-aware default.
        self._timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else default_mcp_timeout_seconds()
        )
        self._session = session

    # ----- IndexJobPort surface -----

    def submit_index_job(
        self, source_ref: str, content_identity: str
    ) -> IndexJobSubmission:
        """Submit (or join) the durable job for the pair and return the
        persisted row plus the service's ``reused`` flag."""
        if not _is_valid_locator(source_ref) or not _is_valid_locator(
            content_identity
        ):
            raise IndexJobInvalidLocatorError(
                "submit_index_job requires non-empty source_ref and "
                "content_identity within the locator ceiling"
            )
        body = self._call_tool(
            "submit_index_job",
            {
                "source_ref": source_ref,
                "content_identity": content_identity,
            },
        )
        reused = body.get("reused")
        if not isinstance(reused, bool):
            raise IndexJobUnavailableError(
                "submit_index_job response carries no boolean reused flag"
            )
        return IndexJobSubmission(
            record=_record_from_body(body, where="submit_index_job"),
            reused=reused,
        )

    def poll_index_job(self, job_id: str) -> IndexJobPoll:
        """Read the persisted job state; a pure read — nothing here
        re-queues or mutates anything server-side."""
        if not _is_valid_locator(job_id):
            raise IndexJobInvalidLocatorError(
                "poll_index_job requires a non-empty job_id within the "
                "locator ceiling"
            )
        body = self._call_tool(
            "poll_index_job", {"job_id": job_id}
        )
        return IndexJobPoll(
            record=_record_from_body(body, where="poll_index_job"),
            community_index_state=_community_state_from_body(body),
        )

    def _spawn_and_call(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """One-shot stdio exchange: spawn, handshake, call, read two
        responses. Raises ``OSError`` / ``subprocess.TimeoutExpired``
        on any transport failure, which the caller translates.

        The notification has no response, so the two response lines
        must be initialize first, then tools/call.
        """
        if not self._command:
            raise OSError("no MCP command configured")
        process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(
                build_handshake_wire(payload, client_name=_MCP_CLIENT_NAME),
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise OSError(f"MCP index-job call timed out: {exc}") from exc
        if process.returncode != 0:
            raise OSError(
                f"MCP server exited {process.returncode}: "
                f"{stderr.decode(errors='replace')!r}"
            )
        try:
            responses = parse_jsonrpc_response_lines(stdout)
        except MCPStdioProtocolError as exc:
            raise IndexJobUnavailableError(
                "malformed JSON-RPC response from the MCP server: "
                f"{exc.__cause__}"
            ) from exc
        if len(responses) < 2:
            raise IndexJobUnavailableError(
                "MCP handshake or tools/call response is missing"
            )
        try:
            validate_initialize_response(responses[0])
        except MCPStdioProtocolError as exc:
            raise IndexJobUnavailableError(str(exc)) from exc
        return responses[1]

    # ----- transport -----

    def _call_tool(
        self, name: str, arguments: Mapping[str, str]
    ) -> dict[str, Any]:
        """Run one tools/call and return the decoded business JSON body."""
        payload = build_tools_call_frame(name, arguments)
        try:
            if self._session is not None:
                response = self._session.call(payload, self._timeout)
            else:
                response = self._spawn_and_call(payload)
        except (OSError, subprocess.SubprocessError) as exc:
            # ``MCPStdioTransportError`` is an ``OSError`` subclass; a
            # session timeout, a dead process and a failed spawn all
            # mean the same thing here: no trustworthy answer.
            raise IndexJobUnavailableError(
                f"index-job call {name!r} could not reach the service: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except MCPStdioProtocolError as exc:
            raise IndexJobUnavailableError(
                f"index-job call {name!r} got an off-contract frame: {exc}"
            ) from exc
        return _business_body(response, payload)


def _first_text_block(result: Mapping[str, Any]) -> str | None:
    """The JSON text of the first text content block, if any."""
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            return str(block["text"])
    return None


def _decode_refusal(text: str) -> IndexJobError:
    """The typed failure an ``isError`` body names, read off its JSON
    ``error`` field — never prose-matched, and an undecodable refusal
    degrades to unavailable rather than a guessed code."""
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise IndexJobUnavailableError(
            f"index-job refusal text is not JSON: {exc}"
        ) from exc
    if not isinstance(body, Mapping):
        raise IndexJobUnavailableError(
            "index-job refusal body is not a JSON object"
        )
    code = body.get("error")
    if not isinstance(code, str):
        raise IndexJobUnavailableError(
            "index-job refusal body carries no string error field"
        )
    message = body.get("message")
    active_job_id = body.get("active_job_id")
    return index_job_error_for_code(
        code,
        message if isinstance(message, str) else code,
        active_job_id=active_job_id
        if isinstance(active_job_id, str)
        else None,
    )


def _business_body(
    response: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate the JSON-RPC envelope and decode the tool's JSON body.

    Accepted shapes mirror the push adapter: the canonical MCP
    ``tools/call`` content-block envelope, or the server unwrapping the
    business object into ``result`` directly. A JSON-RPC ``error``
    frame is a transport-level failure, not a named refusal: the named
    refusals ride ``isError`` results with a decodable body.
    """
    if not isinstance(response, Mapping):
        raise IndexJobUnavailableError(
            f"index-job response is {type(response).__name__}, expected "
            "an object"
        )
    if response.get("id") != payload.get("id"):
        raise IndexJobUnavailableError(
            "index-job tools/call response id does not match the request"
        )
    if "error" in response:
        raise IndexJobUnavailableError(
            f"index-job call returned a JSON-RPC error: "
            f"{response['error']!r}"
        )
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise IndexJobUnavailableError(
            "index-job response carries no result object"
        )
    if result.get("isError") is True:
        text = _first_text_block(result)
        if text is None:
            raise IndexJobUnavailableError(
                "index-job refusal carries no text content block"
            )
        raise _decode_refusal(text)
    text = _first_text_block(result)
    if text is not None:
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise IndexJobUnavailableError(
                f"index-job response text is not JSON: {exc}"
            ) from exc
    else:
        # Unwrapped variant: the business object sits in ``result``.
        body = dict(result)
    if not isinstance(body, Mapping):
        raise IndexJobUnavailableError(
            "index-job response body is not a JSON object"
        )
    return dict(body)


def _text_or_none(body: Mapping[str, Any], name: str, *, where: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise IndexJobUnavailableError(
            f"{where} response field {name!r} is {value!r}, expected a "
            "non-empty string or null"
        )
    return value


def _record_from_body(body: Mapping[str, Any], *, where: str) -> IndexJobRecord:
    """One persisted job row, validated field by field.

    Every required field is checked before it is trusted: coercing a
    short or mistyped row into a plausible record would let a broken
    service answer settle a job fin never sees fail.
    """
    for name in ("job_id", "source_ref", "content_identity", "status"):
        value = body.get(name)
        if not isinstance(value, str) or not value:
            raise IndexJobUnavailableError(
                f"{where} response field {name!r} is {value!r}, expected "
                "a non-empty string"
            )
    if body["status"] not in INDEX_JOB_STATUSES:
        raise IndexJobUnavailableError(
            f"{where} response reports unknown job status {body['status']!r}"
        )
    return IndexJobRecord(
        job_id=body["job_id"],
        source_ref=body["source_ref"],
        content_identity=body["content_identity"],
        status=body["status"],
        started_at=_text_or_none(body, "started_at", where=where),
        finished_at=_text_or_none(body, "finished_at", where=where),
        failure_code=_text_or_none(body, "failure_code", where=where),
    )


def _community_state_from_body(
    body: Mapping[str, Any],
) -> CommunityIndexState:
    """The separate corpus-wide sub-object a poll must carry; its
    absence is off-contract, not a silent ``None`` ready claim."""
    state = body.get("community_index_state")
    if not isinstance(state, Mapping):
        raise IndexJobUnavailableError(
            "poll_index_job response carries no community_index_state "
            "sub-object"
        )
    status = state.get("status")
    if not isinstance(status, str) or not status:
        raise IndexJobUnavailableError(
            "community_index_state carries no string status"
        )
    failure_code = state.get("failure_code")
    if failure_code is not None and not isinstance(failure_code, str):
        raise IndexJobUnavailableError(
            "community_index_state failure_code is neither null nor a string"
        )
    return CommunityIndexState(status=status, failure_code=failure_code)


__all__ = ["MCPIndexJobPort"]
