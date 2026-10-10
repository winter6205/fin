"""QP8 wire contract for the typed structured request on retrieve."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.port import InvalidBusinessQueryError, new_database_rows
from fin.host.structured_evidence import validate_structured_result


STRUCTURED_QUERY = {
    "schema": "iknow.structured_query",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "filters": [
        {"field": "canonical_name", "op": "eq", "value": "Northwind Logistics"}
    ],
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


class _CapturingAdapter(MCPRetrieveAdapter):
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        super().__init__(command=[])
        self.responses = list(responses or [])
        self.payloads: list[dict[str, Any]] = []

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(copy.deepcopy(payload))
        if not self.responses:
            raise AssertionError("unexpected MCP call")
        return self.responses.pop(0)


def _success(structured_result: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"chunks": []}
    if structured_result is not None:
        result["structured_result"] = structured_result
    return {"result": result}


def _mcp_error(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": {
            "isError": True,
            "content": [
                {"type": "text", "text": json.dumps(body, separators=(",", ":"))}
            ],
        }
    }


def _arguments(adapter: _CapturingAdapter, index: int = 0) -> dict[str, Any]:
    return adapter.payloads[index]["params"]["arguments"]


def test_structured_request_is_forwarded_as_an_object_under_the_only_wire_key() -> None:
    adapter = _CapturingAdapter([_success(STRUCTURED_RESULT)])

    result = adapter.retrieve(
        "Describe the matching organization",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"chunks": [], "structured_result": STRUCTURED_RESULT}
    arguments = _arguments(adapter)
    assert arguments["filters"] == {
        "search_mode": "sql",
        "structured_query": STRUCTURED_QUERY,
    }
    assert "sql_window_start" not in arguments["filters"]
    assert "sql_window_end" not in arguments["filters"]
    assert set(arguments) == {"query", "limit", "filters"}


def test_descriptive_query_changes_do_not_change_typed_predicates() -> None:
    adapter = _CapturingAdapter(
        [_success(STRUCTURED_RESULT), _success(STRUCTURED_RESULT)]
    )

    first_result = adapter.retrieve(
        "Describe the matching organization",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )
    second_result = adapter.retrieve(
        "Find the organization called Northwind Logistics",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    first = _arguments(adapter, 0)
    second = _arguments(adapter, 1)
    assert first["query"] != second["query"]
    assert first["filters"] == second["filters"]
    assert first["filters"]["structured_query"] == STRUCTURED_QUERY
    assert first_result["structured_result"] == STRUCTURED_RESULT
    assert second_result["structured_result"] == STRUCTURED_RESULT


@pytest.mark.parametrize("mode", [None, "graph", "global"])
def test_structured_request_refuses_an_unbound_or_non_sql_mode(mode: str | None) -> None:
    adapter = _CapturingAdapter()

    result = adapter.retrieve(
        "Find the entity",
        search_mode=mode,
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "malformed_structured_query"}
    assert adapter.payloads == []


@pytest.mark.parametrize(
    "outer_filter",
    [
        {"document_id": "doc-1"},
        {"cik": "0000320193"},
        {"ticker": "AAPL"},
        {"filing_date": "2026-10-01"},
    ],
    ids=["document-id", "cik", "ticker", "filing-date"],
)
def test_structured_request_refuses_outer_filters_that_change_its_scope(
    outer_filter: dict[str, str],
) -> None:
    adapter = _CapturingAdapter()

    result = adapter.retrieve(
        "Find the entity",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
        sql_window_start="2026-10-01",
        sql_window_end="2026-10-31",
        **outer_filter,
    )

    assert result == {"error": "malformed_structured_query"}
    assert adapter.payloads == []


def test_structured_sql_does_not_require_the_legacy_expiry_window() -> None:
    adapter = _CapturingAdapter([_success(STRUCTURED_RESULT)])

    try:
        result = adapter.retrieve(
            "Find the entity",
            search_mode="sql",
            structured_request=STRUCTURED_QUERY,
        )
    except InvalidBusinessQueryError as exc:
        pytest.fail(f"structured sql must not require the legacy window: {exc}")

    assert result == {"chunks": [], "structured_result": STRUCTURED_RESULT}
    assert "filters" in _arguments(adapter)
    assert adapter.payloads


def test_structured_request_refuses_success_without_structured_result() -> None:
    adapter = _CapturingAdapter([_success()])

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "structured_search_unavailable"}
    assert len(adapter.payloads) == 1


def test_structured_result_sibling_is_preserved_as_a_success_value() -> None:
    assert validate_structured_result(STRUCTURED_RESULT) is None
    adapter = _CapturingAdapter([_success(STRUCTURED_RESULT)])

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"chunks": [], "structured_result": STRUCTURED_RESULT}


def test_named_structured_error_remains_typed_instead_of_becoming_a_miss() -> None:
    error = {
        "schema": "iknow.structured_error",
        "schema_version": 1,
        "error": {"code": "unsupported_field", "retryable": False},
    }
    adapter = _CapturingAdapter(
        [
            {
                "result": {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": json.dumps(error, separators=(",", ":"))}
                    ],
                }
            }
        ]
    )

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "unsupported_field"}
    assert "chunks" not in result


def test_decoded_integral_float_schema_version_preserves_typed_error_code() -> None:
    response = _mcp_error(
        {
            "schema": "iknow.structured_error",
            "schema_version": 1.0,
            "error": {"code": "unsupported_field", "retryable": False},
        }
    )
    adapter = _CapturingAdapter([response])

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "unsupported_field"}


@pytest.mark.parametrize("schema_version", [True, 1.5], ids=["boolean", "fractional"])
def test_non_integer_or_boolean_error_schema_version_is_unavailable(
    schema_version: object,
) -> None:
    adapter = _CapturingAdapter(
        [
            _mcp_error(
                {
                    "schema": "iknow.structured_error",
                    "schema_version": schema_version,
                    "error": {"code": "unsupported_field", "retryable": False},
                }
            )
        ]
    )

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "structured_search_unavailable"}


def test_unknown_structured_error_code_is_unavailable() -> None:
    adapter = _CapturingAdapter(
        [
            _mcp_error(
                {
                    "schema": "iknow.structured_error",
                    "schema_version": 1,
                    "error": {"code": "future_query_error", "retryable": False},
                }
            )
        ]
    )

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "structured_search_unavailable"}


@pytest.mark.parametrize(
    "include_structured_result",
    [False, True],
    ids=["legacy-only", "alongside-structured-result"],
)
def test_structured_request_refuses_legacy_database_rows(
    include_structured_result: bool,
) -> None:
    response = _success(STRUCTURED_RESULT if include_structured_result else None)
    response["result"]["database_rows"] = new_database_rows([])
    adapter = _CapturingAdapter([response])

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "structured_search_unavailable"}


@pytest.mark.parametrize(
    "legacy_code",
    ["sql_no_matching_rows", "sql_query_denied"],
    ids=["legacy-empty", "legacy-refusal"],
)
def test_structured_request_does_not_reinterpret_legacy_sql_exits(
    legacy_code: str,
) -> None:
    adapter = _CapturingAdapter([_mcp_error({"error": legacy_code})])

    result = adapter.retrieve(
        "List structured entities",
        search_mode="sql",
        structured_request=STRUCTURED_QUERY,
    )

    assert result == {"error": "structured_search_unavailable"}
