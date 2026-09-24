"""T3 five-class matrix, host side: 档 × the send door's named 409/400
exits and the run-payload mapping of the 综述-not-ready token.

``specs/search-mode-retrieve.md`` Boundaries name five test classes on
the port (empty / negative / overflow / concurrent / exception). The
port-level half lives in ``tests/retrieve/test_fake_port.py`` and
``tests/retrieve/test_mcp_adapter.py`` (T3 sections). This module owns
the **named-exit path** half:

* concurrent: the 409 ``concurrent_run`` guard is orthogonal to the 档
  selector — an open 档 (``graph`` or the newly opened ``global``) does
  not bypass it, and the one plan slot still refused at routing (``sql``)
  is named BEFORE the probe (a retry of the in-flight run would not
  answer a 「真库未接」 request).
* exception: the knowledge-MCP gate's 409 ``knowledge_mcp_fault``
  applies to an open-tier send exactly as to a tier-less one, and the
  routing-time 400 still wins over it — the unopened 档's named refusal
  is not a transport fact, so it reads the same whether MCP is up or down.
* negative: ``community_summary_index_failure`` (T1's token) rides the
  run payload with its OWN ordinary-person copy and is never invented
  by fin's retrieve-side decision from an empty or failed retrieve.

Fake is not a 档 (SC8): nothing here asserts any 档's ranking — only
which named exit answers which request.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_FAULT,
    KNOWLEDGE_MCP_FAULT_ERROR,
    KNOWLEDGE_MCP_FAULT_MESSAGE,
    publish_knowledge_mcp_state,
)
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    CORPUS_NOT_READY,
    MAPPING_DROP,
    REAL_DB_NOT_WIRED,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    TIER_NOT_OPEN,
    named_refusal_reason,
    retrieve_blocking_failure,
    retrieve_outcomes_delta,
)
from fin.host.run_payload import ORDINARY_PERSON_COPY, _run_to_response_payload


KNOWN_IDS = {"AAPL-2022Q3"}


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


def _compile() -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="answer")]),
        known_document_ids=KNOWN_IDS,
    )


@pytest.fixture
def graph(http_app: FastAPI) -> Any:
    from fin.host.http import get_graph

    built = _compile()
    http_app.dependency_overrides[get_graph] = lambda: built
    return built


class _LockedGraph:
    """Wraps a compiled graph but forces the route's in-flight probe.

    The concurrency guard is exactly ``graph.is_locked(thread_id)``
    probed BEFORE the SSE headers (spec: 409 decided before the
    stream). Driving a real run into the locked state needs a blocking
    model and threads (``test_workspace_http`` does that for the plain
    contract); here the fact under test is WHICH named exit wins, so
    the probe is made deterministic and ``invoke`` is forbidden: a
    request refused with 400/409 must never start a run.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def is_locked(self, thread_id: str) -> bool:
        return True

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused send must never start a run")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def locked_graph(http_app: FastAPI) -> Any:
    from fin.host.http import get_graph

    built = _LockedGraph(_compile())
    http_app.dependency_overrides[get_graph] = lambda: built
    return built


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "mcp")


# ----- concurrent: 409 ``concurrent_run`` × 档 selector -----


@pytest.mark.parametrize("tier", ["graph", "global"])
def test_an_open_tier_send_still_meets_the_concurrent_run_409(
    tier: str, client: TestClient, store, locked_graph
) -> None:
    """The 档 selector is routing metadata, not a lane: naming an OPEN
    档 (``graph``, or ``global`` since 综述 was opened into a real report
    arm) while another run is in flight gets the same plain-JSON 409
    ``concurrent_run`` as a tier-less send — the opened 档 clears the
    routing gate and meets the lock, it is not a 「档未开」 400."""
    thread_id = store.create_thread(title="existing").id

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": [tier]},
    )

    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": "concurrent_run"}


@pytest.mark.parametrize("tier", ["sql"])
def test_an_unopened_tier_is_named_before_the_concurrent_run_409(
    tier: str, client: TestClient, store, locked_graph
) -> None:
    """Exit ORDER on the concurrency seam: the routing-time 400 naming the
    plan slot fires BEFORE the lock probe — telling the operator to wait
    for the in-flight run would be wrong advice for a 档 this product has
    not opened, and retrying after the run ends answers the same 400.
    ``sql`` (真数据库) is the plan slot left standing once 综述 was opened;
    it takes that named exit exactly as the retired letter once did, and
    starts no run."""
    thread_id = store.create_thread(title="existing").id

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": [tier]},
    )

    assert r.status_code == 400, r.text
    assert r.json() == {"error": REAL_DB_NOT_WIRED}


# ----- exception: 409 ``knowledge_mcp_fault`` × 档 selector -----


@pytest.mark.parametrize("tier", ["graph", "global"])
def test_the_mcp_fault_gate_409_applies_to_an_open_tier_send(
    tier: str, client: TestClient, store, graph, mcp_env
) -> None:
    """Boundary 'exception' on the named-409 surface: with the product
    MCP adapter published 坏, an OPEN-档 send is refused by the gate
    exactly like a tier-less send — the 档 never reaches the (down)
    retrieve, and the two 409 tokens stay distinguishable
    (``knowledge_mcp_fault`` here vs ``concurrent_run`` above). ``global``
    clears the routing gate now that 综述 is open, so it meets this
    transport fault rather than a 「档未开」 400."""
    thread_id = store.create_thread(title="existing").id
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_FAULT)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": [tier]},
    )

    assert r.status_code == 409, r.text
    assert r.json() == {
        "error": KNOWLEDGE_MCP_FAULT_ERROR,
        "message": KNOWLEDGE_MCP_FAULT_MESSAGE,
    }


