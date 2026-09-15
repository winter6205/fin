"""具名失败 reach the operator surface: SSE status frame + turn row (T3).

``tests/host/test_named_failure_retrieve.py`` pins the payload the two
surfaces are BUILT from. This module pins the surfaces themselves —
the live SSE ``status`` frame the SPA renders, and the persisted
``RunRecord`` a reload replays — because 「运行/错误面可见」 is the
actual SC4/SC5 acceptance, not the intermediate dict.

The two scenarios are the ones the ticket exists for:

* empty-bound 资料集合 (0 chunks, 0 drops) -> ``corpus_not_ready``
  with retrieve-side ordinary-person copy, NOT a verifier reason;
* a plain ungrounded answer with no retrieve at all -> the verifier's
  own ``unknown_locator`` (the existing exit stays reachable).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.named_failures import CORPUS_NOT_READY
from fin.host.run_payload import ORDINARY_PERSON_COPY


KNOWN_IDS = {"AAPL-2022Q3"}
UNKNOWN_LOCATOR_ANSWER = (
    "Per the ZZZZ-9999Q1 filing, revenue was up across all segments "
    "this quarter."
)


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def store(http_app: FastAPI):
    from fin.host.feedback import InMemoryFeedbackStore
    from fin.host.http import get_feedback_store, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    thread_store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: thread_store
    http_app.dependency_overrides[get_feedback_store] = (
        lambda: InMemoryFeedbackStore()
    )
    return thread_store


def _install(http_app: FastAPI, model: ScriptedChatModel) -> Any:
    from fin.host.http import get_graph

    built = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: built
    return built


def _parse_sse(body: str) -> list[tuple[str, Any]]:
    import json

    events: list[tuple[str, Any]] = []
    name = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[len("data: ") :])))
    return events


def _send(
    client: TestClient, question: str = "What was AAPL net sales in 2023 Q3?"
) -> list:
    created = client.post("/api/threads", json={"first_message": "q"}).json()
    with client.stream(
        "POST",
        f"/api/threads/{created['id']}/messages",
        json={"question": question},
    ) as r:
        body = "".join(r.iter_text())
    return list(_parse_sse(body)) + [("__thread__", created["id"])]


def _status_frame(events: list) -> dict[str, Any]:
    return next(data for name, data in events if name == "status")


# ----- the live wire -----


def test_empty_bound_retrieve_names_the_failure_on_the_status_frame(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """SC4 on the wire: the 0-chunk retrieve ends the SSE run on the
    retrieve-side reason, with copy the operator can act on."""
    _install(
        http_app,
        ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": "what was revenue"},
                            "id": "c-retrieve",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Revenue was up across all segments."),
            ]
        ),
    )

    events = _send(client)
    status = _status_frame(events)

    assert status["status"] == "refuse"
    assert status["reason"] == CORPUS_NOT_READY
    assert status["assistant_message"] == ORDINARY_PERSON_COPY[CORPUS_NOT_READY]


def test_the_turn_row_persists_the_named_failure(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """A reload replays the same verdict: the row is not blank and does
    not collapse into a verifier reason."""
    _install(
        http_app,
        ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": "what was revenue"},
                            "id": "c-retrieve",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Revenue was up across all segments."),
            ]
        ),
    )

    events = _send(client)
    thread_id = next(data for name, data in events if name == "__thread__")

    detail = client.get(f"/api/threads/{thread_id}").json()
    assert len(detail["turns"]) == 1
    turn = detail["turns"][0]
    assert turn["status"] == "refuse"
    assert turn["status_reason"] == CORPUS_NOT_READY
    assert turn["assistant_message"] == ORDINARY_PERSON_COPY[CORPUS_NOT_READY]


def test_all_dropped_hits_name_the_mapping_failure_on_the_wire(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """SC5 on the wire: retrieve returned a hit, the mapper dropped it.
    The SSE reason must be ``mapping_drop`` — not the verifier's
    ``citation_repair_cap`` (which is what an ungrounded answer with a
    healthy retrieve would produce) and not 未就绪."""
    from fin.host.http import get_graph
    from fin.host.named_failures import MAPPING_DROP
    from fin.retrieve import Chunk
    from fin.retrieve.hit_mapping import MappingFailureLedger
    from fin.retrieve.read_port import read_from_pages

    class _DroppingPort:
        """Returns a hit whose ``source_ref`` is not a fin locator."""

        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "chunks": [
                    Chunk(
                        text=(
                            "Total net sales for the quarter increased by "
                            "nine percent compared with the prior year."
                        ),
                        source_ref="iknow-row-000123",  # type: ignore[arg-type]
                        score=9.0,
                    )
                ]
            }

        def read(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return read_from_pages({}, "", 1, 1)

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "what was revenue"},
                        "id": "c-retrieve",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Revenue was up across all segments."),
        ]
    )
    built = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_DroppingPort(),
        read_port=_DroppingPort(),
        known_document_ids=KNOWN_IDS,
        mapping_failure_ledger=MappingFailureLedger(),
    )
    http_app.dependency_overrides[get_graph] = lambda: built

    events = _send(client)
    status = _status_frame(events)

    assert status["status"] == "refuse"
    assert status["reason"] == MAPPING_DROP
    assert status["assistant_message"] == ORDINARY_PERSON_COPY[MAPPING_DROP]
    assert status["assistant_message"] != ORDINARY_PERSON_COPY[CORPUS_NOT_READY]
    assert status["assistant_message"] != ORDINARY_PERSON_COPY["unknown_locator"]


def test_a_run_without_retrieve_keeps_the_verifier_reason_on_the_wire(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """The third exit stays reachable and distinguishable: no retrieve
    at all is not 未就绪."""
    _install(
        http_app,
        ScriptedChatModel(script=[AIMessage(content=UNKNOWN_LOCATOR_ANSWER)]),
    )

    status = _status_frame(_send(client))

    assert status["status"] == "refuse"
    assert status["reason"] == "unknown_locator"
    assert status["assistant_message"] == ORDINARY_PERSON_COPY["unknown_locator"]
    assert status["assistant_message"] != ORDINARY_PERSON_COPY[CORPUS_NOT_READY]
