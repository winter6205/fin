"""Choice-window HITL after grounded clarify (spec search-mode-host, Ticket 2).

Opt-in only (``uv run pytest -q -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_search_mode_cascade.py``).

Contract covered here (ADR 0062 HITL, spec SC2 "clarify window" + SC4):

* grounded + ``decision_tier == "clarify"`` pauses the run BEFORE any
  model / retrieve call on a LangGraph ``interrupt`` whose payload is
  the choice window (``kind`` + non-empty ``options`` of ``{id,label}``,
  C/D present but marked unopened).
* ``Command(resume=pick)`` assigns the picked 档 (auditable
  ``decision_tier="operator_pick"`` + ``signals.resume_pick``) and the
  grounded leaf then proceeds (retrieve runs).
* an unknown pick REOPENS the window (never a silent C); a pick of an
  unopened 档 is the 具名失败 ``typed_fail`` recorded on the audit.
* the workspace SSE wire emits a ``choice_window`` frame; ``POST
  /threads/{id}/resume`` accepts ``{"pick": ...}``; a send while a
  window is pending keeps the 409 ``concurrent_run`` semantics; the
  body-less crash resume is untouched.
* optional pin (send ``tiers: ["B"]`` — a retired letter an old client
  still sends, or a lasting token — / ``configurable`` pin) assigns
  without opening the window (SC4).
* with the factory flag off (the default), the pre-T2 flow is intact —
  this is what keeps the default-collected suites green.

Five boundary classes (spec Boundaries "Test classes") map to:
empty -> ``test_ambiguous_grounded_ask_opens_choice_window`` (an ask
the cascade cannot name IS the empty/residual input here);
negative -> ``test_pick_of_unopened_tier_is_typed_fail_not_silent_c``;
overflow -> ``test_invalid_pick_reopens_window_then_valid_pick_resolves``
(repeated picks never accumulate a 档 — no silent default under any
number of retries);
concurrent -> ``test_send_while_choice_pending_returns_409``;
exception -> ``test_choice_window_off_by_default_grounded_clarify_completes``
(no interrupt machinery runs when the lane is off).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fin.host import ScriptedChatModel, compile_graph
from fin.host.named_failures import TIER_NOT_OPEN
from fin.host.state import reset_input_state

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Grounded by the track rules tier, unnamed by the 档 rules tier, no
# inherit on a fresh thread -> the cascade residual ``clarify``.
AMBIGUOUS = "What was AAPL net sales in 2023 Q3?"


def _cfg(thread_id: str, **configurable: Any) -> dict:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def _input(text: str) -> dict[str, Any]:
    return {**reset_input_state(text), "evidence": []}


def _interrupt_payload(out: dict[str, Any]) -> dict[str, Any] | None:
    """The first pending interrupt's payload, or ``None``."""
    interrupts = out.get("__interrupt__") or ()
    if not interrupts:
        return None
    return interrupts[0].value


def _compile(
    script: list[AIMessage],
    *,
    choice_window: bool = True,
    classify_model: Any | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        classify_model=classify_model,
        known_document_ids=KNOWN_IDS,
        enable_choice_window=choice_window,
    )


_ANSWER = [AIMessage(content="Net sales grew year over year.")]

_RETRIEVE_CALL = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "retrieve",
            "args": {"query": "AAPL net sales"},
            "id": "c-retrieve-window",
            "type": "tool_call",
        }
    ],
)


# ---------------------------------------------------------------------------
# graph lane: the clarify residual pauses on the choice window
# ---------------------------------------------------------------------------


def test_ambiguous_grounded_ask_opens_choice_window() -> None:
    g = _compile(_ANSWER)
    out = g.invoke(_input(AMBIGUOUS), config=_cfg("cw-ambiguous"))
    payload = _interrupt_payload(out)
    assert payload is not None
    assert out["track"] == "grounded"
    assert out["search_mode_decision"]["decision_tier"] == "clarify"
    # The pause happens BEFORE the grounded leaf ever runs.
    assert out["step_count"] == 0


def test_choice_window_payload_contract_ids_and_labels() -> None:
    """Payload contract (spec SC2 "choice window payload"): the host/
    graph payload alone must carry ``kind`` + non-empty ``options`` of
    ``{id, label}`` — no painted SPA is consulted anywhere here."""
    g = _compile(_ANSWER)
    out = g.invoke(_input(AMBIGUOUS), config=_cfg("cw-contract"))
    payload = _interrupt_payload(out)
    assert payload is not None
    assert payload["kind"] == "choice_window"
    options = payload["options"]
    assert options
    for option in options:
        assert set(option) >= {"id", "label"}
        assert option["id"] and option["label"]
    # The ids are the LASTING tokens: the value a pick resumes with is
    # already the value the audit and the port carry.
    assert [o["id"] for o in options] == ["A", "graph", "global", "D"]
    # 综述/真数据库 may be present but marked unopened (spec Clarify bullet).
    assert [o["opened"] for o in options] == [True, True, False, False]


