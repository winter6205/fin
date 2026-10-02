"""The cascade-assigned 档 reaches the retrieve port (SC2 / SC3 hop).

The rename (T1–T3) only matters once the 档 the operator chose is the
value the RANKING SERVICE reads. Until this ticket the hop was missing:
``fin.retrieve.tool.retrieve`` passed its ``search_mode`` through and the
host tool never supplied it, so every grounded run reached
``resolve_search_mode(None)`` and the wire carried NO
``filters.search_mode`` key — the operator's 查找 vs 知识问答 choice changed
the injected copy text and nothing else.

What is pinned here, in order:

* the POSITIVE hop — an assigned 知识问答 sends ``graph``, an assigned
  查找 sends ``lexical``, an assigned 综述 sends ``global``, and — since
  #322 T7 (6e8ed97) opened 真数据库 — an assigned sql turn sends ``sql``
  with the port's ``sql_window_*`` pair recorded beside it: all four
  lasting tokens ride the same hop (spec SC2);
* the ABSENT assignment — an unassigned turn (clarify residual) sends no
  档 at all (``None`` → the port omits the key → iknow's own default);
  routing-time refusals have zero producers since the table emptied, and
  the hop may never swap a named 档 for a substitute one (SC3 / SC6);
* the INVALID assignment — the hop forwards the audit VERBATIM, so a
  retired letter that somehow survives in state is named by the port as
  ``invalid_search_mode`` instead of being guessed into a 档;
* the LLM-visible signature — 档 is never a model argument (ADR 0061:
  the cascade is the single source of truth, the model cannot pick or
  lie about it);
* the cache-safety property — the compiled graph is cached and shared,
  so the 档 must be resolved PER CALL from that run's state; two runs on
  one cached graph carry their own (or no) 档.

Fake port + scripted model only: what is asserted is the TOKEN a call
carries, never how iknow ranks (spec SC8).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host.graph import (
    _make_retrieve_tool,
    compile_graph,
)
from fin.host.named_failures import RETRIEVE_TIERS
from fin.host.scripted_model import ScriptedChatModel
from fin.host.search_mode_decision import assigned_retrieve_mode
from fin.host.state import reset_input_state
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.port import (
    Chunk,
    InvalidSearchModeError,
    resolve_search_mode,
)
from search_mode_pins import RETIRED_LETTERS

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3"}
PLAIN_ASK = "what was AAPL net sales in 2023 Q3?"


def _utterance(spelling: str) -> str:
    return f"{spelling}档 {PLAIN_ASK}"


def _hit() -> Chunk:
    return Chunk(
        text="Apple net sales for fiscal 2023 third quarter grew.",
        source_ref=Locator(
            document_id="AAPL-2022Q3", version="a" * 64, unit=1
        ),
        score=1.0,
    )


def _port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={"revenue": [_hit()]},
        known_document_ids=KNOWN_IDS,
    )


def _script() -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue"},
                    "id": "c-hop",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Per the filings: revenue grew."),
    ]


def _compile(
    port: FakeRetrievePort,
    checkpointer: Any | None = None,
    model: Any | None = None,
) -> Any:
    """One cached-eligible graph: same model/checkpointer types as the
    sibling suite, so the only variable under test is the 档."""
    return compile_graph(
        checkpointer=(
            checkpointer
            if checkpointer is not None
            else InMemorySaver()
        ),
        model=(
            model
            if model is not None
            else ScriptedChatModel(script=_script())
        ),
        adapter=port,
        known_document_ids=KNOWN_IDS,
    )


def _run(graph: Any, text: str, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        {**reset_input_state(text), "evidence": []},
        config={"configurable": {"thread_id": thread_id}},
    )


def _modes(port: FakeRetrievePort, *, since: int = 0) -> list[Any]:
    return [call["search_mode"] for call in port.calls[since:]]


# ----- the positive hop (SC2) -----


@pytest.mark.parametrize(
    ("spelling", "lasting"),
    [("graph", "graph"), ("GRAPH", "graph"), ("知识问答", "graph")],
)
def test_an_assigned_知识问答_sends_graph_on_the_port(
    spelling: str, lasting: str
) -> None:
    port = _port()
    out = _run(_compile(port), _utterance(spelling), "hop-qa")

    assert out["track"] == "grounded"
    assert out["search_mode_decision"]["search_mode"] == lasting
    modes = _modes(port)
    assert modes, "the grounded retrieve must have reached the port"
    assert modes == [lasting] * len(modes), modes


def test_an_assigned_查找_sends_lexical_on_the_port() -> None:
    """lexical is a real open 档 and proves the hop is not a
    hardcoded 知识问答 default."""
    port = _port()
    out = _run(_compile(port), _utterance("lexical"), "hop-lexical")

    assert out["search_mode_decision"]["search_mode"] == "lexical"
    assert _modes(port) == ["lexical"]


def test_an_assigned_综述_sends_global_on_the_port() -> None:
    """综述 opened into a real report arm: naming it is now the POSITIVE
    hop, not a refusal — the port is handed exactly ``global``, never
    ``None``, never a substitute 档."""
    port = _port()
    out = _run(_compile(port), _utterance("global"), "hop-global")

    assert out["track"] == "grounded"
    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "global"
    assert audit["failure_code"] is None
    modes = _modes(port)
    assert modes, "the grounded retrieve must have reached the port"
    assert modes == ["global"] * len(modes), modes


# ----- the absent assignment (SC2 / SC6) -----


def test_an_assigned_真数据库_sends_sql_and_never_a_substitute() -> None:
    """Flipped by #322 T7 (commit 6e8ed97): the old pin asserted the
    cascade typed-failed sql (``failure_code == real_db_not_wired``,
    ``modes == [None]``) so the hop could never smuggle it onto the
    wire. The 档 is OPEN now: naming it ASSIGNS it, the hop sends
    exactly ``sql`` — never ``None``, never a graph/lexical substitute
    (the refused-graph-downgrade trap this replaces) — and the port
    records the sql 档's window pair beside the token (values ``None``
    until a window source exists; the KEYS are the port shape)."""
    port = _port()
    out = _run(_compile(port), _utterance("sql"), "hop-sql")

    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "sql"
    assert audit["failure_code"] is None
    assert audit["decision_tier"] == "rules"
    assert audit["signals"].get("tier_naming") == "sql"
    modes = _modes(port)
    assert modes, "the grounded retrieve must have reached the port"
    assert modes == ["sql"] * len(modes), modes
    # No substitute ever rode this hop: not a None, not another 档.
    assert not set(modes) - {"sql"}
    for call in port.calls:
        assert "sql_window_start" in call and "sql_window_end" in call, call


# ----- the invalid assignment (SC3) -----


def test_the_hop_forwards_the_audit_verbatim_and_the_port_names_it() -> None:
    """No normalization inside the hop: the audit is the cascade's own
    output, so a retired letter sitting in it means the boundary was
    skipped — the port must NAME that, not quietly pick a 档."""
    assert assigned_retrieve_mode(
        {"search_mode_decision": {"search_mode": "B"}}
    ) == "B"
    assert assigned_retrieve_mode(
        {"search_mode_decision": {"search_mode": "local"}}
    ) == "local"
    for raw in (*RETIRED_LETTERS, "local"):
        with pytest.raises(InvalidSearchModeError):
            resolve_search_mode(assigned_retrieve_mode(
                {"search_mode_decision": {"search_mode": raw}}
            ))
    # ``sql`` keeps ITS own name through this hop and the port seam now —
    # #322 T7 (6e8ed97) flipped the old pin, which asserted
    # ``pytest.raises(TierUnavailableError)`` for sql. The refusal table is
    # empty, so every closed-set token forwards verbatim and
    # ``TierUnavailableError`` has no reachable raise site left (the class
    # stays as the machinery for a future plan slot, pinned by
    # ``tests/retrieve/``).
    assert assigned_retrieve_mode(
        {"search_mode_decision": {"search_mode": "sql"}}
    ) == "sql"
    for token in RETRIEVE_TIERS:
        assert resolve_search_mode(token) == token, token


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"search_mode_decision": {}},
        {"search_mode_decision": None},
        {"search_mode_decision": {"search_mode": None}},
    ],
)
def test_an_absent_audit_sends_no_档(state: Any) -> None:
    assert assigned_retrieve_mode(state) is None


def test_a_non_mapping_audit_is_named_by_the_port_not_swallowed() -> None:
    """A channel value that is not the audit mapping means a boundary was
    skipped, so it is NOT the same thing as "no 档 was assigned": the hop
    forwards it verbatim and :func:`resolve_search_mode` names the run
    ``invalid_search_mode``. Returning ``None`` here would quietly hand the
    turn to iknow's own default 档 — a silent assignment the spec forbids
    (SC3 / SC6), and the tool would report hits nobody asked for."""
    forwarded = assigned_retrieve_mode({"search_mode_decision": "B"})

    assert forwarded == "B"
    with pytest.raises(InvalidSearchModeError) as excinfo:
        resolve_search_mode(forwarded)
    assert excinfo.value.code == "invalid_search_mode"


# ----- the wire shape (SC2, not just the port seam) -----


class _CapturingMcpAdapter(MCPRetrieveAdapter):
    """Records the JSON-RPC payload a run would put on the wire.

    Skips the parent ``__init__`` exactly like the scripted adapter in
    ``tests/retrieve/test_mcp_adapter.py``, so no subprocess is
    allocated: the only fact under test is what the host PUT IN the
    request, which is the other half of the hop.
    """

    def __init__(self) -> None:
        self._command = None
        self._locator_map: dict[str, Locator] = {}
        self._known_doc_ids: frozenset[str] | None = None
        self._timeout = 5.0
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        return {"hits": []}


def _arguments(adapter: _CapturingMcpAdapter) -> list[dict[str, Any]]:
    return [payload["params"]["arguments"] for payload in adapter.payloads]


def test_a_grounded_run_puts_the_档_on_the_wire_filters() -> None:
    adapter = _CapturingMcpAdapter()
    out = _run(_compile(adapter), _utterance("graph"), "wire-graph")

    assert out["search_mode_decision"]["search_mode"] == "graph"
    arguments = _arguments(adapter)
    assert arguments, "the grounded retrieve reached the wire"
    assert all(
        arg["filters"]["search_mode"] == "graph" for arg in arguments
    ), arguments


def test_an_unassigned_turn_puts_no_档_key_on_the_wire() -> None:
    """The absent half AT THE WIRE, re-sourced by #322 T7 (6e8ed97): the
    old case drove it with a refused sql turn, but refusals have no
    producer now, so the genuinely ABSENT assignment is the clarify
    residual — no 档 named, judge absent → ``search_mode=None`` → no key,
    so iknow answers with its own default instead of a 档 fin invented."""
    adapter = _CapturingMcpAdapter()
    out = _run(_compile(adapter), PLAIN_ASK, "wire-unassigned")

    assert out["search_mode_decision"]["search_mode"] is None
    arguments = _arguments(adapter)
    assert arguments, "the scripted retrieve still ran"
    assert all(
        "search_mode" not in arg.get("filters", {})
        for arg in arguments
    ), arguments


def test_an_assigned_sql_turn_never_puts_a_substitute_档_on_the_wire() -> None:
    """SC3/SC6 at the WIRE half: an assigned sql turn may only ever ride
    ``sql`` — never a graph/lexical substitute. Since A2 the MCP seam
    additionally requires the contract-expiry window, so an unwindowed
    sql request is refused ``invalid_business_query`` BEFORE anything
    crosses the wire (the exact gate is pinned by
    ``tests/host/test_sql_row_evidence.py``); what this pins is that the
    refused request never gets rewritten onto another 档 on the way."""
    adapter = _CapturingMcpAdapter()
    out = _run(_compile(adapter), _utterance("sql"), "wire-sql")

    assert out["search_mode_decision"]["search_mode"] == "sql"
    arguments = _arguments(adapter)
    # Either nothing crossed (window gate refused by name) or what
    # crossed carries sql — no other token may appear, ever.
    for arg in arguments:
        assert arg.get("filters", {}).get("search_mode", "sql") == "sql", arg


def test_the_档_is_not_a_model_visible_tool_argument() -> None:
    tool = _make_retrieve_tool(_port())
    assert "search_mode" not in tool.args, tool.args
    assert "state" not in tool.args, tool.args


# ----- the sql window as a model-supplied tool argument (T1 / T2) -----


class _RecordingWindowAdapter(_CapturingMcpAdapter):
    """Records the exact kwargs the host forwarded, THEN runs the real gate.

    The record is taken at the port's own entry, before
    ``resolve_sql_window`` refuses, so a window that is refused is still
    proven to have reached the gate spelled the way the model spelled it.
    Ordering is the whole point: recording after the gate would prove
    nothing for every case this file is here to cover.
    """

    def __init__(self) -> None:
        super().__init__()
        self.forwarded: list[dict[str, Any]] = []

    def retrieve(  # type: ignore[override]
        self, query: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.forwarded.append(dict(kwargs))
        return super().retrieve(query, **kwargs)


def _sql_ask() -> str:
    # ``PLAIN_ASK`` keeps the TRACK grounded, so the 档 cascade actually
    # runs and assigns the named 档; a bare sql question without it lands
    # on the clarify residual and never reaches the port.
    return f"sql档 AAPL net sales 2023 Q3, which contracts expire?"


def _window_script(**args: Any) -> list[AIMessage]:
    """One scripted MODEL turn whose retrieve call carries ``args``."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue", **args},
                    "id": "c-window",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Per the filings: revenue grew."),
    ]


