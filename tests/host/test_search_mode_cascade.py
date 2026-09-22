"""Search-mode (检索档) cascade after grounded (spec search-mode-host, Ticket 1).

Opt-in only (``uv run pytest -q -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_soft_track_ship.py``).

Five boundary classes (spec Boundaries "Test classes"), mapped to names:

* empty      -> ``test_empty_utterance_residual_clarifies_without_inventing_c``
* negative   -> ``test_jev_label_outside_the_closed_set_is_a_typed_exit`` /
                ``test_pin_of_unopened_tier_is_typed_fail_not_silent_c``
* overflow   -> ``test_cascade_has_no_step_cap_of_its_own`` /
                ``test_snapshot_inherit_does_not_wipe_history``
* concurrent -> ``test_mode_channel_does_not_leak_across_threads`` (the
                same-thread 409 lane is HTTP-surface and already owned by
                ``tests/host/test_search_mode_named_exits.py``; the T1
                cascade itself is not an HTTP surface)
* exception  -> ``test_jev_transport_error_is_a_typed_exit_not_crash``
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_SCHEMA,
    JEV_CHOICE_EXIT_TRANSPORT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoiceError,
)
from fin.host.named_failures import REAL_DB_NOT_WIRED, TIER_NOT_OPEN
from fin.host.run_payload import _run_to_response_payload
from fin.host.named_failures import LASTING_RETRIEVE_TIERS
from fin.host.search_mode_decision import (
    MODE_LABELS,
    clarify_options,
    decide_search_mode,
    mode_snapshot_patch,
    normalize_search_mode,
)
from fin.host.state import reset_input_state
from jev_fake import FakeJevChoice

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _msgs(text: str) -> list[HumanMessage]:
    return [HumanMessage(content=text)]


class _JudgeThatThrows:
    """A judge client that fails outside the typed ``JevChoiceError`` set."""

    def choose(self, **kwargs: Any) -> Any:
        raise RuntimeError("judge backend down")


def _compile(
    script: list[AIMessage],
    *,
    jev_client: Any | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        jev_client=jev_client,
        known_document_ids=KNOWN_IDS,
    )


# ---------------------------------------------------------------------------
# rules tier: a named 档 (lasting token, retired letter, or 检索档 label)
# ---------------------------------------------------------------------------


def test_rules_assign_mode_from_tier_letter() -> None:
    decision = decide_search_mode(_msgs("A档：what was AAPL net sales in 2023 Q3?"))
    assert decision.search_mode == "A"
    assert decision.decision_tier == "rules"
    assert decision.failure_code is None
    assert decision.options == []


def test_rules_assign_mode_from_context_label() -> None:
    decision = decide_search_mode(_msgs("知识问答档 what was AAPL 2023 Q3 revenue?"))
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "rules"


def test_two_different_named_tiers_stay_ambiguous_not_first_match() -> None:
    """Boundary: a naming that cannot uniquely name one 档 is ambiguous —
    rules must not pick one and silently call it high-confidence. The
    retired letter in the naming still resolves to its lasting token."""
    decision = decide_search_mode(_msgs("A档还是B档？"))
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("ambiguous_naming") == ["A", "graph"]


# ---------------------------------------------------------------------------
# optional pin / override: assigns without opening the window
# ---------------------------------------------------------------------------


def test_pin_assigns_mode_without_window() -> None:
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="B"
    )
    # The pin arrives as the retired letter an old client still sends
    # and is recorded as the lasting token the port will carry.
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("pinned") is True
    assert decision.options == []  # no window opened by a pin


def test_pin_of_unopened_tier_is_typed_fail_not_silent_c() -> None:
    """Boundary 'negative': pin 综述 (a plan-slot 档) → 具名失败 档未开;
    the residual must not invent ANY mode, and must not be a silent
    global assignment either. The refusal names the LASTING token."""
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="C"
    )
    assert decision.search_mode is None
    assert decision.decision_tier == "typed_fail"
    assert decision.failure_code == TIER_NOT_OPEN
    assert decision.signals.get("requested_mode") == "global"


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
# Jev 档 tier (only when rules miss and inherit would not decide)
# ---------------------------------------------------------------------------


def test_jev_valid_mode_assigns_the_jev_tier() -> None:
    fake = FakeJevChoice([("graph", 0.9)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert len(fake.calls) == 1
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "jev"
    # The judge's own answer travels on the audit, not just its label:
    # confidence and the ``mode_jev_*`` signal names are what make the
    # tier attributable after the fact.
    assert decision.confidence == 0.9
    assert decision.signals["mode_jev_label"] == "graph"
    assert decision.signals["mode_jev_confidence"] == 0.9
    assert decision.failure_code is None


@pytest.mark.parametrize("label", ["E", "", "Z", "research"])
def test_jev_label_outside_the_closed_set_is_a_typed_exit(label: str) -> None:
    """Boundary 'negative': a judge label the 档 SSOT cannot map at all
    names no 档 -- typed EXIT, residual clarify, never a silent global."""
    fake = FakeJevChoice([(label, 0.99)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert len(fake.calls) == 1
    assert decision.search_mode is None
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_INVALID_SEARCH_MODE
    assert decision.decision_tier == "clarify"
    assert decision.options  # the window data exists for Ticket 2 to render


@pytest.mark.parametrize("label", ["B", " b "])
def test_jev_retired_spelling_resolves_to_its_lasting_tier(label: str) -> None:
    """A retired letter is NOT out of set: the answer goes through the
    same SSOT total function, so ``B`` resolves to ``graph`` and is
    judged open, while only an unmappable label above can exit. Pinning
    this here is what keeps the alias rename from being re-litigated at
    the judge seam."""
    fake = FakeJevChoice([(label, 0.9)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "jev"
    assert "mode_jev_exit" not in decision.signals
    assert decision.signals["mode_jev_label"] == label


def test_jev_schema_error_is_a_typed_exit() -> None:
    """An unusable judge payload names nothing: the EXIT is recorded and
    the residual stays clarify."""
    fake = FakeJevChoice(
        [JevChoiceError("not a choice object", exit_code=JEV_CHOICE_EXIT_SCHEMA)]
    )
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_SCHEMA
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_jev_transport_error_is_a_typed_exit_not_crash() -> None:
    """Boundary 'exception': an unreachable judge records the transport
    EXIT and falls through the cascade -- the run cannot crash and no 档
    is the silent default."""
    fake = FakeJevChoice(
        [JevChoiceError("down", exit_code=JEV_CHOICE_EXIT_TRANSPORT)]
    )
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert len(fake.calls) == 1  # exactly one attempt; no retry storm
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_TRANSPORT
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_jev_unexpected_throw_is_contained_as_an_exit() -> None:
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"),
        jev_client=_JudgeThatThrows(),
    )
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_UNEXPECTED_ERROR
    # The type name is the whole diagnostic: an un-typed failure must be
    # attributable on the audit without leaking a message or a traceback.
    assert decision.signals["mode_jev_error_type"] == "RuntimeError"
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


@pytest.mark.parametrize("label", ["global", "graph"])
def test_jev_low_confidence_falls_through_to_residual(label: str) -> None:
    """Boundary 'low_confidence' EXIT: an unsure name must not decide,
    whether it names an unopened 档 or an OPEN one (silent-open ban) --
    the EXIT falls through either way."""
    fake = FakeJevChoice([(label, 0.1)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_LOW_CONFIDENCE
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.options  # the choice window still gets its data


def test_jev_naming_unopened_tier_is_typed_fail() -> None:
    fake = FakeJevChoice([("global", 0.95)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.search_mode is None
    assert decision.decision_tier == "typed_fail"
    assert decision.failure_code == TIER_NOT_OPEN
    # The refusal names the LASTING token it wanted, so the operator can
    # see which 档 the judge asked for.
    assert decision.signals["requested_mode"] == "global"


def test_jev_skipped_when_inherit_would_decide() -> None:
    """Mirror of ``decide_track``'s peek: when inherit decides, the
    judge is NOT consulted (a judge answer cannot steal the
    inherited 档)."""
    fake = FakeJevChoice([("A", 0.99)])
    snapshots = [
        {
            "company": "AAPL",
            "period": "2023 Q3",
            "document_ids": [],
            "search_mode": "B",
        }
    ]
    decision = decide_search_mode(
        _msgs("and 2022 Q3?"), snapshots=snapshots, jev_client=fake
    )
    assert fake.calls == []
    # A 档 stored before the rename is still inheritable, and what the
    # turn records is the lasting token.
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "inherit"
    # Inherit won the turn, so the judge was not the reason anything was
    # decided: a client-less run must not blame an absent judge on a turn
    # inherit was going to answer anyway.
    clientless = decide_search_mode(
        _msgs("and 2022 Q3?"), snapshots=snapshots, jev_client=None
    )
    assert clientless.decision_tier == "inherit"
    assert "mode_jev_exit" not in clientless.signals


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
    # A 档 recorded before the rename still carries the job forward, and
    # what this turn inherits is the lasting token.
    snapshots = [_snap_with_mode("AAPL", "2023 Q3", "B")]
    decision = decide_search_mode(_msgs("and 2022 Q3?"), snapshots=snapshots)
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "inherit"
    assert decision.signals.get("inherited_mode") == "graph"


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
    silent default, and the judge is never consulted for nothing."""
    fake: FakeJevChoice = FakeJevChoice([])  # any call would raise
    decision = decide_search_mode(_msgs(""), jev_client=fake)
    assert fake.calls == []
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.options == clarify_options()


