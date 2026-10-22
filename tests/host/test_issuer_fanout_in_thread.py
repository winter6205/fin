"""Multi-issuer fan-out inside the host graph (T6 / spec Focus 4, SC7).

ADR 0034 fixes the topology: the host graph fans out one node per
issuer, JOINS, and only then runs the **citation verifier** once on the
synthesized answer. This suite pins that shape end to end:

* a scripted turn at the cap synthesizes with >=1 citation for every
  issuer that contributed evidence;
* one issuer over the cap is a typed ``issuer_overflow`` that ends the
  run — never a ``done`` that silently drops the extra issuer;
* an issuer with no hits and an issuer whose retrieve is unavailable
  stay distinguishable in the joined envelope instead of vanishing;
* Soft-Track classify stays flat — the fan-out lives after the
  research leaf, not inside the track decision.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.host.tools.issuer_fanout import (
    ISSUER_FANOUT_SCHEMA,
    ISSUER_FANOUT_TOOL,
    MAX_ISSUERS,
    issuer_query,
)
from fin.host.workspace_http import _node_frame_payloads
from fin.retrieve import FakeRetrievePort
from fin.retrieve.port import Chunk
from search_mode_pins import RETIRED_LETTERS


QUERY = "services revenue"
CAP_ROSTER = ["AAPL", "MSFT", "AMZN", "NVDA", "GOOG"]
OVER_CAP_ROSTER = CAP_ROSTER + ["TSLA"]

# Both Soft-Track slots (a known company + a period) are present so the
# rules tier lands on ``research``; a slot-less ask routes to clarify
# and never reaches a tool.
FANOUT_QUESTION = (
    "Compare services revenue across AAPL, MSFT, AMZN, NVDA and GOOG "
    "for Q3 2023."
)


def _document_id(issuer: str) -> str:
    return f"{issuer}-2023Q3"


def _version(issuer: str) -> str:
    return (issuer[0].lower() * 64)[:64]


# One evidence passage per issuer. Each is >=32 characters (the gate's
# minimum extractive span) and shares no 32-character run with any
# other, so a citation can only come from the issuer it belongs to —
# boilerplate passages would let one issuer's span vouch for another
# and the coverage contract below would pass vacuously. Digit-free on
# purpose: a number would be a claim the numeric gate has to match.
_PASSAGES = {
    "AAPL": "Apple grew services and wearables faster than hardware.",
    "MSFT": "Microsoft cloud bookings expanded on enterprise renewals.",
    "AMZN": "Third-party seller fees lifted the marketplace mix again.",
    "NVDA": "Accelerated computing demand outran the available supply.",
    "GOOG": "Search monetization held steady as cloud losses narrowed.",
    "TSLA": "Energy storage deployments doubled on a softer vehicle mix.",
}


def _page_text(issuer: str) -> str:
    return _PASSAGES[issuer]


def _chunk(issuer: str, unit: int = 1) -> Chunk:
    return Chunk(
        text=_page_text(issuer),
        source_ref=Locator(
            document_id=_document_id(issuer),
            version=_version(issuer),
            unit=unit,
        ),
        score=1.0,
    )


KNOWN_IDS = {_document_id(issuer) for issuer in OVER_CAP_ROSTER}


class RecordingPort:
    """Retrieve port that records every query and can fail one issuer.

    Wraps a ``FakeRetrievePort`` so the hit path stays the shared
    contract; ``unavailable_for`` reproduces the ADR 0030 typed EXIT
    for a single issuer without a live MCP process.
    """

    def __init__(
        self,
        *,
        issuers: list[str],
        unavailable_for: str | None = None,
    ) -> None:
        self._fake = FakeRetrievePort(
            hits_by_query={
                issuer_query(issuer, QUERY): [_chunk(issuer)]
                for issuer in issuers
            }
        )
        self._unavailable_for = unavailable_for
        self.queries: list[str] = []
        # The 档 each branch call carried (search-mode SC2): ``None`` is
        # "no assignment" — the typed fail — which the port records as no
        # key sent. The routing residual is an assignment now (ADR 0104),
        # so it arrives as the recorded default ``graph``.
        self.modes: list[Any] = []

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(query)
        self.modes.append(kwargs.get("search_mode"))
        ticker = kwargs.get("ticker")
        if self._unavailable_for and ticker == self._unavailable_for:
            return {"error": "retrieve_unavailable"}
        return self._fake.retrieve(query, **kwargs)


def _fanout_script(
    *, issuers: list[str], answer: str, query: str = QUERY
) -> list[AIMessage]:
    """``research_issuers`` fan-out, then the synthesized prose."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": ISSUER_FANOUT_TOOL,
                    "args": {"issuers": issuers, "query": query},
                    "id": "c-fanout",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _synthesis(issuers: list[str]) -> str:
    return " ".join(_page_text(issuer) for issuer in issuers)