def _run_with_args(
    adapter: Any, args: dict[str, Any], thread_id: str
) -> dict[str, Any]:
    return _run(
        _compile(adapter, model=ScriptedChatModel(script=_window_script(**args))),
        _sql_ask(),
        thread_id,
    )


def test_the_sql_window_arguments_are_model_visible() -> None:
    """T1: the model supplies the pair, so the two arguments and their
    format guidance are part of the tool's contract surface. LangChain
    derives ``tool.args`` from the signature; the tool description is the
    text the model reads about the tool as a whole, and each property's
    own ``description`` is what rides next to that argument in the
    tool-call schema the model fills in (contract rule 7)."""
    tool = _make_retrieve_tool(_port())

    assert "sql_window_start" in tool.args, tool.args
    assert "sql_window_end" in tool.args, tool.args

    schema = cast(Any, tool.tool_call_schema).model_json_schema()
    properties = schema["properties"]
    for name in ("sql_window_start", "sql_window_end"):
        description = properties[name].get("description", "")
        assert description.strip(), (
            name,
            "carries no description in the tool-call schema, so the "
            "contract's per-argument guidance has no surface to live on",
            properties[name],
        )
        assert "YYYY-MM-DD" in description, (name, description)
        assert "both" in description.lower(), (name, description)

    description = tool.description or ""
    # The load-bearing instruction is pinned case-sensitively and in
    # full: "omit" alone is trivia (``attachment_ids`` already used it),
    # so only the both-or-neither rule itself is asserted here.
    assert "pass both or neither" in description, description
    assert "OMIT both" in description, description


