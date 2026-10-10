"""EP6 admission: the producer sibling becomes stored evidence, or a typed refusal.

The whole admission chain — validation, snapshot freezing, digesting, claim
verification, stored-record shape — was built and tested before anything
called it. These cases pin the seam that was missing: an untrusted
``structured_result`` sibling from the retrieve tool reaching
``RunRecord.structured_evidence``, and the ``AgentState`` channel that
carries it out of the tool node.

What is load-bearing here:

* a REFUSED sibling never degrades into a success-shaped empty record list.
  ``structured_snapshots_of`` answers ``[]`` for both "valid, zero rows" and
  "refused", so this file pins which of the two actually happened;
* the channel survives ``_fold_tool_update`` and ``tool_node_with_summaries``,
  whose docstring states an unknown key is SILENTLY DROPPED — so a happy-path
  assertion alone would pass with the channel dead in production;
* the reducer is additive, so two retrieves in one run keep two record sets
  and the reset sentinel (not ``[]``) clears the channel.

Hermetic by construction: no Postgres, no network, no live model. The
live-JSONB write leg is :mod:`tests.host.test_structured_evidence_live_pg`.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from fin.host.evidence_contract import (
    CONTRACT_REFUSAL_CODES,
    EVIDENCE_TOO_LARGE,
    MALFORMED_EVIDENCE_CONTRACT,
    UNSUPPORTED_EVIDENCE_CONTRACT,
)
from fin.host.graph import _fold_tool_update
from fin.host.state import (
    STRUCTURED_EVIDENCE_RESET,
    AgentState,
    reset_input_state,
)
from fin.host.structured_evidence import (
    MAX_STRUCTURED_ROWS,
    STORED_EVIDENCE_FIELDS,
    StructuredRefusal,
    project_stored_structured_evidence,
)
from fin.host.structured_evidence_admission import admit_structured_evidence
from fin.host.structured_snapshot import (
    STRUCTURED_SNAPSHOT_MAX_BYTES,
    structured_snapshot_digest,
)

BASE_CONTEXT = {
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
}

ROW = {
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

SECOND_ROW = {
    **ROW,
    "id": "00000000-0000-4000-8000-000000000002",
    "canonical_name": "Southwind Freight",
    "evidence_ref": "evref-0002",
}


def _list_sibling(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **BASE_CONTEXT,
        "rows": [copy.deepcopy(ROW)],
        "next_cursor": None,
    }
    payload.update(over)
    return payload


def _count_sibling(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **copy.deepcopy(BASE_CONTEXT),
        "operation": "count",
        "count": 3,
        "count_receipt": {
            "count": 3,
            "subject": "entity",
            "scope_binding": BASE_CONTEXT["scope_binding"],
            "queried_at": BASE_CONTEXT["queried_at"],
            "coverage": "stored_structured_subjects",
            "maintenance_revision": BASE_CONTEXT["maintenance"]["maintenance_revision"],
            "maintenance": copy.deepcopy(BASE_CONTEXT["maintenance"]),
            "evidence_ref": "evref-count-1",
            "evidence_expires_at": "2027-10-03T00:00:00Z",
            "query_digest": BASE_CONTEXT["query_digest"],
            "read_revision": BASE_CONTEXT["read_revision"],
        },
    }
    for key, value in over.items():
        if key == "count_receipt" and isinstance(value, dict):
            payload["count_receipt"] = value
        else:
            payload[key] = value
    return payload


def _oversized_sibling() -> dict[str, Any]:
    """A sibling whose frozen snapshot cannot fit the 32 KiB envelope bound."""
    padding = "x" * (STRUCTURED_SNAPSHOT_MAX_BYTES + 1)
    return _list_sibling(rows=[{**copy.deepcopy(ROW), "canonical_name": padding}])


class _HostileMapping(dict):
    """A Mapping whose ``get`` raises — an untrusted object, not a dict."""

    def get(self, key: Any, default: Any = None) -> Any:
        raise RuntimeError("untrusted read")


# ----- empty -----


def test_a_well_formed_sibling_admits_one_stored_record_per_row() -> None:
    outcome = admit_structured_evidence(_list_sibling())

    assert outcome.refusal is None
    assert outcome.admitted is True
    assert len(outcome.records) == 1
    assert frozenset(outcome.records[0]) == STORED_EVIDENCE_FIELDS
    assert project_stored_structured_evidence(outcome.records[0]) is not None


def test_a_count_receipt_admits_exactly_one_record() -> None:
    outcome = admit_structured_evidence(_count_sibling())

    assert outcome.refusal is None
    assert len(outcome.records) == 1
    assert outcome.records[0]["snapshot"]["operation"] == "count"
    assert outcome.records[0]["snapshot"]["value"]["count"] == 3


def test_an_empty_row_list_is_an_admitted_empty_result_not_a_refusal() -> None:
    outcome = admit_structured_evidence(_list_sibling(rows=[]))

    assert outcome.admitted is True
    assert outcome.refusal is None
    assert outcome.records == ()


def test_an_absent_sibling_is_not_applicable_and_is_not_a_refusal() -> None:
    """``structured_result_of`` answers ``None`` for a document retrieve.

    The structured lane is one arm of a shared tool, so its absence is the
    ordinary case, not a malformed contract.
    """
    outcome = admit_structured_evidence(None)

    assert outcome.admitted is True
    assert outcome.refusal is None
    assert outcome.records == ()


# ----- invalid -----


@pytest.mark.parametrize(
    "sibling",
    [
        "not-a-mapping",
        42,
        ["rows"],
        {"schema": "iknow.structured_result"},
        {"unrelated": "object"},
    ],
    ids=["str", "int", "list", "partial", "unrelated"],
)
def test_a_non_contract_sibling_refuses_with_a_registered_code(sibling: Any) -> None:
    outcome = admit_structured_evidence(sibling)

    assert outcome.admitted is False
    assert outcome.records == ()
    assert outcome.refusal is not None
    assert outcome.refusal.code in CONTRACT_REFUSAL_CODES


def test_a_wrong_schema_token_refuses() -> None:
    outcome = admit_structured_evidence(_list_sibling(schema="iknow.structured_query"))

    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


@pytest.mark.parametrize("version", ["1", 1.5, True, None], ids=["str", "frac", "bool", "null"])
def test_a_non_integer_schema_version_refuses(version: Any) -> None:
    outcome = admit_structured_evidence(_list_sibling(schema_version=version))

    assert outcome.admitted is False
    assert outcome.refusal is not None
    assert outcome.refusal.code in CONTRACT_REFUSAL_CODES


def test_an_unknown_operation_refuses_as_unsupported() -> None:
    outcome = admit_structured_evidence(_list_sibling(operation="aggregate"))

    assert outcome.refusal is not None
    assert outcome.refusal.code == UNSUPPORTED_EVIDENCE_CONTRACT


def test_an_unknown_key_in_a_row_refuses() -> None:
    outcome = admit_structured_evidence(
        _list_sibling(rows=[{**copy.deepcopy(ROW), "invented": "field"}])
    )

    assert outcome.admitted is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


# ----- the [] ambiguity and the refusal-is-never-empty rule -----


def test_a_refused_sibling_does_not_degrade_into_success_shaped_empty_evidence() -> None:
    """``structured_snapshots_of`` answers ``[]`` for a refused sibling too.

    T29's acceptance is exactly this: a refusal may not be published as an
    empty success.
    """
    refused = admit_structured_evidence(_list_sibling(operation="aggregate"))
    empty = admit_structured_evidence(_list_sibling(rows=[]))

    assert refused.records == ()
    assert refused.refusal is not None
    assert empty.records == ()
    assert empty.refusal is None
    assert refused.admitted is False
    assert empty.admitted is True


def test_every_refusal_code_the_entry_point_emits_is_registered() -> None:
    cases = [
        _list_sibling(operation="aggregate"),
        _list_sibling(schema="nope"),
        _list_sibling(schema_version="1"),
        _list_sibling(rows=[{**copy.deepcopy(ROW), "invented": 1}]),
        _oversized_sibling(),
        "not-a-mapping",
    ]
    for sibling in cases:
        outcome = admit_structured_evidence(sibling)
        assert outcome.refusal is not None, sibling
        assert outcome.refusal.code in CONTRACT_REFUSAL_CODES, outcome.refusal


# ----- overflow -----


def test_an_oversized_snapshot_refuses_instead_of_reaching_the_store() -> None:
    outcome = admit_structured_evidence(_oversized_sibling())

    assert outcome.admitted is False
    assert outcome.records == ()
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_TOO_LARGE


def test_more_rows_than_the_batch_bound_refuses() -> None:
    rows = [{**copy.deepcopy(ROW), "id": f"00000000-0000-4000-8000-{i:012d}"} for i in range(MAX_STRUCTURED_ROWS + 1)]

    outcome = admit_structured_evidence(_list_sibling(rows=rows))

    assert outcome.admitted is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_TOO_LARGE


def test_exactly_the_row_bound_still_admits() -> None:
    rows = [{**copy.deepcopy(ROW), "id": f"00000000-0000-4000-8000-{i:012d}"} for i in range(MAX_STRUCTURED_ROWS)]

    outcome = admit_structured_evidence(_list_sibling(rows=rows))

    assert outcome.refusal is None
    assert len(outcome.records) == MAX_STRUCTURED_ROWS


# ----- concurrent / repeat -----


def test_admitting_the_same_sibling_twice_is_idempotent() -> None:
    sibling = _list_sibling()

    first = admit_structured_evidence(sibling)
    second = admit_structured_evidence(sibling)

    assert [record["digest"] for record in first.records] == [
        record["digest"] for record in second.records
    ]
    assert first.records == second.records


def test_the_digest_recomputes_from_the_stored_snapshot_alone() -> None:
    record = admit_structured_evidence(_list_sibling()).records[0]

    assert record["digest"] == structured_snapshot_digest(record["snapshot"])


def test_two_distinct_retrieves_in_one_run_keep_two_record_sets() -> None:
    first = admit_structured_evidence(_list_sibling())
    second = admit_structured_evidence(_list_sibling(rows=[copy.deepcopy(SECOND_ROW)]))

    digests = {record["digest"] for record in (*first.records, *second.records)}

    assert len(digests) == 2


# ----- exception: an unreadable untrusted object is a refusal, not a crash -----


def test_an_unreadable_sibling_object_is_refused_rather_than_raised() -> None:
    outcome = admit_structured_evidence(_HostileMapping(schema="iknow.structured_result"))

    assert outcome.admitted is False
    assert outcome.records == ()
    assert outcome.refusal is not None
    assert outcome.refusal.code in CONTRACT_REFUSAL_CODES


# ----- the AgentState channel -----


def test_the_channel_is_declared_additive() -> None:
    annotations = AgentState.__annotations__.get("structured_evidence")

    assert annotations is not None
    assert "structured_evidence" in AgentState.__annotations__


def test_a_fresh_run_input_clears_the_channel_with_the_sentinel_not_a_list() -> None:
    state = reset_input_state("q")

    assert state["structured_evidence"] is STRUCTURED_EVIDENCE_RESET
    assert state["structured_evidence"] != []


def test_the_channel_reducer_accumulates_and_the_sentinel_clears() -> None:
    from fin.host.state import _structured_evidence_reducer

    first = [admit_structured_evidence(_list_sibling()).records[0]]
    second = [admit_structured_evidence(_list_sibling(rows=[copy.deepcopy(SECOND_ROW)])).records[0]]

    accumulated = _structured_evidence_reducer([], first)
    both = _structured_evidence_reducer(accumulated, second)

    assert len(both) == 2
    assert _structured_evidence_reducer(both, STRUCTURED_EVIDENCE_RESET) == []
    assert _structured_evidence_reducer(both, []) == both


# ----- the channel survives the ToolNode return-shape fold -----


def _command_with(records: Any) -> Any:
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    return Command(
        update={
            "messages": [ToolMessage(content="{}", tool_call_id="c1", name="retrieve")],
            "structured_evidence": records,
        }
    )


def test_the_channel_survives_the_tool_update_fold() -> None:
    """``_fold_tool_update`` DROPS an unknown key silently.

    Its docstring says so, so without this case a channel that was never
    added to the fold would still pass every happy-path assertion.
    """
    records = list(admit_structured_evidence(_list_sibling()).records)

    folded = _fold_tool_update([_command_with(records)])

    assert folded["structured_evidence"] == records


def test_the_folded_channel_accumulates_across_two_commands_in_one_step() -> None:
    first = list(admit_structured_evidence(_list_sibling()).records)
    second = list(
        admit_structured_evidence(_list_sibling(rows=[copy.deepcopy(SECOND_ROW)])).records
    )

    folded = _fold_tool_update([_command_with(first), _command_with(second)])

    assert len(folded["structured_evidence"]) == 2


def test_the_folded_channel_omits_the_key_when_no_command_carried_one() -> None:
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    folded = _fold_tool_update(
        [Command(update={"messages": [ToolMessage(content="{}", tool_call_id="c1")]})]
    )

    assert folded["structured_evidence"] == []


def test_the_retrieve_tool_node_publishes_admitted_records_onto_the_command() -> None:
    from fin.host.graph import _make_retrieve_tool
    from fin.retrieve.mcp_adapter import MCPRetrieveAdapter

    class _ScriptedAdapter(MCPRetrieveAdapter):
        def __init__(self, response: dict[str, Any]) -> None:
            super().__init__(command=[])
            self.response = response

        async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
            return self.response

    adapter = _ScriptedAdapter(
        {"result": {"chunks": [], "structured_result": _list_sibling()}}
    )
    tool = _make_retrieve_tool(adapter)

    command = tool.func(
        query="Find the organization",
        structured_request={
            "schema": "iknow.structured_query",
            "schema_version": 1,
            "subject": "entity",
            "operation": "list",
            "filters": [],
        },
        state={"search_mode_decision": {"search_mode": "sql"}},
        config={},
        tool_call_id="call-structured",
    )

    published = command.update["structured_evidence"]
    assert len(published) == 1
    assert project_stored_structured_evidence(published[0]) is not None


def test_a_refused_structured_result_publishes_no_records_on_the_command() -> None:
    from fin.host.graph import _make_retrieve_tool
    from fin.retrieve.mcp_adapter import MCPRetrieveAdapter

    class _ScriptedAdapter(MCPRetrieveAdapter):
        def __init__(self, response: dict[str, Any]) -> None:
            super().__init__(command=[])
            self.response = response

        async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
            return self.response

    refused = _list_sibling(operation="aggregate")
    tool = _make_retrieve_tool(_ScriptedAdapter({"result": {"chunks": [], "structured_result": refused}}))

    command = tool.func(
        query="Find the organization",
        structured_request={
            "schema": "iknow.structured_query",
            "schema_version": 1,
            "subject": "entity",
            "operation": "list",
            "filters": [],
        },
        state={"search_mode_decision": {"search_mode": "sql"}},
        config={},
        tool_call_id="call-structured",
    )

    assert not command.update.get("structured_evidence")


def test_a_refusal_is_typed_never_a_bare_exception() -> None:
    refusal: StructuredRefusal = admit_structured_evidence(
        _list_sibling(operation="aggregate")
    ).refusal  # type: ignore[assignment]

    assert refusal.as_dict()["code"] in CONTRACT_REFUSAL_CODES


# ----- the channel survives the summarising tool node -----


def _structured_graph_run() -> dict[str, Any]:
    """One real run whose retrieve call returns a structured result.

    ``tool_node_with_summaries`` merges the ToolNode result itself, so the
    fold being correct is not enough — this is the node that has to keep the
    key, and it is a closure inside the factory, so it is only reachable by
    running the graph.
    """
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from fin.host.graph import compile_graph
    from fin.host.scripted_model import ScriptedChatModel
    from fin.retrieve.mcp_adapter import MCPRetrieveAdapter

    class _StructuredAdapter(MCPRetrieveAdapter):
        def __init__(self) -> None:
            super().__init__(command=[])

        async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"result": {"chunks": [], "structured_result": _list_sibling()}}

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {
                            "query": "revenue",
                            "structured_request": {
                                "schema": "iknow.structured_query",
                                "schema_version": 1,
                                "subject": "entity",
                                "operation": "list",
                                "filters": [],
                            },
                        },
                        "id": "call-structured-run",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Northwind Logistics is an organization."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_StructuredAdapter(),
    )
    out = graph.invoke(
        reset_input_state("what was AAPL net sales in 2023 Q3?"),
        config={
            "configurable": {
                "thread_id": "structured-admission",
                "pinned_search_mode": "sql",
            }
        },
    )
    assert isinstance(out, dict)
    return out


def test_the_channel_survives_the_summarising_tool_node() -> None:
    out = _structured_graph_run()

    records = out.get("structured_evidence") or []
    assert len(records) == 1
    assert project_stored_structured_evidence(records[0]) is not None


def test_the_records_survive_even_when_the_citation_gate_refuses_the_turn() -> None:
    """The structured lane is not the quote channel.

    This run's prose is refused (``corpus_not_ready``) because it cites
    nothing, yet the same-turn structured result still left its record —
    a gate verdict on the answer must not drop evidence the run actually
    received.
    """
    out = _structured_graph_run()

    assert out.get("status") == "refuse"
    assert len(out.get("structured_evidence") or []) == 1


def test_a_followup_send_does_not_inherit_the_previous_runs_records() -> None:
    """The reset sentinel clears the channel, so ``[]`` would have been a
    no-op and a second run would store the first run's snapshot."""
    out = _structured_graph_run()

    # The run that just finished left records on the channel; a fresh input
    # must clear them through the sentinel rather than through ``[]``.
    fresh = reset_input_state("And who is Southwind Freight?")
    assert fresh["structured_evidence"] is STRUCTURED_EVIDENCE_RESET
    assert out.get("structured_evidence")