def _run(
    script: list[AIMessage],
    *,
    adapter: Any,
    thread_id: str,
    question: str = FANOUT_QUESTION,
    config_extra: dict[str, Any] | None = None,
    ledger: Any | None = None,
) -> dict:
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        adapter=adapter,
        known_document_ids=KNOWN_IDS,
        tool_ledger=ledger,
    )
    configurable: dict[str, Any] = {"thread_id": thread_id}
    configurable.update(config_extra or {})
    return graph.invoke(
        reset_input_state(question),
        config={"configurable": configurable},
    )


def _tool_messages(state: dict, name: str) -> list[ToolMessage]:
    return [
        message
        for message in state["messages"]
        if isinstance(message, ToolMessage)
        and getattr(message, "name", "") == name
    ]


# ----- wiring -----


def test_compile_graph_binds_research_issuers_after_compare_facts() -> None:
    """Additive binding: the fan-out tool is registered LAST so the five
    prior tools keep their positions."""
    model = ScriptedChatModel(script=[])
    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(),
        known_document_ids=KNOWN_IDS,
    )
    names = [getattr(t, "name", repr(t)) for t in model.bound_tools]
    assert ISSUER_FANOUT_TOOL in names
    assert names.index(ISSUER_FANOUT_TOOL) > names.index("compare_facts")


def test_reset_input_state_clears_the_fanout_channels() -> None:
    """A follow-up run must not inherit the previous run's roster."""
    fresh = reset_input_state("q")
    assert fresh["issuer_plan"] == {}
    assert fresh["issuer_fanout"] == {}


# ----- SC7 positive: full cap synthesizes with a citation per contributor -----


def test_full_cap_fanout_synthesizes_with_a_citation_per_contributor() -> None:
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(
            issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)
        ),
        adapter=port,
        thread_id="t-fanout-cap",
    )
    assert len(CAP_ROSTER) == MAX_ISSUERS
    assert out["status"] == "done"
    assert out["status_reason"] == "done"
    cited = {citation.document_id for citation in out["citations"]}
    assert cited == {_document_id(issuer) for issuer in CAP_ROSTER}


def test_fanout_branches_retrieve_under_the_assigned_档() -> None:
    """SC2 through the fan-out hop: the 档 the cascade assigned this run
    rides EVERY branch call, stamped by the router off the run's state —
    never by the model's tool args, which carry only roster + query."""
    port = RecordingPort(issuers=["AAPL", "MSFT"])
    out = _run(
        _fanout_script(issuers=["AAPL", "MSFT"], answer=_synthesis(["AAPL", "MSFT"])),
        adapter=port,
        thread_id="t-fanout-mode",
        config_extra={"pinned_search_mode": "graph"},
    )
    assert out["search_mode_decision"]["search_mode"] == "graph"
    assert len(port.modes) == 2, port.queries
    assert port.modes == ["graph", "graph"], port.modes


def test_fanout_branches_carry_the_fallback_档_when_the_cascade_cannot_decide() -> None:
    """The residual IS an assignment (ADR 0104): a turn the cascade could
    not route gets the recorded default ``graph`` at the ``fallback`` tier,
    so every branch call carries that 档.

    This replaces the old "nothing is assigned -> no 档 on the branches"
    premise: the routing residual is no longer an unassigned turn, and a
    branch hop may not invent a 档 of its own either — the ONE token the
    audit names is the token every branch rides, on every branch.
    """
    port = RecordingPort(issuers=["AAPL", "MSFT"])
    out = _run(
        _fanout_script(issuers=["AAPL", "MSFT"], answer=_synthesis(["AAPL", "MSFT"])),
        adapter=port,
        thread_id="t-fanout-no-mode",
    )
    audit = out["search_mode_decision"]
    assert audit["decision_tier"] == "fallback", audit
    assert audit["search_mode"] == "graph", audit
    assert audit["confidence"] == 0.0, audit
    # No mode picker is opened by a routing decision, so the options stay
    # empty — the residual may not resurrect the retired window.
    assert audit["options"] == [], audit
    assert port.modes, "the branches still retrieved"
    assert port.modes == ["graph", "graph"], port.modes