def test_resume_pick_assigns_mode_and_grounded_retrieves() -> None:
    """SC2 "clarify window": resume with a pick -> that 档 is assigned
    (auditable operator pick) and the grounded leaf then proceeds."""
    g = _compile([_RETRIEVE_CALL, AIMessage(content="Per the filings: net sales grew.")])
    tid = "cw-resume-pick"
    out = g.invoke(_input(AMBIGUOUS), config=_cfg(tid))
    assert _interrupt_payload(out) is not None

    # A stale client that still sends the retired letter is resolved to
    # the lasting token before it is matched against the option ids.
    done = g.invoke(Command(resume="B"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    audit = done["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["decision_tier"] == "operator_pick"
    assert audit["signals"].get("resume_pick") == "graph"
    assert audit["options"] == []  # the window is consumed once resolved
    assert audit["failure_code"] is None
    # The pick rides the durable snapshot history (inherit source, SC4-
    # adjacent): the CURRENT job now carries the picked 档.
    assert any(
        s.get("search_mode") == "graph"
        for s in done.get("status_snapshots") or []
    )
    # ...and grounded proceeded: the ONE retrieve tool ran after the pick.
    summaries = done.get("tool_summaries") or []
    assert any(entry.get("name") == "retrieve" for entry in summaries)


def test_invalid_pick_reopens_window_then_valid_pick_resolves() -> None:
    """An out-of-list pick never silent-Cs: the window REOPENS (with the
    rejected value recorded for the operator), and a later valid pick
    resolves normally."""
    g = _compile(_ANSWER)
    tid = "cw-invalid-pick"
    out = g.invoke(_input(AMBIGUOUS), config=_cfg(tid))
    assert _interrupt_payload(out) is not None

    again = g.invoke(Command(resume="Z"), config=_cfg(tid))
    payload = _interrupt_payload(again)
    assert payload is not None
    assert payload["kind"] == "choice_window"
    assert "Z" in payload["rejected_picks"]
    # No 档 was invented by the rejected attempt.
    assert again["search_mode_decision"]["search_mode"] is None

    done = g.invoke(Command(resume="A"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    audit = done["search_mode_decision"]
    assert audit["search_mode"] == "A"
    assert audit["decision_tier"] == "operator_pick"
    assert audit["signals"].get("rejected_picks") == ["Z"]


def test_pick_of_unopened_tier_is_typed_fail_not_silent_c() -> None:
    """Boundary 'negative': the operator picking 综述 (unopened) gets the
    具名失败 on the audit — the cascade never substitutes another 档."""
    g = _compile(_ANSWER)
    tid = "cw-unopened-pick"
    g.invoke(_input(AMBIGUOUS), config=_cfg(tid))
    done = g.invoke(Command(resume="C"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    audit = done["search_mode_decision"]
    assert audit["decision_tier"] == "typed_fail"
    assert audit["failure_code"] == TIER_NOT_OPEN
    assert audit["search_mode"] is None
    assert audit["signals"].get("requested_mode") == "global"


def test_lowercase_lasting_token_pick_resolves() -> None:
    """The window's own tokens must survive the pick: a lowercase
    ``graph`` (an option id spelled exactly as emitted) resolves to the
    知识问答 档 instead of being upper-cased into something that matches no
    option — and its upper-cased lookalike resolves to the same 档."""
    for spelling in ("graph", "GRAPH", " Graph "):
        g = _compile(_ANSWER)
        tid = f"cw-lowercase-pick-{spelling.strip()}"
        g.invoke(_input(AMBIGUOUS), config=_cfg(tid))
        done = g.invoke(Command(resume=spelling), config=_cfg(tid))
        assert not done.get("__interrupt__"), spelling
        audit = done["search_mode_decision"]
        assert audit["search_mode"] == "graph", spelling
        assert audit["decision_tier"] == "operator_pick", spelling
        assert audit["signals"].get("resume_pick") == "graph", spelling


def test_greeting_dialogue_never_opens_the_window() -> None:
    """A dialogue-track turn never reaches the search cascade, so it
    never interrupts (spec assumption 2)."""
    g = _compile([AIMessage(content="Hi there.")])
    out = g.invoke(_input("Hi!"), config=_cfg("cw-greeting"))
    assert out["track"] == "dialogue"
    assert not out.get("__interrupt__")
    assert not out.get("search_mode_decision")


def test_pin_assigns_mode_without_window() -> None:
    """SC4 at graph level: the documented pin field rides the run config
    and assigns the 档 (rules tier) without opening a window."""
    g = _compile(_ANSWER)
    out = g.invoke(
        _input(AMBIGUOUS),
        config=_cfg("cw-pin", pinned_search_mode="B"),
    )
    assert not out.get("__interrupt__")
    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["decision_tier"] == "rules"
    assert audit["signals"].get("pinned") is True


def test_choice_window_off_by_default_grounded_clarify_completes() -> None:
    """Factory default OFF keeps the pre-T2 flow: the clarify residual
    runs through to the grounded leaf without any interrupt (this is
    what keeps the default-collected suites green)."""
    g = _compile([AIMessage(content="Net sales grew.")], choice_window=False)
    out = g.invoke(_input(AMBIGUOUS), config=_cfg("cw-off"))
    assert not out.get("__interrupt__")
    assert out["search_mode_decision"]["decision_tier"] == "clarify"
    assert out["search_mode_decision"]["search_mode"] is None


# ---------------------------------------------------------------------------
# workspace HTTP lane: SSE contract + resume body + 409 + pin
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
    """Same wire parser as ``test_workspace_http._parse_sse_events``."""
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


def test_send_streams_choice_window_event(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """The send SSE carries a ``choice_window`` frame whose data is the
    interrupt payload — the payload contract holds on the WIRE, not only
    in the graph (spec SC2 last bullet)."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(r.text)
    names = [name for name, _ in events]
    assert names[-1] == "choice_window"
    payload = events[-1][1]
    assert payload["kind"] == "choice_window"
    assert payload["options"]
    for option in payload["options"]:
        assert set(option) >= {"id", "label"}
    # The paused run emits no terminal frames (the run is NOT over).
    assert "status" not in names
    assert "citations" not in names


def test_send_while_choice_pending_returns_409(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """Boundary 'concurrent': a same-thread send while a window is
    pending keeps the plain-409 ``concurrent_run`` semantics."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    first = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS},
    )
    assert second.status_code == 409
    assert second.json() == {"error": "concurrent_run"}


def test_resume_with_pick_streams_the_completed_run(
    client: TestClient, store, http_app: FastAPI
) -> None:
    _install_window_graph(http_app, _ANSWER)
    from fin.host.http import get_graph

    installed = http_app.dependency_overrides[get_graph]()
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS},
    )
    assert _parse_sse_events(r.text)[-1][0] == "choice_window"

    resumed = client.post(
        f"/api/threads/{thread_id}/resume", json={"pick": "B"}
    )
    assert resumed.status_code == 200
    assert resumed.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(resumed.text)
    names = [name for name, _ in events]
    assert "choice_window" not in names
    assert names[-1] == "citations"
    status_frame = next(data for name, data in events if name == "status")
    assert status_frame["status"] in ("done", "refuse")
    assert installed.interrupt_pending(thread_id) is None
    audit = installed._graph.get_state(
        _cfg(thread_id)
    ).values["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["decision_tier"] == "operator_pick"


def test_resume_without_pick_reemits_the_window(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """A body-less resume against a pending window REDRAWS the window on
    the same SSE wire (SPA reload path) — and the existing body-less
    crash-resume contract is untouched (see the 204 case below)."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    sent = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS},
    )
    assert _parse_sse_events(sent.text)[-1][0] == "choice_window"

    redraw = client.post(f"/api/threads/{thread_id}/resume")
    assert redraw.status_code == 200
    assert redraw.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(redraw.text)
    assert events[-1][0] == "choice_window"
    assert events[-1][1]["kind"] == "choice_window"


def test_resume_pick_without_pending_choice_is_400(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """A pick when no window is pending is a typed 400 — never a
    silently ignored field, never a crash-resume hijack."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(f"/api/threads/{thread_id}/resume", json={"pick": "A"})
    assert r.status_code == 400
    assert r.json() == {"error": "choice_not_pending"}


def test_bodyless_resume_on_fresh_thread_still_204(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """The optional resume body must not change the pre-existing crash-
    resume surface: nothing to resume is still a plain 204."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(f"/api/threads/{thread_id}/resume")
    assert r.status_code == 204


def test_ask_answers_choice_pending_409(http_app: FastAPI, client: TestClient) -> None:
    """The unattended ``POST /ask`` sync-invoke contract with the HITL
    lane on: a clarify ask PAUSES (never a silent 档) and the surface
    answers the typed 409 ``choice_pending``."""
    _install_window_graph(http_app, _ANSWER)
    r = client.post("/ask", json={"question": AMBIGUOUS})
    assert r.status_code == 409
    assert r.json() == {"error": "choice_pending"}


def test_send_with_pinned_tier_skips_the_window(
    client: TestClient, store, http_app: FastAPI
) -> None:
    """SC4 on the wire: the existing ``tiers`` selector field pins an
    OPEN 档 — the send runs through (no choice_window frame, terminal
    frames present) and the audit records the rules-tier pin."""
    _install_window_graph(http_app, _ANSWER)
    thread_id = _new_thread(client)
    r = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"question": AMBIGUOUS, "tiers": ["B"]},
    )
    assert r.status_code == 200
    events = _parse_sse_events(r.text)
    names = [name for name, _ in events]
    assert "choice_window" not in names
    assert names[-1] == "citations"
    from fin.host.http import get_graph

    installed = http_app.dependency_overrides[get_graph]()
    audit = installed._graph.get_state(
        _cfg(thread_id)
    ).values["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["decision_tier"] == "rules"
    assert audit["signals"].get("pinned") is True
