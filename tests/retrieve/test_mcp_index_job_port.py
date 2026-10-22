"""Wire-contract tests for MCPIndexJobPort against a stub session.

The stub plays the JSON-RPC half of an iknow-memory server: every
assertion is about the payload fin sends and the typed failure fin
reads off the answer. No live process is involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fin.retrieve.index_job_port import (
    CommunityIndexState,
    IndexJobError,
    IndexJobIdentityMismatchError,
    IndexJobInProgressError,
    IndexJobInvalidLocatorError,
    IndexJobPoll,
    IndexJobRecord,
    IndexJobSubmission,
    IndexJobUnknownJobError,
    IndexJobUnknownSourceError,
    IndexJobUnavailableError,
)
from fin.retrieve.mcp_index_job_port import MCPIndexJobPort
from fin.retrieve.mcp_session import (
    MCPStdioProtocolError,
    MCPStdioTransportError,
)

_TOOL_REQUEST_ID = 2

SUBMIT_BODY: dict[str, Any] = {
    "job_id": "job-1",
    "source_ref": "src-1",
    "content_identity": "id-1",
    "status": "in_progress",
    "started_at": None,
    "finished_at": None,
    "failure_code": None,
    "reused": False,
}

POLL_BODY: dict[str, Any] = {
    "job_id": "job-1",
    "source_ref": "src-1",
    "content_identity": "id-1",
    "status": "ready",
    "started_at": "2026-01-01T00:00:00.000Z",
    "finished_at": "2026-01-01T00:02:00.000Z",
    "failure_code": None,
    "community_index_state": {"status": "in_progress", "failure_code": None},
}


class StubSession:
    """Records payloads and replays scripted responses or exceptions."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def call(
        self, payload: dict[str, Any], timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.calls.append(payload)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _tool_response(body: Any, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [
            {"type": "text", "text": body if isinstance(body, str) else json.dumps(body)}
        ]
    }
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": _TOOL_REQUEST_ID, "result": result}


# ----- submit -----


def test_submit_sends_the_pair_on_the_wire() -> None:
    session = StubSession(_tool_response(SUBMIT_BODY))
    port = MCPIndexJobPort(session=session)
    port.submit_index_job("src-1", "id-1")
    assert session.calls == [
        {
            "jsonrpc": "2.0",
            "id": _TOOL_REQUEST_ID,
            "method": "tools/call",
            "params": {
                "name": "submit_index_job",
                "arguments": {"source_ref": "src-1", "content_identity": "id-1"},
            },
        }
    ]


def test_submit_parses_record_and_reused_flag() -> None:
    port = MCPIndexJobPort(session=StubSession(_tool_response(SUBMIT_BODY)))
    submission = port.submit_index_job("src-1", "id-1")
    assert submission == IndexJobSubmission(
        record=IndexJobRecord(
            job_id="job-1",
            source_ref="src-1",
            content_identity="id-1",
            status="in_progress",
            started_at=None,
            finished_at=None,
            failure_code=None,
        ),
        reused=False,
    )


def test_submit_rejects_blank_locators_before_the_round_trip() -> None:
    session = StubSession()
    port = MCPIndexJobPort(session=session)
    with pytest.raises(IndexJobInvalidLocatorError):
        port.submit_index_job("", "id-1")
    with pytest.raises(IndexJobInvalidLocatorError):
        port.submit_index_job("   ", "id-1")
    with pytest.raises(IndexJobInvalidLocatorError):
        port.submit_index_job("src-1", "x" * 1025)
    assert session.calls == []


def test_submit_requires_boolean_reused() -> None:
    body = dict(SUBMIT_BODY)
    del body["reused"]
    port = MCPIndexJobPort(session=StubSession(_tool_response(body)))
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")


# ----- poll -----


def test_poll_sends_the_job_id() -> None:
    session = StubSession(_tool_response(POLL_BODY))
    port = MCPIndexJobPort(session=session)
    port.poll_index_job("job-1")
    assert session.calls[0]["params"] == {
        "name": "poll_index_job",
        "arguments": {"job_id": "job-1"},
    }


def test_poll_parses_record_and_separate_community_state() -> None:
    port = MCPIndexJobPort(session=StubSession(_tool_response(POLL_BODY)))
    poll = port.poll_index_job("job-1")
    assert poll == IndexJobPoll(
        record=IndexJobRecord(
            job_id="job-1",
            source_ref="src-1",
            content_identity="id-1",
            status="ready",
            started_at="2026-01-01T00:00:00.000Z",
            finished_at="2026-01-01T00:02:00.000Z",
            failure_code=None,
        ),
        community_index_state=CommunityIndexState("in_progress", None),
    )


def test_poll_accepts_the_unwrapped_result_variant() -> None:
    response = {"jsonrpc": "2.0", "id": _TOOL_REQUEST_ID, "result": POLL_BODY}
    port = MCPIndexJobPort(session=StubSession(response))
    poll = port.poll_index_job("job-1")
    assert poll.record.status == "ready"
    assert poll.community_index_state == CommunityIndexState("in_progress", None)


def test_poll_accepts_an_explicit_null_community_state() -> None:
    """A never-written C singleton answers ``null`` on the wire, and a
    present ``null`` is that honest "no claim yet" answer — not a body
    fin has to reject. The job row beside it is still parsed in full."""
    body = {**POLL_BODY, "community_index_state": None}
    port = MCPIndexJobPort(session=StubSession(_tool_response(body)))
    poll = port.poll_index_job("job-1")
    assert poll.community_index_state is None
    assert poll.record == IndexJobRecord(
        job_id="job-1",
        source_ref="src-1",
        content_identity="id-1",
        status="ready",
        started_at="2026-01-01T00:00:00.000Z",
        finished_at="2026-01-01T00:02:00.000Z",
        failure_code=None,
    )


