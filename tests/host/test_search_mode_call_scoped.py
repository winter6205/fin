"""Call-scoped 检索档 copy + post-compaction re-inject (ADR 0061 / T3).

Opt-in only (``uv run pytest -q -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_search_mode_cascade.py``).

Contract under test (specs/search-mode-host.md :47, SC2 "call-scoped
copy", ADR 0061):

* when the turn's cascade ASSIGNED a 档 (rules / llm / inherit /
  operator_pick), the usage copy rides the model-facing payload at
  model-call time — injected EXACTLY ONCE per call, in near context.
* checkpointed ``messages`` never store the copy: history is never
  rewritten when the mode changes, and a second turn with a different
  档 leaves the first turn's checkpointed messages byte-identical.
* after session compaction (history rebuilt via ``RemoveMessage``) the
  NEXT model call still carries the current turn's 档 copy — the
  call-scoped shape satisfies re-injection by construction; this suite
  locks that behaviour.
* unassigned turns (dialogue track, clarify awaiting a pick; the
  typed-fail class has no producer since the tracked change T7 emptied the refusal
  table) inject NOTHING.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.host.agent_instructions import (
    HOST_INSTRUCTIONS_MARKER,
    SEARCH_MODE_INSTRUCTIONS_MARKER,
    SEARCH_MODE_USAGE_COPY,
    messages_for_model,
)
from fin.host.graph import compile_graph
from fin.host.scripted_model import ScriptedChatModel
from fin.host.search_mode_decision import MODE_LABELS
from fin.host.session_compaction import session_token_budget
from fin.host.state import reset_input_state

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
# Same small override the compaction-gate suite uses so a scripted
# fixture crosses the 90% trigger without a 230k-char history.
TEST_BUDGET = 1_600


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class _RecordingScriptedModel(ScriptedChatModel):
    """Scripted model that captures every call's message payload.

    ``_stream`` without a stream script delegates to ``_generate``, so
    overriding ``_generate`` captures both the sync and the streaming
    surface the graph uses.
    """

    captured: list = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.captured.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


class _StubSummarizer:
    """Deterministic compaction summarizer (never the recorded model,
    so captured calls are the graph's own model calls only)."""

    def __init__(self, text: str = "Summary of the evicted history.") -> None:
        self.text = text

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content=self.text)


def _compile(model: Any, **kwargs: Any) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        summarizer=_StubSummarizer(),
        **kwargs,
    )


def _mode_blocks(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [m for m in messages if SEARCH_MODE_INSTRUCTIONS_MARKER in str(m.content)]


def _checkpoint_messages(g: Any, thread_id: str) -> list[BaseMessage]:
    values = g._graph.get_state(_cfg(thread_id)).values or {}
    return list(values.get("messages") or [])


def _invoke(g: Any, text: str, thread_id: str) -> dict:
    return g.invoke(
        {**reset_input_state(text), "evidence": []}, config=_cfg(thread_id)
    )


# ----- seeded fat history (compaction trigger) -----

_TOOL_PAYLOAD = "p" * 12_000


def _tool_pair(ai_text: str, tool_body: str, call_id: str) -> list[BaseMessage]:
    return [
        AIMessage(
            content=ai_text,
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "unit": 1,
                        }
                    },
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=tool_body, tool_call_id=call_id),
    ]


def _seed_messages() -> list[BaseMessage]:
    """A fat history far above 90% of the 1_600-token test budget
    (same shape as ``test_session_compaction_gate._seed_messages``)."""
    messages: list[BaseMessage] = []
    for index in range(3):
        messages += _tool_pair(
            f"calling read_source {index}", _TOOL_PAYLOAD, f"c-seed-{index}"
        )
    messages += _tool_pair(
        "calling read_source 3",
        '{"source_ref": {"document_id": "AAPL-2022Q3", "unit": 1}}',
        "c-seed-3",
    )
    messages.append(AIMessage(content="x" * 300))
    return messages


def _seed_checkpoint(
    g: Any,
    thread_id: str,
    messages: list[BaseMessage],
    *,
    status_snapshots: list[dict[str, Any]] | None = None,
) -> None:
    state: dict[str, Any] = {"messages": messages}
    if status_snapshots is not None:
        state["status_snapshots"] = status_snapshots
    g._graph.update_state(_cfg(thread_id), state, as_node="model")


# ===========================================================================
# unit: messages_for_model(search_mode=...)
# ===========================================================================


def test_messages_for_model_injects_mode_copy_once() -> None:
    out = messages_for_model(
        [HumanMessage(content="what was AAPL revenue?")],
        track="grounded",
        search_mode="graph",
    )
    blocks = _mode_blocks(out)
    assert len(blocks) == 1
    assert blocks[0] is out[0]
    assert isinstance(out[0], SystemMessage)
    assert str(out[0].content).count(SEARCH_MODE_INSTRUCTIONS_MARKER) == 1
    assert MODE_LABELS["graph"] in str(out[0].content)
    assert str(out[0].content).count(MODE_LABELS["graph"]) == 1


