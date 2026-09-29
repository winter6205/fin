"""T6 host graph and workspace persistence for analysis envelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator, load_extracted_tree
from fin.corpus.facts import Fact, write_facts
from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve import FakeRetrievePort


DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "corpus"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
AAPL_2023Q3_VERSION = (
    "7b9b54830f070aab56cd842b391dd81d21613aacda2cb92234f43b997930b0ea"
)
KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q3"}

# The ``read_source`` company leg over the committed corpus units. Only
# the scripted ``read_source`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
_READ_ADAPTER = FakeRetrievePort(
    pages_by_doc={
        "AAPL-2022Q3": load_extracted_tree(
            DATA_DIR / "manifest.json", DATA_DIR / "extracted"
        )["AAPL-2022Q3"]
    }
)
PAGE_SPAN = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"


def _redline_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "redline",
                "args": {
                    "before_document_id": "AAPL-2022Q3",
                    "before_version": AAPL_2022Q3_VERSION,
                    "after_document_id": "AAPL-2023Q3",
                    "after_version": AAPL_2023Q3_VERSION,
                },
                "id": "c-redline",
                "type": "tool_call",
            }
        ],
    )


def test_compile_graph_registers_t6_analysis_tools() -> None:
    model = ScriptedChatModel(script=[])

    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        manifest_path=DATA_DIR / "manifest.json",
        extracted_root=DATA_DIR / "extracted",
        facts_root=DATA_DIR / "facts",
    )

    names = [getattr(tool, "name", "") for tool in model.bound_tools]
    # read_source is bound last (ADR 0087);
    # the analysis trio keeps its relative order ahead of it.
    assert names[-4:] == [
        "redline",
        "extract_tables",
        "three_year_statement",
        "read_source",
    ]


def test_redline_tool_result_lands_on_graph_state() -> None:
    model = ScriptedChatModel(
        script=[_redline_call(), AIMessage(content="The filings changed by paragraph.")]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        manifest_path=DATA_DIR / "manifest.json",
        extracted_root=DATA_DIR / "extracted",
        facts_root=DATA_DIR / "facts",
    )

    result = graph.invoke(
        {
            **reset_input_state(
                "Compare AAPL 2022 and 2023 filing paragraphs."
            ),
        },
        config={"configurable": {"thread_id": "analysis-state"}},
    )

    assert len(result["analysis_results"]) == 1
    analysis = result["analysis_results"][0]
    assert analysis["schema"] == "fin.redline.v1"
    assert analysis["tool_call_id"] == "c-redline"


@pytest.fixture
def analysis_http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


def _parse_sse(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    event_name = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            events.append((event_name, json.loads(line[6:])))
            event_name = ""
    return events


def test_workspace_get_history_contains_structured_redline(
    analysis_http_app: FastAPI,
) -> None:
    from fin.host.http import get_graph, get_thread_store

    model = ScriptedChatModel(
        script=[
            _redline_call(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": "AAPL-2022Q3",
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
            AIMessage(content=f"{PAGE_SPAN} The filings changed by paragraph."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        manifest_path=DATA_DIR / "manifest.json",
        extracted_root=DATA_DIR / "extracted",
        facts_root=DATA_DIR / "facts",
    )
    store = InMemoryThreadStore()
    analysis_http_app.dependency_overrides[get_graph] = lambda: graph
    analysis_http_app.dependency_overrides[get_thread_store] = lambda: store
    client = TestClient(analysis_http_app)

    created = client.post(
        "/api/threads", json={"first_message": "Compare filing paragraphs"}
    ).json()
    response = client.post(
        f"/api/threads/{created['id']}/messages",
        json={"question": "Compare AAPL 2022 and 2023 filing paragraphs."},
    )

    assert response.status_code == 200
    assert any(name == "analysis" for name, _ in _parse_sse(response.text))
    history = client.get(f"/api/threads/{created['id']}")
    assert history.status_code == 200
    persisted = history.json()["turns"][0]["analysis_results"]
    assert persisted[0]["schema"] == "fin.redline.v1"
    assert persisted[0]["tool_call_id"] == "c-redline"


def test_statement_numbers_are_checked_by_numeric_gate(tmp_path: Path) -> None:
    facts_root = tmp_path / "facts"
    for year, value in ((2021, 100), (2022, 120), (2023, 150)):
        document_id = f"AAPL-{year}FY"
        write_facts(
            document_id=document_id,
            facts_root=facts_root,
            facts=[
                Fact(
                    concept="us-gaap:Revenues",
                    value=value,
                    period=f"{year}-FY",
                    unit="USD",
                    scale="millions",
                    source_locator=Locator(
                        document_id=document_id, version="f" * 64, unit=4
                    ),
                )
            ],
        )
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "three_year_statement",
                        "args": {
                            "issuer": "AAPL",
                            "periods": ["2021-FY", "2022-FY", "2023-FY"],
                            "concepts": ["us-gaap:Revenues"],
                        },
                        "id": "c-statement",
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
                                "document_id": "AAPL-2022Q3",
                                "version": AAPL_2022Q3_VERSION,
                                "unit": 1,
                            },
                            "position": 0,
                        },
                        "id": "c-read-statement",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"{PAGE_SPAN} Revenue was $999 million."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
        manifest_path=DATA_DIR / "manifest.json",
        extracted_root=DATA_DIR / "extracted",
        facts_root=facts_root,
    )

    result = graph.invoke(
        reset_input_state(
            "Show AAPL revenue for 2021 FY, 2022 FY, and 2023 FY."
        ),
        config={"configurable": {"thread_id": "statement-numeric-gate"}},
    )

    assert result["status"] == "refuse"
    assert result["status_reason"] == "numeric_gate_fail"