def test_clarify_options_are_structured_with_opened_flags() -> None:
    """The Ticket-2 choice window consumes this: ids + operator labels;
    the SSOT (``TIER_UNAVAILABLE_CODE``) marks 综述/真数据库 unopened — no
    duplicated 档 enumeration here. The ids are the LASTING tokens, so a
    pick resumes with exactly the value the audit and the port carry."""
    options = clarify_options()
    assert [o["id"] for o in options] == ["A", "graph", "global", "D"]
    assert [o["label"] for o in options] == [
        MODE_LABELS[t] for t in ("A", "graph", "global", "D")
    ]
    assert [o["opened"] for o in options] == [True, True, False, False]


def test_normalize_search_mode_closed_set_only() -> None:
    assert normalize_search_mode("E") is None
    assert normalize_search_mode(None) is None
    assert normalize_search_mode(3) is None


def test_normalize_lands_every_accepted_spelling_on_a_lasting_token() -> None:
    """The rename is only real if BOTH halves resolve: a lowercase lasting
    token arrives intact, and a retired letter normalizes to it. Case and
    padding stay tolerated (a selector is a closed token, not prose), and
    the answer is always the token the port will carry."""
    for spelling, lasting in (
        ("B", "graph"),
        (" b ", "graph"),
        ("C", "global"),
        (" c ", "global"),
        ("graph", "graph"),
        ("GRAPH", "graph"),
        ("global", "global"),
        ("Global", "global"),
        ("A", "A"),
        (" a ", "A"),
        ("D", "D"),
    ):
        assert normalize_search_mode(spelling) == lasting, spelling
    assert set(LASTING_RETRIEVE_TIERS) == {
        normalize_search_mode(spelling)
        for spelling in ("a", "graph", "global", "D")
    }


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
    prior = _snap_with_mode("AAPL", "2023 Q3", "graph")
    assert mode_snapshot_patch(prior, "graph") == []
    assert mode_snapshot_patch(prior, None) == []
    assert mode_snapshot_patch(None, "graph") == [
        {"company": "", "period": "", "document_ids": [], "search_mode": "graph"}
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


def test_graph_judge_assigns_mode_after_grounded_rules() -> None:
    """The injected judge runs the 档 Choice exactly once for a grounded,
    un-named, non-inheritable ask (the track rules tier needed no
    judge call at all)."""
    fake = FakeJevChoice([("graph", 0.9)])
    g = _compile(
        [AIMessage(content="Net sales grew year over year.")], jev_client=fake
    )
    out = g.invoke(
        {
            **reset_input_state("what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-judge-once"),
    )
    assert out["track"] == "grounded"
    assert [call["question_id"] for call in fake.calls] == ["search_mode"]
    assert out["search_mode_decision"]["search_mode"] == "graph"
    assert out["search_mode_decision"]["decision_tier"] == "jev"


def test_judge_instructions_teach_the_lasting_tokens() -> None:
    """Nothing the judge is told may claim ``B`` / ``C`` are the
    lasting MCP tokens. The Choice instruction seam is where that lesson
    is taught, so the text it actually sends has to name ``graph`` /
    ``global`` -- and a reply spelled with a lasting token must assign, or
    the instruction would be offering a vocabulary the cascade rejects."""
    fake = FakeJevChoice([("graph", 0.9)])
    g = _compile(
        [AIMessage(content="Net sales grew year over year.")], jev_client=fake
    )
    out = g.invoke(
        {
            **reset_input_state("what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-judge-instructions"),
    )
    assert len(fake.calls) == 1
    instructions = fake.calls[0]["instructions"]
    assert '"graph"' not in instructions and "graph" in instructions
    assert "global" in instructions
    assert '"A"|"B"|"C"|"D"' not in instructions
    assert out["search_mode_decision"]["search_mode"] == "graph"


def test_inherit_across_two_graph_turns_without_a_picker() -> None:
    """SC2 bullet 2: turn 1 names 知识问答 via the rules label; a short
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
    assert out1["search_mode_decision"]["search_mode"] == "graph"
    assert out1["search_mode_decision"]["decision_tier"] == "rules"
    snaps_after_one = list(out1.get("status_snapshots") or [])
    assert any(s.get("search_mode") == "graph" for s in snaps_after_one)

    out2 = g.invoke(
        {**reset_input_state("and 2022 Q3?"), "evidence": []},
        config=_cfg(tid),
    )
    assert out2["track"] == "grounded"
    assert out2["search_mode_decision"]["decision_tier"] == "inherit"
    assert out2["search_mode_decision"]["search_mode"] == "graph"
    snaps_after_two = list(out2.get("status_snapshots") or [])
    assert len(snaps_after_two) > len(snaps_after_one)
    assert snaps_after_two[: len(snaps_after_one)] == snaps_after_one


def test_search_mode_decision_is_run_scoped() -> None:
    """A new run clears the audit channel (mirror of ``track_decision``
    reset): the greeting run after an assigned 档 carries no stale 档."""
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
    assert out_a["search_mode_decision"]["search_mode"] == "graph"
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
        "search_mode": "graph",
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