def test_messages_for_model_injection_is_idempotent() -> None:
    """Re-feeding the output (re-entry / retry) must not stack a second
    copy — the marker check keeps exactly one block."""
    first = messages_for_model(
        [HumanMessage(content="q")], track="grounded", search_mode="graph"
    )
    second = messages_for_model(first, track="grounded", search_mode="graph")
    assert len(_mode_blocks(second)) == 1
    assert str(second[0].content).count(SEARCH_MODE_INSTRUCTIONS_MARKER) == 1


def test_unassigned_or_unknown_mode_injects_nothing() -> None:
    """Typed no-op: ``None`` / empty / out-of-set tokens leave the
    payload byte-identical to the pre-T3 call (OFF-path rule)."""
    baseline = messages_for_model(
        [HumanMessage(content="q")], track="grounded"
    )
    for mode in (None, "", "E", "fast", 3, True):
        out = messages_for_model(
            [HumanMessage(content="q")], track="grounded", search_mode=mode
        )
        assert [str(m.content) for m in out] == [str(m.content) for m in baseline]
        assert not _mode_blocks(out)


def test_each_mode_copy_names_only_its_tier() -> None:
    """Every 档 carries its own usage copy naming exactly its label —
    查找 / 知识问答 / 综述 / 真数据库 (CONTEXT 检索档). The copy-key /
    retired-spelling superset is pinned once in
    ``test_search_mode_usage_copy_keys.py``, not re-derived here."""
    for mode, label in MODE_LABELS.items():
        content = str(
            messages_for_model(
                [HumanMessage(content="q")], track="grounded", search_mode=mode
            )[0].content
        )
        assert label in content
        for other in MODE_LABELS.values():
            if other != label:
                assert other not in content


def test_track_prompt_stays_first_with_mode_copy() -> None:
    """The copy rides INSIDE the head SystemMessage after the track
    prompt — near context, one message, old SystemMessages untouched."""
    out = messages_for_model(
        [HumanMessage(content="q")], track="grounded", search_mode="lexical"
    )
    head = str(out[0].content)
    assert head.startswith(HOST_INSTRUCTIONS_MARKER)
    assert head.index(HOST_INSTRUCTIONS_MARKER) < head.index(
        SEARCH_MODE_INSTRUCTIONS_MARKER
    )


# ===========================================================================
# graph: checkpoint never stores the copy; model call sees it exactly once
# ===========================================================================


def test_assigned_mode_copy_absent_from_checkpointed_history() -> None:
    """SC2 "call-scoped copy" half 1: after a lexical-assigned turn, the
    checkpoint's ``messages`` hold no search-mode usage SystemMessage."""
    model = _RecordingScriptedModel(
        script=[AIMessage(content="Net sales grew year over year.")]
    )
    g = _compile(model)
    out = _invoke(
        g, "lexical档 what was AAPL net sales in 2023 Q3?", "smcp-checkpoint"
    )
    assert out["search_mode_decision"]["search_mode"] == "lexical"
    checkpoint = _checkpoint_messages(g, "smcp-checkpoint")
    assert checkpoint
    assert not _mode_blocks(checkpoint)
    for message in checkpoint:
        assert SEARCH_MODE_USAGE_COPY["lexical"] not in str(message.content)
    # The model itself DID see the copy, exactly once, in the head.
    assert model.captured
    call = model.captured[-1]
    assert len(_mode_blocks(call)) == 1


def test_model_payload_holds_copy_exactly_once_per_call() -> None:
    """A grounded tool loop calls the model twice; EACH call payload
    carries the 档 copy exactly once (call-scoped, never stacked)."""
    model = _RecordingScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "AAPL net sales"},
                        "id": "c-retrieve-smcp",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Per the filings: net sales grew."),
        ]
    )
    g = _compile(model)
    _invoke(g, "lexical档 what was AAPL net sales in 2023 Q3?", "smcp-tool-loop")
    assert len(model.captured) >= 2
    for call in model.captured:
        blocks = _mode_blocks(call)
        assert len(blocks) == 1
        assert str(call[0].content).count(SEARCH_MODE_INSTRUCTIONS_MARKER) == 1


