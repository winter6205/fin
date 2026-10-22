"""``compare_facts`` on the wire and in the turn table (T3 / SC2).

The acceptance line for this ticket: a scripted research turn that
called the comparison tool must leave ``GET /api/threads/{id}``
holding the STRUCTURE — rows, columns, cells, deltas, locators —
not a prose table the SPA would have to re-parse.

Four seams carry it, and each has a test below:

1. ``_run_to_response_payload`` lifts ``comparisons`` off the final
   graph state into the response payload;
2. ``RunRecord.comparisons`` persists it (in-memory store round
   trip, copy-isolated like ``citations``);
3. ``GET /api/threads/{id}`` serializes it onto the turn row;
4. the send stream emits a ``comparison`` SSE frame when the tool
   returns, so the live view renders the table before the answer.

A refused turn keeps its comparison. The table is tool output, not
model prose: it is what CONTRADICTED the fabricated answer, so
hiding it would hide the evidence for the refusal. Citations stay
empty on ``refuse`` / ``step_cap`` — that contract is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.evidence.evidence_contract import AdmissionContext, validate_evidence
from fin.host.run_payload import _run_to_response_payload
from fin.host.thread_store import InMemoryThreadStore, RunRecord
from fin.host.tools.compare_facts import COMPARISON_SCHEMA
from fin.retrieve import FakeRetrievePort


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "AAPL-2023Q3", "MSFT-2023Q3"}

# The ``read_source`` company leg over the committed corpus units. Only
# the scripted ``read_source`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
_READ_ADAPTER = FakeRetrievePort(
    pages_by_doc={
        AAPL_2022Q3: load_extracted_tree(
            Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
        )[AAPL_2022Q3]
    }
)

# Digit-free verbatim slice of the committed AAPL-2022Q3 page 1: the
# citation gate needs a >=32-char extractive span, and a number
# inside the span would itself be a claim for the numeric gate.
PAGE_SPAN = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"

COMPARE_QUESTION = "Compare AAPL revenue for Q3 2023 versus Q3 2022."
COMPARISON_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "kind",
        "axis",
        "concept",
        "unit",
        "scale",
        "columns",
        "rows",
        "deltas",
        "missing",
        "hit_count",
        "cell_count",
        "locators",
    }
)


def _comparison_context(compare_seed) -> AdmissionContext:
    """Context from the two fact sources explicitly seeded for this run."""
    return AdmissionContext(
        received_source_versions=frozenset(
            {
                (compare_seed.aapl_2022_doc, compare_seed.aapl_2022_version),
                (compare_seed.aapl_2023_doc, compare_seed.aapl_2023_version),
            }
        )
    )


# ----- fixtures -----


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def store(http_app: FastAPI) -> InMemoryThreadStore:
    from fin.host.http import get_thread_store

    made = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: made
    return made


def _install_graph(
    http_app: FastAPI, script: list[AIMessage], *, facts_root: Path
) -> Any:
    from fin.host.http import get_graph

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        facts_root=facts_root,
    )
    http_app.dependency_overrides[get_graph] = lambda: graph
    return graph


def _compare_then_answer_script(
    *, answer: str, periods: list[str]
) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "compare_facts",
                    "args": {
                        "concept": "us-gaap:Revenues",
                        "periods": periods,
                        "issuers": ["AAPL"],
                    },
                    "id": "c-compare",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "unit": 1,
                        },
                        "position": 0,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:") :].strip()
    if "event" in current and "data" in current:
        events.append((current["event"], json.loads(current["data"])))
    return events


def _send(
    client: TestClient,
    store: InMemoryThreadStore,
    http_app: FastAPI,
    *,
    script: list[AIMessage],
    facts_root: Path,
) -> tuple[str, list[tuple[str, Any]]]:
    """Create a thread, run one send, return ``(thread_id, events)``."""
    _install_graph(http_app, script, facts_root=facts_root)
    thread_id = client.post(
        "/api/threads", json={"first_message": COMPARE_QUESTION}
    ).json()["id"]
    response = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": COMPARE_QUESTION}
    )
    assert response.status_code == 200
    return thread_id, _parse_sse_events(response.text)


# ----- 1. run payload -----


def test_run_payload_lifts_comparisons_off_the_state() -> None:
    envelope = {"schema": COMPARISON_SCHEMA, "rows": [], "tool_call_id": "c-1"}
    payload = _run_to_response_payload(
        {"status": "done", "answer": "a", "comparisons": [envelope]}
    )
    assert payload["comparisons"] == [envelope]


def test_run_payload_comparisons_default_to_empty() -> None:
    """A run that never compared still carries the key, so no reader
    branches on its absence."""
    assert _run_to_response_payload({"status": "done"})["comparisons"] == []
    assert _run_to_response_payload({"status": "refuse"})["comparisons"] == []


def test_run_payload_comparisons_survive_a_refusal() -> None:
    """``refuse`` empties ``answer`` and ``citations`` — but the
    comparison is TOOL output, and it is what contradicted the
    answer."""
    envelope = {"schema": COMPARISON_SCHEMA, "rows": []}
    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": "numeric_gate_fail",
            "comparisons": [envelope],
        }
    )
    assert payload["citations"] == []
    assert payload["comparisons"] == [envelope]


def test_run_payload_drops_non_dict_comparison_entries() -> None:
    """Defensive: the channel is graph-owned, but a malformed entry
    must not reach a Pydantic response model."""
    payload = _run_to_response_payload(
        {"status": "done", "comparisons": ["nope", None, {"schema": "x"}]}
    )
    assert payload["comparisons"] == [{"schema": "x"}]


# ----- 2. RunRecord persistence -----


def test_run_record_round_trips_comparisons() -> None:
    made = InMemoryThreadStore()
    thread = made.create_thread(title="t")
    made.append_run(
        RunRecord(
            id="r-1",
            thread_id=thread.id,
            question="q",
            status="done",
            comparisons=[{"schema": COMPARISON_SCHEMA, "rows": []}],
        )
    )
    assert made.list_runs(thread.id)[0].comparisons == [
        {"schema": COMPARISON_SCHEMA, "rows": []}
    ]


def test_listed_comparisons_are_copies() -> None:
    """Same invariant ``citations`` holds: a caller mutating the
    returned record cannot corrupt the store."""
    made = InMemoryThreadStore()
    thread = made.create_thread(title="t")
    made.append_run(
        RunRecord(
            id="r-1",
            thread_id=thread.id,
            question="q",
            status="done",
            comparisons=[{"schema": COMPARISON_SCHEMA, "rows": []}],
        )
    )
    made.list_runs(thread.id)[0].comparisons[0]["rows"].append("tampered")
    assert made.list_runs(thread.id)[0].comparisons[0]["rows"] == []


def test_run_record_defaults_comparisons_to_empty() -> None:
    assert RunRecord(id="r", thread_id="t", question="q", status="done").comparisons == []


# ----- 3. GET turn row (the ticket's acceptance line) -----


def test_get_thread_turn_row_carries_the_structured_comparison(
    client: TestClient, store, http_app: FastAPI, compare_seed
) -> None:
    thread_id, _ = _send(
        client,
        store,
        http_app,
        script=_compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue grew year over year.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
    )
    turn = client.get(f"/api/threads/{thread_id}").json()["turns"][0]
    assert turn["status"] == "done"
    comparison = turn["comparisons"][0]
    assert set(comparison) == COMPARISON_ENVELOPE_FIELDS
    assert comparison["schema"] == COMPARISON_SCHEMA
    comparison_summary = next(
        summary
        for summary in turn["tool_summaries"]
        if summary["name"] == "compare_facts"
    )
    assert comparison_summary["tool_call_id"] == "c-compare"
    validation = validate_evidence(
        {"comparison": comparison}, _comparison_context(compare_seed)
    )
    assert validation.accepted, validation.refusal
    assert comparison["axis"] == "period"
    assert comparison["concept"] == compare_seed.concept
    assert [c["key"] for c in comparison["columns"]] == [
        compare_seed.period_2022,
        compare_seed.period_2023,
    ]
    assert [cell["value"] for cell in comparison["rows"][0]["cells"]] == [
        compare_seed.aapl_2022_value,
        compare_seed.aapl_2023_value,
    ]
    assert comparison["deltas"][0]["direction"] == "up"
    assert comparison["locators"][0]["document_id"] == compare_seed.aapl_2022_doc
    # The persisted turn row carries the typed unit ordinal, so the SPA
    # and the export can cite the row without a legacy page field.
    assert comparison["locators"][0]["unit"] == 4


def test_turn_row_without_a_comparison_serializes_an_empty_list(
    client: TestClient, store, http_app: FastAPI, compare_seed
) -> None:
    """The key is always present, so the SPA never branches on
    ``undefined``."""
    script = _compare_then_answer_script(
        answer=f"{PAGE_SPAN} Revenue grew.", periods=[]
    )
    thread_id, _ = _send(
        client, store, http_app, script=script, facts_root=compare_seed.root
    )
    turn = client.get(f"/api/threads/{thread_id}").json()["turns"][0]
    assert turn["comparisons"] == []


def test_fabricated_numbers_turn_is_not_done_but_keeps_the_table(
    client: TestClient, store, http_app: FastAPI, compare_seed
) -> None:
    """SC2 acceptance: the numeric gate refuses, citations are empty,
    and the operator still sees the numbers the filing DID report."""
    thread_id, _ = _send(
        client,
        store,
        http_app,
        script=_compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue rose from $500,000 million to "
            f"$900,000 million.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
    )
    turn = client.get(f"/api/threads/{thread_id}").json()["turns"][0]
    assert turn["status"] == "refuse"
    assert turn["status_reason"] == "numeric_gate_fail"
    assert turn["answer"] == ""
    assert turn["citations"] == []
    assert turn["comparisons"][0]["hit_count"] == 2


# ----- 4. SSE comparison frame -----


def test_send_stream_emits_a_comparison_frame(
    client: TestClient, store, http_app: FastAPI, compare_seed
) -> None:
    _, events = _send(
        client,
        store,
        http_app,
        script=_compare_then_answer_script(
            answer=f"{PAGE_SPAN} Revenue grew year over year.",
            periods=[compare_seed.period_2022, compare_seed.period_2023],
        ),
        facts_root=compare_seed.root,
    )
    names = [name for name, _ in events]
    frames = [data for name, data in events if name == "comparison"]
    assert len(frames) == 1
    assert set(frames[0]) == COMPARISON_ENVELOPE_FIELDS
    assert frames[0]["schema"] == COMPARISON_SCHEMA
    tool_results = [
        data
        for name, data in events
        if name == "tool_result" and data["name"] == "compare_facts"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "c-compare"
    validation = validate_evidence(
        {"comparison": frames[0]}, _comparison_context(compare_seed)
    )
    assert validation.accepted, validation.refusal
    # The table lands BEFORE the terminal citations frame closes the
    # reader, so the live view renders it without waiting for a GET.
    assert names.index("comparison") < names.index("citations")


def test_a_typed_empty_comparison_emits_no_frame(
    client: TestClient, store, http_app: FastAPI, compare_seed
) -> None:
    """``empty_comparison`` has no columns: there is nothing to
    render, and an empty table on the wire reads as a real answer."""
    _, events = _send(
        client,
        store,
        http_app,
        script=_compare_then_answer_script(
            answer=f"{PAGE_SPAN} No data.", periods=[]
        ),
        facts_root=compare_seed.root,
    )
    assert [name for name, _ in events if name == "comparison"] == []


def test_other_tools_emit_no_comparison_frame(
    client: TestClient, store, http_app: FastAPI, compare_seed
) -> None:
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "unit": 1,
                        },
                        "position": 0,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=f"{PAGE_SPAN} Filed with the commission."),
    ]
    _, events = _send(
        client, store, http_app, script=script, facts_root=compare_seed.root
    )
    assert [name for name, _ in events if name == "comparison"] == []
