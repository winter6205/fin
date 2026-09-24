"""未开/未接 档 是具名失败, 不是文档检索 (T6, spec SC9) — 综述开档后修订版.

SC9 原始形态是 "``global`` and ``sql`` remain unused: if invoked, named
未开/未接, no fake SQL retrieve." fin task 4 把 ``global`` 综述开成了真实的
综述报告 arm: 它不再是路由期 400, 而是走普通 run。开档后本文件锁定的
不变量随之位移:

* 路由期具名 400 只剩 ``sql`` 真数据库 一个 plan slot — 真库未接仍是
  ``real_db_not_wired``, 不得一律 ``unknown_locator`` + 空 citations。
* ``global`` 综述 现在与 lexical/graph 一样是 OPEN 档: 点名它启动 run,
  绝不返回 400; 它的运行时 未就绪 exit 是另一个 token
  (``community_summary_index_failure``, 见下方 T1 段落), 不是「档未开」。
* 分类输出仍是 closed 档 set (不确定则澄清), 未开档 只按 NAME 询问,
  **禁止开放业务意图桶** — 单纯谈论 summaries/database 不算点名 档。

The refusal is a 400 (``attachment_not_found``'s door: the request
names something this product cannot serve), NOT a 409 — the SPA's 409
path owns exactly two meanings (a held run, the 知识 MCP gate) and a
new 409 token would be reported to the operator as 「还有一轮在运行」,
a false claim (``web/src/api.ts`` sendConflictError).

``tier_not_open`` 仍是公开 token (closed vocabulary + 具名失败 distinctness
guard 保留它), 但开档后它 NO LONGER has a routing-time producer: the send
door looks the code up in ``TIER_UNAVAILABLE_CODE`` (now ``sql`` only), so
a 综述 pin can never yield it again.

lexical 查找 / graph 知识问答 / global 综述 are the open 档 the same doc
leaves serveable; they are deliberately NOT refused here (see
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
    RETRIEVE_TIERS,
    TIER_UNAVAILABLE_CODE,
    unavailable_tier,
)
from fin.host.search_mode_decision import normalize_search_mode
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import resolve_search_mode
from search_mode_pins import RETIRED_LETTERS


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


def test_a_requested_review_tier_is_no_longer_refused() -> None:
    """The lookup is by the LASTING token, exactly. 综述 was opened into a
    real report arm, so ``global`` left ``TIER_UNAVAILABLE_CODE``: naming
    it at the door no longer refuses — it routes. The value is read
    exactly (case variant / padding is refused upstream as a 422 — see
    ``test_a_retired_letter_send_is_a_typed_refusal``), and this helper now
    names NOTHING for it."""
    assert unavailable_tier(["global"]) is None


def test_a_requested_real_db_tier_is_unavailable() -> None:
    assert unavailable_tier(["sql"]) == "sql"


@pytest.mark.parametrize("tier", ["lexical", "graph"])
def test_the_open_tiers_are_not_refused(tier: str) -> None:
    """查找 and 知识问答 are the 阶段一 path; naming them is not a
    failure."""
    assert unavailable_tier([tier]) is None


@pytest.mark.parametrize("tier", RETIRED_LETTERS)
def test_a_retired_letter_is_not_a_tier_at_this_seam(tier: str) -> None:
    """Zero coexistence reaches the refusal helper too: a bare letter is
    no longer a 档 spelling, so it is not read as 综述/真数据库 either —
    this helper names nothing for it (the door refuses it by TYPE before
    this lookup could matter)."""
    assert unavailable_tier([tier]) is None


def test_an_unknown_token_is_not_guessed_at() -> None:
    """Only the closed tier list can request a 档 — an arbitrary
    string is never read as 综述 / 真数据库 (禁止开放业务意图桶)."""
    assert unavailable_tier(["summary", "database"]) is None


def test_the_closed_tier_list_is_the_lasting_set() -> None:
    """The letters left the closed set with the rename: the four lasting
    tokens ARE the list the port forwards from, and nothing else can join
    it without being a lasting token."""
    assert RETRIEVE_TIERS == ("lexical", "graph", "global", "sql")


def test_the_door_accepts_exactly_the_closed_set() -> None:
    """Zero coexistence, proven on the door MODEL rather than on a tuple:
    each retired letter is a typed validation error the schema raises before
    any handler runs, and the lasting tokens validate. A letter that got
    past this door is a wiring mistake, and the only honest answer to a
    mistake is a named one.
    """
    from pydantic import ValidationError

    from fin.host.workspace_http import SendMessageRequest

    for letter in RETIRED_LETTERS:
        with pytest.raises(ValidationError, match="tiers"):
            SendMessageRequest(question="q", tiers=[letter])
        # the door's own normalizing helper agrees: a letter resolves to
        # nothing, it is never quietly read as a 档 on the way in.
        assert normalize_search_mode(letter) is None
    for token in RETRIEVE_TIERS:
        assert SendMessageRequest(
            question="q", tiers=[token]
        ).tiers == [token]


# ----- T1 (search-mode-retrieve): the public 综述-not-ready code -----


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
    the 综述 not-ready code names the failed community-summary JOB.
    Collapsing them would tell an operator to open a 档 that IS open."""
    from fin.host.named_failures import (
        COMMUNITY_SUMMARY_INDEX_FAILURE,
        TIER_NOT_OPEN,
        TIER_UNAVAILABLE_CODE,
    )

    assert COMMUNITY_SUMMARY_INDEX_FAILURE != TIER_NOT_OPEN
    # It is a runtime not-ready fact, not a routing table entry: the
    # 档-unavailable map holds only the surviving plan-slot code (sql),
    # and the 综述 runtime token is never one of them.
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
    """Drift guard: the send door's enum must BE the SSOT's closed set, and
    so must the port's Literal.

    Re-spelling the tokens in the Pydantic model would let a 档 added to
    ``RETRIEVE_TIERS`` keep validating against the old set — the drift this
    asserts against. The door's accepted set and the port's forwarded set are
    one closed set, so the guard pins both halves at once: what the door
    accepts IS what the port forwards."""
    from typing import get_args

    from fin.host.named_failures import RetrieveTier
    from fin.host.workspace_http import SendMessageRequest

    field = SendMessageRequest.model_fields["tiers"]
    schemas = [field.annotation, *get_args(field.annotation)]

    assert get_args(RetrieveTier) == RETRIEVE_TIERS
    assert any(
        get_args(schema) == RETRIEVE_TIERS for schema in schemas
    ), schemas


