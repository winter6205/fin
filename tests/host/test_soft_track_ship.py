"""Soft-Track ship gate (T5): opt-in MUST suites + boundary matrix.

Selected only via ``uv run pytest -q -m soft_track``. Default
``uv run pytest -q`` deselects these (see ``tests/host/conftest.py``).

ScriptedChatModel + InMemorySaver only — no live LLM.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.state import REPAIR_CAP, reset_input_state
from fin.host.track_decision import decide_track

pytestmark = [pytest.mark.soft_track]

AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


@pytest.fixture(scope="module")
def aapl_page1_text() -> str:
    from fin.corpus.extracted import load_extracted_tree

    tree = load_extracted_tree(
        Path("data/corpus/manifest.json"),
        Path("data/corpus/extracted"),
    )
    return tree[AAPL_2022Q3][0].text


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _assert_track_decision(td: dict[str, Any], *, track: str) -> None:
    assert isinstance(td, dict)
    assert td.get("track") == track
    assert "confidence" in td
    assert "decision_tier" in td
    assert isinstance(td.get("signals"), dict)


def _parse_sse(body: str) -> list[tuple[str, Any]]:
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


def _no_tools(messages: list) -> None:
    for msg in messages:
        if isinstance(msg, AIMessage):
            assert not msg.tool_calls


def _empty_cites(out: dict) -> None:
    cites = out.get("citations", ()) or ()
    assert list(cites) == []


# ---------------------------------------------------------------------------
# MUST (1) greeting → dialogue
# ---------------------------------------------------------------------------


def test_must_greeting_dialogue_no_tools_no_gate_done_empty_cites() -> None:
    model = ScriptedChatModel(
        script=[AIMessage(content="Hello! How can I help with 10-Q research?")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("Hi!"), "evidence": []},
        config=_cfg("ship-greeting"),
    )
    assert out["track"] == "dialogue"
    assert out["status"] == "done"
    _empty_cites(out)
    _no_tools(out["messages"])
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
    _assert_track_decision(out["track_decision"], track="dialogue")


def test_must_greeting_boundary_thanks_is_dialogue() -> None:
    """Boundary: thanks-style greeting still dialogue (rules)."""
    model = ScriptedChatModel(script=[AIMessage(content="You're welcome.")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("Thanks!"), "evidence": []},
        config=_cfg("ship-greeting-thanks"),
    )
    assert out["track"] == "dialogue"
    assert out["status"] == "done"
    _empty_cites(out)


def test_must_greeting_boundary_no_durable_dialogue_system() -> None:
    """Boundary: call-scoped dialogue SystemMessage not checkpointed."""
    from fin.host.agent_instructions import DIALOGUE_INSTRUCTIONS_MARKER

    model = ScriptedChatModel(script=[AIMessage(content="Hi.")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    g.invoke(
        {**reset_input_state("Hello"), "evidence": []},
        config=_cfg("ship-greeting-nodurable"),
    )
    snap = g._graph.get_state(_cfg("ship-greeting-nodurable"))
    msgs = list((snap.values or {}).get("messages") or [])
    assert not any(
        isinstance(m, SystemMessage)
        and DIALOGUE_INSTRUCTIONS_MARKER in str(m.content)
        for m in msgs
    )


# ---------------------------------------------------------------------------
# MUST (2) underspec clarify
# ---------------------------------------------------------------------------


def test_must_underspec_clarify_no_tools_done_empty_cites() -> None:
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Which company and period? Options include AAPL, MSFT, "
                    "AMZN and periods 2022 Q3, 2023 Q1–Q3."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("What was revenue?"), "evidence": []},
        config=_cfg("ship-clarify"),
    )
    assert out["track"] == "clarify"
    assert out["status"] == "done"
    _empty_cites(out)
    _no_tools(out["messages"])
    assert "AAPL" in out["answer"] or "MSFT" in out["answer"]
    _assert_track_decision(out["track_decision"], track="clarify")


def test_must_clarify_boundary_metric_only_stays_clarify() -> None:
    """Boundary: bare metric ask without company/period → clarify."""
    model = ScriptedChatModel(
        script=[AIMessage(content="Need company and period from the frozen 12.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("What was operating income?"), "evidence": []},
        config=_cfg("ship-clarify-metric"),
    )
    assert out["track"] == "clarify"
    assert out["status"] == "done"
    _empty_cites(out)


def test_must_clarify_boundary_never_enters_citation_gate() -> None:
    model = ScriptedChatModel(script=[AIMessage(content="Which filing?")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("net sales?"), "evidence": []},
        config=_cfg("ship-clarify-nogate"),
    )
    assert out["track"] == "clarify"
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
    assert out["status"] != "refuse"


# ---------------------------------------------------------------------------
# MUST (3) off_topic soft done
# ---------------------------------------------------------------------------


def test_must_off_topic_soft_done_not_refuse() -> None:
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "I focus on SEC 10-Q filings in this workspace. "
                    "I cannot help with weather forecasts."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            **reset_input_state("What's the weather in Seattle today?"),
            "evidence": [],
        },
        config=_cfg("ship-off-topic"),
    )
    assert out["track"] == "off_topic"
    assert out["status"] == "done"
    assert out["status"] != "refuse"
    _empty_cites(out)
    _no_tools(out["messages"])
    _assert_track_decision(out["track_decision"], track="off_topic")


def test_must_off_topic_boundary_recipe_soft_done() -> None:
    model = ScriptedChatModel(
        script=[AIMessage(content="I only cover frozen 10-Q filings here.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            **reset_input_state("What's a good pasta recipe for dinner?"),
            "evidence": [],
        },
        config=_cfg("ship-off-recipe"),
    )
    assert out["track"] == "off_topic"
    assert out["status"] == "done"


def test_must_off_topic_boundary_empty_cites_and_answer() -> None:
    model = ScriptedChatModel(
        script=[AIMessage(content="Out of scope for this 10-Q workspace.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("Tell me a joke about cats"), "evidence": []},
        config=_cfg("ship-off-joke"),
    )
    assert out["track"] == "off_topic"
    _empty_cites(out)
    assert out["answer"]


# ---------------------------------------------------------------------------
# MUST (4) research tools+span
# ---------------------------------------------------------------------------


def test_must_research_tools_span_done_with_citation(aapl_page1_text: str) -> None:
    span = aapl_page1_text[:80]
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "page": 1,
                            "page_limit": 1,
                        },
                        "id": "c-read-ship",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2022 Q3?"),
            "evidence": [],
        },
        config=_cfg("ship-research"),
    )
    assert out["track"] == "research"
    assert out["status"] == "done"
    assert len(out["citations"]) >= 1
    _assert_track_decision(out["track_decision"], track="research")


def test_must_research_boundary_bare_q_is_dialogue(aapl_page1_text: str) -> None:
    """Boundary (no single global default track): bare ``q`` → dialogue
    via the residual conditional_policy branch, NOT research."""
    span = aapl_page1_text[:80]
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "page": 1,
                            "page_limit": 1,
                        },
                        "id": "c-read-q",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("q"), "evidence": []},
        config=_cfg("ship-research-q"),
    )
    assert out["track"] == "dialogue"
    assert out["status"] == "done"


def test_must_research_boundary_workspace_sse_track_and_history(
    aapl_page1_text: str,
) -> None:
    """Wire: SSE status carries track=research; GET turn has track_decision."""
    from fin.host.http import app, get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    span = aapl_page1_text[:80]
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "page": 1,
                            "page_limit": 1,
                        },
                        "id": "c-read-wire",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    store = InMemoryThreadStore()
    app.dependency_overrides[get_graph] = lambda: g
    app.dependency_overrides[get_thread_store] = lambda: store
    try:
        client = TestClient(app)
        created = client.post(
            "/api/threads",
            json={"first_message": "AAPL 2022 Q3"},
        ).json()
        r = client.post(
            f"/api/threads/{created['id']}/messages",
            json={"question": "What was AAPL net sales in 2022 Q3?"},
        )
        assert r.status_code == 200
        events = _parse_sse(r.text)
        status_frames = [d for e, d in events if e == "status"]
        assert status_frames
        terminal = status_frames[-1]
        assert terminal.get("track") == "research"
        assert terminal.get("status") == "done"

        detail = client.get(f"/api/threads/{created['id']}").json()
        assert detail["turns"]
        td = detail["turns"][-1].get("track_decision") or {}
        _assert_track_decision(td, track="research")
        runs = store.list_runs(created["id"])
        assert runs
        _assert_track_decision(runs[-1].track_decision, track="research")
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# MUST (5) prior snapshot inherit research
# ---------------------------------------------------------------------------


def test_must_prior_snapshot_short_followup_inherits_research() -> None:
    model = ScriptedChatModel(
        script=[
            AIMessage(content="Looking up AAPL 2023 Q3."),
            AIMessage(content="need evidence for EPS"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    tid = "ship-inherit"
    g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid),
    )
    out = g.invoke(
        {**reset_input_state("and EPS?"), "evidence": []},
        config=_cfg(tid),
    )
    assert out["track"] == "research"
    assert out["track_decision"]["decision_tier"] == "inherit"
    _assert_track_decision(out["track_decision"], track="research")


def test_must_inherit_boundary_decide_track_unit() -> None:
    decision = decide_track(
        [HumanMessage(content="and EPS?")],
        snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": ["AAPL-2023Q3"],
            }
        ],
    )
    assert decision.track == "research"
    assert decision.decision_tier == "inherit"


def test_must_inherit_boundary_empty_snapshot_does_not_force_research() -> None:
    """Boundary (empty class): short follow-up with empty snapshots ≠ inherit."""
    decision = decide_track(
        [HumanMessage(content="and EPS?")],
        snapshots=[],
    )
    assert decision.decision_tier != "inherit"


# ---------------------------------------------------------------------------
# 5-class Soft-Track matrix
# ---------------------------------------------------------------------------


def test_matrix_empty_blank_underspec_or_empty_snapshot() -> None:
    """Empty: blank-ish underspec filing ask → clarify; empty snapshot no inherit."""
    model = ScriptedChatModel(
        script=[AIMessage(content="Which company and period?")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("revenue"), "evidence": []},
        config=_cfg("ship-empty"),
    )
    assert out["track"] == "clarify"
    # Empty-snapshot inherit already covered; blank residual → dialogue:
    blank = decide_track([HumanMessage(content="   ")], snapshots=[])
    assert blank.track == "dialogue"


def test_matrix_negative_off_domain_wrong_track() -> None:
    """Negative: off-domain is off_topic, not research refuse."""
    model = ScriptedChatModel(
        script=[AIMessage(content="I only do 10-Q filings.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {**reset_input_state("What's the weather in Seattle today?"), "evidence": []},
        config=_cfg("ship-neg"),
    )
    assert out["track"] == "off_topic"
    assert out["track"] != "research"
    assert out["status"] == "done"
    assert out["status"] != "refuse"


def test_matrix_overflow_caps_research_only_snapshots_retained(
    aapl_page1_text: str,
) -> None:
    """Overflow: citation_repair_cap / step_cap on research; snapshots survive."""
    # Repair-cap path (research only).
    bounce_script = [
        AIMessage(content="No span here — inventing prose.")
        for _ in range(REPAIR_CAP + 2)
    ]
    model = ScriptedChatModel(script=bounce_script)
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
    )
    tid = "ship-overflow-repair"
    out = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2022 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid),
    )
    assert out["track"] == "research"
    assert out["status"] in ("refuse", "step_cap", "done")
    if out["status"] == "refuse":
        assert out["status_reason"] == "citation_repair_cap"

    # Snapshot history not wiped by reset_input_state / follow-up.
    model2 = ScriptedChatModel(
        script=[
            AIMessage(content="AAPL context."),
            AIMessage(content="still AAPL."),
        ]
    )
    g2 = compile_graph(
        checkpointer=InMemorySaver(),
        model=model2,
        known_document_ids=KNOWN_IDS,
    )
    tid2 = "ship-overflow-snap"
    g2.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid2),
    )
    out2 = g2.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2023 Q2?"),
            "evidence": [],
        },
        config=_cfg(tid2),
    )
    snaps = list(out2.get("status_snapshots") or [])
    assert len(snaps) >= 2


def test_matrix_concurrent_soft_track_send_409() -> None:
    """Concurrent: same-thread Soft-Track send still 409 ConcurrentRunError."""
    from fin.host.http import app, get_graph, get_thread_store
    from fin.host.scripted_model import ScriptedChatModel as SCM
    from fin.host.thread_store import InMemoryThreadStore

    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(SCM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    blocking = BlockingModel(script=[AIMessage(content="Hello soft track.")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
    )
    store = InMemoryThreadStore()
    app.dependency_overrides[get_graph] = lambda: g
    app.dependency_overrides[get_thread_store] = lambda: store
    try:
        client = TestClient(app)
        created = client.post(
            "/api/threads", json={"first_message": "Hi"}
        ).json()
        results: dict[str, Any] = {}

        def hit(label: str) -> None:
            results[label] = client.post(
                f"/api/threads/{created['id']}/messages",
                json={"question": "Hi!"},
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(hit, "first")
            assert entered.wait(timeout=5.0)
            f2 = ex.submit(hit, "second")
            time.sleep(0.2)
            release.set()
            f1.result(timeout=10.0)
            f2.result(timeout=10.0)

        statuses = sorted(r.status_code for r in results.values())
        assert statuses == [200, 409], statuses
        err = next(r for r in results.values() if r.status_code == 409)
        assert err.headers["content-type"].startswith("application/json")
        assert err.json() == {"error": "concurrent_run"}
    finally:
        app.dependency_overrides.clear()


def test_matrix_exception_classify_exit_defined_track() -> None:
    """Exception: invalid structured classify → typed tier EXIT, not crash."""

    class _Stub:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
            self.calls += 1
            return AIMessage(content='{"track": "supervisor"}')

    stub = _Stub()
    decision = decide_track(
        [HumanMessage(content="hmm maybe this?")],
        classify_model=stub,
    )
    assert stub.calls == 1
    assert decision.track in ("dialogue", "clarify", "off_topic", "research")
    assert decision.signals.get("classify_exit") == "invalid_track"
    assert decision.decision_tier != "llm"

    model = ScriptedChatModel(
        script=[AIMessage(content="Soft reply after classify EXIT.")]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        classify_model=stub,
        known_document_ids=KNOWN_IDS,
    )
    out = g.invoke(
        {
            **reset_input_state("hmm maybe this?"),
            "evidence": [],
        },
        config=_cfg("ship-exc"),
    )
    assert out["track"] in ("dialogue", "clarify", "off_topic", "research")
    assert out["track_decision"]["signals"].get("classify_exit") == "invalid_track"
    if out["track"] != "research":
        assert out["status"] == "done"
        assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
