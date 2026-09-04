"""Tests for the ``/compact`` intercept (plan T2, ADR 0044).

Contract under test (docs/adr/0044-session-compaction-token-occupancy.md):

* ``/compact`` is intercepted BEFORE Soft-Track classify and runs the
  SAME compaction gate as the >= 90% auto path — same eviction units,
  same summary write, same ``RemoveMessage`` write-back — WITHOUT
  waiting for the 90% trigger (a thread already under 90% may still
  compact; the operator asked).
* ``/compact`` is NOT classified as a user question and is not a
  research tool: the scripted research model is never invoked, no
  ``tool_call`` / ``tool_result`` frames flow, no ``track`` rides the
  status frame.
* Concurrency: the 409 ``concurrent_run`` contract is unchanged.
* Summary failure is typed and graceful: receipt copy, thread usable.
* Empty / trivial payload: a no-op receipt, no crash.
* Eviction writes back through the checkpointer: checkpoint messages
  actually shrink and the checkpoint stays terminal (no resume banner).

Budget override: same trick as ``test_session_compaction_gate.py`` —
``session_token_budget(1_600)`` so fixtures cross the thresholds
without 230k-char histories. Verified: the ContextVar propagates
through starlette's ``TestClient`` into the route handler.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.compaction import _SUMMARY_MARKER
from fin.host.session_compaction import session_token_budget


# Same fixture ids as tests/host/test_workspace_http.py.
AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

TEST_BUDGET = 1_600  # estimated tokens; matches the T1 gate tests


# ----- fixtures (mirrors test_workspace_http.py; tests are not a package) -----


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, ThreadStore

    store: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store

    default_graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: default_graph
    return store


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    """Parse a ``text/event-stream`` body into ``(event, data)`` pairs."""
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
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


# ----- /compact fixture history -----


_TOOL_PAYLOAD = "p" * 12_000  # 3_000 estimated tokens per big ToolMessage


def _tool_pair(ai_text: str, tool_body: str, call_id: str) -> list:
    return [
        AIMessage(
            content=ai_text,
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "document_id": AAPL_2022Q3,
                        "version": AAPL_2022Q3_VERSION,
                        "page": 1,
                        "page_limit": 1,
                    },
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=tool_body, tool_call_id=call_id),
    ]


def _fat_history() -> list:
    """~9_200 estimated tokens — far above the 90% trigger at 1_600."""
    messages: list = []
    for index in range(3):
        messages += _tool_pair(
            f"calling read_file {index}", _TOOL_PAYLOAD, f"c-seed-{index}"
        )
    messages += _tool_pair(
        "calling read_file 3",
        json.dumps({"document_id": AAPL_2022Q3, "page": 1}),
        "c-seed-3",
    )
    messages.append(AIMessage(content="x" * 300))
    messages.append(HumanMessage(content="Break down AAPL 2023 Q3 revenue"))
    return messages


def _mid_history() -> list:
    """~75% occupancy at 1_600 tokens — UNDER the 90% trigger, ABOVE
    the 40% floor, with an evictable oldest pair."""
    # pair_old: AI 80 chars (20 tokens) + tool 2_400 chars (600 tokens)
    old = _tool_pair("old call", "o" * 2_400, "c-old")
    # pair_new: AI 80 chars (20 tokens) + tool 2_400 chars (600 tokens)
    new = _tool_pair("new call", "n" * 2_400, "c-new")
    return old + new + [HumanMessage(content="z" * 40)]


def _install_compact_graph(
    http_app: FastAPI,
    *,
    script: list[AIMessage] | None = None,
    summarizer: Any = None,
) -> Any:
    from fin.host.http import get_graph

    model = ScriptedChatModel(
        script=script if script is not None else [AIMessage(content="RESEARCH-ANSWER-SENTINEL")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        summarizer=summarizer,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _seed_checkpoint(g: Any, thread_id: str, messages: list, *, as_node: str = "model") -> None:
    g._graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": messages},
        as_node=as_node,
    )


def _checkpoint_messages(g: Any, thread_id: str) -> list:
    values = g._graph.get_state({"configurable": {"thread_id": thread_id}}).values or {}
    return list(values.get("messages") or [])


def _post_compact(client: TestClient, thread_id: str, question: str = "/compact"):
    with session_token_budget(TEST_BUDGET):
        with client.stream(
            "POST",
            f"/api/threads/{thread_id}/messages",
            json={"question": question},
        ) as r:
            status_code = r.status_code
            content_type = r.headers.get("content-type", "")
            body = "".join(r.iter_text()) if status_code == 200 else ""
    return status_code, content_type, body


class ExplodingSummarizer:
    def invoke(self, messages: list) -> AIMessage:
        raise RuntimeError("model backend unavailable")


# ===== the intercept =====


def test_compact_runs_gate_and_is_not_classified_as_a_question(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """/compact on a fat research thread runs the gate, never calls the
    research model (no classify, no research run), and emits no tool
    frames. The scripted sentinel answer must not reach the wire."""
    summarizer = ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")])
    g = _install_compact_graph(http_app, summarizer=summarizer)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _seed_checkpoint(g, tid, _fat_history())
    before = _checkpoint_messages(g, tid)

    status_code, content_type, body = _post_compact(client, tid)

    assert status_code == 200
    assert content_type.startswith("text/event-stream")

    events = _parse_sse_events(body)
    event_types = [name for name, _ in events]
    assert event_types[-1] == "citations"  # terminal frame closes the reader

    # Receipt frames only — no tool frames, no streamed research prose.
    assert "RESEARCH-ANSWER-SENTINEL" not in body
    assert "tool_call" not in event_types
    assert "tool_result" not in event_types

    status_event = next(data for name, data in events if name == "status")
    assert status_event["status"] == "done"
    # Not a research/classify turn: no track rides the status frame.
    assert "track" not in status_event

    # The checkpoint actually shrank and carries the summary.
    after = _checkpoint_messages(g, tid)
    assert len(after) < len(before)
    assert isinstance(after[0], SystemMessage)
    assert _SUMMARY_MARKER in after[0].content

    # The receipt turn is persisted with an empty track decision.
    runs = fresh_store.list_runs(tid)
    assert len(runs) == 1
    assert runs[0].status == "done"
    assert runs[0].track_decision == {}
    assert runs[0].question == "/compact"


def test_compact_below_trigger_still_compacts_when_gate_produces_update(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A thread already UNDER the 90% trigger may still compact: the
    operator asked, so the gate runs without waiting for 90% and
    evicts down toward the 40% floor."""
    summarizer = ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")])
    g = _install_compact_graph(http_app, summarizer=summarizer)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _seed_checkpoint(g, tid, _mid_history())
    before = _checkpoint_messages(g, tid)

    status_code, _ct, body = _post_compact(client, tid)

    assert status_code == 200
    events = _parse_sse_events(body)
    answer = next(data for name, data in events if name == "answer")
    # Success receipt, not the no-op copy.
    assert "压缩" in answer["text"]
    assert "暂不需要压缩" not in answer["text"]

    after = _checkpoint_messages(g, tid)
    assert len(after) < len(before)
    assert isinstance(after[0], SystemMessage)
    assert _SUMMARY_MARKER in after[0].content
    # Pair integrity: the newest pair survives complete (AI + ToolMessage).
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "c-new" for m in after)
    assert any(
        isinstance(m, AIMessage)
        and any(c.get("id") == "c-new" for c in (m.tool_calls or []))
        for m in after
    )