def test_poll_accepts_a_null_community_state_on_the_unwrapped_variant() -> None:
    body = {**POLL_BODY, "community_index_state": None}
    response = {"jsonrpc": "2.0", "id": _TOOL_REQUEST_ID, "result": body}
    port = MCPIndexJobPort(session=StubSession(response))
    assert port.poll_index_job("job-1").community_index_state is None


def test_poll_requires_the_community_state_key() -> None:
    body = {key: value for key, value in POLL_BODY.items() if key != "community_index_state"}
    port = MCPIndexJobPort(session=StubSession(_tool_response(body)))
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


@pytest.mark.parametrize(
    "state",
    [
        pytest.param([], id="list"),
        pytest.param("in_progress", id="string"),
        pytest.param(7, id="number"),
        pytest.param({}, id="empty_object"),
        pytest.param({"failure_code": None}, id="no_status"),
        pytest.param({"status": ""}, id="blank_status"),
        pytest.param({"status": 1}, id="mistyped_status"),
        pytest.param({"status": "ready", "failure_code": 7}, id="mistyped_failure_code"),
    ],
)
def test_poll_rejects_a_malformed_community_state(state: Any) -> None:
    """Only an explicit ``null`` reads as "not written yet"; anything
    else that is not a well-formed sub-object stays off-contract."""
    body = {**POLL_BODY, "community_index_state": state}
    port = MCPIndexJobPort(session=StubSession(_tool_response(body)))
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


def test_poll_rejects_blank_job_id_before_the_round_trip() -> None:
    session = StubSession()
    port = MCPIndexJobPort(session=session)
    with pytest.raises(IndexJobInvalidLocatorError):
        port.poll_index_job(" ")
    assert session.calls == []


# ----- typed refusals -----


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("index_job_invalid_locator", IndexJobInvalidLocatorError),
        ("index_job_unknown_source", IndexJobUnknownSourceError),
        ("index_job_identity_mismatch", IndexJobIdentityMismatchError),
        ("index_job_unknown_job", IndexJobUnknownJobError),
        ("index_job_in_progress", IndexJobInProgressError),
    ],
)
def test_is_error_body_projects_the_typed_failure(
    code: str, error_type: type[IndexJobError]
) -> None:
    refusal = {"error": code, "message": "unrelated prose"}
    port = MCPIndexJobPort(
        session=StubSession(_tool_response(refusal, is_error=True))
    )
    with pytest.raises(error_type) as caught:
        port.submit_index_job("src-1", "id-1")
    assert caught.value.code == code


def test_in_progress_refusal_carries_active_job_id() -> None:
    refusal = {
        "error": "index_job_in_progress",
        "message": "open",
        "active_job_id": "job-9",
    }
    port = MCPIndexJobPort(
        session=StubSession(_tool_response(refusal, is_error=True))
    )
    with pytest.raises(IndexJobInProgressError) as caught:
        port.submit_index_job("src-1", "id-1")
    assert caught.value.active_job_id == "job-9"


def test_code_is_read_off_the_field_not_the_prose() -> None:
    # The prose QUOTES a known literal; the missing/invalid ``error``
    # field must still degrade to unavailable, never to the quoted code.
    refusal = {"message": "sounds like index_job_unknown_source to you"}
    port = MCPIndexJobPort(
        session=StubSession(_tool_response(refusal, is_error=True))
    )
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")


def test_unnamed_refusal_code_degrades_to_unavailable() -> None:
    refusal = {"error": "index_job_hallucinated", "message": "off-contract"}
    port = MCPIndexJobPort(
        session=StubSession(_tool_response(refusal, is_error=True))
    )
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")


def test_undecodable_refusal_is_unavailable() -> None:
    port = MCPIndexJobPort(
        session=StubSession(_tool_response("not json at all", is_error=True))
    )
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")


# ----- transport / envelope failures -----


def test_session_transport_error_is_unavailable() -> None:
    session = StubSession(MCPStdioTransportError("dead process"))
    port = MCPIndexJobPort(session=session)
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


def test_session_protocol_error_is_unavailable() -> None:
    session = StubSession(MCPStdioProtocolError("off-contract frame"))
    port = MCPIndexJobPort(session=session)
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


def test_json_rpc_error_frame_is_unavailable() -> None:
    response = {
        "jsonrpc": "2.0",
        "id": _TOOL_REQUEST_ID,
        "error": {"code": -32603, "message": "internal"},
    }
    port = MCPIndexJobPort(session=StubSession(response))
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


def test_response_id_mismatch_is_unavailable() -> None:
    response = _tool_response(POLL_BODY)
    response["id"] = 99
    port = MCPIndexJobPort(session=StubSession(response))
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


def test_success_body_with_unknown_status_is_unavailable() -> None:
    body = dict(SUBMIT_BODY)
    body["status"] = "probably_fine"
    port = MCPIndexJobPort(session=StubSession(_tool_response(body)))
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")


def test_success_body_with_missing_fields_is_unavailable() -> None:
    body = dict(SUBMIT_BODY)
    del body["job_id"]
    port = MCPIndexJobPort(session=StubSession(_tool_response(body)))
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")


def test_success_text_that_is_not_json_is_unavailable() -> None:
    port = MCPIndexJobPort(
        session=StubSession(_tool_response("<html>502</html>"))
    )
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")


def test_no_transport_configured_is_unavailable() -> None:
    port = MCPIndexJobPort(command=None)
    with pytest.raises(IndexJobUnavailableError):
        port.submit_index_job("src-1", "id-1")
    with pytest.raises(IndexJobUnavailableError):
        port.poll_index_job("job-1")
