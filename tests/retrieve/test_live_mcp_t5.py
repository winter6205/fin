"""P1 T5: product retrieve wiring and degraded-empty handling."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.retrieve.capabilities import capability_switch_hits
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter

from live_iknow_support import IKNOW_WORKTREE_ROOT, child_records, live_iknow


pytestmark = pytest.mark.mcp


class _DegradedEmptyAdapter(MCPRetrieveAdapter):
    """Return the exact degraded-empty envelope from a service."""

    def __init__(self) -> None:
        super().__init__(locator_map={})

    async def _call_mcp(self, _payload: dict) -> dict:
        body = {
            "chunks": [],
            "degraded": True,
            "degraded_reasons": ["vector:attempted-and-failed"],
        }
        return {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(body)}
                ]
            },
        }


def test_degraded_empty_response_is_named_failure_not_empty_success() -> None:
    result = _DegradedEmptyAdapter().retrieve("operating revenue")

    assert result["error"] == "retrieve_degraded"
    assert result["degraded"] is True
    assert "chunks" not in result


def test_compiled_product_retrieve_sends_no_capability_off_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compiled host path sends only the retrieve contract fields —
    plus the 档 this turn was ASSIGNED, and nothing else.

    A routing residual is an ASSIGNMENT now (ADR 0104: the recorded
    default ``graph``), so the retrieve call carries the 档 capability
    filter the service reads (``filters.search_mode``). The prohibition
    this test is named for is unchanged and is the one that matters: no
    capability-OFF key ever reaches the wire.
    """

    # The envelope-based wire matches the operator-console worktree
    # schema; the main-checkout ``push`` schema predates T1 and rejects
    # the ``chunks`` field. Run against the worktree build.
    with live_iknow(monkeypatch, root=IKNOW_WORKTREE_ROOT) as live:
        records = child_records()
        pushed_text = records[0].text
        live.push.push([record.to_wire() for record in records])
        graph = compile_graph(
            checkpointer=InMemorySaver(),
            model=ScriptedChatModel(
                script=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "retrieve",
                                "args": {
                                    "query": "operating revenue",
                                    "limit": len(records) + 1,
                                },
                                "id": "live-retrieve",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content=pushed_text),
                ]
            ),
            adapter=live.retrieve,
            known_document_ids={"LIVE-2026Q3"},
        )

        graph.invoke(
            reset_input_state("What was AAPL revenue in 2023 Q1?"),
            config={"configurable": {"thread_id": "live-t5"}},
        )

        retrieve_calls = [
            request
            for request in live.session.requests
            if request["params"]["name"] == "retrieve"
        ]
        assert len(retrieve_calls) == 1
        arguments = retrieve_calls[0]["params"]["arguments"]
        assert capability_switch_hits(arguments) == ()
        assert set(arguments) == {"query", "limit", "filters"}
        # The 档 the cascade recorded for this turn is the one the service
        # is told — the recorded default rides the wire like any other
        # assigned 档.
        assert arguments["filters"] == {"search_mode": "graph"}
