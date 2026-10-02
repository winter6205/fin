"""Search-mode (检索档) cascade after grounded (spec search-mode-host, Ticket 1).

Opt-in only (``uv run pytest -q -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_soft_track_ship.py``).

Five boundary classes (spec Boundaries "Test classes"), mapped to names:

* empty      -> ``test_empty_utterance_residual_clarifies_without_inventing_c``
* negative   -> ``test_jev_label_outside_the_closed_set_is_a_typed_exit`` /
                ``test_invalid_pin_is_typed_exit_and_falls_through``
                (the two assignment-flip tests
                ``test_pin_of_the_global_tier_is_now_assigned_not_a_typed_fail``
                and ``test_pin_of_real_db_tier_is_now_assigned_not_a_typed_fail``
                record that with the refusal table empty only
                out-of-closed-set input stays a negative)
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

import fin.host.search_mode_decision as smd
from fin.host import ScriptedChatModel, compile_graph
from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_SCHEMA,
    JEV_CHOICE_EXIT_TRANSPORT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoiceClient,
    JevChoiceError,
)
from fin.host.named_failures import (
    REAL_DB_NOT_WIRED,
    TIER_UNAVAILABLE_CODE,
)
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
    """A judge client that fails outside the typed ``JevChoiceError`` set.

    The kwargs it was handed are recorded, so a containment test can also
    say what the cascade tried to send before it broke.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def choose(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
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
# rules tier: a named 档 (lasting token or 检索档 label)
# ---------------------------------------------------------------------------


def test_rules_assign_mode_from_lasting_token() -> None:
    decision = decide_search_mode(
        _msgs("lexical档：what was AAPL net sales in 2023 Q3?")
    )
    assert decision.search_mode == "lexical"
    assert decision.decision_tier == "rules"
    assert decision.failure_code is None
    assert decision.options == []


def test_rules_assign_mode_from_context_label() -> None:
    decision = decide_search_mode(_msgs("知识问答档 what was AAPL 2023 Q3 revenue?"))
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "rules"


def test_two_different_named_tiers_stay_ambiguous_not_first_match() -> None:
    """Boundary: a naming that cannot uniquely name one 档 is ambiguous —
    rules must not pick one and silently call it high-confidence."""
    decision = decide_search_mode(_msgs("lexical档还是graph档？"))
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.signals.get("ambiguous_naming") == ["lexical", "graph"]


def test_a_letter_next_to_the_marker_names_no_tier() -> None:
    """Zero coexistence at the rules tier: a retired single letter next to
    档 is ordinary prose — rules neither assign nor mark ambiguity, the ask
    falls through like any unnamed one."""
    decision = decide_search_mode(_msgs("A档还是B档？"))
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert "ambiguous_naming" not in decision.signals
    assert "tier_naming" not in decision.signals


# The 档 naming pattern is BUILT from the SSOT (token and label alternations
# joined longest-first) instead of spelling the four names by hand. That is
# only safe while the derived pattern recognises exactly what the
# hand-written one did, so both directions are asserted here through the
# PUBLIC cascade rather than against the compiled regex: every lasting token
# and every operator label still names its 档 in either word order, and a
# retired letter still names nothing at all.
_NAMING_QUESTION = "what was AAPL net sales in 2023 Q3?"

# The label an utterance names, read back off the public label map.
_MODE_BY_LABEL = {label: mode for mode, label in MODE_LABELS.items()}


def _named_mode(text: str) -> str | None:
    """The 档 the rules tier resolved for this utterance, open or not.

    An open 档 is assigned (``tier_naming``); a named-but-unopened one exits
    as 具名失败 and records the token it refused (``requested_mode``). Both
    are the rules tier having MATCHED the naming — which is what the
    alternation is under test for.
    """
    decision = decide_search_mode(_msgs(text))
    signals = decision.signals
    return signals.get("tier_naming") or signals.get("requested_mode")


@pytest.mark.parametrize("token", LASTING_RETRIEVE_TIERS)
def test_every_lasting_token_names_its_tier_in_either_word_order(
    token: str,
) -> None:
    assert _named_mode(f"{token}档 {_NAMING_QUESTION}") == token
    assert _named_mode(f"档 {token} {_NAMING_QUESTION}") == token


@pytest.mark.parametrize("label", list(MODE_LABELS.values()))
def test_every_operator_label_names_its_tier_in_either_word_order(
    label: str,
) -> None:
    expected = _MODE_BY_LABEL[label]
    assert _named_mode(f"{label}档 {_NAMING_QUESTION}") == expected
    assert _named_mode(f"档 {label} {_NAMING_QUESTION}") == expected


@pytest.mark.parametrize("letter", ["A", "B", "C", "D"])
def test_a_retired_letter_in_neither_word_order_names_a_tier(
    letter: str,
) -> None:
    assert _named_mode(f"{letter}档 {_NAMING_QUESTION}") is None
    assert _named_mode(f"档 {letter} {_NAMING_QUESTION}") is None


# ---------------------------------------------------------------------------
# optional pin / override: assigns without opening the window
# ---------------------------------------------------------------------------


def test_pin_assigns_mode_without_window() -> None:
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="graph"
    )
    # The pin arrives as the lasting token itself and is recorded as the
    # value the port will carry.
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "rules"
    assert decision.signals.get("pinned") is True
    assert decision.options == []  # no window opened by a pin


