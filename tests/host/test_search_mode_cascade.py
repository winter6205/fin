"""Search-mode (检索档) cascade after grounded (spec search-mode-host, Ticket 1).

Opt-in only (``uv run pytest -q -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_soft_track_ship.py``).

Five boundary classes (spec Boundaries "Test classes"), mapped to names:

* empty      -> ``test_empty_utterance_residual_clarifies_without_inventing_c``
* negative   -> ``test_classify_invalid_enum_is_typed_exit_not_silent_c`` /
                ``test_pin_of_unopened_tier_is_typed_fail_not_silent_c``
* overflow   -> ``test_cascade_has_no_step_cap_of_its_own`` /
                ``test_snapshot_inherit_does_not_wipe_history``
* concurrent -> ``test_mode_channel_does_not_leak_across_threads`` (the
                same-thread 409 lane is HTTP-surface and already owned by
                ``tests/host/test_search_mode_named_exits.py``; the T1
                cascade itself is not an HTTP surface)
* exception  -> ``test_classify_model_error_is_typed_exit_not_crash``
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.named_failures import REAL_DB_NOT_WIRED, TIER_NOT_OPEN
from fin.host.run_payload import _run_to_response_payload
from fin.host.search_mode_decision import (
    MODE_LABELS,
    clarify_options,
    decide_search_mode,
    mode_snapshot_patch,
    normalize_search_mode,
)
from fin.host.state import reset_input_state

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _msgs(text: str) -> list[HumanMessage]:
    return [HumanMessage(content=text)]


class _ClassifyStub:
    """Minimal ``classify_model`` seam: same ``.invoke`` shape the
    Soft-Track injection tests use."""

    def __init__(self, payload: str = "", raises: bool = False) -> None:
        self.payload = payload
        self.raises = raises
        self.calls = 0

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        self.calls += 1
        if self.raises:
            raise RuntimeError("classify backend down")
        return AIMessage(content=self.payload)


def _compile(
    script: list[AIMessage],
    *,
    classify_model: Any | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        classify_model=classify_model,
        known_document_ids=KNOWN_IDS,
    )


# ---------------------------------------------------------------------------
# rules tier: explicit A/B/C/D or 检索档 label names the mode, no LLM
# ---------------------------------------------------------------------------


def test_rules_assign_mode_from_tier_letter() -> None:
    decision = decide_search_mode(_msgs("A档：what was AAPL net sales in 2023 Q3?"))
    assert decision.search_mode == "A"
    assert decision.decision_tier == "rules"
    assert decision.failure_code is None
    assert decision.options == []


def test_rules_assign_mode_from_context_label() -> None:
    decision = decide_search_mode(_msgs("知识问答档 what was AAPL 2023 Q3 revenue?"))
    assert decision.search_mode == "B"
    assert decision.decision_tier == "rules"


def test_two_different_named_tiers_stay_ambiguous_not_first_match() -> None:
    """Boundary: a naming that cannot uniquely name A/B/C/D is ambiguous —
    rules must not pick one and silently call it high-confidence."""
    decision = decide_search_mode(_msgs("A档还是B档？"))
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("ambiguous_naming") == ["A", "B"]


# ---------------------------------------------------------------------------
# optional pin / override: assigns without opening the window
# ---------------------------------------------------------------------------


def test_pin_assigns_mode_without_window() -> None:
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="B"
    )
    assert decision.search_mode == "B"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("pinned") is True
    assert decision.options == []  # no window opened by a pin


def test_pin_of_unopened_tier_is_typed_fail_not_silent_c() -> None:
    """Boundary 'negative': pin 综述 (a plan-slot 档) → 具名失败 档未开;
    the residual must not invent ANY mode, and must not be a silent C
    assignment either."""
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="C"
    )
    assert decision.search_mode is None
    assert decision.decision_tier == "typed_fail"
    assert decision.failure_code == TIER_NOT_OPEN
    assert decision.signals.get("requested_mode") == "C"


def test_pin_of_real_db_tier_is_typed_fail_with_its_own_code() -> None:
    """「真库未接」 is a DISTINCT exit from 「档未开」 — never collapsed."""
    decision = decide_search_mode(_msgs("what was revenue?"), pinned_mode="D")
    assert decision.search_mode is None
    assert decision.decision_tier == "typed_fail"
    assert decision.failure_code == REAL_DB_NOT_WIRED


def test_invalid_pin_is_typed_exit_and_falls_through() -> None:
    """A pin outside the closed set is a named ``invalid_search_mode``
    EXIT: recorded, then the cascade continues — never a crash, never
    an invented 档."""
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="E"
    )
    assert decision.search_mode is None
    assert decision.signals.get("pin_exit") == "invalid_search_mode"
    assert decision.decision_tier == "clarify"


# ---------------------------------------------------------------------------
# structured classify tier (only when rules miss and inherit would not decide)
# ---------------------------------------------------------------------------


def test_classify_valid_mode_assigns_llm_tier() -> None:
    stub = _ClassifyStub('```json\n{"search_mode": "B", "confidence": 0.9}\n```')
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), classify_model=stub
    )
    assert stub.calls == 1
    assert decision.search_mode == "B"
    assert decision.decision_tier == "llm"


def test_classify_invalid_enum_is_typed_exit_not_silent_c() -> None:
    """Boundary 'negative': a classify enum outside A|B|C|D names no 档 —
    typed EXIT, residual clarify, never a silent C."""
    stub = _ClassifyStub('{"search_mode": "E"}')
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), classify_model=stub
    )
    assert stub.calls == 1
    assert decision.search_mode is None
    assert decision.signals.get("classify_exit") == "invalid_search_mode"
    assert decision.decision_tier == "clarify"
    assert decision.options  # the window data exists for Ticket 2 to render


def test_classify_unparseable_json_is_schema_parse_exit() -> None:
    stub = _ClassifyStub("definitely not json")
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), classify_model=stub
    )
    assert decision.signals.get("classify_exit") == "schema_parse"
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_classify_model_error_is_typed_exit_not_crash() -> None:
    """Boundary 'exception': a raising classify backend records
    ``model_error`` and falls through the cascade — the run cannot crash
    and C is not the silent default."""
    stub = _ClassifyStub(raises=True)
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), classify_model=stub
    )
    assert decision.signals.get("classify_exit") == "model_error"
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_classify_low_confidence_falls_through_to_residual() -> None:
    """Boundary 'low_confidence' EXIT: an unsure C answer must not assign
    C either (silent-open ban) — the EXIT falls through."""
    stub = _ClassifyStub('{"search_mode": "B", "confidence": 0.1}')
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), classify_model=stub
    )
    assert decision.signals.get("classify_exit") == "low_confidence"
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_classify_naming_unopened_tier_is_typed_fail() -> None:
    stub = _ClassifyStub('{"search_mode": "C", "confidence": 0.95}')
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), classify_model=stub
    )
    assert decision.search_mode is None
    assert decision.decision_tier == "typed_fail"
    assert decision.failure_code == TIER_NOT_OPEN


def test_classify_skipped_when_inherit_would_decide() -> None:
    """Mirror of ``decide_track``'s peek: when inherit decides, the
    classify model is NOT consulted (a production model cannot steal the
    inherited 档)."""
    stub = _ClassifyStub('{"search_mode": "A", "confidence": 0.99}')
    snapshots = [
        {
            "company": "AAPL",
            "period": "2023 Q3",
            "document_ids": [],
            "search_mode": "B",
        }
    ]
    decision = decide_search_mode(
        _msgs("and 2022 Q3?"), snapshots=snapshots, classify_model=stub
    )
    assert stub.calls == 0
    assert decision.search_mode == "B"
    assert decision.decision_tier == "inherit"


# ---------------------------------------------------------------------------
# inherit tier
# ---------------------------------------------------------------------------


def _snap_with_mode(company: str, period: str, mode: str) -> dict[str, Any]:
    return {
        "company": company,
        "period": period,
        "document_ids": [],
        "search_mode": mode,
    }


def test_inherit_reuses_last_mode_on_short_follow_up() -> None:
    snapshots = [_snap_with_mode("AAPL", "2023 Q3", "B")]
    decision = decide_search_mode(_msgs("and 2022 Q3?"), snapshots=snapshots)
    assert decision.search_mode == "B"
    assert decision.decision_tier == "inherit"
    assert decision.signals.get("inherited_mode") == "B"


def test_inherit_blocked_after_company_change_wipe() -> None:
    """Do not inherit across a NEW retrieve job: the company-change wipe
    snapshot clears the job the old 档 belonged to."""
    snapshots = [
        _snap_with_mode("AAPL", "2023 Q3", "B"),
        {"company": "MSFT", "period": "", "document_ids": []},
    ]
    decision = decide_search_mode(
        _msgs("MSFT 2023 Q1 revenue?"), snapshots=snapshots
    )
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_inherit_requires_research_context_in_latest_snapshot() -> None:
    """A cleared / contextless snapshot cannot carry a 档 forward."""
    snapshots = [{"company": "", "period": "", "document_ids": []}]
    decision = decide_search_mode(_msgs("and 2022 Q3?"), snapshots=snapshots)
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


# ---------------------------------------------------------------------------
# clarify residual + boundary 'empty'
# ---------------------------------------------------------------------------


def test_empty_utterance_residual_clarifies_without_inventing_c() -> None:
    """Boundary 'empty': a blank post-grounded utterance with no inherit
    clarifies (window data emitted) — no 档 is invented, C is not the
    silent default."""
    decision = decide_search_mode(_msgs(""))
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.options == clarify_options()


def test_clarify_options_are_structured_with_opened_flags() -> None:
    """The Ticket-2 choice window consumes this: ids + operator labels;
    the SSOT (``TIER_UNAVAILABLE_CODE``) marks C/D unopened — no
    duplicated 档 enumeration here."""
    options = clarify_options()
    assert [o["id"] for o in options] == ["A", "B", "C", "D"]
    assert [o["label"] for o in options] == [
        MODE_LABELS[t] for t in ("A", "B", "C", "D")
    ]
    assert [o["opened"] for o in options] == [True, True, False, False]


def test_normalize_search_mode_closed_set_only() -> None:
    assert normalize_search_mode(" c ") == "C"
    assert normalize_search_mode("a") == "A"
    assert normalize_search_mode("E") is None
    assert normalize_search_mode(None) is None
    assert normalize_search_mode(3) is None


# ---------------------------------------------------------------------------
# boundary 'overflow': no extra cap, snapshot inherit does not wipe
# ---------------------------------------------------------------------------


def test_cascade_has_no_step_cap_of_its_own() -> None:
    """Search mode reuses the grounded caps only: 40 cascade resolutions
    in a row run fine with no 档-specific fuse (and no mode drifts)."""
    for _ in range(40):
        decision = decide_search_mode(_msgs("what was revenue again?"))
        assert decision.decision_tier in ("clarify", "typed_fail")
        assert decision.search_mode is None


def test_snapshot_inherit_does_not_wipe_history() -> None:
    """The mode patch is an APPEND carrying the working context — the
    prior snapshot entry is never rewritten or dropped."""
    prior = _snap_with_mode("AAPL", "2023 Q3", "B")
    patch = mode_snapshot_patch(prior, "A")
    assert len(patch) == 1
    assert patch[0]["search_mode"] == "A"
    assert patch[0]["company"] == "AAPL"
    assert patch[0]["period"] == "2023 Q3"
    assert prior == _snap_with_mode("AAPL", "2023 Q3", "B")  # no mutation


def test_mode_patch_skipped_when_latest_already_carries_mode() -> None:
    prior = _snap_with_mode("AAPL", "2023 Q3", "B")
    assert mode_snapshot_patch(prior, "B") == []
    assert mode_snapshot_patch(prior, None) == []
    assert mode_snapshot_patch(None, "B") == [
        {"company": "", "period": "", "document_ids": [], "search_mode": "B"}
    ]


# ---------------------------------------------------------------------------
# graph wiring: cascade runs ONLY after decide_track returns grounded
# ---------------------------------------------------------------------------


def test_greeting_has_no_search_mode_assignment() -> None:
    """SC2 bullet 1 (negative half): a dialogue-track run assigns no 档 —
    non-grounded tracks never reach the cascade."""
    g = _compile([AIMessage(content="Hi there.")])
    out = g.invoke(
        {**reset_input_state("Hi!"), "evidence": []},
        config=_cfg("smc-greeting"),
    )
    assert out["track"] == "dialogue"
    assert not out.get("search_mode_decision")


def test_grounded_rule_mode_assigned_before_retrieve() -> None:
    """SC2 bullet 1 (positive half): grounded + high-confidence rule
    assigns ``search_mode`` at the classify node — i.e. strictly before
    the model's retrieve tool call lands (classify is the graph entry
    node; the retrieve call below proves the tool side ran after)."""
    g = _compile(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "AAPL net sales"},
                        "id": "c-retrieve-smc",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Per the filings: net sales grew."),
        ]
    )
    out = g.invoke(
        {
            **reset_input_state("A档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-before-retrieve"),
    )
    assert out["track"] == "grounded"
    assert out["search_mode_decision"]["search_mode"] == "A"
    assert out["search_mode_decision"]["decision_tier"] == "rules"
    summaries = out.get("tool_summaries") or []
    assert any(entry.get("name") == "retrieve" for entry in summaries)


def test_graph_classify_assigns_mode_after_grounded_rules() -> None:
    """The injected classify model runs the search-mode classify exactly
    once for a grounded, un-named, non-inheritable ask (track rules did
    NOT call it)."""
    stub = _ClassifyStub('{"search_mode": "B", "confidence": 0.9}')
    g = _compile([AIMessage(content="Net sales grew year over year.")], classify_model=stub)
    out = g.invoke(
        {
            **reset_input_state("what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-classify-once"),
    )
    assert out["track"] == "grounded"
    assert stub.calls == 1
    assert out["search_mode_decision"]["search_mode"] == "B"
    assert out["search_mode_decision"]["decision_tier"] == "llm"


def test_inherit_across_two_graph_turns_without_a_picker() -> None:
    """SC2 bullet 2: turn 1 names B via the rules label; a short
    same-company follow-up inherits ``decision_tier=inherit`` — and the
    append-only snapshot history keeps every earlier entry (overflow:
    inherit never wipes)."""
    g = _compile([AIMessage(content="Answer one."), AIMessage(content="Answer two.")])
    tid = "smc-inherit-two-turns"
    out1 = g.invoke(
        {
            **reset_input_state("知识问答档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid),
    )
    assert out1["search_mode_decision"]["search_mode"] == "B"
    assert out1["search_mode_decision"]["decision_tier"] == "rules"
    snaps_after_one = list(out1.get("status_snapshots") or [])
    assert any(s.get("search_mode") == "B" for s in snaps_after_one)

    out2 = g.invoke(
        {**reset_input_state("and 2022 Q3?"), "evidence": []},
        config=_cfg(tid),
    )
    assert out2["track"] == "grounded"
    assert out2["search_mode_decision"]["decision_tier"] == "inherit"
    assert out2["search_mode_decision"]["search_mode"] == "B"
    snaps_after_two = list(out2.get("status_snapshots") or [])
    assert len(snaps_after_two) > len(snaps_after_one)
    assert snaps_after_two[: len(snaps_after_one)] == snaps_after_one


def test_search_mode_decision_is_run_scoped() -> None:
    """A new run clears the audit channel (mirror of ``track_decision``
    reset): the greeting run after a B assignment carries no stale 档."""
    g = _compile(
        [AIMessage(content="Answer one."), AIMessage(content="Hi! How can I help?")]
    )
    tid = "smc-run-scoped"
    out1 = g.invoke(
        {
            **reset_input_state("A档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid),
    )
    assert out1["search_mode_decision"]["search_mode"] == "A"
    out2 = g.invoke(
        {**reset_input_state("Hi!"), "evidence": []}, config=_cfg(tid)
    )
    assert out2["track"] == "dialogue"
    assert not out2.get("search_mode_decision")


def test_mode_channel_does_not_leak_across_threads() -> None:
    """Boundary 'concurrent' (state-safety half): the per-thread
    snapshot channel is the inherit source, so a second thread's
    short follow-up cannot inherit the first thread's 档. The same-thread
    HTTP 409 lane is already owned by
    ``tests/host/test_search_mode_named_exits.py`` (the T1 cascade is not
    an HTTP surface)."""
    g = _compile(
        [
            AIMessage(content="Answer A thread."),
            AIMessage(content="Answer B thread."),
        ]
    )
    out_a = g.invoke(
        {
            **reset_input_state("知识问答档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-leak-a"),
    )
    assert out_a["search_mode_decision"]["search_mode"] == "B"
    out_b = g.invoke(
        {**reset_input_state("and 2022 Q3?"), "evidence": []},
        config=_cfg("smc-leak-b"),
    )
    assert out_b["search_mode_decision"]["decision_tier"] == "clarify"
    assert out_b["search_mode_decision"]["search_mode"] is None


def test_typed_fail_in_utterance_names_failure_and_invents_no_mode() -> None:
    """「综述」 named in the utterance is an unopened 档: the run records
    the 具名失败 (``tier_not_open``) with ``search_mode=None`` — the
    cascade does not invent a 档, and the graph does not crash."""
    g = _compile([AIMessage(content="Net sales grew.")])
    out = g.invoke(
        {
            **reset_input_state("综述档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-typed-fail"),
    )
    assert out["track"] == "grounded"
    smd = out["search_mode_decision"]
    assert smd["search_mode"] is None
    assert smd["decision_tier"] == "typed_fail"
    assert smd["failure_code"] == TIER_NOT_OPEN
    assert out["status"] in ("done", "refuse")  # no crash either way


# ---------------------------------------------------------------------------
# run payload audit mirror (track_decision pattern)
# ---------------------------------------------------------------------------


def test_run_payload_mirrors_search_mode_decision() -> None:
    smd = {
        "search_mode": "B",
        "confidence": 0.9,
        "decision_tier": "rules",
        "signals": {"pinned": True},
        "options": [],
        "failure_code": None,
    }
    payload = _run_to_response_payload(
        {"status": "done", "answer": "ok", "search_mode_decision": smd}
    )
    assert payload["search_mode_decision"] == smd


def test_run_payload_defaults_to_empty_dict_without_decision() -> None:
    payload = _run_to_response_payload({"status": "refuse", "status_reason": "x"})
    assert payload["search_mode_decision"] == {}