@pytest.mark.parametrize("letter", RETIRED_LETTERS)
def test_fanout_branches_carry_no_档_when_the_pin_is_refused(letter: str) -> None:
    """The typed fail is the only hop that still sends no 档: an
    explicitly pinned token outside the closed set stays a NAMED
    rejection (``invalid_search_mode``), so the port is handed ``None``
    instead of the recorded default.

    This keeps the coverage the retired "nothing is assigned" case used
    to carry: the fallback must never reinterpret a refused explicit
    selection as an answer, and the branches must not paper over it.
    The out-of-set spelling is the shared retired-letter pin, so the
    refusal is proven for every spelling the closed set dropped.
    """
    port = RecordingPort(issuers=["AAPL", "MSFT"])
    out = _run(
        _fanout_script(issuers=["AAPL", "MSFT"], answer=_synthesis(["AAPL", "MSFT"])),
        adapter=port,
        thread_id=f"t-fanout-refused-pin-{letter}",
        config_extra={"pinned_search_mode": letter},
    )
    audit = out["search_mode_decision"]
    assert audit["decision_tier"] == "typed_fail", audit
    assert audit["failure_code"] == "invalid_search_mode", audit
    assert audit["search_mode"] is None, audit
    assert port.modes, "the branches still retrieved"
    assert set(port.modes) == {None}, port.modes


def test_fanout_runs_one_worker_per_issuer() -> None:
    """The fan-out is per issuer and bounded by the cap: five issuers,
    five port calls, no re-entry."""
    port = RecordingPort(issuers=CAP_ROSTER)
    _run(
        _fanout_script(issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)),
        adapter=port,
        thread_id="t-fanout-workers",
    )
    assert sorted(port.queries) == sorted(
        issuer_query(issuer, QUERY) for issuer in CAP_ROSTER
    )


def test_join_envelope_lands_on_state_for_the_turn_row() -> None:
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)),
        adapter=port,
        thread_id="t-fanout-envelope",
    )
    fanout = out["issuer_fanout"]
    assert fanout["schema"] == ISSUER_FANOUT_SCHEMA
    assert fanout["contributing"] == CAP_ROSTER
    assert fanout["cap"] == MAX_ISSUERS
    assert fanout["locator_count"] == len(CAP_ROSTER)


def test_fanout_narrates_one_human_readable_tool_summary() -> None:
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)),
        adapter=port,
        thread_id="t-fanout-summary",
    )
    summaries = {s["name"]: s["summary"] for s in out["tool_summaries"]}
    assert "5 issuers" in summaries[ISSUER_FANOUT_TOOL]
    assert "{" not in summaries[ISSUER_FANOUT_TOOL]


def test_workspace_narrates_the_join_on_the_tool_result_frame() -> None:
    """No new SSE frame type: the fan-out rides the tool trace the
    workspace already renders, so the operator sees the roster without
    the thread surface learning a sixth envelope."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)),
        adapter=port,
        thread_id="t-fanout-sse",
    )
    message = _tool_messages(out, ISSUER_FANOUT_TOOL)[0]
    frames = dict(_node_frame_payloads(message))
    assert frames["tool_result"]["name"] == ISSUER_FANOUT_TOOL
    assert "5 issuers" in frames["tool_result"]["summary"]
    assert "{" not in frames["tool_result"]["summary"]


def test_the_join_answers_the_fanout_call_exactly_once() -> None:
    """One tool call, one ``ToolMessage``: the verifier runs after the
    join, not once per issuer (ADR 0034 rejects verifier-per-issuer)."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)),
        adapter=port,
        thread_id="t-fanout-join-once",
    )
    messages = _tool_messages(out, ISSUER_FANOUT_TOOL)
    assert len(messages) == 1
    assert messages[0].tool_call_id == "c-fanout"