def test_a_pinned_tier_reaches_the_port_as_its_token(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """The lasting token end to end, on the real HTTP path: the door
    answers 200, the run's audit reads the token, that value is one the
    port's own seam accepts, and the wire call carries exactly it —
    nothing rewrites the 档 on the way from the door to the port."""
    from fin.host.http import get_graph

    port = FakeRetrievePort(
        hits_by_query={"revenue": []},
        known_document_ids=KNOWN_IDS,
    )
    built = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": "revenue"},
                            "id": "c-pin",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="The answer."),
            ]
        ),
        adapter=port,
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: built
    thread_id = _new_thread(client)

    sent = client.post(
        f"/api/threads/{thread_id}/messages",
        json={
            "question": "What was AAPL net sales in 2023 Q3?",
            "tiers": ["graph"],
        },
    )

    assert sent.status_code == 200, sent.text
    assert port.calls, "the grounded retrieve must have reached the port"
    audit = built._graph.get_state(
        {"configurable": {"thread_id": thread_id}}
    ).values["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["signals"].get("pinned") is True
    assert resolve_search_mode(audit["search_mode"]) == "graph"
    # POSITIVE: the door's pin reaches the WIRE call as exactly that token.
    assert [call["search_mode"] for call in port.calls] == [
        "graph"
    ] * len(port.calls), [call["search_mode"] for call in port.calls]


def test_every_tier_code_key_is_a_tier_the_door_accepts() -> None:
    """A code for a 档 outside the closed list would be unreachable:
    the send schema rejects the token before the refusal can fire."""
    assert set(TIER_UNAVAILABLE_CODE) <= set(RETRIEVE_TIERS)


# ----- the send door -----


def test_naming_tier_global_starts_a_run_not_a_refusal(
    client: TestClient, store, graph
) -> None:
    """The send door takes the LASTING token, and 综述 is now OPEN: a
    ``global`` pin routes like graph/lexical — it is not 档未开, and the
    run actually starts (it is not silently served by a fallback 档 and
    not refused with a routing 400)."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "Summarize the filings", "tiers": ["global"]},
    )

    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers["content-type"], r.headers
    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["turns"], "opening 综述 must let the run start, not refuse it"


def test_naming_tier_sql_refuses_real_db_not_wired(
    client: TestClient, store, graph
) -> None:
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "How many rows are in the sales table?", "tiers": ["sql"]},
    )

    assert r.status_code == 400, r.text
    assert r.json() == {"error": REAL_DB_NOT_WIRED}


def test_the_global_and_sql_pins_do_not_collapse_to_one_exit(
    client: TestClient, store, graph
) -> None:
    """SC9's distinguishable-facts point survives 综述 being opened, with
    the reversal the open forces: a ``sql`` pin is STILL a named routing
    400 (真库未接), while a ``global`` pin is no longer a refusal at all
    (综述 open). They must not collapse into one answer — one starts a
    run, the other refuses before any run."""
    thread_id = _new_thread(client)

    review = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["global"]},
    )
    real_db = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["sql"]},
    )

    assert review.status_code == 200, review.text
    assert "text/event-stream" in review.headers["content-type"], review.headers
    assert real_db.status_code == 400, real_db.text
    assert real_db.json() == {"error": REAL_DB_NOT_WIRED}


def test_the_refusal_happens_before_any_run_starts(
    client: TestClient, store, graph
) -> None:
    """A refused 档 must not leave a turn row: no run was invoked, so
    the thread history stays empty. 综述 opened, so the surviving plan
    slot 真数据库 (``sql``) carries this invariant — its named 400 fires
    before the graph runs."""
    thread_id = _new_thread(client)

    client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "How many rows are in the sales table?", "tiers": ["sql"]},
    )

    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["turns"] == []


@pytest.mark.parametrize("tier", ["lexical", "graph", "global"])
def test_naming_an_open_tier_does_not_refuse(
    tier: str, client: TestClient, store, graph
) -> None:
    """查找、知识问答 and 综述 ride the ordinary send path (the run, not a
    refusal): an open lasting token is never 档未开."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "What was AAPL net sales in 2023 Q3?", "tiers": [tier]},
    )

    assert r.status_code == 200, r.text


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
    (422), never silently read as global / sql."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": ["Z"]},
    )

    assert r.status_code == 422


@pytest.mark.parametrize(
    "token",
    [
        *RETIRED_LETTERS,
        "GLOBAL",
        "SQL",
        " sql ",
        "LEXICAL",
        "GRAPH",
    ],
)
def test_a_retired_letter_send_is_a_typed_refusal(
    token: str, client: TestClient, store, graph
) -> None:
    """Zero coexistence at the door: ``tiers: ["A"]`` is no longer a
    spelling that means something — it is the same kind of bad selector
    as ``"Z"``, refused by the schema (422) before any run starts and
    before the 400 refusal table is consulted. The enum is typed to the
    closed set, so a non-exact spelling (case variant or padding included)
    is refused by type, not normalized: this is the ONE seam that decides
    what a selector may spell, and the refusal names the accepted set so
    the operator can read it."""
    thread_id = _new_thread(client)

    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "q", "tiers": [token]},
    )

    assert r.status_code == 422, r.text
    body = r.text
    for lasting in RETRIEVE_TIERS:
        assert lasting in body, body
    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["turns"] == []
