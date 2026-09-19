"""C 综述 / D 真数据库 未开未接 是具名失败, 不是文档检索 (T6, spec SC9).

SC9: "C and D remain unused: if invoked, named 未开/未接, no fake SQL
retrieve." The wayfinder doc (闭环与分档 §1) fixes both halves of the
contract:

* "分类输出仅为 ``A|B|C|D``（不确定则澄清）；每档一套问答模板。
  **禁止开放业务意图桶**" — C/D are asked for by NAME, never inferred
  from business intent.
* "A/C/D 未开或未就绪：**具名失败**（查找未开 / 综述未开 / 真库未接），
  不得一律 ``unknown_locator`` + 空 citations."

The refusal is a 400 (``attachment_not_found``'s door: the request
names something this product cannot serve), NOT a 409 — the SPA's 409
path owns exactly two meanings (a held run, the 知识 MCP gate) and a
new 409 token would be reported to the operator as 「还有一轮在运行」,
a false claim (``web/src/api.ts`` sendConflictError).

So this ticket implements the smallest honest surface: the send door
accepts an explicit 档 selector and refuses a named C/D BEFORE the run
starts, with the typed-JSON body every other workspace refusal uses.
No classifier is built — a request that merely TALKS about summaries
or a real database is not evidence that C/D was invoked, and guessing
would be the 开放业务意图桶 the doc forbids.

A is 查找, which the same doc leaves openable on the default stack; it
is deliberately NOT refused here (see
``fin.host.named_failures.TIER_UNAVAILABLE_CODE``).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.named_failures import (
    REAL_DB_NOT_WIRED,
    TIER_NOT_OPEN,
    TIER_UNAVAILABLE_CODE,
    RETRIEVE_TIERS,
    unavailable_tier,
)


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
    from fin.host.http import get_feedback_store, get_thread_store
    from fin.host.feedback import InMemoryFeedbackStore
    from fin.host.thread_store import InMemoryThreadStore

    thread_store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: thread_store
    http_app.dependency_overrides[get_feedback_store] = (
        lambda: InMemoryFeedbackStore()
    )
    return thread_store


@pytest.fixture
def graph(http_app: FastAPI) -> Any:
    from fin.host.http import get_graph

    built = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="answer")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: built
    return built


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "q"}).json()["id"]


# ----- the pure decision -----


def test_no_requested_tier_is_not_a_tier_refusal() -> None:
    assert unavailable_tier(()) is None


@pytest.mark.parametrize("tier", ["C", "c"])
def test_a_requested_review_tier_is_unavailable(tier: str) -> None:
    assert unavailable_tier([tier]) == "C"


@pytest.mark.parametrize("tier", ["D", "d"])
def test_a_requested_real_db_tier_is_unavailable(tier: str) -> None:
    assert unavailable_tier([tier]) == "D"


@pytest.mark.parametrize("tier", ["A", "B"])
def test_the_open_tiers_are_not_refused(tier: str) -> None:
    """A 查找 and B 知识问答 are the 阶段一 path; naming them is not a
    failure."""
    assert unavailable_tier([tier]) is None


def test_an_unknown_token_is_not_guessed_at() -> None:
    """Only the closed tier list can request a 档 — an arbitrary
    string is never read as C/D (禁止开放业务意图桶)."""
    assert unavailable_tier(["summary", "sql", "database"]) is None


def test_the_closed_tier_list_is_a_b_c_d() -> None:
    assert RETRIEVE_TIERS == ("A", "B", "C", "D")


# ----- T1 (search-mode-retrieve): the public C-not-ready code -----


def test_community_summary_index_failure_is_public_with_its_wire_token() -> (
    None
):
    """The code is a public wire token (spec Boundaries): asserted by
    NAME and exact value, never by matching prose."""
    import fin.host.named_failures as named_failures

    assert named_failures.COMMUNITY_SUMMARY_INDEX_FAILURE == (
        "community_summary_index_failure"
    )
    assert "COMMUNITY_SUMMARY_INDEX_FAILURE" in named_failures.__all__


def test_community_summary_index_failure_is_not_tier_not_open() -> None:
    """``tier_not_open`` stays「档未发/点了关闭的档」(routing-time 400);
    the C not-ready code names the failed community-summary JOB.
    Collapsing them would tell an operator to open a 档 that IS open."""
    from fin.host.named_failures import (
        COMMUNITY_SUMMARY_INDEX_FAILURE,
        TIER_NOT_OPEN,
        TIER_UNAVAILABLE_CODE,
    )

    assert COMMUNITY_SUMMARY_INDEX_FAILURE != TIER_NOT_OPEN
    # It is a runtime not-ready fact, not a routing table entry: the
    # 档-unavailable map keeps its two「未开」codes only.
    assert COMMUNITY_SUMMARY_INDEX_FAILURE not in TIER_UNAVAILABLE_CODE
    assert COMMUNITY_SUMMARY_INDEX_FAILURE not in TIER_UNAVAILABLE_CODE.values()


def test_community_summary_index_failure_does_not_collapse_into_unknown_locator() -> (
    None
):
    """具名失败 means the run/错误 surface shows its OWN copy: the code
    must ride the SSE ``reason`` (the existing passthrough in
    ``workspace_http``) and resolve through ``ordinary_person_copy``,
    never falling through to the verifier's ``unknown_locator`` wording
    or an empty string."""
    from fin.host.named_failures import COMMUNITY_SUMMARY_INDEX_FAILURE
    from fin.host.run_payload import ORDINARY_PERSON_COPY, ordinary_person_copy

    assert COMMUNITY_SUMMARY_INDEX_FAILURE in ORDINARY_PERSON_COPY
    copy = ordinary_person_copy(
        "refuse", COMMUNITY_SUMMARY_INDEX_FAILURE
    )
    assert copy.strip()
    assert copy != ORDINARY_PERSON_COPY["unknown_locator"]
    # Ordinary-person prose: the internal token never leaks (same rule
    # as ``test_numeric_gate_fail_copy_is_ordinary_prose``).
    assert COMMUNITY_SUMMARY_INDEX_FAILURE not in copy


def test_the_send_schema_enum_is_derived_from_the_tier_ssot() -> None:
    """Drift guard: the send door's enum must BE the SSOT's closed set.

    Re-spelling the four tokens in the Pydantic model would let a 档
    added to ``RETRIEVE_TIERS`` keep validating against the old set —
    the drift this asserts against. Both faces are read here, so the
    test fails whichever side is edited alone.
    """
    from typing import get_args

    from fin.host.named_failures import RetrieveTier
    from fin.host.workspace_http import SendMessageRequest

    field = SendMessageRequest.model_fields["tiers"]
    schemas = [field.annotation, *get_args(field.annotation)]

    assert get_args(RetrieveTier) == RETRIEVE_TIERS
    # The Literal the SSOT derives is what the field actually carries:
    # edit either side alone and this stops holding.
    assert any(
        get_args(schema) == RETRIEVE_TIERS for schema in schemas
    ), schemas


def test_every_tier_code_key_is_a_tier_the_door_accepts() -> None:
    """A code for a 档 outside the closed list would be unreachable:
    the send schema rejects the token before the refusal can fire."""
    assert set(TIER_UNAVAILABLE_CODE) <= set(RETRIEVE_TIERS)


# ----- the send door -----


def test_naming_tier_c_refuses_tier_not_open(
    client: TestClient, store, graph
) -> None:
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "Summarize the filings", "tiers": ["C"]},
    )

    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": TIER_NOT_OPEN}


def test_naming_tier_d_refuses_real_db_not_wired(
    client: TestClient, store, graph
) -> None:
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "How many rows are in the sales table?", "tiers": ["D"]},
    )

    assert r.status_code == 400, r.text
    assert r.json() == {"error": REAL_DB_NOT_WIRED}


def test_the_two_tier_refusals_are_distinguishable(
    client: TestClient, store, graph
) -> None:
    """The point of SC9: 综述未开 and 真库未接 are different product
    facts and must not collapse into one token."""
    thread_id = _new_thread(client)

    review = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["C"]},
    )
    real_db = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["D"]},
    )

    assert review.json()["error"] != real_db.json()["error"]
    assert {review.json()["error"], real_db.json()["error"]} == {
        TIER_NOT_OPEN,
        REAL_DB_NOT_WIRED,
    }


def test_the_refusal_happens_before_any_run_starts(
    client: TestClient, store, graph
) -> None:
    """A refused 档 must not leave a turn row: no run was invoked, so
    the thread history stays empty."""
    thread_id = _new_thread(client)

    client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "Summarize the filings", "tiers": ["C"]},
    )

    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["turns"] == []


def test_naming_an_open_tier_does_not_refuse(
    client: TestClient, store, graph
) -> None:
    """A/B ride the ordinary send path (the run, not a refusal)."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?", "tiers": ["B"]},
    )

    assert r.status_code == 200


def test_a_send_without_a_tier_selector_is_unchanged(
    client: TestClient, store, graph
) -> None:
    """The overwhelming majority of sends name no 档; they must not
    learn a new failure mode."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?"},
    )

    assert r.status_code == 200


def test_a_question_merely_about_summaries_is_not_a_tier_request(
    client: TestClient, store, graph
) -> None:
    """Negative case for the forbidden 业务意图桶: prose about
    summarizing is not an explicit C selector."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "Summarize the revenue trend across the filings"},
    )

    assert r.status_code == 200


def test_an_unknown_tier_token_is_rejected_as_input(
    client: TestClient, store, graph
) -> None:
    """The selector is a closed enum: a bogus token is an INPUT error
    (422), never silently read as C/D."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["Z"]},
    )

    assert r.status_code == 422