def test_second_turn_new_mode_leaves_first_history_unedited() -> None:
    """SC2 "call-scoped copy" half 2: turn 2 names 知识问答 while turn 1
    ran 查找; turn 1's checkpointed messages stay byte-identical (history
    is NEVER rewritten on a mode change) and the new call carries only
    the lasting token."""
    model = _RecordingScriptedModel(
        script=[
            AIMessage(content="Answer one."),
            AIMessage(content="Answer two."),
        ]
    )
    g = _compile(model)
    tid = "smcp-two-modes"
    out1 = _invoke(g, "lexical档 what was AAPL net sales in 2023 Q3?", tid)
    assert out1["search_mode_decision"]["search_mode"] == "lexical"
    first_history = [
        (getattr(m, "id", None), str(m.content))
        for m in _checkpoint_messages(g, tid)
    ]
    assert first_history

    out2 = _invoke(g, "graph档 and 2022 Q3?", tid)
    assert out2["search_mode_decision"]["search_mode"] == "graph"
    second_history = [
        (getattr(m, "id", None), str(m.content))
        for m in _checkpoint_messages(g, tid)
    ]
    # The prior checkpointed rows survive verbatim as a prefix.
    assert second_history[: len(first_history)] == first_history
    assert not _mode_blocks(_checkpoint_messages(g, tid))
    last_call = model.captured[-1]
    block = str(_mode_blocks(last_call)[0].content)
    # The second turn's copy names only its own 档 — the first turn's
    # 查找 copy is gone from the fresh payload, not stacked onto it.
    assert "检索档 graph" in block
    assert "检索档 lexical" not in block


def test_dialogue_turn_injects_no_copy() -> None:
    """Non-grounded tracks assign no 档, so the leaf call has no copy."""
    model = _RecordingScriptedModel(
        script=[AIMessage(content="Hi! How can I help?")]
    )
    g = _compile(model)
    out = _invoke(g, "Hi!", "smcp-dialogue")
    assert out["track"] == "dialogue"
    assert model.captured
    for call in model.captured:
        assert not _mode_blocks(call)


def test_clarify_grounded_turn_injects_no_copy() -> None:
    """An unassigned grounded turn injects NOTHING: the tracked change T7 (6e8ed97)
    emptied the routing-time refusal table, so the residual that leaves
    ``search_mode=None`` is now the clarify turn (no 档 named, no judge
    client) — the same "nothing assigned, nothing injected" contract the
    old typed-fail case carried (that case rode ``sql``, first as the
    unopened ``global``, and is now impossible for a closed-set naming;
    the positive flip is pinned by
    :func:`test_assigned_sql_turn_injects_its_copy_once`)."""
    model = _RecordingScriptedModel(
        script=[AIMessage(content="Net sales grew.")]
    )
    g = _compile(model)
    out = _invoke(g, "what was AAPL net sales in 2023 Q3?", "smcp-clarify")
    assert out["track"] == "grounded"
    assert out["search_mode_decision"]["decision_tier"] == "clarify"
    assert out["search_mode_decision"]["search_mode"] is None
    for call in model.captured:
        assert not _mode_blocks(call)


def test_assigned_sql_turn_injects_its_copy_once() -> None:
    """The the tracked change T7 flip of the old typed-fail pin, equal half 2: naming
    the now-open 真数据库 assigns it, so the grounded model call carries
    the ``sql`` usage copy exactly once — 综述 and 真数据库 both moved
    from "no copy, refusal" to "own copy, routed" and neither is the
    other 档's copy."""
    model = _RecordingScriptedModel(
        script=[AIMessage(content="Net sales grew.")]
    )
    g = _compile(model)
    out = _invoke(g, "sql档 what was AAPL net sales in 2023 Q3?", "smcp-sql-copy")
    audit = out["search_mode_decision"]
    assert audit["decision_tier"] == "rules"
    assert audit["search_mode"] == "sql"
    assert audit["failure_code"] is None
    assert model.captured
    call = model.captured[-1]
    blocks = _mode_blocks(call)
    assert len(blocks) == 1
    assert "检索档 sql" in str(blocks[0].content)
    assert SEARCH_MODE_USAGE_COPY["sql"] in str(blocks[0].content)


def test_copy_reinjected_after_compaction() -> None:
    """ADR 0061 re-inject: the compaction gate rebuilds history via
    ``RemoveMessage``; the NEXT model call of the same run still carries
    the current turn's (inherited) 知识问答 copy exactly once."""
    model = _RecordingScriptedModel(
        script=[AIMessage(content="Net sales grew year over year.")]
    )
    g = _compile(model)
    tid = "smcp-compaction"
    _seed_checkpoint(
        g,
        tid,
        _seed_messages(),
        status_snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": [AAPL_2022Q3],
                "search_mode": "graph",
            }
        ],
    )
    with session_token_budget(TEST_BUDGET):
        out = g.invoke(
            {
                **reset_input_state("and 2022 Q3?"),
                "evidence": [],
            },
            config=_cfg(tid),
        )
    assert out["search_mode_decision"]["decision_tier"] == "inherit"
    assert out["search_mode_decision"]["search_mode"] == "graph"
    assert model.captured
    call = model.captured[-1]
    # The history the model sees was rebuilt by the gate: the summary
    # SystemMessage is present inside the fat seeded window's tail.
    assert any(
        isinstance(m, SystemMessage)
        and m.additional_kwargs.get("fin_compaction_summary") is True
        for m in call
    )
    blocks = _mode_blocks(call)
    assert len(blocks) == 1
    assert blocks[0] is call[0]
    assert "检索档 graph" in str(blocks[0].content)
    # And the copy itself is still call-scoped after the rebuild.
    assert not _mode_blocks(_checkpoint_messages(g, tid))