def test_pin_of_the_global_tier_is_now_assigned_not_a_typed_fail() -> None:
    """Boundary flipped by opening global (fin task 4): pin 综述 no longer
    exits 具名失败 档未开 — the cascade assigns the LASTING token at the
    rules tier, exactly as it assigns graph/lexical. This is the opposite
    of the old 「不 invent 档」 failure; the tracked change T7 flipped the ``sql`` pin the
    same way (next test), so NO closed-set pin can produce a typed fail."""
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), pinned_mode="global"
    )
    assert decision.search_mode == "global"
    assert decision.decision_tier == "rules"
    assert decision.failure_code is None
    assert decision.signals.get("pinned") is True


def test_pin_of_real_db_tier_is_now_assigned_not_a_typed_fail() -> None:
    """Flipped by the tracked change T7 (commit 6e8ed97): the old pin asserted
    ``typed_fail`` carrying ``real_db_not_wired`` — the 「真库未接」 exit
    distinct from 「档未开」. Opening sql emptied the refusal table, so
    the pin now assigns the LASTING token at the rules tier exactly like
    global/lexical, with no failure code. 「真库未接」 survives narrowed:
    it is the producer's structured not-provisioned wire token, no longer
    a cascade exit (spec search-mode-business-sql-contract §7)."""
    decision = decide_search_mode(_msgs("what was revenue?"), pinned_mode="sql")
    assert decision.search_mode == "sql"
    assert decision.decision_tier == "rules"
    assert decision.failure_code is None
    assert decision.signals.get("pinned") is True
    # And the typed-fail class has no closed-set producer left: the SSOT
    # the cascade reads is literally empty.
    assert TIER_UNAVAILABLE_CODE == {}


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


