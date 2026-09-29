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

from fin.corpus.extracted import ExtractedPage, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import HIDDEN_BOUNCE_MARKER
from fin.host.jev_choice import JEV_CHOICE_EXIT_SCHEMA, JevChoiceError
from fin.host.state import REPAIR_CAP, reset_input_state
from fin.host.track_decision import decide_track
from fin.retrieve import FakeRetrievePort
from jev_fake import FakeJevChoice

pytestmark = [pytest.mark.soft_track]

AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

MANIFEST_PATH = Path("data/corpus/manifest.json")
EXTRACTED_ROOT = Path("data/corpus/extracted")

_READ_MANIFEST: dict[str, tuple[ExtractedPage, ...]] | None = None


def _read_manifest() -> dict[str, tuple[ExtractedPage, ...]]:
    """The committed extracted tree as a ``{document_id: pages}`` read
    manifest.

    Only the scripted ``read_source`` transcripts need bodies: the graph
    never reads the tree on its own (ADR 0046 / 0047), so the test hands
    it in through the bound adapter's ``pages_by_doc``. Cached per
    module: ``ExtractedPage`` is frozen and ``FakeRetrievePort`` copies
    what it is handed, so no graph build disturbs another case's bodies.
    """
    global _READ_MANIFEST
    if _READ_MANIFEST is None:
        _READ_MANIFEST = load_extracted_tree(MANIFEST_PATH, EXTRACTED_ROOT)
    return _READ_MANIFEST


def _tree_read_adapter() -> FakeRetrievePort:
    """A fake retrieve port whose company read leg serves the committed
    corpus, so scripted ``read_source`` calls land on real filing text."""
    return FakeRetrievePort(pages_by_doc=_read_manifest())


