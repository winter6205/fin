"""Nothing retired leaves the host toward the retrieve port (rename).

The rename is only real once the value a run hands the port is the
lasting token. The port seam keeps EXACT matching (a bad send must stay
a named mistake, not a guess), so the whole burden is on the host side:
every entry point — an utterance naming a 档, an HTTP pin, a choice-window
pick, the cascade's own output — resolves through
``fin.retrieve.tiers.normalize_tier_token`` BEFORE anything downstream can
read the value.

With the letters retired outright there is nothing left to resolve FROM, so
this file pins both halves of that: the word forms still reach the port as
themselves, and a retired letter no longer reaches it as anything — at the
utterance, the pin and the seam alike.

Opt-in (``uv run pytest -q -m soft_track``; deselected by default).
Scripted model + :class:`~fin.retrieve.fake_port.FakeRetrievePort` only:
what is asserted is the TOKEN a call carries, never how iknow ranks
(Fake is not a 档).

* ``test_an_assigned_tier_records_its_token_before_retrieve`` — the
  audit lands at the classify node, i.e. before the port sees a call,
  and every call the port records carries exactly that lasting token.
* ``test_a_pinned_letter_is_a_named_exit_and_routes_nothing`` — the same
  seam an old client used to pin with a letter.
* ``test_a_newly_opened_ask_assigns_its_token_and_starts_no_letter`` —
  the plan slot that #322 T7 opened (``sql``) now reads as a plain
  assignment on every audit field, and the typed-fail shape no longer
  fires for ANY closed-set naming.
* ``test_the_value_handed_to_the_port_is_never_a_letter`` — the port
  seam's own view of what normalization produces.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.host.agent_instructions import SEARCH_MODE_INSTRUCTIONS_MARKER
from fin.host.graph import compile_graph
from fin.host.named_failures import (
    LASTING_RETRIEVE_TIERS,
    TIER_UNAVAILABLE_CODE,
)
from fin.host.search_mode_decision import (
    decide_search_mode,
    normalize_search_mode,
)
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import reset_input_state
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import (
    InvalidSearchModeError,
    TierUnavailableError,
    resolve_search_mode,
)
from search_mode_pins import RETIRED_LETTERS

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3"}

# Every spelling a request can arrive with for each OPENED 档: the lasting
# token, a case variant, and the CONTEXT label an utterance names it with.
# ``global`` joined this set when fin task 4 opened 综述 into a real report
# arm; ``sql`` joined it at #322 T7 (commit 6e8ed97), when 真数据库 became a
# real arm too — every lasting token in the closed set now routes itself to
# the port, and no plan slot is left to refuse (``TIER_UNAVAILABLE_CODE``
# is empty; the mechanism stays for a future slot).
OPENED_SPELLINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("graph", ("graph", "GRAPH", "知识问答")),
    ("lexical", ("lexical", "LEXICAL", "查找")),
    ("global", ("global", "Global", "综述")),
    ("sql", ("sql", "SQL", "真数据库")),
)
# Formerly the unopened plan slot with the 具名失败 its name yielded
# (``("sql", REAL_DB_NOT_WIRED, ...)``); #322 T7 moved sql into
# OPENED_SPELLINGS and this set is EMPTY — the typed-fail entry point
# below keeps testing it as a NEGATIVE invariant (nothing in the closed
# set can produce it).
UNOPENED_SPELLINGS: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
# A pin is a WIRE field, so only its token spellings can arrive; the
# CONTEXT label is an utterance form the pin seam never sees.
PIN_SPELLINGS: tuple[str, ...] = ("graph", "GRAPH")


class _RecordingScriptedModel(ScriptedChatModel):
    """Captures each model call's message payload (ordering proof below).

    ``_stream`` without a stream script delegates to ``_generate``, so
    overriding ``_generate`` covers both surfaces the graph uses.
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


# Track rules ground a research ask; this is the same shape the cascade
# suite uses, so the only variable under test is the 档 spelling.
PLAIN_ASK = "what was AAPL net sales in 2023 Q3?"


def _utterance(spelling: str) -> str:
    return f"{spelling}档 {PLAIN_ASK}"


def _port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={"revenue": []},
        known_document_ids=KNOWN_IDS,
    )


def _retrieve_then_answer() -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue"},
                    "id": "c-retrieve-normalize",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Per the filings: revenue grew."),
    ]