def test_soft_track_classify_stays_flat_for_a_fanout_turn() -> None:
    """ADR 0034: the fan-out is a host-graph topology change, not a new
    track — classify still resolves the turn on the rules tier."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER)),
        adapter=port,
        thread_id="t-fanout-flat",
    )
    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "rules"


# ----- SC7 negative: over the cap is a typed overflow -----


def test_over_cap_roster_is_a_typed_overflow_not_a_done() -> None:
    port = RecordingPort(issuers=OVER_CAP_ROSTER)
    out = _run(
        _fanout_script(
            issuers=OVER_CAP_ROSTER, answer=_synthesis(OVER_CAP_ROSTER)
        ),
        adapter=port,
        thread_id="t-fanout-overflow",
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "issuer_overflow"
    assert tuple(out.get("citations") or ()) == ()


def test_overflow_records_the_limit_and_the_full_ask() -> None:
    port = RecordingPort(issuers=OVER_CAP_ROSTER)
    out = _run(
        _fanout_script(
            issuers=OVER_CAP_ROSTER, answer=_synthesis(OVER_CAP_ROSTER)
        ),
        adapter=port,
        thread_id="t-fanout-overflow-shape",
    )
    plan = out["issuer_plan"]
    assert plan["error"] == "issuer_overflow"
    assert plan["limit"] == MAX_ISSUERS
    assert plan["requested"] == len(OVER_CAP_ROSTER)
    assert plan["issuers"] == OVER_CAP_ROSTER


def test_overflow_runs_no_issuer_worker() -> None:
    """"Not a hang" means the cap is checked on the REQUEST: the graph
    never fans out the over-cap roster and then throws the work away."""
    port = RecordingPort(issuers=OVER_CAP_ROSTER)
    _run(
        _fanout_script(
            issuers=OVER_CAP_ROSTER, answer=_synthesis(OVER_CAP_ROSTER)
        ),
        adapter=port,
        thread_id="t-fanout-overflow-workers",
    )
    assert port.queries == []


def test_overflow_still_answers_the_tool_call() -> None:
    """The typed EXIT reaches the message history (and so the workspace
    tool trace) instead of the run ending on a dangling tool call."""
    port = RecordingPort(issuers=OVER_CAP_ROSTER)
    out = _run(
        _fanout_script(
            issuers=OVER_CAP_ROSTER, answer=_synthesis(OVER_CAP_ROSTER)
        ),
        adapter=port,
        thread_id="t-fanout-overflow-trace",
    )
    messages = _tool_messages(out, ISSUER_FANOUT_TOOL)
    assert len(messages) == 1
    assert "issuer_overflow" in messages[0].content


# ----- empty / negative / exception issuers -----


def test_issuer_without_hits_is_reported_empty_not_dropped() -> None:
    roster = ["AAPL", "MSFT", "NVDA"]
    port = RecordingPort(issuers=["AAPL", "MSFT"])
    out = _run(
        _fanout_script(issuers=roster, answer=_synthesis(["AAPL", "MSFT"])),
        adapter=port,
        thread_id="t-fanout-empty-issuer",
    )
    assert out["status"] == "done"
    fanout = out["issuer_fanout"]
    assert fanout["contributing"] == ["AAPL", "MSFT"]
    assert fanout["empty"] == ["NVDA"]


def test_unavailable_retrieve_for_one_issuer_stays_typed() -> None:
    roster = ["AAPL", "MSFT"]
    port = RecordingPort(issuers=roster, unavailable_for="MSFT")
    out = _run(
        _fanout_script(issuers=roster, answer=_synthesis(["AAPL"])),
        adapter=port,
        thread_id="t-fanout-unavailable",
    )
    fanout = out["issuer_fanout"]
    assert fanout["unavailable"] == ["MSFT"]
    assert fanout["empty"] == []
    assert fanout["contributing"] == ["AAPL"]


def test_empty_roster_is_a_typed_exit_the_model_can_see() -> None:
    """No issuers at all is ``empty_fanout``: a typed EXIT on the tool
    message, not an overflow and not a crash."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(issuers=[], answer="No issuers were named."),
        adapter=port,
        thread_id="t-fanout-empty-roster",
    )
    messages = _tool_messages(out, ISSUER_FANOUT_TOOL)
    assert "empty_fanout" in messages[0].content
    assert out["status_reason"] != "issuer_overflow"
    # The run still terminates (the citation verifier had nothing to
    # match, so it bounces to the repair cap and refuses).
    assert out["status"] in ("refuse", "done", "step_cap")
    assert port.queries == []


# ----- the request is validated against the canonical contract ------------


def _raw_fanout_call(issuers: Any, query: str = QUERY) -> AIMessage:
    """A fan-out call whose ``issuers`` is whatever the model typed.

    ``args`` are passed through untouched on purpose: the point of these
    cases is that the RUNTIME refusal happens before the planner can
    raise or fan out per character.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": ISSUER_FANOUT_TOOL,
                "args": {"issuers": issuers, "query": query},
                "id": "c-fanout",
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.parametrize(
    "issuers",
    [5, "AAPL"],
    ids=["wrong-type-int", "wrong-type-string"],
)
def test_a_wrong_typed_roster_is_refused_before_the_fanout(
    issuers: Any,
) -> None:
    """A non-list roster is a schema refusal, not a TypeError or a fan-out.

    ``5`` used to raise ``TypeError`` inside the planner and ``"AAPL"``
    used to fan out per character; both now answer the model with the
    same value-free payload the ordinary tool lane uses.
    """
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        [_raw_fanout_call(issuers), AIMessage(content="Answer.")],
        adapter=port,
        thread_id=f"t-fanout-invalid-{type(issuers).__name__}",
    )

    messages = _tool_messages(out, ISSUER_FANOUT_TOOL)
    assert len(messages) == 1, messages
    payload = json.loads(str(messages[0].content))
    assert payload["error"] == "invalid_tool_arguments"
    # Value-free: the submitted roster is not echoed back.
    assert str(issuers) not in str(messages[0].content)
    assert "issuers" in payload["feedback"]
    # No branch opened and no worker ran.
    assert port.queries == []
    assert out.get("issuer_findings") == []
    assert out.get("issuer_plan", {}).get("error") == "invalid_tool_arguments"


def test_a_refused_roster_is_never_a_type_error() -> None:
    """The refusal is a value the run carries, not an exception."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        [_raw_fanout_call(5), AIMessage(content="Answer.")],
        adapter=port,
        thread_id="t-fanout-no-typeerror",
    )
    assert out["messages"]
    assert "TypeError" not in str(out["messages"])
    assert "Traceback" not in str(out["messages"])