def _read_source_call(unit: int, call_id: str) -> AIMessage:
    """One ``read_source`` hop for one frozen unit, read from its start."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": AAPL_2022Q3,
                        "version": AAPL_2022Q3_VERSION,
                        "unit": unit,
                    },
                    "position": 0,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _tool_message_names(messages: list[Any]) -> set[str]:
    return {
        name
        for name in (getattr(message, "name", None) for message in messages)
        if name
    }


def _tool_call_names(messages: list[Any]) -> set[str]:
    return {
        call["name"]
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
    }


@pytest.fixture(scope="module")
def aapl_page1_text() -> str:
    return _read_manifest()[AAPL_2022Q3][0].text


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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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
            _read_source_call(1, "c-read-ship"),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_tree_read_adapter(),
    )
    out = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2022 Q3?"),
            "evidence": [],
        },
        config=_cfg("ship-research"),
    )
    assert out["track"] == "grounded"
    assert out["status"] == "done"
    assert len(out["citations"]) >= 1
    _assert_track_decision(out["track_decision"], track="grounded")
    # The citation names the frozen unit this run read — ``unit``, never
    # the retired whole-page ``page`` ordinal (spec decision 5).
    citation = out["citations"][0]
    assert citation.kind == "company"
    assert citation.document_id == AAPL_2022Q3
    assert citation.version == AAPL_2022Q3_VERSION
    assert citation.unit == 1
    assert citation.quote == span
    # The retired whole-page seam is gone with no compatibility path: the
    # research leaf called ``read_source`` and nothing named ``read_file``
    # entered the transcript (spec thread-attachment-source-read SC11).
    assert _tool_call_names(out["messages"]) == {"read_source"}
    assert "read_source" in _tool_message_names(out["messages"])
    assert "read_file" not in _tool_message_names(out["messages"])


def test_must_gray_zone_bare_q_follows_judged_dialogue() -> None:
    """Boundary (no single global default track): bare ``q`` is
    gray-zone — the Jev Choice decides it (ADR 0080). A judged
    ``dialogue`` lands the dialogue leaf, NOT research."""
    model = ScriptedChatModel(
        script=[AIMessage(content="I can help with 10-Q questions.")]
    )
    fake = FakeJevChoice([("dialogue", 0.9)])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=fake,
        known_document_ids=KNOWN_IDS,
        adapter=_tree_read_adapter(),
    )
    out = g.invoke(
        {**reset_input_state("q"), "evidence": []},
        config=_cfg("ship-research-q"),
    )
    assert fake.calls  # the judge was asked exactly for the gray zone
    assert out["track"] == "dialogue"
    assert out["track_decision"]["decision_tier"] == "jev"
    assert out["status"] == "done"
    _no_tools(out["messages"])


def test_must_research_boundary_workspace_sse_track_and_history(
    aapl_page1_text: str,
) -> None:
    """Wire: SSE status carries track=research; GET turn has track_decision."""
    from fin.host.http import app, get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore

    span = aapl_page1_text[:80]
    model = ScriptedChatModel(
        script=[
            _read_source_call(1, "c-read-wire"),
            AIMessage(content=f"Per the filing: {span}"),
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_tree_read_adapter(),
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
        assert terminal.get("track") == "grounded"
        assert terminal.get("status") == "done"

        # The retired whole-page seam is gone with no compatibility path:
        # the only source-read hop on this wire is ``read_source``, and its
        # locator is the frozen company triple — neither ``read_file`` nor
        # its ``page`` / ``page_limit`` keys may resurface here
        # (spec thread-attachment-source-read SC11).
        tool_call_frames = [d for e, d in events if e == "tool_call"]
        assert [d["name"] for d in tool_call_frames] == ["read_source"]
        read_args = tool_call_frames[0]["args"]
        assert set(read_args) == {"source_ref", "position"}
        assert set(read_args["source_ref"]) == {"document_id", "version", "unit"}
        assert read_args["source_ref"]["document_id"] == AAPL_2022Q3
        assert read_args["source_ref"]["version"] == AAPL_2022Q3_VERSION
        assert read_args["source_ref"]["unit"] == 1
        assert "read_file" not in {
            d["name"] for e, d in events if e in ("tool_call", "tool_result")
        }

        # The passed citation is the unit the run read, on the wire and in
        # the stored turn row: ``unit`` carries the ordinal, ``display:
        # "page"`` only says that unit is presented as a physical page.
        wire_citation = next(d for e, d in events if e == "citations")[0]
        assert set(wire_citation) == {
            "kind",
            "document_id",
            "attachment_id",
            "version",
            "unit",
            "display",
            "filename",
            "quote",
        }
        assert wire_citation["document_id"] == AAPL_2022Q3
        assert wire_citation["version"] == AAPL_2022Q3_VERSION
        assert wire_citation["unit"] == 1
        assert wire_citation["display"] == "page"
        assert wire_citation["quote"] == span

        detail = client.get(f"/api/threads/{created['id']}").json()
        assert detail["turns"]
        td = detail["turns"][-1].get("track_decision") or {}
        _assert_track_decision(td, track="grounded")
        stored_citation = detail["turns"][-1]["citations"][0]
        assert stored_citation["unit"] == 1
        assert "page" not in stored_citation
        runs = store.list_runs(created["id"])
        assert runs
        _assert_track_decision(runs[-1].track_decision, track="grounded")
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
        adapter=_tree_read_adapter(),
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
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "inherit"
    _assert_track_decision(out["track_decision"], track="grounded")


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
    assert decision.track == "grounded"
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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
    )
    out = g.invoke(
        {**reset_input_state("What's the weather in Seattle today?"), "evidence": []},
        config=_cfg("ship-neg"),
    )
    assert out["track"] == "off_topic"
    assert out["track"] != "grounded"
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
        adapter=_tree_read_adapter(),
    )
    tid = "ship-overflow-repair"
    out = g.invoke(
        {
            **reset_input_state("What was AAPL net sales in 2022 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid),
    )
    assert out["track"] == "grounded"
    # Overflow terminals only: ``done`` is not an overflow outcome and
    # must stay out of the accepted set (spec SC2).
    assert out["status"] in ("refuse", "step_cap"), out["status"]
    if out["status"] == "refuse":
        assert out["status_reason"] == "citation_repair_cap"
    else:
        assert out["status_reason"] == "step_cap"

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
        adapter=_tree_read_adapter(),
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
        adapter=_tree_read_adapter(),
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


def test_matrix_exception_judge_exit_defined_track() -> None:
    """Exception: a typed judge exit fail-closes to clarify — never a
    crash, never a silent track (ADR 0080)."""
    fake = FakeJevChoice(
        [JevChoiceError("bad schema", exit_code=JEV_CHOICE_EXIT_SCHEMA)]
    )
    decision = decide_track(
        [HumanMessage(content="hmm maybe this?")],
        jev_client=fake,
    )
    assert decision.track == "clarify"
    assert decision.signals.get("jev_exit") == "schema"
    assert decision.decision_tier == "clarify"

    model = ScriptedChatModel(
        script=[AIMessage(content="Soft reply after judge EXIT.")]
    )
    fake_graph = FakeJevChoice(
        [JevChoiceError("bad schema", exit_code=JEV_CHOICE_EXIT_SCHEMA)]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        jev_client=fake_graph,
        known_document_ids=KNOWN_IDS,
        adapter=_tree_read_adapter(),
    )
    out = g.invoke(
        {
            **reset_input_state("hmm maybe this?"),
            "evidence": [],
        },
        config=_cfg("ship-exc"),
    )
    assert out["track"] == "clarify"
    assert out["track_decision"]["signals"].get("jev_exit") == "schema"
    assert out["status"] == "done"
    assert HIDDEN_BOUNCE_MARKER not in str(out.get("messages", []))