def test_compact_trivial_payload_is_noop_receipt(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A payload where the gate produces no update (nothing evictable)
    gets a no-op receipt and the checkpoint is untouched."""
    summarizer = ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")])
    g = _install_compact_graph(http_app, summarizer=summarizer)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    small = [HumanMessage(content="hi"), AIMessage(content="x" * 80)]
    _seed_checkpoint(g, tid, small)
    before = _checkpoint_messages(g, tid)

    status_code, _ct, body = _post_compact(client, tid)

    assert status_code == 200
    events = _parse_sse_events(body)
    answer = next(data for name, data in events if name == "answer")
    assert "暂不需要压缩" in answer["text"]
    assert _checkpoint_messages(g, tid) == before


def test_compact_summary_failure_is_graceful_receipt_thread_usable(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A failing summarizer is a typed, graceful outcome: receipt copy
    (NOT a 500, NOT a refuse), checkpoint unchanged, thread usable
    afterwards."""
    g = _install_compact_graph(
        http_app,
        summarizer=ExplodingSummarizer(),
        script=[AIMessage(content="answer after compact")],
    )

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _seed_checkpoint(g, tid, _fat_history())
    before = _checkpoint_messages(g, tid)

    status_code, _ct, body = _post_compact(client, tid)

    assert status_code == 200
    events = _parse_sse_events(body)
    status_event = next(data for name, data in events if name == "status")
    assert status_event["status"] == "done"  # turn not refused
    answer = next(data for name, data in events if name == "answer")
    assert "压缩未完成" in answer["text"]
    # The checkpoint is untouched — no half-written compaction.
    assert _checkpoint_messages(g, tid) == before

    # Thread usable after: an ordinary send still completes.
    with session_token_budget(TEST_BUDGET):
        with client.stream(
            "POST",
            f"/api/threads/{tid}/messages",
            json={"question": "What was AAPL net sales?"},
        ) as r:
            follow_body = "".join(r.iter_text())
    follow_events = _parse_sse_events(follow_body)
    assert any(
        name == "status" and data.get("status") == "done"
        for name, data in follow_events
    )


def test_compact_concurrent_run_returns_409(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The 409 concurrent_run contract is unchanged: /compact while a
    run is in flight is a plain application/json 409, no SSE."""
    g = _install_compact_graph(http_app)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _seed_checkpoint(g, tid, _fat_history())

    lock = g._lock_for(tid)
    lock.acquire()
    try:
        status_code, content_type, _body = _post_compact(client, tid)
        assert status_code == 409
        assert content_type.startswith("application/json")
    finally:
        lock.release()


def test_compact_empty_thread_noop_receipt_no_crash(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A thread with no checkpoint at all: no-op receipt, no crash, and
    no checkpoint fabricated by the intercept."""
    _install_compact_graph(http_app)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]

    status_code, _ct, body = _post_compact(client, tid)

    assert status_code == 200
    events = _parse_sse_events(body)
    answer = next(data for name, data in events if name == "answer")
    assert "暂不需要压缩" in answer["text"]
    # No compaction write happened -> still no checkpoint.
    from fin.host.http import get_graph

    graph = http_app.dependency_overrides[get_graph]()
    assert graph.occupancy(tid) is None


def test_compact_checkpoint_shrinks_and_stays_terminal(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """After an evicting /compact the checkpoint is shorter, the
    summary leads, and no pending nodes remain (no resume banner)."""
    summarizer = ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")])
    g = _install_compact_graph(http_app, summarizer=summarizer)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _seed_checkpoint(g, tid, _fat_history())
    before = _checkpoint_messages(g, tid)

    status_code, _ct, _body = _post_compact(client, tid)

    assert status_code == 200
    after = _checkpoint_messages(g, tid)
    assert len(after) < len(before)
    assert isinstance(after[0], SystemMessage)
    assert g.pending_nodes(tid) == ()


def test_compact_mid_run_pending_thread_is_noop_not_clobbered(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A crashed / mid-run thread (pending checkpoint nodes) must not
    have its resumable state clobbered by /compact: no-op receipt."""
    summarizer = ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")])
    g = _install_compact_graph(http_app, summarizer=summarizer)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    # as_node="tools" leaves pending next=('compaction_gate',) — a
    # resumable mid-run checkpoint.
    _seed_checkpoint(g, tid, _fat_history(), as_node="tools")
    assert g.pending_nodes(tid) == ("compaction_gate",)
    before = _checkpoint_messages(g, tid)

    status_code, _ct, body = _post_compact(client, tid)

    assert status_code == 200
    events = _parse_sse_events(body)
    answer = next(data for name, data in events if name == "answer")
    assert "未完成的回合" in answer["text"]
    assert _checkpoint_messages(g, tid) == before
    assert g.pending_nodes(tid) == ("compaction_gate",)


def test_compact_whitespace_and_case_insensitive_match(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """The command matches after surrounding whitespace and
    case-insensitively (' /Compact ' == '/compact')."""
    summarizer = ScriptedChatModel(script=[AIMessage(content="EVICTED-SUMMARY")])
    g = _install_compact_graph(http_app, summarizer=summarizer)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    tid = created["id"]
    _seed_checkpoint(g, tid, _fat_history())

    status_code, _ct, body = _post_compact(client, tid, question="  /Compact  ")

    assert status_code == 200
    # The intercept handled the command — the research model never ran.
    assert "RESEARCH-ANSWER-SENTINEL" not in body
    assert len(_checkpoint_messages(g, tid)) < len(_fat_history())


def test_compact_does_not_rewrite_default_title(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A first-ever send of /compact must not set the thread title to
    the command string — the title update guard skips /compact."""
    _install_compact_graph(http_app)

    created = client.post("/api/threads", json={"first_message": ""}).json()
    tid = created["id"]

    status_code, _ct, _body = _post_compact(client, tid)
    assert status_code == 200

    detail = client.get(f"/api/threads/{tid}").json()
    assert detail["title"] != "/compact"