@pytest.mark.parametrize("label", ["E", "", "Z", "research", "A", "b"])
def test_jev_label_outside_the_closed_set_is_a_typed_exit(label: str) -> None:
    """Boundary 'negative': a judge label the 档 SSOT cannot map at all
    names no 档 -- typed EXIT, residual clarify, never a silent global.
    Retired single letters sit OUTSIDE the closed set now: zero
    coexistence means an ``A`` answer from the judge is refused exactly
    like ``E``, not quietly read as a spelling of some 档."""
    fake = FakeJevChoice([(label, 0.99)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert len(fake.calls) == 1
    assert decision.search_mode is None
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_INVALID_SEARCH_MODE
    assert decision.decision_tier == "clarify"
    assert decision.options  # the window data exists for Ticket 2 to render


@pytest.mark.parametrize("label", ["graph", "GRAPH", " lexical "])
def test_jev_lasting_spelling_resolves_to_its_tier(label: str) -> None:
    """The lasting tokens are the ONLY accepted spellings: every case or
    padding variant of one goes through the same SSOT total function and
    is judged open, while the retired letters above can only exit. Pinning
    this here is what keeps the rename from being re-litigated at the
    judge seam."""
    fake = FakeJevChoice([(label, 0.9)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.search_mode == label.strip().lower()
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
    judge = _JudgeThatThrows()
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"),
        jev_client=judge,
    )
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_UNEXPECTED_ERROR
    # The type name is the whole diagnostic: an un-typed failure must be
    # attributable on the audit without leaking a message or a traceback.
    assert decision.signals["mode_jev_error_type"] == "RuntimeError"
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    # What it broke on was a request carrying the per-option discriminators,
    # keyed by exactly the options the 档 cascade offered.
    (call,) = judge.calls
    assert set(call["criteria"]) == set(call["options"]) == set(
        LASTING_RETRIEVE_TIERS
    )


def test_miswired_criteria_is_contained_as_an_exit(
    monkeypatch: pytest.MonkeyPatch,
    jev_never_sent_client: tuple[JevChoiceClient, list[dict[str, Any]]],
) -> None:
    """The cascade's own landing point for a criteria table that stopped
    covering the closed set — the residue of adding a 档 to
    ``LASTING_RETRIEVE_TIERS`` without its gloss. The real client refuses
    to send it (``ValueError``), and the cascade must still fall through to
    clarify with no 档 carried, naming the throw as a wiring bug rather
    than as a judge fact on the audit.

    The 档 lane's twin at the default gate is
    ``test_miswired_leaf_criteria_is_contained_as_an_exit`` in
    ``tests/host/test_track_decision.py``: the same containment for the
    track cascade, kept OUT of this opt-in file so a criteria wiring break
    fails the plain ``uv run pytest -q`` gate too.
    """
    monkeypatch.delitem(smd._JEV_MODE_TIER_GLOSSES, "graph")

    client, sent = jev_never_sent_client
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=client
    )
    assert sent == []  # the explicit never-sent count (the fixture's raise is the belt)
    assert decision.signals["mode_jev_exit"] == JEV_CHOICE_EXIT_UNEXPECTED_ERROR
    assert decision.signals["mode_jev_error_type"] == "ValueError"
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


@pytest.mark.parametrize("label", ["global", "graph"])
def test_jev_low_confidence_falls_through_to_residual(label: str) -> None:
    """Boundary 'low_confidence' EXIT: an unsure name must not decide,
    whichever 档 it names -- the ban is on a SILENT assignment, and now
    that ``global`` is open a low-confidence 综述 is refused exactly like a
    low-confidence ``graph``. The EXIT falls through either way."""
    fake = FakeJevChoice([(label, 0.1)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_LOW_CONFIDENCE
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"
    assert decision.options  # the choice window still gets its data


def test_jev_naming_the_global_tier_is_assigned_at_the_jev_tier() -> None:
    """Opened global flipped this exit: a judge naming 综述 now assigns
    the 档 at the ``jev`` tier instead of a typed 「档未开」failure. The
    judge's own label still rides the audit, and the refusal code is gone
    (fin never invents a failure for a 档 it now runs)."""
    fake = FakeJevChoice([("global", 0.95)])
    decision = decide_search_mode(
        _msgs("what was AAPL net sales in 2023 Q3?"), jev_client=fake
    )
    assert decision.search_mode == "global"
    assert decision.decision_tier == "jev"
    assert decision.failure_code is None
    assert decision.signals["mode_jev_label"] == "global"


def test_jev_skipped_when_inherit_would_decide() -> None:
    """Mirror of ``decide_track``'s peek: when inherit decides, the
    judge is NOT consulted (a judge answer cannot steal the
    inherited 档)."""
    fake = FakeJevChoice([("lexical", 0.99)])
    snapshots = [
        {
            "company": "AAPL",
            "period": "2023 Q3",
            "document_ids": [],
            "search_mode": "graph",
        }
    ]
    decision = decide_search_mode(
        _msgs("and 2022 Q3?"), snapshots=snapshots, jev_client=fake
    )
    assert fake.calls == []
    # Inherit reads the durable history, so the stored lasting token is
    # what the turn records.
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
    snapshots = [_snap_with_mode("AAPL", "2023 Q3", "graph")]
    decision = decide_search_mode(_msgs("and 2022 Q3?"), snapshots=snapshots)
    assert decision.search_mode == "graph"
    assert decision.decision_tier == "inherit"
    assert decision.signals.get("inherited_mode") == "graph"


def test_inherit_ignores_a_retired_letter_in_history() -> None:
    """Zero coexistence reaches the durable channel too: a stray retired
    letter is not a 档 spelling, so it carries no job forward — the short
    follow-up gets the residual clarify rather than an inherited mode."""
    snapshots = [_snap_with_mode("AAPL", "2023 Q3", "B")]
    decision = decide_search_mode(_msgs("and 2022 Q3?"), snapshots=snapshots)
    assert decision.search_mode is None
    assert decision.decision_tier == "clarify"


def test_inherit_blocked_after_company_change_wipe() -> None:
    """Do not inherit across a NEW retrieve job: the company-change wipe
    snapshot clears the job the old 档 belonged to."""
    snapshots = [
        _snap_with_mode("AAPL", "2023 Q3", "graph"),
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


def test_clarify_options_are_structured_with_opened_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Ticket-2 choice window consumes this: ids + operator labels;
    the ``opened`` flag reads the ``TIER_UNAVAILABLE_CODE`` SSOT — empty
    since the tracked change T7 (commit 6e8ed97) opened sql, so ALL FOUR 档 now render
    opened (old pin: ``[True, True, True, False]`` with 真数据库 marked
    unopened). Equal-or-stronger replaces the frozen fourth slot with a
    two-sided proof: the current table value renders all-open, AND the
    flag still FOLLOWS the table — monkeypatching a row back in flips
    exactly that option and nothing else, which is what keeps the
    mechanism honest for the next plan slot. No duplicated 档
    enumeration here; the ids are the LASTING tokens, so a pick resumes
    with exactly the value the audit and the port carry."""
    options = clarify_options()
    assert [o["id"] for o in options] == list(LASTING_RETRIEVE_TIERS)
    assert [o["label"] for o in options] == [
        MODE_LABELS[t] for t in LASTING_RETRIEVE_TIERS
    ]
    assert [o["opened"] for o in options] == [True] * len(options)

    monkeypatch.setitem(smd.TIER_UNAVAILABLE_CODE, "sql", REAL_DB_NOT_WIRED)
    flipped = clarify_options()
    assert [o["opened"] for o in flipped] == [
        o["id"] != "sql" for o in options
    ], flipped


def test_normalize_search_mode_closed_set_only() -> None:
    assert normalize_search_mode("E") is None
    assert normalize_search_mode(None) is None
    assert normalize_search_mode(3) is None


def test_normalize_lands_every_accepted_spelling_on_a_lasting_token() -> None:
    """The closed set alone: each lasting token arrives intact, case and
    padding stay tolerated (a selector is a closed token, not prose), and
    the answer is always the token the port will carry. Every retired
    letter — whatever its case or padding — resolves to NOTHING: with zero
    coexistence there is no alias left to land on."""
    for spelling, lasting in (
        ("lexical", "lexical"),
        ("LEXICAL", "lexical"),
        (" lexical ", "lexical"),
        ("graph", "graph"),
        ("GRAPH", "graph"),
        ("global", "global"),
        ("Global", "global"),
        ("sql", "sql"),
        ("SQL", "sql"),
    ):
        assert normalize_search_mode(spelling) == lasting, spelling
    for letter in ("A", "B", "C", "D", " a ", "b", "C", "d"):
        assert normalize_search_mode(letter) is None, letter


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
    prior = _snap_with_mode("AAPL", "2023 Q3", "graph")
    patch = mode_snapshot_patch(prior, "lexical")
    assert len(patch) == 1
    assert patch[0]["search_mode"] == "lexical"
    assert patch[0]["company"] == "AAPL"
    assert patch[0]["period"] == "2023 Q3"
    assert prior == _snap_with_mode("AAPL", "2023 Q3", "graph")  # no mutation


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
            **reset_input_state("lexical档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-before-retrieve"),
    )
    assert out["track"] == "grounded"
    assert out["search_mode_decision"]["search_mode"] == "lexical"
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
    """Nothing the judge is told may claim a retired letter is a lasting
    MCP token. The Choice instruction seam is where that lesson is taught,
    so the text it actually sends has to name the four lasting tokens --
    and a reply spelled with one must assign, or the instruction would be
    offering a vocabulary the cascade rejects."""
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
    assert all(token in instructions for token in LASTING_RETRIEVE_TIERS)
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
            **reset_input_state("graph档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg(tid),
    )
    assert out1["search_mode_decision"]["search_mode"] == "graph"
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


def test_naming_the_global_tier_in_the_utterance_assigns_it() -> None:
    """Flipped by opening global (fin task 4): 「综述」 named in the
    utterance is now an OPEN 档 — the cascade assigns ``search_mode=global``
    at the rules tier instead of recording a ``tier_not_open`` failure. The
    graph still runs cleanly either way."""
    g = _compile([AIMessage(content="Net sales grew.")])
    out = g.invoke(
        {
            **reset_input_state("综述档 what was AAPL net sales in 2023 Q3?"),
            "evidence": [],
        },
        config=_cfg("smc-global-assigned"),
    )
    assert out["track"] == "grounded"
    mode_decision = out["search_mode_decision"]
    assert mode_decision["search_mode"] == "global"
    assert mode_decision["decision_tier"] == "rules"
    assert mode_decision["failure_code"] is None
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