def test_a_model_supplied_window_reaches_the_wire_as_the_exact_bounds() -> None:
    """T2: the two arguments ride the ONE existing port call and bind
    into the adapter filters verbatim — no reformat, no reordering, no
    bound invented.

    The half this file covers is the transport SHAPE. The live
    business-ledger leg is a separate file,
    ``tests/retrieve/test_live_sql_window_user_path.py``, which spends
    the same model-supplied pair against the real producer.
    """
    adapter = _CapturingMcpAdapter()
    out = _run_with_args(
        adapter,
        {"sql_window_start": "2026-01-01", "sql_window_end": "2026-04-30"},
        "wire-sql-window",
    )

    assert out["search_mode_decision"]["search_mode"] == "sql"
    sql_calls = [
        a for a in _arguments(adapter)
        if a.get("filters", {}).get("search_mode") == "sql"
    ]
    assert sql_calls, _arguments(adapter)
    for arg in sql_calls:
        assert arg["filters"]["sql_window_start"] == "2026-01-01", arg
        assert arg["filters"]["sql_window_end"] == "2026-04-30", arg


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({}, id="missing"),
        pytest.param({"sql_window_start": "2026-01-01"}, id="half"),
        pytest.param(
            {"sql_window_start": "2026-01-01", "sql_window_end": "later"},
            id="malformed",
        ),
        pytest.param(
            {"sql_window_start": "20260101", "sql_window_end": "2026-04-30"},
            id="basic-form",
        ),
        pytest.param(
            {"sql_window_start": "2026-04-30", "sql_window_end": "2026-01-01"},
            id="reversed",
        ),
    ],
)
def test_an_unbindable_model_window_is_refused_by_name_before_the_wire(
    args: dict[str, Any],
) -> None:
    """T1: every unusable pair REACHES ``resolve_sql_window`` with exactly
    the values the model spelled, and is refused ``invalid_business_query``
    before the transport.

    The forwarding assertion is the load-bearing half. The envelope alone
    cannot tell this seam from the base tree: pydantic drops unknown tool
    arguments, so a host that never declared the pair would reach the same
    gate with ``None`` and produce the same named refusal. Asserting the
    exact supplied values is what makes the case fail if the wiring is
    reverted — including the malformed and reversed spellings, which are
    values only a forwarding implementation can carry.
    """
    adapter = _RecordingWindowAdapter()
    out = _run_with_args(adapter, args, "wire-sql-refused")

    assert out["search_mode_decision"]["search_mode"] == "sql"

    assert adapter.forwarded, "the port was never reached"
    for call in adapter.forwarded:
        assert call["search_mode"] == "sql", call
        # The ``missing`` case pins that NO bound was invented to fill the
        # pair: the port receives both keys, spelled as ``None``, rather
        # than one of them defaulted. That is this slot's new surface, and
        # it is not what the pre-existing 档-only sql case asserts.
        assert "sql_window_start" in call and "sql_window_end" in call, call
        assert call["sql_window_start"] == args.get("sql_window_start"), call
        assert call["sql_window_end"] == args.get("sql_window_end"), call

    for arg in _arguments(adapter):
        filters = arg.get("filters", {})
        assert filters.get("search_mode") != "sql", arg
        assert "sql_window_start" not in filters, arg
        assert "sql_window_end" not in filters, arg
    refusals = [
        m.content
        for m in out["messages"]
        if getattr(m, "type", "") == "tool"
        and isinstance(m.content, str)
        and "invalid_business_query" in m.content
    ]
    assert refusals, [m.content for m in out["messages"]]


