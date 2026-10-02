"""The sql window from the USER utterance, against the LIVE producer.

Issue pre330 T2 (``docs/validation/2026-10-02-sql-date-input-contract.md``).

The live SQL cases in ``test_live_search_modes_pg_es.py`` inject the
window pair straight at the port, so they prove the PRODUCER half and say
nothing about whether a pair can ever reach that port from a tool call.
This case closes that gap in ONE hop: a single ``graph.invoke`` carries
the two MODEL-SUPPLIED tool arguments through the real
``MCPRetrieveAdapter`` to the real producer process, and the row envelope
the model then reads is asserted against ids restated from the committed
ledger fixture, never read back from the database it just queried.

One hop, not two. A stubbed transport plus an out-of-band second
``retrieve`` call re-spelling the 档 from a literal would let the two
halves drift onto different windows and still both go green, which is
the gap this file exists to close.

The model turn is the suite's ``ScriptedChatModel`` double, not a live
LLM: what is proven is the ARG PLUMBING from a tool call through the real
transport to real rows, not model behavior in the field.

Own preconditions, fail-closed: this file owns its guards (the ledger URL
and the producer build), because a required live acceptance that can
``skip`` is not evidence (``.claude/rules/test.md``).
"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

# The ``live`` fixture is the pg_es live module's; importing it here reuses
# that ONE already-booted producer session for this file instead of paying a
# second PG+ES+Neo4j boot.
from test_live_search_modes_pg_es import live  # noqa: F401

from fin.host.graph import compile_graph
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import reset_input_state
from fin.retrieve.port import DATABASE_ROWS_KEY
from live_iknow_support import IKNOW_ROOT

import pytest

# ``sql_live``, NOT ``mcp``: this leg asserts rows out of the real seeded
# ledger, and ``mcp`` is the fixture-server protocol smoke
# (``pyproject.toml`` marker definitions). Under ``mcp`` this file would
# fold into a green that proves no ledger was touched.
pytestmark = pytest.mark.sql_live

# The window the USER stated, and the window the scripted model copies
# onto the tool call. The model supplying the pair is the seam under test,
# so these are the same pair on purpose: the contract is that the tool call
# carries what the user said, unchanged. The grounded AAPL clause is not
# decoration — without it the turn lands on the clarify residual and the
# 档 cascade never assigns ``sql`` (same reason the sibling host case
# spells it that way).
_UTTERANCE = (
    "sql档 AAPL net sales 2023 Q3, which contracts expire between "
    "2026-01-01 and 2026-04-30?"
)
_MODEL_WINDOW: dict[str, str] = {
    "sql_window_start": "2026-01-01",
    "sql_window_end": "2026-04-30",
}
# Restated from ``tests/retrieve/fixtures/ledger/002_seed.sql`` (4
# counterparties / 6 contracts / 5 active), NOT read back from the query.
_EXPECTED_CONTRACT_IDS = ["C-2315", "C-2302", "C-2291", "C-2024Q1"]


def _script() -> list[AIMessage]:
    """One scripted MODEL turn whose retrieve call carries the pair."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue", **_MODEL_WINDOW},
                    "id": "c-window",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Per the filings: revenue grew."),
    ]


def _sql_filters(live_session: Any) -> list[dict[str, Any]]:
    """The ``filters`` block of every sql retrieve the producer was asked."""
    return [
        request["params"]["arguments"]["filters"]
        for request in live_session.session.requests
        if request.get("method") == "tools/call"
        and request.get("params", {}).get("name") == "retrieve"
        and request.get("params", {})
        .get("arguments", {})
        .get("filters", {})
        .get("search_mode")
        == "sql"
    ]


def test_a_user_uttered_window_reaches_the_live_producer(
    request: pytest.FixtureRequest,
) -> None:
    """Tool args -> real adapter -> real producer rows, in ONE invocation.

    The preconditions are asserted, never skipped, following
    ``test_live_search_modes_pg_es.py``: a required live acceptance whose
    dependency is missing is blocked evidence, and a green skip is not
    evidence at all.
    """
    if not os.environ.get("FIN_BUSINESS_LEDGER_URL", "").strip():
        pytest.fail(
            "FIN_BUSINESS_LEDGER_URL is unset: the live sql-window user-path "
            "acceptance is blocked, not skipped"
        )
    if not (IKNOW_ROOT / "dist" / "index.js").is_file():
        pytest.fail(
            f"the pinned iknow producer build is missing under {IKNOW_ROOT}; "
            "the live sql-window user-path acceptance is blocked, not skipped"
        )

    live_session = request.getfixturevalue("live")
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=_script()),
        adapter=live_session.retrieve,
        known_document_ids={"AAPL-2022Q3"},
    )
    out = graph.invoke(
        {**reset_input_state(_UTTERANCE), "evidence": []},
        config={"configurable": {"thread_id": "s1-sql-window"}},
    )

    # The pair the model put on the tool call is the pair the producer was
    # asked — read off the wire the real adapter built, in this same run.
    sql_filters = _sql_filters(live_session)
    assert sql_filters, "no sql retrieve reached the live producer"
    for filters in sql_filters:
        assert filters["sql_window_start"] == _MODEL_WINDOW["sql_window_start"], filters
        assert filters["sql_window_end"] == _MODEL_WINDOW["sql_window_end"], filters

    # The row envelope the MODEL read is the producer's answer to that one
    # call: same invocation, no second query, no re-spelled 档.
    envelopes = [
        json.loads(m.content)
        for m in out["messages"]
        if getattr(m, "type", "") == "tool" and isinstance(m.content, str)
    ]
    assert envelopes, [getattr(m, "content", m) for m in out["messages"]]
    assert all("error" not in envelope for envelope in envelopes), envelopes
    rows = [
        envelope[DATABASE_ROWS_KEY]
        for envelope in envelopes
        if DATABASE_ROWS_KEY in envelope
    ]
    assert len(rows) == 1, envelopes
    envelope = rows[0]
    assert envelope["result_count"] == len(envelope["rows"]) == 4, envelope
    assert [
        row["record_key"]["contract_id"] for row in envelope["rows"]
    ] == _EXPECTED_CONTRACT_IDS