class _RecordingLedger:
    """The ledger port, captured in memory."""

    def __init__(self) -> None:
        self.observations: list[Any] = []

    def record(
        self,
        observations: Any,
        *,
        thread_id: str,
        run_id: str,
        attempt_id: str,
        attempt_generation: int,
        thread_generation: int,
    ) -> int:
        self.observations.extend(observations)
        return len(observations)

    def read_run(self, run_id: str) -> Any:
        return ()


def test_a_refused_roster_records_its_validation_facts() -> None:
    """The refusal is a ledger fact, not a silent rejection.

    The refused fan-out commits the same schema verdict the ordinary
    tool lane commits, so the measurement source sees the rejection.
    """
    ledger = _RecordingLedger()
    port = RecordingPort(issuers=CAP_ROSTER)
    _run(
        [_raw_fanout_call(5), AIMessage(content="Answer.")],
        adapter=port,
        thread_id="t-fanout-validation-facts",
        ledger=ledger,
        config_extra={
            "runtime_run_id": "run-fanout-1",
            "runtime_attempt_id": "attempt-1",
            "runtime_attempt_generation": 1,
            "thread_generation": 1,
        },
    )

    kinds = [observation.kind for observation in ledger.observations]
    assert kinds == ["schema_rejected", "correction_offered"], kinds
    assert {
        observation.tool_name for observation in ledger.observations
    } == {ISSUER_FANOUT_TOOL}
    # The fact carries the field path, never the submitted roster.
    assert ledger.observations[0].field_paths == ("issuers",)
    assert "5" not in repr(ledger.observations)


# ----- the single citation verifier still gates the synthesis -----


def test_contributing_issuer_dropped_from_the_answer_does_not_done() -> None:
    """SC7: every issuer that contributed evidence needs >=1 citation.
    An answer that quotes four of five contributors dropped one — that
    is exactly the silent drop the spec forbids."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(
            issuers=CAP_ROSTER, answer=_synthesis(CAP_ROSTER[:-1])
        ),
        adapter=port,
        thread_id="t-fanout-partial",
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "issuer_coverage_fail"
    assert tuple(out.get("citations") or ()) == ()


def test_an_uncited_synthesis_still_bounces_to_the_repair_cap() -> None:
    """The verifier is unchanged: prose with no verbatim span never
    reaches ``done`` just because the fan-out gathered evidence."""
    port = RecordingPort(issuers=CAP_ROSTER)
    out = _run(
        _fanout_script(
            issuers=CAP_ROSTER,
            answer="All five issuers grew, broadly speaking, this quarter.",
        ),
        adapter=port,
        thread_id="t-fanout-uncited",
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "citation_repair_cap"


# ----- run scoping -----


def test_a_follow_up_run_does_not_inherit_the_previous_roster() -> None:
    port = RecordingPort(issuers=CAP_ROSTER)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=_fanout_script(
                issuers=["AAPL"], answer=_synthesis(["AAPL"])
            )
        ),
        adapter=port,
        known_document_ids=KNOWN_IDS,
    )
    config = {"configurable": {"thread_id": "t-fanout-followup"}}
    first = graph.invoke(reset_input_state(FANOUT_QUESTION), config=config)
    assert first["issuer_fanout"]["contributing"] == ["AAPL"]

    second = graph.invoke(
        reset_input_state("And AAPL for Q3 2023 overall?"), config=config
    )
    # The scripted model has run out of tool calls, so run 2 makes no
    # fan-out of its own; the channels must be empty rather than
    # replaying run 1's roster onto this turn.
    assert second["issuer_fanout"] == {}
    assert second["issuer_plan"] == {}
    assert second["issuer_findings"] == []