def test_a_document_档_never_learns_the_window_keys() -> None:
    """The window keys are the sql 档's alone. The model DID supply them
    and the port DID receive them — asserted here, so the case fails if the
    forwarding is reverted instead of passing vacuously on a tree that
    never emits the keys — and the adapter still drops them before the
    wire, so the document envelope stays byte-identical."""
    adapter = _RecordingWindowAdapter()
    graph = _compile(
        cast(Any, adapter),
        model=ScriptedChatModel(
            script=_window_script(
                sql_window_start="2026-01-01", sql_window_end="2026-04-30"
            )
        ),
    )
    out = _run(graph, _utterance("lexical"), "wire-lexical-window")

    assert out["search_mode_decision"]["search_mode"] == "lexical"
    assert adapter.forwarded, "the port was never reached"
    for call in adapter.forwarded:
        assert call["sql_window_start"] == "2026-01-01", call
        assert call["sql_window_end"] == "2026-04-30", call
    arguments = _arguments(adapter)
    assert arguments, "the scripted retrieve still ran"
    for arg in arguments:
        assert "sql_window_start" not in arg.get("filters", {}), arg
        assert "sql_window_end" not in arg.get("filters", {}), arg


def test_the_graph_still_binds_exactly_one_retrieve_tool() -> None:
    """ADR 0061: routing the 档 must not add a second retrieve surface."""
    port = _port()
    model = ScriptedChatModel(script=[])
    compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=KNOWN_IDS,
    )
    names = [getattr(t, "name", repr(t)) for t in model.bound_tools]
    assert names.count("retrieve") == 1


