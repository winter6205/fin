"""QP8 host-route tests for the single model-facing retrieve tool."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from fin.host.graph import _make_retrieve_tool
from fin.host.evidence.structured_evidence import validate_structured_result
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


STRUCTURED_QUERY = {
    "schema": "iknow.structured_query",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "filters": [],
}

STRUCTURED_RESULT = {
    "schema": "iknow.structured_result",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "query_digest": "sha256:0f1e2d3c",
    "scope_binding": "scope-fin-tenant-7",
    "read_revision": "rev-118",
    "queried_at": "2026-10-03T00:00:00Z",
    "maintenance": {
        "maintenance_revision": "maintenance-rev-12",
        "sources_in_scope": 1,
        "published_sources": 1,
        "desired_index": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "conflict_checks": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    },
    "rows": [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "canonical_name": "Northwind Logistics",
            "type": "organization",
            "display_support_id": "00000000-0000-4000-8000-000000000101",
            "display_disagreement": False,
            "evidence_ref": "evref-0001",
            "evidence_expires_at": "2027-10-03T00:00:00Z",
            "provenance": [
                {
                    "support_id": "00000000-0000-4000-8000-000000000201",
                    "source_ref": "company-source-1",
                    "source_version": "source-version-1",
                    "location": {
                        "document_id": "example-document",
                        "version": "frozen-unit-version",
                        "unit": 1,
                    },
                    "span": {"start": 0, "end": 42},
                    "extraction_ref": "extraction-revision-1",
                }
            ],
            "provenance_complete": True,
            "provenance_next_ref": None,
        }
    ],
    "next_cursor": None,
}


class _ScriptedMCPAdapter(MCPRetrieveAdapter):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(command=[])
        self.response = response
        self.payloads: list[dict[str, Any]] = []

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.response


def _tool_message(command: Any) -> dict[str, Any]:
    messages = command.update["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], ToolMessage)
    return json.loads(messages[0].content)


def _call_host_retrieve(
    adapter: MCPRetrieveAdapter,
    *,
    search_mode: str | None,
) -> tuple[Any, Any]:
    tool = _make_retrieve_tool(adapter)
    # The model-visible schema carries the query object, but never a mode
    # selector. The mode is injected from this run's cascade state.
    assert "structured_request" in tool.args
    assert "search_mode" not in tool.args
    command = tool.func(
        query="Find the organization",
        structured_request=STRUCTURED_QUERY,
        state={"search_mode_decision": {"search_mode": search_mode}},
        config={},
        tool_call_id="call-structured-retrieve",
    )
    return command, tool


def test_host_cascade_binds_sql_and_forwards_the_structured_result_sibling() -> None:
    assert validate_structured_result(STRUCTURED_RESULT) is None
    adapter = _ScriptedMCPAdapter(
        {"result": {"chunks": [], "structured_result": STRUCTURED_RESULT}}
    )

    command, _tool = _call_host_retrieve(adapter, search_mode="sql")

    payload = _tool_message(command)
    assert payload == {
        "chunks": [],
        "distribution": "zero",
        "structured_result": STRUCTURED_RESULT,
    }
    arguments = adapter.payloads[0]["params"]["arguments"]
    assert arguments["filters"]["search_mode"] == "sql"
    assert arguments["filters"]["structured_query"] == STRUCTURED_QUERY
    assert "sql_window_start" not in arguments["filters"]
    assert "sql_window_end" not in arguments["filters"]
    assert not command.update.get("evidence")


def test_host_surfaces_a_named_structured_error_without_an_empty_chunks_answer() -> None:
    structured_error = {
        "schema": "iknow.structured_error",
        "schema_version": 1,
        "error": {"code": "malformed_structured_query", "retryable": False},
    }
    adapter = _ScriptedMCPAdapter(
        {
            "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": json.dumps(structured_error)}
                ],
            }
        }
    )

    command, _tool = _call_host_retrieve(adapter, search_mode="sql")

    payload = _tool_message(command)
    assert payload == {"error": "malformed_structured_query"}
    assert "chunks" not in payload
    assert not command.update.get("evidence")


def test_host_refuses_a_structured_query_without_a_sql_assignment() -> None:
    for mode in (None, "graph", "global"):
        adapter = _ScriptedMCPAdapter({"result": {"chunks": []}})

        command, _tool = _call_host_retrieve(adapter, search_mode=mode)

        assert _tool_message(command) == {"error": "malformed_structured_query"}
        assert adapter.payloads == []