def test_an_unopened_tier_is_named_before_the_mcp_fault_gate(
    client: TestClient, store, graph, mcp_env
) -> None:
    """The unopened 档's named refusal is not a transport fact: the 400
    wins over the closed gate, so an operator with MCP down and a
    ``sql`` (真数据库) pin gets the SAME answer as with MCP up — never a
    retry-them advice token."""
    thread_id = store.create_thread(title="existing").id
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_FAULT)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["sql"]},
    )

    assert r.status_code == 400, r.text
    assert r.json() == {"error": REAL_DB_NOT_WIRED}


# ----- negative: the 综述-not-ready token on the run payload -----


def test_community_summary_index_failure_rides_the_run_payload_intact() -> None:
    """SC4's run surface: when the service-side index fact reaches the
    run as ``status_reason``, the payload carries the token UNCHANGED
    and resolves it to its own ordinary-person copy — a refuse never
    collapses into the verifier's wording or an empty bubble."""
    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": COMMUNITY_SUMMARY_INDEX_FAILURE,
        }
    )

    assert payload["status"] == "refuse"
    assert payload["reason"] == COMMUNITY_SUMMARY_INDEX_FAILURE
    assert payload["citations"] == []
    assert (
        payload["assistant_message"]
        == ORDINARY_PERSON_COPY[COMMUNITY_SUMMARY_INDEX_FAILURE]
    )
    assert payload["assistant_message"].strip()
    # Distinct copy: not the empty-bound 资料集合 wording it would be
    # confused with if the two exits collapsed.
    assert (
        payload["assistant_message"]
        != ORDINARY_PERSON_COPY[CORPUS_NOT_READY]
    )


def test_fin_never_invents_the_community_summary_index_failure() -> None:
    """Direction of the mapping: ``community_summary_index_failure``
    names the SERVICE's index fact. fin's own retrieve-side decision
    reads only chunk/mapping outcomes — an empty collection stays
    ``corpus_not_ready`` and never gets upgraded to the 综述-index token
    (that would blame the summary job for a collection that simply has
    no hits; the global pin itself is covered by the adapter/fake T3
    sections)."""
    assert (
        retrieve_blocking_failure([{"chunks": 0, "mapping_failures": 0}])
        == CORPUS_NOT_READY
    )
    assert (
        named_refusal_reason(
            evidence=[],
            outcomes=[{"chunks": 0, "mapping_failures": 0}],
            verifier_reason="unknown_locator",
        )
        != COMMUNITY_SUMMARY_INDEX_FAILURE
    )


def test_a_recorded_community_summary_refusal_emits_the_runtime_code() -> None:
    """The positive chain (mirror of the Fast-fact decision tests): when a
    retrieve call answered the global 综述 index's typed refusal, the tool
    records :data:`RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY` and the
    no-chunk run resolves to ``community_summary_index_failure`` — the same
    token the operator copy carries — not ``corpus_not_ready``. The flag is
    the ONLY source (see the never-invents test above), and any chunk>0
    pre-empts it for the verifier's own verdict."""
    record = {
        "chunks": 0,
        "mapping_failures": 0,
        RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
    }
    assert retrieve_blocking_failure([record]) == COMMUNITY_SUMMARY_INDEX_FAILURE
    # A later call that produced a usable chunk wins: the refusal is not
    # pre-empted onto a run retrieval actually answered.
    assert (
        retrieve_blocking_failure(
            [record, {"chunks": 2, "mapping_failures": 0}]
        )
        is None
    )
    # The tool seam rebuilds records through ``retrieve_outcomes_delta``;
    # dropping the flag there would erase the decision's only path to it.
    assert retrieve_outcomes_delta([record], baseline=0) == [record]


def test_the_retrieve_tool_forwards_the_community_fact_into_the_run_record() -> (
    None
):
    """The tool seam itself — not a hand-built record — must carry a
    recorded global 综述 report-index refusal onto ``retrieve_outcomes``:
    the end-to-end mirror of the Fast 抽图 tool-seam test and the
    decision-layer test above. A run over ``FakeRetrievePort
    (community_summary_unready=True)`` flows port flag →
    ``copy_recorded_job_facts`` → run record → the no-chunk run resolving
    to ``community_summary_index_failure``, all hermetically (the live
    cross-repo case proves the real server's wire frame)."""
    from fin.host.graph import _make_retrieve_tool
    from fin.retrieve.fake_port import FakeRetrievePort
    from fin.retrieve.hit_mapping import MappingFailureLedger

    tool = _make_retrieve_tool(
        FakeRetrievePort(community_summary_unready=True),
        mapping_ledger=MappingFailureLedger(),
    )
    command = tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "跨多份材料的综述问题"},
            "id": "c-community-unready",
            "type": "tool_call",
        }
    )
    assert command.update["retrieve_outcomes"] == [
        {
            "chunks": 0,
            "mapping_failures": 0,
            RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
        }
    ]
    assert (
        retrieve_blocking_failure(command.update["retrieve_outcomes"])
        == COMMUNITY_SUMMARY_INDEX_FAILURE
    )


def test_the_five_retrieve_side_codes_are_mutually_distinct() -> None:
    """The SSOT guard for 具名失败 (spec Boundaries): the five codes are
    five different public tokens — an accidental alias would merge two
    product facts into one exit the operator cannot act on."""
    codes = {
        CORPUS_NOT_READY,
        MAPPING_DROP,
        TIER_NOT_OPEN,
        REAL_DB_NOT_WIRED,
        COMMUNITY_SUMMARY_INDEX_FAILURE,
    }
    assert codes == {
        "corpus_not_ready",
        "mapping_drop",
        "tier_not_open",
        "real_db_not_wired",
        "community_summary_index_failure",
    }
