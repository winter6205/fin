"""Fast propose 的 HTTP send 面（search-mode host T4 follow-up）。

Opt-in only (``uv run pytest -q -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_search_mode_choice_window.py`` HTTP lane).

Ticket 4 落地了 graph 侧的 propose 窗（seed
``extract_method_proposal={"proposed":"fast"}`` + grounded →
``extract_method_choice`` interrupt），但 send 面没有请求字段能注入这个
**state 字段** seed，工作台因此不可达。本文件锁 HTTP 契约：

* ``SendMessageRequest.propose_extract_method="fast"`` → 该 run 的 input
  state 携带 seed；pin 住开放档（``tiers:["B"]``）时 SSE 的唯一窗是
  方法窗（options standard/fast，title 区别于档窗）；
* resume ``{"pick":"fast"}`` → 审计 ``applied is False`` +
  ``operator_confirmed True`` + ``picked "fast"``，run 走完
  （terminal 帧齐）；
* 不带字段 → 无方法窗（现状回归锁）；
* 非 grounded（greeting）带字段 → 不开窗（spec assumption 2）；
* 非法值 ``"turbo"`` → pydantic 422（closed Literal，同 ``tiers`` 风格），
  run 从不启动。

Boundary classes map to: empty/negative ->
``test_send_with_invalid_method_is_422_and_run_never_starts``;
normal -> ``test_send_with_propose_fast_streams_method_window``;
non-trigger -> ``test_send_without_field_has_no_method_window`` +
``test_greeting_send_with_field_completes_without_window``.
The 409/pending and redraw surfaces are T2's, already locked in
``test_search_mode_choice_window.py``; this ticket adds no new resume
semantics beyond feeding the SAME window.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Grounded by the track rules tier; ``tiers:["B"]`` pins the 档 so the
# ONLY possible window on these sends is the extract-method one.
AMBIGUOUS = "What was AAPL net sales in 2023 Q3?"

_ANSWER = [AIMessage(content="Net sales grew year over year.")]


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# HTTP lane fixtures (same shape as test_search_mode_choice_window.py)
# ---------------------------------------------------------------------------


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
    from fin.host.http import get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    thread_store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: thread_store
    return thread_store


def _install_window_graph(http_app: FastAPI, script: list[AIMessage]) -> Any:
    from fin.host.http import get_graph

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_IDS,
        enable_choice_window=True,
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


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
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def _new_thread(client: TestClient) -> str:
    created = client.post("/api/threads", json={"first_message": "研究"}).json()
    return created["id"]


def _state_values(installed: Any, thread_id: str) -> dict[str, Any]:
    return installed._graph.get_state(_cfg(thread_id)).values


# ---------------------------------------------------------------------------
# the send -> window -> resume round trip on the wire
# ---------------------------------------------------------------------------


def test_send_with_propose_fast_streams_method_window(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """The T4 HTTP gap: the request field seeds the run's state and the
    SSE ends on the METHOD window — options standard/fast, and the
    title/prompt name 抽图 so the SPA never paints it as a 档 picker."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "tiers": ["B"], "propose_extract_method": "fast"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(r.text)
    names = [name for name, _ in events]
    assert names[-1] == "choice_window"
    payload = events[-1][1]
    assert payload["kind"] == "choice_window"
    assert sorted(str(o.get("id")) for o in payload["options"] or []) == [
        "fast",
        "standard",
    ]
    window_text = f"{payload.get('title')}{payload.get('prompt')}"
    assert "抽图" in window_text
    # A paused run emits no terminal frames (T2 contract holds here too).
    assert "status" not in names
    assert "citations" not in names


def test_resume_fast_after_send_records_confirmed_proposal(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """ADR 0068 end-to-end on the wire: pick ``fast`` resumes the SAME
    run to completion and the audit reads proposed/picked fast,
    operator_confirmed True, applied False."""
    installed = _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    sent = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "tiers": ["B"], "propose_extract_method": "fast"},
    )
    assert _parse_sse_events(sent.text)[-1][0] == "choice_window"

    resumed = client.post(
        f"/api/threads/{thread_id}/resume", json={"pick": "fast"}
    )
    assert resumed.status_code == 200
    events = _parse_sse_events(resumed.text)
    names = [name for name, _ in events]
    assert "choice_window" not in names
    assert names[-1] == "citations"
    assert installed.interrupt_pending(thread_id) is None
    proposal = _state_values(installed, thread_id)["extract_method_proposal"]
    assert proposal["proposed"] == "fast"
    assert proposal["picked"] == "fast"
    assert proposal["applied"] is False
    assert proposal["operator_confirmed"] is True
    # The 档 pin is untouched by the method pick (Fast is never a 档).
    assert _state_values(installed, thread_id)["search_mode_decision"][
        "search_mode"
    ] == "B"


# ---------------------------------------------------------------------------
# non-triggers: today's wire stays byte-identical without the field
# ---------------------------------------------------------------------------


def test_send_without_field_has_no_method_window(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """Regression lock: an ordinary pinned grounded send never opens
    either window and its audit stays the per-run empty reset."""
    installed = _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "tiers": ["B"]},
    )
    assert r.status_code == 200
    names = [name for name, _ in _parse_sse_events(r.text)]
    assert "choice_window" not in names
    assert names[-1] == "citations"
    assert _state_values(installed, thread_id)["extract_method_proposal"] == {}


def test_seed_does_not_leak_to_next_send(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """``reset_input_state`` zeroes the seed channel every run: the
    follow-up send after a resolved propose window must NOT reopen it
    (the injection rides the per-run input dict, not the checkpoint)."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    sent = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "tiers": ["B"], "propose_extract_method": "fast"},
    )
    assert _parse_sse_events(sent.text)[-1][0] == "choice_window"
    client.post(f"/api/threads/{thread_id}/resume", json={"pick": "standard"})

    second = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "tiers": ["B"]},
    )
    assert second.status_code == 200
    names = [name for name, _ in _parse_sse_events(second.text)]
    assert "choice_window" not in names
    assert names[-1] == "citations"


def test_greeting_send_with_field_completes_without_window(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """Spec assumption 2 at the HTTP surface: the dialogue track reaches
    neither window — the seed rides a greeting and is ignored."""
    _install_window_graph(http_app, [AIMessage(content="Hi there.")])
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": "Hi!", "propose_extract_method": "fast"},
    )
    assert r.status_code == 200
    names = [name for name, _ in _parse_sse_events(r.text)]
    assert "choice_window" not in names
    assert names[-1] == "citations"


def test_send_with_invalid_method_is_422_and_run_never_starts(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """Closed-Literal input contract (same style as ``tiers``): a bogus
    method token is a pydantic 422 BEFORE any graph invoke — never a
    crash, never a silently ignored field."""
    installed = _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "propose_extract_method": "turbo"},
    )
    assert r.status_code == 422
    # No run started: nothing is pending and the thread has no state.
    assert installed.interrupt_pending(thread_id) is None
    assert not installed._graph.get_state(_cfg(thread_id)).values