# ----- the cache-safety property (constraint 2) -----


def test_two_runs_on_one_cached_graph_carry_their_own_档() -> None:
    """The compiled graph is cached and reused across runs, so a 档
    captured at compile time would leak run 1's choice into run 2. One
    graph object, three runs, three different wire values."""
    port = _port()
    checkpointer = InMemorySaver()
    model = ScriptedChatModel(script=_script())
    graph = _compile(port, checkpointer, model)
    # The cache really is in play: a second compile with the same
    # identity objects is served from it, so it is the SAME proxy whose
    # tool closures would carry a captured 档.
    assert compile_graph(
        checkpointer=checkpointer,
        model=model,
        adapter=port,
        known_document_ids=KNOWN_IDS,
    ) is graph

    expected = [
        ("graph", "graph"),
        ("lexical", "lexical"),
        ("global", "global"),
    ]
    for spelling, lasting in expected:
        model._index = 0
        model.script = _script()
        before = len(port.calls)
        _run(graph, _utterance(spelling), f"leak-{spelling}")
        modes = _modes(port, since=before)
        assert modes and modes == [lasting] * len(modes), (spelling, modes)

    # All three opened 档 rode their OWN run's call — three distinct
    # lasting tokens on one cached graph, no leakage between runs, and a
    # retired letter never reached the wire at all.
    assert [m for m in _modes(port) if m is not None] == [
        "graph",
        "lexical",
        "global",
    ]
    assert not set(_modes(port)) & set(RETIRED_LETTERS)
