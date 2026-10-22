"""The host ``retrieve`` tool forwards the generic metadata filter.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Retrieve request and
metadata filters": the public metadata-filter object is one of the
arguments the model may supply; the host tool forwards it to the port
UNTOUCHED — the port seam owns validation and the canonical
``invalid_filter`` refusal before any I/O. The docstring describes the
four keys (``title`` / ``doc_type`` / ``company`` / ``period``) so the
model-visible surface names the contract.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.host.graph import _make_retrieve_tool
from fin.retrieve import Chunk


class _RecordingPort:
    """Retrieve port recording every kwargs it received."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result or {"chunks": [], "distribution": "zero"}
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return dict(self._result)


def _locator() -> Locator:
    return Locator(document_id="DOC-2023Q1", version="v-" + "a" * 60, unit=1)


def _chunk() -> Chunk:
    return Chunk(text="t", source_ref=_locator(), score=1.0)


def _invoke(adapter: Any, **kwargs: Any):
    tool = _make_retrieve_tool(adapter)
    payload = {"query": "what was revenue", **kwargs}
    return tool.invoke(
        {
            "name": "retrieve",
            "args": payload,
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


def test_host_tool_forwards_generic_filters_to_the_port() -> None:
    port = _RecordingPort({"chunks": [_chunk()], "distribution": "hit"})
    _invoke(port, filters={"company": "Acme", "period": "FY2025"})
    assert len(port.calls) == 1
    assert port.calls[0]["filters"] == {"company": "Acme", "period": "FY2025"}


def test_host_refuses_invalid_filters_before_reading_snapshot_or_port() -> None:
    """An invalid map is named before scope authority or retrieval I/O."""
    port = _RecordingPort()
    snapshot_reads: list[str] = []

    def read_snapshot(thread_id: str) -> None:
        snapshot_reads.append(thread_id)
        return None

    tool = _make_retrieve_tool(port, snapshot_reader=read_snapshot)
    command = tool.func(
        query="what was revenue",
        filters={"issuer": "acme"},
        tool_call_id="c-retrieve",
        state={},
        config={"configurable": {"thread_id": "thread-1"}},
    )
    payload = json.loads(command.update["messages"][0].content)

    assert payload == {"error": "invalid_filter"}
    assert snapshot_reads == []
    assert port.calls == []


def test_host_tool_records_the_absent_filter_as_none() -> None:
    """The absent form reaches the port as ``None`` — the port's
    established "argument absent, do not send" marker, the same
    convention ``search_mode=None`` holds. No invented empty dict."""
    port = _RecordingPort()
    _invoke(port)
    assert port.calls[0]["filters"] is None


def test_host_tool_docstring_names_the_four_metadata_keys() -> None:
    """The model-visible docstring describes the generic metadata
    filter contract: the four catalog-owned keys, exact matching."""
    tool = _make_retrieve_tool(_RecordingPort())
    description = tool.description
    assert "filters" in description
    for key in ("title", "doc_type", "company", "period"):
        assert key in description, key


def test_host_tool_surfaces_the_port_refusal_envelope() -> None:
    """A port ``invalid_filter`` refusal reaches the model as the error
    dict — no chunks, no rewrite, no evidence."""
    port = _RecordingPort(result={"error": "invalid_filter"})
    # ``{"company": "Acme"}`` is SHAPE-valid: the canonical schema owns the
    # shape (the four keys, value bounds), while the port owns the semantic
    # refusal. A shape-valid map therefore reaches the port, so this test
    # observes the port's own ``invalid_filter`` verdict forwarded unchanged.
    command = _invoke(port, filters={"company": "Acme"})
    payload = json.loads(command.update["messages"][0].content)
    assert payload == {"error": "invalid_filter"}
    assert "chunks" not in payload
    assert not command.update.get("evidence")
    # The refusal is the port's, not a host-side shape rejection: the call
    # was actually forwarded with the shape-valid map.
    assert len(port.calls) == 1
    assert port.calls[0]["filters"] == {"company": "Acme"}


# ----- a generic filter beside a structured request is refused, not dropped -----

_STRUCTURED = {"entity_type": "Organization", "select": ["name"]}


def _invoke_sql(adapter: Any, **kwargs: Any):
    """Invoke the tool with the ``sql`` 档 the host cascade assigns to a
    structured request (SC2: the model cannot pick the mode itself)."""
    tool = _make_retrieve_tool(adapter)
    return tool.func(
        query="find it",
        structured_request=dict(_STRUCTURED),
        state={"search_mode_decision": {"search_mode": "sql"}},
        config={},
        tool_call_id="c-retrieve",
        **kwargs,
    )


def test_a_structured_request_with_a_generic_filter_is_refused_explicitly() -> None:
    """The generic map is not translated into SQL predicates, so a
    structured request combined with one would silently drop the
    narrowing. The named refusal keeps the drop impossible — and the
    port is never reached, so nothing was searched with a lost filter.
    """
    port = _RecordingPort()
    payload = json.loads(_invoke_sql(port, filters={"doc_type": "10-Q"}).update["messages"][0].content)
    assert payload == {"error": "unsupported_filter_mode"}
    assert port.calls == []


@pytest.mark.parametrize(
    "outer",
    [
        pytest.param({"cik": "0000320193"}, id="legacy-cik"),
        pytest.param({"form": "10-Q"}, id="legacy-form"),
        pytest.param({"ticker": "AAPL"}, id="legacy-ticker"),
        pytest.param({"filing_date": "2023-09-30"}, id="legacy-filing-date"),
    ],
)
def test_a_structured_request_with_a_legacy_filter_keeps_its_own_code(outer: dict[str, str]) -> None:
    """The legacy outer names keep the malformed-query code they have
    always answered. Only the generic map is its own named refusal — a
    blanket rename would have moved existing callers' advice."""
    port = _RecordingPort()
    payload = json.loads(_invoke_sql(port, **outer).update["messages"][0].content)
    assert payload == {"error": "malformed_structured_query"}
    assert port.calls == []


def test_a_structured_request_with_a_document_locator_is_a_scope_refusal() -> None:
    """A structured read has no exact-match filter channel, so the trusted
    scope it cannot carry is precisely what cannot be honored — the spec's
    own ``unsupported_source_scope``, the same fact upstream refuses under
    ``unsupported_field``. The port is never reached, so no arm ever
    searched under a scope it dropped."""
    port = _RecordingPort()
    payload = json.loads(
        _invoke_sql(port, document_id="DOC-2023Q1").update["messages"][0].content
    )
    assert payload == {"error": "unsupported_source_scope"}
    assert "chunks" not in payload
    assert port.calls == []


def test_an_out_of_contract_map_keeps_its_code_beside_structured_request() -> None:
    """A malformed generic map keeps ``invalid_filter`` precedence."""
    port = _RecordingPort(result={"error": "invalid_filter"})
    payload = json.loads(
        _invoke_sql(port, filters={"cik": "1"}).update["messages"][0].content
    )
    assert payload == {"error": "invalid_filter"}
    assert port.calls == []


def test_a_whole_scope_filter_beside_a_structured_request_still_binds() -> None:
    """A map that normalizes to nothing narrows nothing, so it does not
    become the generic-filter refusal — the port is reached instead.
    The refusal that follows is the structured lane's own (this fake
    port answers no ``structured_result`` sibling), which is the point:
    the combination itself was not what failed."""
    port = _RecordingPort()
    payload = json.loads(
        _invoke_sql(port, filters={"title": "   "}).update["messages"][0].content
    )
    assert payload != {"error": "unsupported_filter_mode"}
    assert len(port.calls) == 1


def test_a_structured_request_alone_still_binds() -> None:
    """Control: the refusals belong to the COMBINATION, not to the
    structured request itself. The port is reached and the answer
    travels; the ``structured_search_unavailable`` this fake earns is the
    structured lane's own missing-sibling rule, not a filter refusal."""
    port = _RecordingPort({"chunks": [_chunk()], "distribution": "hit"})
    payload = json.loads(_invoke_sql(port).update["messages"][0].content)
    assert payload == {"error": "structured_search_unavailable"}
    assert len(port.calls) == 1