def _run(
    text: str, *, pinned: str | None = None
) -> tuple[dict[str, Any], FakeRetrievePort, _RecordingScriptedModel]:
    """One grounded run whose retrieve call lands on a recording port."""
    port = _port()
    model = _RecordingScriptedModel(script=_retrieve_then_answer())
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=KNOWN_IDS,
    )
    configurable: dict[str, Any] = {"thread_id": f"np-{pinned or 'utterance'}"}
    if pinned is not None:
        configurable["pinned_search_mode"] = pinned
    out = graph.invoke(
        {**reset_input_state(text), "evidence": []},
        config={"configurable": configurable},
    )
    return out, port, model


def _mode_copy_blocks(model: _RecordingScriptedModel) -> list[str]:
    return [
        str(call[0].content)
        for call in model.captured
        if call and SEARCH_MODE_INSTRUCTIONS_MARKER in str(call[0].content)
    ]


def _retired_letter_in(payload: Any) -> bool:
    """True if any string in the audit is EXACTLY a retired letter: the
    trail must read as the lasting tokens the port will carry."""
    if isinstance(payload, str):
        return payload in RETIRED_LETTERS
    if isinstance(payload, dict):
        return any(_retired_letter_in(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_retired_letter_in(value) for value in payload)
    return False


# ----- the assigned-档 acceptance halves -----


@pytest.mark.parametrize(
    "lasting, spellings",
    OPENED_SPELLINGS,
    ids=[lasting for lasting, _ in OPENED_SPELLINGS],
)
def test_an_assigned_tier_records_its_token_before_retrieve(
    lasting: str, spellings: tuple[str, ...]
) -> None:
    """The 档 is recorded as its lasting token and the port is handed that
    token. Ordering is proven, not assumed: the model call that emitted the
    retrieve tool call already carried this 档's usage copy, so the
    assignment existed before the port call it describes."""
    for spelling in spellings:
        out, port, model = _run(_utterance(spelling))

        assert out["track"] == "grounded"
        audit = out["search_mode_decision"]
        assert audit["search_mode"] == lasting, spelling
        assert not _retired_letter_in(audit)
        assert port.calls, "the grounded retrieve must have reached the port"
        # POSITIVE: the assigned 档 IS the value the port was handed — not
        # merely "not a letter", which an unassigned run would satisfy too.
        assert [call["search_mode"] for call in port.calls] == [
            lasting
        ] * len(port.calls), [call["search_mode"] for call in port.calls]
        # The retrieve call is the SECOND model call of this run: the copy on
        # the first one is the audit's own token, visible before the port ran.
        blocks = _mode_copy_blocks(model)
        assert blocks and all(
            f"检索档 {lasting}" in block for block in blocks
        ), (spelling, blocks)


def test_a_newly_opened_ask_assigns_its_token_and_starts_no_letter() -> None:
    """Flipped by #322 T7 (commit 6e8ed97): the old case pinned the
    ``sql`` utterance as ``typed_fail`` + ``failure_code=real_db_not_wired``
    + ``search_mode=None`` handed to the port. The 档 is open now, so
    naming it ASSIGNS it — the audit reads the lasting token on every
    field — and the typed-fail shape is kept as a NEGATIVE invariant:
    with the refusal table empty, no closed-set naming anywhere (token,
    case variant, or CONTEXT label) can produce ``typed_fail``, a failure
    code, a substitute 档, or a retired letter in the trail."""
    assert UNOPENED_SPELLINGS == ()
    assert TIER_UNAVAILABLE_CODE == {}

    for lasting, _spellings in OPENED_SPELLINGS:
        for spelling in _spellings:
            out, port, _model = _run(_utterance(spelling))

            audit = out["search_mode_decision"]
            assert audit["decision_tier"] == "rules", spelling
            assert audit["search_mode"] == lasting, spelling
            assert audit["failure_code"] is None, spelling
            assert not _retired_letter_in(audit)
            assert port.calls, "the grounded retrieve must have reached the port"
            # POSITIVE: the assigned 档 IS the value the port was handed —
            # never ``None`` (that was the refusal shape) and never some
            # other 档 (the substitute trap).
            assert [
                call["search_mode"] for call in port.calls
            ] == [lasting] * len(port.calls), [
                call["search_mode"] for call in port.calls
            ]
            assert all(
                call["search_mode"] not in RETIRED_LETTERS
                for call in port.calls
            )


@pytest.mark.parametrize("spelling", PIN_SPELLINGS)
def test_a_pinned_tier_hands_the_port_its_lasting_token(
    spelling: str,
) -> None:
    """Whatever the case the client spelled, the run's audit and anything
    the port is handed carry the lasting token."""
    out, port, _model = _run(PLAIN_ASK, pinned=spelling)

    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["signals"].get("pinned") is True
    assert not _retired_letter_in(audit)
    # POSITIVE: a pin is an assignment, so the port is handed its lasting
    # token — nothing may rewrite it on the way.
    assert port.calls, "the grounded retrieve must have reached the port"
    assert [call["search_mode"] for call in port.calls] == [
        "graph"
    ] * len(port.calls), [call["search_mode"] for call in port.calls]
    assert all(
        call["search_mode"] not in RETIRED_LETTERS for call in port.calls
    )


@pytest.mark.parametrize("letter", RETIRED_LETTERS)
def test_a_pinned_letter_is_a_named_exit_and_routes_nothing(
    letter: str,
) -> None:
    """The pin seam keeps its old client honest by name, not by guess: a
    retired letter is out of the closed set, so the cascade records the
    ``invalid_search_mode`` EXIT and hands no 档 to the port."""
    out, port, _model = _run(PLAIN_ASK, pinned=letter)

    audit = out["search_mode_decision"]
    assert audit["signals"].get("pin_exit") == "invalid_search_mode", letter
    assert audit["signals"].get("pin") == letter
    assert audit["search_mode"] is None
    # The residual may still be a clarify (no judge injected here), which is
    # the same "invent nothing" answer the closed set gives everywhere else.
    assert [call["search_mode"] for call in port.calls] == [None] * len(
        port.calls
    ), [call["search_mode"] for call in port.calls]


@pytest.mark.parametrize("letter", RETIRED_LETTERS)
def test_an_utterance_naming_a_letter_resolves_no_tier(letter: str) -> None:
    """Zero coexistence at the entry: ``A档`` / ``D档`` is ordinary prose the
    judge and the rest of the cascade read, not a named 档 — the letter class
    left the naming pattern together with the alias map, so the rules tier
    cannot even see it as a candidate."""
    decision = decide_search_mode(
        [HumanMessage(content=f"{letter}档 {PLAIN_ASK}")]
    )
    assert decision.search_mode is None
    assert "tier_naming" not in decision.signals


# ----- the port seam's own view -----


def test_the_value_handed_to_the_port_is_never_a_letter() -> None:
    """Normalization runs strictly before the seam: the token the host
    would send resolves on the port too. The seam's exactness is
    untouched — a letter sent WITHOUT normalization is still the wire's
    own mistake to name, which is why normalize cannot live behind the
    port. Only TOKEN spellings reach this seam: the CONTEXT label is an
    utterance form the normalizer never carries (the rules tier maps it
    through ``TIER_LABELS``, the wire never sees it)."""
    for lasting, spellings in (
        ("graph", ("graph", "GRAPH", " graph ")),
        ("lexical", ("lexical", "LEXICAL")),
        ("global", ("global", "Global", " global ")),
        ("sql", ("sql", "SQL", " sql ")),
    ):
        for spelling in spellings:
            resolved = normalize_search_mode(spelling)
            assert resolved == lasting, spelling
            assert resolved in LASTING_RETRIEVE_TIERS, spelling

    assert resolve_search_mode(normalize_search_mode("GRAPH")) == "graph"
    assert resolve_search_mode(normalize_search_mode("GLOBAL")) == "global"
    # Every closed-set token — ``sql`` included since #322 T7 opened it
    # (the old pin asserted a ``TierUnavailableError`` carrying
    # ``real_db_not_wired`` for it) — crosses the seam as itself. The
    # refusal class stays as machinery for a future plan slot; with the
    # table empty it has no reachable raise site, which is what the loop
    # below proves by resolving ALL FOUR tokens without ever catching it.
    assert TIER_UNAVAILABLE_CODE == {}
    for token in LASTING_RETRIEVE_TIERS:
        try:
            assert resolve_search_mode(token) == token, token
        except TierUnavailableError as exc:  # pragma: no cover - the trap
            raise AssertionError(
                f"{token} still refused at the seam: {exc.code}"
            ) from exc

    # And the seam keeps its own name for a letter that arrives UN-normalized:
    # the closed set is all the port forwards, so a leftover letter is a
    # wiring bug it refuses rather than a 档 it quietly reroutes.
    for letter in RETIRED_LETTERS:
        try:
            resolve_search_mode(letter)
        except InvalidSearchModeError as exc:
            assert exc.code == "invalid_search_mode", letter
        else:
            raise AssertionError(f"a retired letter {letter!r} crossed the seam")
